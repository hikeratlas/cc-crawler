#!/usr/bin/env bash
# Look up every domain in the hand-curated lists in a Common Crawl domain-graph
# ranks file. Writes data/reports/graph_presence_<release>.csv with harmonic
# centrality rank, PageRank rank and host count (blank = not in the graph).
#
# Usage: scripts/graph_presence.sh [release]   (default: cc-main-2026-jul-aug-sep)
# Streams the ~2.4 GB ranks file; nothing large is written to disk.
set -euo pipefail
cd "$(dirname "$0")/.."
release=${1:-cc-main-2026-jul-aug-sep}
url="${RANKS_URL:-https://data.commoncrawl.org/projects/hyperlinkgraph/$release/domain/$release-domain-ranks.txt.gz}"
out="data/reports/graph_presence_$release.csv"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# list,domain pairs from the curated files (first CSV column, header skipped)
for f in data/hubs.csv data/eval/known_blogs.csv data/eval/negatives.csv; do
  tail -n +2 "$f" | cut -d, -f1 | sed "s|^|$(basename "$f" .csv),|"
done > "$tmp/lists.csv"

# ranks columns: harmonicc_pos harmonicc_val pr_pos pr_val host_rev n_hosts
curl -sS --fail --retry 4 "$url" | gzip -dc | awk -F'\t' -v L="$tmp/lists.csv" '
  BEGIN { while ((getline l < L) > 0) { split(l, a, ","); n = split(a[2], p, ".");
            r = p[n]; for (i = n - 1; i > 0; i--) r = r "." p[i]; want[r] = 1 } }
  ($5 in want) { print $5 "\t" $1 "\t" $3 "\t" $6 }' > "$tmp/hits.tsv"

awk -F'\t' -v OFS=, '
  NR == FNR { n = split($1, p, "."); d = p[n]; for (i = n - 1; i > 0; i--) d = d "." p[i];
              hc[d] = $2; pr[d] = $3; nh[d] = $4; next }
  FNR == 1 { print "list,domain,harmonic_rank,pagerank_rank,n_hosts"; next }
  { split($0, a, ","); print a[1], a[2], hc[a[2]], pr[a[2]], nh[a[2]] }' \
  "$tmp/hits.tsv" <(echo header; cat "$tmp/lists.csv") > "$out"
echo "wrote $out ($(grep -c ',,,$' "$out" || true) domains not found in $release)"
