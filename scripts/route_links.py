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
import functools
import glob
import gzip
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract import GENERIC, norm  # noqa: E402

csv.field_size_limit(1 << 30)

# A route's destination and the hike table's feature are the same place when
# their names match and they are this close (a lake route ends at the shore
# where the trail arrives, the hike table has the lake's centre: 3 km apart
# for Garibaldi Lake).
DEST_NEAR_M = {"lake": 8000, "camp": 3000, "default": 800}
# A feature the page mentions is "along" the route when this close to its line.
ALONG_M = 300
# A page found by a local trail's name counts as on the route when it, or a
# place it mentions, is this close to the route's line.
NEAR_ROUTE_M = 5000
# Feature kinds that say nothing about the way up.
NOT_WAYPOINT = {"park"}


@functools.lru_cache(maxsize=None)
def key_name(name):
    s = norm(name or "")
    s = re.sub(r"^the ", "", s)
    s = re.sub(r"\b(trailhead|trail head|parking lot|parking|car park)\b", "", s)
    return " ".join(s.split())


@functools.lru_cache(maxsize=None)
def trail_key(name):
    """Trail names compared without the generic words: "Helm Creek Trail" == "Helm Creek"."""
    s = key_name(name)
    s = re.sub(r"\b(trail|trails|path|route|loop|track)\b", "", s)
    return " ".join(s.split())


def metres(a, b):
    """Approximate distance between (lon, lat) points."""
    k = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((a[0] - b[0]) * k, a[1] - b[1]) * 111_320


_BOXES = {}


def within_m(p, line, m):
    """Whether point p is within m metres of a polyline. Points outside the
    line's bounding box widened by m are rejected without walking the line
    (most are: a page's places are mostly far from any one route), so the
    answer is to_line_m's, faster."""
    if not line:
        return False
    box = _BOXES.get(id(line))
    if box is None or box[0] is not line:
        xs, ys = [q[0] for q in line], [q[1] for q in line]
        box = _BOXES[id(line)] = (line, min(xs), max(xs), min(ys), max(ys))
    _, x0, x1, y0, y1 = box
    dy = m / 111_320
    if p[1] < y0 - dy or p[1] > y1 + dy:
        return False
    # Degrees of longitude per metre grow towards the poles: widen by the
    # box's poleward edge, so the test only ever rejects points that are
    # farther than m.
    k = math.cos(math.radians(min(89.0, max(abs(y0), abs(y1)) + dy)))
    dx = m / (111_320 * k)
    if p[0] < x0 - dx or p[0] > x1 + dx:
        return False
    return to_line_m(p, line) <= m


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

        def add(rid, typ, h, title, route, frm, dest=None, dest_at=None, dest_kind=None, dest_ids=None):
            out.append({
                "id": rid, "key": h.get("key", ""), "dest_ids": [str(x) for x in dest_ids or []],
                "type": typ, "park_id": pid, "park": pname, "title": title,
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
                h["dest"], h.get("dest_at"), h.get("dest_kind"), h.get("dest_osm_ids"))
        for i, b in enumerate(doc.get("base_camps", [])):
            camp = b["camp"]
            add(f"{pid}/base/{i}", "approach", {"kind": "approach", "key": b.get("key", "")},
                f"{camp} from {b['from']}", b["approach"], b["from"], camp, b.get("at"), "camp",
                [b["camp_osm_id"]] if b.get("camp_osm_id") else None)
            for j, h in enumerate(b.get("hikes", [])):
                add(f"{pid}/base/{i}/{j}", "from-camp", h, named(h, f"{h['dest']} from {camp}"), h["route"],
                    camp, h["dest"], h.get("dest_at"), h.get("dest_kind"), h.get("dest_osm_ids"))
        for i, t in enumerate(doc.get("point_to_points", [])):
            title = named(t, f"{t['from']} loop" if t.get("kind") == "loop" else f"{t['from']} to {t.get('to', '')}")
            add(f"{pid}/multi/{i}", "multi-day", t, title, t["route"], t["from"])
    return out


EXCLUDED_URLS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "excluded_urls.csv")
# What the website shows per route: trip reports and trail guides that follow
# it, and pages about its destination; personal sites first, at most two
# pages per site, so one prolific site doesn't fill the list.
REPORTS_MAX, ABOUT_MAX, PER_SITE_MAX = 8, 8, 2
FEATURE_KEYS = ("name", "kind", "osm_id", "lat", "lon", "long_trail")


def feature_records(features_path, extracted):
    """Yield {url, features} records (and one {long_trails} record) from a
    route_features file or from extracted-*.jsonl.gz."""
    paths = [features_path] if features_path else sorted({p for pat in extracted for p in glob.glob(pat)})
    for p in paths:
        with gzip.open(p, "rt") as f:
            for line in f:
                if '"url"' in line or '"long_trails"' in line:
                    yield json.loads(line)


def write_features(hikes_path, extracted, out):
    """The places each hike-table page mentions, trimmed to what linking
    needs: a much smaller input than every extracted page."""
    urls = {r["url"] for r in read_tsv(hikes_path)}
    long_trails, n = set(), 0
    with gzip.open(out, "wt") as o:
        for r in feature_records(None, extracted):
            fs = r.get("features") or []
            long_trails |= {f["name"] for f in fs if f.get("long_trail")}
            if r["url"] in urls:
                o.write(json.dumps({"url": r["url"], "features": [
                    {k: f[k] for k in FEATURE_KEYS if f.get(k) is not None} for f in fs]}) + "\n")
                n += 1
        o.write(json.dumps({"long_trails": sorted(long_trails)}) + "\n")
    print(f"wrote {n:,} pages' features to {out}", file=sys.stderr)


def write_bundle(routes, rows, pages, out_dir):
    """reports.bin: each park's links as gzipped JSON, end to end, located by
    reports.json ({"bundle": "reports.bin", "parks": {<park id>: [offset,
    length]}}), like the routes build's own bundle. A park's JSON maps each
    route's key (its id for builds without keys) to
      {"reports": [...], "about": [...]}
    where reports follow the route and about pages are about its destination
    (way up unknown, or another way). Each page is {url, title, site,
    personal, type, date}, with empty fields left out."""
    by_route = defaultdict(lambda: {"route": [], "destination": []})
    for i, u, m, _ in rows:
        by_route[i][m].append(u)

    def page(u):
        p = pages[u]
        e = {"url": u, "title": p["name"], "site": p["site"], "personal": p["site_kind"] == "personal",
             "type": p["page_type"], "date": p["date"]}
        return {k: v for k, v in e.items() if v not in ("", None)}

    def pick(urls, n):
        urls = sorted(set(urls), key=lambda u: (pages[u]["site_kind"] != "personal",
                                                pages[u]["page_type"] != "trip_report",
                                                -int((pages[u]["date"] or "0")[:4] or 0), pages[u]["name"], u))
        out, per_site = [], Counter()
        for u in urls:
            if per_site[pages[u]["site"]] < PER_SITE_MAX:
                per_site[pages[u]["site"]] += 1
                out.append(page(u))
                if len(out) == n:
                    break
        return out

    parks = defaultdict(dict)
    for i, links in by_route.items():
        rt = routes[i]
        shown = pick(links["route"], REPORTS_MAX)
        seen = {p["url"] for p in shown}
        about = pick([u for u in links["destination"] if u not in seen], ABOUT_MAX)
        if shown or about:
            parks[rt["park_id"]][rt["key"] or rt["id"]] = {k: v for k, v in (("reports", shown), ("about", about)) if v}
    index, off = {}, 0
    with open(os.path.join(out_dir, "reports.bin"), "wb") as b:
        for pid in sorted(parks):
            gz = gzip.compress(json.dumps(parks[pid], separators=(",", ":"), sort_keys=True).encode(), mtime=0)
            b.write(gz)
            index[pid] = [off, len(gz)]
            off += len(gz)
    json.dump({"bundle": "reports.bin", "parks": index}, open(os.path.join(out_dir, "reports.json"), "w"))
    print(f"reports bundle: {len(parks):,} parks, "
          f"{sum(len(v) for v in parks.values()):,} routes, {off / 1e6:.1f} MB", file=sys.stderr)


def read_tsv(path):
    with gzip.open(path, "rt") as f:
        yield from csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--routes-index")
    ap.add_argument("--routes-bundle")
    ap.add_argument("--hikes", required=True)
    ap.add_argument("--groups")
    ap.add_argument("--extracted", nargs="+", default=[], help="extracted-*.jsonl.gz (globs allowed)")
    ap.add_argument("--features", help="route_features.jsonl.gz (from --write-features), instead of --extracted")
    ap.add_argument("--write-features", metavar="FILE",
                    help="only write the places each hike-table page mentions (from --extracted) to FILE, the "
                         "compact input that hikeratlas/basemap's routes build links with, and exit")
    ap.add_argument("--exclude-urls", default=EXCLUDED_URLS_CSV, help="CSV of pages never to link (url,reason)")
    ap.add_argument("--out-dir", help="route_links.tsv.gz and route_links_report.md")
    ap.add_argument("--bundle-dir", help="reports.json and reports.bin, for the website (see write_bundle)")
    ap.add_argument("--sample-parks", default="Garibaldi,Killarney,Jasper",
                    help="comma-separated park name fragments to list in the report")
    args = ap.parse_args()
    if args.write_features:
        write_features(args.hikes, args.extracted, args.write_features)
        return
    if not (args.routes_index and args.routes_bundle and args.groups and (args.out_dir or args.bundle_dir)):
        ap.error("--routes-index, --routes-bundle, --groups and --out-dir or --bundle-dir are required")
    for d in (args.out_dir, args.bundle_dir):
        if d:
            os.makedirs(d, exist_ok=True)
    excluded = set()
    if args.exclude_urls and os.path.exists(args.exclude_urls):
        excluded = {r["url"].strip() for r in csv.DictReader(open(args.exclude_urls)) if r["url"].strip()}

    routes = load_routes(args.routes_index, args.routes_bundle)
    print(f"{len(routes):,} routes", file=sys.stderr)

    # Destination features from the hike groups: name -> [(osm id, (lon, lat), kind)]
    feats_by_name = defaultdict(list)
    group_ids = {}  # any OSM id of a destination group -> (all its ids, position)
    for g in read_tsv(args.groups):
        if g["group_kind"] != "destination" or not g["lat"]:
            continue
        ids = [g["osm_id"]] + [x for x in re.split(r"[;\s]+", g.get("merged_osm_ids") or "") if x]
        feats_by_name[key_name(g["name"])].append((ids, (float(g["lon"]), float(g["lat"])), g["feature_kind"]))
        for x in ids:
            group_ids[x] = (ids, (float(g["lon"]), float(g["lat"])))

    # Pages by title destination and by title trail.
    # The place index includes the curated routes themselves (kind "hike",
    # osm_id "<park>/day/<i>"), so extraction already names some routes.
    pages, by_dest, by_trail, by_route = {}, defaultdict(list), defaultdict(list), defaultdict(list)
    for r in read_tsv(args.hikes):
        u = r["url"]
        if u in excluded:
            continue
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
            # By OSM id where the routes build has it (routes#153), else by
            # name and position: the same place can be two OSM features
            # (Panorama Ridge is a viewpoint node in routes and a peak in the
            # place index).
            # Within the same distance as a name match: a route to the shore
            # of Lake Superior or Lake Tahoe isn't a hike to the lake that
            # every page naming it is about.
            near = DEST_NEAR_M.get(rt["dest_kind"], DEST_NEAR_M["default"])
            hits = [group_ids[x][0] for x in rt["dest_ids"]
                    if x in group_ids and metres(group_ids[x][1], rt["dest_at"]) <= near]
            if not hits:
                hits = [ids for ids, p, _ in feats_by_name.get(key_name(rt["dest"]), [])
                        if metres(p, rt["dest_at"]) <= near]
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
    for r in feature_records(args.features, args.extracted):
        if "long_trails" in r:
            long_trails |= {trail_key(n) for n in r["long_trails"]}
            continue
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
            if within_m((f["lon"], f["lat"]), rt["line"], ALONG_M):
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

    def near_route(rt, u):
        """The page is about somewhere near this route: its location, or a
        place it mentions, within NEAR_ROUTE_M of the line."""
        p = pages[u]
        pts = [(float(p["lon"]), float(p["lat"]))] if p.get("lat") and p.get("lon") else []
        pts += [(f["lon"], f["lat"]) for f in pfeat.get(u, []) if f.get("lat") is not None and f.get("kind") != "park"]
        return any(within_m(q, rt["line"], NEAR_ROUTE_M) for q in pts)

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
                # Found by the route's name ("Appalachian Trail", "La Cloche
                # Silhouette Trail"). A long trail's pages are mostly
                # elsewhere, so they must mention a place along this route's
                # line. A trail of one park's (La Cloche) is the same trail
                # wherever the page is in that park: near the line will do.
                # A page whose length doesn't fit (a day hike to Silver Peak
                # on a 72 km route) is about the trail, not this trip.
                # Only for a route that follows a named OSM trail (not one
                # found by its title, "Bright Angel from Plateau Point
                # Trailhead"), with a name that isn't generic ("Shelter
                # Trail") or a long trail's.
                tk = trail_key(rt["name"])
                local = bool(tk) and tk not in long_trails and any(t not in GENERIC for t in tk.split())
                if not (ev["along"] or (local and near_route(rt, u))):
                    continue
                fits = not ev["ratio"] or 0.5 <= ev["ratio"] <= 2.0
                rows.append((i, u, "route" if fits else "destination", ev))
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

    if args.bundle_dir:
        write_bundle(routes, rows, pages, args.bundle_dir)
    if not args.out_dir:
        return
    cols = ["route_id", "route_key", "park_id", "park", "route_title", "route_type", "match", "url", "title", "site", "site_kind",
            "page_type", "date", "along", "via", "trailhead", "length_ratio"]
    rows.sort(key=lambda x: (routes[x[0]]["id"], x[2] != "route", x[1]))
    with gzip.open(os.path.join(args.out_dir, "route_links.tsv.gz"), "wt") as f:
        w = csv.writer(f, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\", lineterminator="\n")
        w.writerow(cols)
        for i, u, m, ev in rows:
            rt, p = routes[i], pages[u]
            w.writerow([rt["id"], rt["key"], rt["park_id"], rt["park"], rt["title"], rt["type"], m, u,
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
