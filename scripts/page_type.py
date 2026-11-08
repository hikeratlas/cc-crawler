#!/usr/bin/env python3
"""Page-type model: trip_report, trail_guide, list_article, area_guide, other.

  features(title, text, url, n_features)  -> dict of numeric features
  predict(model, feats)                   -> (type, probabilities)
  train  --labels L.tsv --sample S.tsv --pages 'pages-*.jsonl.gz' --extracted 'extracted-*.jsonl.gz'
         fit a multinomial logistic regression (stdlib), report cross-validated
         accuracy against the rule-based types, and save the model JSON.

Labels: data/eval/page_types.tsv (id, url, type). Stdlib only.
"""
import argparse
import csv
import glob
import gzip
import json
import math
import os
import random
import re
from collections import Counter, defaultdict

TYPES = ("trip_report", "trail_guide", "list_article", "area_guide", "other")

W = re.compile(r"[A-Za-z']+")
FIRST_SING = re.compile(r"\b(I|my|me|I'm|I've|I'd)\b")
FIRST_PLUR = re.compile(r"\b(we|our|us|we're|we've|we'd)\b", re.I)
YOU = re.compile(r"\b(you|your|you'll|you're)\b", re.I)
PAST = re.compile(r"\b(i|we)\s+(hiked|started|reached|arrived|climbed|walked|camped|decided|made|headed|parked|"
                  r"set|got|turned|summited|continued|spent|saw|left|took|found|had|were|was|drove|woke|ate)\b", re.I)
TIME_STORY = re.compile(r"\b(yesterday|this morning|last (?:week|weekend|year|summer|fall|spring|winter)|"
                        r"the next morning|that night|day \d+|mile \d+)\b", re.I)
FIELDS = re.compile(r"\b(distance|length|elevation gain|difficulty|trailhead|trail type|route type|dogs|"
                    r"best season|time required|hiking time|permits?|elevation|surface|duration)\s*:", re.I)
DIRECTIONS = re.compile(r"\b(turn (?:left|right)|bear (?:left|right)|follow the|continue (?:on|along|straight)|"
                        r"the trail (?:climbs|descends|follows|crosses)|at the junction|from the trailhead)\b", re.I)
NUMBERED = re.compile(r"(?:^|\s)(?:#?\d{1,2}[.)]|#\d{1,2})\s+[A-Z]")
AREA = re.compile(r"\b(things to do|where to stay|when to visit|best time to visit|how to get there|lodging|"
                  r"campgrounds?|visitor cent(?:er|re)|entrance fee|itinerary|restaurants?|hotels?)\b", re.I)
GEAR = re.compile(r"\b(review|price|\$\d|ounces|oz\b|grams|waterproof|buy|shop|product|brand|tested|"
                  r"pros|cons|cart|shipping)\b", re.I)
NEWS = re.compile(r"\b(event|register|registration|meeting|volunteers?|press release|announce[ds]?|"
                  r"members?|donate|board|podcast|episode|race|results)\b", re.I)
SKILL = re.compile(r"\b(tips?|how to|beginners?|safety|training|checklist|should you|mistakes)\b", re.I)
T_LIST = re.compile(r"(^\s*\d{1,2}\s)|\b(best|top \d+|\d{1,2} (?:best|great|easy|amazing|epic|awesome|favorite|"
                    r"beautiful|scenic|hikes|trails|waterfalls)|hikes (?:in|near)|trails (?:in|near))\b", re.I)
T_AREA = re.compile(r"\b(guide|things to do|visitor|visiting|itinerary|travel|vacation|weekend in|days in)\b", re.I)
T_REPORT = re.compile(r"\b(trip report|day \d+|part \d+|recap|journal|my |our |we )", re.I)
T_OTHER = re.compile(r"\b(review|tips|how to|gear|news|event|podcast|episode|meeting|race|shop|sale|"
                     r"archives?|category|tag|page \d+)\b", re.I)
T_TRAIL = re.compile(r"\b(trail|hike|loop|peak|summit|mount|mt\.?|falls|lake|ridge|canyon|via)\b", re.I)
T_YEAR = re.compile(r"\b(19|20)\d\d\b")
U_DATE = re.compile(r"/(19|20)\d\d/\d{1,2}/")
U_TRAIL = re.compile(r"/(trails?|hikes?|hiking-trails|routes?)/", re.I)
U_OTHER = re.compile(r"/(gear|reviews?|news|events?|shop|products?|tag|category|podcast|store)/", re.I)


def rate(rx, text, words):
    return math.log1p(1000 * len(rx.findall(text)) / max(words, 50))


def features(title, text, url, n_features=0):
    title = title or ""
    text = text or ""
    words = len(W.findall(text))
    has = lambda rx, s: 1.0 if rx.search(s) else 0.0
    return {
        "log_words": math.log1p(words) / 8,
        "first_sing": rate(FIRST_SING, text, words),
        "first_plur": rate(FIRST_PLUR, text, words),
        "you": rate(YOU, text, words),
        "past": rate(PAST, text, words),
        "time_story": rate(TIME_STORY, text, words),
        "fields": math.log1p(len(FIELDS.findall(text))),
        "directions": rate(DIRECTIONS, text, words),
        "numbered": math.log1p(len(NUMBERED.findall(text))),
        "area": rate(AREA, text, words),
        "gear": rate(GEAR, text, words),
        "news": rate(NEWS, text, words),
        "skill": rate(SKILL, text, words),
        "t_list": has(T_LIST, title),
        "t_area": has(T_AREA, title),
        "t_report": has(T_REPORT, title),
        "t_other": has(T_OTHER, title),
        "t_trail": has(T_TRAIL, title),
        "t_year": has(T_YEAR, title),
        "u_date": has(U_DATE, url),
        "u_trail": has(U_TRAIL, url),
        "u_other": has(U_OTHER, url),
        "n_features": math.log1p(n_features),
    }


def predict(model, feats):
    names = model["features"]
    x = [(feats[n] - m) / s for n, m, s in zip(names, model["mean"], model["std"])]
    logits = [b + sum(wj * xj for wj, xj in zip(w, x)) for w, b in zip(model["w"], model["b"])]
    mx = max(logits)
    e = [math.exp(v - mx) for v in logits]
    z = sum(e)
    probs = {t: v / z for t, v in zip(model["types"], e)}
    return max(probs, key=probs.get), probs


def fit(X, y, k, l2=1.0, iters=1500, lr=0.3):
    d = len(X[0])
    n = len(X)
    W_ = [[0.0] * d for _ in range(k)]
    B = [0.0] * k
    for _ in range(iters):
        gW = [[0.0] * d for _ in range(k)]
        gB = [0.0] * k
        for xi, yi in zip(X, y):
            logits = [B[c] + sum(a * b for a, b in zip(W_[c], xi)) for c in range(k)]
            mx = max(logits)
            e = [math.exp(v - mx) for v in logits]
            z = sum(e)
            for c in range(k):
                err = e[c] / z - (1.0 if c == yi else 0.0)
                gB[c] += err
                row = gW[c]
                for j in range(d):
                    row[j] += err * xi[j]
        for c in range(k):
            B[c] -= lr * gB[c] / n
            W_[c] = [w - lr * (g / n + l2 * w / n) for w, g in zip(W_[c], gW[c])]
    return W_, B


def standardise(rows, names):
    cols = list(zip(*[[r[n] for n in names] for r in rows]))
    mean = [sum(c) / len(c) for c in cols]
    std = [max(1e-6, (sum((v - m) ** 2 for v in c) / len(c)) ** 0.5) for c, m in zip(cols, mean)]
    return mean, std


def train(args):
    with open(args.labels) as f:
        labels = {r["url"]: r["type"] for r in csv.DictReader(f, delimiter="\t")}
    ex = {}
    for p in sorted({p for pat in args.extracted for p in glob.glob(pat)}):
        with gzip.open(p, "rt") as f:
            for line in f:
                r = json.loads(line)
                if r["url"] in labels:
                    ex[r["url"]] = r
    rows = []
    for p in sorted({p for pat in args.pages for p in glob.glob(pat)}):
        with gzip.open(p, "rt") as f:
            for line in f:
                pg = json.loads(line)
                u = pg["url"]
                if u in labels and u in ex:
                    feats = features(pg.get("title"), pg.get("text"), u, len(ex[u]["features"]))
                    rows.append((u, feats, TYPES.index(labels[u]), ex[u]["page_type"]))
    names = list(rows[0][1])
    print(f"{len(rows)} labelled pages; types: {Counter(TYPES[r[2]] for r in rows)}")

    # cross-validated accuracy and confusion
    rnd = random.Random(args.seed)
    idx = list(range(len(rows)))
    rnd.shuffle(idx)
    folds = [idx[i::5] for i in range(5)]
    conf = Counter()
    correct = rule_correct = 0
    rule_map = {"trip_report": "trip_report", "trail_guide": "trail_guide", "list_article": "list_article",
                "article": "other", "general": "other"}
    for fold in folds:
        test = set(fold)
        tr = [rows[i] for i in idx if i not in test]
        mean, std = standardise([r[1] for r in tr], names)
        X = [[(r[1][n] - m) / s for n, m, s in zip(names, mean, std)] for r in tr]
        w, b = fit(X, [r[2] for r in tr], len(TYPES), l2=args.l2)
        model = {"types": TYPES, "features": names, "mean": mean, "std": std, "w": w, "b": b}
        for i in fold:
            u, feats, yi, rule = rows[i]
            pred, _ = predict(model, feats)
            conf[(TYPES[yi], pred)] += 1
            correct += pred == TYPES[yi]
            rule_correct += rule_map.get(rule, "other") == TYPES[yi]
    n = len(rows)
    print(f"cross-validated accuracy: model {correct / n:.0%}, rules {rule_correct / n:.0%}")
    print("confusion (rows = label, cols = predicted):")
    print("".ljust(14) + "".join(t[:10].rjust(12) for t in TYPES))
    for t in TYPES:
        print(t.ljust(14) + "".join(str(conf[(t, p)]).rjust(12) for p in TYPES))
    for t in TYPES:
        tp = conf[(t, t)]
        pp = sum(conf[(x, t)] for x in TYPES)
        ap = sum(conf[(t, x)] for x in TYPES)
        print(f"  {t:13} precision {tp / pp if pp else 0:.0%}  recall {tp / ap if ap else 0:.0%}")

    mean, std = standardise([r[1] for r in rows], names)
    X = [[(r[1][n_] - m) / s for n_, m, s in zip(names, mean, std)] for r in rows]
    w, b = fit(X, [r[2] for r in rows], len(TYPES), l2=args.l2)
    with open(args.out, "w") as f:
        json.dump({"types": TYPES, "features": names, "mean": mean, "std": std, "w": w, "b": b}, f, indent=1)
    print(f"wrote {args.out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--labels", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data",
                                                    "eval", "page_types.tsv"))
    t.add_argument("--pages", nargs="+", required=True)
    t.add_argument("--extracted", nargs="+", required=True)
    t.add_argument("--l2", type=float, default=20.0)
    t.add_argument("--seed", type=int, default=3)
    t.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data",
                                                 "models", "type_model.json"))
    args = ap.parse_args()
    train(args)


if __name__ == "__main__":
    main()
