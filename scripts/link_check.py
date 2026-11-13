#!/usr/bin/env python3
"""Which hike pages are still there (hikeratlas/cc-crawler#79, docs/DESIGN.md "Stale pages").

Common Crawl remembers pages that have since gone: deleted (404), domains
lapsed and parked or taken over by spam, blogs made private, sites moved.
That's fine for ranking, not for links shown to people. This checks each
hike page on the live web and gives it a status:

  ok        the page is there (final_url says where, if it moved with its
            content: same slug on a new domain, http -> https, blogspot ->
            the blog's own domain)
  http_only ok, but only over plain http
  stale     gone: 404/410, the domain no longer resolves, parked or for
            sale, private (a sign-in page, 401), redirected to another
            domain without the page's slug (a squatter, a casino), sent to
            the site's home page, a "page not found" page. Connection and
            certificate failures count only when they also failed in the
            previous run, at least --confirm-days earlier: they come and go.
  unknown   blocked (403, 429, 503, a Cloudflare challenge, "bot
            verification"): datacenter IPs, like GitHub's runners, are often
            turned away from pages that are fine. Kept.

  plan   hikes.tsv.gz -> partitions of URLs, each site in one partition
  check  a partition -> its statuses (at most 2 requests per host at once)
  merge  the partitions + the previous run's link_status.tsv.gz -> link_status.tsv.gz

link_status.tsv.gz: url, status, reason, final_url, title, checked (date),
first_failed (date of the first of consecutive transient failures).
"""
import argparse
import collections
import concurrent.futures as cf
import csv
import datetime
import glob
import gzip
import hashlib
import html
import http.client
import os
import re
import socket
import ssl
import sys
import threading
import urllib.error
import urllib.request
from urllib.parse import urlparse

csv.field_size_limit(1 << 30)

UA = "Mozilla/5.0 (compatible; HikerAtlasLinkCheck/1.0; +https://www.hikeratlas.com)"
COLS = ["url", "status", "reason", "final_url", "title", "checked", "first_failed"]
TRANSIENT = {"connect", "timeout", "tls", "server"}

PARKED = re.compile(r"(domain (?:name )?(?:is|may be) for sale|buy this domain|this domain is parked|"
                    r"hugedomains\.com|sedo\.com|dan\.com/|afternic|parkingcrew|bodis\.com|"
                    r"this domain (?:name )?has expired|domain has expired|is available for purchase|"
                    r"brandbucket\.com|godaddy\.com/forsale|parked free|courtesy of godaddy)", re.I)
# Hosting placeholders: the domain is someone's, the blog isn't there.
PLACEHOLDER = re.compile(r"(^welcome .*(bluehost|hostgator|godaddy|siteground|dreamhost)|account suspended|"
                         r"this account has been suspended|future home of|domain default page|"
                         r"^index of /|apache2 .*default page|welcome to nginx|website coming soon|"
                         r"^coming soon$|under construction|site not found|unknown domain)", re.I)
PRIVATE = re.compile(r"(this site is private|this blog is open to invited readers only|"
                     r"protected blog|log in to see this site|this content is password protected)", re.I)
SOFT404 = re.compile(r"(page not found|not found\b|(?<![\d,.])404(?![\d,.'’])|page (?:you requested )?(?:could not|cannot|can.t) be found|"
                     r"page you were looking for in this blog does not exist|nothing (?:was )?found|"
                     r"this page doesn.t exist|post not found)", re.I)
BLOCKED_TITLE = re.compile(r"(just a moment|attention required|bot verification|access denied|are you a robot|"
                           r"security check|ddos|captcha)", re.I)
SIGNIN = re.compile(r"(accounts\.google\.com|/wp-login\.php|login\.|/signin|/sign-in)", re.I)
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def host(u):
    return urlparse(u).netloc.lower().split(":")[0].removeprefix("www.").removeprefix("m.").removeprefix("mail.")


def slug(path):
    """The last meaningful path segment: "/2016/07/veach-gap.html" -> "veach-gap"."""
    segs = [s for s in path.rstrip("/").split("/") if s]
    s = segs[-1] if segs else ""
    return re.sub(r"\.(html?|php|aspx?)$", "", s.lower())


def blocked(code, title):
    return code in (403, 429, 503, 520, 521, 522, 523, 524, 525, 526) or bool(BLOCKED_TITLE.search(title or ""))


_locks = collections.defaultdict(lambda: threading.Semaphore(2))


def fetch(u, timeout):
    """(status code, final url, first 300 KB of the body as text); raises on network errors."""
    with _locks[host(u)]:
        req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.geturl(), r.read(300_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read(100_000).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            return e.code, e.geturl() or u, body


def net_reason(e):
    r = e.reason if isinstance(e, urllib.error.URLError) else e
    s = str(r)
    if isinstance(r, socket.gaierror) or "Name or service not known" in s or "nodename nor servname" in s \
            or "No address associated" in s:
        return "dns"
    if isinstance(r, ssl.SSLError) or "SSL" in s or "CERTIFICATE" in s:
        return "tls"
    if isinstance(r, (socket.timeout, TimeoutError)) or "timed out" in s:
        return "timeout"
    return "connect"


def classify(u, timeout=20):
    """(status, reason, final_url, title) for one page."""
    tries = [u]
    if u.startswith("http://"):
        tries = ["https://" + u[7:], u]  # prefer https where it works
    last = None
    for i, t in enumerate(tries):
        try:
            code, final, body = fetch(t, timeout)
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            last = e
            continue
        m = TITLE.search(body)
        title = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()[:200] if m else ""
        if blocked(code, title):
            return "unknown", "blocked", "", title
        if code in (404, 410):
            if i == 0 and len(tries) == 2:
                continue  # https 404 on an http site: try plain http
            return "stale", "gone", "", title
        if code == 401:
            return "stale", "private", "", title
        if code >= 500:
            if i == 0 and len(tries) == 2:
                continue
            return "stale", "server", "", title
        if code >= 400:
            return "unknown", f"http_{code}", "", title
        head = body[:30000]
        if PARKED.search(head) or PLACEHOLDER.search(title):
            return "stale", "parked", final, title
        if PRIVATE.search(head) or SIGNIN.search(final):
            return "stale", "private", final, title
        fu, ou = urlparse(final), urlparse(u)
        if host(final) != host(u):
            # Moved with its content keeps the page's slug ("mount-pierce").
            s = slug(ou.path)
            if s and len(s) >= 5 and slug(fu.path) == s:
                return "ok", "moved", final, title
            return "stale", "moved_away", final, title
        if fu.path.strip("/") == "" and ou.path.strip("/") != "":
            return "stale", "to_home", final, title
        if SOFT404.search(title):
            return "stale", "soft404", final, title
        if i == 1:  # only plain http answered
            return "http_only", "", final if final != u else "", title
        return "ok", "", final if final.rstrip("/") != u.rstrip("/") else "", title
    reason = net_reason(last) if last is not None else "connect"
    return "stale", reason, "", ""


def read_tsv(path):
    with gzip.open(path, "rt") as f:
        yield from csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)


def interleave(urls):
    """Round-robin across hosts: the workers spread over many sites instead of
    queueing behind one site's 2-request limit."""
    by = collections.defaultdict(list)
    for u in urls:
        by[host(u)].append(u)
    queues = sorted(by.values(), key=lambda q: (-len(q), q[0]))
    out = []
    for i in range(max((len(q) for q in queues), default=0)):
        out += [q[i] for q in queues if i < len(q)]
    return out


def plan(args):
    urls = sorted({r["url"] for r in read_tsv(args.hikes)})
    os.makedirs(args.out_dir, exist_ok=True)
    # Each site in one partition; the biggest sites first, each into the
    # partition with the fewest pages so far (two 5,000-page sites in one
    # partition outlast the job's time limit).
    by = collections.defaultdict(list)
    for u in urls:
        by[host(u)].append(u)
    parts = [[] for _ in range(args.partitions)]
    for h in sorted(by, key=lambda h: (-len(by[h]), hashlib.md5(h.encode()).hexdigest())):
        min(parts, key=len).extend(by[h])
    parts = [interleave(p) for p in parts]
    for i, p in enumerate(parts):
        with open(os.path.join(args.out_dir, f"part-{i}.txt"), "w") as f:
            f.write("\n".join(p) + "\n")
    print(f"{len(urls):,} pages, {args.partitions} partitions ({min(map(len, parts)):,}-{max(map(len, parts)):,})",
          file=sys.stderr)


def check(args):
    urls = [ln.strip() for ln in open(args.urls) if ln.strip()]
    today = datetime.date.today().isoformat()
    n = collections.Counter()
    with gzip.open(args.out, "wt") as o, cf.ThreadPoolExecutor(args.workers) as ex:
        w = csv.writer(o, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\", lineterminator="\n")
        w.writerow(COLS)
        for i, (u, res) in enumerate(zip(urls, ex.map(lambda x: classify(x, args.timeout), urls)), 1):
            status, reason, final, title = res
            n[f"{status}:{reason}" if reason else status] += 1
            w.writerow([u, status, reason, final, title.replace("\t", " ").replace("\\", " "), today, ""])
            if i % 1000 == 0:
                print(f"  {i:,}/{len(urls):,}", file=sys.stderr)
    print(f"{len(urls):,} pages: {dict(n.most_common())}", file=sys.stderr)


def merge(args):
    prev = {r["url"]: r for r in read_tsv(args.previous)} if args.previous and os.path.exists(args.previous) else {}
    out, n = [], collections.Counter()
    for p in sorted({p for pat in args.parts for p in glob.glob(pat)}):
        for r in read_tsv(p):
            if r["status"] == "stale" and r["reason"] in TRANSIENT:
                # Stale only when it already failed this way a while ago.
                old = prev.get(r["url"])
                first = (old.get("first_failed") or (old["checked"] if old["reason"] in TRANSIENT else "")) if old else ""
                if old and old["reason"] in TRANSIENT and first:
                    days = (datetime.date.fromisoformat(r["checked"]) - datetime.date.fromisoformat(first)).days
                    r["first_failed"] = first
                    if days < args.confirm_days:
                        r["status"] = "unknown"
                else:
                    r["first_failed"] = r["checked"]
                    r["status"] = "unknown"
            n[f"{r['status']}:{r['reason']}" if r["reason"] else r["status"]] += 1
            out.append(r)
    # Pages this run didn't reach (a partition that ran out of time) keep
    # their previous status.
    seen = {r["url"] for r in out}
    carried = [r for u, r in prev.items() if u not in seen]
    for r in carried:
        n[f"{r['status']}:{r['reason']}" if r["reason"] else r["status"]] += 1
    out += carried
    with gzip.open(args.out, "wt") as o:
        w = csv.DictWriter(o, fieldnames=COLS, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\",
                           lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for r in out:
            w.writerow(r)
    total = len(out)
    lines = ["# Link check", "", f"{total - len(carried):,} hike pages checked on the live web"
             + (f"; {len(carried):,} not reached this run keep the previous run's status." if carried else "."), "",
             "| status | reason | pages | share |", "|---|---|---:|---:|"]
    for k, v in n.most_common():
        s, _, reason = k.partition(":")
        lines.append(f"| {s} | {reason} | {v:,} | {v / max(total, 1):.1%} |")
    open(os.path.join(os.path.dirname(os.path.abspath(args.out)), "link_check_report.md"), "w").write(
        "\n".join(lines) + "\n")
    print("\n".join(lines), file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--hikes", required=True)
    p.add_argument("--partitions", type=int, default=8)
    p.add_argument("--out-dir", required=True)
    c = sub.add_parser("check")
    c.add_argument("--urls", required=True, help="a partition from plan (one URL per line)")
    c.add_argument("--out", required=True)
    c.add_argument("--workers", type=int, default=32)
    c.add_argument("--timeout", type=int, default=20)
    m = sub.add_parser("merge")
    m.add_argument("--parts", nargs="+", required=True)
    m.add_argument("--previous", help="the previous run's link_status.tsv.gz")
    m.add_argument("--confirm-days", type=int, default=7)
    m.add_argument("--out", required=True)
    args = ap.parse_args()
    {"plan": plan, "check": check, "merge": merge}[args.cmd](args)


if __name__ == "__main__":
    main()
