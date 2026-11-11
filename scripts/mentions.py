#!/usr/bin/env python3
"""How many sites write about each place: a popularity signal for Hiker Atlas's
curated routes (hikeratlas/basemap#281).

The score of a curated route (hikeratlas/routes) took its popularity from the
destination's QRank, which is near zero for most trails, passes and small
lakes: they have no Wikidata item. The hike table knows which trails and
destinations the trip reports and trail guides are about, so the number of
distinct sites with such a page is a popularity measure for exactly those
places: Johnston Canyon Trail 117 sites, Lake Agnes 37, Larch Valley Trail 10.

For each OSM id in the hike table's destination_osm_ids and trail_osm_ids (a
page's subjects, not every place it mentions), this counts the distinct sites
whose pages name it, and takes its name, kind and position from the page
features (route_links.py --write-features). Ids that are Hiker Atlas route keys
(<park>/day/<i>, from pages matched to the routes themselves) are left out:
counting them would feed the routes' own picks back into their score.

  python3 scripts/mentions.py --hikes hikes.tsv.gz --features route_features.jsonl.gz \\
      --out mentions.tsv.gz

Writes osm_id, sites, pages, kind, name, lat, lon (tab-separated, gzipped,
most-mentioned first). Stdlib only.
"""
import argparse
import csv
import gzip
import json
import sys
from collections import Counter, defaultdict

csv.field_size_limit(1 << 30)


def ids(s):
    return [x.strip() for x in s.split(";") if x.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hikes", required=True, help="hikes.tsv.gz (hikes.py)")
    ap.add_argument("--features", required=True, help="route_features.jsonl.gz (route_links.py --write-features)")
    ap.add_argument("--out", required=True, help="mentions.tsv.gz to write")
    ap.add_argument("--min-sites", type=int, default=1, help="leave out places fewer sites write about")
    a = ap.parse_args(argv)

    sites = defaultdict(set)
    pages = Counter()
    with gzip.open(a.hikes, "rt", newline="") as f:
        for r in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            site = (r.get("site") or "").lower()
            if not site:
                continue
            for i in set(ids(r["destination_osm_ids"]) + ids(r["trail_osm_ids"])):
                # Route keys (<park>/day/<i>): the routes' own picks.
                if not i.isdigit():
                    continue
                sites[i].add(site)
                pages[i] += 1

    # Name, kind and position: the commonest name a page gives it.
    about = {}
    names = defaultdict(Counter)
    with gzip.open(a.features, "rt") as f:
        for line in f:
            for ft in json.loads(line).get("features", []):
                i = str(ft.get("osm_id", ""))
                if i not in sites:
                    continue
                names[i][ft.get("name", "")] += 1
                about.setdefault(i, (ft.get("kind", ""), ft.get("lat"), ft.get("lon")))

    rows = sorted(((len(s), i) for i, s in sites.items() if len(s) >= a.min_sites), reverse=True)
    n = 0
    with gzip.open(a.out, "wt", newline="") as f:
        w = csv.writer(f, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\", lineterminator="\n")
        w.writerow(["osm_id", "sites", "pages", "kind", "name", "lat", "lon"])
        for count, i in rows:
            kind, lat, lon = about.get(i, ("", None, None))
            name = names[i].most_common(1)[0][0] if names[i] else ""
            w.writerow([i, count, pages[i], kind, name.replace("\t", " "),
                        "" if lat is None else f"{lat:.5f}", "" if lon is None else f"{lon:.5f}"])
            n += 1
    print(f"mentions: {n} places, {sum(1 for c, _ in rows if c >= 5)} with 5+ sites", file=sys.stderr)


if __name__ == "__main__":
    main()
