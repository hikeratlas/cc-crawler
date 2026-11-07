package ccio

import (
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func fastTiming(t *testing.T) {
	oldMin, oldMax, oldBase := MinInterval, MaxInterval, BackoffBase
	MinInterval, MaxInterval, BackoffBase = time.Millisecond, 50*time.Millisecond, time.Millisecond
	pace = &pacer{}
	t.Cleanup(func() { MinInterval, MaxInterval, BackoffBase = oldMin, oldMax, oldBase; pace = &pacer{} })
}

// A server that answers the first n requests with 403, then serves body.
func flaky(n int32, body string) (*httptest.Server, *int32) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if atomic.AddInt32(&calls, 1) <= n {
			http.Error(w, "slow down", http.StatusForbidden)
			return
		}
		var start, end int
		if _, err := fmt.Sscanf(r.Header.Get("Range"), "bytes=%d-%d", &start, &end); err == nil {
			w.WriteHeader(http.StatusPartialContent)
			io.WriteString(w, body[start:end+1])
			return
		}
		if _, err := fmt.Sscanf(r.Header.Get("Range"), "bytes=%d-", &start); err == nil {
			w.WriteHeader(http.StatusPartialContent)
			io.WriteString(w, body[start:])
			return
		}
		io.WriteString(w, body)
	}))
	return srv, &calls
}

func TestFetchRangeRetriesAndSlowsDown(t *testing.T) {
	fastTiming(t)
	srv, calls := flaky(2, "0123456789")
	defer srv.Close()

	got, err := FetchRange(srv.URL, 3, 4)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "3456" {
		t.Fatalf("got %q", got)
	}
	if *calls != 3 {
		t.Fatalf("calls = %d, want 3", *calls)
	}
	if SlowDowns() != 2 {
		t.Fatalf("SlowDowns = %d, want 2", SlowDowns())
	}
	if pace.interval != 4*time.Millisecond {
		t.Fatalf("interval = %s, want doubled twice to 4ms", pace.interval)
	}
}

func TestPacerEasesBackAfterSuccesses(t *testing.T) {
	fastTiming(t)
	pace.interval = 40 * time.Millisecond
	for i := 0; i < 20; i++ {
		pace.ok()
	}
	if pace.interval != 36*time.Millisecond {
		t.Fatalf("interval = %s, want 36ms after 20 successes", pace.interval)
	}
}

func TestOpenStreamsWholeBodyAfterThrottling(t *testing.T) {
	fastTiming(t)
	body := strings.Repeat("abc", 1000)
	srv, _ := flaky(1, body)
	defer srv.Close()
	r, err := Open(srv.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	got, err := io.ReadAll(r)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != body {
		t.Fatalf("got %d bytes, want %d", len(got), len(body))
	}
}
