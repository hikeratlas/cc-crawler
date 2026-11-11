#!/usr/bin/env python3
"""Link hike pages to Hiker Atlas curated routes (hikeratlas/routes).

A curated route goes from a trailhead to a destination (a peak, lake,
waterfall, viewpoint or hut) along named trails. A page belongs to a route
when it is about the same destination and, where the destination has several
routes, when it follows the same way up:

  1. destination: the page's title names the route's destination (the hike
     table's destination_osm_ids). The route's destination is matched to an
     OSM feature by name and position (routes only carry name and position).
  2. way up: the trails and places the page mentions (extracted features,
     with positions) lie along the route's line, or the page names the
     route's trails (`via`) or trailhead.
  3. length: the page's distance is close to the route's, round trip or one
     way. Only used to break ties and to flag a likely different route.

Routes that follow an OSM hiking route (they carry its name, e.g. "Garibaldi
Lake Trail") also take pages whose title names that trail.

Each link is `route` (the evidence points at this route among the
destination's routes) or `destination` (the page is about the destination;
its way up is unknown or shared by several routes).

  python3 scripts/route_links.py --routes-index index.json --routes-bundle bundle.bin \\
      --hikes hikes.tsv.gz --groups hike_groups.tsv.gz --extracted 'extracted-*.jsonl.gz' \\
      --out-dir out/

Writes route_links.tsv.gz (one row per route and page) and
route_links_report.md.
"""
import argparse
import csv
import glob
import gzip
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract import norm  # noqa: E402

csv.field_size_limit(1 << 30)

# A route's destination and the hike table's feature are the same place when
# their names match and they are this close (a lake route ends at the shore
# where the trail arrives, the hike table has the lake's centre: 3 km apart
# for Garibaldi Lake).
DEST_NEAR_M = {"lake": 8000, "camp": 3000, "default": 800}
# A feature the page mentions is "along" the route when this close to its line.
ALONG_M = 300
# Feature kinds that say nothing about the way up.
NOT_WAYPOINT = {"park"}


def key_name(name):
    s = norm(name or "")
    s = re.sub(r"^the ", "", s)
    s = re.sub(r"\b(trailhead|trail head|parking lot|parking|car park)\b", "", s)
    return " ".join(s.split())


def trail_key(name):
    """Trail names compared without the generic words: "Helm Creek Trail" == "Helm Creek"."""
    s = key_name(name)
    s = re.sub(r"\b(trail|trails|path|route|loop|track)\b", "", s)
    return " ".join(s.split())


def metres(a, b):
    """Approximate distance between (lon, lat) points."""
    k = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((a[0] - b[0]) * k, a[1] - b[1]) * 111_320


def to_line_m(p, line):
    """Distance from point p to a polyline, in metres (equirectangular)."""
    if not line:
        return float("inf")
    k = math.cos(math.radians(p[1]))
    px, py = p[0] * k, p[1]
    best = float("inf")
    prev = None
    for q in line:
        qx, qy = q[0] * k, q[1]
        if prev is None:
            d = math.hypot(px - qx, py - qy)
        else:
            ax, ay = prev
            dx, dy = qx - ax, qy - ay
            L = dx * dx + dy * dy
            t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
            d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        best = min(best, d)
        prev = (qx, qy)
    return best * 111_320


def load_routes(index_path, bundle_path):
    """Flatten every park's routes, with the ids the routes build gives them."""
    index = json.load(open(index_path))
    data = open(bundle_path, "rb").read()
    out = []
    for pid, (off, ln) in index["parks"].items():
        doc = json.loads(gzip.decompress(data[off:off + ln]))
        park = doc.get("park", {})
        if str(park.get("id")) != pid:
            continue  # an alias entry pointing at another park's routes
        pname = park.get("name", "")

        def add(rid, typ, h, title, route, frm, dest=None, dest_at=None, dest_kind=None):
            out.append({
                "id": rid, "type": typ, "park_id": pid, "park": pname, "title": title,
                "name": h.get("name") or "", "from": frm, "dest": dest or "", "dest_at": dest_at,
                "dest_kind": dest_kind or "", "kind": h.get("kind", ""),
                "km": route.get("km"), "one_way_km": route.get("one_way_km"), "gain": route.get("gain"),
                "via": route.get("via") or [], "line": route.get("line") or [],
                "passes": [n for _, ns in route.get("passes") or [] for n in ns],
            })

        def named(h, otherwise):
            return h.get("name") or otherwise

        for i, h in enumerate(doc.get("day_hikes", [])):
            add(f"{pid}/day/{i}", "day", h, named(h, f"{h['dest']} from {h['from']}"), h["route"], h["from"],
                h["dest"], h.get("dest_at"), h.get("dest_kind"))
        for i, b in enumerate(doc.get("base_camps", [])):
            camp = b["camp"]
            add(f"{pid}/base/{i}", "approach", {"kind": "approach"}, f"{camp} from {b['from']}", b["approach"],
                b["from"], camp, b.get("at"), "camp")
            for j, h in enumerate(b.get("hikes", [])):
                add(f"{pid}/base/{i}/{j}", "from-camp", h, named(h, f"{h['dest']} from {camp}"), h["route"],
                    camp, h["dest"], h.get("dest_at"), h.get("dest_kind"))
        for i, t in enumerate(doc.get("point_to_points", [])):
            title = named(t, f"{t['from']} loop" if t.get("kind") == "loop" else f"{t['from']} to {t.get('to', '')}")
            add(f"{pid}/multi/{i}", "multi-day", t, title, t["route"], t["from"])
    return out


def read_tsv(path):
    with gzip.open(path, "rt") as f:
        yield from csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--routes-index", required=True)
    ap.add_argument("--routes-bundle", required=True)
    ap.add_argument("--hikes", required=True)
    ap.add_argument("--groups", required=True)
    ap.add_argument("--extracted", nargs="+", default=[], help="extracted-*.jsonl.gz (globs allowed)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--sample-parks", default="Garibaldi,Killarney,Jasper",
                    help="comma-separated park name fragments to list in the report")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    routes = load_routes(args.routes_index, args.routes_bundle)
    print(f"{len(routes):,} routes", file=sys.stderr)

    # Destination features from the hike groups: name -> [(osm id, (lon, lat), kind)]
    feats_by_name = defaultdict(list)
    for g in read_tsv(args.groups):
        if g["group_kind"] != "destination" or not g["lat"]:
            continue
        ids = [g["osm_id"]] + [x for x in re.split(r"[;\s]+", g.get("merged_osm_ids") or "") if x]
        feats_by_name[key_name(g["name"])].append((ids, (float(g["lon"]), float(g["lat"])), g["feature_kind"]))

    # Pages by title destination and by title trail.
    # The place index includes the curated routes themselves (kind "hike",
    # osm_id "<park>/day/<i>"), so extraction already names some routes.
    pages, by_dest, by_trail, by_route = {}, defaultdict(list), defaultdict(list), defaultdict(list)
    for r in read_tsv(args.hikes):
        u = r["url"]
        pages[u] = r
        for oid in (r["destination_osm_ids"] or "").split(";"):
            if oid.strip():
                by_dest[oid.strip()].append(u)
        for t in (r["trails"] or "").split(";"):
            if t.strip():
                by_trail[trail_key(t)].append(u)
        for oid in (r["trail_osm_ids"] or "").split(";"):
            if "/" in oid:
                by_route[oid.strip()].append(u)

    # Each route's destination feature(s), and its candidate pages.
    cand = defaultdict(set)   # route index -> urls
    by_name = defaultdict(set)  # route index -> urls found by the route's own name, not its destination
    dest_of = {}              # route index -> osm ids
    unmatched = Counter()
    for i, rt in enumerate(routes):
        if rt["dest"] and rt["dest_at"]:  # approaches too: many camps are named lakes
            near = DEST_NEAR_M.get(rt["dest_kind"], DEST_NEAR_M["default"])
            hits = [ids for ids, p, _ in feats_by_name.get(key_name(rt["dest"]), []) if metres(p, rt["dest_at"]) <= near]
            if hits:
                dest_of[i] = {x for ids in hits for x in ids}
                for oid in dest_of[i]:
                    cand[i].update(by_dest.get(oid, ()))
            else:
                unmatched[rt["dest_kind"]] += 1
        named = set(by_route.get(rt["id"], ()))
        if rt["name"]:
            named |= set(by_trail.get(trail_key(rt["name"]), ()))
        by_name[i] = named - cand[i]
        cand[i] |= named
    need = set().union(*cand.values()) if cand else set()
    print(f"{len(dest_of):,} routes matched to a destination; {len(need):,} candidate pages", file=sys.stderr)

    # Features each candidate page mentions (from the extraction).
    pfeat = {}
    long_trails = set()
    for p in sorted({p for pat in args.extracted for p in glob.glob(pat)}):
        with gzip.open(p, "rt") as f:
            for line in f:
                if '"url"' not in line:
                    continue
                r = json.loads(line)
                if r["url"] in need:
                    pfeat[r["url"]] = r.get("features") or []
                for f in r.get("features") or []:
                    if f.get("long_trail"):
                        long_trails.add(trail_key(f["name"]))

    # Score every (route, page) pair, then decide route vs destination links.
    def evidence(rt, u, dest_ids):
        fs = pfeat.get(u, [])
        own = {trail_key(rt["dest"]), trail_key(rt["name"])} - {""}
        along = []
        for f in fs:
            if (f.get("lat") is None or f.get("kind") in NOT_WAYPOINT or str(f.get("osm_id")) in dest_ids
                    or trail_key(f["name"]) in own):
                continue
            if to_line_m((f["lon"], f["lat"]), rt["line"]) <= ALONG_M:
                along.append(f["name"])
        names = {trail_key(f["name"]) for f in fs} | {trail_key(t) for t in (pages[u]["trails"] or "").split(";") if t}
        names.discard("")
        # A trail named after the destination ("Mount Bierstadt Trail") or the
        # route itself says nothing about which way up: every page about the
        # peak names it.
        # Long trails (Appalachian, Pacific Crest, John Muir) are named on
        # thru-hike pages hundreds of miles away; they don't say which way up.
        via = [v for v in rt["via"] if trail_key(v) and trail_key(v) in names and trail_key(v) not in own
               and trail_key(v) not in long_trails]
        # A trailhead counts only when it isn't just one of those trails
        # ("Appalachian Trail trailhead").
        th = key_name(rt["from"])
        trailhead = bool(th and trail_key(rt["from"]) not in own | long_trails | {trail_key(v) for v in rt["via"]}
                         and th in {key_name(f["name"]) for f in fs})
        d = pages[u]["distance_mi"]
        ratio = None
        if d and rt["km"]:
            km = float(d) * 1.609
            ratio = min((km / x for x in (rt["km"], rt["one_way_km"]) if x), key=lambda r: abs(math.log(r)))
        return {"along": sorted(set(along)), "via": via, "trailhead": trailhead, "ratio": ratio}

    def strength(ev):
        # A distance far from the route's (round trip or one way) says the
        # page took another route, or a longer trip through this one.
        if ev["ratio"] and not 0.5 <= ev["ratio"] <= 2.0:
            return 0
        return len(ev["along"]) + 2 * len(ev["via"]) + 2 * ev["trailhead"]

    by_page_dest = defaultdict(list)  # (url, destination osm ids) -> [(route index, evidence)]
    rows = []
    for i, urls in cand.items():
        rt = routes[i]
        dids = dest_of.get(i, set())
        for u in urls:
            ev = evidence(rt, u, dids)
            if u in by_name[i]:
                # Found by the route's name ("Appalachian Trail", "Colorado
                # Trail"): a long trail's pages are mostly elsewhere, so the
                # page must mention a place along this route's line.
                if ev["along"] and strength(ev):
                    rows.append((i, u, "route", ev))
            else:
                by_page_dest[(u, frozenset(dids))].append((i, ev))

    for (u, _), opts in by_page_dest.items():
        best = max(strength(ev) for _, ev in opts)
        top = [o for o in opts if strength(o[1]) == best]
        if best > 0 and len(top) < len(opts):
            for i, ev in top:
                rows.append((i, u, "route", ev))
        elif len(opts) == 1 and best > 0:
            rows.append((opts[0][0], u, "route", opts[0][1]))
        else:
            # No evidence that separates the routes: keep the one whose length
            # fits clearly, else link the page to the destination's routes.
            fit = [o for o in opts if o[1]["ratio"] and 0.8 <= o[1]["ratio"] <= 1.25]
            if len(fit) == 1 and len(opts) > 1:
                rows.append((fit[0][0], u, "route", fit[0][1]))
            else:
                for i, ev in opts:
                    rows.append((i, u, "destination", ev))

    cols = ["route_id", "park_id", "park", "route_title", "route_type", "match", "url", "title", "site", "site_kind",
            "page_type", "date", "along", "via", "trailhead", "length_ratio"]
    rows.sort(key=lambda x: (routes[x[0]]["id"], x[2] != "route", x[1]))
    with gzip.open(os.path.join(args.out_dir, "route_links.tsv.gz"), "wt") as f:
        w = csv.writer(f, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\", lineterminator="\n")
        w.writerow(cols)
        for i, u, m, ev in rows:
            rt, p = routes[i], pages[u]
            w.writerow([rt["id"], rt["park_id"], rt["park"], rt["title"], rt["type"], m, u,
                        (p["name"] or "").replace("\t", " "), p["site"], p["site_kind"], p["page_type"], p["date"],
                        "; ".join(ev["along"]), "; ".join(ev["via"]), int(ev["trailhead"]),
                        "" if ev["ratio"] is None else f"{ev['ratio']:.2f}"])

    # Report.
    linked = defaultdict(Counter)
    for i, u, m, ev in rows:
        linked[i][m] += 1
        if pages[u]["site_kind"] == "personal":
            linked[i][m + "_personal"] += 1
    by_type = Counter(rt["type"] for rt in routes)
    lines = ["# Route links", "",
             f"Generated by `scripts/route_links.py`. {len(routes):,} curated routes; {len(rows):,} route-page links "
             f"({sum(1 for r in rows if r[2] == 'route'):,} route-specific).", "",
             "| route type | routes | destination matched | any link | route-specific link | personal route-specific |",
             "|---|---:|---:|---:|---:|---:|"]
    for t in ("day", "from-camp", "approach", "multi-day"):
        idx = [i for i, rt in enumerate(routes) if rt["type"] == t]
        lines.append(f"| {t} | {by_type[t]:,} | {sum(1 for i in idx if i in dest_of):,} | "
                     f"{sum(1 for i in idx if linked[i]):,} | {sum(1 for i in idx if linked[i]['route']):,} | "
                     f"{sum(1 for i in idx if linked[i]['route_personal']):,} |")
    lines += ["", f"Route destinations with no hike-table feature of that name nearby: {sum(unmatched.values()):,} "
              f"({', '.join(f'{k} {v:,}' for k, v in unmatched.most_common())}).", ""]
    for frag in [x.strip() for x in args.sample_parks.split(",") if x.strip()]:
        idx = [i for i, rt in enumerate(routes) if frag.lower() in rt["park"].lower()]
        if not idx:
            continue
        lines += [f"## {routes[idx[0]]['park']}", "", "| route | destination | route-specific | destination only | example |",
                  "|---|---|---:|---:|---|"]
        for i in idx:
            ex = next((u for j, u, m, _ in rows if j == i and m == "route"), "")
            lines.append(f"| {routes[i]['title']} | {routes[i]['dest']} | {linked[i]['route']} | "
                         f"{linked[i]['destination']} | {ex} |")
        lines.append("")
    open(os.path.join(args.out_dir, "route_links_report.md"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines[:12]), file=sys.stderr)


if __name__ == "__main__":
    main()
