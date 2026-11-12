// Command warcfetch fetches individual pages out of Common Crawl's WARC files
// using the (warc_filename, warc_record_offset, warc_record_length) pointers
// that cmd/sitecdx and scripts/site_report.py carry along: one HTTP range
// request per record, paced through internal/ccio. For each record it writes
// one JSON line with the page title, meta description, and readable text.
// With -images it writes the page's content images instead of its text: each
// <img> with its alt, title, size, the caption next to it and the link around
// it, plus the og:image (scripts/page_images.py picks a thumbnail from them),
// and the page's GPS track links: GPX/KML/KMZ files, Google My Maps, CalTopo
// and other track sites (scripts/page_tracks.py fetches them).
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
	OGImage     string            `json:"og_image,omitempty"`
	Images      []image           `json:"images,omitempty"`
	Tracks      []trackLink       `json:"tracks,omitempty"`
	Error       string            `json:"error,omitempty"`
	Row         map[string]string `json:"row"`
}

// image is one <img> in a page's content (-images).
type image struct {
	Src     string `json:"src"`
	Alt     string `json:"alt,omitempty"`
	Title   string `json:"title,omitempty"`
	Caption string `json:"caption,omitempty"`
	Link    string `json:"link,omitempty"` // href of an <a> around it
	Width   int    `json:"width,omitempty"`
	Height  int    `json:"height,omitempty"`
}

// trackLink is a link to a GPS track in a page's content (-images).
type trackLink struct {
	Kind string `json:"kind"`           // gpx_file, google_mymaps, caltopo, gaia, strava, ...
	URL  string `json:"url"`            // as written in the page (may be relative)
	Text string `json:"text,omitempty"` // the link's text, when it is an <a>
}

var imagesMode bool

func main() {
	in := flag.String("in", "", "input TSV with url and WARC pointer columns")
	out := flag.String("out", "pages.jsonl", "output JSON lines")
	maxText := flag.Int("max-text", 6000, "truncate extracted text to this many characters (0 = no limit)")
	concurrency := flag.Int("concurrency", 2, "parallel range requests (all share one adaptive pacer)")
	flag.BoolVar(&imagesMode, "images", false, "write each page's content images (with captions) instead of its text")
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
	if imagesMode {
		r.OGImage, r.Images = images(string(body))
		r.Tracks = tracks(string(body))
		r.Text = ""
	}
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
	reList    = regexp.MustCompile(`(?is)<(?:ul|ol)\b[^>]*>.*?</(?:ul|ol)>`)
	reSelect  = regexp.MustCompile(`(?is)<select\b.*?</select>`)
	reLI      = regexp.MustCompile(`(?i)<li\b`)
	reA       = regexp.MustCompile(`(?i)<a\b`)
)

// dropMenus removes <select> dropdowns and link lists that look like
// navigation: <ul>/<ol> elements with at least six items, nearly all of
// them links. Runs a few passes so nested menus unwind.
func dropMenus(doc string) string {
	doc = reSelect.ReplaceAllString(doc, " ")
	for pass := 0; pass < 4; pass++ {
		changed := false
		doc = reList.ReplaceAllStringFunc(doc, func(m string) string {
			items := len(reLI.FindAllStringIndex(m, -1))
			links := len(reA.FindAllStringIndex(m, -1))
			if items >= 6 && links*10 >= items*8 {
				changed = true
				return " "
			}
			return m
		})
		if !changed {
			break
		}
	}
	return doc
}

// extract returns the title, meta description, and readable text of an HTML
// page. It prefers the <article> or <main> element when there is one.
func extract(doc string) (title, desc, text string) {
	if m := reTitle.FindStringSubmatch(doc); m != nil {
		title = clean(html.UnescapeString(reTag.ReplaceAllString(m[1], "")))
	}
	if m := reDesc.FindStringSubmatch(doc); m != nil {
		desc = clean(html.UnescapeString(m[1]))
	}
	body := dropMenus(reDropAll.ReplaceAllString(doc, " "))
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

var (
	reOG       = regexp.MustCompile(`(?is)<meta[^>]+property=["']og:image["'][^>]*content=["']([^"']+)["']`)
	reOG2      = regexp.MustCompile(`(?is)<meta[^>]+content=["']([^"']+)["'][^>]*property=["']og:image["']`)
	reImg      = regexp.MustCompile(`(?is)<img\b[^>]*>`)
	reAOpen    = regexp.MustCompile(`(?is)<a\b[^>]*>`)
	reCapAfter = regexp.MustCompile(`(?is)^.{0,600}?<(figcaption|[a-z0-9]+[^>]*class=["'][^"']*caption[^"']*["'])[^>]*>(.*?)</`)
	reDropImg  = regexp.MustCompile(`(?is)<(script|style|noscript|nav|header|footer|aside|form|template)\b.*?</(?:script|style|noscript|nav|header|footer|aside|form|template)>`)
)

func attr(tag, name string) string {
	re := regexp.MustCompile(`(?is)\b` + name + `\s*=\s*(?:"([^"]*)"|'([^']*)')`)
	m := re.FindStringSubmatch(tag)
	if m == nil {
		return ""
	}
	return clean(html.UnescapeString(m[1] + m[2]))
}

// images returns a page's og:image and its content images: <img> outside
// navigation, headers, footers and sidebars, with the caption that follows
// within a few hundred characters (<figcaption>, WordPress's wp-caption-text,
// Blogger's tr-caption) and the href of an <a> that wraps it.
func images(doc string) (og string, out []image) {
	if m := reOG.FindStringSubmatch(doc); m != nil {
		og = html.UnescapeString(m[1])
	} else if m := reOG2.FindStringSubmatch(doc); m != nil {
		og = html.UnescapeString(m[1])
	}
	body := reDropImg.ReplaceAllString(doc, " ")
	for _, loc := range reImg.FindAllStringIndex(body, -1) {
		tag := body[loc[0]:loc[1]]
		src := attr(tag, "data-src")
		if src == "" {
			src = attr(tag, "src")
		}
		if src == "" || strings.HasPrefix(src, "data:") {
			continue
		}
		im := image{Src: src, Alt: attr(tag, "alt"), Title: attr(tag, "title")}
		im.Width, _ = strconv.Atoi(attr(tag, "width"))
		im.Height, _ = strconv.Atoi(attr(tag, "height"))
		// The <a> around it: the last one opened before it and not closed since.
		before := body[max(0, loc[0]-2000):loc[0]]
		if as := reAOpen.FindAllStringIndex(before, -1); len(as) > 0 {
			last := as[len(as)-1]
			if !strings.Contains(strings.ToLower(before[last[1]:]), "</a>") {
				im.Link = attr(before[last[0]:last[1]], "href")
			}
		}
		if m := reCapAfter.FindStringSubmatch(body[loc[1]:min(len(body), loc[1]+1500)]); m != nil {
			im.Caption = clean(html.UnescapeString(reTag.ReplaceAllString(m[2], " ")))
		}
		out = append(out, im)
		if len(out) == 80 {
			break
		}
	}
	return
}

// Track links, by kind. A file link must end in .gpx/.kml/.kmz (a query
// string may follow); the sites' patterns name one map, track or route.
var trackPats = []struct {
	kind string
	re   *regexp.Regexp
}{
	{"gpx_file", regexp.MustCompile(`(?i)(?:href|src|data-[a-z-]+|gpx|url|file)\\?["']?\s*[=:]\s*\\?["']([^"'<>\s]+?\.(?:gpx|kml|kmz)(?:\?[^"'<>\s]*)?)\\?["']`)},
	{"google_mymaps", regexp.MustCompile(`(?i)(https?:)?//(?:www\.)?google\.[a-z.]+/maps/d/(?:u/\d+/)?(?:viewer|embed|edit|kml)\?[^"'<>\s]*mid=[A-Za-z0-9_-]+[^"'<>\s]*`)},
	{"caltopo", regexp.MustCompile(`(?i)(https?:)?//(?:www\.)?caltopo\.com/m/[A-Za-z0-9]+`)},
	{"gaia", regexp.MustCompile(`(?i)https?://(?:www\.)?gaiagps\.com/(?:public|map|hike|datasummary|track|route)/[^"'<>\s]+`)},
	{"strava", regexp.MustCompile(`(?i)https?://(?:www\.)?strava\.com/(?:activities|routes)/\d+`)},
	{"ridewithgps", regexp.MustCompile(`(?i)https?://(?:www\.)?ridewithgps\.com/(?:routes|trips)/\d+`)},
	{"alltrails", regexp.MustCompile(`(?i)https?://(?:www\.)?alltrails\.com/(?:explore/)?(?:recording|map)/[^"'<>\s?#]+`)},
	{"garmin", regexp.MustCompile(`(?i)https?://connect\.garmin\.com/(?:modern/)?(?:activity|course)/\d+`)},
	{"wikiloc", regexp.MustCompile(`(?i)https?://(?:www\.)?wikiloc\.com/[a-z-]+-trails/[^"'<>\s?#]+`)},
	{"outdooractive", regexp.MustCompile(`(?i)https?://(?:www\.)?outdooractive\.com/[a-z]{2}/(?:route|track)/[^"'<>\s?#]+`)},
	{"mapmy", regexp.MustCompile(`(?i)https?://(?:www\.)?mapmy(?:hike|run|walk|ride|fitness)\.com/(?:[a-z]{2}/)?routes/view/\d+`)},
	{"plotaroute", regexp.MustCompile(`(?i)https?://(?:www\.)?plotaroute\.com/route/\d+`)},
	{"trailforks", regexp.MustCompile(`(?i)https?://(?:www\.)?trailforks\.com/route/[^"'<>\s?#]+`)},
}

var (
	reDropTrack = regexp.MustCompile(`(?is)<(nav|header|footer|aside)\b.*?</(?:nav|header|footer|aside)>`)
	reAClose    = regexp.MustCompile(`(?is)^[^>]*>(.{0,300}?)</a>`)
)

// tracks returns a page's GPS track links, outside navigation, headers,
// footers and sidebars (which link other posts' tracks), in page order,
// each once.
func tracks(doc string) (out []trackLink) {
	body := reDropTrack.ReplaceAllString(doc, " ")
	seen := map[string]bool{}
	for _, p := range trackPats {
		for _, m := range p.re.FindAllStringSubmatchIndex(body, -1) {
			s, e := m[0], m[1]
			if p.kind == "gpx_file" {
				s, e = m[2], m[3]
			}
			u := html.UnescapeString(strings.ReplaceAll(body[s:e], `\/`, "/"))
			if strings.HasPrefix(u, "//") {
				u = "https:" + u
			}
			if seen[u] {
				continue
			}
			seen[u] = true
			t := trackLink{Kind: p.kind, URL: u}
			// Link text: the match sits inside an <a ...> tag's attributes.
			before := body[max(0, s-1000):s]
			if i := strings.LastIndex(strings.ToLower(before), "<a "); i >= 0 && !strings.Contains(before[i:], ">") {
				if c := reAClose.FindStringSubmatch(body[e:min(len(body), e+1500)]); c != nil {
					t.Text = clean(html.UnescapeString(reTag.ReplaceAllString(c[1], " ")))
				}
			}
			out = append(out, t)
			if len(out) == 40 {
				return
			}
		}
	}
	return
}
