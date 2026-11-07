// Package ccio reads Common Crawl data over HTTPS politely and robustly.
//
// Common Crawl's CDN (data.commoncrawl.org) answers bursts of requests with
// HTTP 403 or 503, and long transfers occasionally drop. Everything here
// retries with exponential backoff, and long streams resume from the current
// byte offset with a Range request.
package ccio

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"time"
)

// UserAgent identifies this project to Common Crawl.
const UserAgent = "hikeratlas-cc-crawler/0.1 (+https://github.com/hikeratlas/cc-crawler)"

// Base is the HTTPS root of Common Crawl's public dataset.
const Base = "https://data.commoncrawl.org"

// MaxAttempts bounds retries for one request or one stall in a stream. With
// backoff capped at two minutes, 12 attempts wait out ~12 minutes of throttling.
var MaxAttempts = 12

var client = &http.Client{Timeout: 0}

// Open returns a reader for a local path or an http(s) URL. URL reads resume
// from the current offset when the connection drops or the CDN throttles.
func Open(src string) (io.ReadCloser, error) {
	if strings.HasPrefix(src, "http://") || strings.HasPrefix(src, "https://") {
		return &resumingReader{url: src}, nil
	}
	return os.Open(src)
}

type resumingReader struct {
	url  string
	off  int64
	body io.ReadCloser
}

func (r *resumingReader) Read(p []byte) (int, error) {
	for attempt := 0; ; attempt++ {
		if r.body == nil {
			body, err := get(r.url, r.off, -1)
			if err != nil {
				if attempt+1 >= MaxAttempts {
					return 0, err
				}
				Backoff(attempt, err)
				continue
			}
			r.body = body
		}
		n, err := r.body.Read(p)
		r.off += int64(n)
		if err == nil || errors.Is(err, io.EOF) {
			return n, err
		}
		r.body.Close()
		r.body = nil
		if n > 0 {
			return n, nil
		}
		if attempt+1 >= MaxAttempts {
			return 0, err
		}
		Backoff(attempt, err)
	}
}

func (r *resumingReader) Close() error {
	if r.body != nil {
		return r.body.Close()
	}
	return nil
}

// FetchRange downloads bytes [off, off+length) of url, retrying on errors,
// throttling and short reads.
func FetchRange(url string, off, length int64) ([]byte, error) {
	var lastErr error
	for attempt := 0; attempt < MaxAttempts; attempt++ {
		body, err := get(url, off, length)
		if err == nil {
			var buf bytes.Buffer
			buf.Grow(int(length))
			_, err = io.Copy(&buf, body)
			body.Close()
			if err == nil && int64(buf.Len()) == length {
				return buf.Bytes(), nil
			}
			if err == nil {
				err = fmt.Errorf("short read: got %d of %d bytes", buf.Len(), length)
			}
		}
		lastErr = err
		Backoff(attempt, err)
	}
	return nil, lastErr
}

// get issues a GET, with a Range header when off > 0 or length >= 0.
func get(url string, off, length int64) (io.ReadCloser, error) {
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", UserAgent)
	ranged := off > 0 || length >= 0
	if length >= 0 {
		req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", off, off+length-1))
	} else if off > 0 {
		req.Header.Set("Range", fmt.Sprintf("bytes=%d-", off))
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	want := http.StatusOK
	if ranged {
		want = http.StatusPartialContent
	}
	if resp.StatusCode != want {
		resp.Body.Close()
		return nil, fmt.Errorf("GET %s (offset %d): %s", url, off, resp.Status)
	}
	return resp.Body, nil
}

// Backoff sleeps 2s, 4s, 8s, … capped at two minutes, and logs why.
func Backoff(attempt int, err error) {
	d := time.Duration(1<<attempt) * 2 * time.Second
	if d > 2*time.Minute {
		d = 2 * time.Minute
	}
	log.Printf("retrying in %s: %v", d, err)
	time.Sleep(d)
}
