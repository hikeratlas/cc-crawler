// Package ccio reads Common Crawl data over HTTPS politely and robustly.
//
// Common Crawl's CDN (data.commoncrawl.org) enforces explicit rate-limit
// rules: requests over the limit get HTTP 403, and S3 backpressure shows up
// as 503 (see https://status.commoncrawl.org/). Their guidance is to slow
// down when you see either, not just retry. So every request here goes
// through one shared pacer that starts at MinInterval between requests,
// doubles the interval on each burst of 403/503/429 (up to MaxInterval), and
// eases back with a RecoveryHalfLife once the refusals stop. Failed requests also retry with backoff,
// and long streams resume from the current byte offset with a Range request.
package ccio

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// UserAgent identifies this project to Common Crawl.
const UserAgent = "hikeratlas-cc-crawler/0.1 (+https://github.com/hikeratlas/cc-crawler)"

// Base is the HTTPS root of Common Crawl's public dataset.
const Base = "https://data.commoncrawl.org"

// MaxAttempts bounds retries for one request or one stall in a stream. With
// backoff capped at two minutes, 12 attempts wait out ~17 minutes of throttling.
var MaxAttempts = 12

var client = &http.Client{Timeout: 0}

// BackoffBase is the first retry delay; each further retry doubles it.
var BackoffBase = 5 * time.Second

// Pacing bounds. MinInterval is the fastest steady rate (requests start at
// most this often across the whole process); MaxInterval is the slowest.
var (
	MinInterval = 250 * time.Millisecond
	MaxInterval = 30 * time.Second
)

type pacer struct {
	mu           sync.Mutex
	interval     time.Duration // interval right after the last throttle signal
	lastThrottle time.Time
	next         time.Time
	slowed       int // number of slow-down signals seen
}

var pace = &pacer{}

// RecoveryHalfLife is how quickly the extra delay decays once rate-limit
// responses stop: it halves every RecoveryHalfLife without a new signal.
var RecoveryHalfLife = 30 * time.Second

// current returns the pacing interval now: the interval set at the last
// throttle, decaying exponentially back to MinInterval. Caller holds mu.
func (p *pacer) current(now time.Time) time.Duration {
	if p.interval <= MinInterval || p.lastThrottle.IsZero() {
		return MinInterval
	}
	halves := float64(now.Sub(p.lastThrottle)) / float64(RecoveryHalfLife)
	extra := time.Duration(float64(p.interval-MinInterval) * math.Pow(0.5, halves))
	if extra*100 < MinInterval { // within 1% of the floor
		return MinInterval
	}
	return MinInterval + extra
}

// wait blocks until this caller may start a request.
func (p *pacer) wait() {
	p.mu.Lock()
	now := time.Now()
	start := p.next
	if start.Before(now) {
		start = now
	}
	p.next = start.Add(p.current(start))
	p.mu.Unlock()
	time.Sleep(time.Until(start))
}

// throttled records a rate-limit signal. Several signals within two seconds
// (e.g. concurrent requests refused together) count as one: the interval
// doubles once per burst, up to MaxInterval.
func (p *pacer) throttled() {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.slowed++
	now := time.Now()
	if !p.lastThrottle.IsZero() && now.Sub(p.lastThrottle) < 2*time.Second {
		return
	}
	cur := p.current(now)
	p.interval = cur * 2
	if p.interval > MaxInterval {
		p.interval = MaxInterval
	}
	p.lastThrottle = now
	p.next = now.Add(p.interval)
	log.Printf("rate limited: pacing requests %s apart, easing back with a %s half-life", p.interval, RecoveryHalfLife)
}

// ok records a success. Recovery is time-based (see current), so there is
// nothing to do; kept as a hook for future use.
func (p *pacer) ok() {}

// SlowDowns reports how many rate-limit responses this process has seen.
func SlowDowns() int {
	pace.mu.Lock()
	defer pace.mu.Unlock()
	return pace.slowed
}

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
	pace.wait()
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
		switch resp.StatusCode {
		case http.StatusForbidden, http.StatusTooManyRequests, http.StatusServiceUnavailable:
			pace.throttled()
		}
		return nil, fmt.Errorf("GET %s (offset %d): %s", url, off, resp.Status)
	}
	pace.ok()
	return resp.Body, nil
}

// Backoff sleeps 5s, 10s, 20s, … capped at two minutes, and logs why.
// (Common Crawl suggests backing off to at least ~10 s between retries.)
func Backoff(attempt int, err error) {
	d := time.Duration(1<<attempt) * BackoffBase
	if d > 2*time.Minute {
		d = 2 * time.Minute
	}
	log.Printf("retrying in %s: %v", d, err)
	time.Sleep(d)
}
