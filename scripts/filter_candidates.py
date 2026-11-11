#!/usr/bin/env python3
"""Re-filter an existing candidate list with the current URL rules.

Keeps only pages whose slug matches (slug_match = 1; with --all, any page) and that no rule in
scripts/urlrules.py drops, and reports what was removed and why. Use it to
apply rule changes to a published candidate list without re-running the
harvest.

Usage:
  scripts/filter_candidates.py --candidates 'candidates-part-*.tsv.gz' --out-dir filtered/
"""
import argparse
import csv
import glob
import gzip
import hashlib
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import urlrules  # noqa: E402
import slugnames  # noqa: E402
from site_report import slug_text  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", nargs="+", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--all", action="store_true",
                    help="keep pages whose slug doesn't match too (candidates from site-urls posts/all mode)")
    ap.add_argument("--sites", help="keep only sites listed in this file (one per line, # comments)")
    ap.add_argument("--index", help="Hiker Atlas autosuggest.db: rows whose URL names a hike or landform count "
                    "as slug matches (slugnames.py), so the cap and slug filter keep them")
    ap.add_argument("--names-only", action="store_true",
                    help="with --index: keep only rows whose URL names a Hiker Atlas hike or landform "
                         "(not just a hiking word): a big harvest's most precise part (DESIGN.md, fix 3)")
    ap.add_argument("--stopword-names-only", action="store_true",
                    help="keep only rows whose URL names a hike only once stop words are removed "
                         "(data/gazetteer/stopword_names.txt: \"top-of-giant\"), to re-run old harvests for them")
    ap.add_argument("--max-per-site", type=int, default=0,
                    help="keep at most this many pages per site (0 = no cap), chosen by URL hash, so one big "
                         "site (a newspaper whose URLs name streets) can't swamp the page pass")
    ap.add_argument("--max-unmatched", type=int, default=0,
                    help="with --all: keep at most this many non-matching pages per site (0 = no cap), chosen by URL hash")
    ap.add_argument("--skip-urls", nargs="*", default=[],
                    help="gzip TSVs with a url column (page-pass pages.tsv.gz): leave out pages already scored")
    ap.add_argument("--exclude", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data",
                                                      "excluded_sites.csv"),
                    help="sites never kept (data/excluded_sites.csv)")
    args = ap.parse_args()
    if args.names_only and not args.index:
        ap.error("--names-only needs --index")
    excluded = set()
    if args.exclude and os.path.exists(args.exclude):
        with open(args.exclude, newline="") as f:
            excluded = {r["site"].strip().lower() for r in csv.DictReader(f)}
    only = None
    if args.sites:
        with open(args.sites) as f:
            # A section entry (14ers.com/php14ers/tripreport.php) keeps its site.
            only = {ln.split("\t")[0].strip().split("/")[0].removeprefix("www.")
                    for ln in f if ln.strip() and not ln.startswith("#")}
    def matched(row, col):
        if args.stopword_names_only:
            s = slug_text(row[col["url"]])
            return slugnames.match_stopword(s) is not None and (
                not args.index or slugnames.match(s, args.index) == slugnames.match_stopword(s))
        if args.names_only:
            if "gear" in col and row[col["gear"]] == "1":
                return False
            return slugnames.match(slug_text(row[col["url"]]), args.index) is not None
        if row[col["slug_match"]] == "1":
            return True
        if not args.index or ("gear" in col and row[col["gear"]] == "1"):
            return False
        return slugnames.match(slug_text(row[col["url"]]), args.index) is not None

    key = lambda u: re.sub(r"^https?://(www\.)?", "", u.strip(), flags=re.I).rstrip("/").lower()
    skip = set()
    for path in args.skip_urls:
        with gzip.open(path, "rt") as f:
            for r in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
                skip.add(key(r["url"]))
    # Per-site cap on non-matching pages: rank them by a hash of the URL so
    # every run (site-content calls this twice) keeps the same ones.
    allowed = None
    if args.all and args.max_unmatched:
        unmatched = defaultdict(list)
        for path in sorted({p for pat in args.candidates for p in glob.glob(pat)}):
            with gzip.open(path, "rt") as f:
                col = {c: i for i, c in enumerate(next(f).rstrip("\n").split("\t"))}
                for line in f:
                    row = line.rstrip("\n").split("\t")
                    if not matched(row, col):
                        unmatched[row[col["site"]]].append(row[col["url"]])
        allowed = set()
        for site, urls in unmatched.items():
            urls.sort(key=lambda u: hashlib.md5(u.encode()).hexdigest())
            allowed.update(urls[:args.max_unmatched])
    # Per-site cap on every page kept, ranked by URL hash like the above.
    capped = None
    if args.max_per_site:
        per_site = defaultdict(set)
        for path in sorted({p for pat in args.candidates for p in glob.glob(pat)}):
            with gzip.open(path, "rt") as f:
                col = {c: i for i, c in enumerate(next(f).rstrip("\n").split("\t"))}
                for line in f:
                    row = line.rstrip("\n").split("\t")
                    if only is not None and row[col["site"]] not in only:
                        continue
                    if skip and key(row[col["url"]]) in skip:
                        continue
                    if matched(row, col) or args.all:
                        per_site[row[col["site"]]].add(row[col["url"]])
        capped = set()
        for site, urls in per_site.items():
            capped.update(sorted(urls, key=lambda u: hashlib.md5(u.encode()).hexdigest())[:args.max_per_site])
    os.makedirs(args.out_dir, exist_ok=True)
    why = Counter()
    kept = total = named = 0
    sites = set()
    for path in sorted({p for pat in args.candidates for p in glob.glob(pat)}):
        # Several site-urls releases can share file names (candidates-part-0); keep them apart.
        parent = os.path.basename(os.path.dirname(os.path.abspath(path)))
        out = os.path.join(args.out_dir, f"{parent}-{os.path.basename(path)}")
        with gzip.open(path, "rt") as f, gzip.open(out, "wt") as o:
            header = next(f)
            o.write(header)
            col = {c: i for i, c in enumerate(header.rstrip("\n").split("\t"))}
            for line in f:
                total += 1
                row = line.rstrip("\n").split("\t")
                site = row[col["site"]]
                if site.lower() in excluded:
                    why["excluded_site"] += 1
                    continue
                if only is not None and site not in only:
                    why["not_in_site_list"] += 1
                    continue
                if skip and key(row[col["url"]]) in skip:
                    why["already_scored"] += 1
                    continue
                m = matched(row, col)
                if allowed is not None and not m and row[col["url"]] not in allowed:
                    why["over_unmatched_cap"] += 1
                    continue
                if not m and (args.names_only or args.stopword_names_only or not args.all):
                    why["no_name_in_url" if args.names_only or args.stopword_names_only else "no_slug_match"] += 1
                    continue
                if capped is not None and row[col["url"]] not in capped:
                    why["over_site_cap"] += 1
                    continue
                if m and row[col["slug_match"]] != "1":
                    named += 1
                    row[col["slug_match"]] = "1"
                    line = "\t".join(row) + "\n"
                r = urlrules.drop_reason(row[col["url"]], row[col["site"]])
                if r:
                    why[r] += 1
                    continue
                o.write(line)
                kept += 1
                sites.add(row[col["site"]])
    print(f"kept {kept:,} of {total:,} candidate URLs from {len(sites):,} sites")
    if args.index:
        print(f"  {named:,} rows matched by a hike or landform name in the URL (slugnames.py)")
    for k, v in why.most_common():
        print(f"  dropped {v:,}: {k}")


if __name__ == "__main__":
    main()
