# cc-crawler design

> **Living document.** Update it as stages land, assumptions get measured, and
> decisions change. Record notable changes in the [Decision log](#decision-log).

## Goal

Use [Common Crawl](https://commoncrawl.org) to find **English-language blog
posts** about hiking in **the US and Canada**. That means posts that talk about:

- hiking trails,
- the landmark destinations on them (summits, lakes, waterfalls, viewpoints, huts…),
- national parks (and, in practice, state/provincial parks and other protected areas).

The **first deliverable is a URL list**: candidate blog domains plus the
candidate post URLs on them, with enough metadata to re-fetch each page from
Common Crawl later (`warc_filename`, `warc_record_offset`, `warc_record_length`).
Richer extraction (text, trail and place mentions, geocoding, joining to OSM)
comes later.

### Out of scope for now

- **Official pages** (NPS, state/provincial park sites, agencies). We expect to
  get these from other signals, such as OSM `website=` tags and Wikidata.
- Non-English content, and places outside the US and Canada.
- Anything that needs an LLM budget. Classification is heuristic or uses
  small models trained locally.

## Constraints

| Constraint | Consequence |
|---|---|
| Very cost-conscious | No bulk WARC processing. No paid compute (Athena, EMR). |
| Runs in **GitHub Actions**, public repo | Standard runners are free and unlimited: 4 vCPU, 16 GB RAM, ~14 GB disk, 6 h per job, ~20 concurrent jobs. Stream big files instead of storing them. |
| Common Crawl throttles (HTTP 503 "slow down") | Low concurrency, exponential backoff, and a funnel that keeps the number of fetches small. |
| No LLM budget | Heuristics, gazetteers, and tiny locally trained classifiers (e.g. logistic regression or fastText on URL features). |

## Strategy: a funnel built on metadata

```
domain web graph ──► seed domains ──► URL index per domain ──► candidate post URLs
   (Stage 1–3)         (Stage 3)          (Stage 4)                (Stage 5)
                           ▲                                          │
                           └──────── snowball / new hubs (Stage 6) ◄──┘
                                       range-fetch samples for validation (Stage 7)
                                       WET sample for recall (Stage 8)
```

We only download page bodies at the very end, and only for small samples. The
work is done by three cheap Common Crawl datasets:

1. **Domain-level web graph.** "Domain A links to domain B", aggregated over
   three monthly crawls.
2. **Columnar URL index** (Parquet). One row per captured URL, sorted by SURT
   (reversed host). Because of the sort order, all URLs for one domain sit
   together and DuckDB can skip the rest of the index.
3. **WARC range requests.** `warc_filename + offset + length` fetches one record
   (~10–50 KB) without downloading the 1 GB WARC it lives in.

## Unit of identity: the *site*

Everything downstream is keyed on a **site**, not a registered domain:

- Normally, a site is the **registered domain** (`sectionhiker.com`).
- Under **blog-platform parents** (`blogspot.com`, `wordpress.com`,
  `github.io`, `wixsite.com`, `weebly.com`, `tumblr.com`, `typepad.com`, …), a
  site is the **host** (`1happyhiker.blogspot.com`).
- Later, possibly a **host + path prefix** for path-based platforms
  (`medium.com/@user`).

The platform parent list lives in one place:
[`data/platforms.csv`](../data/platforms.csv). A single function maps a URL or
host to its site. Graph stages and index stages all use it, so adding platform
blogs is a matter of feeding in more inputs, not reworking the schema.

The columnar index supports this directly. `url_host_name` (or a SURT prefix
such as `com,blogspot,1happyhiker)`) selects a single platform blog as cheaply
as `url_host_registered_domain` selects a domain.

---

## Stage 0: Reference data and evaluation set

Before building anything clever, create a way to tell whether it works.

- **[`data/hubs.csv`](../data/hubs.csv)**: hand-curated **hiking hub domains**.
  These are sites that hiking bloggers link to much more often than the general
  web does. Columns: `domain, category, weight, status, notes`.
  - `category`: `trail_db`, `peakbagging`, `mapping`, `conditions`,
    `permits_parks`, `trail_org`, plus `advocacy`, `other_activity` and
    `too_generic`.
  - `weight`: a hand-set starting point. It gets combined with a measured IDF
    (hub in-degree) in Stage 3.
  - `status`: `active` (used), `candidate` (needs a decision), or `excluded`
    (kept for the record and why).
  - **Hubs must be pay-level domains (PLDs).** `fs.usda.gov` becomes `usda.gov`
    and `parks.canada.ca` becomes `canada.ca` in the domain graph, which is far
    too generic. Those need the host graph. `pc.gc.ca` is fine because `gc.ca` is
    a public suffix.
- **[`data/eval/known_blogs.csv`](../data/eval/known_blogs.csv)**: hand-verified
  US/Canada hiking sites that should come out, magazines included. Used to
  measure seed recall. Columns: `domain, region, type, authorship, split,
  notes`. `authorship` (`personal`/`community`/`professional`) is the ground
  truth for `site_kind`. Each row is split `dev`/`holdout`; only
  tune thresholds against `dev`. `type` (`personal_blog`, `guide_site`,
  `multi_author`, `mixed_travel`, `magazine`) lets us report recall per kind of
  site.
- **[`data/eval/negatives.csv`](../data/eval/negatives.csv)**: domains that
  should *not* come out (retailers, gear reviews, aggregators, tourism boards,
  SEO listicles, expired domains), with a `reason`.
- **Gazetteers** for later stages. US/Canadian state and province names and
  abbreviations, plus multi-token park, summit and trail names from GNIS (US),
  CGNDB (Canada), and/or the hikeratlas OSM extracts.

`scripts/graph_presence.sh [release]` looks up every listed domain in a graph
release's ranks file and writes `data/reports/graph_presence_<release>.csv`
(harmonic rank, PageRank rank, host count; blank means not in the graph). Re-run
it whenever the lists change.

Lessons from drafting these lists (Nov 2026):

- **Expired blog domains are a real hazard.** `nobodyhikesinla.com` (a
  well-known LA hiking blog) and `hikeitbaby.com` now serve gambling spam. Old
  inlinks and old crawls still make them look like hiking sites. Later stages
  should check that each domain's *recent* captures still look on-topic. Older
  crawls are the place to get the good content.
- **Domain names are a poor signal on their own.** Scanning the ranks file
  for domains containing `hik`, `trail`, `summit` and so on returned ~29k
  domains in the 2026-jul-aug-sep graph. The top of that list is dominated by
  tech "summits", heritage and bourbon "trails", and Japanese words containing
  "hika"/"shika". It did turn up useful trail-organization hubs.
- **Many blogs block plain HTTP fetches** (Cloudflare, 403). That's one more
  reason to read page content from Common Crawl, not the live web.

**Done when:** the hub list and eval lists are committed and reviewed.

## Stage 1: Pick a graph release and resolve hub IDs

Web graph releases are listed in
[`graphinfo.json`](https://index.commoncrawl.org/graphinfo.json) and on the
[Web Graphs page](https://commoncrawl.org/web-graphs). Each release covers about
three crawls. Files live under
`https://data.commoncrawl.org/projects/hyperlinkgraph/<release>/domain/`.

What we've seen (Nov 2026):

| File | Format | Size (approx.) |
|---|---|---|
| `<release>-domain-vertices.txt.gz` | `id \t rev_domain \t num_hosts`, sorted by `rev_domain`, so ids are in that order (`0 aaa.11111 1`) | ~0.9–1 GB |
| `<release>-domain-edges.txt.gz` | `from_id \t to_id`, sorted by `from_id` | ~7–8.5 GB |
| `<release>-domain-ranks.txt.gz` | harmonic centrality and PageRank by `#host_rev` (a name, not an id), plus `#n_hosts` | ~2.4 GB |
| `<release>-domain-t.graph` | transposed BVGraph (inlinks), for the WebGraph Java library | ~3.7 GB |

That's about 135M domain nodes and 1.8B edges.

> ⚠️ The newest release (`cc-main-2026-aug-sep-oct`) returned **403** for its
> vertices file when we checked, while `cc-main-2026-jul-aug-sep` was complete.
> The pipeline must pick the newest release **whose required files all exist**.

**Steps**

1. Download (or stream) the vertices file once and look up the ids of
   everything in `hubs.csv`. Domains are stored reversed (`com.alltrails`).
2. Optionally, keep a filtered `id → rev_domain` table for US/CA-friendly or
   unknown TLDs. The full table is ~135M rows, about 1 GB as Parquet, which fits
   in the Actions cache or a release asset.

**Done when:** we have `hub_ids.parquet` for the chosen release.

## Stage 2: Stream the edges and score every domain against the hubs

One pass over the edges file, streamed straight from HTTPS
(`curl … | gzip -dc | tool`) with nothing written to disk. For each `from_id`,
compute:

- `hub_links`: the number of **distinct hubs** it links to, plus the sum of hub
  weights and the categories hit.
- `out_degree`: its total outlinks. Big portals link to everything, so we need
  this to normalize.

Because edges are sorted by `from_id`, the aggregates can be computed with
constant memory. This step also yields **hub in-degree** ("how many domains
link to alltrails.com"), which gives an IDF-style weight per hub.

**Implementation:** [`cmd/hubscore`](../cmd/hubscore/main.go), a single Go
binary with no dependencies.

1. Pass 1 streams the vertices and resolves hub and eval-domain ids.
2. Pass 2 streams the edges once. It relies on the `from_id` sort: one
   domain's edges are aggregated, then emitted. A dense `[]int8` id → hub
   table (~135 MB) keeps the per-edge cost to an array lookup.
3. Pass 3 streams the vertices again to attach names and host counts.

HTTPS reads resume from the current byte offset with a `Range` request if a
connection drops. Self-links are ignored. Rows are emitted for every domain
linking to ≥ 1 hub, plus every eval domain, even with zero hubs.

Run it with the [`graph-seeds`](../.github/workflows/graph-seeds.yml)
workflow (manual dispatch, Arm runners), or locally with
`go run ./cmd/hubscore -release <release>`. The workflow takes a list of
releases, scores each in its own matrix job (at most two at once), and merges
them in `scripts/seed_report.py`. A domain's hubs are the union across
releases, and the report shows per-release coverage of the eval lists.

Releases checked to exist with the same file layout (Nov 2026):
`cc-main-2026-jul-aug-sep`, `cc-main-2025-may-jun-jul`,
`cc-main-2024-may-jun-jul`, `cc-main-2023-may-sep-nov`,
`cc-main-2022-may-jun-aug`. These are the workflow's default set, one
per year.

**Output:** `domain_hub_scores.tsv.gz` with `id, domain, n_hosts, out_degree,
n_hubs, weight_sum, categories, hubs, in_eval`; `hub_stats.tsv` with each
hub's id and in-degree; and `meta.json`.

Write the aggregator so its inputs are generic: a vertices stream, an edges
stream, a `node → hub` map, and a `node → site` filter. Stage 2b then reuses
it unchanged.

### First results (cc-main-2026-jul-aug-sep, 2026-11-07)

Full report: [`data/reports/stage2_cc-main-2026-jul-aug-sep.md`](../data/reports/stage2_cc-main-2026-jul-aug-sep.md).

- **Fast:** 133M vertices and 2.15B edges in **~6 min end to end** on 4 vCPU
  / 16 GB (edge pass ~5 min). An earlier run took ~10 min because Common
  Crawl's CDN returned 403s (rate limiting) for ~5 min; the resuming reader
  waited them out.
- **Funnel:** 154k domains link to ≥ 1 hub; 35k to ≥ 2; 22k to ≥ 2 hubs in
  ≥ 2 categories; 3.8k to ≥ 5.
- **Top of the ranking looks right.** Thru-hiking blogs, regional hiking
  sites, trail guides and trip-report sites dominate the top 100.
- **What scores high that shouldn't:** gear makers (cottage gear brands such
  as tarptent.com), retailers (rei.com), gear-review sites (cleverhiker.com),
  and big user-content platforms (strava.com). Stage 3 needs a
  commerce/gear signal, probably from URL patterns in Stage 4 (`/product/`,
  `/shop/`, `/cart`).
- **Recall on known blogs is 52% at ≥ 2 hubs**, but the misses are mostly a
  *coverage* problem rather than a scoring one. 11 of 33 known blogs have
  **no outlinks at all** in this graph (plus the blogspot blog, which the
  domain graph can't represent). They're link targets only, meaning
  Common Crawl didn't fetch them in those three crawls. Two block CCBot in
  robots.txt (hikingguy.com, halfwayanywhere.com). Others may be blocked at
  the edge (e.g. Cloudflare's AI-bot blocking, which includes CCBot) or just
  not reached. Of the known blogs that *were* crawled, 17 of 21 (81%) pass
  ≥ 2 hubs.
- **Implication:** union several graph releases, including **older ones**
  from before AI-crawler blocking became common. Stage 4 should look for
  each seed's captures in older crawls too.
- nps.gov has by far the highest in-degree (101k linking domains). Its idf
  already discounts it, but it is the main source of single-hub noise.

### Union of five releases, 2022–2026 (graph-seeds run 2, 2026-11-07)

Full report: [`data/reports/stage2_union_2022-2026.md`](../data/reports/stage2_union_2022-2026.md).
Outputs: the `graph-seeds-run-2` GitHub release. The run took 17 min on Arm
runners, two releases at a time, ~6.5–9 min each.

| release | domains ≥ 2 hubs | known blogs crawled | known blogs ≥ 2 hubs |
|---|---:|---:|---:|
| 2026-jul-aug-sep | 35,034 | 21/33 | 17/33 |
| 2025-may-jun-jul | 34,589 | 25/33 | 23/33 |
| 2024-may-jun-jul | 40,170 | 26/33 | 25/33 |
| 2023-may-sep-nov | 42,411 | 28/33 | 28/33 |
| 2022-may-jun-aug | 41,659 | 28/33 | 28/33 |
| **union** | **88,780** | **30/33** | **29/33 (88%)** |

- **Older releases fix most of the coverage gap.** Recall goes from 52% (one
  release) to 88% (union), and the dev and holdout splits agree.
  Section Hiker, Modern Hiker, Hiking Guy, SoCal Hiker and Hikespeak were all
  crawled in 2022–2024 and then went `nc` (not crawled) in 2025–2026. That
  fits sites starting to block CCBot.
- **Crawl depth for big sites also fell.** backpacker.com linked to 37–38
  hubs in 2022–2023 but only 0–2 in 2024–2026; outsideonline.com went 44–45
  → 2–6. Common Crawl probably fetched far fewer of their pages recently.
  **Stage 4 should look across older crawls, not just the newest.**
- Still missing: climb-utah.com and thehikinglife.com (never crawled in any
  release), kentuckyhiker.com (crawled, no hub links), and the blogspot blog
  (needs Stage 2b).
- **The cost of the union is noise.** 88.8k domains have ≥ 2 hubs, against
  35k from one release. Negatives rank higher too, because every domain
  accumulates hubs across releases (theoutbound.com #27, rei.com #44). Expired
  blogs come back via their old crawls (nobodyhikesinla.com #751). Stage 4 must
  check what a site's pages are (commerce, listicles) and whether its
  **recent** captures are still on-topic.
- Hub domains are now excluded from the seed ranking (they were ranking as
  seeds themselves, e.g. hikingproject.com).

## Stage 2b: Host-graph pass for platform blogs

Same scoring as Stage 2, on the **host-level graph** (2026-aug-sep-oct:
~243M hosts, ~2.4B edges; 48 vertices files totalling ~1.7 GiB and 192 edges
files totalling ~10.9 GiB).

1. Stream the host vertices once and keep two id sets:
   - **hub hosts**: every host whose registered domain is in `hubs.csv`
     (`www.alltrails.com`, `alltrails.com`, `m.alltrails.com`, …), mapped back
     to its hub;
   - **platform hosts**: every host under a platform parent (blogspot alone is
     ~4M hosts).
   Both fit comfortably in memory as integer sets.
2. Stream the edges, keeping only edges from platform hosts to hub hosts, and
   aggregate per source host as in Stage 2. The edges are split into 192
   files, so this can be a matrix (e.g. 8 jobs × 24 files) followed by a
   merge.
3. Feed the results into Stage 3 alongside the domain-level scores, with
   `site = host`.

Cost is roughly 1.5× the domain pass, spread across a matrix. Doing it later is
cheap provided the site abstraction above is in place from the start.

**Implementation (2026-11-08):** `hubscore -graph host` (`cmd/hubscore/host.go`),
run with the `graph-seeds` workflow's `graph = host` input. It reads the
vertex and edge parts listed in `<release>-host-{vertices,edges}.paths.gz`,
one job per release (no matrix: edges are filtered to platform sources with
a bitset, so the pass is bound by download). Platforms come from
`data/platforms.csv`. Output has the domain-mode columns (`domain` = host,
`n_hosts` = 1), so `seed_report.py` ranks it unchanged, and `site-urls`
takes the resulting `graph-seeds-run-<n>` release as before: `sitecdx`
matches sites by SURT host prefix, so `foo.blogspot.com` is looked up as its
own site. Hub in-degrees count linking platform hosts only.

**First run (`graph-seeds-run-3`, five releases, ~10 min each):** 2,976
platform blogs link to ≥ 2 hubs (blogspot 1,936, wordpress.com 671, substack
99, typepad 92, others 178). The top of the ranking is almost all personal
hiking blogs (distancebackpacker, pacificnwseasons, jansjaunts,
stuebysoutdoorjournal); the 2-hub tail is noisy (nps.gov plus one hub on
weaving, politics and SEO blogs). The Stage 4 classify pass
(`site-urls-run-7`, crawls 2026-34, 2023-40, 2020-50) kept only 184: many
blogs have < 5 captures in recent crawls (`thin`), and diary-style URLs
(`/2015/06/day-12.html`) rarely name the hike, so good blogs read as
`off_topic` (runsuerun, mdk10outside, ucmountaineering). `site-urls` gained
a `mode = posts` input for this batch: every English blog-post URL is a
candidate, on `off_topic` sites too, and the content stages (site-content,
page pass) decide. Crawls before 2018-39 have no language field, so the
oldest crawl worth adding is 2018-51.

Baseline before this pass: the hike table had 20 blogspot sites (454 hikes),
one wixsite and **no** wordpress.com blogs, all found by the slug scan.

## Stage 3: Turn hub scores into seed domains

Start with a simple, explainable score and refine it against the eval set:

```
score = Σ_hubs weight(h) · idf(h)          # rewards linking to several specific hubs
        × specificity(out_degree)          # penalises domains that link everywhere
```

Filters:

- `hub_links ≥ 2` distinct hubs, ideally in ≥ 2 categories, e.g. a trail DB plus
  a permit or park site.
- Drop huge domains using `num_hosts` and the ranks file: top-N harmonic
  centrality, news, social, and retail.
- TLD prior: favour `.com/.org/.net/.ca/.us/.blog/…`. Drop obviously non-English
  ccTLDs.
- Apply `negatives.csv`.

**Classify the site** (`site_kind`) so that output can be curated for a
diverse mix of voices. Professional content counts. It's just labelled.

- `personal`: individual bloggers and trip-report sites;
- `community`: multi-author or user-submitted (The Trek, forums);
- `professional`: magazines, media, commercial guide publishers.

Heuristics to start, no LLM: the curated lists, graph prominence (harmonic
rank and `num_hosts`; magazines sit far higher than personal blogs), number
of distinct authors and bylines (Stage 4/7), and ad and affiliate density
(Stage 7). Treat it as a soft label with a confidence.

**Blog platforms need special handling.** The domain graph aggregates hosts
into pay-level domains using the [public suffix list](https://publicsuffix.org),
but **only its ICANN section**. In practice, every hosted-blog platform
collapses into a single node:

| Platform | Domain-graph node (2026-jul-aug-sep) |
|---|---|
| `*.blogspot.com` | **one** node, `com.blogspot`, with ~4.0M hosts. Verified: no per-blog entries in the vertices file, even though blogspot is in the PSL's *private* section. |
| `*.github.io`, `*.wixsite.com` | Expected to collapse the same way (also private-section suffixes). Not yet verified. |
| `wordpress.com`, `medium.com`, `substack.com`, `squarespace.com`, `weebly.com`, `tumblr.com`, `typepad.com` | Not public suffixes at all, so each is one node. |

Consequences:

- Blogs on their **own domain** (most established hiking blogs) work in the
  domain graph.
- **Platform-hosted blogs need the host-level graph.** See Stage 2b. They are
  probably where much of the grassroots, small-audience content lives.
- Medium also puts blogs in paths (`medium.com/@user`), which neither graph can
  separate. Defer those to Stage 4's URL-level matching.
- `data/eval/known_blogs.csv` includes one blogspot blog
  (`1happyhiker.blogspot.com`), which exists to catch this case.

**Output:** `seeds.parquet` (ranked domains with their score components) and a
per-run report of the precision and recall proxies against `known_blogs.csv`
and `negatives.csv`.

**Done when:** recall on the held-out known blogs is acceptable (target TBD,
e.g. ≥ 60%), and a hand review of a random sample of 100 seeds shows they're
mostly hiking or outdoor blogs.

## Stage 4: Validate seeds with the URL index

### Which index: CDX (zipnum) for seed lookups, Parquet for whole-index scans

Both indexes live on `data.commoncrawl.org`. We measured them in Nov 2026:

- **Columnar (Parquet) index:** ~300 files per crawl, each ~7.3M rows in 5
  row groups (~150 MB each). Each file covers one contiguous SURT range, but
  file numbers are *not* in SURT order. The host columns are tiny (~1 MB per
  file, since they're sorted), but `url` is ~95 MB per file, and `url_path` and
  `url_surtkey` are similar. Pulling the columns we need for *all* rows is
  ~30–60 GB per crawl. That's the right tool for a whole-index scan, such as
  a URL classifier over every English page (Stage 7), not for per-site lookups.
- **CDX (zipnum) index:** 300 sorted, gzipped shards (~800 MB each) plus a
  `cluster.idx` (~100 MB) listing the first SURT key and byte range of each
  block of ~3,000 lines (~270 KB compressed). One site usually lives in one
  block, so the top 3,000 seeds need ~3,000 blocks, about **1 GB per crawl**.
  This is what Stage 4 uses.

`index.commoncrawl.org` (the CDX query API) refuses connections from some
cloud networks, ours included, so we read the zipnum files directly.

### Rate limits on data.commoncrawl.org

From [Common Crawl's status page](https://status.commoncrawl.org/): HTTPS
access goes through CloudFront, where **4xx (here: 403) responses are
explicit rate-limit rules** and 5xx are S3 backpressure. The whole service
handles "a few thousand requests per second" for everyone. The guidance is
to **slow down** on 4xx/5xx, not just retry. Retrying alone "does not work
well enough for partial file downloads, such as index lookups". No
particular User-Agent is required (they block a UA only as a last resort
against abusive clients); ours names the project and repo.

What we saw: the first `site-urls` run sent ~2,500 Range requests in 64 s
(4 concurrent, ~40 req/s) and was then refused with 403 for ~6 minutes.
Whole-file streams (`hubscore`) rarely notice, because once a request gets
through the rest of the file streams without further checks.

So `internal/ccio` paces every request through one shared, adaptive
limiter. It starts at one request per 250 ms and doubles the interval once
per *burst* of 403/429/503 responses (signals within 2 s count as one), up
to 30 s. The extra delay then decays with a 30 s half-life once refusals
stop.

The first version recovered per request instead (−10% after every 20
successes) and treated each concurrent refusal as a separate signal. In
the classify run, that turned a few one-minute 403 bursts (10–20 refusals
per job) into hours at a 10–30 s pace: partition 0 spent ~3 of its 4 hours
recovering. The four jobs running in parallel got their first 403 within
the same 5 s, which hints at a shared trigger (a shared egress IP or a
Common Crawl-wide spike). But the next four parallel jobs saw almost
none. Jobs now log their public IP so we can check. Retries back off from 5 s, doubling up to 2 min.
`sitecdx` also merges blocks across gaps of up to 4 MB (CloudFront limits
requests, not bytes) and defaults to 2 concurrent requests. A crawl lookup for
3,000 sites takes ~10 min at that pace.

### Implementation

- [`internal/ccio`](../internal/ccio/ccio.go): shared HTTPS reader with
  retry, backoff and Range-resume. Common Crawl's CDN answers bursts with 403.
- [`cmd/sitecdx`](../cmd/sitecdx/main.go): loads a crawl's `cluster.idx`,
  finds the blocks covering each site's SURT range (`host)` … `host-`, which
  covers the domain and all subdomains), caps it at `-max-blocks-per-site`
  (default 4, ~12k captures), merges adjacent blocks into large Range
  requests, and writes every matching capture with its WARC pointer.
- [`scripts/site_report.py`](../scripts/site_report.py): per-site features
  over unique HTML pages (`text/html` or `application/xhtml+xml`, status 200,
  excluding boilerplate such as `/tag/`, `/page/2`, `/feed`):

  | feature | signal |
  |---|---|
  | `hike_share` | hiking terms in the path (hike, trail, trek, summit, scramble, snowshoe, thru…) |
  | `place_share` | landform terms (lake, falls, peak, canyon, ridge, pass, national-park…) |
  | `geo_share` | a US state, Canadian province/territory or national park name ([`data/gazetteer/`](../data/gazetteer/)) |
  | `blog_share` | `/2023/07/…` or a long hyphenated slug |
  | `shop_share` | `/product/`, `/shop/`, `/cart`, `/collections/`… |
  | `gear_share` | gear-review slugs (best-, -review, boots, tents, jackets…) |
  | `eng_share` | Common Crawl's primary detected language is English |
  | newest vs older | slug-match share in the newest crawl with ≥ 20 pages vs older crawls |

  Verdicts (first-cut thresholds): `hiking` (keep all content pages),
  `mixed` (keep slug-matching pages only), and `drifted` (newest crawl's
  match share < 25% of older crawls', e.g. an expired domain; keep older
  matching pages). Sites that are dropped: `shop` (≥ 25% commerce paths and
  more than hiking paths), `gear` (≥ 45% gear-review paths), `off_topic`,
  `thin` (< 5 pages captured) and `non_english`.
- Workflow: [`site-urls`](../.github/workflows/site-urls.yml) (Arm). The
  site list is split round-robin into N partitions. Each partition is one
  job that looks its sites up in every crawl, then scores them
  (`site_report.py score`). A final job merges the per-site rows
  (`site_report.py report`). No job ever holds more than its own sites.
- **Two passes**, because all ~89k seeds × 5 crawls × 4 blocks would be
  ~180M index lines:
  1. *classify*: all seeds, two crawls (one old, one recent), 1 block per
     site, which is enough to judge a site;
  2. *harvest*: only `hiking`/`mixed`/`drifted` sites from pass 1, all
     crawls, up to 4 blocks per site, producing the candidate URL list.

Fixes after the first top-3,000 run (site-urls run 1):

- **Block sampling bias:** the per-site cap used to take a site's
  alphabetically *first* blocks. A forum-heavy or long site was judged by
  whatever sorts first, e.g. `/forum/search.php` pages. `sitecdx` now picks
  evenly spaced blocks across the site's range.
- **Per-page language:** a page counts only if Common Crawl detects English
  *and* its path has no non-English locale prefix (`/de/`, `/fr-ca/`).
  Everything else is measured over English pages only, so a site with a
  big localized section (visitutah.com) is no longer `non_english`.
- **Other activities:** jeep, 4x4, ATV, OHV, overlanding, mountain biking,
  golf, wine, breweries and horseback make a page not match. A site whose
  name contains one of these unambiguous terms (trailsoffroad.com) is
  `other_activity`.
- **Drift:** login, user, profile and phpBB utility pages are boilerplate.
  A site counts as `drifted` only if its newest crawl is mostly *new* URLs
  across ≥ 3 sections of the site (mountaineers.org's newest crawl was a
  single feedback forum).
- `sitecdx -html-only` (default) keeps only status-200 HTML captures,
  ~22% fewer rows.
- Classify pass over all seeds (site-urls run 2; report:
  [`data/reports/stage4_classify_site-urls-run-2.md`](../data/reports/stage4_classify_site-urls-run-2.md)):
  88,727 sites, 76,640 with captures in CC-MAIN-2026-34 or CC-MAIN-2023-40.
  2,554 `hiking`, 7,255 `mixed`, 83 `drifted`. The `hiking` share falls from
  42% in the top 500 to ~1% past rank 40k, but 1,785 `hiking` sites rank
  below 5,000. The run took 6 h: the first four partitions took 3.3–4.7 h
  (paced down by rate limits), the second four ~1 h.
- Locale bug found there: any two-letter first path segment counted as a
  language prefix (roadtripryan.com's `/go/…` came out `non_english`).
  Only real ISO 639-1 codes count now. The harvest re-checks the
  `non_english` sites with the fix.
- Harvest pass (site-urls run 4; report:
  [`data/reports/stage5_harvest_site-urls-run-4.md`](../data/reports/stage5_harvest_site-urls-run-4.md)):
  15,812 sites (classify's hiking/mixed/drifted plus non_english for the
  locale-bug re-check), 5 crawls, up to 4 blocks per site. **9,153 sites
  kept** (2,604 hiking, 6,458 mixed, 91 drifted), **2.0M candidate URLs**
  (1.03M with a hiking/place slug). It took 1 h 25 min: 8 jobs of ~40 min
  each, ~8.5k requests per job. With the new pacer, the worst pacing interval was
  under 1 s.
  - Re-checking the 5,905 `non_english` sites rescued only 92, so the
    locale bug had little effect.
  - All 8 jobs had distinct public IPs, so parallel jobs don't share a rate
    limit. The two jobs with the most 403 bursts (11 and 12, against 0–2
    for the rest) were on neighbouring IPs (135.18.239.65 and .98), which
    hints that Common Crawl's limit may apply per subnet.
  - Known issues: expired blogs whose newest crawl is spam in another
    language (nobodyhikesinla.com, hikeitbaby.com) come out `hiking`, not
    `drifted`. Their kept pages are the genuine older posts, but the
    drift check ignores non-English pages, so it misses the change.
    backpacker.com comes out `gear` (45%+ gear-review slugs) once more of
    the site is sampled.
- Known limit: forum sites (oregonhikers.org is ~99% phpBB) have no words
  in their topic URLs, so URL scoring can't judge them; content checks
  (Stage 7) can.

Calibration notes from the eval lists (one crawl, CC-MAIN-2023-40):

- 22 of the 24 known blogs captured there come out `hiking`; travel blogs and
  magazines come out `mixed`, as intended.
- Gear sites separate cleanly on `gear_share`: OutdoorGearLab 96%,
  Switchback 61%, Greenbelly 46%; most blogs sit at 4–21%. **But
  sectionhiker.com is at 37–44%**, so the 45% threshold is fragile.
- Aggregators (10adventures.com, theoutbound.com) come out `hiking`. Their
  pages *are* about hiking; they are just not blogs. `site_kind` (Stage 3)
  is where that distinction belongs.
- Some captures are useless even with status 200: backpacker.com's 2023
  captures are almost all 302 redirects (consent or paywall bounce).

## Stage 5: Select candidate post URLs (the first deliverable)

From accepted domains, keep URLs that:

- are HTML, `fetch_status = 200`, primary language English;
- are not listing or boilerplate pages: `/tag/`, `/category/`, `/page/N`,
  `/feed`, `/author/`, `/search`, `/wp-json`, `?replytocom=`, attachments;
- are deduped on `url_surtkey` (latest capture wins) and on `content_digest`.

Then tag each URL:

- `slug_match`: whether the slug has hiking terms and/or a US/CA place;
- `site_score` and `site_kind`: from Stages 3 and 4.

URLs from strong domains are kept **even without** a slug match. Many good
posts have slugs like `/day-3/`.

**Output:** `candidates/<crawl>.parquet` plus a rolled-up `urls.parquet`,
published as a GitHub Release asset or a Hugging Face dataset.

**Columns:** `url, site, site_kind, crawl, fetch_time, slug_match, site_score,
warc_filename, warc_record_offset, warc_record_length, content_digest`.

## Stage 6: Snowball (expand the seeds)

Once we have high-confidence seed blogs, use them to find more:

- **New hubs**: domains linked to by an unusually large share of seed blogs
  (blogrolls, regional trail sites, guidebook publishers). Add them to
  `hubs.csv` at a learned weight after human review.
- **Co-citation**: domains that share many outlink targets with seed blogs.
- **Inlinks from seeds**: blogs linking to other blogs, via the transposed
  graph or a forward-edge filter on seed `from_id`s.
- Re-run Stages 2–5. Stop when new seeds mostly fail review.

Guard against drift. Every round's additions are compared against
`negatives.csv` and spot-checked.

## Stage 6b: Find sites by hike names in their URLs

The web-graph seeds only find sites that hubs link to, and the domain graph
can't see platform blogs. Personal blogs put the hike's name in the URL
(`/2019/08/panorama-ridge/`), and Stage 9 gave us a list of real hike names.
`scripts/slug_scan.py` scans one crawl's columnar URL index for them. It
reads only the host, path, status, MIME and language columns, with range
requests: ~70 MB and ~40 s per index file, ~21 GB per crawl. The
`slug-scan` workflow runs it as 16 Arm jobs.

- **Names.** Multi-word names from the autosuggest index. A name counts only
  if it contains a landform or trail word (lake, peak, falls, ridge,
  trail…) or has qrank ≥ 100: OSM has trails called "Happy Birthday" and
  "Peanut Butter". Names ending in Trail, Loop or Hike also match without
  that last word.
- **Skipped.** Matches followed by a street word ("ridge-avenue"), and
  paths our URL rules already drop (real estate, shops, attachments).
- **Per site** (registered domain, or host under blogspot and other
  platforms) we count:
  - distinct names;
  - names in URLs that also contain a hiking word ("cue names");
  - hits and pages.
- **Score.** Each name is weighted by its rarity across sites (idf). Cue
  names count in full and other names at 0.1. The total is scaled down
  when a site's hiking URLs are under 2% of its pages (lyft.com,
  macys.com).

**Pilot (4 index files of CC-MAIN-2026-34).**
- The top of the ranking is mostly hiking and outdoor sites: known ones
  (southshoretrails.com, southdakota.com) and new personal blogs
  (photomomlinda, gulplife, roadtripsandhikes, marcy-twss and
  mountainwandering, all on blogspot).
- 13 of the 28 known sites in those files ranked in the top 100.
- Remaining noise is mostly real-estate sites and big general sites. The
  existing site-content and page-pass stages filter candidates by their
  content anyway, so this stage only needs a good shortlist.

**Full run (slug-scan-run-1, CC-MAIN-2026-34, 2026-11-08).**
- **Coverage.** 1.57M sites have at least one URL naming a hike.
  - 1,737 of our 4,403 page-pass sites show up with ≥ 5 distinct names.
  - Sites scoring ≥ 5: 83k.

  | score | sites | already known |
  |---|---:|---:|
  | ≥ 200 | 904 | 259 |
  | 100–200 | 1,370 | 272 |
  | 50–100 | 2,895 | 335 |
  | 25–50 | 6,973 | 407 |
  | 10–25 | 28,601 | 458 |

- **Precision.** Eyeballing random new sites by score band:
  - ≥ 100: roughly 60% hiking or outdoor sites (seniorhikerphotos,
    hikestgeorge, westkootenayhiking.ca, ozarkhighlandstrail.com).
  - 50–100: ~35%.
  - 25–50: ~20%.

  The rest are real estate, running races, golf, travel agents, and
  galleries that title paintings after places.
- **Watch list.**
  - Panorama Ridge: 87 sites (vancouvertrails, northshorehikers.org,
    rambleon.ca, wanderung.ca, several personal sites).
  - Tonquin Valley: 16 (some guiding and blog sites, many art galleries).
  - La Cloche Silhouette: 1 new site (myoverdueadventures.com).
  - "Green gardens" is too generic a phrase to be useful.

### Second batch through Stages 4–9 (2026-11-08)

The 4,303 new sites scoring ≥ 50 went through the same stages as the
first seed list, as a second batch:

| stage | run | sites | pages / URLs |
|---|---|---:|---:|
| URL index classify | `site-urls-run-5` | 2,206 kept | |
| harvest | `site-urls-run-6` | 2,064 | 496,759 URLs |
| site content | `site-content-run-2` | 780 kept | 156,907 URLs |
| page pass | workflow run 42445288065 | 780 (164 personal) | 107,854 pages kept |
| extraction, combined with batch 1 | `extract-run-6` | | |

- **Page-pass release.** The page pass's publish step got `HTTP 403:
  Resource not accessible by integration` from `POST /releases` three
  times, although its token had `contents: write` and the extract run
  minutes later published normally. Cause (found later, see Runner budget):
  `--target "$GITHUB_SHA"` on a commit older than `main`'s workflow files. There is no
  `page-pass-run-2` release; its outputs are the run's artifacts (kept 30
  days). `extract.yml` gained a `page_pass_run_id` input to read them, and
  now always uploads its report and hike tables as an `extract-report`
  artifact.
- **Junk sites.** The biggest new contributor was ridepal.app (2,437
  "hikes"), a mountain-bike trail map database. A hand check of the top
  new sites found 11 more that aren't hiking: bike and motorcycle route
  apps, snowmobile reports, RC rock crawling, radio tower listings,
  campsite booking, hotels, cabin rentals, a park directory, driving audio
  tours and a radio station. They are listed in `data/excluded_sites.csv`
  (5,950 pages dropped). The slug scan's score can't
  tell these apart, because their URLs really are trail names.
- **Combined result** (both batches, after the exclusions):

  | | batch 1 | combined |
  |---|---:|---:|
  | sites with hikes | 3,024 | 3,529 |
  | hikes | 61,199 | 68,294 |
  | hike groups | 19,936 | 22,687 |
  | groups covered by ≥ 5 sites | 1,581 | 1,924 |
  | groups covered by ≥ 10 sites | 487 | 583 |

  Good new personal sources: stavislost.com (949 hikes), harrimanhiker.com,
  runbikehike.us, robshikingdiary.com, yetihikes.net.
- **Watch list.** Panorama Ridge 10 → 12 sites (5 → 7 personal); La
  Cloche Silhouette Trail 2 → 4; Green Gardens Trail 2 → 4; Tonquin Valley
  Trail now has a group of its own (1 site). "Hike #25: The Crack" stays
  ungrouped: the index names the trail `The "Crack" Trail` and the title
  omits "Trail".

## Stage 7: Content spot checks (cheap, sampled)

Range-fetch a **sample** of candidate URLs (e.g. a few hundred per run) from
their WARC records. Run a heuristic page check:

- density of hiking vocabulary;
- gazetteer hits for US/CA places;
- signs of a blog page: `article`/`BlogPosting` markup, a byline, a date,
  comments;
- geo hints: `geo.position`/ICBM meta, JSON-LD coordinates, GPX/KML links.

Uses:

- precision estimates per stage and per score bucket;
- **training labels** for a tiny URL-only classifier (char n-grams with
  logistic regression or fastText). Later, that classifier can be applied to
  the *whole* URL index to find hiking posts on domains the graph missed.

### First spot check (site-urls run 4, 2026-11-07)

Report: [`data/reports/stage7_spotcheck_site-urls-run-4.md`](../data/reports/stage7_spotcheck_site-urls-run-4.md).
Tools: [`scripts/sample_candidates.py`](../scripts/sample_candidates.py)
(stratified sample) and [`cmd/warcfetch`](../cmd/warcfetch/main.go) (fetches
WARC records by pointer and extracts the title and text).

**Weighted precision of the 2.0M candidate URLs is ~16%** (13% for
specific trails/destinations in the US/Canada). Pages from `hiking` sites
without a hiking slug, half the list, are ~3% relevant. About 31% of pages
are junk (attachments, search URLs, bot checks), and real-estate, RV and
shop pages slip through. Cheap URL rules get to ~35% while keeping ~84% of
good pages. Going further needs page content.

### URL rules, page model, and site-content pass (2026-11-07)

- **URL rules** ([`scripts/urlrules.py`](../scripts/urlrules.py), used by
  `site_report.py` and [`scripts/filter_candidates.py`](../scripts/filter_candidates.py)):
  only slug-matching pages from any site; drop URLs with query strings,
  real-estate paths/sites, shop paths and image-attachment slugs. On the
  first labelled sample they drop 47 bad pages and no good ones. Applied to
  run 4: **2.0M → 845k URLs from 8,933 sites**. A fresh labelled sample of
  300 from the filtered list measured **39% precision** (core 31%), up
  from 16%.
- **Text extraction** in `cmd/warcfetch` now drops `<select>` dropdowns and
  link-list menus (≥ 6 items, ≥ 80% links). Menus full of "hikes by state"
  were making image-attachment pages look like hiking content.
- **Page model** ([`scripts/page_features.py`](../scripts/page_features.py),
  [`scripts/train_page_model.py`](../scripts/train_page_model.py),
  [`data/models/page_model.json`](../data/models/page_model.json)): a
  stdlib logistic regression over 11 text features. The strongest are
  hiking-term count (+), other-activity terms (−: biking, skiing, RV,
  fishing, hunting…), non-US place terms (−), commerce terms (−),
  distances in miles/feet (+) and first-person voice (+). Trained on the
  500 labelled pages (70/30 split). Held-out precision/recall varies a lot
  between splits (150 test pages): at p ≥ 0.5, precision 57–83% and recall
  47–76%, so roughly **65–70% precision at 60–65% recall per page**. The
  base rate is 30%.
- To retrain: re-fetch the labelled pages with
  `warcfetch -in data/eval/spotcheck/<file>_labels.tsv -out <file>.jsonl`.
  The label files carry WARC pointers, so no third-party page text is
  stored in the repo. Then run `train_page_model.py`.
- **Site-content pass** ([`scripts/site_content.py`](../scripts/site_content.py),
  [`site-content`](../.github/workflows/site-content.yml) workflow): fetch
  up to 5 random filtered candidates per site (~43k pages), score them,
  and keep a site when the mean relevance ≥ 0.35. Averaging over pages
  smooths out the per-page noise. The output is the final candidate list
  (the filtered candidates of kept sites).

### Site-content run 1 results (2026-11-07)

Report: [`data/reports/stage7_site-content-run-1.md`](../data/reports/stage7_site-content-run-1.md);
outputs in the `site-content-run-1` release. ~43k pages fetched in 49 min
(8 Arm jobs).

- **4,403 of 8,933 sites kept → 323,762 candidate URLs** (from 845k).
- Known blogs: 26 of the 28 sampled are kept. Dropped: andrewskurka.com
  (its sampled pages were gear giveaways) and outsideonline.com.
- Negatives still kept are mostly professional hiking content
  (onlyinyourstate.com, visitutah.com, theoutbound.com, 10adventures.com,
  travelalberta.com). They should get the professional `site_kind` label
  rather than be dropped. Expired blogs (nobodyhikesinla.com,
  hikeitbaby.com) are kept on the strength of their genuine older pages.
- Precision estimated from the 300 labelled filtered-list pages: pages on
  kept sites are **64% relevant (95% CI 55–71%; core 48%)**, against 19%
  on dropped sites. **72% of the good pages are retained.** This is
  somewhat optimistic, since the page model was partly trained on these
  labels. A fresh sample from the final list would give an unbiased
  number.
- Remaining errors on kept sites: stub and empty pages on otherwise good
  aggregators (topo place stubs, empty lists, photo albums), other
  activities (running/biking pages), and some non-US routes. Most are
  short or low-scoring pages, so a page-level pass (fetch and score every
  remaining page) is the obvious next filter.

### Unbiased check of the final list, and the page pass (2026-11-07)

- **Fresh sample of 150 pages from the site-content-run-1 list** (no
  overlap with earlier labels; `data/eval/spotcheck/site-content-run-1_labels.tsv`):
  **63% ± 8% precision** (core 53%), matching the 64% estimate. A third of
  the relevant pages are personal.
- Page model retrained on all **650 labels**. Held out (196 pages, 5
  splits): at p ≥ 0.5, precision 68–82% and recall 63–79%. Trained on the
  first 500 labels only and tested on the 150 unseen final-list pages, a
  page filter at **p ≥ 0.3 lifts precision 62% → 73% while keeping 92% of
  relevant pages** (p ≥ 0.4: 77% / 85%).
- **Site kind:** `train_page_model.py --target personal` fits a second
  model on hiking content pages (kind P vs C): first-person and singular
  pronouns, affiliate disclosures (+), business and commerce terms (−). Per
  page it is weak (accuracy 71–85% against a 66–72% baseline; personal recall
  30–60%), so it's used only as a per-site average: `site_kind` is
  personal when the mean personal score of a site's kept pages is ≥ 0.5.
  Checked against the authorship labels in `known_blogs.csv` in the report.
- **Page pass** ([`scripts/page_pass.py`](../scripts/page_pass.py),
  [`page-pass`](../.github/workflows/page-pass.yml) workflow): fetch every
  remaining candidate page (324k, 16 partitions keeping each site
  together), score relevance and personal-ness, and keep pages at p ≥ 0.3.
  Publishes the final page list, a site table with `site_kind`, and the
  fetched page text, ready for the richer extraction later.

### Page pass run 1 results (2026-11-07)

Report: [`data/reports/stage7_page-pass-run-1.md`](../data/reports/stage7_page-pass-run-1.md).
Outputs in the `page-pass-run-1` release: `pages.tsv.gz` (the current
deliverable), `sites.tsv.gz`, and `page-text-part-*.jsonl.gz` (the fetched
text of all 324k pages, for later extraction).

- All 323,762 candidate pages were fetched; **262,586 were kept** (81%) at
  relevance ≥ 0.3, across 4,403 sites. From the unseen-sample test,
  expected precision is ~73% (from 63%) with ~92% of relevant pages
  retained.
- **Site kind:** at the original 0.5 cutoff, only 9 of 25 known blogs
  matched their authorship label. Personal blogs written as trail guides
  (modernhiker.com, hikespeak.com, roadtripryan.com) use little first
  person and score 0.2–0.45. As a *ranking*, `personal_score` does better:
  on 188 sites with hand labels (spot-check pages plus known blogs) its
  **AUC is 0.79**. At ≥ 0.35 it's ~72% precise for personal sites with
  ~59% recall (somewhat optimistic, since these sites overlap the training
  labels). The default cutoff is now 0.35 (1,643 sites, 63,851 kept pages). Downstream users
  should treat `personal_score` as a sort key, not a verdict. Better
  signals would be site-level: number of distinct bylines, site size,
  graph prominence, and about pages.

## Stage 8: Measure recall with WET samples

The graph-based funnel can't see blogs that don't link to our hubs. To measure
how many we miss:

1. Download a random sample of WET files (plain text, ~100 MB each), e.g.
   100–200 files spread over a matrix of jobs.
2. Run a keyword and gazetteer scan to find hiking-blog-like English pages
   about US/CA places.
3. Check what share of those pages' domains appear in `seeds.parquet`.

Domains we missed reveal new hubs and vocabulary. Feed them back into Stage 0
and Stage 6.

---

## Stage 9: Extraction (first pass)

`scripts/extract.py` (stdlib) reads the page text saved by the page pass and,
for every kept page, writes one JSON record:

- **page_type**: `trail_guide` (≥ 2 stat labels such as "Distance:" or
  "Elevation gain:"), `list_article` (listicle title), `trip_report`
  (first-person past tense: "we hiked", "I reached"; ≥ 4 such phrases wins
  over a stats box), `article`, or `general` (no located features).
- **features**: trails, hikes, peaks, lakes, waterfalls, cliffs and parks,
  matched against the Hiker Atlas autosuggest index
  (`north-america.autosuggest.db`, US and Canada only). Matching is a greedy
  longest match over capitalised spans of 2+ tokens. Names made only of
  generic words, one-word names and a short stoplist of ordinary phrases
  ("out and back", "runner up") are skipped.
- **location**: one region per page. Every candidate location of every
  mentioned name is tried as the page's centre; a centre scores the
  mentions of each name with a candidate within 100 km, weighted up when the
  feature's state or park is also named on the page. The best centre wins,
  and names with no candidate near it are dropped. A **confidence gate**
  keeps the region only if one of its states is named on the page, three
  names agree, one feature is well known (Wikidata qrank ≥ 1000), or two
  names agree and one has qrank ≥ 100. Otherwise the page has no location
  and no features.
- **Long-distance trails** (Appalachian, Pacific Crest, Arizona Trail…: 34
  names) are kept as
  features with `long_trail: true` and no coordinates, and don't vote on the
  region. Their segments are scattered across states, so any one segment's
  location is arbitrary. A long trail needs:
  - a trail-like name (trail, way, path, trace…);
  - ≥ 3 segments spread over > 300 km;
  - qrank ≥ 100;
  - segments that form a chain: ≥ 80% of them have another within 40 km.

  The chain test was added after run 3, which also counted common names
  ("Lost Lake", "Skyline Trail": 310 names) as long trails and so threw away
  their locations.
- **Site boilerplate**: a name on ≥ 50% of a site's pages (sites with ≥ 10
  pages) is navigation and is ignored for that site. Each site's pages sit in
  one page-pass partition, so this is a two-pass job per partition.
- State names inside a matched feature name ("Mount Washington") don't count
  as mentions of the state.
- **distance_mi** (title first, then a body number near "round trip",
  "total", "loop"…), **gain_ft**, **date** (from the URL, else the first
  "Month D, YYYY" in the text).

### First results (page-pass-run-1 partition 5, 2026-11-07)

18,553 kept pages in under 2 minutes on one core.

| field | share |
|---|---:|
| location | 48% |
| 3+ features | 25% |
| distance | 58% |
| gain | 39% |
| date | 59% |

Page types: general 45%, article 28%, trail_guide 11%, list_article 10%,
trip_report 5%. Eyeballing 40 located pages before the confidence gate:
~70% placed correctly; the errors were non-US pages (Wikiloc trails in
Cyprus or Portugal on "Hiking Trails, Minnesota") and single weak names
("Great Wall" → Idaho, Haleakalā's "Sands Trail" → Massachusetts). After
the gate and stoplist, ~26 of 30 placed correctly. The gate cut located
pages from 66% to 48%.

Known limits:

- A page resolves to one region only.
- The index's park attribute is sometimes off (an Arizona Mt. Baldy tagged
  "Gila Wilderness").
- Features on unlocated pages are discarded, even when correct (e.g. a
  single obscure trail name on a Bay Area running blog).
- Distances from listicles and visitor guides are often one hike's number,
  not the page's.
- The trip_report rate looks low.

### Full run 1 and fixes (extract-run-1, 2026-11-08)

Over all 262,586 pages: 69% located, 38% with ≥ 3 features. The top parks
exposed three systematic errors, now fixed (above):

- "Mount Washington" resolved to Washington state on 1,877 myhikes.org
  pages, because the word "Washington" in the name counted as a state
  mention.
- The Appalachian Trail resolved to one Connecticut segment ("Hamlet Hill")
  on 1,300 thetrek.co pages.
- Nav menus put the same park on every page of a site: n2backpacking.com
  had "Great Smoky Mountains NP" on 1,800 pages.

On two partitions the fixes changed the state of 4,200 of 39,600 pages,
mostly by un-locating photo, attachment and announcement pages that had only
inherited a site's menu.

### Full run 2 (extract-run-2, 2026-11-08)

With the fixes: 64% of pages located (167,177), 34% with ≥ 3 features,
distance 38%, gain 15%, date 37%. Another 6,139 pages have only a state, from
a long trail plus a named state. The top parks are now the expected ones
(Rocky Mountain, Great Smoky Mountains, Yosemite, Glacier, Grand Canyon,
Yellowstone). In a random sample of 40 located pages, ~37 had the right
state. The misses: a Utah singletrack placed in Colorado; an Appalachian
Trail wish-list post placed in Michigan by "Lake Michigan"; and a
"Public Land" match on a policy story.

### Page-type model (2026-11-08)

The rule-based types matched hand labels on only 57% of pages. For example,
43 of 60 rule `list_article` pages were something else, and the rules had
no type for park and destination guides. 300 pages (at most one per site,
60 from each rule type) were labelled into five types in
`data/eval/page_types.tsv`: trip_report 68, trail_guide 64, list_article
39, area_guide 26, other 103. `scripts/page_type.py` fits a stdlib
multinomial logistic regression over 23 text features:
- first-person and "you" rates;
- narrative past tense and story time words;
- stat-box labels, directions and numbered items;
- area-guide, gear, news and skills vocabulary;
- title and URL patterns;
- the number of extracted features.

Five-fold cross-validated accuracy is **76%**, against 57% for the rules (L2 = 20).
Trip reports: ~80% precision and recall. The model's output is
`page_type`, with `page_type_p`; the old rules are kept as `rule_type`.

### Hikes (Stage 9b, 2026-11-08)

Every trip report and trail guide gets a `hike` record:
- a name (the title minus the site name);
- destinations: peaks, lakes, waterfalls named in the title, else the
  page's most-mentioned one;
- trails, from the title first, including long trails;
- park and state;
- distance, gain, route shape (loop, out-and-back…), difficulty, and a
  date for trip reports.

`scripts/hikes.py` keeps hikes that have a location and a destination or
trail. It joins each hike to its site's kind (personal or organisation) and
writes `hikes.tsv.gz` plus `hikes_report.md`, which lists the most-covered
destinations and a sample from personal sites.

On partition 5, 8,149 hike pages gave 3,429 hikes. 48% of hike pages were
dropped for having no location: mostly non-US trails, and one-word peak
names ("Moosilauke") that matching ignores.

### Full run 3 (extract-run-3, 2026-11-08)

Page types over 262,586 pages: other 52%, trail_guide 26%, trip_report 12%,
list_article 7%, area_guide 3%. These 100,509 trip reports and trail guides
gave **62,079 hikes** from 3,069 sites; 8,152 of the hikes are from
personal sites. Field coverage among the hikes:

| field | share |
|---|---:|
| destination | 64% |
| trail | 64% |
| distance | 67% |
| gain | 28% |
| date (trip reports) | 18% |
| difficulty | 25% |

Two sites dominate: trailrunproject.com and theoutbound.com supply 23% of
hikes. The most-covered destinations are what you'd expect: Mount Rainier
on 214 sites, then Crater Lake, Angels Landing, Half Dome and Mount
Whitney. About 24 of 30 random hike rows were usable. The misses:
- archive or index pages typed trail_guide ("Blog Archives",
  "Destinations - Hiking");
- a list article ("7 Best Hikes in Mt Rainier") typed trail_guide;
- a nearby lake picked over the title's own lake;
- a Grand Canyon page placed in Utah.

### Hike fixes after run 3 (2026-11-08)

- **One-word names from the title.** A one-word peak, lake or waterfall
  name counts when all of these hold:
  - it is capitalised in the title and never lower case in the text;
  - it isn't next to another capitalised word or followed by a comma or
    state abbreviation ("Ogden, Utah", "Chris Townsend");
  - it isn't a state name;
  - it is well known (qrank ≥ 1000);
  - its state is named on the page.

  Peaks called "Mount X" also answer to "X" ("Hiking Moosilauke").
- **Title votes.** A name in the title gets 3 extra votes for the page's
  region.
- **Peaks over routes.** When the index has a route ("hike") and a peak,
  lake or waterfall with the same name within 10 km, the feature is the
  peak ("Catoctin Mountain", "Mount Mitchell").
- **Long trails.** The chain test (above).
- **Hike table filters.** Index or archive pages and list titles
  ("7 Best Hikes in…") are dropped from `hikes.tsv.gz`.

Adding archive and list features to the page-type model didn't help (75%
vs 76%, within noise), so those pages are filtered by title and URL instead.
On partitions 5 and 7, 870 hikes changed destination or trail, and
eyeballing showed mostly fixes: Convict Lake, Lake Isabelle, Union Falls,
Catoctin Mountain, the Grand Canyon in Arizona. 360 index and list pages
were dropped.

### Full run 4 and hike groups (extract-run-4, 2026-11-08)

Run 4: 61,199 hikes. 63% have a destination and 67% a trail. About 25 of
30 random rows were usable, against ~24 in run 3.

**Hike groups** (`hike_groups.tsv.gz`, from `scripts/hikes.py`) put pages
about the same hike together:
- **The key** is the hike's main destination OSM ID, else its main trail.
  A long trail alone groups by long trail and state ("Appalachian Trail
  (Maine)").
- **Routes:** different routes to one summit share a group, and their
  trails are listed as variants with counts. Mount Rainier: Skyline Trail
  56, Wonderland Trail 42. Half Dome: Glacier Point, Mist Trail.
- **Merging:** groups whose names match (ignoring trail/loop/hike) are
  merged when within 15 km, chaining. This joins a trail's separate OSM
  ways and the index's "Bright Angel" route with "Bright Angel Trail"
  (140 sites).
- **Each group row** has:
  - site, personal-site and page counts;
  - median distance and gain;
  - the top trails;
  - up to 5 example URLs, one per site, personal sites and trip reports
    first.
- **`hikes.tsv.gz`** gains a `group_id` column.

On run 4 there are 19,936 groups. 487 groups are covered by ≥ 10 sites
(16,174 hikes); 12,953 come from a single site.

**Trail-first keys (after run 5).** Pages about a named trail were split
by whichever lake or peak they mentioned most: Tonquin Valley Trail went
to Cavell Lake and Chrome Lake, and La Cloche Silhouette Trail was spread
over 4 groups. Now, when the title names a trail before any destination,
the trail is the key. This gives 20,974 groups, and Mount Rainier's
"Skyline Trail" pages now group under the Skyline Trail.

Known gaps:
- "South Rim" matched the Black Canyon's South Rim in Colorado for
  Grand Canyon pages. It and "North Rim" are now stoplisted.
- Very long trails that fail the chain test (the Bruce Trail) stay split
  into several groups.

The `extract` workflow runs this over all 16 page-text partitions (Arm
matrix) and publishes an `extract-run-<n>` release with
`extracted-*.jsonl.gz` and a report.

### Where pages and sites are lost (2026-11-08)

The data is for linking blog posts to hikes, so recall at each filter
matters more than a few extra junk pages, which later stages remove.

**Site content (Stage 7) was the biggest cut for the slug-scan batch:**
2,060 sites in, 780 kept. Hand labels on 80 dropped and 40 kept sites
(from their URLs and best titles): ~26% of dropped sites were real US/CA
hiking sites (hikethehudsonvalley.com, jameskaiser.com,
floridarambler.com, car-free.ca, algonquinpark.com, christiededman.com),
and ~70% of kept sites were. Estimated precision 72%, recall 63%. Three
causes, seen in the scores of the 5 sampled pages:

- **Bot-check interstitials.** Common Crawl captured "One moment,
  please..." pages (11 words, p = 0.03) for some sites; christiededman.com
  had 4 of 5. Only 0.4% of batch-1 page-pass pages, but fatal when a site
  is judged on 5.
- **Image attachment pages** (WordPress photo pages, 0–35 words).
- **"km" counted as foreign.** `NONUS` in `page_features.py` included km
  and kilometres, which Canadian pages use: avontrail.ca, car-free.ca,
  algonquinpark.com and pnoa.ca all lost points. All five watch-list
  hikes are Canadian.

Fixes: km/kilometres removed from `NONUS` and the page model retrained on
the same 650 labelled pages (held-out precision/recall unchanged, since
they are almost all US); `site_content.py` leaves pages under 50 words out
of a site's mean. Estimated on the labelled sites: precision 70%, recall
75%; batch 2 keeps 977 sites instead of 780. Keeping the mean of the best
two pages would reach ~86% recall at ~61% precision. Small personal blogs
also miss the slug-scan cutoff (score ≥ 50); golivexplore.com and
myoverdueadventures.com scored 12 and 20.

**Extraction drops 37% of trip reports and trail guides.** A labelled
sample of 60 "no location" and 60 "no destination or trail" pages from
batch 1:

- No location: ~55% correctly dropped (Wikiloc pages outside North
  America, the Alps, Skye, India, news, races). Of the ~45% real hikes,
  most name a trail the index doesn't have (Berlin Lake Trail, Pu'u o
  Hulu); a few fail the confidence gate because the best-scoring centre
  is in the wrong state while the page names the right one ("Chief Trail",
  Indiana vs the Stawamus Chief, British Columbia).
- No destination or trail: ~40% are real single hikes. Most are a small
  park or preserve with no named peak or trail (Barrett Preserve, Otis
  Farm Bird Sanctuary, Sondermann Park), and some are spelling mismatches:
  "Angel's Landing" vs "Angels Landing", "Mount Yonah" vs OSM's "Yonah
  Mountain", "Lower Yosemite Falls" vs "Lower Yosemite Fall".

Fixes in `extract.py`: apostrophes are dropped when matching names;
"Mount X" ↔ "X Mountain" and "X Fall" → "X Falls" aliases, added only
where no feature has that name (otherwise "Mount Wilson" turned into
"Wilson Mountain"); when the best centre fails the gate, the best centre
in the state the page names most is used; "Crowders Mountain State Park"
in a title gives the peak Crowders Mountain as the destination; and a
small park or preserve named in the title (qrank < 1000, not national,
wilderness or provincial, with a real name beyond "conservation area")
becomes the hike, grouped as a `park` group. Two looser gate rules
(accepting rare names, or a title name with ≤ 2 features) were tried and
dropped: they let in generic phrases like "Above the Rim" (Texas) and
"Roadside Attraction".

Effect on two batch-1 partitions (24k pages): located 14,256 → 15,066
(+6%), hikes 4,538 → 4,818 (+6%; 286 gained, 6 lost). About 70–75% of a
sample of gained hikes were right, about the same as the table overall.
Most come from small parks and preserves, plus the apostrophe fix
("Queens Garden Trail", "Sam's Knob").

### Re-run with the fixes, and authentic sites only (2026-11-08)

Both batches went back through Stages 7–9 with the site-filter and
extraction fixes above. Batch 1: `site-content-run-3` (5,544 sites,
478k URLs) and page-pass run 42463103937 (its first attempt lost one of 32
partitions to a dead runner and published `page-pass-run-3` without it;
the re-run's complete outputs are the run's artifacts). Batch 2:
`site-content-run-4` (977 sites, 196k URLs), `page-pass-run-4`,
`extract-run-8`. Combined: `extract-run-9`; with the exclusions below,
`extract-run-10` (the current hike table).

| | `extract-run-7` | `extract-run-9` | `extract-run-10` (authentic only) |
|---|---:|---:|---:|
| hikes | 68,129 | 92,963 | 46,863 |
| from personal sites | 10,633 | 20,869 | 20,252 |
| personal share | 16% | 22% | 43% |
| hike groups | 22,663 | 30,231 | 16,282 |
| groups with 10+ sites | — | 770 | 461 |

Personal sites with hikes doubled (780 → 1,607). Most of the other gain
came from large trail databases the looser site filter now let in:
hiiker.app (9.8k), trailrunproject.com (7.8k), theoutbound.com (6.8k),
mtbproject.com (4.8k, often in the wrong state), fatmap.com,
powderproject.com (skiing).

The hike table is for linking authentic, human-written posts to hikes, so
**aggregators, trail databases, generated directories, content farms and
non-hiking sites are excluded** at the hike table. They are listed with a
reason in `data/excluded_sites.csv` (73 sites, chosen by hand from every
site with ≥ ~30 hikes); `hikes.py` reads it and reports drops per reason.
Personal sites, clubs, park agencies, magazines and small guide sites
written by people (hikespeak.com, climb13ers.com, nynjtc.org) stay. The
exclusions drop half the hikes but only ~600 from personal sites (all
onlyinyourstate.com, a listicle site the classifier calls personal).
`hikes_report.md` now lists the 40 largest organisation sites so new
aggregators can be spotted and added on each run.

Watch list (sites, authentic only): Panorama Ridge 10 (8 personal), La
Cloche Silhouette Trail 3, Green Gardens Trail 3, Tonquin Valley 1; "Hike
#25: The Crack" still ungrouped (title lacks "Trail").

Note: `site_kind` calls several one-person sites "organisation"
(dwhike.com, hikespeak.com, gjhikes.com), so the personal share is a lower
bound.

### Precision of the hike table (`extract-run-10`, 2026-11-08)

150 random hikes checked by hand from title, URL and the linked features:

| verdict | hikes | share |
|---|---:|---:|
| right post, right feature | 108 | 72% |
| right area, secondary feature ("Lake Ramona via Lake Poway" → Lake Poway) | 13 | 9% |
| wrong feature | 11 | 7% |
| not one hike (road trips, news, an elopement, a lodge page, gravel rides, a "year of hiking" recap) | 18 | 12% |

Of the pages that are one hike, 92% link to the right feature or a nearby
one. Most wrong links are a **common name resolved in the wrong state**
when the page's real place isn't confidently found: Ozette's Sand Point →
Oregon, Ausable Chasm's Elephant Rock → Yosemite, DuPont's Triple Falls →
Arkansas, Kaena Point's "Coastal Trail" → Pennsylvania, Torrey Pines'
Yucca Point → Kings Canyon. Non-hike pages are the larger problem; a few
whole sites (gravel-bike routes, an elopement photographer, a radio
station) were added to `data/excluded_sites.csv`.

### Link file

`hikes.py` writes `links.tsv.gz` for showing posts on a feature's page:
one row per (OSM feature, post) with `osm_id, primary, role, feature,
feature_kind, state, group_id, url, title, site, site_kind, page_type, date,
distance_mi, gain_ft`. A post links to the feature its group is about
(`primary = 1`, including features merged into that group) and to the other
destinations, trails and parks it names (`primary = 0`). URL variants
(http/https, `www.`, `/index.php/`, trailing slash) count once per feature.
On `extract-run-10`'s pages: 64k links across 20k features.

### Sanity check: two hand-built enthusiast sites (2026-11-08)

bobspirko.ca (hand-built HTML scramble reports) and benmassey.ca
(WordPress) are known blogs. Both pass the seeds (ranks 679 and 823) and
reach the hike table, but the pipeline saw a fraction of them. Looked up in
all 126 crawls with a CDX index (2013–2026):

| | bobspirko.ca | benmassey.ca |
|---|---:|---:|
| unique pages in Common Crawl, 2013–2026 | 1,129 | 92 |
| seen in the 5 crawls harvested (2022–2026) | 358 | 44 |
| slug-matching candidates | 118 | 15 |
| kept by the page pass | 116 | 15 |
| hikes in `extract-run-11` | 93 | 10 |

Two losses, both before the content stages (which kept 98%):

1. **Only recent crawls are harvested.** Bob Spirko's site was crawled
   most in 2014–2021.
2. **The slug filter misses CamelCase paths**
   (`/OtherScrambles/BC/EmeraldPeak/EmeraldPeak.html`): 421 of 1,129 pages
   matched, even across all crawls.

Fixes in `site_report.py`: slug matching splits CamelCase
(`emerald-peak`); pages with no language tag (crawls before 2018-39) count
as English when the site's tagged pages are; and `mode = all` keeps every
English page. `filter_candidates.py --all` (site-content
`all_candidates`) stops site-content dropping pages whose slug doesn't
match; without it, posts-mode candidates were silently reduced to slug
matches. On the two sites' full archive: 444 candidates (old code) → 850
(CamelCase) → 1,209 (all mode).

**Deep re-harvest** of every site with hikes in `extract-run-11`, over 14
crawls (the latest of each year 2014–2021, plus 2022-27, 2023-40, 2024-26,
2025-26, 2026-34 and 2026-43): the 1,603 personal sites
(`data/deep/personal_sites.txt`) in `all` mode, and the other 2,469
(`data/deep/other_sites.txt`) in `slug` mode, so large tourism and
government sites don't swamp the page pass. `site-urls` takes them with
`site_source = file:<path>`.

Personal-site harvest (`site-urls-run-9`): 1.62M candidates, but most from
eval-list sites the plan step always adds (nationalgeographic.com,
tripoto.com, theoutbound.com) and from big sites mislabelled `personal`
(outsideonline.com, gearjunkie.com). Real enthusiast sites are small
(median 239 candidates, p90 1,246; bobspirko.ca 491). `filter_candidates.py`
(site-content inputs) gained `--sites` (keep a list), `--max-unmatched`
(per-site cap on non-matching pages, chosen by URL hash; 1,000 here) and
`--skip-urls` (pages already scored by earlier page passes), and always
drops `data/excluded_sites.csv`. Result: 601k new pages from 1,603 sites;
site-content runs with threshold 0 so known-good sites aren't re-judged on
8 random pages.

Platform blogs: site-content (`site-content-run-6`, 8 pages per blog, all
candidates) kept 155 of 1,890 blogs, 71,749 URLs.

**Results (2026-11-09).** Page passes 5 (platform blogs) and 6 (personal
deep) were published from their artifacts with `publish-run`, then
extracted, each combined with everything before it:

| | hikes | personal | bobspirko.ca | benmassey.ca |
|---|---:|---:|---:|---:|
| `extract-run-11` (batches 1–2) | 46,725 | ~20,100 | 93 | 10 |
| `extract-run-12` (+ platform blogs) | 50,270 | 22,366 | 93 | 10 |
| `extract-run-13` (+ personal deep) | 87,544 | 60,163 | 384 | 21 |
| local rebuild with the fixes below | 83,543 | 52,256 | 384 | 21 |

Watch list (hikes, before → after): Panorama Ridge 15 → 29, Tonquin
Valley 4 → 4, La Cloche Silhouette 5 → 7, Green Gardens 4 → 5, Half Dome
179 → 301, Angels Landing 224 → 357.

A 60-row sample of the new personal hikes showed a new failure: about a
fifth were blog **date archives** (`/2017/07/`, `/2016`,
`2010_12_01_archive.html`), which the all-mode harvest brings in.
`hikes.py` now drops them by URL (`DATE_URL`; ~4,000 rows, none from the
two enthusiast sites). Title rules ("July 2017") were tried and dropped:
they also hit real posts with date titles.

Second fix: a site can be in several page passes with different
`site_kind`s (172 sites, almost all `personal` in batch 1–2 and
`organisation` in the deep pass, whose non-post pages score lower).
`hikes.py` used to take whichever file came last. It now takes the kind
from the pass that fetched the most of the site's pages, so the result no
longer depends on file order. Neither label is reliably right on these
sites (americanalpineclub.org and outsideonline.com are organisations;
stephabegg.com and denverdavis.com are personal); a better personal model
is future work.

A 50-row sample of the rebuilt table's new hikes: ~68% right (the
`extract-run-10` sample was 72%). Misses: features resolved in the wrong
region (Hilo's Rainbow Falls → California, Kilimanjaro → Mount Whitney),
travel listicles ("Six Mistakes to Avoid at Lake Louise"), and American
Alpine Club accident reports (climbing; 777 rows). americanalpineclub.org
and australianhiker.com.au were added to the exclusions. Extract releases now
also carry `links.tsv.gz`.

### Batch 7 and duplicate pages (2026-11-09)

Page pass 7 (the other sites, `site-content-run-8`, 210k URLs) and
`extract-run-14` brought the table to 97,222 hikes (52,636 personal).
dwhike.com went from 1,175 to 4,178 rows: it is a SmugMug gallery site, and
every photo page (`/Gallery/i-AbC123`) repeats the gallery's text.
`hikes.py` now keeps one row per page, counting http/https and %-escape
variants once. Where the last path segment is an id (`i-AbC123`, `147`), it
keeps one row per (site, title, parent folder). The id rule matters:
grouping by title alone merged a blog whose every post is titled with the
blog's name. Local rebuild: 93,086 hikes (52,471 personal); dwhike.com 583.

## Recall on rare hikes (2026-11-09)

**Question:** how much of what's written about lesser-known hikes do we
find? Name counts can't answer it: Barbara Martin's seven Tonquin Valley
days are in the table, filed under Lectern Peak, Surprise Point, Amethyst
Lakes and Chrome Lake.

**Test set.** `data/eval/recall/rare_hikes.tsv` lists 33 lesser-known
hikes, one or two per state or province, each with an area box.
`data/eval/recall/reports.tsv` holds 363 trip reports and guides found by
web search (280 personal, 83 organisation), excluding aggregators. WordPress
`?p=` shortlinks were resolved to permalinks.

**Method.** `cmd/urlcdx` checks each URL against the 14 crawls we harvest:
it looks up the one CDX block that would hold the URL. `scripts/recall_trace.py`
then follows the URL through seeds, site-urls verdicts, harvested
candidates, site-content, page pass, extraction and the hike table. A
report counts as found when a row for it lies inside the hike's box. As a
check, no URL that the pipeline harvested was reported missing by `urlcdx`.

**Result (`extract-run-13` plus the date-archive fix), personal pages:**

| stage | pages |
|---|---:|
| ground truth | 280 |
| not in any of the 14 crawls | 162 (58%) |
| in Common Crawl | 118 |
| found, right place | **37 (31% of crawled)** |

Where the 81 crawled personal pages were lost:

| loss | pages | examples |
|---|---:|---|
| site judged off_topic / non_english / shop | 23 | climber.org, 14ers.com trip reports, zenfolio, university home pages; 8 Blogger blogs judged as the single site `blogspot.com` |
| dropped at site-content | 18 | `/mount-northover/`, `/mount-cabot/`, `/kings_throne.html`, `/second-attempt-at-carlton-peak/`: slug has no hiking word and no state, so `slug_match = 0` (slug mode drops them; all mode's 1,000-page random cap drops most) |
| page not in harvested captures | 16 | site harvested in 1–3 crawls but the page is only in others (sonnybou.ca: harvested in 2026-34 only; its Northover, Siyeh and Emory pages are in 2022–23); block cap samples big sites |
| seeded, never harvested | 6 | below the slug-scan score cut-off (myoverdueadventures.com scored 20 < 50) |
| wrong place | 6 | Top of the Giant → Marie Louise Lake; Willow Lake → Kit Carson Peak (nearby, outside the box) |
| extraction | 10 | multi-day and long reports typed `other`, `area_guide` or `list_article` |

Organisation pages: 43 of 83 crawled, 3 found, mostly lost to site
verdicts and harvest sampling. 11 of the 33 hikes have no report found.

**Fix candidates, by size:**

1. **Name-aware slug match.** Count a slug as matching when it contains a
   Hiker Atlas name (`slug_scan.load_names`). Recovers 9 of 10 sampled
   site-content drops; adds 14% more candidates on the truth sites, with
   noise to filter ("rock climbing", "north face", state names).
2. **Harvest every kept site in all 14 crawls**, not just the deep
   personal list; cap pages, not blocks.
3. **Host-level verdicts for Blogger/WordPress blogs** found in the
   domain graph, so one blog isn't judged as `blogspot.com`.
4. **Forum and club trip reports:** 14ers.com, climber.org,
   backpackinglight.com, oregonhikers.org are off_topic or sampled away
   at site level, but their trip-report sections are what we want.
5. Lower the slug-scan cut-off for personal-looking sites.

**Fix 1, name-aware slug match (done).** `scripts/slugnames.py` finds Hiker
Atlas names in URL slugs. A name qualifies if it ends in a landform word,
starts with mount/mt/mont/lake, or is a trail name minus "trail"
("kings throne"). Regions, generic-only names, and a short stoplist ("co
op", "water treatment") are excluded. `site_report.py score --index` and
`filter_candidates.py --index` use it: in site-urls, a name in the slug makes
the page a match. In site-content, rows with `slug_match = 0` whose URL names a hike are kept
in slug mode and skip all mode's random cap. Both workflows download the
index by default (`index_url`). On the truth sites' candidates it adds
matches for 11% of previously unmatched rows, mostly real (Sevenmile Mesa,
Phantom Falls, Oyster Dome), and recovers 10 of 10 sampled misses. On one
partition of the deep personal harvest it keeps ~2,000 more pages that the
cap dropped. site-content now takes several site-urls releases at once
(comma-separated). Rows are written in one column order and de-duplicated
by URL, so existing slug-mode harvests can be re-filtered in one run.

First run (`site-content-run-9`: site-urls runs 4, 6, 9 and 10, threshold
0.35, scored pages skipped): 158,857 rows matched only by name; 960 of 9,538
sites kept, 102,598 URLs. Wrong for sites already kept: their best pages
were scored before, so the 5-page sample came from leftovers and scored low.
Only 14 of 1,488 deep personal sites passed (bobspirko.ca 0.18, kuresman.com
0.06), and 4 of ~23 traced misses came through. Second run: the same
harvests limited to `data/deep/kept_sites.txt` (6,437 sites kept by
site-content runs 3–8) at threshold 0. The page pass takes both releases
at once (`site_content_release` is comma-separated; URLs de-duplicated).

Skipping by `pages.tsv.gz` was also wrong: it lists only kept pages, so
every page a pass rejected was fetched again. The kept-sites re-filter
(`site-content-run-10`) kept 418k URLs, but only 99k are name-only matches.
Most of the rest had been tried and rejected before. Page passes now also
publish `scored.tsv.gz` (every page tried, with its relevance), and
`scripts/scored_urls.py` builds the skip list from it, or from the
fetched page text of older releases. site-content and page-pass both use
it (`skip_pages_from`).

**Fix 1 results (2026-11-09).** `site-content-run-10` (kept sites,
threshold 0) plus run 9. One page pass over both (`page-pass-run-8`):
142,590 new pages after skipping 241,043 already tried; 64,042 kept from
1,622 sites. Combined with every earlier batch (`extract-run-15`, local
rebuild): **105,318 hikes, 56,616 personal**. Recall on crawled personal
pages: **37 → 42 of 118 (31% → 36%)**. Newly found: Mount Northover
(giantsgate.com), Yellow Aster Butte (two kuresman.com reports), Medicine Bow
Peak (adventr.co) and Carlton Peak (thirdeyemom.com). Eight more traced
pages now get past site-content but are lost later: page type `other` (3),
no location (3), wrong place (2). That makes extraction the next
bottleneck for these sites, along with site verdicts and harvest coverage.
backpacker.com is now the largest organisation site (1,588 rows).
Kept (user decision).

**Fix 2: unseeded blogs and trip-report sections (2026-11-09).** The
trace's "blogspot.com judged off_topic" bucket was misleading. Those 7
Blogger blogs were never seeded: they link to no hub, so the host graph
skips them, and the slug scan covered only CC-MAIN-2026-34. Their pages
are almost all in the 2017–2019 crawls. Slug scans of 2017-51, 2018-51,
2019-51 and 2021-49 (about 20 minutes each) are meant to find blogs like
them.

Forum and club trip reports (14ers.com, climber.org, oregonhikers.org,
backpackinglight.com, ...) sit on sites that are hubs, or that look off topic
as a whole. `data/sections.tsv` lists their trip-report sections as host +
path prefix. `sitecdx` reads only those sections (cap
`-max-blocks-per-section`, default 200), `urlrules` keeps their query-string
URLs (`?trip=`, `viewtopic.php?t=`), and `filter_candidates --sites`
matches a section's site. In a CC-MAIN-2023-40 sample, wta.org, nwhikers.net,
trailjournals.com and clubtread.com had no captures (WTA appears to block the
crawler); they stay listed for other crawls.

**Fix 2 results (2026-11-10).**

| stage | run | sites | pages / URLs |
|---|---|---:|---:|
| slug scans | `slug-scan-run-4..7` (2019-51, 2018-51, 2017-51, 2021-49) | | |
| new sites picked | `scripts/pick_slug_sites.py` → `data/deep/slug2_sites.txt` | 14,956 (5,618 blog-platform) | |
| harvest, `posts` mode, 4 old crawls | `site-urls-run-11` | 12,241 hiking/mixed | 7.36M URLs |
| sections, `all` mode, 14 crawls | `site-urls-run-12` | 14 sections | ~102k section URLs |
| site content (new sites, 0.35, ≤ 200 unmatched/site) | `site-content-run-11` | 2,150 kept | 236,473 |
| site content (sections, threshold 0) | `site-content-run-12` | 47 | 159,337 |
| page pass | `page-pass-run-9` | 2,187 (1,328 personal) | 222,758 kept |
| extraction, combined with runs 8, 11–15 | `extract-run-16` | | |

- `pick_slug_sites.py`: best score across releases ≥ 50, or ≥ 15 on blog
  platforms; skips known, seeded and excluded sites, and country domains
  other than .ca/.us (no language field before 2018-39). `slug_scan.py`
  retries range reads cut off mid-transfer (2018-51 failed four shards with
  `IncompleteRead`); `slug-scan-run-4` (2019-51) has 15 of 16 shards.
- A random 30 of the kept new sites: about 22 enthusiast blogs
  (nh48.wordpress.com, 900-miles.blogspot.com, heberhiking, serialhiking,
  wyeastblog, hikingwithtj).
- **Query-string duplicates.** `extract-run-16` gave 14ers.com, hikearizona.com,
  highsierratopix.com, oregonhikers.org and nwhikers.net one row each:
  `hikes.py`'s duplicate-page key dropped the query string, so every
  `tripreport.php?trip=N` was the same page. The key now keeps query
  arguments except view-only ones (thread page, filters, highlights,
  tracking), and forum topics (`viewtopic.php?`, `showthread.php?`) count
  once per (site, title), because each reply has its own `?p=` link.
  maxtrails.com (generated "campgrounds near X trail" pages, 1,357 rows) is
  excluded.
- **Hike table** (`extract-run-18`, both fixes; run 17 lost a partition to the artifact-list race, now fixed): 105,318 → **154,482 hikes**,
  personal 56,616 → **95,098**. 1,857 sites are new, with 36.6k rows (25.2k
  personal: explor8ion, pahikes, bootsonthetrail, sghnh48, verndewit...).
  Sections: hikearizona 5,098, 14ers 4,492, highsierratopix 1,979,
  climber.org 1,298, backcountrypost 965, postholer 1,000, cascadeclimbers
  723, oregonhikers 524, vftt 178, nwhikers 112. Watch areas: La Cloche
  13 → 25, Killarney 19 → 31, Tonquin 12 → 15, Panorama Ridge 123 → 134.
- **Recall** (personal reports in CC): 41 → 44 of 117 (38%). Newly found:
  two climber.org reports (Tallac, Wheeler Peak) and virginiatrailguide's
  North Fork Mountain. Several more now get through harvest but are lost
  later: three climber.org Willow Lake reports and dirtinmyshoes' Little
  Wild Horse resolve to the wrong place (a different "Willow Lake"; a
  generic "Slot Canyon" trail), and the 14ers Medicine Bow report is typed
  `other`. Most still-missing blogs are not in the scanned crawls' indexes
  at all. Next candidates: disambiguating common names by the other
  features on the page (Kit Carson Peak, Challenger Point), and fix 3
  (every kept site across all 14 crawls).

**Name variants in extraction (2026-11-10).** The wrong-place reports were
mostly names the index didn't match, not bad disambiguation: OSM has "Kit
Carson Mountain" where climbers write "Kit Carson Peak" or "Kit Carson
Pk.", and "Little Wild Horse Canyon Trail" where pages write "Little Wild
Horse Canyon". `extract.py` now:

- reads Pk. / Pt. / Mtn(s) as peak / point / mountain(s);
- matches "X Mountain" as "X Peak" and back, for well-known peaks only
  (qrank >= 1000; an obscure Rider Mountain in South Carolina matched
  "Trail Rider Peak", a Washington Pompey Peak moved a Virginia report);
- matches "X Canyon/Gulch/Wash/Narrows/Gorge/Coulee/Draw Trail" as "X
  Canyon" etc. Two-word ones ("Antelope Canyon", "Deadmans Canyon") often
  mean an unindexed canyon elsewhere, so they vote for the region with one
  mention and no title bonus.

Tested on the 87 recall-test pages with text (fetched from their WARC
records) and on page-pass-run-9's 222,758 pages, both against the public
index of 2026-11-10 (it changes; compare runs on one index):

| | recall pages right | wrong | corpus gained | moved | lost |
|---|---:|---:|---:|---:|---:|
| main | 65 | 21 | | | |
| this change | 75 | 12 | 1,554 | 1,296 | 119 |

Of 40 sampled moves about 15 are clearly better (14ers "No Name" → Jagged
Mountain, The Wave → Coyote Buttes, Mount Sheridan → Colorado), about 5
worse, the rest neutral. Gains are mostly 14ers and forum reports.

An ambiguity weight (a name's vote divided by 1 + ln(#features)/2, so
"Willow Creek" counts less than "Crestone Needle") was tried and dropped:
one more recall page, but 4,014 pages moved and 1,328 lost their location,
many correct ones (Mount Wilson, Longs Peak, Mount Harvard). Pages that
write only "Kit Carson" and "Challenger" can still go wrong.

Published as `extract-run-27`: page passes 3-8 re-extracted one at a time
(`extract-run-19..23` and `-25`; `-24` and `-26` each lack partition 14 after
a connection reset while downloading the index, fixed by `curl
--retry-all-errors`), then combined with page-pass-run-9. Same 222,758 pages
as `extract-run-18`:

| | extract-run-18 | extract-run-27 |
|---|---:|---:|
| hikes | 154,482 | 157,446 |
| personal sites | 95,098 | 97,213 |
| with trail | 73,616 | 78,041 |
| recall test, personal, right place | 44 / 117 | 54 / 117 |
| recall test, wrong place | 12 | 8 |

New finds: six Little Wild Horse Canyon reports (four had no location,
two the wrong one), Aravaipa Canyon (desertlavender), Willow Lake
(climber.org, rickcrandall), the Escarpment (seekinglost). Top of the Giant
on northernontario.travel moved off: its trail's OSM centroid is just past
the 25 km test radius, as in the local test.

**One-hike pages typed "other" (2026-11-10).** 20 recall-test pages were
lost to page type: one-hike pages the type model (trained on 300 labelled
pages) calls `other`, `list_article` or `area_guide` by a small margin, with
trip report + trail guide together at 0.35-0.5 ("Hiking Mount Cabot", "Mt
Cammerer on the Appalachian Trail", "Wedgemount Lake Hike Guide").
`extract.py` now takes the likelier hike type when p(trip_report) +
p(trail_guide) >= 0.3, the title names a trail, peak, lake, waterfall or
cliff the page resolved to, and the title doesn't read as many hikes, an
area, gear, news, books/maps or a forum question ("...?"). Such records
carry `type_rule = title_hike`.

On page-pass-run-9 (local, public index): 9,493 of 395k pages change type
(mostly `other`), and the hike table grows 50,794 -> 56,542 (+2,822 from
personal sites). Biggest gains: hikearizona photosets (1,280), trails.com
(1,191), 14ers.com (484), highsierratopix (308). In a sample of 60 new rows
about 55 are real hike pages; the rest spam (a florist, an eye clinic), a
road trip and a GPS-track aggregator. Before tightening, forum questions,
a news thread, book and topo-map pages and an event calendar got through.
On the recall pages with text it fixes 7 of 14 (Tonquin on
inafarawayland, Wedgemount, Jacks River, Emory Peak, Mount Cabot, Mt
Cammerer, Shining Rock) and changes no other recall page's type. Still
missed: list-style titles ("6 Things to Know about the Mt. Tallac Hike"),
titles that name only a region (Aravaipa, the Pecos), and pages whose hike
probability is under 0.3.

Published with page passes 3-8 re-extracted (`extract-run-28..33`) and
combined in `extract-run-34`, with trails.com excluded (a Demand Media
content farm, ~1,500 rows):

| | extract-run-27 | extract-run-34 |
|---|---:|---:|
| hikes | 157,446 | 178,360 |
| personal sites | 97,213 | 102,852 |
| organisation sites | 60,184 | 75,417 |
| recall test, personal, right place | 54 / 117 | 58 / 117 |
| recall test, organisation, right place | 3 | 5 |
| recall test, wrong place | 8 | 9 |

Recall gains: Tonquin (inafarawayland), Emory Peak, Mount Cabot, Jacks
River, Shining Rock and Mt Cammerer (ashevilletrails); Wedgemount
(twooutliers) now reaches the table but in the wrong place. Most of the
organisation gain is real (americansouthwest.net +1,981, backpacker.com
+811, digthefalls, socialhiker hike logs), but five generated sites came
through and are now excluded: naturalatlas.com, mountainzone.com (a stub
page per summit), trekweather.com, alaska.guide and pixeoapp.com (~1,600
rows). Rebuilt without them as `extract-run-35`: 176,760 hikes (102,852
personal, 73,817 organisation); the recall test is unchanged.

## Route links (2026-11-11)

Which pages follow each Hiker Atlas curated route (hikeratlas/routes).
`scripts/route_links.py` (workflow `route-links.yml`) reads a routes build
(`index.json` plus its bundle: per park, day hikes, base camps with their
approach and from-camp hikes, and multi-day trips, each with destination
name, kind and position, trailhead, `via` trail names, length, gain and
line) and the combined hike table:

1. **Destination.** A route's destination is matched to a hike-group
   feature by name and position (800 m; 3 km for camps; 8 km for lakes,
   whose routes end at the shore where the trail arrives, while the hike
   table has the centre: 3 km apart for Garibaldi Lake). Candidate pages
   are those whose title names that feature. Routes that follow an OSM
   hiking route ("Garibaldi Lake Trail") also take pages naming that trail,
   and pages that extraction already matched to the route itself (the place
   index contains the curated routes, kind `hike`, id `<park>/day/<i>`).
2. **Way up.** Evidence that a page follows this route rather than another
   to the same destination: places it mentions within 300 m of the route's
   line, the route's `via` trails, its trailhead. Not evidence: trails named
   after the destination or the route ("Mount Bierstadt Trail"), long trails
   (a thru-hiker names the Appalachian Trail everywhere), trailheads named
   after either. Pages found only by a long route's name must mention a
   place along this route's line.
3. **Length.** A page distance outside 0.5-2x the route's (round trip or one
   way) cancels the way-up evidence; among routes with equal evidence, the
   one whose length fits within 0.8-1.25x wins.

A link is `route` when the evidence picks this route among the
destination's routes, else `destination` (on every route to it).

First run, routes build of 2026-11-10 and `extract-run-35`: 69,943 links,
18,068 route-specific; 4,061 of 32,077 day hikes have a destination with
pages (most curated routes go to features nobody in the corpus wrote
about), 2,100 have a route-specific link from a personal site. In a
sample of 40 route-specific links about 30 are right; the rest are races
and rides, history or news pages that name the place, and trips that only
pass through. Garibaldi: Garibaldi Lake from Rubble Creek 28 route + 80
destination links, Panorama Ridge's three from-camp routes 12-18 each.

**Routes-side changes (routes#153, basemap#265).** Route ids are
positions (`<park>/day/3`) and shift between builds. Routes now also write
a stable `key` (`<park>|day|<trailhead>|<destination>|<kind>`), which
`route_links.tsv.gz` carries as `route_key`; store links by key. They also
pass on OSM ids: `dest_osm_ids`, `from_osm_id`, `camp_osm_id`,
`name_osm_id`. POIs (viewpoints, waterfalls, huts, trailheads) have ids in
today's tiles; peaks and lakes get them with basemap#265. The matcher joins
a destination by id first and falls back to name and position when no id
matches: the same place can be two OSM features (Panorama Ridge is a
viewpoint node in routes and a peak in the place index). Trail ways keep
names only (their ids are in debug tiles only).

An id match must still be within the name-match distance: by id alone,
every page naming Lake Superior or Lake Tahoe joined every route ending on
their shores (+26,000 links, nearly all wrong). With routes#153 built
(routes bundle 42952774778, every peak, lake, viewpoint, waterfall, hut and
spring destination has an id; 25,750 of 35,392 hikes a trailhead id):
70,448 links, 18,281 route-specific; 4,094 day hikes with a destination
that has pages (4,061 by name alone).

**Pages found by a local trail's name (2026-11-11).** La Cloche Silhouette
Trail had one link of 20 pages naming it: a page found only by a route's name
had to mention a place within 300 m of the route's line (so Appalachian Trail
pages from Maine don't join a Georgia segment), and a Silver Peak day hike
failed the length check on the 72 km route. Now, for a route that follows a
named OSM trail whose name is neither generic ("Shelter Trail") nor a long
trail's, a page anywhere within 5 km of the line counts, and one whose length
doesn't fit becomes an `about` link instead of being dropped. Routes found
only by their title ("Bright Angel from Plateau Point Trailhead") keep the
strict rule. La Cloche: 12 route links and 8 about. Overall, on routes
bundle 42965334081: route-specific links 18,280 -> 30,403, routes with
something to show 6,139 -> 8,501; about 22 of 30 sampled new links right
(the misses name the trail in passing: an FAQ page, a rock-climbing page).

**Fix 3 harvest (2026-11-11).** site-urls-run-13 (posts mode, 14 crawls, 2
blocks per site) over `redo_sites.txt` returned 39.1M candidate rows; one
partition's 2.27M rows held 260k unique URLs with a hiking word and 204k that
name a Hiker Atlas place, mostly junk: newspapers, directories and
attorneys' listings whose URLs name streets ("Willow St", "Old Town").
Neither the slug filter nor site-urls' verdicts separate them. So the sites
are split: `redo_kept.txt` (4,442 sites a page pass already kept: vetted,
and re-judging them samples leftovers and scores low) and `redo_other.txt`
(12,197, judged by site-content as usual). `filter_candidates.py` gains
`--names-only` (only URLs naming a Hiker Atlas place) and `--max-per-site`
(by URL hash), both site-content inputs. Part 1, kept sites, names only, at
most 50 per site: ~95k URLs before leaving out pages already tried.

Results. Part 1 (`site-content-run-13`): 52,746 URLs from 2,900 sites after
leaving out pages already tried. Part 2 (`site-content-run-14`, judged by 5
pages at 0.35): 729 of 11,896 sites kept, 18,410 URLs; the rest were the
newspapers and directories. `page-pass-run-10` fetched 71,156 pages and kept
25,567 (10,927 personal). Combined as `extract-run-37` (page passes 3-10):
186,203 hikes (from 176,760), 108,996 personal (from 102,852), 7,773 sites
(from 6,816). Recall: 59 of 117 personal test reports (from 58; a Wolverine
Peak report on livinginak.blogspot.com). As estimated, fix 3 adds pages more
than recall: most "page not in harvested captures" misses are on sites with
no kept pages, which this harvest didn't cover. `route-inputs-run-2` carries
the new table to basemap's routes build.

Part 3, blogs. The recall misses left after fix 3 have mixed causes (pages
never captured, sites never selected, personal pages on university hosts),
and a pass over every small site with hike-name URLs and nothing kept would
be ~12k sites and millions of pages. One cause is cheap to fix: part 2 drops
personal blogs whose 5 sampled pages average under 0.35 because the blog is
about more than hiking (desertsurvivor.blogspot.com 0.159, travisnovitsky
0.212, both with recall-test reports). `redo_blogs.txt`: 841 blog-platform
sites part 2 judged at 0.15-0.35, run again without a site bar (names only,
at most 50 a site); the page model judges each page.

Results: `site-content-run-15` kept 17,291 URLs from the 841 blogs;
`page-pass-run-11` kept 4,145 pages (3,128 personal) from 825 of them.
Combined as `extract-run-38` (page passes 3-11): 188,202 hikes (+1,999),
110,541 personal (+1,545), 8,407 sites (+634). Recall: 61 of 117 personal
(desertsurvivor's Wheeler Peak report). travisnovitsky's Top of the Giant
report stays out: its URL is `top-of-giant.html`, which the place-name
filter doesn't match to "Top of the Giant", and the blog hit the 50 cap.

**Stop words in slugs (2026-11-11).** Slugs often drop stop words
(travisnovitsky's `top-of-giant.html` for Top of the Giant). Matching every
index name with stop words removed from name and slug adds 2,567 matches per
2.27M URLs, nearly all phrases ("fourth of july" 544, "out and about", "sound
of music": OSM has trails by those names); limited to landform names, 120,
also phrases ("down by the river", "mind the gap"). `slugnames.py` now does
it only for `data/gazetteer/stopword_names.txt`: names with stop words that
have a hike group of 3+ sites (La Plata Peak, Mount of the Holy Cross, Trail
of Ten Falls, Skyline to the Sea, Hall of Mosses), minus a dozen dropped by
hand as phrases or brands (Sea to Summit). ~91 more URLs per 2.27M, mostly
on target. `filter_candidates.py --stopword-names-only` re-runs old harvests
for just those.

Re-run over site-urls runs 2 and 4-13 (run 1 has no candidate files):
53.6M candidate rows -> 1,984 new URLs from 696 sites (`site-content-run-17`)
-> `page-pass-run-12` kept 456 -> `extract-run-39`: 188,406 hikes (+204),
110,633 personal, 8,516 sites (+109). Recall unchanged at 61/117:
travisnovitsky's Top of the Giant report now reaches extraction, located in
Sleeping Giant PP, but has no destination or trail, so hikes.py drops it.
OSM calls the trail "Top of the Giant Trail", and extraction drops a trailing
"Trail" from a name only for canyon trails.

**Trail names without "Trail" (2026-11-11).** `data/gazetteer/trail_aliases.txt`
lists trails extraction also matches without the trailing "Trail". It is
built from extract-run-39's trail groups with 3+ sites (1,524 names), minus
bases that are other features' names. A base is kept if page text (a
quarter of page-pass-run-9) writes the full "X Trail" at least 30% of the
times it writes X. Bases mentioned fewer than 5 times are kept only with 3+
words. A few generic ones are dropped by hand. That leaves 434, e.g. Top of
the Giant, South Kaibab, Alum Cave, Siyeh Pass, Tuckerman Ravine. Dropped as
phrases: High Sierra (1,642 full vs 66,324 bare), West Coast, Big Sur, Old
Growth, Fourth of July, Trans Canada (the highway). On the recall pages:
Siyeh Pass x3, Top of the Giant (Sleeping Giant, travisnovitsky), Art Loeb.
On 65k pages of page-pass-run-9: 111 gain a location, 5 lose one, 130 move
>25 km, mostly within a state.

Published as `extract-run-49`: page passes 3-11 re-extracted one at a time
(`extract-run-40..48`), then combined with page-pass-run-12. 190,444 hikes
(+2,038 over extract-run-39), 111,713 personal, 95,943 with a trail (from
92,965). Recall: 65 of 117 personal (from 61), 8 organisation (from 5), 7 in
the wrong place (from 10). New finds: Top of the Giant x5 (thelostgirlsguide,
hikebiketravel, travisnovitsky, northernontario.travel x2) and Siyeh Pass
(followthatnerd, glacierguides); hike734's Siyeh report now reaches the
table, but in the wrong place.

### Route links in production (2026-11-11)

basemap's routes build links trip reports to the routes it has just
curated, so links always match the build:

1. cc-crawler's `route-inputs.yml` publishes `route-inputs-run-<n>`:
   `hikes.tsv.gz`, `hike_groups.tsv.gz` and `route_features.jsonl.gz`
   (each hike-table page's mentioned places, 15 MB; `route_links.py
   --write-features`). Run it after each combined extraction.
2. basemap's `routes.yml` checks out cc-crawler (public), downloads the
   newest `route-inputs-run-*` release and runs `route_links.py --features
   ... --bundle-dir`, then uploads `reports.json` and `reports.bin` next to
   the routes bundle (`routes/<namespace>/`).
3. The website reads them like the routes bundle: per park, route key ->
   `{reports, about}`; at most 8 of each, personal sites first, at most two
   per site.

`data/excluded_urls.csv` (url,reason) lists pages never to link; it is read
from cc-crawler's main branch at each routes build.

First bundle (routes bundle 42952774778, `extract-run-35`): 2,373 parks,
6,139 routes with something to show, 1.8 MB.

## Runner budget

The account is on GitHub's free plan: **20 concurrent jobs, shared with the
owner's other repositories**. cc-crawler jobs run for hours, so they must
not take all the slots.

- Matrix caps: page-pass 6, extract 4, slug-scan 4, site-urls 4,
  site-content 4, graph-seeds 2.
- Run **one** heavy cc-crawler workflow at a time (page-pass, site-urls,
  site-content, extract, slug-scan, graph-seeds); queue the next only when
  the previous finishes. That keeps the repo at ≤ 6 of the 20 slots.

**Release publishing 403s.** `gh release create --target "$GITHUB_SHA"`
fails with `HTTP 403: Resource not accessible by integration` whenever
`main`'s workflow files changed after the run started: creating a tag on a
commit whose workflow files differ from the default branch needs the
`workflows` permission, which `GITHUB_TOKEN` can't have. It hit page-pass
run 2 three times and site-urls run 10. Releases are now tagged at the
default branch head (no `--target`) and the notes record the run's commit.
Runs dispatched before a workflow change can still fail; their outputs stay
in the run's artifacts, and the `publish-run` workflow (input: the run id)
creates the release the run should have made from them, under the same tag.

## GitHub Actions layout (proposed)

| Workflow | Trigger | Jobs |
|---|---|---|
| `graph-seeds.yml` | manual and scheduled (new graph release) | Stage 1–3: single job, streams the edges file |
| `index-candidates.yml` | manual and scheduled (new crawl in `collinfo.json`) | Stage 4–5: matrix over seed-domain batches or index shards |
| `snowball.yml` | manual | Stage 6 |
| `spot-check.yml` | manual | Stage 7–8: matrix of polite fetchers |

Conventions:

- **All jobs run on Arm** (`runs-on: ubuntu-24.04-arm`, free for public repos).
  Any binaries or tools we add must have arm64 builds.
- Small, reviewable inputs (`data/*.csv`) live in git. Large outputs go to
  **Release assets** (≤ 2 GB each) or a Hugging Face dataset, never git.
- Every output records the graph release and crawl ids it came from.
- HTTP clients use a descriptive User-Agent, backoff on 503, and capped
  concurrency.

## Proposed repo layout

```
docs/DESIGN.md          ← this file
data/hubs.csv           ← hub domains, weights, categories
data/platforms.csv      ← blog-platform parents where site = host
data/eval/              ← known_blogs.csv, negatives.csv
data/gazetteer/         ← (generated or downloaded) place names
src/                    ← pipeline code (language TBD; likely Python + DuckDB, plus a small Go/Rust streamer if needed)
.github/workflows/      ← one workflow per stage group
```

## Status

| Stage | Status | Notes |
|---|---|---|
| 0: Reference data and eval set | 🟨 drafted, in review | `hubs.csv`, `known_blogs.csv`, `negatives.csv` drafted; graph presence checked against `cc-main-2026-jul-aug-sep` |
| 1: Graph release and hub IDs | ✅ done | `cmd/hubscore` pass 1; latest release still missing its vertices file, so default is `cc-main-2026-jul-aug-sep` |
| 2: Edge stream and hub scoring | ✅ done | `cmd/hubscore` + `graph-seeds` workflow; first results above |
| 2b: Host-graph pass for platform blogs | 🟨 implemented | `hubscore -graph host`; first run pending |
| 3: Seed domains | 🟨 first cut | `scripts/seed_report.py`: union of 5 releases, 88.8k domains ≥ 2 hubs, 88% recall; needs gear/commerce and expired-domain filters (Stage 4 signals) |
| 4: URL index validation | ✅ first full run | all 88.7k seeds classified; 9,153 sites kept after harvest |
| 5: Candidate URL list | ✅ first full run | 2.0M candidate URLs in the `site-urls-run-4` release; **first deliverable** |
| 6: Snowball | ⬜ not started | |
| 6b: Slug scan | 🟨 first batch done | `slug-scan` workflow; 4,303 new sites → 977 after Stages 4–7 (`site-content-run-4`) |
| 7: Content spot checks | ✅ page pass run 1 | 262,586 pages from 4,403 sites, ~73% precision (est.); text archived |
| 8: WET recall sample | ⬜ not started | |
| 9: Extraction | 🟨 first pass | two batches combined, authentic sites only, in `extract-run-10`: 46,863 hikes in 16,282 groups (43% personal); page-type model (76%); `extract-run-2`: page type, OSM features and region, distance, gain, date; 64% of pages located, ~90% of those in the right state |

## Open questions

- How many domains link to ≥ 2 hubs? Is the long tail mostly blogs or mostly
  aggregators and SEO sites? (Measure in Stage 2.)
- Is one release per year enough, or would every release (roughly quarterly)
  raise recall further? The union of five already reaches 88%; the remaining
  misses are mostly never-crawled sites.
- Common Crawl's CDN rate-limits bursts with 403s (seen on graph files, the
  index, and `index.commoncrawl.org` refusing connections from some
  networks). Every reader needs retry with backoff; DuckDB's httpfs does not
  retry 403 by default.
- How should we treat general travel blogs that only sometimes cover hiking?
  (Current plan: keep them at the URL level, ranked lower at the domain level.)
- How well can `site_kind` be inferred without an LLM? Measure it against the
  labelled rows in `known_blogs.csv`.
- Path-based platforms (`medium.com/@user`): worth handling, or rare for this
  topic?
- Where should outputs live long-term: GitHub Releases or a Hugging Face dataset?

## Decision log

| Date | Decision |
|---|---|
| 2026-11-07 | Focus on **blogs** only; official pages deferred. |
| 2026-11-07 | Repo is public so Actions minutes are free; **no LLM budget** to start. |
| 2026-11-07 | First deliverable is a **URL list** (with WARC pointers for later extraction). |
| 2026-11-07 | Magazines **count**, flagged `site_kind = professional` so output can be curated for diversity. |
| 2026-11-07 | Mixed travel blogs that often cover hiking count. |
| 2026-11-07 | Listicle-style guide sites (e.g. vancouvertrails.com) are hubs, not blogs. |
| 2026-11-07 | Dropped candidate hubs: trailforks, mountainproject, komoot, npca, nationalparks.org, americantrails, railstotrails. |
| 2026-11-07 | Everything is keyed on **site** (domain, or host under blog platforms) so platform blogs (Stage 2b) slot in without rework. |
| 2026-11-07 | Found that the domain graph collapses blogspot (and presumably other private-PSL platforms) into one node; platform blogs need the host graph. |
| 2026-11-07 | Stage 1–2 implemented as a single Go binary (`cmd/hubscore`) streaming the graph over HTTPS; no DuckDB needed for this stage. |
| 2026-11-07 | All GitHub Actions jobs run on Arm runners (`ubuntu-24.04-arm`), not x86. |
| 2026-11-07 | Extraction matches names against the Hiker Atlas autosuggest index and resolves each page to **one region**, preferring no location over a weak one. |
| 2026-11-07 | Seeds come from the union of one graph release per year (2022–2026): recall 52% → 88%. |
| 2026-11-07 | Stage 4 reads the CDX (zipnum) index directly for per-site lookups; Parquet is kept for later whole-index scans. |
| 2026-11-07 | Seed discovery starts from the **domain-level web graph** using hand-curated hiking hubs. |
| 2026-11-08 | Slug-scan sites are a second batch through Stages 4–9; batches combine at the hike table. Non-hiking sites the score can't catch are excluded by hand in `hikes.py`. |
| 2026-11-08 | The hike table keeps **authentic, human-written sites only**: aggregators, trail databases, generated directories and content farms are excluded by hand (`data/excluded_sites.csv`). |
| 2026-11-08 | Runner budget: free plan, 20 concurrent jobs shared with other repos. Matrix caps ≤ 6 and one heavy cc-crawler run at a time. |

### Mentions: a popularity signal for routes (2026-11-11)

hikeratlas/basemap#281: a curated route's popularity came from its
destination's QRank, which is near zero for most trails, passes and small
lakes (no Wikidata item), so routes ranked obscure scrambles to well-known
peaks above Johnston Canyon and left out Larch Valley. `scripts/mentions.py`
counts, for each OSM id in the hike table's destination_osm_ids and
trail_osm_ids, the distinct sites with a page about it, with its name, kind
and position from route_features.jsonl.gz. Route keys (pages matched to the
routes themselves) are left out, so the routes' own picks don't feed back
into their score. basemap's routes build runs it on the route-inputs release
before curating and hands the result to routes (`--mentions`).

From route-inputs-run-3: 38,791 places, 6,705 with 5+ sites. Around Banff:
Johnston Canyon Trail 118 sites, Moraine Lake 92, Lake Agnes 48, Sulphur
Mountain 35, Larch Valley Trail 10, Consolation Lakes 10; the routes' top
picks there had 0-4 (Cirrus Mountain 1, Upper Michele Lake 0).
