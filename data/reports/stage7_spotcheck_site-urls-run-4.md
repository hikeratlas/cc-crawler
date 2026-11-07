# Stage 7 spot check: site-urls run 4 candidate URLs

A stratified random sample of 200 of the 2.0M candidate URLs from the
`site-urls-run-4` release. Each page was fetched from its WARC record
(`cmd/warcfetch`; 200/200 fetched, 0 errors) and labelled by reading the
title and extracted text. The labels are in
[`data/eval/spotcheck/site-urls-run-4_labels.tsv`](../eval/spotcheck/site-urls-run-4_labels.tsv).

Sample: `scripts/sample_candidates.py --sizes hiking:1=60,hiking:0=60,mixed:1=60,drifted:1=20`
(stratum = site verdict : whether the URL slug matches; at most 2 pages per
site per stratum; seed 20261107).

## Labels

- **topic**: `T` specific trail / hike / destination / national park
  content; `H` general hiking (tips, gear, logistics); `O` other outdoor or
  travel (camping, RV, skiing, biking, fishing, sightseeing); `X`
  off-topic (real estate, shops, unrelated); `E` empty or junk (image
  attachment pages, search/filter results, bot checks, stubs).
- **geo**: `U` US/Canada, `N` elsewhere, `?` unclear.
- **kind**: `P` personal/individual, `C` commercial/organisation/aggregator.

"Core" = `T` in US/Canada. "Broad" = core plus general hiking (`H`) that
isn't clearly elsewhere.

## Results

| stratum | population | n | core | broad | junk (E) | off-topic (X) | other outdoor (O) |
|---|---:|---:|---:|---:|---:|---:|---:|
| hiking site, hiking slug | 567,023 | 60 | 23% (14–35%) | 32% (21–44%) | 27% | 15% | 13% |
| hiking site, no slug | 979,756 | 60 | 3% (1–11%) | 3% (1–11%) | 35% | 27% | 30% |
| mixed site, hiking slug | 433,165 | 60 | 22% (13–34%) | 23% (14–35%) | 28% | 22% | 22% |
| drifted site | 26,412 | 20 | 5% (1–24%) | 5% (1–24%) | 25% | 35% | 25% |
| **weighted overall** | 2,006,356 | 200 | **13%** | **16%** | 31% | 22% | 23% |

(95% Wilson intervals in brackets.) Of the 36 broadly relevant pages, 12
are personal. 16 sampled hiking pages were outside the US/Canada.

## What goes wrong

1. **"Keep every page of a hiking site" doesn't work.** Pages from `hiking`
   sites without a hiking slug are half the list (980k) and only ~3%
   relevant.
2. **Junk pages (~31%)**: WordPress image-attachment pages, search and
   filter URLs with query strings, bot-check interstitials, stubs.
3. **Off-topic sites that slipped through**: lake real estate
   (lakehomes.com, lakehouse.com, land-sale sites), whose slugs are full
   of "lake", "trail", "pass" and "meadow"; RV and campground directories;
   shop category and product pages.
4. **Outside US/Canada**: guided-trip sellers and route databases
   (explore-share, fatmap, routeyou, 10adventures).

## Rules replayed on the sample

| rules | URLs kept | precision (broad) | good pages kept |
|---|---:|---:|---:|
| none | 2.01M | 16% | 100% |
| drop no-slug pages | 1.03M | 27% | 90% |
| + query strings, real estate, shop paths ("safe" set) | 0.76M | 35% | 84% |
| + attachment-like slugs, RV/campground | 0.55M | 39% | 68% |
| simple content rule on fetched text (hiking-term density, US/CA place, ≥150 words) | – | ~48% | ~69% |

URL rules alone level off around 35–40%. Beyond that, page content is
needed. A first heuristic on the text gets ~48% precision on its own, held
back by page chrome (menus full of hiking words) and a small gazetteer. 200
labels are too few to tune much further without overfitting.
