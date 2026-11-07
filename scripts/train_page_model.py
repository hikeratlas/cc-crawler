#!/usr/bin/env python3
"""Fit the page-relevance model (logistic regression over page_features.py)
on hand-labelled spot-check pages, report held-out precision/recall, and
save the weights as JSON. Stdlib only.

A page counts as relevant ("broad") if it's about a specific trail, hike,
destination or national park in the US/Canada (topic T, geo U), or general
hiking content not clearly elsewhere (topic H, geo U or ?).

Usage:
  scripts/train_page_model.py --labels A.tsv --pages A.jsonl [--labels B.tsv --pages B.jsonl] \
      --out data/models/page_model.json
"""
import argparse
import csv
import json
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from page_features import features  # noqa: E402


def relevant(r):
    return (r["topic"] == "T" and r["geo"] == "U") or (r["topic"] == "H" and r["geo"] in ("U", "?"))


def fit(X, y, l2=1.0, iters=3000, lr=0.1):
    d = len(X[0])
    w = [0.0] * d
    b = 0.0
    n = len(X)
    for _ in range(iters):
        gw = [0.0] * d
        gb = 0.0
        for xi, yi in zip(X, y):
            p = 1 / (1 + math.exp(-(b + sum(a * c for a, c in zip(w, xi)))))
            e = p - yi
            gb += e
            for j in range(d):
                gw[j] += e * xi[j]
        b -= lr * gb / n
        w = [wj - lr * (gj / n + l2 * wj / n) for wj, gj in zip(w, gw)]
    return w, b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", action="append", required=True)
    ap.add_argument("--pages", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--holdout", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rows = []
    for lab_path, page_path in zip(args.labels, args.pages):
        labels = {int(r["idx"]): r for r in csv.DictReader(open(lab_path), delimiter="\t")}
        for i, line in enumerate(open(page_path)):
            rows.append((features(json.loads(line)), 1 if relevant(labels[i]) else 0))
    keys = sorted(rows[0][0])
    rnd = random.Random(args.seed)
    rnd.shuffle(rows)
    cut = int(len(rows) * (1 - args.holdout))
    train, test = rows[:cut], rows[cut:]
    mean = {k: sum(r[0][k] for r in train) / len(train) for k in keys}
    std = {k: (math.sqrt(sum((r[0][k] - mean[k]) ** 2 for r in train) / len(train)) or 1.0) for k in keys}
    norm = lambda f: [(f[k] - mean[k]) / std[k] for k in keys]
    w, b = fit([norm(f) for f, _ in train], [y for _, y in train])
    model = {"bias": b, "weights": dict(zip(keys, w)), "mean": mean, "std": std,
             "trained_on": len(train), "base_rate": sum(y for _, y in rows) / len(rows)}

    def prob(f):
        return 1 / (1 + math.exp(-(b + sum(a * c for a, c in zip(w, norm(f))))))

    print(f"{len(rows)} labelled pages ({sum(y for _, y in rows)} relevant); train {len(train)}, held out {len(test)}")
    print("weights (standardised):", ", ".join(f"{k} {v:+.2f}" for k, v in sorted(zip(keys, w), key=lambda kv: -abs(kv[1]))))
    print("held-out precision / recall by threshold:")
    total = sum(y for _, y in test)
    for t in (0.3, 0.4, 0.5, 0.6, 0.7):
        sel = [y for f, y in test if prob(f) >= t]
        tp = sum(sel)
        print(f"  p >= {t:.1f}: keeps {len(sel):3d}/{len(test)}, precision {tp / max(1, len(sel)):.0%}, recall {tp / max(1, total):.0%}")
    model["holdout"] = {str(t): None for t in ()}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(model, f, indent=2, sort_keys=True)
    print("wrote", args.out)


if __name__ == "__main__":
    main()
