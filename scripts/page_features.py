"""Text features for scoring whether a fetched page is about hiking in the
US/Canada. Shared by scripts/train_page_model.py and scripts/site_content.py.
Stdlib only."""
import math
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
GAZ = os.path.join(HERE, "..", "data", "gazetteer")


def _terms(name):
    with open(os.path.join(GAZ, name)) as f:
        return [l.strip().lower() for l in f if l.strip() and not l.startswith("#")]


_places = _terms("regions.txt") + _terms("national_parks.txt") + _terms("areas.txt") + [
    "national park", "national forest", "state park", "provincial park", "wilderness area",
    "national monument", "national recreation area", "state forest", "conservation area"]
GEO = re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(set(_places), key=len, reverse=True)) + r")\b")
ABBR = re.compile(r"(?:,\s*|\b)(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|"
                  r"NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|BC|AB|SK|MB|ON|QC|NB|NS|PE|NL|YT|NT|NU)\b")
HIKE = re.compile(r"\b(hik(?:e|es|ed|ing|ers?)|trail(?:s|head|heads)?|summit(?:s|ed)?|switchbacks?|elevation gain|"
                  r"round[- ]trip|out[- ]and[- ]back|loop|backpack(?:ing|ers?)?|scrambl\w*|ridge(?:line)?|peaks?|"
                  r"waterfalls?|falls|campsite|wilderness|overlook|viewpoint|trek(?:king)?|snowshoe\w*|thru[- ]hik\w*)\b")
DIST = re.compile(r"\b\d+(?:\.\d+)?\s*(?:miles?|mi)\b|\b\d[\d,]*\s*(?:feet|ft)\b")
NONUS = re.compile(r"\b(km|kilomet(?:re|er)s|switzerland|swiss|italy|italian|france|french alps|germany|spain|portugal|"
                   r"norway|sweden|finland|denmark|scotland|england|wales|ireland|uk|australia|tasmania|new zealand|"
                   r"nepal|himalaya\w*|peru|patagonia|chile|argentina|japan|slovenia|austria|iceland|pakistan|"
                   r"tanzania|kenya|morocco|india|europe|asia|africa|south america)\b")
COMMERCE = re.compile(r"\b(add to cart|in stock|free shipping|checkout|bedrooms?|bathrooms?|sq\.? ?ft|mls|"
                      r"price per (?:acre|night)|per night|book now|lot size|subscribe and save|sku)\b")
OTHER = re.compile(r"\b(mountain bik\w*|mtb|bikes?|biking|cycling|ski(?:ing|er|ers)?|snowboard\w*|lift tickets?|"
                    r"rv|campground|hookups?|fishing|anglers?|hunting|hunters?|golf|kayak\w*|atv|ohv|4x4|"
                    r"jeep|horseback|equestrian|climbing route)\b")
FIRST = re.compile(r"\b(i|we|my|our|me|us)\b")
SINGULAR = re.compile(r"\b(i|my|me)\b")
AFFILIATE = re.compile(r"\b(affiliate links?|earn (?:a )?(?:small )?commission|as an amazon associate)\b")
BUSINESS = re.compile(r"\b(book now|our team|our guides|guided|tours?|reservations?|inc\.?|llc|company|call us|contact us|"
                      r"pricing|rates|members?hip|donate|staff|visitors? center)\b")


def features(page):
    """page: dict with title, description, text, words (cmd/warcfetch output)."""
    title = (page.get("title") or "").lower()
    raw = (page.get("title") or "") + " " + (page.get("description") or "") + " " + (page.get("text") or "")
    t = raw.lower()
    n = max(1, len(t.split()))
    words = page.get("words") or n
    hike = len(HIKE.findall(t))
    geo = len(GEO.findall(t)) + len(ABBR.findall(raw))
    return {
        "log_words": math.log1p(words),
        "short": 1.0 if words < 150 else 0.0,
        "hike_density": min(10.0, 100.0 * hike / n),
        "log_hike": math.log1p(hike),
        "title_hike": 1.0 if HIKE.search(title) else 0.0,
        "log_geo": math.log1p(geo),
        "log_nonus": math.log1p(len(NONUS.findall(t))),
        "log_commerce": math.log1p(len(COMMERCE.findall(t))),
        "other_density": min(10.0, 100.0 * len(OTHER.findall(t)) / n),
        "first_person": min(10.0, 100.0 * len(FIRST.findall(t)) / n),
        "log_dist": math.log1p(len(DIST.findall(t))),
    }


def kind_features(page):
    """Features for whether a page reads as written by an individual (personal
    blog) rather than an organisation or business."""
    t = ((page.get("title") or "") + " " + (page.get("text") or "")).lower()
    n = max(1, len(t.split()))
    return {
        "first_person": min(10.0, 100.0 * len(FIRST.findall(t)) / n),
        "singular": min(10.0, 100.0 * len(SINGULAR.findall(t)) / n),
        "log_affiliate": math.log1p(len(AFFILIATE.findall(t))),
        "log_business": math.log1p(len(BUSINESS.findall(t))),
        "log_commerce": math.log1p(len(COMMERCE.findall(t))),
        "log_words": math.log1p(n),
    }


FEATURES = {"relevant": features, "personal": kind_features}


def score(page, model):
    """Model probability for a page (relevant, or personal, depending on the
    model's target), from a train_page_model.py model."""
    f = FEATURES[model.get("target", "relevant")](page)
    z = model["bias"] + sum(model["weights"][k] * (f[k] - model["mean"][k]) / model["std"][k] for k in model["weights"])
    return 1.0 / (1.0 + math.exp(-z))
