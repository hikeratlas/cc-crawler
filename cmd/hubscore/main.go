// Command hubscore implements Stages 1–2 (and, with -graph host, Stage 2b) of docs/DESIGN.md: it resolves the
// hub domains from data/hubs.csv to ids in a Common Crawl domain-level web
// graph, streams the graph's edges once, and writes one row per domain that
// links to at least one hub (plus every domain in the eval lists, so recall
// can be measured even when they link to no hubs).
//
// Inputs are read as gzip streams from local paths or https URLs (see
// internal/ccio for retry and resume behaviour).
package main

import (
	"bufio"
	"compress/gzip"
	"encoding/csv"
	"flag"
	"fmt"
	"log"
	"math/bits"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/hikeratlas/cc-crawler/internal/ccio"
)

const graphBase = ccio.Base + "/projects/hyperlinkgraph"

type hub struct {
	domain   string
	category string
	weight   float64
	id       int64
	inDegree int64 // distinct domains linking to this hub
}

type row struct {
	id        int64
	outDegree int64
	hubs      [2]uint64 // bitset over hub index (max 128 hubs)
}

func main() {
	release := flag.String("release", "cc-main-2026-jul-aug-sep", "web graph release name")
	vertices := flag.String("vertices", "", "domain vertices .txt.gz (path or URL); default derived from -release")
	edges := flag.String("edges", "", "domain edges .txt.gz (path or URL); default derived from -release")
	hubsCSV := flag.String("hubs", "data/hubs.csv", "hub list")
	evalCSVs := flag.String("eval", "data/eval/known_blogs.csv,data/eval/negatives.csv", "comma-separated CSVs whose first column lists domains to always report")
	out := flag.String("out", "out/domain_hub_scores.tsv.gz", "per-domain output (gzip TSV)")
	hubOut := flag.String("hub-out", "out/hub_stats.tsv", "per-hub output (TSV)")
	metaOut := flag.String("meta-out", "out/meta.json", "run metadata (JSON)")
	maxEdges := flag.Int64("max-edges", 0, "stop after this many edges (0 = all); for smoke tests")
	graph := flag.String("graph", "domain", "domain: score domains (Stage 2); host: score platform-hosted blogs on the host graph (Stage 2b)")
	platformsCSV := flag.String("platforms", "data/platforms.csv", "host mode: blog platforms whose subdomains are scored")
	flag.Parse()
	start := time.Now()
	if *graph == "host" {
		runHost(hostOpts{release: *release, platformsCSV: *platformsCSV, evalCSVs: *evalCSVs, hubsCSV: *hubsCSV,
			out: *out, hubOut: *hubOut, metaOut: *metaOut, maxEdges: *maxEdges})
		return
	} else if *graph != "domain" {
		log.Fatalf("unknown -graph %q", *graph)
	}

	if *vertices == "" {
		*vertices = fmt.Sprintf("%s/%s/domain/%s-domain-vertices.txt.gz", graphBase, *release, *release)
	}
	if *edges == "" {
		*edges = fmt.Sprintf("%s/%s/domain/%s-domain-edges.txt.gz", graphBase, *release, *release)
	}

	hubs := loadHubs(*hubsCSV)
	if len(hubs) > 128 {
		log.Fatalf("%d active hubs; bitset supports 128", len(hubs))
	}
	evalDomains := map[string]bool{}
	for _, p := range strings.Split(*evalCSVs, ",") {
		if p = strings.TrimSpace(p); p != "" {
			for _, d := range firstColumn(p) {
				evalDomains[d] = true
			}
		}
	}

	// ---- pass 1: vertices -> ids ----
	want := map[string]int{} // rev domain -> hub index, or -1 for eval-only
	for d := range evalDomains {
		want[reverse(d)] = -1
	}
	for i, h := range hubs {
		want[reverse(h.domain)] = i
	}
	evalIDs := map[int64]bool{}
	var maxID int64
	t0 := time.Now()
	scanTSV(*vertices, func(f [][]byte) bool {
		id := parseInt(f[0])
		if id > maxID {
			maxID = id
		}
		if i, ok := want[string(f[1])]; ok {
			if i >= 0 {
				hubs[i].id = id
			} else {
				evalIDs[id] = true
			}
		}
		return true
	})
	log.Printf("pass 1: %d vertices in %s", maxID+1, time.Since(t0).Round(time.Second))
	hubOf := make([]int8, maxID+1) // 0 = not a hub, else hub index + 1
	found := 0
	for i, h := range hubs {
		if h.id < 0 {
			log.Printf("hub not in graph: %s", h.domain)
			continue
		}
		hubOf[h.id] = int8(i + 1)
		found++
	}
	log.Printf("resolved %d/%d hubs, %d/%d eval domains", found, len(hubs), len(evalIDs), len(evalDomains))

	// ---- pass 2: edges, sorted by from_id ----
	var rows []row
	cur := row{id: -1}
	var nEdges int64
	flush := func() {
		if cur.id < 0 {
			return
		}
		has := cur.hubs[0]|cur.hubs[1] != 0
		if has {
			for w := 0; w < 2; w++ {
				for b := cur.hubs[w]; b != 0; b &= b - 1 {
					hubs[w*64+bits.TrailingZeros64(b)].inDegree++
				}
			}
		}
		if has || evalIDs[cur.id] {
			rows = append(rows, cur)
		}
	}
	t0 = time.Now()
	sorted := true
	scanTSV(*edges, func(f [][]byte) bool {
		from, to := parseInt(f[0]), parseInt(f[1])
		if from != cur.id {
			if from < cur.id {
				sorted = false
			}
			flush()
			cur = row{id: from}
		}
		cur.outDegree++
		if from != to && to <= maxID {
			if h := hubOf[to]; h > 0 {
				i := int(h - 1)
				cur.hubs[i/64] |= 1 << (i % 64)
			}
		}
		nEdges++
		if nEdges%200_000_000 == 0 {
			log.Printf("pass 2: %dM edges, %d rows, %s", nEdges/1_000_000, len(rows), time.Since(t0).Round(time.Second))
		}
		return *maxEdges == 0 || nEdges < *maxEdges
	})
	flush()
	log.Printf("pass 2: %d edges, %d rows in %s", nEdges, len(rows), time.Since(t0).Round(time.Second))
	if !sorted {
		log.Printf("warning: edges were not sorted by from_id; rows for a domain may be split")
	}
	// Eval domains that never appear as an edge source are dangling nodes:
	// linked to, but not crawled in this graph's window. Report them anyway.
	seen := map[int64]bool{}
	for _, r := range rows {
		if evalIDs[r.id] {
			seen[r.id] = true
		}
	}
	for id := range evalIDs {
		if !seen[id] {
			rows = append(rows, row{id: id})
		}
	}
	sort.Slice(rows, func(a, b int) bool { return rows[a].id < rows[b].id })

	// ---- pass 3: vertices again, attach names to emitted rows ----
	if err := os.MkdirAll(dirOf(*out), 0o755); err != nil {
		log.Fatal(err)
	}
	fo, err := os.Create(*out)
	if err != nil {
		log.Fatal(err)
	}
	gz := gzip.NewWriter(fo)
	w := bufio.NewWriterSize(gz, 1<<20)
	fmt.Fprintln(w, "id\tdomain\tn_hosts\tout_degree\tn_hubs\tweight_sum\tcategories\thubs\tin_eval")
	t0 = time.Now()
	next := 0
	scanTSV(*vertices, func(f [][]byte) bool {
		if next >= len(rows) {
			return false
		}
		id := parseInt(f[0])
		for next < len(rows) && rows[next].id < id {
			next++ // edge source missing from vertices; skip
		}
		if next < len(rows) && rows[next].id == id {
			r := rows[next]
			var names, cats []string
			catSeen := map[string]bool{}
			wsum := 0.0
			for i, h := range hubs {
				if r.hubs[i/64]&(1<<(i%64)) != 0 {
					names = append(names, h.domain)
					wsum += h.weight
					if !catSeen[h.category] {
						catSeen[h.category] = true
						cats = append(cats, h.category)
					}
				}
			}
			sort.Strings(cats)
			dom := reverse(string(f[1]))
			fmt.Fprintf(w, "%d\t%s\t%s\t%d\t%d\t%.2f\t%s\t%s\t%t\n", id, dom, f[2], r.outDegree,
				len(names), wsum, strings.Join(cats, ","), strings.Join(names, ","), evalDomains[dom])
			next++
		}
		return true
	})
	must(w.Flush())
	must(gz.Close())
	must(fo.Close())
	log.Printf("pass 3: wrote %s in %s", *out, time.Since(t0).Round(time.Second))

	fh, err := os.Create(*hubOut)
	if err != nil {
		log.Fatal(err)
	}
	fmt.Fprintln(fh, "hub\tcategory\tweight\tid\tin_degree")
	for _, h := range hubs {
		fmt.Fprintf(fh, "%s\t%s\t%.2f\t%d\t%d\n", h.domain, h.category, h.weight, h.id, h.inDegree)
	}
	must(fh.Close())

	meta := fmt.Sprintf("{\n  \"release\": %q,\n  \"vertices\": %d,\n  \"edges\": %d,\n  \"rows\": %d,\n  \"hubs_resolved\": %d,\n  \"hubs_active\": %d,\n  \"seconds\": %d\n}\n",
		*release, maxID+1, nEdges, len(rows), found, len(hubs), int(time.Since(start).Seconds()))
	must(os.WriteFile(*metaOut, []byte(meta), 0o644))
}

func loadHubs(path string) []hub {
	f, err := os.Open(path)
	if err != nil {
		log.Fatal(err)
	}
	defer f.Close()
	recs, err := csv.NewReader(f).ReadAll()
	if err != nil {
		log.Fatal(err)
	}
	col := map[string]int{}
	for i, h := range recs[0] {
		col[h] = i
	}
	var hubs []hub
	for _, r := range recs[1:] {
		wt, _ := strconv.ParseFloat(r[col["weight"]], 64)
		if r[col["status"]] != "active" || wt <= 0 {
			continue
		}
		hubs = append(hubs, hub{domain: r[col["domain"]], category: r[col["category"]], weight: wt, id: -1})
	}
	return hubs
}

func firstColumn(path string) []string {
	f, err := os.Open(path)
	if err != nil {
		log.Fatal(err)
	}
	defer f.Close()
	recs, err := csv.NewReader(f).ReadAll()
	if err != nil {
		log.Fatal(err)
	}
	var out []string
	for _, r := range recs[1:] {
		out = append(out, r[0])
	}
	return out
}

// reverse turns "www.example.com" into "com.example.www" and back.
func reverse(d string) string {
	p := strings.Split(d, ".")
	for i, j := 0, len(p)-1; i < j; i, j = i+1, j-1 {
		p[i], p[j] = p[j], p[i]
	}
	return strings.Join(p, ".")
}

func parseInt(b []byte) int64 {
	var n int64
	for _, c := range b {
		n = n*10 + int64(c-'0')
	}
	return n
}

// scanTSV streams a gzipped TSV and calls fn with the fields of each line
// until fn returns false. Field slices are only valid during the call.
func scanTSV(src string, fn func([][]byte) bool) {
	r, err := ccio.Open(src)
	if err != nil {
		log.Fatalf("%s: %v", src, err)
	}
	defer r.Close()
	gz, err := gzip.NewReader(bufio.NewReaderSize(r, 4<<20))
	if err != nil {
		log.Fatalf("%s: %v", src, err)
	}
	sc := bufio.NewScanner(gz)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	fields := make([][]byte, 0, 4)
	for sc.Scan() {
		line := sc.Bytes()
		fields = fields[:0]
		start := 0
		for i, c := range line {
			if c == '\t' {
				fields = append(fields, line[start:i])
				start = i + 1
			}
		}
		fields = append(fields, line[start:])
		if !fn(fields) {
			return
		}
	}
	if err := sc.Err(); err != nil {
		log.Fatalf("%s: %v", src, err)
	}
}

func dirOf(p string) string {
	if i := strings.LastIndex(p, "/"); i >= 0 {
		return p[:i]
	}
	return "."
}

func must(err error) {
	if err != nil {
		log.Fatal(err)
	}
}
