#!/usr/bin/env python3
"""A photo for each hike page: an image whose caption names the page's own
destination or trail (docs/DESIGN.md, "Photos").

A page in the hike table is already about a destination or trail. An image
on it counts only when its caption, alt text, title or file name names one
of those: "page match plus caption match", so a page's logo, ads, author
photo and other posts' thumbnails don't qualify.

  plan  hikes.tsv.gz + page passes' pages.tsv.gz (WARC pointers) ->
        partitions of hike pages for `warcfetch -images`
  pick  warcfetch -images output + hikes.tsv.gz -> page_images.tsv.gz:
        url, image (absolute URL), source (figcaption/alt/title/file),
        label (the matching text), og (1 when it is also the page's og:image)

Rules for an image (pick):
  - not linked to another page (a related-post thumbnail names the same
    trail but shows another post), not tiny (width or height under 200),
    not a logo, icon, avatar, ad or share button by its file name;
  - its text names one of the page's destinations or trails of two or more
    words ("Garnet Lake", not "Lake");
  - not a map, topographic print, sign, poster or gear shot by its text.
The best image ranks a caption first, then alt and title, then the file
name, and prefers the page's og:image (the site's own choice of preview)
and larger images. A file name alone must not be a camera-style "...001".
"""
import argparse
import csv
import glob
import gzip
import json
import os
import re
import sys
from urllib.parse import unquote, urljoin, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract import norm  # noqa: E402

csv.field_size_limit(1 << 30)

SKIP_SRC = re.compile(r"(logo|icon|avatar|gravatar|sprite|banner|badge|button|emoji|pixel|spacer|1x1|/ads?/|"
                      r"[-_]ads?[-_.]|share|facebook|twitter|pinterest|instagram|feedburner|widget|placeholder)",
                      re.I)
BAD_TEXT = re.compile(r"\b(map|maps|topo|topographic|print|prints|poster|sign|signs|signpost|brochure|logo|"
                      r"advertisement|book|guidebook|gear|jacket|boots|pack|tent|elevation profile|screenshot|"
                      r"strava|gpx|chart)\b", re.I)
IMAGE_EXT = re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)", re.I)
CAMERA = re.compile(r"\d{3,}")
WEIGHT = {"caption": 4, "alt": 3, "title": 3, "file": 2}


def read_tsv(path):
    with gzip.open(path, "rt") as f:
        yield from csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)


def join(base, ref):
    """urljoin that gives "" for a malformed URL (an og:image of
    "http://[uploads_url]/..." raises in urllib) instead of failing."""
    try:
        return urljoin(base, ref or "")
    except ValueError:
        return ""


def page_names(row):
    """A hike row's destination and trail names, normalized, of 2+ words."""
    out = set()
    for col in ("destinations", "trails"):
        for n in (row.get(col) or "").split(";"):
            k = norm(n.strip())
            if len(k.split()) >= 2:
                out.add(k)
    return out


def names_in(text, names):
    t = " " + norm(text) + " "
    return [n for n in names if " " + n + " " in t]


def plan(args):
    want = {r["url"] for r in read_tsv(args.hikes)}
    rows = {}
    cols = None
    for p in sorted({p for pat in args.pages for p in glob.glob(pat)}):
        with gzip.open(p, "rt") as f:
            rd = csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
            for r in rd:
                if r["url"] in want and r.get("warc_filename") and r["url"] not in rows:
                    rows[r["url"]] = r
    cols = ["url", "site", "warc_filename", "warc_record_offset", "warc_record_length"]
    os.makedirs(args.out_dir, exist_ok=True)
    files = [open(os.path.join(args.out_dir, f"part-{i}.tsv"), "w") for i in range(args.partitions)]
    for f in files:
        f.write("\t".join(cols) + "\n")
    for i, u in enumerate(sorted(rows)):
        r = rows[u]
        files[i % args.partitions].write("\t".join(str(r.get(c, "")).replace("\t", " ") for c in cols) + "\n")
    for f in files:
        f.close()
    print(f"{len(rows):,} of {len(want):,} hike pages have a WARC pointer; {args.partitions} partitions",
          file=sys.stderr)


def best_image(page, names):
    """(score, image url, source, label, og) of the page's best caption-matched image, or None."""
    url = page["url"]
    og = join(url, page.get("og_image"))
    og_key = re.sub(r"(-\d+x\d+)?(\.\w+)(\?.*)?$", r"\2", og.split("://")[-1]) if og else None
    host = urlparse(url).netloc
    best = None
    for im in page.get("images") or []:
        src = join(url, im["src"])
        if not src.startswith("http") or SKIP_SRC.search(src):
            continue
        if (im.get("width") and im["width"] < 200) or (im.get("height") and im["height"] < 200):
            continue
        link = im.get("link") or ""
        if link and not IMAGE_EXT.search(link) and join(url, link).split("#")[0] != url.split("#")[0]:
            try:
                lp = urlparse(join(url, link))
            except ValueError:
                continue
            # A link to the image's own full size (Blogger, Flickr pages of the
            # same photo) is fine; a link to another post isn't.
            if lp.netloc == host or not re.search(r"(blogger|bp\.blogspot|flickr|smugmug|googleusercontent)",
                                                  lp.netloc + lp.path):
                continue
        fname = unquote(src.rsplit("/", 1)[-1].split("?")[0]).rsplit(".", 1)[0]
        fields = {"caption": im.get("caption") or "", "alt": im.get("alt") or "", "title": im.get("title") or "",
                  "file": re.sub(r"[-_+.]+", " ", fname)}
        for source in ("caption", "alt", "title", "file"):
            text = fields[source]
            if not text or not names_in(text, names) or BAD_TEXT.search(text):
                continue
            if source == "file" and CAMERA.search(text):
                continue
            is_og = bool(og_key) and re.sub(r"(-\d+x\d+)?(\.\w+)(\?.*)?$", r"\2", src.split("://")[-1]) == og_key
            score = WEIGHT[source] + (2 if is_og else 0) + (1 if (im.get("width") or 0) >= 600 else 0)
            cand = (score, src, source, text[:200], int(is_og))
            if best is None or cand[0] > best[0]:
                best = cand
            break
    return best


def pick(args):
    names = {r["url"]: page_names(r) for r in read_tsv(args.hikes)}
    n = found = 0
    by_source = {}
    with gzip.open(args.out, "wt") as o:
        o.write("url\timage\tsource\tlabel\tog\n")
        for p in sorted({p for pat in args.pages for p in glob.glob(pat)}):
            opener = gzip.open if p.endswith(".gz") else open
            with opener(p, "rt") as f:
                for line in f:
                    page = json.loads(line)
                    nm = names.get(page["url"])
                    if not nm or page.get("error"):
                        continue
                    n += 1
                    b = best_image(page, nm)
                    if b:
                        found += 1
                        by_source[b[2]] = by_source.get(b[2], 0) + 1
                        label = re.sub(r"\s+", " ", b[3]).replace("\t", " ")
                        o.write(f"{page['url']}\t{b[1]}\t{b[2]}\t{label}\t{b[4]}\n")
    print(f"{found:,} of {n:,} pages with names have a caption-matched image; by source {by_source}",
          file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--hikes", required=True)
    p.add_argument("--pages", nargs="+", required=True, help="page passes' pages.tsv.gz (globs allowed)")
    p.add_argument("--partitions", type=int, default=8)
    p.add_argument("--out-dir", required=True)
    k = sub.add_parser("pick")
    k.add_argument("--hikes", required=True)
    k.add_argument("--pages", nargs="+", required=True, help="warcfetch -images output (.jsonl[.gz], globs)")
    k.add_argument("--out", required=True)
    args = ap.parse_args()
    plan(args) if args.cmd == "plan" else pick(args)


if __name__ == "__main__":
    main()
