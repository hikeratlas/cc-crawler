"""Hike and landform names in URL slugs (docs/DESIGN.md, "Recall on rare hikes").

The keyword rules in site_report.py only accept a slug with a hiking word
("hike", "trail") or a place word plus a state name. Hand-built enthusiast
sites name the destination instead: /mount-northover/, /kings_throne.html,
/second-attempt-at-carlton-peak/. This module finds Hiker Atlas names in a
slug so those pages count as matches.

Names come from the autosuggest index through slug_scan.load_names, with
stricter rules, because OSM names include phrases that are common in URLs
for other reasons ("rock climbing", "north face", "water treatment"). A
name is kept when it
  - ends in a landform word (lake, peak, canyon, ...), or starts with
    mount / mt / mont / lake / lac, or
  - comes from a trail, loop or hike name with its last word dropped
    ("kings throne trail" -> "kings throne"), and
  - isn't a region (a state or province, or a name ending in park, forest,
    island, ...) and isn't made only of generic words.
"""
import os
import re
import sqlite3
from functools import lru_cache

SPLIT = re.compile(r"[^a-z0-9]+")
CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

LANDFORM = set("""
lake lakes mountain mountains peak peaks falls creek river ridge pass canyon rock rocks butte mesa
dome hill hills knob bald point gap notch glacier basin meadow meadows valley spring springs
summit lookout overlook arch arches gorge hollow cliff cliffs bluff bluffs crest saddle cirque tarn
pond tower narrows slot wash fork brook cascade cascades cave caves glen col tor spire needle
""".split())
PREFIX = {"mount", "mt", "mont", "lake", "lac", "pic", "pico"}
TRAILISH = {"trail", "loop", "hike", "route", "traverse", "path"}
REGION_END = {"park", "parks", "forest", "wilderness", "island", "islands", "state", "area", "preserve",
              "reserve", "county", "city", "range", "mountains", "valley-park", "sp", "np", "nf"}
GENERIC = set("""
the a an of and to in on at by for de la le du des north south east west upper lower big little old new
day rock rocky high hot cold red white black green blue grand great long short iron mountain lake point
falls springs water climbing face art state national park forest
""".split())

# Trail names that are ordinary phrases in URLs (gear brands, services, events).
STOP = {tuple(x.split()) for x in """
co op|water treatment|cross country|high country|park service|mountain laurel|gregory mountain|iron horse|
bodily harm|sale mountain|paddling lake|great plains|around the world|the first|exclamation point|iron bear
""".replace("\n", "").split("|") if x.strip()}
ARTICLES = {"the", "a", "an", "of", "and", "to", "in", "on", "at", "by", "for", "around", "with"}

_HERE = os.path.dirname(os.path.abspath(__file__))


def _regions():
    out = set()
    for f in ("regions.txt", "national_parks.txt", "areas.txt"):
        p = os.path.join(_HERE, "..", "data", "gazetteer", f)
        if os.path.exists(p):
            for ln in open(p):
                ln = ln.strip().lower()
                if ln and not ln.startswith("#"):
                    out.add(tuple(t for t in SPLIT.split(ln) if t))
    return out


def load(index_path):
    """Set of name token tuples from the autosuggest index."""
    con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    regions = _regions()
    out = set()
    q = ("SELECT name FROM items WHERE kind IN ('trail','hike','peak','lake','waterfall','cliff','saddle',"
         "'valley','spring','cave','glacier','arch','viewpoint') AND country IN ('United States','Canada')")
    for (name,) in con.execute(q):
        toks = tuple(t for t in SPLIT.split(name.lower().replace("'", "").replace("’", "")) if t)
        cands = []
        if 2 <= len(toks) <= 6:
            cands.append(toks)
        if 3 <= len(toks) <= 7 and toks[-1] in TRAILISH:
            cands.append(toks[:-1])
        for t in cands:
            last, first = t[-1], t[0]
            derived = t is not toks
            if not (last in LANDFORM or first in PREFIX or derived):
                continue
            if last in REGION_END or last in TRAILISH or t in regions:
                continue
            if all(w in GENERIC or w in LANDFORM or w in PREFIX for w in t):
                continue  # "big rock", "north lake", "mount point"
            if any(w.isdigit() for w in t) or t in STOP:
                continue
            if derived and ARTICLES.intersection(t):
                continue  # "around the world trail", "the first trail"
            out.add(t)
    return out


# Slugs often drop stop words ("top-of-giant" for Top of the Giant). Matching
# every name that way matches everyday phrases ("fourth of july", "out and
# about" are OSM trails too), so only the names in stopword_names.txt (hike
# names people write reports about) also match with stop words removed.
STOPWORDS = {"the", "a", "an", "of", "and", "at", "in", "on", "to", "by", "for", "de", "la", "le", "du", "des"}


def _strip(toks):
    return tuple(t for t in toks if t not in STOPWORDS)


@lru_cache(maxsize=1)
def _stopword_names():
    """stop-word-free name tokens -> the name."""
    p = os.path.join(_HERE, "..", "data", "gazetteer", "stopword_names.txt")
    out = {}
    if os.path.exists(p):
        for ln in open(p):
            name = ln.split("#")[0].strip().lower()
            if name:
                toks = tuple(t for t in SPLIT.split(name) if t)
                if len(_strip(toks)) >= 2:
                    out[_strip(toks)] = " ".join(toks)
    return out


def match_stopword(slug):
    """A stopword_names.txt name found in a slug with stop words removed from
    both ("top-of-giant", "top-giant"), or None."""
    names = _stopword_names()
    if not names:
        return None
    toks = _strip([t for t in SPLIT.split(slug.replace("'", "")) if t])
    maxlen = max(len(k) for k in names)
    for n in range(min(maxlen, len(toks)), 1, -1):
        for i in range(len(toks) - n + 1):
            t = tuple(toks[i:i + n])
            if t in names:
                return names[t]
    return None


@lru_cache(maxsize=1)
def _cached(index_path):
    names = load(index_path)
    return names, max((len(k) for k in names), default=0)


def match(slug, index_path):
    """The longest index name found in a slug (already lower-cased, CamelCase split), or None."""
    names, maxlen = _cached(index_path)
    toks = [t for t in SPLIT.split(slug.replace("'", "")) if t]
    for n in range(min(maxlen, len(toks)), 1, -1):
        for i in range(len(toks) - n + 1):
            t = tuple(toks[i:i + n])
            if t in names:
                return " ".join(t)
    return match_stopword(slug)
