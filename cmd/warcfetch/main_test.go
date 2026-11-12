package main

import (
	"strings"
	"testing"
)

func TestExtractDropsMenusKeepsContent(t *testing.T) {
	menu := "<ul>" + strings.Repeat(`<li><a href="/x">Hikes in Utah</a></li>`, 10) + "</ul>"
	doc := `<html><head><title>Lake Desolation Hike</title>
<meta name="description" content="A trip report."></head><body>` + menu + `
<p>We started at the trailhead at mile 11 and climbed 2,500 feet.</p>
<ul><li>Distance: 8 miles</li><li>Gain: 2,500 ft</li></ul>
<select><option>A</option><option>B</option><option>C</option><option>D</option><option>E</option><option>F</option></select>
</body></html>`
	title, desc, text := extract(doc)
	if title != "Lake Desolation Hike" || desc != "A trip report." {
		t.Fatalf("title/desc = %q / %q", title, desc)
	}
	if strings.Contains(text, "Hikes in Utah") {
		t.Errorf("menu text survived: %q", text)
	}
	if strings.Contains(text, "A B C") {
		t.Errorf("select options survived: %q", text)
	}
	for _, want := range []string{"trailhead at mile 11", "Distance: 8 miles"} {
		if !strings.Contains(text, want) {
			t.Errorf("missing %q in %q", want, text)
		}
	}
}

func TestImagesFindsCaptionsAndLinks(t *testing.T) {
	doc := `<html><head><meta property="og:image" content="https://x.org/hero.jpg"></head><body>
<header><img src="/logo.png" alt="Site logo"></header>
<figure><img src="https://x.org/a.jpg" alt="Morning at Garnet Lake" width="800"><figcaption>Garnet Lake from the <b>outlet</b></figcaption></figure>
<p><a href="https://x.org/other-post/"><img src="https://x.org/thumb.jpg" alt="Another post"></a></p>
<div class="wp-caption"><img src="https://x.org/b.jpg"><p class="wp-caption-text">Ritter Range</p></div>
<footer><img src="/badge.png"></footer></body></html>`
	og, ims := images(doc)
	if og != "https://x.org/hero.jpg" {
		t.Fatalf("og = %q", og)
	}
	if len(ims) != 3 {
		t.Fatalf("got %d images: %+v", len(ims), ims)
	}
	if ims[0].Caption != "Garnet Lake from the outlet" || ims[0].Width != 800 || ims[0].Alt != "Morning at Garnet Lake" {
		t.Errorf("figure image: %+v", ims[0])
	}
	if ims[1].Link != "https://x.org/other-post/" {
		t.Errorf("linked image: %+v", ims[1])
	}
	if ims[2].Caption != "Ritter Range" {
		t.Errorf("wp-caption image: %+v", ims[2])
	}
}
