#!/usr/bin/env python3
"""Stage 7: fetch and score every remaining candidate page.

  split   divide a candidate list into partitions for cmd/warcfetch, keeping
          each site's pages together
  score   score every fetched page with the relevance model and the
          personal/professional model, keep pages with relevance >=
          --threshold, and summarise each site (pages kept, mean personal
          score -> site_kind). Writes the final page list, a site table and
          a report.

Models: data/models/page_model.json (relevance) and data/models/kind_model.json
(personal vs organisation/business), both from scripts/train_page_model.py.
Stdlib only.

Usage:
  scripts/page_pass.py split --candidates candidates.tsv.gz --partitions 16 --out-dir plan/
  scripts/page_pass.py score --pages 'pages-*.jsonl.gz' --out-dir out/ [--threshold 0.3]
"""
import argparse
import csv
import glob
import gzip
import json
import os
import random
import statistics
import sys
import zlib
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from page_features import score as model_score  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "..", "data", "models")


def files(patterns):
    return sorted({p for pat in patterns for p in glob.glob(pat)})


def opener(path):
    return gzip.open(path, "rt") if path.endswith(".gz") else open(path)


def split(args):
    os.makedirs(args.out_dir, exist_ok=True)
    outs = [open(os.path.join(args.out_dir, f"part-{i}.tsv"), "w") for i in range(args.partitions)]
    counts = [0] * args.partitions
    header = None
    for path in files(args.candidates):
        with opener(path) as f:
            h = next(f)
            if header is None:
                header = h
                for o in outs:
                    o.write(h)
            col = {c: i for i, c in enumerate(h.rstrip("\n").split("\t"))}
            for line in f:
                site = line.split("\t", col["site"] + 1)[col["site"]]
                k = zlib.crc32(site.encode()) % args.partitions
                outs[k].write(line)
                counts[k] += 1
    for o in outs:
        o.close()
    print(f"{sum(counts):,} pages in {args.partitions} partitions (min {min(counts):,}, max {max(counts):,})")


def score(args):
    with open(args.model) as f:
        rel_model = json.load(f)
    with open(args.kind_model) as f:
        kind_model = json.load(f)

    os.makedirs(args.out_dir, exist_ok=True)
    pcols = ["url", "site", "p_relevant", "p_personal", "words", "title", "crawl", "timestamp",
             "warc_filename", "warc_record_offset", "warc_record_length"]
    sites = defaultdict(lambda: {"pages": 0, "fetched": 0, "kept": 0, "rel": [], "personal": []})
    n_pages = n_kept = 0
    with gzip.open(os.path.join(args.out_dir, "pages.tsv.gz"), "wt") as out:
        out.write("\t".join(pcols) + "\n")
        for path in files(args.pages):
            with opener(path) as f:
                for line in f:
                    r = json.loads(line)
                    row = r["row"]
                    s = sites[row["site"]]
                    s["pages"] += 1
                    n_pages += 1
                    if r.get("error") or r.get("http_status") != 200:
                        continue
                    s["fetched"] += 1
                    p_rel = model_score(r, rel_model)
                    s["rel"].append(p_rel)
                    if p_rel < args.threshold:
                        continue
                    p_per = model_score(r, kind_model) if r["words"] >= 100 else None
                    if p_per is not None:
                        s["personal"].append(p_per)
                    s["kept"] += 1
                    n_kept += 1
                    vals = {"url": r["url"], "site": row["site"], "p_relevant": f"{p_rel:.3f}",
                            "p_personal": "" if p_per is None else f"{p_per:.3f}", "words": r["words"],
                            "title": (r.get("title") or "").replace("\t", " ")[:200],
                            **{k: row.get(k, "") for k in ("crawl", "timestamp", "warc_filename",
                                                          "warc_record_offset", "warc_record_length")}}
                    out.write("\t".join(str(vals[c]) for c in pcols) + "\n")

    rows = []
    for site, s in sites.items():
        personal = statistics.mean(s["personal"]) if s["personal"] else None
        kind = "" if personal is None else ("personal" if personal >= args.personal_threshold else "organisation")
        rows.append({"site": site, "pages": s["pages"], "fetched": s["fetched"], "kept": s["kept"],
                     "mean_relevance": statistics.mean(s["rel"]) if s["rel"] else 0.0,
                     "personal_score": personal, "site_kind": kind})
    rows.sort(key=lambda r: -r["kept"])
    scols = ["site", "site_kind", "personal_score", "kept", "fetched", "pages", "mean_relevance"]
    fmt = lambda v: "" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))
    with gzip.open(os.path.join(args.out_dir, "sites.tsv.gz"), "wt") as f:
        f.write("\t".join(scols) + "\n")
        for r in rows:
            f.write("\t".join(fmt(r[c]) for c in scols) + "\n")

    # ---- report
    by_site = {r["site"]: r for r in rows}
    with open(args.known, newline="") as f:
        known = list(csv.DictReader(f))
    with_pages = [r for r in rows if r["kept"] > 0]
    kinds = defaultdict(lambda: [0, 0])
    for r in with_pages:
        kinds[r["site_kind"] or "unknown"][0] += 1
        kinds[r["site_kind"] or "unknown"][1] += r["kept"]
    fetched = sum(r["fetched"] for r in rows)
    L = []
    w = L.append
    w("# Stage 7 report: every candidate page fetched and scored\n")
    w(f"Generated by `scripts/page_pass.py score`. A page is kept when its relevance score is ≥ {args.threshold}; "
      f"a site is `personal` when the mean personal score of its kept pages is ≥ {args.personal_threshold}.\n")
    w("| | pages | sites |\n|---|---:|---:|")
    w(f"| candidates | {n_pages:,} | {len(rows):,} |")
    w(f"| fetched OK | {fetched:,} | |")
    w(f"| **kept** | **{n_kept:,}** | **{len(with_pages):,}** |")
    w("")
    w("## Kept pages by site kind\n")
    w("| site kind | sites | pages |\n|---|---:|---:|")
    for k, (ns, npg) in sorted(kinds.items(), key=lambda kv: -kv[1][1]):
        w(f"| {k} | {ns:,} | {npg:,} |")
    w("")
    w("## Known blogs (site kind vs. the list's authorship label)\n")
    w("| site | authorship (list) | predicted kind | personal score | pages kept |\n|---|---|---|---:|---:|")
    agree = total = 0
    for it in known:
        r = by_site.get(it["domain"])
        if not r or not r["site_kind"]:
            w(f"| {it['domain']} | {it.get('authorship', '')} | – | – | {r['kept'] if r else 0} |")
            continue
        want = "personal" if it.get("authorship") == "personal" else "organisation"
        total += 1
        agree += want == r["site_kind"]
        w(f"| {it['domain']} | {it.get('authorship', '')} | {r['site_kind']} | {r['personal_score']:.2f} | {r['kept']:,} |")
    if total:
        w(f"\nAgreement with the list: {agree}/{total} ({100 * agree / total:.0f}%).\n")
    rnd = random.Random(5)
    for k in ("personal", "organisation"):
        group = [r for r in with_pages if r["site_kind"] == k]
        w(f"## Random {k} sites\n")
        w("| site | personal score | pages kept |\n|---|---:|---:|")
        for r in sorted(rnd.sample(group, min(30, len(group))), key=lambda r: -r["personal_score"]):
            w(f"| {r['site']} | {r['personal_score']:.2f} | {r['kept']:,} |")
        w("")
    with open(os.path.join(args.out_dir, "report.md"), "w") as f:
        f.write("\n".join(L))
    print(f"kept {n_kept:,} of {n_pages:,} pages from {len(with_pages):,} sites")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("split")
    s.add_argument("--candidates", nargs="+", required=True)
    s.add_argument("--partitions", type=int, default=16)
    s.add_argument("--out-dir", required=True)
    c = sub.add_parser("score")
    c.add_argument("--pages", nargs="+", required=True, help="warcfetch outputs (.jsonl or .jsonl.gz; globs allowed)")
    c.add_argument("--model", default=os.path.join(MODELS, "page_model.json"))
    c.add_argument("--kind-model", default=os.path.join(MODELS, "kind_model.json"))
    c.add_argument("--threshold", type=float, default=0.3)
    c.add_argument("--personal-threshold", type=float, default=0.5)
    c.add_argument("--known", default=os.path.join(HERE, "..", "data", "eval", "known_blogs.csv"))
    c.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    split(args) if args.cmd == "split" else score(args)


if __name__ == "__main__":
    main()
