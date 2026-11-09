// Command sitecdx implements the lookup half of Stage 4 in docs/DESIGN.md: it
// lists every capture of a set of sites in one Common Crawl crawl, using the
// crawl's CDX ("zipnum") index rather than scanning the columnar index.
//
// The CDX index is ~300 sorted, gzipped shards plus a small cluster.idx that
// records the first SURT key and byte range of every block of ~3,000 lines.
// For each site we binary-search cluster.idx for the blocks covering its SURT
// range, merge adjacent blocks into a few large Range requests, and keep the
// lines whose host belongs to one of the sites.
//
// Sites are registered domains (example.com: matches example.com and every
// subdomain) or hosts under a blog platform (someone.blogspot.com). A site can
// also be a section, host plus path prefix (14ers.com/php14ers/tripreport.php):
// only that part of the site is read, capped by -max-blocks-per-section, for
// trip-report forums on sites whose other pages make them look off topic
// (data/sections.csv).
package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/hikeratlas/cc-crawler/internal/ccio"
)

type block struct {
	key    string // first SURT key in the block (without timestamp)
	shard  string
	offset int64
	length int64
}

type cdxRecord struct {
	URL       string `json:"url"`
	Mime      string `json:"mime"`
	MimeDet   string `json:"mime-detected"`
	Status    string `json:"status"`
	Digest    string `json:"digest"`
	Length    string `json:"length"`
	Offset    string `json:"offset"`
	Filename  string `json:"filename"`
	Languages string `json:"languages"`
	Redirect  string `json:"redirect"`
}

func main() {
	crawl := flag.String("crawl", "CC-MAIN-2026-34", "crawl id")
	htmlOnly := flag.Bool("html-only", true, "keep only status-200 HTML captures (text/html, application/xhtml+xml)")
	sitesPath := flag.String("sites", "", "file of sites, one per line (first tab-separated column; header lines starting with # or 'rank' are skipped)")
	out := flag.String("out", "out/captures.tsv.gz", "output (gzip TSV)")
	maxSectionBlocks := flag.Int("max-blocks-per-section", 200, "cap on CDX blocks read per section (host/path entries)")
	maxBlocks := flag.Int("max-blocks-per-site", 4, "cap on CDX blocks read per site (~3,000 captures each); larger sites get evenly spaced blocks")
	concurrency := flag.Int("concurrency", 2, "parallel range requests (all requests also share one adaptive pacer)")
	maxRequest := flag.Int64("max-request-bytes", 64<<20, "merge nearby blocks into requests up to this size")
	maxGap := flag.Int64("max-gap-bytes", 4<<20, "merge blocks separated by up to this many unneeded bytes (fewer requests, more bytes)")
	minInterval := flag.Duration("min-interval", 250*time.Millisecond, "fastest pace between request starts")
	flag.Parse()
	start := time.Now()
	ccio.MinInterval = *minInterval

	sites := readSites(*sitesPath)
	siteSet := map[string]bool{}      // SURT host prefix, e.g. "com,example"
	sections := map[string][]string{} // SURT host -> path prefixes, for section entries
	whole := map[string]bool{}        // hosts listed without a path
	for _, s := range sites {
		if i := strings.IndexByte(s, '/'); i > 0 {
			h := surtHost(strings.TrimPrefix(s[:i], "www."))
			siteSet[h] = true
			sections[h] = append(sections[h], strings.ToLower(s[i:]))
			continue
		}
		siteSet[surtHost(s)] = true
		whole[surtHost(s)] = true
	}
	for h := range whole {
		delete(sections, h) // a whole-site entry wins over its sections
	}
	log.Printf("%d sites (%d with sections only)", len(siteSet), len(sections))

	base := fmt.Sprintf("%s/cc-index/collections/%s/indexes/", ccio.Base, *crawl)
	blocks := loadClusterIdx(base + "cluster.idx")
	log.Printf("cluster.idx: %d blocks (%s)", len(blocks), time.Since(start).Round(time.Second))

	// Which blocks does each site need?
	need := map[int]bool{}
	capped := 0
	type span struct {
		lo, hi string
		cap    int
	}
	var spans []span
	for host := range siteSet {
		if ps, ok := sections[host]; ok {
			for _, p := range ps {
				spans = append(spans, span{host + ")" + p, host + ")" + p + "\xff", *maxSectionBlocks})
			}
			continue
		}
		spans = append(spans, span{host + ")", host + "-", *maxBlocks}) // ')' < ',' < '-': "host)…" and "host,sub)…"
	}
	for _, sp := range spans {
		lo, hi, maxB := sp.lo, sp.hi, sp.cap
		i := sort.Search(len(blocks), func(i int) bool { return blocks[i].key > lo }) - 1
		if i < 0 {
			i = 0
		}
		// All blocks covering the site; if there are more than the cap, take
		// evenly spaced ones so the sample spans the whole site rather than
		// just its alphabetically first URLs.
		first := i
		for i < len(blocks) && blocks[i].key < hi {
			i++
		}
		if n := i - first; n > maxB {
			capped++
			for k := 0; k < maxB; k++ {
				need[first+k*n/maxB] = true
			}
		} else {
			for j := first; j < i; j++ {
				need[j] = true
			}
		}
	}
	idx := make([]int, 0, len(need))
	for i := range need {
		idx = append(idx, i)
	}
	sort.Ints(idx)

	// Merge blocks in the same shard that are adjacent or separated by a small
	// gap. Common Crawl rate-limits requests, not bytes, so a few extra bytes
	// per request are a good trade.
	type request struct {
		shard       string
		off, length int64
	}
	var reqs []request
	var bytesTotal, bytesFetched int64
	for _, i := range idx {
		b := blocks[i]
		bytesTotal += b.length
		if n := len(reqs); n > 0 {
			last := &reqs[n-1]
			gap := b.offset - (last.off + last.length)
			if last.shard == b.shard && gap >= 0 && gap <= *maxGap && last.length+gap+b.length <= *maxRequest {
				last.length += gap + b.length
				continue
			}
		}
		reqs = append(reqs, request{b.shard, b.offset, b.length})
	}
	for _, r := range reqs {
		bytesFetched += r.length
	}
	log.Printf("%d blocks (%.1f MB needed, %.1f MB fetched) in %d requests; %d sites capped at %d blocks",
		len(idx), float64(bytesTotal)/1e6, float64(bytesFetched)/1e6, len(reqs), capped, *maxBlocks)

	if err := os.MkdirAll(dirOf(*out), 0o755); err != nil {
		log.Fatal(err)
	}
	fo, err := os.Create(*out)
	must(err)
	gzw := gzip.NewWriter(fo)
	w := bufio.NewWriterSize(gzw, 1<<20)
	fmt.Fprintln(w, "site\tcrawl\ttimestamp\turl\tstatus\tmime\tlanguages\tredirect\twarc_filename\twarc_record_offset\twarc_record_length\tdigest")

	var mu sync.Mutex
	var kept, seen int64
	jobs := make(chan request)
	var wg sync.WaitGroup
	for k := 0; k < *concurrency; k++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for r := range jobs {
				data, err := ccio.FetchRange(base+r.shard, r.off, r.length)
				if err != nil {
					log.Fatalf("%s@%d: %v", r.shard, r.off, err)
				}
				lines := decodeBlocks(data)
				var buf bytes.Buffer
				var k2, s2 int64
				for _, line := range lines {
					s2++
					site, rec, ts, ok := parseLine(line, siteSet)
					if !ok || !inSection(line, sections) {
						continue
					}
					mime := rec.MimeDet
					if mime == "" {
						mime = rec.Mime
					}
					if *htmlOnly && (rec.Status != "200" || (mime != "text/html" && mime != "application/xhtml+xml")) {
						continue
					}
					fmt.Fprintf(&buf, "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n", site, *crawl, ts, clean(rec.URL),
						rec.Status, mime, rec.Languages, clean(rec.Redirect), rec.Filename, rec.Offset, rec.Length, rec.Digest)
					k2++
				}
				mu.Lock()
				w.Write(buf.Bytes())
				kept += k2
				seen += s2
				mu.Unlock()
			}
		}()
	}
	for i, r := range reqs {
		jobs <- r
		if (i+1)%500 == 0 {
			log.Printf("%d/%d requests", i+1, len(reqs))
		}
	}
	close(jobs)
	wg.Wait()
	must(w.Flush())
	must(gzw.Close())
	must(fo.Close())
	log.Printf("kept %d of %d CDX lines; wrote %s in %s; %d rate-limit responses",
		kept, seen, *out, time.Since(start).Round(time.Second), ccio.SlowDowns())
}

// inSection keeps a line unless its host has section entries and its path
// matches none of them.
func inSection(line []byte, sections map[string][]string) bool {
	if len(sections) == 0 {
		return true
	}
	sp := bytes.IndexByte(line, ' ')
	if sp < 0 {
		return false
	}
	key := string(line[:sp])
	paren := strings.IndexByte(key, ')')
	if paren < 0 {
		return false
	}
	host := key[:paren]
	if c := strings.IndexByte(host, ':'); c >= 0 {
		host = host[:c]
	}
	ps, ok := sections[host]
	if !ok {
		return true
	}
	for _, p := range ps {
		if strings.HasPrefix(key[paren+1:], p) {
			return true
		}
	}
	return false
}

func readSites(path string) []string {
	f, err := os.Open(path)
	must(err)
	defer f.Close()
	var r io.Reader = f
	if strings.HasSuffix(path, ".gz") {
		gz, err := gzip.NewReader(f)
		must(err)
		r = gz
	}
	var sites []string
	sc := bufio.NewScanner(r)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, "rank\t") || strings.HasPrefix(line, "domain") {
			continue
		}
		col := strings.Split(line, "\t")[0]
		sites = append(sites, strings.ToLower(col))
	}
	must(sc.Err())
	return sites
}

// surtHost turns "www.Example.com" into "com,example,www".
func surtHost(d string) string {
	p := strings.Split(strings.ToLower(d), ".")
	for i, j := 0, len(p)-1; i < j; i, j = i+1, j-1 {
		p[i], p[j] = p[j], p[i]
	}
	return strings.Join(p, ",")
}

// unsurtHost turns "com,example,www" into "www.example.com".
func unsurtHost(s string) string { return reverseJoin(strings.Split(s, ","), ".") }

func reverseJoin(p []string, sep string) string {
	for i, j := 0, len(p)-1; i < j; i, j = i+1, j-1 {
		p[i], p[j] = p[j], p[i]
	}
	return strings.Join(p, sep)
}

func loadClusterIdx(url string) []block {
	r, err := ccio.Open(url)
	must(err)
	defer r.Close()
	var blocks []block
	sc := bufio.NewScanner(bufio.NewReaderSize(r, 1<<20))
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		// "<surt> <timestamp>\t<shard>\t<offset>\t<length>\t<seq>"
		f := strings.Split(sc.Text(), "\t")
		if len(f) < 4 {
			continue
		}
		key := f[0]
		if sp := strings.IndexByte(key, ' '); sp >= 0 {
			key = key[:sp]
		}
		off, _ := strconv.ParseInt(f[2], 10, 64)
		n, _ := strconv.ParseInt(f[3], 10, 64)
		blocks = append(blocks, block{key, f[1], off, n})
	}
	must(sc.Err())
	return blocks
}

// decodeBlocks gunzips one or more concatenated gzip members.
func decodeBlocks(data []byte) [][]byte {
	gz, err := gzip.NewReader(bytes.NewReader(data))
	must(err)
	raw, err := io.ReadAll(gz)
	must(err)
	return bytes.Split(bytes.TrimRight(raw, "\n"), []byte("\n"))
}

// parseLine keeps a CDX line if its host is one of the sites or a subdomain
// of one. Line format: "<surt> <timestamp> <json>".
func parseLine(line []byte, sites map[string]bool) (string, cdxRecord, string, bool) {
	var rec cdxRecord
	sp1 := bytes.IndexByte(line, ' ')
	if sp1 < 0 {
		return "", rec, "", false
	}
	surt := line[:sp1]
	paren := bytes.IndexByte(surt, ')')
	if paren < 0 {
		return "", rec, "", false
	}
	host := string(surt[:paren])
	if i := strings.IndexByte(host, ':'); i >= 0 { // strip port
		host = host[:i]
	}
	site := ""
	for h := host; ; {
		if sites[h] {
			site = h
			break
		}
		i := strings.LastIndexByte(h, ',')
		if i < 0 {
			break
		}
		h = h[:i]
	}
	if site == "" {
		return "", rec, "", false
	}
	rest := line[sp1+1:]
	sp2 := bytes.IndexByte(rest, ' ')
	if sp2 < 0 {
		return "", rec, "", false
	}
	if err := json.Unmarshal(rest[sp2+1:], &rec); err != nil {
		return "", rec, "", false
	}
	return unsurtHost(site), rec, string(rest[:sp2]), true
}

func clean(s string) string { return strings.NewReplacer("\t", " ", "\n", " ").Replace(s) }

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
