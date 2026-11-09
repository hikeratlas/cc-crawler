"""Every URL a page pass has already tried, as one TSV (column: url).

Reads a page-pass release's scored.tsv.gz (every page tried) or, for older
releases without it, the fetched page text (pages-part-*.jsonl.gz), which
also holds every page tried. pages.tsv.gz alone is not enough: it lists only
the kept pages, so skipping by it re-fetches every rejected page.

  scripts/scored_urls.py --out skip.tsv.gz rel1/scored.tsv.gz 'rel2/pages-part-*.jsonl.gz'
"""
import argparse
import glob
import gzip
import json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    n = 0
    with gzip.open(args.out, "wt") as o:
        o.write("url\n")
        for path in sorted({p for pat in args.inputs for p in glob.glob(pat)}):
            with gzip.open(path, "rt") as f:
                if path.endswith(".jsonl.gz"):
                    for line in f:
                        o.write(json.loads(line)["url"] + "\n")
                        n += 1
                else:
                    col = next(f).rstrip("\n").split("\t").index("url")
                    for line in f:
                        o.write(line.rstrip("\n").split("\t")[col] + "\n")
                        n += 1
    print(f"{n:,} scored URLs")


if __name__ == "__main__":
    main()
