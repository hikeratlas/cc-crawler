#!/usr/bin/env python3
"""Stage 9b: one row per hike, from extract.py outputs.

Takes the `hike` record of every trip report and trail guide, keeps those
with a location and at least one destination or trail, joins the site's
kind (personal / organisation, from page_pass.py's sites.tsv.gz) for
curation, and writes hikes.tsv.gz plus a markdown summary. Stdlib only.

Usage:
  scripts/hikes.py --in 'extracted-*.jsonl.gz' [--sites sites.tsv.gz] --out-dir out/
"""
import argparse
import csv
import glob
import gzip
import json
import math
import os
import random
import re
from collections import Counter, defaultdict

# Pages that aren't one hike even when typed trip_report / trail_guide.
INDEX_TITLE = re.compile(r"\b(archives?|categor(?:y|ies)|tag(?:ged)?|posts|search results|page \d+|"
                         r"destinations|recent|latest)\b|^(?:blog|home|hiking|hikes|trails?|backpacking)\b\s*(?:[-|–]|$)", re.I)
INDEX_URL = re.compile(r"/(tag|tags|category|categories|archive|archives|author|page/\d+|search)(/|$)|[?&]page=", re.I)
# Blog date archives: /2017/, /2017/07/, /2017/07/14/ with nothing after, Blogger's
# 2010_12_01_archive.html and ?updated-max= listings. The deep (all-mode) harvest
# brings these in; each lists many posts, so its features are rarely one hike.
DATE_URL = re.compile(r"/(?:19|20)\d\d(?:/[01]\d(?:/[0-3]\d)?)?/?(?:[?#]|$)|_archive\.html|[?&]updated-max=", re.I)
PLURAL = r"(?:hikes|trails|walks|waterfalls|falls|things|places|views|spots|lakes|peaks|summits|routes|adventures)"
LIST_TITLE = re.compile(r"(?:^\s*|\b(?:top|best)\s+)\d{1,3}\s+(?:[\w'’-]+\s+){0,4}" + PLURAL + r"\b|"
                        r"\bbest\s+(?:[\w'’-]+\s+){0,4}" + PLURAL + r"\b|\b" + PLURAL + r"\s+(?:in|near|around)\b",
                        re.I)

# Sites left out of the hike table: aggregators, trail databases, generated
# directories, content farms and non-hiking sites. The table is for linking
# authentic, human-written posts to hikes. One row per site with a reason; see
# data/excluded_sites.csv.
EXCLUDED_SITES_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "excluded_sites.csv")


def load_excluded(path):
    with open(path, newline="") as f:
        return {r["site"].strip().lower(): r["reason"].strip() for r in csv.DictReader(f) if r["site"].strip()}


COLS = ["group_id", "name", "page_type", "page_type_p", "destinations", "destination_osm_ids", "trails", "trail_osm_ids",
        "park", "state", "lat", "lon", "distance_mi", "gain_ft", "route", "difficulty", "date",
        "site", "site_kind", "url"]


def squash(s):
    """Lower case, letters and digits only: 'The "Crack" Trail' -> 'thecracktrail'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def group_key(h, by_osm):
    """The hike's group: its main destination, else its main trail.

    Different routes to one summit share a group (the trails are listed as
    variants). A long trail alone groups by trail and state: thru-hike
    posts about the Appalachian Trail in Maine. Returns (id, info) where
    info describes the group's feature.
    """
    # A page about a named trail ("Tonquin Valley Trail", "La Cloche
    # Silhouette Trail") groups by the trail, not by whichever lake or
    # peak it mentions most: trail first when the title names a trail
    # before any destination ("Green Gardens Trail: Hiking in Gros Morne").
    title = squash(h["name"])
    pos = lambda items: min((title.find(squash(i["name"])) for i in items
                             if i.get("osm_id") and squash(i["name"]) in title), default=10 ** 6)
    order = (("destination", h["destinations"]), ("trail", h["trails"]))
    if pos(h["trails"]) < pos(h["destinations"]):
        order = order[::-1]
    order += (("park", h.get("parks") or []),)  # a small park or preserve as the hike
    for kind, items in order:
        for it in items:
            if it.get("osm_id"):
                f = by_osm.get(it["osm_id"], {})
                return (f"osm:{it['osm_id']}",
                        {"group_kind": kind, "name": it["name"], "feature_kind": it["kind"], "osm_id": it["osm_id"],
                         "lat": f.get("lat", ""), "lon": f.get("lon", ""), "ele_m": f.get("ele_m", ""),
                         "state": f.get("state") or h["state"] or "", "park": f.get("park") or h["park"] or ""})
    for it in h["trails"]:
        if it["kind"] == "long_trail":
            st = h["state"] or ""
            return (f"long:{it['name']}:{st}",
                    {"group_kind": "long_trail", "name": f"{it['name']} ({st})" if st else it["name"],
                     "feature_kind": "long_trail", "osm_id": "", "lat": "", "lon": "", "ele_m": "",
                     "state": st, "park": ""})
    return None


GCOLS = ["group_id", "group_kind", "name", "feature_kind", "osm_id", "state", "park", "lat", "lon", "ele_m",
         "sites", "personal_sites", "pages", "trip_reports", "trail_guides", "median_distance_mi",
         "median_gain_ft", "trails", "merged_osm_ids", "example_urls"]


def median(vals):
    vals = sorted(vals)
    if not vals:
        return ""
    m = len(vals) // 2
    v = vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2
    return round(v, 1)


def build_groups(rows):
    groups = {}
    for r in rows:
        g = r.pop("_group")
        if not g:
            r["group_id"] = ""
            continue
        gid, info = g
        r["group_id"] = gid
        G = groups.setdefault(gid, {**info, "group_id": gid, "rows": []})
        G["rows"].append(r)
    groups = merge_same_name(groups)
    for G in groups.values():
        for r in G["rows"]:
            r["group_id"] = G["group_id"]
    out = []
    for G in groups.values():
        rs = G.pop("rows")
        sites = {r["site"] for r in rs}
        personal = {r["site"] for r in rs if r["site_kind"] == "personal"}
        own = " ".join(NAME_NOISE.sub(" ", G["name"].lower()).split())
        trails = Counter(t for r in rs for t in r["trails"].split("; ")
                         if t and " ".join(NAME_NOISE.sub(" ", t.lower()).split()) != own)
        # Examples: one page per site, personal sites and trip reports first.
        ex, seen_sites = [], set()
        for r in sorted(rs, key=lambda r: (r["site_kind"] != "personal", r["page_type"] != "trip_report",
                                          -float(r["page_type_p"] or 0))):
            if r["site"] not in seen_sites:
                seen_sites.add(r["site"])
                ex.append(r["url"])
        out.append({**G, "sites": len(sites), "personal_sites": len(personal), "pages": len(rs),
                    "trip_reports": sum(r["page_type"] == "trip_report" for r in rs),
                    "trail_guides": sum(r["page_type"] == "trail_guide" for r in rs),
                    "median_distance_mi": median([float(r["distance_mi"]) for r in rs if r["distance_mi"] != ""]),
                    "median_gain_ft": median([float(r["gain_ft"]) for r in rs if r["gain_ft"] != ""]),
                    "trails": "; ".join(f"{t} ({c})" for t, c in trails.most_common(5)),
                    "example_urls": " ".join(ex[:5])})
    out.sort(key=lambda g: (-g["sites"], -g["pages"], g["name"]))
    return out


NAME_NOISE = re.compile(r"\b(trail|trailhead|trails|loop|hike|path|the)\b")


def km(a, b):
    x = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return 6371 * math.hypot(x, math.radians(b[0] - a[0]))


def merge_same_name(groups, radius_km=15):
    """One trail is often several OSM ways, and the index has both "Bright
    Angel" (a route) and "Bright Angel Trail". Merge groups whose names match
    (ignoring trail/loop/hike) and lie within radius_km of each other,
    chaining, so a long trail's segments end up together. The merged group
    keeps the identity of its biggest member."""
    by_name = defaultdict(list)
    for gid, G in groups.items():
        if G["lat"] != "" and G["lon"] != "":
            by_name[" ".join(NAME_NOISE.sub(" ", G["name"].lower()).split())].append(gid)
    parent = {gid: gid for gid in groups}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for ids in by_name.values():
        for i, a in enumerate(ids):
            pa = (float(groups[a]["lat"]), float(groups[a]["lon"]))
            for b in ids[i + 1:]:
                if km(pa, (float(groups[b]["lat"]), float(groups[b]["lon"]))) <= radius_km:
                    parent[find(a)] = find(b)
    merged = defaultdict(list)
    for gid in groups:
        merged[find(gid)].append(gid)
    out = {}
    for ids in merged.values():
        ids.sort(key=lambda g: (-len({r["site"] for r in groups[g]["rows"]}),
                                groups[g]["feature_kind"] not in ("peak", "lake", "waterfall", "cliff")))
        head = dict(groups[ids[0]])
        head["rows"] = [r for g in ids for r in groups[g]["rows"]]
        head["merged_osm_ids"] = " ".join(str(groups[g]["osm_id"]) for g in ids[1:])
        out[head["group_id"]] = head
    return out


def write_groups(groups, out_dir):
    with gzip.open(os.path.join(out_dir, "hike_groups.tsv.gz"), "wt") as f:
        f.write("\t".join(GCOLS) + "\n")
        for g in groups:
            f.write("\t".join(str(g[c]).replace("\t", " ") for c in GCOLS) + "\n")


LCOLS = ["osm_id", "primary", "role", "feature", "feature_kind", "state", "group_id", "url", "title", "site",
         "site_kind", "page_type", "date", "distance_mi", "gain_ft"]


def url_key(u):
    """http/https, www., /index.php and a trailing slash don't make a different post."""
    u = re.sub(r"^https?://(www\.)?", "", u.strip(), flags=re.I)
    return u.replace("/index.php/", "/").rstrip("/").lower()


def write_links(rows, groups, out_dir):
    """links.tsv.gz: one row per (OSM feature, post), for showing posts on a
    feature's page. primary = 1 for the feature the post's group is about
    (and the features merged into that group); the other destinations,
    trails and parks the post names get primary = 0."""
    by_gid = {g["group_id"]: g for g in groups}
    links = {}
    for r in rows:
        feats = []
        g = by_gid.get(r["group_id"])
        if g and g["osm_id"] != "":
            for oid in [g["osm_id"]] + g["merged_osm_ids"].split():
                feats.append((str(oid), 1, g["group_kind"], g["name"], g["feature_kind"]))
        feats += [(str(oid), 0, role, name, kind) for oid, role, name, kind in r.pop("_features")]
        for oid, prim, role, name, kind in feats:
            k = (oid, url_key(r["url"]))
            if k in links and links[k]["primary"] >= prim:
                continue
            links[k] = {"osm_id": f"osm:{oid}", "primary": prim, "role": role, "feature": name, "feature_kind": kind,
                        "state": r["state"], "group_id": r["group_id"], "url": r["url"], "title": r["name"],
                        "site": r["site"], "site_kind": r["site_kind"], "page_type": r["page_type"],
                        "date": r["date"], "distance_mi": r["distance_mi"], "gain_ft": r["gain_ft"]}
    out = sorted(links.values(), key=lambda l: (l["osm_id"], -l["primary"], l["site_kind"] != "personal", l["url"]))
    with gzip.open(os.path.join(out_dir, "links.tsv.gz"), "wt") as f:
        f.write("\t".join(LCOLS) + "\n")
        for l in out:
            f.write("\t".join(str(l[c]).replace("\t", " ") for c in LCOLS) + "\n")
    return len(out), len({l["osm_id"] for l in out})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inputs", nargs="+", required=True)
    ap.add_argument("--sites", help="sites.tsv.gz from page_pass.py (adds site_kind)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--exclude", default=EXCLUDED_SITES_CSV, help="CSV of sites to leave out (site,reason,notes)")
    args = ap.parse_args()
    excluded = load_excluded(args.exclude)

    kinds = {}
    if args.sites:
        with gzip.open(args.sites, "rt") as f:
            # A site can appear in several page passes (the file concatenates them) with
            # different kinds; the pass that fetched the most of its pages wins, so the
            # result doesn't depend on file order.
            most = {}
            for r in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
                n = int(r.get("fetched") or 0)
                if r["site"] not in most or n > most[r["site"]]:
                    most[r["site"]], kinds[r["site"]] = n, r["site_kind"]

    os.makedirs(args.out_dir, exist_ok=True)
    seen = Counter()
    urls_seen = set()
    rows = []
    for path in sorted({p for pat in args.inputs for p in glob.glob(pat)}):
        with gzip.open(path, "rt") as f:
            for line in f:
                r = json.loads(line)
                h = r.get("hike")
                if not h:
                    continue
                if r["url"] in urls_seen:
                    continue  # the same page in two combined batches
                urls_seen.add(r["url"])
                seen["hike pages"] += 1
                if r["site"] in excluded:
                    seen[f"dropped: excluded site ({excluded[r['site']]})"] += 1
                    continue
                title = r["title"] or ""
                if INDEX_TITLE.search(title) or INDEX_URL.search(r["url"]) or DATE_URL.search(r["url"]):
                    seen["dropped: index or archive page"] += 1
                    continue
                if LIST_TITLE.search(r["title"] or ""):
                    seen["dropped: list title"] += 1
                    continue
                if not r["location"]:
                    seen["dropped: no location"] += 1
                    continue
                if not (h["destinations"] or h["trails"] or h.get("parks")):
                    seen["dropped: no destination or trail"] += 1
                    continue
                L = r["location"]
                by_osm = {f["osm_id"]: f for f in r["features"] if f.get("osm_id")}
                rows.append({
                    "_group": group_key(h, by_osm),
                    "_features": [(it["osm_id"], role, it["name"], it.get("kind", ""))
                                  for role, items in (("destination", h["destinations"]), ("trail", h["trails"]),
                                                      ("park", h.get("parks") or []))
                                  for it in items if it.get("osm_id")],
                    "name": h["name"].replace("\t", " "), "page_type": r["page_type"],
                    "page_type_p": r.get("page_type_p", ""),
                    "destinations": "; ".join(d["name"] for d in h["destinations"]),
                    "destination_osm_ids": "; ".join(str(d["osm_id"]) for d in h["destinations"]),
                    "trails": "; ".join(t["name"] for t in h["trails"]),
                    "trail_osm_ids": "; ".join(str(t["osm_id"] or "") for t in h["trails"]),
                    "park": h["park"] or "", "state": h["state"] or "",
                    "lat": L["lat"] if L["lat"] is not None else "", "lon": L["lon"] if L["lon"] is not None else "",
                    "distance_mi": h["distance_mi"] or "", "gain_ft": h["gain_ft"] or "",
                    "route": h["route"] or "", "difficulty": h["difficulty"] or "", "date": h["date"] or "",
                    "site": r["site"], "site_kind": kinds.get(r["site"], ""), "url": r["url"],
                })
    groups = build_groups(rows)
    write_groups(groups, args.out_dir)
    n_links, n_linked = write_links(rows, groups, args.out_dir)
    with gzip.open(os.path.join(args.out_dir, "hikes.tsv.gz"), "wt") as f:
        f.write("\t".join(COLS) + "\n")
        for row in rows:
            f.write("\t".join(str(row[c]) for c in COLS) + "\n")

    # ---- summary
    n = len(rows)
    pct = lambda v: f"{100 * v / n:.0f}%" if n else "–"
    L = []
    w = L.append
    w("# Stage 9b report: hikes\n")
    w(f"Generated by `scripts/hikes.py`. {seen['hike pages']:,} trip reports and trail guides; "
      f"{n:,} kept as hikes (located, with a destination or trail).\n")
    for k, v in seen.items():
        if k.startswith("dropped"):
            w(f"- {k}: {v:,}")
    w("\n| | hikes | share |\n|---|---:|---:|")
    for label, test in (("trip reports", lambda r: r["page_type"] == "trip_report"),
                        ("trail guides", lambda r: r["page_type"] == "trail_guide"),
                        ("personal sites", lambda r: r["site_kind"] == "personal"),
                        ("organisation sites", lambda r: r["site_kind"] == "organisation"),
                        ("with destination", lambda r: r["destinations"]),
                        ("with trail", lambda r: r["trails"]),
                        ("with distance", lambda r: r["distance_mi"] != ""),
                        ("with gain", lambda r: r["gain_ft"] != ""),
                        ("with date (trip reports)", lambda r: r["date"]),
                        ("with difficulty", lambda r: r["difficulty"])):
        v = sum(1 for r in rows if test(r))
        w(f"| {label} | {v:,} | {pct(v)} |")
    sites = Counter(r["site"] for r in rows)
    w(f"\n{len(sites):,} sites contribute hikes; the top 10 contribute "
      f"{sum(v for _, v in sites.most_common(10)):,}.\n")
    w("### Largest organisation sites\n")
    w("Check these for aggregators, trail databases and generated pages; add any to "
      "`data/excluded_sites.csv`.\n")
    w("| site | hikes | example |\n|---|---:|---|")
    first = {}
    for r in rows:
        first.setdefault(r["site"], r)
    org = [(s_, v) for s_, v in sites.most_common() if first[s_]["site_kind"] != "personal"][:40]
    for s_, v in org:
        w(f"| {s_} | {v:,} | [{first[s_]['name'][:50]}]({first[s_]['url']}) |")
    w("")

    w("## Links\n")
    w(f"`links.tsv.gz` has {n_links:,} (OSM feature, post) links across {n_linked:,} features: each post is linked "
      "to the feature its group is about (`primary` = 1) and to the other destinations, trails and parks it names.\n")
    w("## Hike groups\n")
    w("Pages about the same destination (or, without one, the same trail) form one group; see "
      "`hike_groups.tsv.gz`.\n")
    w("| | groups | hikes in them |\n|---|---:|---:|")
    for label, lo, hi in (("1 site", 1, 1), ("2 sites", 2, 2), ("3–4 sites", 3, 4), ("5–9 sites", 5, 9),
                          ("10+ sites", 10, 10 ** 9)):
        gs = [g for g in groups if lo <= g["sites"] <= hi]
        w(f"| {label} | {len(gs):,} | {sum(g['pages'] for g in gs):,} |")
    w(f"| **all** | **{len(groups):,}** | **{sum(g['pages'] for g in groups):,}** |")
    gk = Counter(g["group_kind"] for g in groups)
    w("\nBy kind: " + ", ".join(f"{k} {v:,}" for k, v in gk.most_common()) + ".\n")
    w("### Groups with the most personal sites\n")
    w("| group | kind | state | sites | personal | median mi | median ft | top trails |\n"
      "|---|---|---|---:|---:|---:|---:|---|")
    for g in sorted(groups, key=lambda g: (-g["personal_sites"], -g["sites"]))[:30]:
        w(f"| {g['name']} | {g['feature_kind']} | {g['state']} | {g['sites']} | {g['personal_sites']} | "
          f"{g['median_distance_mi']} | {g['median_gain_ft']} | {g['trails'][:80]} |")
    rnd2 = random.Random(6)
    mid = [g for g in groups if 2 <= g["sites"] <= 6]
    w("\n### Random groups with 2–6 sites")
    w("")
    w("| group | kind | state | sites | pages | median mi | trails | example |\n|---|---|---|---:|---:|---:|---|---|")
    for g in rnd2.sample(mid, min(30, len(mid))):
        first = g["example_urls"].split(" ")[0]
        w(f"| {g['name']} | {g['feature_kind']} | {g['state']} | {g['sites']} | {g['pages']} | "
          f"{g['median_distance_mi']} | {g['trails'][:60]} | [link]({first}) |")
    w("")
    dest = defaultdict(lambda: [0, set(), set(), ""])
    for r in rows:
        if r["destinations"]:
            k = r["destinations"].split("; ")[0]
            d = dest[(k, r["state"])]
            d[0] += 1
            d[1].add(r["site"])
            if r["site_kind"] == "personal":
                d[2].add(r["site"])
    w("## Most-covered destinations\n")
    w("| destination | state | pages | sites | personal sites |\n|---|---|---:|---:|---:|")
    for (k, st), d in sorted(dest.items(), key=lambda kv: -len(kv[1][1]))[:40]:
        w(f"| {k} | {st} | {d[0]:,} | {len(d[1]):,} | {len(d[2]):,} |")
    w("\n## Random hikes from personal sites\n")
    w("| hike | type | destinations | trails | state | mi | ft | date |\n|---|---|---|---|---|---:|---:|---|")
    personal = [r for r in rows if r["site_kind"] == "personal"] or rows
    for r in random.Random(4).sample(personal, min(40, len(personal))):
        nm = r["name"][:60].replace("|", "/")
        w(f"| [{nm}]({r['url']}) | {r['page_type']} | {r['destinations'][:40]} | {r['trails'][:40]} | "
          f"{r['state']} | {r['distance_mi']} | {r['gain_ft']} | {r['date']} |")
    with open(os.path.join(args.out_dir, "hikes_report.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print(f"{n:,} hikes from {len(sites):,} sites")


if __name__ == "__main__":
    main()
