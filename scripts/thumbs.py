#!/usr/bin/env python3
"""Thumbnails for the reports bundle (docs/DESIGN.md, "Photos").

route_links.py --bundle-dir writes each page's chosen photo (page_images.py)
as "img", the photo's address on its own site. This fetches each photo once,
checks it is a photo (an image, at least 300x200, not a strip or a blank),
writes a 320-pixel-wide WebP to OUT/thumbs/<hash>.webp, and rewrites the
bundle so each page with a thumbnail has "thumb": "thumbs/<hash>.webp" (a
path next to the bundle) instead of "img". Pages whose photo fails lose it.

Thumbnails already uploaded (--existing, one hash per line) are not fetched
again: the hash is of the photo's address, so a page keeps its thumbnail
across builds.

Nor are photos that failed in the last --retry-days (30) days (--failed, a
CSV: hash,reason,last_tried,url): most fail the same way every time (gone,
too small, a strip), and fetching them all again took most of a build's
5 minutes here. Those pages go without a photo, as they did when it failed.

  python3 scripts/thumbs.py --bundle-dir reports/ --existing have.txt --failed failed.csv --out out/

Writes out/reports.json, out/reports.bin, out/thumbs/*.webp (new ones only)
and out/failed.csv: the photos still in the bundle without a thumbnail, each
with when it was last tried. Needs Pillow.
"""
import argparse
import collections
import concurrent.futures as cf
import csv
import datetime
import gzip
import hashlib
import io
import json
import os
import sys
import threading
import urllib.request
from urllib.parse import urlparse

from PIL import Image, ImageStat

UA = "HikerAtlas/1.0 (+https://www.hikeratlas.com; trip-report thumbnails)"
MAX_BYTES = 15 << 20
WIDTH = 320


def key(url):
    return hashlib.sha1(url.encode()).hexdigest()[:20]


_host_locks = collections.defaultdict(lambda: threading.Semaphore(2))


def fetch(url):
    """The photo's bytes, at most two requests to one host at a time."""
    with _host_locks[urlparse(url).netloc]:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/*"})
        with urllib.request.urlopen(req, timeout=20) as r:
            if not (r.headers.get("Content-Type") or "").startswith("image/"):
                raise ValueError("not an image: " + str(r.headers.get("Content-Type")))
            data = r.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("too big")
            return data


def thumbnail(data):
    """A WebP thumbnail of a photo, or ValueError when it isn't one."""
    im = Image.open(io.BytesIO(data))
    im.draft("RGB", (WIDTH * 2, WIDTH * 2))  # JPEG: decode at a smaller scale
    im = im.convert("RGB")
    w, h = im.size
    if w < 300 or h < 200:
        raise ValueError(f"small {w}x{h}")
    if not 0.4 <= w / h <= 2.6:
        raise ValueError(f"aspect {w}x{h}")
    if max(ImageStat.Stat(im.resize((32, 32))).stddev) < 12:
        raise ValueError("blank")  # a solid colour or nearly
    im.thumbnail((WIDTH, WIDTH * 2), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "WEBP", quality=72, method=5)
    return out.getvalue()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle-dir", required=True, help="reports.json and reports.bin from route_links.py")
    ap.add_argument("--existing", help="file of thumbnail hashes already uploaded, one per line")
    ap.add_argument("--failed", help="CSV of photos that failed before (hash,reason,last_tried,url), from an earlier run's out/failed.csv")
    ap.add_argument("--retry-days", type=int, default=30, help="try a failed photo again after this many days")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=24)
    args = ap.parse_args()
    os.makedirs(os.path.join(args.out, "thumbs"), exist_ok=True)
    have = set()
    if args.existing and os.path.exists(args.existing):
        have = {ln.strip() for ln in open(args.existing) if ln.strip()}

    index = json.load(open(os.path.join(args.bundle_dir, "reports.json")))
    data = open(os.path.join(args.bundle_dir, index["bundle"]), "rb").read()
    parks = {pid: json.loads(gzip.decompress(data[o:o + n])) for pid, (o, n) in index["parks"].items()}
    urls = {p["img"] for d in parks.values() for v in d.values() for sec in v.values() for p in sec if p.get("img")}
    today = datetime.date.today()
    failed = {}  # hash -> (reason, last tried)
    if args.failed and os.path.exists(args.failed):
        with open(args.failed, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    failed[row["hash"]] = (row["reason"], datetime.date.fromisoformat(row["last_tried"]))
                except (KeyError, ValueError):
                    pass  # a malformed row: that photo is just tried again
    recent = {h for h, (_, when) in failed.items() if (today - when).days < args.retry_days}
    missing = [u for u in urls if key(u) not in have]
    todo = sorted(u for u in missing if key(u) not in recent)
    print(f"{len(urls):,} photos; {len(urls) - len(missing):,} already have thumbnails; "
          f"{len(missing) - len(todo):,} failed in the last {args.retry_days} days; fetching {len(todo):,}",
          file=sys.stderr)

    ok = {u for u in urls if key(u) in have}
    why = collections.Counter()

    def one(u):
        try:
            webp = thumbnail(fetch(u))
        except Exception as e:  # noqa: BLE001 - any failure just drops the photo
            return u, type(e).__name__ if not isinstance(e, ValueError) else str(e).split(" ")[0]
        with open(os.path.join(args.out, "thumbs", key(u) + ".webp"), "wb") as f:
            f.write(webp)
        return u, None

    tried = {}  # url -> its failure, for this run's failures
    with cf.ThreadPoolExecutor(args.workers) as ex:
        for i, (u, err) in enumerate(ex.map(one, todo), 1):
            if err:
                why[err] += 1
                tried[u] = err
            else:
                ok.add(u)
            if i % 500 == 0:
                print(f"  {i:,}/{len(todo):,}", file=sys.stderr)
    print(f"thumbnails: {len(ok):,} of {len(urls):,} photos; failures {dict(why.most_common())}", file=sys.stderr)

    # The photos without a thumbnail, for the next run: this run's failures
    # tried today, and the recent ones it skipped as they were. Photos gone
    # from the bundle are dropped.
    with open(os.path.join(args.out, "failed.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hash", "reason", "last_tried", "url"])
        for u in sorted(u for u in missing if u not in ok):
            h = key(u)
            reason, when = (tried[u], today) if u in tried else failed[h]
            w.writerow([h, reason, when.isoformat(), u])

    # Rewrite the bundle: "thumb" for photos that made it, no "img" either way.
    shown = 0
    out_index, off = {}, 0
    with open(os.path.join(args.out, "reports.bin"), "wb") as b:
        for pid in sorted(parks):
            d = parks[pid]
            for v in d.values():
                for sec in v.values():
                    for p in sec:
                        u = p.pop("img", None)
                        if u in ok:
                            p["thumb"] = f"thumbs/{key(u)}.webp"
                            shown += 1
            gz = gzip.compress(json.dumps(d, separators=(",", ":"), sort_keys=True).encode(), mtime=0)
            b.write(gz)
            out_index[pid] = [off, len(gz)]
            off += len(gz)
    json.dump({"bundle": "reports.bin", "parks": out_index}, open(os.path.join(args.out, "reports.json"), "w"))
    print(f"{shown:,} page entries have a thumbnail", file=sys.stderr)


if __name__ == "__main__":
    main()
