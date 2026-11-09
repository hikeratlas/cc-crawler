#!/usr/bin/env python3
"""Stage 7: judge sites by the content of a few of their pages.

  sample  pick up to --per-site random candidate URLs from every site in a
          (URL-rule-filtered) candidate list, split into partitions for
          cmd/warcfetch
  score   score the fetched pages with the page model, average per site,
          keep or drop each site, and write the final candidate list (the
          filtered candidates of kept sites) plus a report

The page model is data/models/page_model.json (scripts/train_page_model.py).
Stdlib only.

Usage:
  scripts/site_content.py sample --candidates 'filtered/*.tsv.gz' --per-site 5 --partitions 8 --out-dir plan/
  scripts/site_content.py score --pages 'pages-*.jsonl' --candidates 'filtered/*.tsv.gz' \
      --out-dir out/ [--site-threshold 0.35]
"""
import argparse
import csv
import glob
import gzip
import json
import os
import re
import random
import statistics
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from page_features import score as page_score  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


MIN_WORDS = 50


def files(patterns):
    return sorted({p for pat in patterns for p in glob.glob(pat)})


def sample(args):
    rnd = random.Random(args.seed)
    reservoirs = defaultdict(list)  # site -> [(key, line)]
    seen = Counter()
    header = None
    for path in files(args.candidates):
        with gzip.open(path, "rt") as f:
            h = next(f)
            header = header or h
            col = {c: i for i, c in enumerate(h.rstrip("\n").split("\t"))}
            for line in f:
                site = line.split("\t", col["site"] + 1)[col["site"]]
                seen[site] += 1
                r = reservoirs[site]
                if len(r) < args.per_site:
                    r.append(line)
                else:
                    j = rnd.randrange(seen[site])
                    if j < args.per_site:
                        r[j] = line
    sites = sorted(reservoirs)
    rnd.shuffle(sites)
    os.makedirs(args.out_dir, exist_ok=True)
    outs = [open(os.path.join(args.out_dir, f"sample-{i}.tsv"), "w") for i in range(args.partitions)]
    for o in outs:
        o.write(header)
    n = 0
    for k, site in enumerate(sites):
        for line in reservoirs[site]:
            outs[k % args.partitions].write(line)
            n += 1
    for o in outs:
        o.close()
    print(f"{len(sites):,} sites, {n:,} sampled URLs in {args.partitions} partitions")


def score(args):
    with open(args.model) as f:
        model = json.load(f)
    per_site = defaultdict(list)
    for path in files(args.pages):
        with open(path) as f:
            for line in f:
                r = json.loads(line)
                site = r["row"]["site"]
                if r.get("error"):
                    per_site[site].append(None)
                    continue
                per_site[site].append((page_score(r, model), r["words"], r["url"], r.get("title", "")))

    rows = []
    for site, pages in per_site.items():
        ok = [p for p in pages if p]
        # Pages under MIN_WORDS (image attachment pages, "One moment, please..."
        # bot-check interstitials captured by the crawl) say nothing about the
        # site; leave them out of its mean unless they're all there is.
        probs = [p[0] for p in ok if (p[1] or 0) >= MIN_WORDS] or [p[0] for p in ok]
        mean_p = statistics.mean(probs) if probs else 0.0
        share = sum(1 for p in probs if p >= 0.5) / len(probs) if probs else 0.0
        keep = len(ok) > 0 and mean_p >= args.site_threshold
        best = max(ok, key=lambda p: p[0]) if ok else None
        rows.append({"site": site, "keep": int(keep), "pages": len(ok), "errors": len(pages) - len(ok),
                     "mean_p": mean_p, "share_relevant": share,
                     "median_words": int(statistics.median([p[1] for p in ok])) if ok else 0,
                     "best_url": best[2] if best else "", "best_title": (best[3] if best else "").replace("\t", " ")[:120]})
    rows.sort(key=lambda r: -r["mean_p"])
    kept = {r["site"] for r in rows if r["keep"]}

    os.makedirs(args.out_dir, exist_ok=True)
    cols = ["site", "keep", "pages", "errors", "mean_p", "share_relevant", "median_words", "best_url", "best_title"]
    with gzip.open(os.path.join(args.out_dir, "site_content.tsv.gz"), "wt") as f:
        f.write("\t".join(cols) + "\n")
        for r in rows:
            f.write("\t".join(f"{r[c]:.3f}" if isinstance(r[c], float) else str(r[c]) for c in cols) + "\n")

    n_in = n_out = 0
    out_path = os.path.join(args.out_dir, "candidates.tsv.gz")
    # Candidates can come from several site-urls releases: write every row in
    # the first file's column order and keep each URL once.
    out_cols, seen_urls = None, set()
    with gzip.open(out_path, "wt") as o:
        for path in files(args.candidates):
            with gzip.open(path, "rt") as f:
                h = next(f).rstrip("\n").split("\t")
                if out_cols is None:
                    out_cols = h
                    o.write("\t".join(out_cols) + "\n")
                col = {c: i for i, c in enumerate(h)}
                same = h == out_cols
                for line in f:
                    n_in += 1
                    row = line.rstrip("\n").split("\t")
                    if row[col["site"]] not in kept:
                        continue
                    u = re.sub(r"^https?://(www\.)?", "", row[col["url"]].lower()).rstrip("/")
                    if u in seen_urls:
                        continue
                    seen_urls.add(u)
                    o.write(line if same else "\t".join(row[col[c]] if c in col and col[c] < len(row) else ""
                                                       for c in out_cols) + "\n")
                    n_out += 1

    # ---- report
    def read_csv(p):
        with open(p, newline="") as f:
            return list(csv.DictReader(f))
    by_site = {r["site"]: r for r in rows}
    L = []
    w = L.append
    w("# Stage 7 report: sites judged by page content\n")
    w(f"Generated by `scripts/site_content.py score`. Page model: `{os.path.relpath(args.model, os.path.join(HERE, '..'))}` "
      f"(trained on {model.get('trained_on', '?')} labelled pages). A site is kept when the mean relevance of its "
      f"sampled pages is ≥ {args.site_threshold}.\n")
    w("| | sites | candidate URLs |\n|---|---:|---:|")
    w(f"| scored | {len(rows):,} | {n_in:,} |")
    w(f"| **kept** | **{len(kept):,}** | **{n_out:,}** |")
    w(f"| dropped | {len(rows) - len(kept):,} | {n_in - n_out:,} |")
    w("")
    w("## Distribution of site scores\n")
    w("| mean page relevance | sites |\n|---|---:|")
    for lo in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        w(f"| {lo:.1f}–{lo + 0.1:.1f} | {sum(1 for r in rows if lo <= r['mean_p'] < lo + 0.1 or (lo == 0.9 and r['mean_p'] >= 1.0)):,} |")
    w("")
    for title, path in (("Known blogs", args.known), ("Negatives", args.negatives)):
        w(f"## {title}\n")
        w("| site | kept | pages | mean relevance | best page |\n|---|---|---:|---:|---|")
        for it in read_csv(path):
            r = by_site.get(it["domain"])
            if not r:
                w(f"| {it['domain']} | not sampled | | | |")
            else:
                w(f"| {r['site']} | {'yes' if r['keep'] else 'no'} | {r['pages']} | {r['mean_p']:.2f} | {r['best_title'][:70]} |")
        w("")
    rnd = random.Random(3)
    for label, group in (("kept", [r for r in rows if r["keep"]]), ("dropped", [r for r in rows if not r["keep"]])):
        w(f"## Random sample of {label} sites\n")
        w("| site | mean relevance | best page |\n|---|---:|---|")
        for r in sorted(rnd.sample(group, min(30, len(group))), key=lambda r: -r["mean_p"]):
            w(f"| {r['site']} | {r['mean_p']:.2f} | {r['best_title'][:80]} |")
        w("")
    with open(os.path.join(args.out_dir, "report.md"), "w") as f:
        f.write("\n".join(L))
    print(f"kept {len(kept):,} of {len(rows):,} sites; {n_out:,} of {n_in:,} candidate URLs")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--candidates", nargs="+", required=True)
    s.add_argument("--per-site", type=int, default=5)
    s.add_argument("--partitions", type=int, default=8)
    s.add_argument("--seed", type=int, default=20261108)
    s.add_argument("--out-dir", required=True)
    c = sub.add_parser("score")
    c.add_argument("--pages", nargs="+", required=True)
    c.add_argument("--candidates", nargs="+", required=True)
    c.add_argument("--model", default=os.path.join(HERE, "..", "data", "models", "page_model.json"))
    c.add_argument("--site-threshold", type=float, default=0.35)
    c.add_argument("--known", default=os.path.join(HERE, "..", "data", "eval", "known_blogs.csv"))
    c.add_argument("--negatives", default=os.path.join(HERE, "..", "data", "eval", "negatives.csv"))
    c.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    sample(args) if args.cmd == "sample" else score(args)


if __name__ == "__main__":
    main()
