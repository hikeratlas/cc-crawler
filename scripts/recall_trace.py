"""Rare-hike recall test: trace known trip reports through every pipeline stage.

Ground truth is data/eval/recall/reports.tsv: web-searched trip reports and
guides for the 33 lesser-known hikes in data/eval/recall/rare_hikes.tsv. For
each URL this reports the first stage that lost it (docs/DESIGN.md, "Recall on
rare hikes"). Inputs, all in one working directory (argv[1]):

  hikes.tsv, truth.tsv     copies of the two files above
  cdx14.tsv                cmd/urlcdx output for the truth URLs (harvest crawls)
  stages/graph-seeds-*.seeds.tsv.gz, stages/su*.sites.tsv.gz
  stages/su-cand-truth.tsv.gz   site-urls candidate rows, prefixed with the run
  stages/site-content-run-*.cand.tsv.gz, stages/page-pass-run-*.tsv.gz
  ../ss/slug_sites.tsv.gz  slug-scan sites

argv[2] holds the extraction: in/*/extracted-*.jsonl.gz and out/hikes.tsv.gz
(scripts/hikes.py output). Writes trace.tsv and prints counts per stage.
"""
"""Trace each ground-truth URL through the pipeline stages; first failing stage = cause."""
import csv, gzip, glob, json, sys, re, collections
csv.field_size_limit(10**9)
R = sys.argv[1]            # recall dir
C14 = sys.argv[2]          # local combined extract dir (in/, out/)
S = R + "/stages"

from urllib.parse import parse_qsl
def key(u):
    u = u.strip().lower().split("#")[0]
    q = ""
    if "?" in u:
        u, qs = u.split("?", 1)
        args = sorted((k, v) for k, v in parse_qsl(qs, keep_blank_values=True)
                      if k not in ("m", "amp") and not k.startswith("utm_"))
        q = "?" + "&".join(f"{k}={v}" for k, v in args) if args else ""
    u = re.sub(r"^https?://", "", u); u = re.sub(r"^www\.", "", u); u = re.sub(r"/index\.(html?|php)$", "", u)
    return u.rstrip("/") + q
def host(u): return key(u).split("/")[0]
def tsv(p): return csv.DictReader(gzip.open(p, "rt"), delimiter="\t", quoting=csv.QUOTE_NONE)

hikes = {r["id"]: r for r in csv.DictReader(open(R + "/hikes.tsv"), delimiter="\t")}
truth = list(csv.DictReader(open(R + "/truth.tsv"), delimiter="\t"))

# Stage 0: in Common Crawl (14 harvest crawls)
cc = collections.defaultdict(list)
for r in csv.DictReader(open(R + "/cdx14.tsv"), delimiter="\t"):
    if r["found"] == "true" and r["status"] == "200":
        cc[key(r["url"])].append(r["crawl"])
cc_any = {key(r["url"]) for r in csv.DictReader(open(R + "/cdx14.tsv"), delimiter="\t") if r["found"] == "true"}

# Sites: seeds (graph + slug scan) and site-urls verdicts
seeds = set()
for f in glob.glob(S + "/graph-seeds-*.seeds.tsv.gz"):
    seeds |= {r["domain"] for r in tsv(f)}
slug = {r["site"]: float(r["score"] or 0) for r in tsv(R + "/../ss/slug_sites.tsv.gz")}
verdict = collections.defaultdict(set)
for f in glob.glob(S + "/su*.sites.tsv.gz"):
    for r in tsv(f): verdict[r["site"]].add(r["verdict"])
known_sites = seeds | set(slug) | set(verdict)
def site_of(u):
    h = host(u).split(":")[0]; parts = h.split(".")
    for i in range(len(parts) - 1):
        s = ".".join(parts[i:])
        if s in known_sites: return s
    return ".".join(parts[-2:])

su_cand = {key(l.split("\t")[1]) for l in gzip.open(S + "/su-cand-truth.tsv.gz", "rt")}
sc_cand, sc8 = set(), set()
for f in glob.glob(S + "/site-content-run-*.cand.tsv.gz"):
    ks = {key(l.split("\t")[0]) for l in gzip.open(f, "rt")}
    (sc8 if "run-8" in f else sc_cand).update(ks)
pp = {}
for f in glob.glob(S + "/page-pass-run-*.tsv.gz"):
    for r in tsv(f): pp[key(r["url"])] = r
ext = {}
for f in glob.glob(C14 + "/in/*/extracted-*.jsonl.gz"):
    for line in gzip.open(f, "rt"):
        r = json.loads(line); ext[key(r["url"])] = r
table = collections.defaultdict(list)
for r in tsv(C14 + "/out/hikes.tsv.gz"): table[key(r["url"])].append(r)

def inbox(row, h):
    try: la, lo = float(row["lat"]), float(row["lon"])
    except ValueError: return False
    rad = float(h["radius_deg"])
    return abs(la - float(h["lat"])) <= rad and abs(lo - float(h["lon"])) <= rad * 1.6

out = []
for t in truth:
    k, h = key(t["url"]), hikes[t["id"]]; s = site_of(t["url"])
    rec = {"id": t["id"], "kind": t["kind"], "url": t["url"], "site": s, "crawls": len(cc.get(k, []))}
    if k not in cc:
        stage = "not in CC (14 crawls)" if k not in cc_any else "in CC only as redirect/error"
    elif s not in known_sites:
        stage = "site never seeded"
    elif not (verdict[s] & {"hiking", "mixed", "drifted"}) and k not in su_cand:
        stage = "site judged " + "/".join(sorted(verdict[s])) if verdict[s] else "seeded, never harvested (below cut-off)"
    elif k not in su_cand:
        stage = "page not in harvested captures"
    elif k not in sc_cand and k not in pp:
        stage = "pending (site-content-run-8)" if k in sc8 else "dropped at site-content (URL rules / site judged)"
    elif k not in pp:
        stage = "dropped by page model (relevance < 0.3)"
    elif k not in ext or not ext[k].get("hike"):
        e = ext.get(k, {})
        stage = f"extraction: page type {e.get('page_type', '?')}" if e else "extraction: missing"
    elif k not in table:
        stage = "hikes.py: no location / destination, or filtered"
    elif not any(inbox(r, h) for r in table[k]):
        r0 = table[k][0]
        stage = "in table, WRONG place"; rec["got"] = f"{r0['destinations'] or r0['trails']} ({r0['state']})"
    else:
        stage = "FOUND"
    rec["stage"] = stage; out.append(rec)

with open(R + "/trace.tsv", "w") as f:
    w = csv.DictWriter(f, ["id", "kind", "stage", "site", "crawls", "got", "url"], delimiter="\t", extrasaction="ignore")
    w.writeheader(); w.writerows(out)
c = collections.Counter(r["stage"] for r in out)
print(f"{len(out)} truth URLs")
for s, n in c.most_common(): print(f"{n:4d}  {s}")
