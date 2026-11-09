"""URL-level rules for dropping candidate pages that are almost never useful.

Shared by scripts/site_report.py (when candidates are generated) and
scripts/filter_candidates.py (to re-filter an existing candidate list).
Each rule was checked against the hand-labelled spot-check sample
(data/eval/spotcheck/); see docs/DESIGN.md, Stage 7.
"""
import os
import re
from urllib.parse import urlsplit

# Real-estate listings: lake and land sites have slugs full of "lake",
# "trail", "pass" and "meadow", so they slip past the slug regexes.
REALTY_PATH = re.compile(r"real-?estate|for-?sale|homes?-for|/listings?/|/listing-|\bmls\b|lhrmls|/property/|"
                         r"new-homes|/homes/|bedrooms?|\blot-\d|sqft|-acres?-|/acres?/")
REALTY_SITE = re.compile(r"homes?|realty|realestate|realtor|propert|landsales|landleader|lennar|delwebb|acreage")
# Shop category and product pages.
SHOP_PATH = re.compile(r"/(products?|collections|shop|store|cart|checkout|product-tag|product-category|c|b|pg)/")
# Image-attachment and media pages (WordPress attachments, camera file names).
ATTACHMENT = re.compile(r"^(img|imgp|dsc|dscn|dscf|image|photo|pic|screenshot|screen-shot|gopr|pxl|wp)[-_]?\d+|"
                        r"\d{6,}_\d+|\.(jpe?g|png|gif|webp)$")

REASONS = ("query_string", "real_estate", "shop", "attachment")


def _sections():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "sections.tsv")
    if not os.path.exists(path):
        return ()
    with open(path) as f:
        return tuple(ln.split("\t")[0].strip().lower() for ln in f if ln.strip() and not ln.startswith("#"))


# Trip-report sections whose page ids are in the query string (?trip=, viewtopic.php?t=).
SECTIONS = _sections()


def in_section(url):
    u = re.sub(r"^https?://(www\.)?", "", url.lower())
    return any(u.startswith(s) for s in SECTIONS)


def drop_reason(url, site):
    """Return why a candidate URL should be dropped, or None to keep it."""
    p = urlsplit(url)
    if p.query and not in_section(url):
        return "query_string"
    path = p.path.lower()
    if REALTY_PATH.search(path) or REALTY_SITE.search(site.split(".")[0]):
        return "real_estate"
    if SHOP_PATH.search(path):
        return "shop"
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if ATTACHMENT.search(last):
        return "attachment"
    return None
