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

---

## Stage 0: Reference data and evaluation set

Before building anything clever, create a way to tell whether it works.

- **`data/hubs.csv`**: hand-curated **hiking hub domains**. These are sites
  that hiking bloggers link to much more often than the general web does. Each
  row has a `weight` and a `category`. Starting ideas:
  - Trail databases and trip-report sites: `alltrails.com`, `hikingproject.com`,
    `wta.org`, `outdoorproject.com`, `hikingupward.com`, `trailpeak.com`,
    `vancouvertrails.com`, `clubtread.com`.
  - Peak-bagging: `peakbagger.com`, `listsofjohn.com`, `summitpost.org`,
    `14ers.com`, `peakvisor.com`.
  - Mapping and navigation: `caltopo.com`, `gaiagps.com`, `onxmaps.com`.
  - Permits and parks (as link *targets* only): `recreation.gov`, `nps.gov`,
    `parks.canada.ca`, `pc.gc.ca`, `bcparks.ca`, `ontarioparks.ca`, `sepaq.com`,
    `fs.usda.gov`.
  - Trail organizations: `appalachiantrail.org`, `pcta.org`, `brucetrail.org`,
    `continentaldividetrail.org`.
  - Avoid hubs that are too generic (`strava.com`, `instagram.com`) or that are
    mostly about other activities (`trailforks.com` for biking,
    `mountainproject.com` for climbing). These can come back later at low weight.
- **`data/eval/known_blogs.txt`**: a hand-assembled list of **known** US/Canada
  hiking blogs, around 50–100 to start. Used to measure seed recall.
  Never tune thresholds against it without holding some of it back.
- **`data/eval/negatives.txt`**: domains that should *not* come out. Gear
  retailers, tourism boards, news sites, aggregators, SEO "top 10 hikes" farms.
- **Gazetteers** for later stages. US/Canadian state and province names and
  abbreviations, plus multi-token park, summit and trail names from GNIS (US),
  CGNDB (Canada), and/or the hikeratlas OSM extracts.

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

**Implementation options** (to be measured):

- DuckDB `read_csv` on the gzipped stream with a semi-join against the hub ids.
  Simplest. Check memory use.
- A ~50-line Go or Rust streaming aggregator that relies on the sort order.
  Fastest, O(1) memory.
- WebGraph (Java) on the **transposed** BVGraph: list the in-neighbours of each
  hub directly. Very fast, but we'd still need out-degrees from the forward
  graph or offsets.

Expected runtime is tens of minutes on one runner. Mostly I/O and gunzip; use
`pigz`.

**Output:** `domain_hub_scores.parquet` with `from_id, rev_domain, hub_links,
hub_weight_sum, categories, out_degree`, kept only for domains with
`hub_links ≥ 1`.

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
- Apply `negatives.txt`.

**Blog platforms need special handling.** The domain graph aggregates by
pay-level domain using the [public suffix list](https://publicsuffix.org):

| Platform | On the PSL? | Effect in the domain graph |
|---|---|---|
| `blogspot.com`, `github.io`, `wixsite.com` | yes | Each blog is its own domain. ✅ |
| `wordpress.com`, `medium.com`, `substack.com`, `squarespace.com`, `weebly.com`, `tumblr.com`, `typepad.com` | **no** | All blogs collapse into one node. ❌ |

For the second group, run the same hub scoring on the **host-level graph**
(~243M nodes, ~2.4B edges, split into many files), restricted to hosts under
those parents. Medium also uses paths (`medium.com/@user`), which the graph
can't separate at all. Defer those to Stage 4's URL-level matching.

**Output:** `seeds.parquet` (ranked domains with their score components) and a
per-run report of the precision and recall proxies against `known_blogs.txt`
and `negatives.txt`.

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
- `domain_score`: the score from Stage 3 and 4.

URLs from strong domains are kept **even without** a slug match. Many good
posts have slugs like `/day-3/`.

**Output:** `candidates/<crawl>.parquet` plus a rolled-up `urls.parquet`,
published as a GitHub Release asset or a Hugging Face dataset.

**Columns:** `url, domain, crawl, fetch_time, slug_match, domain_score,
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
`negatives.txt` and spot-checked.

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

- Small, reviewable inputs (`data/*.csv`) live in git. Large outputs go to
  **Release assets** (≤ 2 GB each) or a Hugging Face dataset, never git.
- Every output records the graph release and crawl ids it came from.
- HTTP clients use a descriptive User-Agent, backoff on 503, and capped
  concurrency.

## Proposed repo layout

```
docs/DESIGN.md          ← this file
data/hubs.csv           ← hub domains, weights, categories
data/eval/              ← known_blogs.txt, negatives.txt
data/gazetteer/         ← (generated or downloaded) place names
src/                    ← pipeline code (language TBD; likely Python + DuckDB, plus a small Go/Rust streamer if needed)
.github/workflows/      ← one workflow per stage group
```

## Status

| Stage | Status | Notes |
|---|---|---|
| 0: Reference data and eval set | ⬜ not started | |
| 1: Graph release and hub IDs | ⬜ not started | Latest release missing vertices file (see above) |
| 2: Edge stream and hub scoring | ⬜ not started | |
| 3: Seed domains | ⬜ not started | |
| 4: URL index validation | ⬜ not started | |
| 5: Candidate URL list | ⬜ not started | **First deliverable** |
| 6: Snowball | ⬜ not started | |
| 7: Content spot checks | ⬜ not started | |
| 8: WET recall sample | ⬜ not started | |

## Open questions

- How many domains link to ≥ 2 hubs? Is the long tail mostly blogs or mostly
  aggregators and SEO sites? (Measure in Stage 2.)
- Does DuckDB handle the 1.8B-edge stream within 16 GB, or do we need the
  custom streamer?
- Is one graph release enough, or should we union two or three releases to
  catch blogs that were dormant during one window?
- How should we treat general travel blogs that only sometimes cover hiking?
  (Current plan: keep them at the URL level, ranked lower at the domain level.)
- How best to handle non-PSL platforms (`wordpress.com`, `medium.com`, …): host
  graph, URL-level matching, or both?
- Where should outputs live long-term: GitHub Releases or a Hugging Face dataset?

## Decision log

| Date | Decision |
|---|---|
| 2026-11-07 | Focus on **blogs** only; official pages deferred. |
| 2026-11-07 | Repo is public so Actions minutes are free; **no LLM budget** to start. |
| 2026-11-07 | First deliverable is a **URL list** (with WARC pointers for later extraction). |
| 2026-11-07 | Seed discovery starts from the **domain-level web graph** using hand-curated hiking hubs. |
