#!/usr/bin/env python3
"""GPS tracks linked from hike pages (hikeratlas/cc-crawler#82; docs/DESIGN.md "Tracks").

Hike pages link their GPS tracks: a GPX/KML/KMZ file, a Google My Maps map, a
CalTopo map, a Strava or Gaia recording. A track shows which way a page went,
far better than its place names, and tracks that follow no curated route are
routes Hiker Atlas is missing.

  collect  `warcfetch -images` output (its "tracks") + hikes.tsv.gz ->
           track_links.tsv.gz: one row per (page, track link), with where we
           saw it (the Common Crawl record)
  fetch    track_links.tsv.gz -> tracks.jsonl.gz: each fetchable track once,
           parsed into simplified lines, with its provenance; raw/ keeps the
           bytes as fetched, named by their sha1

Fetchable: linked files (live; when the live file is gone, the Internet
Archive's copy), Google My Maps (its KML export) and CalTopo (its public map
API). Strava, Garmin Connect, Gaia and AllTrails need an account or bar
scraping: their links are kept in track_links.tsv.gz, unfetched.

Provenance: every track record says where the track came from and how we got
it: the link as written on each page that links it (the page, its site, the
link text, the crawl record we read it in), the URL fetched, whether from the
live web or the Internet Archive (and the snapshot's time), when, the HTTP
status, the bytes' sha1 and size, and the file's own creator, name and time.
Tracks are the authors' data: used to match pages to routes and find missing
routes, not shown or redistributed as tracks.
"""
import argparse
import collections
import concurrent.futures as cf
import csv
import datetime
import glob
import gzip
import hashlib
import http.client
import io
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from urllib.parse import parse_qs, quote, urljoin, urlparse

csv.field_size_limit(1 << 30)

UA = "Mozilla/5.0 (compatible; HikerAtlasTracks/1.0; +https://www.hikeratlas.com)"
FETCHABLE = {"gpx_file", "google_mymaps", "caltopo"}
LINK_COLS = ["page_url", "site", "kind", "link", "track_id", "text", "warc_filename", "warc_record_offset"]
MAX_BYTES = 30_000_000
SIMPLIFY_M = 10.0
MAX_LINES = 200


def read_tsv(path):
    with gzip.open(path, "rt") as f:
        yield from csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)


def crawl_of(warc_filename):
    m = re.search(r"(CC-MAIN-\d{4}-\d{2})", warc_filename or "")
    return m.group(1) if m else ""


def track_id(kind, link):
    """A stable id for the track a link names, and the URL to fetch it from:
    the same My Maps map linked as viewer, embed or edit is one track."""
    if kind == "google_mymaps":
        mid = (parse_qs(urlparse(link).query).get("mid") or [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{6,}", mid):
            return "", ""
        return f"mymaps:{mid}", f"https://www.google.com/maps/d/kml?mid={mid}&forcekml=1"
    if kind == "caltopo":
        m = re.search(r"caltopo\.com/m/([A-Za-z0-9]+)", link)
        return (f"caltopo:{m.group(1)}", f"https://caltopo.com/api/v1/map/{m.group(1)}/since/0") if m else ("", "")
    if kind == "gpx_file":
        u = link.split("#")[0]
        # A map viewer showing a file (".../MapViewer.html?q=https://.../hike.gpx"): the file.
        for vals in parse_qs(urlparse(u).query).values():
            for v in vals:
                if re.match(r"https?://", v) and re.search(r"\.(gpx|kml|kmz)(\?|$)", v, re.I):
                    u = v
        return ("file:" + u, u) if u.startswith("http") else ("", "")
    m = re.search(r"/(\d{4,})", link)
    return (f"{kind}:{m.group(1)}" if m else f"{kind}:{link.split('#')[0]}"), ""


def collect(args):
    hikes = {r["url"]: r.get("site", "") for r in read_tsv(args.hikes)}
    n_pages = n_with = 0
    kinds = collections.Counter()
    with gzip.open(args.out, "wt") as o:
        w = csv.writer(o, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\", lineterminator="\n")
        w.writerow(LINK_COLS)
        for p in sorted({p for pat in args.pages for p in glob.glob(pat)}):
            opener = gzip.open if p.endswith(".gz") else open
            with opener(p, "rt") as f:
                for line in f:
                    page = json.loads(line)
                    url = page["url"]
                    if url not in hikes or page.get("error"):
                        continue
                    n_pages += 1
                    row = page.get("row") or {}
                    seen = set()
                    for t in page.get("tracks") or []:
                        try:
                            link = urljoin(url, t["url"])
                        except ValueError:
                            continue
                        tid, _ = track_id(t["kind"], link)
                        if not tid or tid in seen:
                            continue
                        seen.add(tid)
                        kinds[t["kind"]] += 1
                        text = re.sub(r"\s+", " ", t.get("text") or "").replace("\t", " ").replace("\\", " ")[:200]
                        w.writerow([url, hikes[url] or row.get("site", ""), t["kind"], link.replace("\t", "%09"), tid,
                                    text, row.get("warc_filename", ""), row.get("warc_record_offset", "")])
                    n_with += bool(seen)
    print(f"{n_with:,} of {n_pages:,} hike pages link a track; links by kind {dict(kinds.most_common())}",
          file=sys.stderr)


# --- fetching ---------------------------------------------------------------

# At most 2 requests at a time per host; 4 for the map services, which serve
# most tracks and many sites' maps.
BIG_HOSTS = {"www.google.com": 4, "caltopo.com": 4, "archive.org": 3, "web.archive.org": 3}
_locks = collections.defaultdict(lambda: threading.Semaphore(2))
for _h, _n in BIG_HOSTS.items():
    _locks[_h] = threading.Semaphore(_n)


def get(u, timeout=40):
    """(status, final url, content type, bytes); raises on network errors."""
    u = quote(u, safe=":/?#[]@!$&'()*+,;=%~")  # spaces and other raw characters in file names
    with _locks[urlparse(u).netloc.lower()]:
        req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.geturl(), r.headers.get("Content-Type", ""), r.read(MAX_BYTES)
        except urllib.error.HTTPError as e:
            return e.code, u, e.headers.get("Content-Type", "") if e.headers else "", b""


def wayback(u):
    """(snapshot url for the raw bytes, timestamp) of the Internet Archive's
    newest good copy of u, or ("", "")."""
    try:
        st, _, _, body = get("https://archive.org/wayback/available?url=" + quote(u, safe=""), timeout=30)
        snap = (json.loads(body or b"{}").get("archived_snapshots") or {}).get("closest") or {}
    except (OSError, ValueError, urllib.error.URLError):
        return "", ""
    if not snap.get("available") or str(snap.get("status")) != "200":
        return "", ""
    ts = snap["timestamp"]
    return f"https://web.archive.org/web/{ts}id_/{u}", ts


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


TAG = r"(?:[A-Za-z0-9_]+:)?"


def first(rx, text):
    m = re.search(rx, text, re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<!\[CDATA\[|\]\]>|<[^>]+>", " ", m.group(1))).strip()[:200] if m else ""


def parse_gpx(text):
    meta = {"creator": first(r"<gpx\b[^>]*\bcreator=[\"']([^\"']*)", text),
            "name": first(rf"<{TAG}metadata>.*?<{TAG}name>(.*?)</", text) or first(rf"<{TAG}name>(.*?)</", text),
            "time": first(rf"<{TAG}time>(.*?)</", text)}
    lines = []
    spans = [m.group(0) for m in re.finditer(rf"<{TAG}(?:trk|rte)\b.*?</{TAG}(?:trk|rte)>", text, re.S | re.I)]
    for span in spans:
        name = first(rf"^<[^>]*>\s*<{TAG}name>(.*?)</", span) or first(rf"<{TAG}name>(.*?)</", span)
        for seg in re.findall(rf"<{TAG}trkseg\b.*?</{TAG}trkseg>", span, re.S | re.I) or [span]:
            pts = []
            for m in re.finditer(rf"<{TAG}(?:trkpt|rtept)\b([^>]*?)(?:/>|>(.*?)</{TAG}(?:trkpt|rtept)>)", seg,
                                 re.S | re.I):
                lat = num(first(r"\blat=[\"']([^\"']*)", m.group(1)))
                lon = num(first(r"\blon=[\"']([^\"']*)", m.group(1)))
                ele = num(first(rf"<{TAG}ele>(.*?)</", m.group(2) or ""))
                if lat is not None and lon is not None:
                    pts.append((lon, lat, ele))
            if len(pts) >= 2:
                lines.append((name, pts))
    return meta, lines


def parse_kml(text):
    meta = {"creator": first(rf"<{TAG}author>(.*?)</{TAG}author>", text),
            "name": first(rf"<{TAG}Document>\s*<{TAG}name>(.*?)</", text) or first(rf"<{TAG}name>(.*?)</", text),
            "time": first(rf"<{TAG}when>(.*?)</", text)}
    lines = []
    for pm in re.findall(rf"<{TAG}Placemark\b.*?</{TAG}Placemark>", text, re.S | re.I):
        name = first(rf"<{TAG}name>(.*?)</", pm)
        for c in re.findall(rf"<{TAG}LineString\b.*?<{TAG}coordinates>(.*?)</", pm, re.S | re.I):
            pts = []
            for tup in c.split():
                v = [num(x) for x in tup.split(",")]
                if len(v) >= 2 and v[0] is not None and v[1] is not None:
                    pts.append((v[0], v[1], v[2] if len(v) > 2 else None))
            if len(pts) >= 2:
                lines.append((name, pts))
        gx = re.findall(r"<gx:coord>(.*?)</gx:coord>", pm, re.S)
        if len(gx) >= 2:
            pts = []
            for g in gx:
                v = [num(x) for x in g.split()]
                if len(v) >= 2 and None not in v[:2]:
                    pts.append((v[0], v[1], v[2] if len(v) > 2 else None))
            if len(pts) >= 2:
                lines.append((name, pts))
    return meta, lines


def parse_caltopo(body):
    d = json.loads(body)
    st = d.get("state") or d.get("result", {}).get("state") or d.get("result") or {}
    feats = st.get("features") or []
    lines = []
    for f in feats:
        g = f.get("geometry") or {}
        props = f.get("properties") or {}
        parts = [g.get("coordinates")] if g.get("type") == "LineString" else \
            (g.get("coordinates") or []) if g.get("type") == "MultiLineString" else []
        for part in parts:
            pts = [(c[0], c[1], c[2] if len(c) > 2 and c[2] else None) for c in part or [] if len(c) >= 2]
            if len(pts) >= 2:
                lines.append((props.get("title") or "", pts))
    title = ((d.get("result") or {}).get("title") or d.get("title") or "") if isinstance(d, dict) else ""
    return {"creator": "", "name": str(title)[:200], "time": ""}, lines


def parse(body, ctype, kind):
    """(format, meta, lines) from fetched bytes."""
    if kind == "caltopo":
        return "caltopo_json", *parse_caltopo(body)
    if body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            names = sorted((n for n in z.namelist() if n.lower().endswith(".kml")), key=lambda n: n != "doc.kml")
            if not names:
                raise ValueError("kmz without kml")
            return "kmz", *parse_kml(z.read(names[0]).decode("utf-8", "replace"))
    text = body.decode("utf-8", "replace")
    head = text[:3000].lower()
    if "<gpx" in head:
        return "gpx", *parse_gpx(text)
    if "<kml" in head:
        return "kml", *parse_kml(text)
    raise ValueError("not gpx/kml" + (" (html)" if "<html" in head else ""))


def dist_m(a, b):
    la1, la2 = math.radians(a[1]), math.radians(b[1])
    dla, dlo = la2 - la1, math.radians(b[0] - a[0])
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(h)))


def simplify(pts, tol_m):
    """Douglas-Peucker on a local equirectangular projection."""
    if len(pts) < 3:
        return list(pts)
    lat0 = math.radians(sum(p[1] for p in pts) / len(pts))
    xy = [(p[0] * 111320 * math.cos(lat0), p[1] * 110540) for p in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = xy[i], xy[j]
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy)
        best, bi = 0.0, -1
        for k in range(i + 1, j):
            x, y = xy[k]
            d = abs(dy * (x - x1) - dx * (y - y1)) / L if L else math.hypot(x - x1, y - y1)
            if d > best:
                best, bi = d, k
        if best > tol_m:
            keep[bi] = True
            stack += [(i, bi), (bi, j)]
    return [p for p, k in zip(pts, keep) if k]


def line_record(name, pts):
    pts = [p for p in pts if -180 <= p[0] <= 180 and -90 <= p[1] <= 90]
    if len(pts) < 2:
        return None
    length = sum(dist_m(a, b) for a, b in zip(pts, pts[1:]))
    gain, base = 0.0, None
    for p in pts:  # climb, ignoring wiggles under 5 m
        if p[2] is None:
            continue
        if base is None or p[2] < base:
            base = p[2]
        elif p[2] - base >= 5:
            gain += p[2] - base
            base = p[2]
    has_ele = any(p[2] for p in pts)  # KML exports often say 0 for "no elevation"
    s = simplify(pts, SIMPLIFY_M)
    return {"name": (name or "")[:200], "n_points": len(pts), "length_m": round(length),
            "gain_m": round(gain) if has_ele else None,
            "bbox": [round(min(p[0] for p in pts), 5), round(min(p[1] for p in pts), 5),
                     round(max(p[0] for p in pts), 5), round(max(p[1] for p in pts), 5)],
            "coords": [[round(p[0], 5), round(p[1], 5)] for p in s]}


def fetch_one(tid, kind, link, fetch_url, raw_dir):
    rec = {"track_id": tid, "kind": kind, "link": link, "fetch_url": fetch_url, "via": "", "archived_at": "",
           "fetched_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "http_status": None, "final_url": "", "content_type": "", "bytes": 0, "sha1": "", "format": "",
           "creator": "", "name": "", "time": "", "error": "", "lines": []}
    tries = [("live", fetch_url, "")]
    body = b""
    for i, (via, u, ts) in enumerate(tries):
        try:
            st, final, ctype, body = get(u)
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            st, final, ctype, body = None, u, "", b""
            rec["error"] = f"{type(e).__name__}: {e}"[:200]
        rec.update(via=via, fetch_url=u, archived_at=ts, http_status=st, final_url=final if final != u else "",
                   content_type=ctype[:100], bytes=len(body))
        ok = st == 200 and body and not (kind == "gpx_file" and b"<html" in body[:2000].lower())
        if ok:
            rec["error"] = ""
            break
        if not rec["error"]:
            rec["error"] = f"http {st}" if st != 200 else "html instead of a track"
        if kind == "gpx_file" and i == 0:  # gone from the live web: the Internet Archive's copy
            wu, wts = wayback(fetch_url)
            if wu:
                tries.append(("wayback", wu, wts))
        body = b""
    if not body:
        return rec
    rec["sha1"] = hashlib.sha1(body).hexdigest()
    if raw_dir:
        with open(os.path.join(raw_dir, rec["sha1"]), "wb") as f:
            f.write(body)
    try:
        fmt, meta, lines = parse(body, rec["content_type"], kind)
    except (ValueError, zipfile.BadZipFile, json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        rec["error"] = f"parse: {e}"[:200]
        return rec
    rec.update(format=fmt, **{k: meta.get(k, "") for k in ("creator", "name", "time")})
    out = [r for r in (line_record(n, p) for n, p in lines[:MAX_LINES]) if r]
    rec["lines"] = out
    rec["n_lines_in_file"] = len(lines)
    if not out:
        rec["error"] = "no lines"
    return rec


def fetch(args):
    links = collections.defaultdict(list)
    first_link = {}
    for r in read_tsv(args.links):
        if r["kind"] not in FETCHABLE:
            continue
        links[r["track_id"]].append(r)
        first_link.setdefault(r["track_id"], r)
    if args.existing and os.path.exists(args.existing):
        done = {}
        with gzip.open(args.existing, "rt") as f:
            for line in f:
                d = json.loads(line)
                if not d.get("error"):
                    done[d["track_id"]] = d
    else:
        done = {}
    # Hash order mixes hosts, so the workers don't all wait on one host's limit.
    todo = sorted((t for t in links if t not in done), key=lambda t: hashlib.md5(t.encode()).hexdigest())
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(links):,} fetchable tracks; {len(done):,} already fetched; fetching {len(todo):,}", file=sys.stderr)
    if args.raw_dir:
        os.makedirs(args.raw_dir, exist_ok=True)

    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes else None

    def job(tid):
        if deadline and time.monotonic() > deadline:
            r = first_link[tid]
            return {"track_id": tid, "kind": r["kind"], "link": r["link"], "fetch_url": "", "via": "",
                    "error": "not fetched: out of time (the next run with --existing fetches it)", "lines": []}
        r = first_link[tid]
        _, fu = track_id(r["kind"], r["link"])
        try:
            return fetch_one(tid, r["kind"], r["link"], fu, args.raw_dir)
        except Exception as e:  # noqa: BLE001 - one odd link must not end the run
            return {"track_id": tid, "kind": r["kind"], "link": r["link"], "fetch_url": fu, "via": "",
                    "error": f"{type(e).__name__}: {e}"[:200], "lines": []}

    n = collections.Counter()
    with gzip.open(args.out, "wt") as o, cf.ThreadPoolExecutor(args.workers) as ex:
        def emit(rec):
            rec["pages"] = [{"url": p["page_url"], "site": p["site"], "link": p["link"], "text": p["text"],
                             "crawl": crawl_of(p["warc_filename"]), "warc_filename": p["warc_filename"],
                             "warc_record_offset": p["warc_record_offset"]} for p in links[rec["track_id"]]]
            rec["n_pages"] = len(rec["pages"])
            rec["n_sites"] = len({p["site"] for p in rec["pages"]})
            o.write(json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n")
            n[f"{rec['kind']}:{'ok' if not rec['error'] else 'fail'}" + (":wayback" if rec["via"] == "wayback" and
                                                                           not rec["error"] else "")] += 1
        for tid in sorted(done):
            if tid in links:
                emit(done[tid])
        for i, rec in enumerate(ex.map(job, todo), 1):
            emit(rec)
            if i % 500 == 0:
                print(f"  {i:,}/{len(todo):,}", file=sys.stderr)
    report(args.out, n)


def report(path, n):
    recs = [json.loads(line) for line in gzip.open(path, "rt")]
    ok = [r for r in recs if not r.get("error")]
    errs = collections.Counter(re.sub(r"\d{3}", "NNN", r["error"])[:40] if r["error"].startswith("http")
                               else r["error"][:40] for r in recs if r.get("error"))
    lines = ["# Page tracks", "",
             f"{len(recs):,} fetchable tracks linked from hike pages; {len(ok):,} fetched and parsed "
             f"({sum(len(r['lines']) for r in ok):,} lines; "
             f"{sum(1 for r in ok if r['via'] == 'wayback'):,} from the Internet Archive). "
             f"{sum(1 for r in ok if r['n_sites'] == 1 and r['n_pages'] <= 3):,} are linked from at most 3 pages "
             "of one site (a page's own track rather than a site-wide map).", "",
             "| kind | outcome | tracks |", "|---|---|---:|"]
    for k, v in sorted(n.items()):
        kind, _, outcome = k.partition(":")
        lines.append(f"| {kind} | {outcome} | {v:,} |")
    lines += ["", "| failure | tracks |", "|---|---:|"] + [f"| {k} | {v:,} |" for k, v in errs.most_common(15)]
    open(os.path.join(os.path.dirname(os.path.abspath(path)), "page_tracks_report.md"), "w").write(
        "\n".join(lines) + "\n")
    print("\n".join(lines), file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--hikes", required=True)
    c.add_argument("--pages", nargs="+", required=True, help="warcfetch -images output (.jsonl[.gz], globs)")
    c.add_argument("--out", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--links", required=True, help="track_links.tsv.gz from collect")
    f.add_argument("--out", required=True)
    f.add_argument("--raw-dir", help="keep each fetched file here, named by its sha1")
    f.add_argument("--existing", help="an earlier tracks.jsonl.gz: tracks fetched then aren't fetched again")
    f.add_argument("--workers", type=int, default=16)
    f.add_argument("--max-minutes", type=float, default=0,
                   help="stop fetching after this long; the rest are written as not fetched, for --existing next time")
    f.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    collect(args) if args.cmd == "collect" else fetch(args)


if __name__ == "__main__":
    main()
