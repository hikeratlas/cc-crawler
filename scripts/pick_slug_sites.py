"""Pick new sites from several slug-scan releases (docs/DESIGN.md, "Fix 2").

Each slug-scan release ranks the sites in one crawl by the hike names in
their URLs. The first scan (CC-MAIN-2026-34) seeded batch 2 with every site
scoring >= 50. Older crawls find blogs that have since gone quiet, which is
where most of the lost recall-test reports live.

A site is picked when its best score across the given releases is at least
--min-score, or --min-platform-score for hosts on blog platforms
(blogspot, wordpress.com, ...: one person's blog has few posts, so it
scores lower). Sites already known to a page pass, already seeded (scored
>= --seeded-score in the --seeded releases), listed in --skip files or
excluded in data/excluded_sites.csv are left out, as are country domains
other than .ca and .us (and a few used as generic ones, like .co and .io).

  python3 scripts/pick_slug_sites.py --scans run4/slug_sites.tsv.gz ... \
      --seeded run1/slug_sites.tsv.gz --skip data/deep/kept_sites.txt ... > sites.txt
"""
import argparse
import csv
import gzip
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Country domains other than Canada's and the US's are almost never about North
# American hikes; crawls before 2018-39 have no language field to catch them.
CC_OK = {"ca", "us", "co", "io", "me", "tv", "ly", "fm", "ai"}


def rows(path):
    with gzip.open(path, "rt") as f:
        yield from csv.DictReader(f, delimiter="\t")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scans", nargs="+", required=True, help="slug_sites.tsv.gz files to pick from")
    ap.add_argument("--seeded", nargs="*", default=[], help="slug_sites.tsv.gz files already seeded")
    ap.add_argument("--seeded-score", type=float, default=50)
    ap.add_argument("--skip", nargs="*", default=[], help="text files of sites to leave out")
    ap.add_argument("--min-score", type=float, default=50)
    ap.add_argument("--min-platform-score", type=float, default=15)
    ap.add_argument("--min-names", type=int, default=3, help="minimum distinct names in the best scan")
    args = ap.parse_args()

    plat = [r["parent"] for r in csv.DictReader(open(os.path.join(HERE, "..", "data", "platforms.csv")))]
    skip = set()
    for r in csv.DictReader(open(os.path.join(HERE, "..", "data", "excluded_sites.csv"))):
        skip.add(r["site"])
    for p in args.skip:
        skip |= {ln.strip() for ln in open(p) if ln.strip() and not ln.startswith("#")}
    for p in args.seeded:
        for r in rows(p):
            if r["known"] == "True" or float(r["score"]) >= args.seeded_score:
                skip.add(r["site"])

    best = {}
    for p in args.scans:
        for r in rows(p):
            s = r["site"]
            if r["known"] == "True":
                skip.add(s)
            sc, n = float(r["score"]), int(r["distinct_names"])
            if s not in best or sc > best[s][0]:
                best[s] = (sc, n)

    picked = []
    for s, (sc, n) in best.items():
        if s in skip or n < args.min_names:
            continue
        tld = s.rsplit(".", 1)[-1]
        if len(tld) == 2 and tld not in CC_OK:
            continue
        on_platform = any(s.endswith("." + p) for p in plat)
        if sc >= (args.min_platform_score if on_platform else args.min_score):
            picked.append((sc, s, on_platform))
    picked.sort(reverse=True)
    print(f"picked {len(picked)} sites ({sum(p for *_, p in picked)} on blog platforms)", file=sys.stderr)
    for _, s, _ in picked:
        print(s)


if __name__ == "__main__":
    main()
