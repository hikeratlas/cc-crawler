#!/usr/bin/env python3
"""First-cut Stage 3 scoring plus an evaluation report (stdlib only).

Reads one or more cmd/hubscore output directories (one per web graph
release), merges them, and writes:
  - a scored, ranked seed list (gzip TSV), and
  - a Markdown report: per-release coverage of the eval lists, hub
    in-degrees, recall on data/eval/known_blogs.csv (dev and holdout), where
    the negatives land, and the top-ranked domains.

Merging releases: a domain's hubs are the UNION of the hubs it linked to in
any release; its out-degree is the maximum across releases. A domain counts
as crawled in a release when it has at least one outlink there.

Score (see docs/DESIGN.md, Stage 3):
  idf(h)       = ln(sum of vertices over releases / sum of in_degree(h) over releases)
  specificity  = 1 / (1 + ln(1 + out_degree / 100))
  score        = specificity * sum over linked hubs of weight(h) * idf(h)

Usage:
  scripts/seed_report.py --run out/RELEASE_A --run out/RELEASE_B \
      --seeds-out seeds.tsv.gz --report-out report.md

Each --run directory holds domain_hub_scores.tsv.gz, hub_stats.tsv and
meta.json as written by cmd/hubscore.
"""
import argparse
import csv
import gzip
import json
import math
import os
import random
from collections import Counter


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_run(d):
    with open(os.path.join(d, "meta.json")) as f:
        meta = json.load(f)
    with open(os.path.join(d, "hub_stats.tsv")) as f:
        hubs = {r["hub"]: r for r in csv.DictReader(f, delimiter="\t")}
    rows = {}
    with gzip.open(os.path.join(d, "domain_hub_scores.tsv.gz"), "rt") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            rows[r["domain"]] = {
                "out_degree": int(r["out_degree"]),
                "n_hosts": int(r["n_hosts"]),
                "hubs": {h for h in r["hubs"].split(",") if h},
            }
    return meta, hubs, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", required=True, help="hubscore output directory; repeat per release")
    ap.add_argument("--known", default="data/eval/known_blogs.csv")
    ap.add_argument("--negatives", default="data/eval/negatives.csv")
    ap.add_argument("--seeds-out", required=True)
    ap.add_argument("--report-out", required=True)
    ap.add_argument("--min-hubs", type=int, default=2, help="seed threshold on distinct hubs")
    args = ap.parse_args()

    runs = [load_run(d) for d in args.run]
    runs.sort(key=lambda r: r[0]["release"], reverse=True)  # newest first
    releases = [m["release"] for m, _, _ in runs]
    total_vertices = sum(m["vertices"] for m, _, _ in runs)

    # pooled hub stats
    hubs = {}
    for _, hs, _ in runs:
        for name, h in hs.items():
            agg = hubs.setdefault(name, {"hub": name, "category": h["category"], "weight": float(h["weight"]), "in_degree": 0, "per": []})
            agg["in_degree"] += int(h["in_degree"])
            agg["per"].append(int(h["in_degree"]))
    for h in hubs.values():
        h["idf"] = math.log(total_vertices / h["in_degree"]) if h["in_degree"] > 0 else 0.0

    # merged domains
    merged = {}
    for i, (_, _, rows) in enumerate(runs):
        for dom, r in rows.items():
            m = merged.setdefault(dom, {"domain": dom, "hubs": set(), "out_degree": 0, "n_hosts": 0,
                                        "per": [None] * len(runs)})
            m["hubs"] |= r["hubs"]
            m["out_degree"] = max(m["out_degree"], r["out_degree"])
            m["n_hosts"] = max(m["n_hosts"], r["n_hosts"])
            m["per"][i] = r
    rows = []
    for m in merged.values():
        linked = [h for h in m["hubs"] if h in hubs]
        spec = 1.0 / (1.0 + math.log(1.0 + m["out_degree"] / 100.0))
        m["n_hubs"] = len(linked)
        cats = sorted({hubs[h]["category"] for h in linked})
        m["categories"] = ",".join(cats)
        m["n_cats"] = len(cats)
        m["score"] = spec * sum(hubs[h]["weight"] * hubs[h]["idf"] for h in linked)
        m["crawled_in"] = sum(1 for p in m["per"] if p and p["out_degree"] > 0)
        rows.append(m)
    rows.sort(key=lambda r: (-r["score"], r["domain"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    by_domain = {r["domain"]: r for r in rows}

    with gzip.open(args.seeds_out, "wt") as f:
        cols = ["rank", "domain", "score", "n_hubs", "n_cats", "out_degree", "n_hosts", "crawled_in", "categories", "hubs"]
        f.write("\t".join(cols) + "\n")
        for r in rows:
            if r["n_hubs"] >= 1:
                vals = {**r, "hubs": ",".join(sorted(r["hubs"])), "score": f"{r['score']:.3f}"}
                f.write("\t".join(str(vals[c]) for c in cols) + "\n")

    linked = [r for r in rows if r["n_hubs"] >= 1]
    seeds = [r for r in linked if r["n_hubs"] >= args.min_hubs]
    known = read_csv(args.known)
    negs = read_csv(args.negatives)
    short = [rel.replace("cc-main-", "") for rel in releases]
    L = []
    w = L.append

    w(f"# Stage 2–3 report: {len(releases)} graph release{'s' if len(releases) > 1 else ''}\n")
    w("Generated by `scripts/seed_report.py` from `cmd/hubscore` output. Scores are a first cut; see DESIGN.md Stage 3.\n")
    w("Releases (newest first): " + ", ".join(f"`{r}`" for r in releases) + "\n")

    w("## Funnel (union of releases)\n")
    w("| | domains |\n|---|---:|")
    for m, _, _ in runs:
        w(f"| in graph `{m['release']}` | {m['vertices']:,} |")
    w(f"| link to ≥ 1 hub | {len(linked):,} |")
    for k in (2, 3, 5, 8):
        w(f"| link to ≥ {k} hubs | {sum(1 for r in linked if r['n_hubs'] >= k):,} |")
    w(f"| ≥ {args.min_hubs} hubs in ≥ 2 categories | {sum(1 for r in seeds if r['n_cats'] >= 2):,} |")
    w("")

    w("## Coverage by release\n")
    w("How many eval domains each release actually crawled (≥ 1 outlink), and how many known blogs pass the seed threshold "
      f"(≥ {args.min_hubs} hubs) using that release alone. The last row is the union.\n")
    w("| release | domains with ≥ 2 hubs | known blogs crawled | known blogs ≥ threshold | negatives crawled |\n|---|---:|---:|---:|---:|")
    for i, (m, _, rr) in enumerate(runs):
        kc = sum(1 for it in known if rr.get(it["domain"], {}).get("out_degree", 0) > 0)
        kp = sum(1 for it in known if len(rr.get(it["domain"], {}).get("hubs", ())) >= args.min_hubs)
        nc = sum(1 for it in negs if rr.get(it["domain"], {}).get("out_degree", 0) > 0)
        n2 = sum(1 for r in rr.values() if len(r["hubs"]) >= 2)
        w(f"| `{m['release']}` | {n2:,} | {kc}/{len(known)} | {kp}/{len(known)} | {nc}/{len(negs)} |")
    kc = sum(1 for it in known if by_domain.get(it["domain"], {}).get("crawled_in", 0) > 0)
    kp = sum(1 for it in known if by_domain.get(it["domain"], {}).get("n_hubs", 0) >= args.min_hubs)
    nc = sum(1 for it in negs if by_domain.get(it["domain"], {}).get("crawled_in", 0) > 0)
    w(f"| **union** | {sum(1 for r in linked if r['n_hubs'] >= 2):,} | {kc}/{len(known)} | {kp}/{len(known)} | {nc}/{len(negs)} |")
    w("")

    w("## Recall on known blogs (union)\n")
    w(f"Share of known blogs that pass each threshold. The seed threshold is ≥ {args.min_hubs} distinct hubs.\n")
    w("| threshold | dev | holdout | all |\n|---|---:|---:|---:|")
    for k in (1, 2, 3, 5):
        cells = []
        for split in ("dev", "holdout", None):
            group = [it for it in known if split is None or it["split"] == split]
            hit = sum(1 for it in group if by_domain.get(it["domain"], {}).get("n_hubs", 0) >= k)
            cells.append(f"{hit}/{len(group)} ({100 * hit / max(1, len(group)):.0f}%)")
        w(f"| ≥ {k} hubs | " + " | ".join(cells) + " |")
    w("")

    def per_release(r):
        cells = []
        for p in r["per"]:
            if p is None:
                cells.append("·")
            elif p["out_degree"] == 0:
                cells.append("nc")
            else:
                cells.append(str(len(p["hubs"])))
        return cells

    def eval_table(items, title, note):
        w(f"## {title}\n")
        w(note + "\n")
        w("| domain | split | hubs (union) | " + " | ".join(short) + " | out-degree | score | rank |")
        w("|---|---|---:|" + "---:|" * len(short) + "---:|---:|---:|")
        for it in sorted(items, key=lambda it: (by_domain.get(it["domain"], {}).get("rank", 10**12), it["domain"])):
            r = by_domain.get(it["domain"])
            split = it.get("split", it.get("reason", ""))
            if not r:
                w(f"| {it['domain']} | {split} | not in any graph | " + " | ".join("·" for _ in short) + " | – | – | – |")
            elif r["crawled_in"] == 0:
                w(f"| {it['domain']} | {split} | never crawled | " + " | ".join(per_release(r)) + " | 0 | – | – |")
            else:
                w(f"| {it['domain']} | {split} | {r['n_hubs']} | " + " | ".join(per_release(r)) +
                  f" | {r['out_degree']:,} | {r['score']:.1f} | {r['rank']:,} |")
        w("")

    legend = ("Per-release columns: number of hubs linked in that release; `nc` = in the graph but not crawled "
              "(no outlinks, e.g. robots.txt or edge blocking of CCBot); `·` = not in that release's graph.")
    eval_table(known, "Known blogs", "Lower rank is better. A blog with 0–1 hubs is invisible to this stage. " + legend)
    eval_table(negs, "Negatives", "These should rank low or fail the threshold. Ones that score high tell us what Stage 3 must filter. " + legend)

    w("## Hubs by in-degree\n")
    w("Domains linking to each hub, summed over releases. Low in-degree hubs are the most specific signal (high idf).\n")
    w("| hub | category | weight | linking domains | " + " | ".join(short) + " | idf |")
    w("|---|---|---:|---:|" + "---:|" * len(short) + "---:|")
    for h in sorted(hubs.values(), key=lambda h: -h["in_degree"]):
        w(f"| {h['hub']} | {h['category']} | {h['weight']:.1f} | {h['in_degree']:,} | " +
          " | ".join(f"{x:,}" for x in h["per"]) + f" | {h['idf']:.2f} |")
    w("")

    w("## Top 75 by score\n")
    w("| rank | domain | hubs | categories | out-degree | crawled in | score |\n|---:|---|---:|---|---:|---:|---:|")
    for r in rows[:75]:
        w(f"| {r['rank']} | {r['domain']} | {r['n_hubs']} | {r['categories']} | {r['out_degree']:,} | {r['crawled_in']}/{len(runs)} | {r['score']:.1f} |")
    w("")

    rnd = random.Random(42)
    mid = sorted((r for r in seeds if r["n_cats"] >= 2), key=lambda r: r["domain"])
    sample = sorted(rnd.sample(mid, min(60, len(mid))), key=lambda r: r["rank"])
    w(f"## Random sample of 60 seeds (≥ {args.min_hubs} hubs, ≥ 2 categories)\n")
    w("For eyeballing precision across the whole seed set, not just the top.\n")
    w("| rank | domain | hubs | out-degree | crawled in | score |\n|---:|---|---:|---:|---:|---:|")
    for r in sample:
        w(f"| {r['rank']:,} | {r['domain']} | {r['n_hubs']} | {r['out_degree']:,} | {r['crawled_in']}/{len(runs)} | {r['score']:.1f} |")
    w("")

    tld = Counter(r["domain"].rsplit(".", 1)[-1] for r in seeds)
    w(f"## TLDs among seeds (≥ {args.min_hubs} hubs)\n")
    w("| tld | domains |\n|---|---:|")
    for t, n in tld.most_common(20):
        w(f"| .{t} | {n:,} |")
    w("")

    with open(args.report_out, "w") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    main()
