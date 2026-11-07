#!/usr/bin/env python3
"""Stage 7: draw a stratified random sample of candidate URLs for checking.

Strata are (site_verdict, slug_match): e.g. pages from `hiking` sites whose
URL has a hiking/place slug, and those whose URL doesn't. Each stratum gets
its own sample size so precision can be reported per stratum, and the
population size of every stratum is written alongside, so overall precision
can be estimated by weighting. At most --per-site pages per site per stratum,
so a few large sites can't dominate.

Usage:
  scripts/sample_candidates.py --candidates 'candidates-part-*.tsv.gz' \
      --sizes hiking:1=60,hiking:0=60,mixed:1=60,drifted:1=20 --out sample.tsv
"""
import argparse
import glob
import gzip
import random
from collections import Counter, defaultdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", nargs="+", required=True)
    ap.add_argument("--sizes", required=True, help="verdict:slug=n,… (slug is 0 or 1)")
    ap.add_argument("--per-site", type=int, default=2)
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    sizes = {}
    for part in args.sizes.split(","):
        k, n = part.split("=")
        verdict, slug = k.split(":")
        sizes[(verdict, slug)] = int(n)

    rnd = random.Random(args.seed)
    # Keep a generous reservoir per stratum (random key, smallest K), then
    # apply the per-site cap when drawing the final sample.
    reservoir = {k: [] for k in sizes}
    keep = {k: 20 * n for k, n in sizes.items()}
    population = Counter()
    header = None
    files = sorted({p for pat in args.candidates for p in glob.glob(pat)})
    for path in files:
        with gzip.open(path, "rt") as f:
            hdr = next(f).rstrip("\n").split("\t")
            header = header or hdr
            col = {c: i for i, c in enumerate(hdr)}
            for line in f:
                row = line.rstrip("\n").split("\t")
                k = (row[col["site_verdict"]], row[col["slug_match"]])
                population[k] += 1
                if k not in reservoir:
                    continue
                r = reservoir[k]
                key = rnd.random()
                if len(r) < keep[k]:
                    r.append((key, row))
                elif key < r[-1][0]:
                    r[-1] = (key, row)
                else:
                    continue
                r.sort(key=lambda x: x[0])

    col = {c: i for i, c in enumerate(header)}
    with open(args.out, "w") as out:
        out.write("stratum\tstratum_population\t" + "\t".join(header) + "\n")
        for k, n in sizes.items():
            per_site = defaultdict(int)
            chosen = 0
            for _, row in reservoir[k]:
                site = row[col["site"]]
                if per_site[site] >= args.per_site:
                    continue
                per_site[site] += 1
                out.write(f"{k[0]}:{k[1]}\t{population[k]}\t" + "\t".join(row) + "\n")
                chosen += 1
                if chosen == n:
                    break
            print(f"{k[0]}:{k[1]}: population {population[k]:,}, sampled {chosen}")
    other = {k: v for k, v in population.items() if k not in sizes}
    if other:
        print("not sampled:", ", ".join(f"{a}:{b}={v:,}" for (a, b), v in sorted(other.items())))


if __name__ == "__main__":
    main()
