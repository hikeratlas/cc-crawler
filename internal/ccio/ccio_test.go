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
	oldMin, oldMax, oldBase, oldHL := MinInterval, MaxInterval, BackoffBase, RecoveryHalfLife
	MinInterval, MaxInterval, BackoffBase, RecoveryHalfLife = time.Millisecond, 50*time.Millisecond, 5*time.Millisecond, time.Hour
	pace = &pacer{}
	t.Cleanup(func() {
		MinInterval, MaxInterval, BackoffBase, RecoveryHalfLife = oldMin, oldMax, oldBase, oldHL
		pace = &pacer{}
	})
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
	// The two refusals came >= 5ms apart but < 2s apart: one burst, one doubling.
	if pace.interval != 2*time.Millisecond {
		t.Fatalf("interval = %s, want one doubling to 2ms", pace.interval)
	}
}

func TestBurstCountsOnceAndSeparateBurstsCompound(t *testing.T) {
	fastTiming(t)
	for i := 0; i < 5; i++ {
		pace.throttled()
	}
	if pace.interval != 2*time.Millisecond {
		t.Fatalf("interval after one burst = %s, want 2ms", pace.interval)
	}
	pace.lastThrottle = pace.lastThrottle.Add(-3 * time.Second) // next signal is a new burst
	pace.throttled()
	if d := pace.interval - 4*time.Millisecond; d > 10*time.Microsecond || d < -10*time.Microsecond {
		t.Fatalf("interval after second burst = %s, want ~4ms", pace.interval)
	}
}

func TestPacerRecoversWithHalfLife(t *testing.T) {
	fastTiming(t)
	MinInterval, RecoveryHalfLife = 100*time.Millisecond, 30*time.Second
	now := time.Now()
	pace.interval, pace.lastThrottle = 30*time.Second+100*time.Millisecond, now
	cases := []struct {
		after time.Duration
		want  time.Duration
	}{
		{0, 30*time.Second + 100*time.Millisecond},
		{30 * time.Second, 15*time.Second + 100*time.Millisecond},
		{60 * time.Second, 7500*time.Millisecond + 100*time.Millisecond},
		{20 * time.Minute, 100 * time.Millisecond},
	}
	for _, c := range cases {
		got := pace.current(now.Add(c.after))
		if d := got - c.want; d > time.Millisecond || d < -time.Millisecond {
			t.Errorf("after %s: interval %s, want %s", c.after, got, c.want)
		}
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
