package controller

import (
	"context"
	"sync"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

const (
	usageTTL     = 5 * time.Second
	usageTimeout = 8 * time.Second
)

// usageSnapshot is the admin UI's view of Router metering. Known is false until
// a read has ever succeeded; Stale means the latest refresh failed and the data
// is the last good reading.
type usageSnapshot struct {
	Usage routeros.Usage
	Known bool
	Stale bool
}

// usageCache shares one Router usage read between all admin pages and polls.
// Its mutex guards only the cache fields and is never held during the Router
// call (and is unrelated to voucherMu). Callers arriving during a refresh wait
// for that same refresh instead of starting their own.
type usageCache struct {
	mu        sync.Mutex
	snapshot  usageSnapshot
	attempted time.Time // last refresh start, successful or not
	flight    *usageFlight
}

type usageFlight struct {
	done     chan struct{}
	snapshot usageSnapshot
}

// usageSnapshot returns the Router usage no older than about usageTTL. A
// failed refresh keeps serving the last good reading, marked stale.
func (s *Server) usageSnapshot() usageSnapshot {
	cache := &s.usage
	cache.mu.Lock()
	if cache.flight == nil && time.Since(cache.attempted) < usageTTL {
		snapshot := cache.snapshot
		cache.mu.Unlock()
		return snapshot
	}
	if flight := cache.flight; flight != nil {
		cache.mu.Unlock()
		<-flight.done
		return flight.snapshot
	}
	flight := &usageFlight{done: make(chan struct{})}
	cache.flight, cache.attempted = flight, time.Now()
	cache.mu.Unlock()

	usage, err := s.readUsage()

	cache.mu.Lock()
	if err == nil {
		cache.snapshot = usageSnapshot{Usage: usage, Known: true}
	} else {
		cache.snapshot.Stale = true
	}
	flight.snapshot = cache.snapshot
	cache.flight = nil
	cache.mu.Unlock()
	close(flight.done)
	return flight.snapshot
}

// readUsage runs on its own timeout, not a request's context, because the
// result is shared with every caller waiting on this refresh.
func (s *Server) readUsage() (routeros.Usage, error) {
	router, err := s.router()
	if err != nil {
		return routeros.Usage{}, err
	}
	defer router.Close()
	ctx, cancel := context.WithTimeout(context.Background(), usageTimeout)
	defer cancel()
	return router.UsageSnapshot(ctx)
}
