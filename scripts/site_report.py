#!/usr/bin/env python3
"""Stage 4–5: score seed sites from their URLs and emit candidate post URLs.

Stdlib only. Two subcommands, so the work can be split across jobs by site:

  score   read cmd/sitecdx captures for one partition of sites (all crawls),
          write that partition's per-site rows and candidate URLs
  report  read every partition's per-site rows, write the Markdown report

Per site, over its unique English HTML content pages (boilerplate such as
/tag/, /page/2, /feed and login pages removed), we measure:

  hike_share   path has a hiking term (hike, trail, trek, summit, …)
  strict_share same, without the ambiguous "trail(s)"
  place_share  landform term (lake, falls, peak, canyon, …)
  geo_share    a US state / Canadian province or a national park name
  blog_share   post-shaped path (/2023/07/…, or a long hyphenated slug)
  shop_share   commerce path (/product/, /shop/, /cart, …)
  gear_share   gear-review slug (best-, -review, boots, tents, …)
  other_share  other activity (jeep, 4x4, atv, mountain-bike, golf, wine, …);
               a site whose name says offroad/4x4/jeep/golf/winery… is
               other_activity regardless

A page is English when Common Crawl's primary detected language is English
and its path has no non-English locale prefix (/de/, /fr-ca/, …). Non-English
pages are dropped before anything else is measured.

Verdicts (first-cut thresholds, see DESIGN.md Stage 4):
  hiking          keep every English content page
  mixed           keep only pages whose slug matches
  drifted         newest crawl is mostly new URLs, spans >= 3 sections of the
                  site, and is far less on-topic than older crawls (typically
                  an expired domain): keep matching pages from before
  shop, gear, other_activity, off_topic, thin, non_english   drop
"""
import argparse
import csv
import glob
import gzip
import random
import re
import sys
from collections import Counter, defaultdict
from urllib.parse import urlsplit

csv.field_size_limit(sys.maxsize)

VERDICTS = ("hiking", "mixed", "drifted", "shop", "gear", "other_activity", "off_topic", "thin", "non_english")
KEEP = ("hiking", "mixed", "drifted")

W = r"(?:^|[^a-z])({})(?=$|[^a-z])"
HIKE = re.compile(W.format(r"hik(?:e|es|ed|ing|er|ers)|trails?|trailheads?|trek(?:s|king)?|backpack(?:ing|er)?|"
                           r"summits?|scrambl(?:e|es|ing)|snowshoe(?:s|ing)?|thru|peakbag\w*|day-?hikes?|bushwhack\w*|"
                           r"switchbacks?|mountaineering"))
STRICT = re.compile(W.format(r"hik(?:e|es|ed|ing|er|ers)|trailheads?|trek(?:s|king)?|backpack(?:ing|er)?|"
                             r"summits?|scrambl(?:e|es|ing)|snowshoe(?:s|ing)?|thru|peakbag\w*|day-?hikes?|"
                             r"bushwhack\w*|switchbacks?|mountaineering"))
PLACE = re.compile(W.format(r"lakes?|falls|waterfalls?|peaks?|mount|mt|mountains?|canyons?|ridges?|pass|"
                            r"wilderness|creeks?|rivers?|buttes?|mesas?|glaciers?|lookouts?|loop|gorge|meadows?|"
                            r"national-park|state-park|provincial-park|basin|cirque|col|notch|dome|arch(?:es)?"))
OTHER = re.compile(W.format(r"jeeps?|4x4|4wd|off-?road(?:ing)?|atvs?|utvs?|ohv|overland(?:ing)?|mountain-?bik(?:e|es|ing)|"
                            r"mtb|dirt-?bik(?:e|es|ing)|snowmobil\w*|golf|wine|winery|wineries|bourbon|ale|beer|"
                            r"brewer(?:y|ies)|equestrian|horseback"))
BLOG = re.compile(r"/(?:19|20)\d\d/\d{1,2}/|/[a-z0-9]+(?:-[a-z0-9]+){3,}/?$")
GEAR = re.compile(W.format(r"best|reviews?|gear|vs|tested|buyers?-guide|deals?|jackets?|boots|shoes|tents?|packs?|"
                           r"sleeping-bags?|stoves?|socks"))
SHOP = re.compile(r"(?:^|/)(products?|shop|store|cart|collections|checkout|buy|basket|sku)(?:/|$|\?)")
BOILER = re.compile(r"/(tags?|category|categories|author|authors|feed|search|wp-json|wp-admin|wp-login|"
                    r"comment-page-\d+|attachment|login|log-in|signin|sign-in|signup|sign-up|register|account|"
                    r"my-account|users?|members?|profiles?|acl_users|privacy(?:-policy)?|terms|contact|about|cdn-cgi)(?:/|$|\?)|"
                    r"/(search|ucp|memberlist|posting|viewonline|faq|mcp|report|privmsg)\.php|"
                    r"/page/\d+/?$|[?&](replytocom|share|amp|utm_[a-z]+|came_from|redirect_to|return_?to)=|"
                    r"require_login|credentials_cookie_auth|"
                    r"\.(xml|json|rss|txt|css|js|jpe?g|png|gif|pdf)$", re.I)
SITE_OTHER = re.compile(r"offroad|4x4|4wd|jeep|overland|golf|winer(?:y|ies)|brewer(?:y|ies)|snowmobil")
LOCALE = re.compile(r"^/(?!en(?:[-_][a-z]{2})?/)[a-z]{2}(?:[-_][a-z]{2})?(?:/|$)")


def load_terms(path):
    terms = [l.strip().lower() for l in open(path) if l.strip() and not l.startswith("#")]
    alts = sorted((re.escape(t).replace(r"\ ", r"(?:[-_+]|%20)") for t in terms), key=len, reverse=True)
    return re.compile(W.format("|".join(alts)))


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
    q = "?" + p.query if p.query else ""
    return host + path + q, (path + q).lower()


# ---------------------------------------------------------------- score

def score(args):
    geo_r, geo_p = load_terms(args.regions), load_terms(args.parks)
    seeds = {}
    if args.seeds:
        with gzip.open(args.seeds, "rt") as f:
            for r in csv.DictReader(f, delimiter="\t"):
                seeds[r["domain"]] = (r["rank"], r["n_hubs"])

    # site -> key -> [path, timestamp, crawl, lang, url, warc_filename, offset, length, digest, crawls(set)]
    pages = defaultdict(dict)
    crawls = set()
    files = sorted({p for pat in args.captures for p in glob.glob(pat)})
    if not files:
        sys.exit("no capture files matched")
    for path in files:
        with gzip.open(path, "rt", newline="") as f:
            rd = csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
            hdr = next(rd)
            c = {k: i for i, k in enumerate(hdr)}
            for row in rd:
                if row[c["status"]] != "200" or row[c["mime"]] not in ("text/html", "application/xhtml+xml"):
                    continue
                site, crawl, ts = row[c["site"]], row[c["crawl"]], row[c["timestamp"]]
                crawls.add(crawl)
                key, path_l = page_key(row[c["url"]])
                if BOILER.search(path_l):
                    continue
                prev = pages[site].get(key)
                if prev is None:
                    pages[site][key] = [path_l, ts, crawl, row[c["languages"]].split(",")[0], row[c["url"]],
                                        row[c["warc_filename"]], row[c["warc_record_offset"]],
                                        row[c["warc_record_length"]], row[c["digest"]], {crawl}]
                else:
                    prev[9].add(crawl)
                    if ts > prev[1]:
                        prev[1:9] = [ts, crawl, row[c["languages"]].split(",")[0], row[c["url"]],
                                     row[c["warc_filename"]], row[c["warc_record_offset"]],
                                     row[c["warc_record_length"]], row[c["digest"]]]

    site_out = gzip.open(args.sites_out, "wt")
    cand_out = gzip.open(args.candidates_out, "wt")
    scols = ["site", "verdict", "seed_rank", "n_hubs", "pages", "eng_pages", "match_pages", "hike_share",
             "strict_share", "place_share", "geo_share", "blog_share", "shop_share", "gear_share", "other_share",
             "eng_share", "newest_crawl", "newest_match", "older_match", "newest_new", "crawls_seen", "examples"]
    ccols = ["url", "site", "site_verdict", "seed_rank", "crawl", "timestamp", "slug_match", "hike", "geo",
             "blog_path", "gear", "warc_filename", "warc_record_offset", "warc_record_length", "digest"]
    site_out.write("\t".join(scols) + "\n")
    cand_out.write("\t".join(ccols) + "\n")
    fmt = lambda v: f"{v:.3f}" if isinstance(v, float) else ("" if v is None else str(v))

    n_sites = n_cand = 0
    for site in sorted(pages):
        allp = list(pages[site].values())
        feats = []
        for p in allp:
            path = p[0]
            eng = p[3] == "eng" and not LOCALE.search(path)
            if not eng:
                continue
            h, st, pl = bool(HIKE.search(path)), bool(STRICT.search(path)), bool(PLACE.search(path))
            g = bool(geo_r.search(path) or geo_p.search(path))
            o = bool(OTHER.search(path))
            f = {"hike": h, "strict": st, "place": pl, "geo": g, "other": o,
                 "blog": bool(BLOG.search(path)), "shop": bool(SHOP.search(path)), "gear": bool(GEAR.search(path)),
                 "match": (h or (pl and g)) and not o}
            feats.append((p, f))
        n, n_eng = len(allp), len(feats)

        def share(key, items=feats):
            return sum(1 for _, f in items if f[key]) / len(items) if items else 0.0

        hike, strict, place, geo, blog, shop, gear, other, match = (
            share(k) for k in ("hike", "strict", "place", "geo", "blog", "shop", "gear", "other", "match"))
        match_pages = sum(1 for _, f in feats if f["match"])

        # Drift: compare the newest crawl with >= 20 English pages against all older crawls.
        by_crawl = defaultdict(list)
        for p, f in feats:
            for cr in p[9]:
                by_crawl[cr].append((p, f))
        dense = [cr for cr in sorted(by_crawl) if len(by_crawl[cr]) >= 20]
        newest = dense[-1] if dense else None
        older = [x for cr in dense[:-1] for x in by_crawl[cr]]
        newest_match = share("match", by_crawl[newest]) if newest else None
        older_match = share("match", older) if older else None
        newest_new = (sum(1 for p, _ in by_crawl[newest] if p[9] == {newest}) / len(by_crawl[newest])
                      if newest and older else None)
        newest_dirs = len({p[0].split("/")[1] if p[0].count("/") > 1 else "" for p, _ in by_crawl[newest]}) if newest else 0
        drifted = (older_match is not None and older_match >= 0.1 and newest_match < 0.25 * older_match
                   and newest_new >= 0.5 and newest_dirs >= 3)

        if n < 5:
            verdict = "thin"
        elif n_eng < 5:
            verdict = "non_english"
        elif drifted:
            verdict = "drifted"
        elif shop >= 0.25 and shop > hike:
            verdict = "shop"
        elif gear >= 0.45:
            verdict = "gear"
        elif (other >= 0.15 and other > strict) or SITE_OTHER.search(site.split(".")[0]):
            verdict = "other_activity"
        elif match_pages >= 5 and (hike >= 0.15 or hike + 0.5 * place >= 0.25 or match >= 0.2):
            verdict = "hiking"
        elif match_pages >= 3 and match >= 0.03:
            verdict = "mixed"
        else:
            verdict = "off_topic"

        rank, n_hubs = seeds.get(site, ("", ""))
        examples = [p[4] for p, f in feats if f["match"]][:3] or [p[4] for p, _ in feats][:3]
        row = {"site": site, "verdict": verdict, "seed_rank": rank, "n_hubs": n_hubs, "pages": n, "eng_pages": n_eng,
               "match_pages": match_pages, "hike_share": hike, "strict_share": strict, "place_share": place,
               "geo_share": geo, "blog_share": blog, "shop_share": shop, "gear_share": gear, "other_share": other,
               "eng_share": n_eng / n if n else 0.0, "newest_crawl": newest, "newest_match": newest_match,
               "older_match": older_match, "newest_new": newest_new,
               "crawls_seen": len({cr for p in allp for cr in p[9]}), "examples": " ".join(examples)}
        site_out.write("\t".join(fmt(row[k]) for k in scols) + "\n")
        n_sites += 1

        if verdict not in KEEP:
            continue
        for p, f in feats:
            if verdict == "hiking":
                if f["other"]:
                    continue
            elif not f["match"]:
                continue
            if verdict == "drifted" and newest in p[9] and len(p[9]) == 1:
                continue
            cand_out.write("\t".join(map(str, [p[4], site, verdict, rank, p[2], p[1], int(f["match"]), int(f["hike"]),
                                               int(f["geo"]), int(f["blog"]), int(f["gear"]), p[5], p[6], p[7], p[8]]))
                           + "\n")
            n_cand += 1
    site_out.close()
    cand_out.close()
    print(f"{n_sites} sites, {n_cand} candidate URLs, crawls: {', '.join(sorted(crawls))}", file=sys.stderr)


# ---------------------------------------------------------------- report

def report(args):
    rows = []
    for pat in args.sites:
        for path in sorted(glob.glob(pat)):
            with gzip.open(path, "rt", newline="") as f:
                rows.extend(csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE))
    for r in rows:
        for k in ("pages", "eng_pages", "match_pages"):
            r[k] = int(r[k])
        for k in ("hike_share", "strict_share", "place_share", "geo_share", "blog_share", "shop_share", "gear_share",
                  "other_share", "eng_share", "newest_match", "older_match", "newest_new"):
            r[k] = float(r[k]) if r[k] != "" else None
    rows.sort(key=lambda r: (int(r["seed_rank"]) if r["seed_rank"] else 10**9, r["site"]))
    by_site = {r["site"]: r for r in rows}
    looked_up = set()
    if args.site_list:
        looked_up = {l.strip() for l in open(args.site_list) if l.strip()}
    known, negs = read_csv(args.known), read_csv(args.negatives)
    n_cand = None
    if args.candidates:
        n_cand = Counter()
        for pat in args.candidates:
            for path in sorted(glob.glob(pat)):
                with gzip.open(path, "rt") as f:
                    next(f)
                    for line in f:
                        parts = line.split("\t", 7)
                        n_cand["all"] += 1
                        n_cand["match"] += parts[6] == "1"
                        n_cand[("site", parts[1])] = 1

    L = []
    w = L.append
    pct = lambda v: "–" if v is None else f"{100 * v:.0f}%"
    w(f"# Stage 4–5 report: {args.title}\n")
    w("Generated by `scripts/site_report.py`. Thresholds are a first cut; see DESIGN.md Stage 4.\n")
    if args.note:
        w(args.note + "\n")
    vc = Counter(r["verdict"] for r in rows)
    meaning = {"hiking": "keep every English content page", "mixed": "keep slug-matching pages only",
               "drifted": "newest crawl is new, off-topic URLs (expired domain?); keep older matching pages",
               "shop": "≥ 25% commerce paths, more than hiking paths",
               "gear": "≥ 45% gear-review paths (best-, -review, boots, tents…)",
               "other_activity": "≥ 15% jeep / ATV / mountain-bike / golf / wine… paths",
               "off_topic": "few hiking slugs", "thin": "< 5 content pages captured",
               "non_english": "< 5 English content pages"}
    w("## Verdicts\n")
    w("| verdict | sites | meaning |\n|---|---:|---|")
    for v in VERDICTS:
        w(f"| {v} | {vc.get(v, 0):,} | {meaning[v]} |")
    w(f"| **scored** | {len(rows):,} | |")
    if looked_up:
        w(f"| no captures | {len(looked_up - set(by_site)):,} | looked up, nothing captured |")
    w("")
    if n_cand is not None:
        sites_c = sum(1 for k in n_cand if isinstance(k, tuple))
        w(f"**Candidate URLs:** {n_cand['all']:,} from {sites_c:,} sites ({n_cand['match']:,} with a hiking/place slug).\n")

    w("## Verdict by seed rank\n")
    w("| seed rank | sites | " + " | ".join(VERDICTS) + " |\n|---|---:|" + "---:|" * len(VERDICTS))
    edges = [1, 501, 1001, 2001, 3001, 5001, 10001, 20001, 40001, 10**9]
    for lo, hi in zip(edges, edges[1:]):
        rs = [r for r in rows if r["seed_rank"] and lo <= int(r["seed_rank"]) < hi]
        if rs:
            c = Counter(r["verdict"] for r in rs)
            w(f"| {lo:,}–{hi - 1:,} | {len(rs):,} | " + " | ".join(f"{c.get(v, 0):,}" for v in VERDICTS) + " |")
    w("")

    def table(items, title, note):
        w(f"## {title}\n")
        w(note + "\n")
        w("| site | verdict | seed rank | pages | eng | hike | place | geo | shop | gear | other | newest vs older (new) |\n"
          "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for it in items:
            r = by_site.get(it["domain"])
            if not r:
                w(f"| {it['domain']} | no captures | | | | | | | | | | |")
                continue
            w(f"| {r['site']} | **{r['verdict']}** | {r['seed_rank'] or '–'} | {r['pages']:,} | {pct(r['eng_share'])} | "
              f"{pct(r['hike_share'])} | {pct(r['place_share'])} | {pct(r['geo_share'])} | {pct(r['shop_share'])} | "
              f"{pct(r['gear_share'])} | {pct(r['other_share'])} | "
              f"{pct(r['newest_match'])} vs {pct(r['older_match'])} ({pct(r['newest_new'])}) |")
        w("")

    table(known, "Known blogs", "Should all be `hiking` (or `mixed` for travel blogs and magazines).")
    table(negs, "Negatives", "Should not be `hiking`.")

    rnd = random.Random(7)
    w("## Random samples for eyeballing\n")
    w("Up to three example URLs per site (slug-matching ones first).\n")
    for v in VERDICTS[:7]:
        rs = sorted((r for r in rows if r["verdict"] == v), key=lambda r: r["site"])
        if not rs:
            continue
        w(f"### {v} ({len(rs):,} sites; showing up to 25)\n")
        for r in sorted(rnd.sample(rs, min(25, len(rs))), key=lambda r: int(r["seed_rank"] or 10**9)):
            ex = " · ".join(e.split("://", 1)[-1][:90] for e in r["examples"].split())
            w(f"- **{r['site']}** (rank {r['seed_rank'] or '–'}, {r['eng_pages']:,} English pages, hike {pct(r['hike_share'])}, "
              f"other {pct(r['other_share'])}): {ex}")
        w("")
    with open(args.report_out, "w") as f:
        f.write("\n".join(L))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score", help="score one partition of sites")
    s.add_argument("--captures", nargs="+", required=True, help="sitecdx outputs (globs allowed)")
    s.add_argument("--seeds", help="seeds.tsv.gz from seed_report.py (for seed rank / hubs columns)")
    s.add_argument("--regions", default="data/gazetteer/regions.txt")
    s.add_argument("--parks", default="data/gazetteer/national_parks.txt")
    s.add_argument("--sites-out", required=True)
    s.add_argument("--candidates-out", required=True)
    r = sub.add_parser("report", help="merge partitions' site rows into a report")
    r.add_argument("--sites", nargs="+", required=True, help="sites part files (globs allowed)")
    r.add_argument("--candidates", nargs="*", help="candidate part files, for counts (globs allowed)")
    r.add_argument("--site-list", help="the list of sites that was looked up")
    r.add_argument("--known", default="data/eval/known_blogs.csv")
    r.add_argument("--negatives", default="data/eval/negatives.csv")
    r.add_argument("--title", default="seed sites scored from their URLs")
    r.add_argument("--note", default="")
    r.add_argument("--report-out", required=True)
    args = ap.parse_args()
    score(args) if args.cmd == "score" else report(args)


if __name__ == "__main__":
    main()
