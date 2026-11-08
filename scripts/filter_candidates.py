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
import glob
import gzip
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import urlrules  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", nargs="+", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--all", action="store_true",
                    help="keep pages whose slug doesn't match too (candidates from site-urls posts/all mode)")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    why = Counter()
    kept = total = 0
    sites = set()
    for path in sorted({p for pat in args.candidates for p in glob.glob(pat)}):
        out = os.path.join(args.out_dir, os.path.basename(path))
        with gzip.open(path, "rt") as f, gzip.open(out, "wt") as o:
            header = next(f)
            o.write(header)
            col = {c: i for i, c in enumerate(header.rstrip("\n").split("\t"))}
            for line in f:
                total += 1
                row = line.rstrip("\n").split("\t")
                if row[col["slug_match"]] != "1" and not args.all:
                    why["no_slug_match"] += 1
                    continue
                r = urlrules.drop_reason(row[col["url"]], row[col["site"]])
                if r:
                    why[r] += 1
                    continue
                o.write(line)
                kept += 1
                sites.add(row[col["site"]])
    print(f"kept {kept:,} of {total:,} candidate URLs from {len(sites):,} sites")
    for k, v in why.most_common():
        print(f"  dropped {v:,}: {k}")


if __name__ == "__main__":
    main()
