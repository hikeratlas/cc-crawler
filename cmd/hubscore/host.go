package main

// Host-graph mode (Stage 2b of docs/DESIGN.md). The domain graph collapses
// every blog on a hosting platform (*.blogspot.com, *.wordpress.com, ...) into
// one node, so those blogs can't be scored there. This mode scores them on the
// host-level graph instead:
//
//  1. Stream the host vertices once. A host is a hub host when its name is a
//     hub domain or a subdomain of one (www.alltrails.com -> alltrails.com).
//     A host is a platform host when it is a subdomain of a parent in
//     data/platforms.csv (foo.blogspot.com).
//  2. Stream the edges and aggregate, per platform host, its out-degree and
//     the set of hubs it links to.
//  3. Stream the vertices again to attach host names.
//
// Output has the same columns as domain mode (domain = host, n_hosts = 1), so
// scripts/seed_report.py ranks it unchanged. Hub in-degrees count linking
// platform hosts only.

import (
	"bufio"
	"compress/gzip"
	"encoding/csv"
	"fmt"
	"log"
	"math/bits"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/hikeratlas/cc-crawler/internal/ccio"
)

type hostOpts struct {
	release, platformsCSV, evalCSVs, hubsCSV, out, hubOut, metaOut string
	maxEdges                                                       int64
}

// partURLs reads a Common Crawl *.paths.gz listing and returns full URLs.
func partURLs(pathsURL string) []string {
	r, err := ccio.Open(pathsURL)
	if err != nil {
		log.Fatalf("%s: %v", pathsURL, err)
	}
	defer r.Close()
	gz, err := gzip.NewReader(r)
	if err != nil {
		log.Fatalf("%s: %v", pathsURL, err)
	}
	var out []string
	sc := bufio.NewScanner(gz)
	for sc.Scan() {
		if p := strings.TrimSpace(sc.Text()); p != "" {
			out = append(out, ccio.Base+"/"+p)
		}
	}
	if err := sc.Err(); err != nil {
		log.Fatalf("%s: %v", pathsURL, err)
	}
	return out
}

func loadPlatforms(path string) []string {
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
		if p := strings.TrimSpace(r[0]); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// prefixMatch walks the dot-separated prefixes of a reversed host name
// ("com.blogspot.foo" -> "com", "com.blogspot", "com.blogspot.foo") and
// returns the value of the first one in m.
func prefixMatch(rev []byte, m map[string]int) (int, int, bool) {
	for i := 0; i <= len(rev); i++ {
		if i == len(rev) || rev[i] == '.' {
			if v, ok := m[string(rev[:i])]; ok {
				return v, i, true
			}
		}
	}
	return 0, 0, false
}

func runHost(o hostOpts) {
	start := time.Now()
	base := fmt.Sprintf("%s/%s/host/%s-host", graphBase, o.release, o.release)
	vparts := partURLs(base + "-vertices.paths.gz")
	eparts := partURLs(base + "-edges.paths.gz")
	log.Printf("%d vertex parts, %d edge parts", len(vparts), len(eparts))

	hubs := loadHubs(o.hubsCSV)
	if len(hubs) > 128 {
		log.Fatalf("%d active hubs; bitset supports 128", len(hubs))
	}
	hubRev := map[string]int{}
	for i, h := range hubs {
		hubRev[reverse(h.domain)] = i
	}
	platforms := loadPlatforms(o.platformsCSV)
	platRev := map[string]int{}
	for i, p := range platforms {
		platRev[reverse(p)] = i
	}
	evalHosts := map[string]bool{}
	for _, p := range strings.Split(o.evalCSVs, ",") {
		if p = strings.TrimSpace(p); p != "" {
			for _, d := range firstColumn(p) {
				evalHosts[reverse(d)] = true
			}
		}
	}

	// ---- pass 1: vertices -> hub and platform ids ----
	// Ids run 0..n-1 in order across the parts, so the tables grow by append.
	var hubOf []int8  // 0 = not a hub host, else hub index + 1
	var keep []uint64 // bitset: platform host or eval host
	evalIDs := map[int64]bool{}
	platCount := make([]int64, len(platforms))
	hubHosts := 0
	var n int64
	t0 := time.Now()
	for _, vp := range vparts {
		scanTSV(vp, func(f [][]byte) bool {
			id, rev := parseInt(f[0]), f[1]
			for n <= id {
				hubOf = append(hubOf, 0)
				if n%64 == 0 {
					keep = append(keep, 0)
				}
				n++
			}
			if i, _, ok := prefixMatch(rev, hubRev); ok {
				hubOf[id] = int8(i + 1)
				hubHosts++
				return true
			}
			// A platform host is strictly below its parent (not blogspot.com itself).
			if i, at, ok := prefixMatch(rev, platRev); ok && at < len(rev) {
				keep[id/64] |= 1 << (id % 64)
				platCount[i]++
			} else if evalHosts[string(rev)] {
				keep[id/64] |= 1 << (id % 64)
				evalIDs[id] = true
			}
			return true
		})
	}
	log.Printf("pass 1: %d hosts, %d hub hosts in %s", n, hubHosts, time.Since(t0).Round(time.Second))
	for i, p := range platforms {
		log.Printf("  %s: %d hosts", p, platCount[i])
	}

	// ---- pass 2: edges ----
	// Edges are sorted by from_id within a part; rows are merged by id in case
	// one source spans parts.
	rows := map[int64]*row{}
	cur := row{id: -1}
	var nEdges int64
	flush := func() {
		if cur.id < 0 || (cur.hubs[0]|cur.hubs[1] == 0 && !evalIDs[cur.id]) {
			return // eval hosts are reported even with no hubs
		}
		if r, ok := rows[cur.id]; ok {
			r.outDegree += cur.outDegree
			r.hubs[0] |= cur.hubs[0]
			r.hubs[1] |= cur.hubs[1]
		} else {
			c := cur
			rows[cur.id] = &c
		}
	}
	t0 = time.Now()
	done := false
	for _, ep := range eparts {
		if done {
			break
		}
		scanTSV(ep, func(f [][]byte) bool {
			from := parseInt(f[0])
			if from >= n || keep[from/64]&(1<<(from%64)) == 0 {
				nEdges++
				return o.maxEdges == 0 || nEdges < o.maxEdges
			}
			if from != cur.id {
				flush()
				cur = row{id: from}
			}
			cur.outDegree++
			if to := parseInt(f[1]); to != from && to < n {
				if h := hubOf[to]; h > 0 {
					i := int(h - 1)
					cur.hubs[i/64] |= 1 << (i % 64)
				}
			}
			nEdges++
			if nEdges%500_000_000 == 0 {
				log.Printf("pass 2: %dM edges, %d rows, %s", nEdges/1_000_000, len(rows), time.Since(t0).Round(time.Second))
			}
			if o.maxEdges > 0 && nEdges >= o.maxEdges {
				done = true
				return false
			}
			return true
		})
		flush()
		cur = row{id: -1}
	}
	log.Printf("pass 2: %d edges, %d rows in %s", nEdges, len(rows), time.Since(t0).Round(time.Second))

	ids := make([]int64, 0, len(rows))
	for id, r := range rows {
		ids = append(ids, id)
		for w := 0; w < 2; w++ {
			for b := r.hubs[w]; b != 0; b &= b - 1 {
				hubs[w*64+bits.TrailingZeros64(b)].inDegree++
			}
		}
	}
	sort.Slice(ids, func(a, b int) bool { return ids[a] < ids[b] })

	// ---- pass 3: vertices again, attach names ----
	if err := os.MkdirAll(dirOf(o.out), 0o755); err != nil {
		log.Fatal(err)
	}
	fo, err := os.Create(o.out)
	if err != nil {
		log.Fatal(err)
	}
	gz := gzip.NewWriter(fo)
	w := bufio.NewWriterSize(gz, 1<<20)
	fmt.Fprintln(w, "id\tdomain\tn_hosts\tout_degree\tn_hubs\tweight_sum\tcategories\thubs\tin_eval")
	t0 = time.Now()
	next := 0
	for _, vp := range vparts {
		if next >= len(ids) {
			break
		}
		scanTSV(vp, func(f [][]byte) bool {
			if next >= len(ids) {
				return false
			}
			id := parseInt(f[0])
			if id != ids[next] {
				return true
			}
			r := rows[id]
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
			fmt.Fprintf(w, "%d\t%s\t1\t%d\t%d\t%.2f\t%s\t%s\t%t\n", id, reverse(string(f[1])), r.outDegree,
				len(names), wsum, strings.Join(cats, ","), strings.Join(names, ","), evalHosts[string(f[1])])
			next++
			return true
		})
	}
	must(w.Flush())
	must(gz.Close())
	must(fo.Close())
	log.Printf("pass 3: wrote %s (%d rows) in %s", o.out, next, time.Since(t0).Round(time.Second))

	fh, err := os.Create(o.hubOut)
	if err != nil {
		log.Fatal(err)
	}
	fmt.Fprintln(fh, "hub\tcategory\tweight\tid\tin_degree")
	for _, h := range hubs {
		fmt.Fprintf(fh, "%s\t%s\t%.2f\t%d\t%d\n", h.domain, h.category, h.weight, h.id, h.inDegree)
	}
	must(fh.Close())

	var pc []string
	for i, p := range platforms {
		pc = append(pc, fmt.Sprintf("%q: %d", p, platCount[i]))
	}
	meta := fmt.Sprintf("{\n  \"release\": %q,\n  \"graph\": \"host\",\n  \"vertices\": %d,\n  \"edges\": %d,\n  \"rows\": %d,\n  \"hub_hosts\": %d,\n  \"hubs_active\": %d,\n  \"platform_hosts\": {%s},\n  \"seconds\": %d\n}\n",
		o.release, n, nEdges, next, hubHosts, len(hubs), strings.Join(pc, ", "), int(time.Since(start).Seconds()))
	must(os.WriteFile(o.metaOut, []byte(meta), 0o644))
}
