#!/usr/bin/env python3
"""Match hike pages' GPS tracks to curated routes, and find routes we're missing
(hikeratlas/cc-crawler#82; docs/DESIGN.md "Tracks").

Input: page_tracks.py's tracks.jsonl.gz (each fetched track's simplified lines
and the pages linking it), the routes build's index.json + bundle.bin, and the
hike table (each page's destinations and trails).

Which lines are a page's own: a track linked from at most 3 pages of one site
with at most 3 lines is the page's track. A bigger map (a site-wide map of
every hike, a page with many tracks) counts only by its lines whose name
names one of the page's destinations or trails.

  walks    the track walks the route: at least WALK_MIN (80%) of the route's
           line is within NEAR_M (100 m) of the track's line
  partial  at least PARTIAL_MIN (50%) of it is
  missing  a page's own line that walks no route and is mostly off every
           route (under 30% of the line within 100 m of any one), in an area
           with curated routes within 10 km: tracks of the same way (each
           covering 60% of the other) cluster into one missing-route candidate

Outputs (--out-dir):
  track_matches.tsv.gz  page_url, track_id, line, line_name, route_key,
                        route_id, park, route_title, route_cover, track_cover,
                        match (walks/partial)
  missing_routes.tsv.gz cluster, n_pages, n_sites, near_park, line_name,
                        length_m, centre (lat,lon), pages, track_ids
  track_match_report.md counts, and the top missing-route candidates
"""
import argparse
import collections
import csv
import gzip
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract import norm  # noqa: E402
from route_links import load_routes  # noqa: E402

csv.field_size_limit(1 << 30)

NEAR_M = 100
WALK_MIN, PARTIAL_MIN = 0.8, 0.5
OFF_ROUTE_MAX = 0.3
CURATED_AREA_M = 10_000
SAME_WAY = 0.6
STEP_M = 40
CELL = 0.01  # grid cell in degrees (~1 km)
MISSING_MAX_M = 80_000  # longer: a road trip or a long trail, not a missing route
# Not a walk: the drive to the trailhead, a road trip, a 4WD approach.
NOT_A_WALK = re.compile(r"\b(directions?|drive|driving|road trip|4wd|car|shuttle|ferry|boat|kayak|paddle|bike|ride)\b",
                        re.I)


def read_tsv(path):
    with gzip.open(path, "rt") as f:
        yield from csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)


def dens(line, step=STEP_M):
    """The line's points every ~step metres."""
    out = []
    for a, b in zip(line, line[1:]):
        d = math.hypot((b[0] - a[0]) * 111320 * math.cos(math.radians(a[1])), (b[1] - a[1]) * 110540)
        n = max(1, int(d / step))
        for k in range(n):
            out.append((a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n))
    out.append((line[-1][0], line[-1][1]))
    return out


def cell(p):
    return (int(math.floor(p[0] / CELL)), int(math.floor(p[1] / CELL)))


def ptgrid(pts):
    g = collections.defaultdict(list)
    for q in pts:
        g[cell(q)].append(q)
    return g


def cover(a, bgrid, m=NEAR_M):
    """Share of points a within m metres of a point in bgrid."""
    if not a:
        return 0.0
    hit = 0
    for p in a:
        c = math.cos(math.radians(p[1]))
        cx, cy = cell(p)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if any(math.hypot((q[0] - p[0]) * 111320 * c, (q[1] - p[1]) * 110540) <= m
                       for q in bgrid.get((cx + dx, cy + dy), ())):
                    hit += 1
                    break
            else:
                continue
            break
    return hit / len(a)


def own_lines(track, page_names):
    """The track's lines that are the page's own (see module doc)."""
    lines = track.get("lines") or []
    small = track.get("n_sites", 1) <= 1 and track.get("n_pages", 1) <= 3 and len(lines) <= 3
    if small:
        return list(enumerate(lines))
    out = []
    for i, ln in enumerate(lines):
        t = " " + norm(ln.get("name") or "") + " "
        if any(" " + n + " " in t for n in page_names):
            out.append((i, ln))
    return out


def page_names(row):
    out = set()
    for col in ("destinations", "trails", "name"):
        for n in (row.get(col) or "").split(";"):
            k = norm(n.strip())
            if len(k.split()) >= 2 or (col != "name" and len(k) >= 6):
                out.add(k)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tracks", required=True, help="tracks.jsonl.gz (page_tracks.py fetch)")
    ap.add_argument("--routes-index", required=True)
    ap.add_argument("--routes-bundle", required=True)
    ap.add_argument("--hikes", required=True, help="hikes.tsv.gz: each page's destinations and trails")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    hikes = {r["url"]: r for r in read_tsv(args.hikes)}
    routes = [r for r in load_routes(args.routes_index, args.routes_bundle) if len(r["line"]) >= 2]
    rd = [dens(r["line"]) for r in routes]
    grid = collections.defaultdict(set)
    for i, d in enumerate(rd):
        for p in d:
            grid[cell(p)].add(i)
    print(f"{len(routes):,} routes with lines", file=sys.stderr)

    os.makedirs(args.out_dir, exist_ok=True)
    n = collections.Counter()
    matched_routes = collections.Counter()
    orphans = []  # (page, site, track_id, line idx, line, dens)
    with gzip.open(os.path.join(args.out_dir, "track_matches.tsv.gz"), "wt") as o:
        w = csv.writer(o, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\", lineterminator="\n")
        w.writerow(["page_url", "track_id", "line", "line_name", "route_key", "route_id", "park", "route_title",
                    "route_cover", "track_cover", "match"])
        for raw in gzip.open(args.tracks, "rt"):
            t = json.loads(raw)
            if t.get("error") or not t.get("lines"):
                continue
            n["tracks"] += 1
            for pg in t["pages"]:
                row = hikes.get(pg["url"])
                if row is None:
                    continue
                lines = own_lines(t, page_names(row))
                if not lines:
                    n["page-tracks without own lines"] += 1
                    continue
                n["page-tracks"] += 1
                best_page = None
                for li, ln in lines:
                    td = dens(ln["coords"])
                    tg = ptgrid(td)
                    cands = set()
                    for p in td:
                        cands |= grid.get(cell(p), set())
                    best_t = 0.0
                    near_any = bool(cands)
                    for i in sorted(cands):
                        rc = cover(rd[i], tg)
                        if rc < PARTIAL_MIN:
                            continue
                        tc = cover(td, ptgrid(rd[i]))
                        best_t = max(best_t, tc)
                        m = "walks" if rc >= WALK_MIN else "partial"
                        r = routes[i]
                        w.writerow([pg["url"], t["track_id"], li, (ln.get("name") or "").replace("\t", " ")[:100],
                                    r["key"], r["id"], r["park"], r["title"], f"{rc:.2f}", f"{tc:.2f}", m])
                        n[f"line-route:{m}"] += 1
                        matched_routes[r["key"]] += 1
                        if best_page is None or (m == "walks" and best_page != "walks"):
                            best_page = m
                    if best_t < OFF_ROUTE_MAX and 1000 <= ln["length_m"] <= MISSING_MAX_M \
                            and not NOT_A_WALK.search(ln.get("name") or ""):
                        if not near_any:  # no route in the same ~1 km cells: is the area curated at all?
                            c0 = ((ln["bbox"][0] + ln["bbox"][2]) / 2, (ln["bbox"][1] + ln["bbox"][3]) / 2)
                            k = int(CURATED_AREA_M / 1000)
                            cx, cy = cell(c0)
                            near_any = any(grid.get((cx + dx, cy + dy)) for dx in range(-k, k + 1)
                                           for dy in range(-k, k + 1))
                        if near_any:
                            orphans.append((pg["url"], pg["site"], t["track_id"], li, ln, td))
                n[f"page-tracks:{best_page or 'no route'}"] += 1

    # Missing routes: cluster orphan lines that follow the same way.
    parent = list(range(len(orphans)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    og = collections.defaultdict(set)
    grids = [ptgrid(o[5]) for o in orphans]
    for i, o in enumerate(orphans):
        for p in o[5]:
            og[cell(p)].add(i)
    for i, o in enumerate(orphans):
        cands = set()
        for p in o[5][::5]:
            cands |= og.get(cell(p), set())
        for j in cands:
            if j <= i or find(i) == find(j):
                continue
            if cover(o[5], grids[j]) >= SAME_WAY and cover(orphans[j][5], grids[i]) >= SAME_WAY:
                parent[find(j)] = find(i)
    clusters = collections.defaultdict(list)
    for i in range(len(orphans)):
        clusters[find(i)].append(orphans[i])

    def nearest_park(ln):
        c0 = ((ln["bbox"][0] + ln["bbox"][2]) / 2, (ln["bbox"][1] + ln["bbox"][3]) / 2)
        cx, cy = cell(c0)
        best = None
        for k in range(0, 11):
            for dx in range(-k, k + 1):
                for dy in range(-k, k + 1):
                    if max(abs(dx), abs(dy)) != k:
                        continue
                    for i in grid.get((cx + dx, cy + dy), ()):
                        q = rd[i][0]
                        d = math.hypot((q[0] - c0[0]) * 111320 * math.cos(math.radians(c0[1])), (q[1] - c0[1]) * 110540)
                        if best is None or d < best[0]:
                            best = (d, routes[i]["park"])
            if best:
                return best[1], c0
        return "", c0

    rows = []
    for members in clusters.values():
        pages = sorted({m[0] for m in members})
        sites = sorted({m[1] for m in members})
        rep = max(members, key=lambda m: (bool(m[4].get("name")), m[4]["n_points"]))
        park, c0 = nearest_park(rep[4])
        name = rep[4].get("name") or ""
        hk = hikes.get(rep[0]) or {}
        rows.append({"n_pages": len(pages), "n_sites": len(sites), "near_park": park,
                     "line_name": name, "page_destinations": hk.get("destinations", ""),
                     "length_m": rep[4]["length_m"], "centre": f"{c0[1]:.5f},{c0[0]:.5f}",
                     "pages": " ".join(pages[:10]), "track_ids": " ".join(sorted({m[2] for m in members})[:10])})
    rows.sort(key=lambda r: (-r["n_sites"], -r["n_pages"], r["near_park"], r["centre"]))
    cols = ["cluster", "n_pages", "n_sites", "near_park", "line_name", "page_destinations", "length_m", "centre",
            "pages", "track_ids"]
    with gzip.open(os.path.join(args.out_dir, "missing_routes.tsv.gz"), "wt") as o:
        w = csv.writer(o, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\", lineterminator="\n")
        w.writerow(cols)
        for k, r in enumerate(rows, 1):
            w.writerow([k] + [str(r[c]).replace("\t", " ") for c in cols[1:]])

    lines = ["# Track matches", "",
             f"{n['tracks']:,} fetched tracks; {n['page-tracks']:,} (page, track) pairs with the page's own lines "
             f"({n['page-tracks without own lines']:,} more where a big map had no line naming the page's places).",
             "",
             f"- walk a curated route (>= {WALK_MIN:.0%} of it within {NEAR_M} m): {n['page-tracks:walks']:,}",
             f"- walk part of one (>= {PARTIAL_MIN:.0%}): {n['page-tracks:partial']:,}",
             f"- no route: {n['page-tracks:no route']:,}",
             f"- routes walked by at least one track: {len(matched_routes):,}",
             "",
             f"Missing-route candidates: {len(rows):,} clusters of tracks that follow no curated route, "
             f"near curated routes (within {CURATED_AREA_M // 1000} km); "
             f"{sum(1 for r in rows if r['n_sites'] >= 2):,} with tracks from 2+ sites.", "",
             "| # | sites | pages | near park | track name | page's destinations | km | centre |",
             "|---:|---:|---:|---|---|---|---:|---|"]
    for k, r in enumerate(rows[:60], 1):
        lines.append(f"| {k} | {r['n_sites']} | {r['n_pages']} | {r['near_park']} | {r['line_name'][:40]} | "
                     f"{r['page_destinations'][:50]} | {r['length_m'] / 1000:.1f} | {r['centre']} |")
    open(os.path.join(args.out_dir, "track_match_report.md"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines[:12]), file=sys.stderr)


if __name__ == "__main__":
    main()
