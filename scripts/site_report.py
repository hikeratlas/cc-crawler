#!/usr/bin/env python3
"""Stage 4–5 (first cut): score seed sites from their URLs, and emit the
candidate post URL list. Stdlib only.

Inputs are cmd/sitecdx outputs (one per crawl), the ranked seeds from
scripts/seed_report.py, the eval lists and the gazetteers.

Per site, over its unique English-or-not HTML pages (status 200, not
boilerplate like /tag/ or /page/2), we measure:
  hike_share   path has a hiking term (hike, trail, trek, summit, …)
  place_share  path has a landform term (lake, falls, peak, canyon, …)
  geo_share    path names a US state / Canadian province or a national park
  blog_share   path looks like a post (/2023/07/…, or a long hyphenated slug)
  shop_share   path looks like commerce (/product/, /shop/, /cart, …)
  gear_share   path looks like a gear review (best-, -review, boots, tents, …)
  eng_share    Common Crawl's detected primary language is English
and compare the newest crawl with the older ones to catch sites whose
content changed (typically expired domains).

Verdicts (first-cut thresholds, see DESIGN.md Stage 4):
  hiking    keep every content page
  mixed     keep only pages whose slug matches
  shop, gear, off_topic, thin, non_english, drifted   drop (drifted keeps
            slug-matching pages from crawls before the change)

Usage:
  scripts/site_report.py --captures out/captures-*.tsv.gz --seeds seeds.tsv.gz \
      --sites-out sites.tsv.gz --candidates-out candidates.tsv.gz --report-out report.md
"""
import argparse
import csv
import glob
import gzip
import random
import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit

HTML = {"text/html", "application/xhtml+xml"}
VERDICTS = ("hiking", "mixed", "drifted", "shop", "gear", "off_topic", "thin", "non_english")

HIKE = re.compile(r"(?:^|[^a-z])(hik(?:e|es|ed|ing|er|ers)|trails?|trailheads?|trek(?:s|king)?|backpack(?:ing|er)?|"
                  r"summits?|scrambl(?:e|es|ing)|snowshoe(?:s|ing)?|thru|peakbag\w*|day-?hikes?|bushwhack\w*|"
                  r"switchbacks?|mountaineering)(?=$|[^a-z])")
PLACE = re.compile(r"(?:^|[^a-z])(lakes?|falls|waterfalls?|peaks?|mount|mt|mountains?|canyons?|ridges?|pass|"
                   r"wilderness|creeks?|rivers?|buttes?|mesas?|glaciers?|lookouts?|loop|gorge|meadows?|"
                   r"national-park|state-park|provincial-park|basin|cirque|col|notch|dome|arch(?:es)?)(?=$|[^a-z])")
BLOG = re.compile(r"/(?:19|20)\d\d/\d{1,2}/|/[a-z0-9]+(?:-[a-z0-9]+){3,}/?$")
GEAR = re.compile(r"(?:^|[^a-z])(best|reviews?|gear|vs|tested|buyers?-guide|deals?|jackets?|boots|shoes|tents?|packs?|"
                  r"sleeping-bags?|stoves?|socks)(?=$|[^a-z])")
SHOP = re.compile(r"(?:^|/)(products?|shop|store|cart|collections|checkout|buy|basket|sku)(?:/|$|\?)")
BOILER = re.compile(r"/(tags?|category|categories|author|authors|feed|search|wp-json|wp-admin|wp-login|"
                    r"comment-page-\d+|attachment|login|account|my-account|privacy(?:-policy)?|terms|"
                    r"contact|about|cdn-cgi)(?:/|$)|/page/\d+/?$|[?&](replytocom|share|amp|utm_[a-z]+)=|"
                    r"\.(xml|json|rss|txt|css|js|jpe?g|png|gif|pdf)$", re.I)


def load_terms(path):
    terms = []
    for line in open(path):
        line = line.strip().lower()
        if line and not line.startswith("#"):
            terms.append(line)
    # word sequence separated by -, _, + or %20 in a path
    alts = sorted((re.escape(t).replace(r"\ ", r"(?:[-_+]|%20)") for t in terms), key=len, reverse=True)
    return re.compile(r"(?:^|[^a-z])(" + "|".join(alts) + r")(?=$|[^a-z])")


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def page_key(url):
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return host + path + ("?" + p.query if p.query else ""), (path + ("?" + p.query if p.query else "")).lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--captures", nargs="+", required=True, help="sitecdx outputs (globs allowed)")
    ap.add_argument("--seeds", required=True, help="seeds.tsv.gz from seed_report.py")
    ap.add_argument("--known", default="data/eval/known_blogs.csv")
    ap.add_argument("--negatives", default="data/eval/negatives.csv")
    ap.add_argument("--regions", default="data/gazetteer/regions.txt")
    ap.add_argument("--parks", default="data/gazetteer/national_parks.txt")
    ap.add_argument("--sites-out", required=True)
    ap.add_argument("--candidates-out", required=True)
    ap.add_argument("--report-out", required=True)
    args = ap.parse_args()

    GEO_R, GEO_P = load_terms(args.regions), load_terms(args.parks)

    seeds = {}
    with gzip.open(args.seeds, "rt") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            seeds[r["domain"]] = r

    # site -> page key -> latest capture
    pages = defaultdict(dict)
    crawls = set()
    status_mix = defaultdict(Counter)
    files = sorted({p for pat in args.captures for p in glob.glob(pat)})
    for path in files:
        with gzip.open(path, "rt", newline="") as f:
            for r in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
                crawls.add(r["crawl"])
                status_mix[r["site"]][r["status"]] += 1
                if r["status"] != "200" or r["mime"] not in HTML:
                    continue
                key, path_l = page_key(r["url"])
                prev = pages[r["site"]].get(key)
                if prev is None or r["timestamp"] > prev["timestamp"]:
                    r["path"] = path_l
                    r["crawls"] = (prev["crawls"] if prev else set()) | {r["crawl"]}
                    pages[r["site"]][key] = r
                else:
                    prev["crawls"].add(r["crawl"])
    crawls = sorted(crawls)
    newest_crawl = crawls[-1] if crawls else None

    def classify_page(p):
        path = p["path"]
        h = bool(HIKE.search(path))
        pl = bool(PLACE.search(path))
        g = bool(GEO_R.search(path) or GEO_P.search(path))
        return {
            "hike": h, "place": pl, "geo": g,
            "blog": bool(BLOG.search(path)),
            "shop": bool(SHOP.search(path)),
            "gear": bool(GEAR.search(path)),
            "eng": p["languages"].split(",")[0] == "eng",
            "match": h or (pl and g),
        }

    site_rows = []
    candidates = []
    all_sites = set(pages) | set(status_mix)
    for site in all_sites:
        content = [p for p in pages.get(site, {}).values() if not BOILER.search(p["path"])]
        for p in content:
            p["f"] = classify_page(p)
        n = len(content)

        def share(key, items=content):
            return sum(1 for p in items if p["f"][key]) / len(items) if items else 0.0

        hike, place, geo, blog, shop, eng, gear = (share(k) for k in ("hike", "place", "geo", "blog", "shop", "eng", "gear"))
        hike_pages = sum(1 for p in content if p["f"]["match"])
        # newest crawl in which this site has >= 20 content pages vs. everything older
        by_crawl = defaultdict(list)
        for p in content:
            for c in p["crawls"]:
                by_crawl[c].append(p)
        dense = [c for c in sorted(by_crawl) if len(by_crawl[c]) >= 20]
        newest = dense[-1] if dense else None
        older = [p for c in dense[:-1] for p in by_crawl[c]]
        newest_match = share("match", by_crawl[newest]) if newest else None
        older_match = share("match", older) if older else None
        drifted = (newest is not None and older_match is not None and older_match >= 0.1
                   and newest_match < 0.25 * older_match)

        if n < 5:
            verdict = "thin"
        elif eng < 0.5:
            verdict = "non_english"
        elif drifted:
            verdict = "drifted"
        elif shop >= 0.25 and shop > hike:
            verdict = "shop"
        elif gear >= 0.45:
            verdict = "gear"
        elif hike_pages >= 5 and (hike >= 0.15 or hike + 0.5 * place >= 0.25 or share("match") >= 0.2):
            verdict = "hiking"
        elif hike_pages >= 3 and share("match") >= 0.03:
            verdict = "mixed"
        else:
            verdict = "off_topic"

        s = seeds.get(site, {})
        row = {
            "site": site, "verdict": verdict, "seed_rank": s.get("rank", ""), "n_hubs": s.get("n_hubs", ""),
            "pages": n, "match_pages": hike_pages, "hike_share": hike, "place_share": place, "geo_share": geo,
            "blog_share": blog, "shop_share": shop, "gear_share": gear, "eng_share": eng,
            "newest_crawl": newest or "", "newest_match": newest_match, "older_match": older_match,
            "captures": sum(status_mix[site].values()),
            "ok_share": status_mix[site]["200"] / max(1, sum(status_mix[site].values())),
            "examples": [p["url"] for p in content if p["f"]["match"]][:3] or [p["url"] for p in content][:3],
        }
        site_rows.append(row)

        keep_all = verdict == "hiking"
        for p in content:
            f = p["f"]
            if not (keep_all and f["eng"]) and not (verdict in ("mixed", "drifted") and f["match"] and f["eng"]):
                continue
            if verdict == "drifted" and newest in p["crawls"] and len(p["crawls"]) == 1:
                continue
            candidates.append({
                "url": p["url"], "site": site, "site_verdict": verdict, "seed_rank": row["seed_rank"],
                "crawl": p["crawl"], "timestamp": p["timestamp"], "slug_match": int(f["match"]),
                "hike": int(f["hike"]), "geo": int(f["geo"]), "blog_path": int(f["blog"]), "gear": int(f["gear"]),
                "warc_filename": p["warc_filename"], "warc_record_offset": p["warc_record_offset"],
                "warc_record_length": p["warc_record_length"], "digest": p["digest"],
            })

    site_rows.sort(key=lambda r: int(r["seed_rank"]) if r["seed_rank"] else 10**9)
    fmt = lambda v: f"{v:.3f}" if isinstance(v, float) else ("" if v is None else str(v))
    cols = ["site", "verdict", "seed_rank", "n_hubs", "pages", "match_pages", "hike_share", "place_share", "geo_share",
            "blog_share", "shop_share", "gear_share", "eng_share", "newest_crawl", "newest_match", "older_match", "captures", "ok_share"]
    with gzip.open(args.sites_out, "wt") as f:
        f.write("\t".join(cols + ["examples"]) + "\n")
        for r in site_rows:
            f.write("\t".join(fmt(r[c]) for c in cols) + "\t" + " ".join(r["examples"]) + "\n")
    ccols = ["url", "site", "site_verdict", "seed_rank", "crawl", "timestamp", "slug_match", "hike", "geo", "blog_path", "gear",
             "warc_filename", "warc_record_offset", "warc_record_length", "digest"]
    with gzip.open(args.candidates_out, "wt") as f:
        f.write("\t".join(ccols) + "\n")
        for c in candidates:
            f.write("\t".join(str(c[k]) for k in ccols) + "\n")

    # ---------------- report ----------------
    by_site = {r["site"]: r for r in site_rows}
    known = read_csv(args.known)
    negs = read_csv(args.negatives)
    L = []
    w = L.append
    pct = lambda v: "–" if v is None else f"{100 * v:.0f}%"
    w("# Stage 4–5 report: seed sites scored from their URLs\n")
    w("Generated by `scripts/site_report.py` from `cmd/sitecdx` captures. Thresholds are a first cut; see DESIGN.md Stage 4.\n")
    w("Crawls: " + ", ".join(f"`{c}`" for c in crawls) + "\n")
    vc = Counter(r["verdict"] for r in site_rows)
    w("## Verdicts\n")
    w("| verdict | sites | meaning |\n|---|---:|---|")
    meaning = {"hiking": "keep every content page", "mixed": "keep slug-matching pages only",
               "drifted": "content changed in the newest crawl (expired domain?); keep older matching pages",
               "shop": "≥ 25% commerce paths, more than hiking paths", "gear": "≥ 45% gear-review paths (best-, -review, boots, tents…)", "off_topic": "few hiking slugs", "thin": "< 5 content pages captured",
               "non_english": "< 50% English pages"}
    for v in VERDICTS:
        w(f"| {v} | {vc.get(v, 0):,} | {meaning[v]} |")
    w(f"| **total** | {len(site_rows):,} | |")
    w("")
    nc = len(candidates)
    w(f"**Candidate URLs:** {nc:,} from {len({c['site'] for c in candidates}):,} sites "
      f"({sum(c['slug_match'] for c in candidates):,} with a hiking/place slug).\n")

    w("## Verdict by seed rank\n")
    w("| seed rank | sites | " + " | ".join(VERDICTS) + " |\n|---|---:|" + "---:|" * len(VERDICTS))
    buckets = [(1, 500), (501, 1000), (1001, 2000), (2001, 3000), (3001, 5000), (5001, 10**9)]
    for lo, hi in buckets:
        rs = [r for r in site_rows if r["seed_rank"] and lo <= int(r["seed_rank"]) <= hi]
        if not rs:
            continue
        c = Counter(r["verdict"] for r in rs)
        w(f"| {lo:,}–{hi:,} | {len(rs):,} | " + " | ".join(f"{c.get(v, 0):,}" for v in
          VERDICTS) + " |")
    w("")

    def table(items, title, note):
        w(f"## {title}\n")
        w(note + "\n")
        w("| site | verdict | seed rank | pages | hike | place | geo | blog | shop | gear | eng | newest vs older match |\n"
          "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for it in items:
            r = by_site.get(it["domain"])
            if not r:
                w(f"| {it['domain']} | no captures | | | | | | | | | | |")
                continue
            w(f"| {r['site']} | **{r['verdict']}** | {r['seed_rank'] or '–'} | {r['pages']:,} | {pct(r['hike_share'])} | "
              f"{pct(r['place_share'])} | {pct(r['geo_share'])} | {pct(r['blog_share'])} | {pct(r['shop_share'])} | {pct(r['gear_share'])} | "
              f"{pct(r['eng_share'])} | {pct(r['newest_match'])} vs {pct(r['older_match'])} |")
        w("")

    table(known, "Known blogs", "Should all be `hiking` (or `mixed` for travel blogs and magazines).")
    table(negs, "Negatives", "Should not be `hiking`.")

    rnd = random.Random(7)
    w("## Random samples for eyeballing\n")
    w("Three example URLs per site (slug-matching ones first).\n")
    for v in ("hiking", "mixed", "drifted", "shop", "gear", "off_topic"):
        rs = sorted((r for r in site_rows if r["verdict"] == v), key=lambda r: r["site"])
        if not rs:
            continue
        w(f"### {v} ({len(rs):,} sites; showing up to 25)\n")
        for r in sorted(rnd.sample(rs, min(25, len(rs))), key=lambda r: int(r["seed_rank"] or 10**9)):
            ex = " · ".join(e.split("://", 1)[-1][:90] for e in r["examples"])
            w(f"- **{r['site']}** (rank {r['seed_rank'] or '–'}, {r['pages']:,} pages, hike {pct(r['hike_share'])}, "
              f"shop {pct(r['shop_share'])}): {ex}")
        w("")

    with open(args.report_out, "w") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    main()
