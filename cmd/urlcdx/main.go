// Command urlcdx checks whether given URLs were captured in Common Crawl
// crawls. It is the "is it in the archive at all?" step of the rare-hike
// recall test (docs/DESIGN.md, "Recall on rare hikes").
//
// For each URL it computes the CDX SURT key, binary-searches each crawl's
// cluster.idx for the one block of ~3,000 lines that would hold it, fetches
// that block, and looks for a capture of the same page (ignoring scheme,
// "www.", a trailing slash and query arguments that don't identify a page). Output is one row per URL
// and crawl: url, crawl, found, status, mime, timestamp, captured url.
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
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"

	"github.com/hikeratlas/cc-crawler/internal/ccio"
)

type block struct {
	key            string
	shard          string
	offset, length int64
}

// surt turns a URL into the CDX key form used for lookup and comparison:
// "com,example)/path?a=1" (lowercase, no www., no trailing slash; see query).
func surt(raw string) (string, bool) {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.Host == "" {
		return "", false
	}
	host := strings.TrimPrefix(strings.ToLower(u.Hostname()), "www.")
	parts := strings.Split(host, ".")
	for i, j := 0, len(parts)-1; i < j; i, j = i+1, j-1 {
		parts[i], parts[j] = parts[j], parts[i]
	}
	p := strings.ToLower(u.EscapedPath())
	p = strings.TrimRight(p, "/")
	return strings.Join(parts, ",") + ")" + p + query(u.Query()), true
}

// query keeps the arguments that identify a page (?p=123, ?trip=9569) in a
// canonical order and drops ones that don't (Blogger's ?m=1, ?amp=1, utm_*).
func query(q url.Values) string {
	var keys []string
	for k := range q {
		lk := strings.ToLower(k)
		if lk == "m" || lk == "amp" || strings.HasPrefix(lk, "utm_") {
			continue
		}
		keys = append(keys, k)
	}
	if len(keys) == 0 {
		return ""
	}
	sort.Strings(keys)
	var b []string
	for _, k := range keys {
		for _, v := range q[k] {
			b = append(b, strings.ToLower(k)+"="+strings.ToLower(v))
		}
	}
	return "?" + strings.Join(b, "&")
}

func loadClusterIdx(u string) []block {
	r, err := ccio.Open(u)
	if err != nil {
		log.Fatal(err)
	}
	defer r.Close()
	var out []block
	sc := bufio.NewScanner(bufio.NewReaderSize(r, 1<<20))
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
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
		out = append(out, block{key, f[1], off, n})
	}
	if err := sc.Err(); err != nil {
		log.Fatal(err)
	}
	return out
}

// lineKey normalises a CDX line's SURT the same way as surt().
func lineKey(s string) string {
	qs := ""
	if q := strings.IndexByte(s, '?'); q >= 0 {
		if v, err := url.ParseQuery(s[q+1:]); err == nil {
			qs = query(v)
		}
		s = s[:q]
	}
	if i := strings.IndexByte(s, ')'); i >= 0 {
		host := s[:i]
		if c := strings.IndexByte(host, ':'); c >= 0 {
			host = host[:c]
		}
		host = strings.TrimSuffix(host, ",www")
		s = host + strings.TrimRight(s[i:], "/")
	}
	return s + qs
}

func main() {
	crawls := flag.String("crawls", "", "comma-separated crawl ids")
	in := flag.String("urls", "", "file of URLs, one per line")
	out := flag.String("out", "urlcdx.tsv", "output TSV")
	flag.Parse()

	type item struct{ raw, key string }
	var items []item
	f, err := os.Open(*in)
	if err != nil {
		log.Fatal(err)
	}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if k, ok := surt(sc.Text()); ok {
			items = append(items, item{strings.TrimSpace(sc.Text()), k})
		}
	}
	f.Close()

	fo, err := os.Create(*out)
	if err != nil {
		log.Fatal(err)
	}
	defer fo.Close()
	w := bufio.NewWriter(fo)
	defer w.Flush()
	fmt.Fprintln(w, "url\tcrawl\tfound\tstatus\tmime\ttimestamp\tcaptured")

	for _, crawl := range strings.Split(*crawls, ",") {
		crawl = strings.TrimSpace(crawl)
		base := fmt.Sprintf("%s/cc-index/collections/%s/indexes/", ccio.Base, crawl)
		blocks := loadClusterIdx(base + "cluster.idx")
		// Group URLs by the block that would hold them; the key may also sit at
		// the start of the next block, so fetch that one too.
		need := map[int][]int{}
		for i, it := range items {
			b := sort.Search(len(blocks), func(j int) bool { return blocks[j].key > it.key }) - 1
			if b < 0 {
				b = 0
			}
			need[b] = append(need[b], i)
			if b+1 < len(blocks) {
				need[b+1] = append(need[b+1], i)
			}
		}
		type hit struct{ status, mime, ts, url string }
		best := map[int]hit{}
		ids := make([]int, 0, len(need))
		for b := range need {
			ids = append(ids, b)
		}
		sort.Ints(ids)
		for _, b := range ids {
			bl := blocks[b]
			data, err := ccio.FetchRange(base+bl.shard, bl.offset, bl.length)
			if err != nil {
				log.Printf("%s block %d: %v", crawl, b, err)
				continue
			}
			gz, err := gzip.NewReader(bytes.NewReader(data))
			if err != nil {
				continue
			}
			raw, _ := io.ReadAll(gz)
			want := map[string][]int{}
			for _, i := range need[b] {
				want[items[i].key] = append(want[items[i].key], i)
			}
			for _, line := range bytes.Split(raw, []byte("\n")) {
				sp1 := bytes.IndexByte(line, ' ')
				if sp1 < 0 {
					continue
				}
				is, ok := want[lineKey(string(line[:sp1]))]
				if !ok {
					continue
				}
				rest := line[sp1+1:]
				sp2 := bytes.IndexByte(rest, ' ')
				if sp2 < 0 {
					continue
				}
				var rec struct{ URL, Mime, Status string }
				if json.Unmarshal(rest[sp2+1:], &rec) != nil {
					continue
				}
				h := hit{rec.Status, rec.Mime, string(rest[:sp2]), rec.URL}
				for _, i := range is {
					// Prefer a 200 capture over redirects and errors.
					if old, seen := best[i]; !seen || (old.status != "200" && h.status == "200") {
						best[i] = h
					}
				}
			}
		}
		n := 0
		for i, it := range items {
			h, ok := best[i]
			if ok {
				n++
			}
			fmt.Fprintf(w, "%s\t%s\t%t\t%s\t%s\t%s\t%s\n", it.raw, crawl, ok, h.status, h.mime, h.ts, h.url)
		}
		log.Printf("%s: %d of %d URLs captured (%d blocks)", crawl, n, len(items), len(ids))
	}
}
