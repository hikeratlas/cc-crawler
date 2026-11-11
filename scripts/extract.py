#!/usr/bin/env python3
"""Stage 9 (first pass): extract structured facts from fetched hiking pages.

For each page fetched by cmd/warcfetch (the page-pass release's
page-text-*.jsonl.gz), this finds:

  page_type      trip_report | trail_guide | list_article | article | general
  features       named trails, hikes, peaks, lakes, waterfalls, cliffs and
                 parks mentioned in the text, matched to OpenStreetMap through
                 Hiker Atlas's own name index (the basemap's autosuggest.db),
                 each with its OSM id and coordinates
  location       where the page is about: median of the matched features
                 that agree with each other, plus state/province and park
  distance_mi    a route distance stated in the text (round trip, total, loop…)
  gain_ft        elevation gain stated in the text
  date           publication date from the URL, else the first date in the text

Name matching works on capitalised phrases in the text (so "the lake trail"
doesn't match "Lake Trail"), skips generic names ("North Trail", "Big
Lake"), and disambiguates names used in many places ("Bush Mountain" names
six peaks) by preferring features in states and parks the page mentions and
features that cluster with the page's other matches. Stdlib only.

Usage:
  scripts/extract.py --index autosuggest.db --pages 'page-text-*.jsonl.gz' \
      [--keep pages.tsv.gz] --out extracted.jsonl.gz
"""
import argparse
import csv
import glob
import gzip
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from collections import ChainMap, Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import page_type as type_model  # noqa: E402

TYPE_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "models", "type_model.json")
with open(TYPE_MODEL_PATH) as _f:
    TYPE_MODEL = json.load(_f)

# Names in the index that are mostly ordinary English phrases on a page.
STOP_NAMES = {"out and back", "runner up", "back to back", "get out", "return policy", "main street",
              "black diamond", "easter egg", "canoe launch", "great wall", "great wall of china",
              "first aid", "san francisco", "south rim", "north rim", "smoky mountain", "smoky mountains", "day hike", "day hikes", "food storage", "bear country", "free shipping"}
SINGLE_KINDS = ("peak", "lake", "waterfall", "cliff")
SINGLE_OK_BEFORE = {"mount", "mt", "hike", "hiking", "climb", "climbing", "summit", "summiting", "to", "up"}
SINGLE = defaultdict(list)  # one-word name -> features; filled by load_index
KINDS = ("trail", "hike", "peak", "lake", "waterfall", "cliff", "park")
GENERIC = set("""
the a an of and at on to in
lake lakes trail trails loop path route spur connector access cutoff cut-off link
north south east west northeast northwest southeast southwest upper lower middle
big little main old new grand great long short high low round
river creek brook run fork branch spring springs pond ponds falls fall cascade cascades
park mountain mountains mount mt peak peaks hill hills ridge ridges knob bald point butte
nature interpretive fitness bike horse equestrian multi-use multiuse service fire road
overlook view vista lookout campground camp trailhead parking lot beach shore lakeshore
canyon valley gap pass notch saddle meadow meadows forest woods wood island
hiking hike hikes walk walks walking scenic historic recreation bridge best
red blue green white black yellow orange
""".split())
STATES = {}
ABBR = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "BC": "British Columbia", "AB": "Alberta", "SK": "Saskatchewan",
    "MB": "Manitoba", "ON": "Ontario", "QC": "Quebec", "NB": "New Brunswick", "NS": "Nova Scotia",
    "PE": "Prince Edward Island", "NL": "Newfoundland and Labrador", "YT": "Yukon",
    "NT": "Northwest Territories", "NU": "Nunavut",
}
for _name in ABBR.values():
    STATES[_name.lower()] = _name
STATES["newfoundland"] = "Newfoundland and Labrador"
STATE_RE = re.compile(r"\b(" + "|".join(re.escape(s) for s in sorted(STATES, key=len, reverse=True)) + r")\b", re.I)
ABBR_RE = re.compile(r",\s*(" + "|".join(ABBR) + r")\b")

TOKEN = re.compile(r"[A-Za-z][A-Za-z'’.\-]*|\d+")
LOWER_OK = {"of", "the", "and", "de", "du", "la", "le", "des", "on", "at", "in", "to"}


def norm(name):
    s = re.sub(r"[’']", "", name.lower())  # "Angel's Landing" == "Angels Landing"
    s = re.sub(r"\bmt\.?\b", "mount", s)
    # Trip-report shorthand: "Kit Carson Pk.", "Challenger Pt.", "Hayden Mtn"
    s = re.sub(r"\bpk\b\.?", "peak", s)
    s = re.sub(r"\bpt\b\.?", "point", s)
    s = re.sub(r"\bmtns\b\.?", "mountains", s)
    s = re.sub(r"\bmtn\b\.?", "mountain", s)
    s = re.sub(r"\bst\.?\b", "saint", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(s.split())


def load_index(path):
    """name -> list of features; also park names for context matching."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    names = defaultdict(list)
    parks = {}
    pending = []  # aliases, added only where no feature has that name
    q = (f"SELECT kind, name, osm_id, park, state, lat, lon, ele, km, qrank FROM items "
         f"WHERE kind IN ({','.join('?' * len(KINDS))}) AND country IN ('United States','Canada')")
    for kind, name, osm_id, park, state, lat, lon, ele, km, qrank in con.execute(q, KINDS):
        key = norm(name)
        toks = key.split()
        if " ".join(toks) in STOP_NAMES:
            continue
        if not toks or all(t in GENERIC for t in toks):
            continue
        if not any(len(t) >= 3 and t.isalpha() and t not in GENERIC for t in toks):
            continue  # needs one real distinguishing word: no "1", "Lake 3", "Big A"
        f = {"kind": kind, "name": name, "osm_id": osm_id, "park": park, "state": state,
             "lat": float(lat), "lon": float(lon), "ele_m": ele, "km": km, "qrank": qrank or 0}
        if len(toks) == 1 or (kind == "peak" and len(toks) == 2 and toks[0] == "mount"):
            # One-word names ("Sawtooth", "Table", "Baldy") collide with
            # ordinary words, so they only count from the title, under the
            # conditions in single_word_names(). "Mount Moosilauke" is also
            # called "Moosilauke".
            word = toks[-1]
            if (kind in SINGLE_KINDS and word.isalpha() and len(word) >= 4 and word not in GENERIC
                    and word not in STATES and (qrank or 0) >= 1000):
                SINGLE[word].append(f)  # well-known only: Moosilauke yes, "Mount General" no
            if len(toks) == 1:
                continue
        names[key].append(f)
        pending += [(alias, f) for alias in aliases(kind, toks, qrank or 0)]
        if kind == "park" and len(toks) >= 2:
            parks[key] = name
    real = set(names)
    for alias, f in pending:
        if alias not in real:
            names[alias].append(f)
            if f["kind"] in ("trail", "hike") and len(alias.split()) <= 2:
                WEAK_ALIASES.add(alias)
    return names, parks, long_trails(names)


CANYONISH = {"canyon", "gulch", "wash", "narrows", "gorge", "coulee", "draw"}


# Trail aliases ("Little Wild Horse Canyon" for "... Canyon Trail") name the
# canyon too, which may be a different, unindexed place ("Antelope Canyon"):
# short ones vote for a page's region with one mention, title or not. Longer
# ones ("Little Wild Horse Canyon") are specific enough to vote in full.
WEAK_ALIASES = set()


def _trail_aliases():
    """data/gazetteer/trail_aliases.txt: trail names also matched without the
    trailing "Trail" ("Top of the Giant" for the Top of the Giant Trail)."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "gazetteer", "trail_aliases.txt")
    if not os.path.exists(p):
        return set()
    return {norm(ln.split("#")[0].strip()) for ln in open(p) if ln.split("#")[0].strip()}


TRAIL_ALIASES = _trail_aliases()


def aliases(kind, toks, qrank=0):
    """Other spellings of a name: OSM's "Yonah Mountain" is usually written
    "Mount Yonah", and "Lower Yosemite Fall" as "Lower Yosemite Falls"."""
    out = []
    if kind == "peak" and len(toks) >= 2:
        if toks[0] == "mount":
            out.append(" ".join(toks[1:] + ["mountain"]))
        elif toks[-1] == "mountain":
            out.append(" ".join(["mount"] + toks[:-1]))
        # OSM's "Kit Carson Mountain" is the climbers' "Kit Carson Peak", and
        # the other way round. Only for names with a distinguishing word, so
        # "Bald Mountain" doesn't become every "Bald Peak".
        # Well-known peaks only (qrank >= 1000): an obscure "Rider Mountain" in South Carolina
        # must not match "Trail Rider Peak" in Colorado.
        base = toks[:-1]
        if toks[-1] in ("mountain", "peak") and any(t not in GENERIC for t in base) and qrank >= 1000:
            out.append(" ".join(base + ["peak" if toks[-1] == "mountain" else "mountain"]))
    if kind in ("trail", "hike") and len(toks) >= 3 and toks[-1] == "trail" and toks[-2] in CANYONISH:
        # "Little Wild Horse Canyon Trail" is written "Little Wild Horse Canyon".
        if any(t not in GENERIC for t in toks[:-2]):
            out.append(" ".join(toks[:-1]))
    # Trails whose name people write without "Trail", from the hike table
    # (trail_aliases.txt): not every trail, or "Santa Fe Trail", "Rock Garden
    # Trail" would match every mention of Santa Fe or a rock garden.
    if kind in ("trail", "hike") and len(toks) >= 3 and toks[-1] == "trail" and " ".join(toks[:-1]) in TRAIL_ALIASES:
        out.append(" ".join(toks[:-1]))
    if kind == "waterfall" and toks[-1] == "fall":
        out.append(" ".join(toks[:-1] + ["falls"]))
    return [a for a in out if a not in STOP_NAMES]


LONG_NAME = re.compile(r"\b(trail|way|path|trace|greenway|route|traverse)\b")


def long_trails(names, spread_km=300, min_qrank=100):
    """Long-distance trails (Appalachian, Pacific Crest, Colorado Trail...).

    The index holds them as many segments spread over states, so any one
    segment's location is arbitrary. They stay features but don't vote on
    a page's region.
    """
    out = set()
    for key, fs in names.items():
        trails = [f for f in fs if f["kind"] in ("trail", "hike")]
        if len(trails) < 3 or max(f["qrank"] for f in trails) < min_qrank:
            continue
        # Common names ("Lost Lake", "Bear Creek") also have many scattered
        # trails; a long trail is named like one and is mostly trail entries.
        # ...but a long trail's segments form a chain: nearly every segment
        # has another one nearby.
        if not LONG_NAME.search(key):
            continue
        pts = [(f["lat"], f["lon"]) for f in trails]
        linked = sum(1 for i, p in enumerate(pts)
                     if any(j != i and fast_km(p, q) <= 40 for j, q in enumerate(pts)))
        if linked < 0.8 * len(pts):
            continue
        a = (trails[0]["lat"], trails[0]["lon"])
        if max(fast_km(a, (f["lat"], f["lon"])) for f in trails) > spread_km:
            out.add(key)
    return out


def haversine_km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(min(1.0, math.sqrt(h)))


def mentions(text, names, max_len=7):
    """Capitalised phrases in the text that are names in the index."""
    toks = [(m.group(0), m.start()) for m in TOKEN.finditer(text)]
    found = Counter()
    i = 0
    n = len(toks)
    while i < n:
        w = toks[i][0]
        if not (w[0].isupper() or w[0].isdigit()):
            i += 1
            continue
        best = None
        for j in range(min(n, i + max_len), i, -1):
            span = [t for t, _ in toks[i:j]]
            last = span[-1]
            if not (last[0].isupper() or last[0].isdigit()):
                continue
            if any(not (t[0].isupper() or t[0].isdigit() or t.lower() in LOWER_OK) for t in span):
                continue
            key = norm(" ".join(span))
            if key in names:
                best = (key, j)
                break
        if best:
            found[best[0]] += 1
            i = best[1]
        else:
            i += 1
    return found


def fast_km(a, b):
    """Equirectangular distance: accurate enough at the ~100 km scale."""
    x = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    y = math.radians(b[0] - a[0])
    return 6371 * math.hypot(x, y)


def resolve(found, names, ctx_states, ctx_parks, radius_km=100, max_cands=25):
    """Pick the page's region, then one feature per name within it.

    Every candidate location is tried as the page's centre. A centre scores,
    for each mentioned name, its best candidate within radius_km: mentions x
    weight, where weight is 1, +1 if the feature's state is named on the page,
    +2 if its park is. Each name counts once per centre. The best centre wins;
    names with no candidate near it are dropped (a page is about one area).
    """
    def weight(f):
        w = 1.0
        if f["state"] in ctx_states:
            w += 1.0
        if f["park"] and norm(f["park"]) in ctx_parks:
            w += 2.0
        return w

    cands = {}
    for key in found:
        cs = sorted(names[key], key=lambda f: (-weight(f), -f["qrank"]))[:max_cands]
        cands[key] = [(f, weight(f), (f["lat"], f["lon"])) for f in cs]
    if not cands:
        return [], None
    centres = list(dict.fromkeys(p for cs in cands.values() for _, _, p in cs))
    scored = []
    for c in centres:
        score = 0.0
        for key, cs in cands.items():
            m = max((w for _, w, p in cs if fast_km(c, p) <= radius_km), default=0.0)
            score += found[key] * m
        scored.append((score, c))
    scored.sort(key=lambda t: -t[0])
    # The best-scoring centre can fail the confidence gate ("Chief Trail" in
    # Indiana) while a lower one is in the state the page names most (the
    # Stawamus Chief, British Columbia): take that one.
    top_state = ctx_states.most_common(1)[0][0] if ctx_states else None
    for i, (_, best) in enumerate(scored):
        kept = keep_near(best, cands, radius_km)
        if not kept:
            continue
        ok = (confident(kept, ctx_states) if i == 0 else
              any(f["state"] == top_state for _, f in kept))
        if ok:
            pts = [(f["lat"], f["lon"]) for k, f in kept for _ in range(found[k])]
            centre = (statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts))
            return kept, centre
    return [], None


def keep_near(best, cands, radius_km):
    """One feature per name within radius_km of the centre."""
    kept = []
    for key, cs in cands.items():
        # Same weight: prefer the peak/lake/waterfall over a route of the same
        # name ("Mount Mitchell" the peak, not the hike), then the nearest.
        near = [(w, f["kind"] in ("peak", "lake", "waterfall", "cliff"), -fast_km(best, p), f)
                for f, w, p in cs if fast_km(best, p) <= radius_km]
        if near:
            f = max(near, key=lambda t: t[:3])[3]
            if f["kind"] not in ("peak", "lake", "waterfall", "cliff"):
                # The index's route "Catoctin Mountain" (a hike, tagged with
                # the park) beats the peak on weight; use the peak if it's close.
                same = [g for g, _, p in cs if g["kind"] in ("peak", "lake", "waterfall", "cliff")
                        and fast_km((f["lat"], f["lon"]), p) <= 10]
                if same:
                    f = min(same, key=lambda g: fast_km((f["lat"], f["lon"]), (g["lat"], g["lon"])))
            kept.append((key, f))
    return kept


def confident(kept, ctx_states):
    """Confidence gate: a weak match ("Great Wall", "Lynn Hill") must not place
    a page in a state the page never names. Accept the region when one of
    its states is named on the page, three names agree, one feature is
    well known (qrank >= 1000: Mount Shasta, Havasu Falls), or two names
    agree and one is moderately known (qrank >= 100)."""
    top = max(f["qrank"] for _, f in kept)
    return (any(f["state"] in ctx_states for _, f in kept) or len(kept) >= 3 or top >= 1000
            or (len(kept) >= 2 and top >= 100))


NUM = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
DIST_CTX = re.compile(r"round[- ]?trip|roundtrip|total|distance|length|loop|one[- ]way|out[- ]and[- ]back|in and out", re.I)
DIST = re.compile(NUM + r"\s*(?:-|\s)?(miles?|mi\b|km\b|kilomet(?:er|re)s?)", re.I)
GAIN = re.compile(r"(?:elevation gain|elevation change|total gain|gain|climb(?:ing)?|ascent|vertical)\s*(?:of|:|is|was|=)?\s*"
                  r"(?:about|approximately|approx\.?|around|roughly|~|nearly|over|just over)?\s*" + NUM +
                  r"\s*(feet|ft\b|foot|m\b|meters|metres|')", re.I)
GAIN2 = re.compile(NUM + r"\s*(feet|ft\b|foot|m\b|meters|metres|')\s*(?:of\s+)?(?:elevation gain|gain|climbing|of climbing|vertical)", re.I)
URL_DATE = re.compile(r"/((?:19|20)\d\d)/(\d{1,2})(?:/(\d{1,2}))?/")
TEXT_DATE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
                       r"\s+(\d{1,2}),?\s+((?:19|20)\d\d)\b")
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                       "September", "October", "November", "December"], 1)}
FIELDS = re.compile(r"\b(distance|length|elevation gain|difficulty|trailhead|trail type|route type|"
                    r"dogs allowed|best season|time required|hiking time|permits?)\s*:", re.I)
LISTY = re.compile(r"^\s*\d{1,2}\s|\b(best|top \d+|(?<![-/\d])\d{1,2} (?:best|great|easy|amazing|incredible|epic)|things to do|"
                   r"where to|guide to|ultimate guide)\b", re.I)
FIRST_PAST = re.compile(r"\b(i|we)\s+(hiked|started|reached|arrived|climbed|walked|camped|decided|made it|"
                        r"headed|parked|set off|got to|turned around|summited|continued|spent)\b", re.I)


def num(s):
    return float(s.replace(",", ""))


def distance_mi(text, title=""):
    # A distance in the title ("Celo Knob (7.3 miles)") is the hike's own;
    # in the body it needs a context word (round trip, total, loop...).
    for m in DIST.finditer(title or ""):
        v = num(m.group(1)) * (0.621371 if m.group(2).lower().startswith("k") else 1)
        if 0.2 <= v <= 300:
            return round(v, 1)
    for m in DIST.finditer(text):
        ctx = text[max(0, m.start() - 60): m.end() + 40]
        if not DIST_CTX.search(ctx):
            continue
        v = num(m.group(1))
        if m.group(2).lower().startswith("k"):
            v *= 0.621371
        if 0.2 <= v <= 300:
            return round(v, 1)
    return None


def gain_ft(text):
    for rx in (GAIN, GAIN2):
        m = rx.search(text)
        if m:
            v = num(m.group(1))
            unit = m.group(2).lower()
            if unit.startswith("m"):
                v *= 3.28084
            if 50 <= v <= 30000:
                return int(round(v, -1))
    return None


def page_date(url, text):
    m = URL_DATE.search(url)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            d = int(m.group(3)) if m.group(3) else None
            return f"{y:04d}-{mo:02d}" + (f"-{d:02d}" if d and 1 <= d <= 31 else "")
    m = TEXT_DATE.search(text)
    if m:
        return f"{int(m.group(3)):04d}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    return None


def page_type(title, text, n_features):
    past = len(FIRST_PAST.findall(text))
    if past >= 4:   # a story told in the first person, even with a stats box
        return "trip_report"
    if len(FIELDS.findall(text)) >= 2:
        return "trail_guide"
    if LISTY.search(title or ""):
        return "list_article"
    if past >= 2:
        return "trip_report"
    if n_features == 0:
        return "general"
    return "article"


def analyse(page, names, parks):
    """First pass over a page: everything except the region."""
    text = page.get("text") or ""
    title = page.get("title") or ""
    full = title + "\n" + text
    found = mentions(full, names)
    ctx_states = Counter(STATES[m.group(1).lower()] for m in STATE_RE.finditer(full))
    for m in ABBR_RE.finditer(full):
        ctx_states[ABBR[m.group(1)]] += 1
    # "Mount Washington" is not a mention of Washington state.
    for key, c in found.items():
        for m in STATE_RE.finditer(key):
            ctx_states[STATES[m.group(1).lower()]] -= c
    ctx_states = Counter({k: v for k, v in ctx_states.items() if v > 0})
    local = single_word_names(title, text, found, ctx_states)
    for k in local:
        found[k] = max(1, len(re.findall(r"\b" + re.escape(k.capitalize()) + r"\b", full)))
    rec = {
        "url": page["url"], "site": page.get("row", {}).get("site"), "title": title,
        "page_type": None, "location": None, "features": [],
        "distance_mi": distance_mi(text, title), "gain_ft": gain_ft(text), "date": page_date(page["url"], text),
        "words": page.get("words"),
        "_local_names": local,
        "_title_keys": set(mentions(title, ChainMap(local, names))),
    }
    return rec, found, ctx_states, (title, text)


def single_word_names(title, text, found, ctx_states):
    """One-word peaks, lakes and falls named in the title (\"Hiking Moosilauke\").

    Accepted only when the word is capitalised in the title, never appears
    in lower case in the text (so it reads as a proper noun, not \"table\" or
    \"sawtooth\"), is not part of a longer name already matched, and has a
    candidate in a state the page names. Returns {word: candidates}.
    """
    out = {}
    if not ctx_states:
        return out
    covered = {t for k in found for t in k.split()}
    for m in re.finditer(r"\b[A-Z][a-z]{3,}\b", title or ""):
        w = m.group(0)
        k = w.lower()
        before = re.findall(r"[\w.']+", title[:m.start()])
        after = title[m.end():].lstrip()
        if before and before[-1][0].isupper() and before[-1].rstrip(".").lower() not in SINGLE_OK_BEFORE:
            continue  # "Chris Townsend"
        if re.match(r"(,|[A-Z]{2}\b|[A-Z][a-z])", after):
            continue  # "Ogden, Utah", "Morrison CO", "Taylor Swift"
        if k not in SINGLE or k in covered or re.search(r"\b" + k + r"\b", text):
            continue
        cands = [f for f in SINGLE[k] if f["state"] in ctx_states]
        if cands:
            out[k] = cands
    return out


def feature(f, mentions_n, located=True):
    out = {"name": f["name"], "kind": f["kind"], "osm_id": f["osm_id"], "mentions": mentions_n,
           "lat": round(f["lat"], 5), "lon": round(f["lon"], 5), "park": f["park"], "state": f["state"]}
    if not located:  # a long trail: no single location
        out.update(osm_id=None, lat=None, lon=None, park=None, state=None, long_trail=True)
    if f["ele_m"]:
        out["ele_m"] = f["ele_m"]
    if f["km"]:
        out["km"] = f["km"]
    return out


def finish(rec, found, ctx_states, title, text, names, parks, long, boiler=()):
    """Second pass: drop site boilerplate, resolve the region, type the page."""
    names = ChainMap(rec.pop("_local_names"), names)
    title_keys = rec.pop("_title_keys")
    found = Counter({k: v for k, v in found.items() if k not in boiler})
    ctx_parks = {k for k in found if k in parks}
    # Votes for the region: mentions, plus 3 for a name in the title, so
    # the title's own lake beats a lake mentioned in passing.
    local = Counter({k: 1 if k in WEAK_ALIASES else v + (3 if k in title_keys else 0)
                     for k, v in found.items() if k not in long})
    kept, centre = resolve(local, names, ctx_states, ctx_parks)
    feats = [feature(f, found[k]) for k, f in kept]
    if kept or ctx_states:
        feats += [feature(max(names[k], key=lambda f: f["qrank"]), found[k], located=False)
                  for k in found if k in long]
    feats.sort(key=lambda f: (-f["mentions"], f["name"]))
    if kept:
        states = Counter(f["state"] for _, f in kept if f["state"])
        pk = Counter(f["park"] for _, f in kept if f["park"])
        rec["location"] = {"lat": round(centre[0], 4), "lon": round(centre[1], 4),
                           "state": states.most_common(1)[0][0] if states else None,
                           "park": pk.most_common(1)[0][0] if pk else None}
    elif ctx_states and any(k in long for k in found):
        # e.g. a thru-hike post: no local features, but the page names a state
        rec["location"] = {"lat": None, "lon": None, "state": ctx_states.most_common(1)[0][0], "park": None}
    rec["features"] = feats[:25]
    # Page type from the trained model (scripts/page_type.py); the old rules
    # (rule_type) are kept for comparison.
    ptype, probs = type_model.predict(TYPE_MODEL, type_model.features(title, text, rec["url"], len(feats)))
    # The model, trained on 300 pages, often calls a one-hike page "other",
    # a list or an area guide by a small margin ("Hiking Mount Cabot", "Mt
    # Cammerer on the Appalachian Trail"). When the title names a feature the
    # page resolved to and the two hike types together are likely enough,
    # take the likelier of them.
    hike_p = probs["trip_report"] + probs["trail_guide"]
    if (ptype not in HIKE_TYPES and hike_p >= TITLE_HIKE_P and not NOT_ONE_HIKE.search(title)
            and any(k in title_keys and f["kind"] in TITLE_HIKE_KINDS for k, f in kept)):
        ptype = max(HIKE_TYPES, key=probs.get)
        rec["type_rule"] = "title_hike"
    rec["page_type"] = ptype
    rec["page_type_p"] = round(probs[ptype], 3)
    rec["rule_type"] = page_type(title, text, len(feats))
    if ptype in HIKE_TYPES:
        rec["hike"] = hike(rec, title, text, kept, found, names, long)
    return rec


HIKE_TYPES = ("trip_report", "trail_guide")
TITLE_HIKE_P = 0.3
TITLE_HIKE_KINDS = {"trail", "hike", "peak", "lake", "waterfall", "cliff"}
# Titles about many hikes or a whole area, even when they name one feature.
NOT_ONE_HIKE = re.compile(r"(^\s*\d{1,2}\s)|\b(best|top \d+|\d{1,2} (?:best|great|easy|amazing|epic|awesome|"
                          r"favorite|beautiful|scenic|hikes|trails|waterfalls|things|ways|reasons|tips|days)|"
                          r"hikes|trails (?:in|near|of|around)|things to do|where to stay|itinerary|"
                          r"days? in|weekend in|camping|campgrounds?|review|gear|news|events?|calendar|books?|guidebooks?|"
                          r"maps?|topo|free photos|shot|killed|rescued?|missing|for sale)\b"
                          r"|\?(?:\s|$)", re.I)   # forum questions: "Onion Valley to Mount Gould?"
PARK_SUFFIX = re.compile(r"\s*(?:(?:state|provincial|national|regional|county|city)\s+)?"
                         r"(?:park|forest|preserve|recreation area|natural area|conservation area|"
                         r"public reserved land|sanctuary|reserve|wildlife area)$")
PARK_WORDS = {"beach", "nature", "center", "centre", "area", "woods", "land", "public", "reserved", "wildlife",
              "conservation", "community", "memorial", "city", "town", "county", "state"}
BIG_PARK = re.compile(r"\b(national|wilderness|provincial park)\b")
DESTINATION_KINDS = ("peak", "lake", "waterfall", "cliff")
TITLE_SPLIT = re.compile(r"\s+[|•·]\s+|\s+[-–—]\s+(?=[^-–—]*$)")
DIFFICULTY = re.compile(r"\bdifficulty\s*(?:rating|level)?\s*:?\s*(easy|moderate|strenuous|difficult|hard|"
                        r"challenging|very difficult|extreme)\b", re.I)
ROUTE = re.compile(r"\b(loop|out[- ]and[- ]back|point[- ]to[- ]point|lollipop|shuttle|thru[- ]hike)\b", re.I)


def hike_name(title, site=""):
    """The title minus the site's name (\"Bald Mountain | Our Adirondacks\",
    \"Wikiloc | Confluence Trail Loop\")."""
    parts = [p.strip() for p in TITLE_SPLIT.split(title or "") if p.strip()]
    stem = re.sub(r"[^a-z0-9]", "", (site or "").split(".")[0].lower())
    if stem and len(parts) > 1:
        parts = [p for p in parts if stem not in re.sub(r"[^a-z0-9]", "", p.lower())] or parts
    name = parts[0] if parts else (title or "")
    name = re.sub(r"\bTrail Trail$", "Trail", name)  # Wikiloc appends "Trail" to every title
    return name.strip()[:150]


def hike(rec, title, text, kept, found, names, long):
    """One hike per trip report or trail guide: its destination, trails and stats.

    Features named in the title come first: \"Celo Knob & Gibbs Mountain via
    Woody Ridge Trail\" -> destinations Celo Knob, Gibbs Mountain; trail Woody
    Ridge Trail. Without a title match, the page's most-mentioned peak, lake
    or waterfall (>= 2 mentions) is the destination.
    """
    by_key = dict(kept)
    in_title = mentions(title or "", names)
    pick = lambda f: {"name": f["name"], "kind": f["kind"], "osm_id": f["osm_id"]}
    dests = [pick(by_key[k]) for k in in_title if k in by_key and by_key[k]["kind"] in DESTINATION_KINDS]
    trails = [pick(by_key[k]) for k in in_title if k in by_key and by_key[k]["kind"] in ("trail", "hike")]
    trails += [{"name": max(names[k], key=lambda f: f["qrank"])["name"], "kind": "long_trail", "osm_id": None}
               for k in in_title if k in long]
    # "Crowders Mountain State Park" in the title: the peak Crowders Mountain.
    park_keys = [k for k in in_title if k in by_key and by_key[k]["kind"] == "park"]
    for k in park_keys:
        base = PARK_SUFFIX.sub("", k)
        f = by_key.get(base)
        if base != k and f and f["kind"] in DESTINATION_KINDS and pick(f) not in dests:
            dests.append(pick(f))
    if not dests:
        cands = sorted(((found[k], f) for k, f in kept if f["kind"] in DESTINATION_KINDS and found[k] >= 2),
                       key=lambda t: -t[0])
        dests = [pick(cands[0][1])] if cands else []
    if not trails:
        cands = sorted(((found[k], f) for k, f in kept if f["kind"] in ("trail", "hike") and found[k] >= 2),
                       key=lambda t: -t[0])
        trails = [pick(cands[0][1])] if cands else []
    # A small park or preserve named in the title is the hike itself
    # ("Barrett Preserve", "Otis Farm Bird Sanctuary"); big, well-known
    # parks hold many different hikes.
    parks = [] if (dests or trails) else [pick(by_key[k]) for k in park_keys
                                         if by_key[k]["qrank"] < 1000 and not BIG_PARK.search(k)
                                         and set(PARK_SUFFIX.sub("", k).split()) - GENERIC - PARK_WORDS]
    d = DIFFICULTY.search(text)
    r = ROUTE.search(title + " " + text[:3000])
    loc = rec["location"] or {}
    return {
        "name": hike_name(title, rec.get("site")),
        "destinations": dests[:4],
        "trails": trails[:4],
        "parks": parks[:1],
        "park": loc.get("park"),
        "state": loc.get("state"),
        "distance_mi": rec["distance_mi"],
        "gain_ft": rec["gain_ft"],
        "route": r.group(1).lower().replace(" ", "-") if r else None,
        "difficulty": d.group(1).lower() if d else None,
        "date": rec["date"] if rec["page_type"] == "trip_report" else None,
    }


def boilerplate(founds, min_pages=10, share=0.5):
    """Names on at least `share` of a site's pages are navigation, not content."""
    if len(founds) < min_pages:
        return set()
    df = Counter(k for f in founds for k in f)
    return {k for k, c in df.items() if c >= share * len(founds)}


def extract(page, names, parks, long=frozenset()):
    """One page on its own (no boilerplate removal)."""
    rec, found, ctx, (title, text) = analyse(page, names, parks)
    return finish(rec, found, ctx, title, text, names, parks, long)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", required=True, help="Hiker Atlas autosuggest.db")
    ap.add_argument("--pages", nargs="+", required=True, help="warcfetch outputs (.jsonl[.gz]; globs allowed)")
    ap.add_argument("--keep", help="pages.tsv.gz from page_pass.py: only extract these URLs")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    names, parks, long = load_index(args.index)
    print(f"index: {len(names):,} names, {len(parks):,} park names, {len(long):,} long trails", file=sys.stderr)
    keep = None
    if args.keep:
        with gzip.open(args.keep, "rt") as f:
            keep = {r["url"] for r in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)}
    n = 0
    stats = Counter()
    with gzip.open(args.out, "wt") as out:
        for path in sorted({p for pat in args.pages for p in glob.glob(pat)}):
            # Pass 1: analyse every page, grouped by site. Each site's pages
            # sit in one page-pass partition, so per-file grouping is complete.
            by_site = defaultdict(list)
            op = gzip.open if path.endswith(".gz") else open
            with op(path, "rt") as f:
                for line in f:
                    page = json.loads(line)
                    if page.get("error") or (keep is not None and page["url"] not in keep):
                        continue
                    rec, found, ctx, tt = analyse(page, names, parks)
                    by_site[rec["site"]].append((rec, found, ctx, tt))
            # Pass 2: drop each site's boilerplate names, resolve, write.
            for site, items in by_site.items():
                boiler = boilerplate([it[1] for it in items])
                stats["boilerplate names"] += len(boiler)
                for rec, found, ctx, (title, text) in items:
                    r = finish(rec, found, ctx, title, text, names, parks, long, boiler)
                    out.write(json.dumps(r, ensure_ascii=False) + "\n")
                    n += 1
                    stats[r["page_type"]] += 1
                    stats["located"] += r["location"] is not None
                    stats["distance"] += r["distance_mi"] is not None
                    stats["gain"] += r["gain_ft"] is not None
    print(f"{n:,} pages; " + ", ".join(f"{k} {v:,}" for k, v in stats.most_common()), file=sys.stderr)


if __name__ == "__main__":
    main()
