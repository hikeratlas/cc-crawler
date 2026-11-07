// Command warcfetch fetches individual pages out of Common Crawl's WARC files
// using the (warc_filename, warc_record_offset, warc_record_length) pointers
// that cmd/sitecdx and scripts/site_report.py carry along: one HTTP range
// request per record, paced through internal/ccio. For each record it writes
// one JSON line with the page title, meta description, and readable text.
//
// Input is a TSV with a header that includes url, warc_filename,
// warc_record_offset and warc_record_length; every other column is copied
// into the output's "row" object.
package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"encoding/json"
	"flag"
	"fmt"
	"html"
	"io"
	"log"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"

	"github.com/hikeratlas/cc-crawler/internal/ccio"
)

type result struct {
	URL         string            `json:"url"`
	HTTPStatus  int               `json:"http_status,omitempty"`
	Title       string            `json:"title,omitempty"`
	Description string            `json:"description,omitempty"`
	Words       int               `json:"words"`
	Text        string            `json:"text,omitempty"`
	Error       string            `json:"error,omitempty"`
	Row         map[string]string `json:"row"`
}

func main() {
	in := flag.String("in", "", "input TSV with url and WARC pointer columns")
	out := flag.String("out", "pages.jsonl", "output JSON lines")
	maxText := flag.Int("max-text", 6000, "truncate extracted text to this many characters (0 = no limit)")
	concurrency := flag.Int("concurrency", 2, "parallel range requests (all share one adaptive pacer)")
	flag.Parse()

	f, err := os.Open(*in)
	must(err)
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	sc.Scan()
	header := strings.Split(sc.Text(), "\t")
	var rows []map[string]string
	for sc.Scan() {
		vals := strings.Split(sc.Text(), "\t")
		row := map[string]string{}
		for i, h := range header {
			if i < len(vals) {
				row[h] = vals[i]
			}
		}
		rows = append(rows, row)
	}
	must(sc.Err())

	results := make([]result, len(rows))
	jobs := make(chan int)
	var wg sync.WaitGroup
	for k := 0; k < *concurrency; k++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range jobs {
				results[i] = fetch(rows[i], *maxText)
			}
		}()
	}
	for i := range rows {
		jobs <- i
		if (i+1)%50 == 0 {
			log.Printf("%d/%d records", i+1, len(rows))
		}
	}
	close(jobs)
	wg.Wait()

	fo, err := os.Create(*out)
	must(err)
	w := bufio.NewWriter(fo)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	failed := 0
	for _, r := range results {
		if r.Error != "" {
			failed++
		}
		must(enc.Encode(r))
	}
	must(w.Flush())
	must(fo.Close())
	log.Printf("wrote %d records to %s (%d errors, %d rate-limit responses)", len(results), *out, failed, ccio.SlowDowns())
}

func fetch(row map[string]string, maxText int) result {
	r := result{URL: row["url"], Row: row}
	off, err1 := strconv.ParseInt(row["warc_record_offset"], 10, 64)
	n, err2 := strconv.ParseInt(row["warc_record_length"], 10, 64)
	if err1 != nil || err2 != nil || row["warc_filename"] == "" {
		r.Error = "missing WARC pointer"
		return r
	}
	data, err := ccio.FetchRange(ccio.Base+"/"+row["warc_filename"], off, n)
	if err != nil {
		r.Error = err.Error()
		return r
	}
	gz, err := gzip.NewReader(bytes.NewReader(data))
	if err != nil {
		r.Error = "gzip: " + err.Error()
		return r
	}
	raw, err := io.ReadAll(gz)
	if err != nil {
		r.Error = "gzip: " + err.Error()
		return r
	}
	// WARC headers, blank line, HTTP headers, blank line, body.
	parts := bytes.SplitN(raw, []byte("\r\n\r\n"), 3)
	if len(parts) < 3 {
		r.Error = "unexpected record layout"
		return r
	}
	httpHead, body := parts[1], parts[2]
	if fields := strings.Fields(string(bytes.SplitN(httpHead, []byte("\r\n"), 2)[0])); len(fields) >= 2 {
		r.HTTPStatus, _ = strconv.Atoi(fields[1])
	}
	r.Title, r.Description, r.Text = extract(string(body))
	r.Words = len(strings.Fields(r.Text))
	if maxText > 0 && len(r.Text) > maxText {
		r.Text = r.Text[:maxText]
	}
	return r
}

var (
	reTitle   = regexp.MustCompile(`(?is)<title[^>]*>(.*?)</title>`)
	reDesc    = regexp.MustCompile(`(?is)<meta[^>]+(?:name|property)=["'](?:description|og:description)["'][^>]*content=["']([^"']*)["']`)
	reDropAll = regexp.MustCompile(`(?is)<(script|style|noscript|svg|nav|header|footer|aside|form|iframe|template)\b.*?</(?:script|style|noscript|svg|nav|header|footer|aside|form|iframe|template)>`)
	reMain    = regexp.MustCompile(`(?is)<(article|main)\b[^>]*>(.*)</(?:article|main)>`)
	reBlock   = regexp.MustCompile(`(?i)</?(p|div|br|li|h[1-6]|tr|section|blockquote)\b[^>]*>`)
	reTag     = regexp.MustCompile(`(?s)<[^>]*>`)
	reSpace   = regexp.MustCompile(`[ \t\r\f\v]+`)
	reLines   = regexp.MustCompile(`\n\s*\n+`)
)

// extract returns the title, meta description, and readable text of an HTML
// page. It prefers the <article> or <main> element when there is one.
func extract(doc string) (title, desc, text string) {
	if m := reTitle.FindStringSubmatch(doc); m != nil {
		title = clean(html.UnescapeString(reTag.ReplaceAllString(m[1], "")))
	}
	if m := reDesc.FindStringSubmatch(doc); m != nil {
		desc = clean(html.UnescapeString(m[1]))
	}
	body := reDropAll.ReplaceAllString(doc, " ")
	if m := reMain.FindStringSubmatch(body); m != nil && len(m[2]) > 500 {
		body = m[2]
	}
	body = reBlock.ReplaceAllString(body, "\n")
	body = html.UnescapeString(reTag.ReplaceAllString(body, " "))
	body = reSpace.ReplaceAllString(body, " ")
	text = strings.TrimSpace(reLines.ReplaceAllString(body, "\n"))
	return
}

func clean(s string) string {
	return strings.TrimSpace(reSpace.ReplaceAllString(strings.ReplaceAll(s, "\n", " "), " "))
}

func must(err error) {
	if err != nil {
		log.Fatal(fmt.Sprint(err))
	}
}
