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
workflow (manual dispatch), or locally with `go run ./cmd/hubscore`.

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

For each seed domain, pull its URLs from the
[columnar index](https://commoncrawl.org/columnar-index)
(`cc-index/table/cc-main/warc/crawl=…/subset=warc/*.parquet`) with DuckDB over
HTTPS. Thanks to the SURT sort and row-group statistics, a batch of domains
reads only a small slice of the index.

Columns used: `url`, `url_host_registered_domain`, `url_path`,
`content_languages`, `content_mime_detected`, `fetch_status`, `fetch_time`,
`content_digest`, `warc_filename`, `warc_record_offset`, `warc_record_length`.

Per-domain features:

- Share of URLs with **hiking slugs**: activity terms (`hike`, `hiking`,
  `trail`, `trek`, `backpack`, `summit`, `peak`, `trailhead`, `thru-hike`,
  `trip-report`, `snowshoe`, `-falls`, `-lake`, …).
- Share of slugs with **US/CA places** (gazetteer hits: states, provinces,
  national park names, multi-token peak and trail names).
- Share with **blog-shaped paths**: `/YYYY/MM/`, long hyphenated slugs, `?p=`.
- English share (`content_languages` primary = `eng`).

Promote domains with a high hiking-slug share. Demote domains where hiking is
a tiny fraction, such as general travel blogs. Keep those too, because their
hiking posts are still useful at the URL level.

Run this across **several crawls** for coverage. Each crawl captures a
different subset of a site.

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
| 2b: Host-graph pass for platform blogs | ⬜ not started | Planned for v1, right after Stage 2 |
| 3: Seed domains | 🟨 first cut | `scripts/seed_report.py`: idf × weight × specificity; needs a gear/commerce filter |
| 4: URL index validation | ⬜ not started | |
| 5: Candidate URL list | ⬜ not started | **First deliverable** |
| 6: Snowball | ⬜ not started | |
| 7: Content spot checks | ⬜ not started | |
| 8: WET recall sample | ⬜ not started | |

## Open questions

- How many domains link to ≥ 2 hubs? Is the long tail mostly blogs or mostly
  aggregators and SEO sites? (Measure in Stage 2.)
- How many graph releases to union, and how far back? 11 of 33 known blogs
  weren't crawled in 2026-jul-aug-sep at all. Check whether older releases
  (2022–2023) have them.
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
| 2026-11-07 | Seed discovery starts from the **domain-level web graph** using hand-curated hiking hubs. |
