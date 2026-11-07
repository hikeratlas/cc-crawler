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
