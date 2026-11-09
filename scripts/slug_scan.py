#!/usr/bin/env python3
"""Stage 6b: find hiking sites by the hike names in their URLs.

Scans Common Crawl's columnar (Parquet) URL index. For every English HTML page
fetched with status 200, it splits the URL path into words and looks for the
names of real trails, peaks, lakes, waterfalls and parks from the Hiker Atlas
autosuggest index ("panorama-ridge", "la-cloche-silhouette-trail"). Hits are
tallied per site (registered domain, or host under blog platforms such as
blogspot.com): a site whose URLs name many different hikes is very likely a
hiking site, whoever links to it.

Reads only the needed columns, with HTTP range requests and the same polite
backoff as the Go tools. Needs pyarrow (the only non-stdlib script).

  scan     --paths cc-index-table.paths.gz --part 3 --parts 16 --index autosuggest.db --out hits-3.tsv.gz
  merge    --in 'hits-*.tsv.gz' --known sites.tsv.gz --out-dir out/
"""
import argparse
import csv
import glob
import gzip
import io
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from urlrules import drop_reason  # noqa: E402

BASE = "https://data.commoncrawl.org/"
HERE = os.path.dirname(os.path.abspath(__file__))
CUE = re.compile(r"(?:^|[^a-z])(hik(?:e|es|ed|ing)|trail|trails|backpack(?:ing)?|trek(?:king)?|summit|scrambl\w*|"
                 r"thru|trip-?report|trailhead|loop|peak|falls)(?:[^a-z]|$)")
SPLIT = re.compile(r"[^a-z0-9]+")
STREET = {"avenue", "ave", "street", "st", "drive", "dr", "road", "rd", "lane", "ln", "court", "ct", "blvd",
          "boulevard", "circle", "cir", "place", "pl", "terrace", "parkway", "pkwy", "highway", "hwy"}
EXT = re.compile(r"\.(?:jpe?g|png|gif|webp|pdf|gpx|kml|zip|mp4|css|js)$")


# ---------------------------------------------------------------- HTTP file

class RangeFile(io.RawIOBase):
    """A read-only, seekable file over HTTP range requests, for pyarrow.

    Retries refusals (403/429/5xx, which Common Crawl's CDN uses for "slow
    down") with exponential backoff, and paces requests.
    """
    min_interval = 0.25

    def __init__(self, url):
        self.url, self.pos, self.size, self._last = url, 0, None, 0.0
        self.bytes_read = 0
        req = urllib.request.Request(url, method="HEAD")
        with self._open(req) as r:
            self.size = int(r.headers["Content-Length"])

    def _open(self, req, attempts=12):
        delay = 5.0
        for i in range(attempts):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                return urllib.request.urlopen(req, timeout=120)
            except urllib.error.HTTPError as e:
                if e.code not in (403, 429, 500, 502, 503, 504) or i == attempts - 1:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if i == attempts - 1:
                    raise
            time.sleep(delay * (0.5 + random.random()))
            delay = min(delay * 2, 300)

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos

    def readinto(self, b):
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        req = urllib.request.Request(self.url, headers={"Range": f"bytes={self.pos}-{self.pos + n - 1}"})
        with self._open(req) as r:
            data = r.read()
        b[:len(data)] = data
        self.pos += len(data)
        self.bytes_read += len(data)
        return len(data)


# ---------------------------------------------------------------- names

# A name counts only if it has a landform or trail word, or is well known:
# OSM has trails called "Happy Birthday", "Peanut Butter" and "Good Luck".
FEATURE_WORDS = set("""
lake lakes mount mountain mountains mt peak peaks trail trails falls fall creek river ridge pass canyon
rock rocks butte mesa dome hill hills knob bald point gap notch glacier basin meadow meadows valley loop
spring springs park forest wilderness island cove summit lookout overlook arch arches gorge hollow cliff
cliffs bluff landing crest saddle cirque tarn pond tower head bluffs narrows slot wash run fork brook
""".split())


def load_names(path):
    """Hike names as word tuples, from the autosuggest index (extract.py's filters)."""
    import extract
    names, parks, long = extract.load_index(path)
    out = {}
    for key, fs in names.items():
        toks = tuple(t for t in SPLIT.split(key.replace("'", "")) if t)
        q = max(f["qrank"] for f in fs)
        if 2 <= len(toks) <= 6 and toks[-1] not in STREET and (FEATURE_WORDS.intersection(toks) or q >= 100):
            out[toks] = max(q, out.get(toks, 0))
            # URLs drop the generic last word: "tonquin-valley", "la-cloche-silhouette"
            if toks[-1] in ("trail", "loop", "hike") and len(toks) >= 3:
                out[toks[:-1]] = max(q, out.get(toks[:-1], 0))
    return out


def load_platforms():
    with open(os.path.join(HERE, "..", "data", "platforms.csv"), newline="") as f:
        return {r["parent"] for r in csv.DictReader(f) if r["site_rule"] == "host"}


def site_of(host, domain, platforms):
    return host if domain in platforms else domain


# ---------------------------------------------------------------- scan

def scan(args):
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    with gzip.open(args.paths, "rt") as f:
        files = [ln.strip() for ln in f if "/subset=warc/" in ln]
    mine = [p for i, p in enumerate(sorted(files)) if i % args.parts == args.part]
    if args.limit:
        mine = mine[:args.limit]
    names = load_names(args.index)
    maxlen = max(len(k) for k in names)
    platforms = load_platforms()
    print(f"{len(names):,} names; {len(mine)} of {len(files)} index files", file=sys.stderr)

    cols = ["url_host_name", "url_host_registered_domain", "url_path", "fetch_status",
            "content_mime_detected", "content_languages"]
    pages = Counter()  # every site's English HTML pages
    sites = defaultdict(lambda: {"hits": 0, "cue_hits": 0, "names": Counter(), "cue_names": set(),
                                 "examples": []})  # sites with hits
    total = kept = 0
    for fi, path in enumerate(mine):
        t0 = time.time()
        rf = RangeFile(BASE + path)
        pf = pq.ParquetFile(io.BufferedReader(rf, buffer_size=1 << 20))
        # Crawls before CC-MAIN-2018-39 have no content_languages (and older
        # ones no detected MIME type): filter on what the file has.
        have = set(pf.schema_arrow.names)
        use = [c for c in cols if c in have]
        for rg in range(pf.num_row_groups):
            t = pf.read_row_group(rg, columns=use)
            total += t.num_rows
            ok = pc.equal(t["fetch_status"], 200)
            if "content_mime_detected" in have:
                ok = pc.and_(ok, pc.equal(t["content_mime_detected"], "text/html"))
            if "content_languages" in have:
                ok = pc.and_(ok, pc.starts_with(pc.fill_null(t["content_languages"], ""), "eng"))
            t = t.filter(ok)
            kept += t.num_rows
            hosts = t["url_host_name"].to_pylist()
            doms = t["url_host_registered_domain"].to_pylist()
            pths = t["url_path"].to_pylist()
            for host, dom, p in zip(hosts, doms, pths):
                if not dom or not p:
                    continue
                site = site_of(host, dom, platforms)
                pages[site] += 1
                lp = p.lower()
                if EXT.search(lp) or drop_reason("http://" + host + p, site):
                    continue
                toks = [x for x in SPLIT.split(lp) if x]
                found = set()
                n = len(toks)
                for i in range(n):
                    for L in range(min(maxlen, n - i), 1, -1):
                        k = tuple(toks[i:i + L])
                        if k in names:
                            if i + L < n and toks[i + L] in STREET:
                                break  # "123-park-drive", "ridge-avenue"
                            found.add(k)
                            break
                if not found:
                    continue
                s = sites[site]
                s["hits"] += 1
                cue = bool(CUE.search(lp))
                s["cue_hits"] += cue
                for k in found:
                    s["names"][" ".join(k)] += 1
                    if cue:
                        s["cue_names"].add(" ".join(k))
                if cue and len(s["examples"]) < 5:
                    s["examples"].append(f"{host}{p}")
        print(f"[{fi + 1}/{len(mine)}] {path.rsplit('/', 1)[-1][:16]} {rf.bytes_read / 1e6:.0f} MB "
              f"{time.time() - t0:.0f}s; rows {total:,}, english html {kept:,}, sites with hits {len(sites):,}",
              file=sys.stderr, flush=True)

    with gzip.open(args.out, "wt") as f:
        f.write("site\tpages\thits\tcue_hits\tnames\texamples\n")
        for site, s in sites.items():
            nm = ";".join(f"{'*' if k in s['cue_names'] else ''}{k}={v}" for k, v in s["names"].most_common(300))
            f.write(f"{site}\t{pages[site]}\t{s['hits']}\t{s['cue_hits']}\t{nm}\t{' '.join(s['examples'])}\n")
    print(f"done: {total:,} rows, {kept:,} English HTML, {len(sites):,} sites with hits", file=sys.stderr)


# ---------------------------------------------------------------- merge

def merge(args):
    csv.field_size_limit(1 << 30)
    agg = defaultdict(lambda: {"pages": 0, "hits": 0, "cue_hits": 0, "names": Counter(), "cue_names": set(),
                               "examples": []})
    for path in sorted({p for pat in args.inputs for p in glob.glob(pat)}):
        with gzip.open(path, "rt") as f:
            for r in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
                a = agg[r["site"]]
                for k in ("pages", "hits", "cue_hits"):
                    a[k] += int(r[k])
                for kv in r["names"].split(";"):
                    if kv:
                        k, v = kv.rsplit("=", 1)
                        if k.startswith("*"):
                            k = k[1:]
                            a["cue_names"].add(k)
                        a["names"][k] += int(v)
                a["examples"] += [e for e in r["examples"].split(" ") if e][:5 - len(a["examples"])]
    known = set()
    if args.known:
        with gzip.open(args.known, "rt") as f:
            known = {r["site"] for r in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)}
    # Rarity: a name used by many sites ("lake tahoe", "covered bridge") says
    # less about a site than one used by few ("oneonta trail").
    df = Counter(k for a in agg.values() for k in a["names"])
    n_sites = max(1, len(agg))
    idf = {k: math.log(n_sites / c) for k, c in df.items()}
    rows = []
    for site, a in agg.items():
        distinct = len(a["names"])
        # Rarity-weighted distinct names: names in URLs with a hiking word in
        # full, the rest at a tenth. Scaled down for big general sites whose
        # hiking URLs are a sliver of the whole (lyft.com, macys.com).
        base = sum(idf[k] * (1.0 if k in a["cue_names"] else 0.1) for k in a["names"])
        share = a["cue_hits"] / a["pages"] if a["pages"] else 0
        score = base * min(1.0, (share / 0.02) ** 0.5)
        rows.append({"site": site, "score": round(score, 1), "distinct_names": distinct,
                     "cue_names": len(a["cue_names"]), "hits": a["hits"], "cue_hits": a["cue_hits"],
                     "pages": a["pages"], "hit_share": a["hits"] / a["pages"] if a["pages"] else 0,
                     "known": site in known, "top_names": ", ".join(k for k, _ in a["names"].most_common(8)),
                     "examples": " ".join(a["examples"])})
    rows.sort(key=lambda r: -r["score"])
    os.makedirs(args.out_dir, exist_ok=True)
    cols = ["site", "score", "distinct_names", "cue_names", "hits", "cue_hits", "pages", "hit_share", "known", "top_names", "examples"]
    with gzip.open(os.path.join(args.out_dir, "slug_sites.tsv.gz"), "wt") as f:
        f.write("\t".join(cols) + "\n")
        for r in rows:
            f.write("\t".join(f"{r[c]:.3f}" if c == "hit_share" else str(r[c]) for c in cols) + "\n")
    L = ["# Stage 6b report: sites found by hike names in URLs\n"]
    L.append(f"{len(rows):,} sites with at least one URL naming a hike; "
             f"{sum(r['known'] for r in rows):,} of them are already in the page pass.\n")
    L.append("| distinct hike names in URLs | sites | already known | new |\n|---|---:|---:|---:|")
    for lo, hi in ((1, 1), (2, 2), (3, 4), (5, 9), (10, 19), (20, 49), (50, 10 ** 9)):
        g = [r for r in rows if lo <= r["distinct_names"] <= hi]
        k = sum(r["known"] for r in g)
        L.append(f"| {lo}{'' if lo == hi else ('+' if hi > 10 ** 8 else f'–{hi}')} | {len(g):,} | {k:,} | {len(g) - k:,} |")
    if known:
        L.append(f"\nKnown sites rediscovered with ≥ 5 distinct names: "
                 f"{sum(1 for r in rows if r['known'] and r['distinct_names'] >= 5):,} of {len(known):,}.\n")
    rnd = random.Random(3)
    for title, pick in (("Top new sites", [r for r in rows if not r["known"]][:40]),
                        ("Random new sites with 5–19 names",
                         rnd.sample(g := [r for r in rows if not r["known"] and 5 <= r["distinct_names"] < 20],
                                    min(40, len(g))))):
        L.append(f"\n## {title}\n")
        L.append("| site | names | cue hits | pages | top names | example |\n|---|---:|---:|---:|---|---|")
        for r in pick:
            ex = r["examples"].split(" ")[0] if r["examples"] else ""
            L.append(f"| {r['site']} | {r['distinct_names']} | {r['cue_hits']} | {r['pages']:,} | "
                     f"{r['top_names'][:70]} | {ex[:80]} |")
    if args.watch:
        L.append("\n## Watch list\n")
        L.append("Sites whose URLs name each watched hike (★ = already known).\n")
        for w in args.watch:
            hit = [r for r in rows if w in agg[r["site"]]["names"]]
            L.append(f"- **{w}** ({len(hit)} sites): " + ", ".join(
                f"{'★' if r['known'] else ''}{r['site']}" for r in hit[:25]))
    with open(os.path.join(args.out_dir, "slug_report.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print(f"{len(rows):,} sites")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--paths", required=True, help="cc-index-table.paths.gz for one crawl")
    s.add_argument("--part", type=int, default=0)
    s.add_argument("--parts", type=int, default=1)
    s.add_argument("--limit", type=int, default=0, help="only the first N of this part's files (testing)")
    s.add_argument("--index", required=True, help="Hiker Atlas autosuggest.db")
    s.add_argument("--out", required=True)
    m = sub.add_parser("merge")
    m.add_argument("--in", dest="inputs", nargs="+", required=True)
    m.add_argument("--known", help="sites.tsv.gz from the page pass")
    m.add_argument("--out-dir", required=True)
    m.add_argument("--watch", nargs="*", default=["panorama ridge", "tonquin valley", "la cloche silhouette",
                                                  "green gardens", "half dome", "angels landing"],
                   help="hike names to list sites for in the report")
    args = ap.parse_args()
    scan(args) if args.cmd == "scan" else merge(args)


if __name__ == "__main__":
    main()
