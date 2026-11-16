package routeros

import (
	"context"
	"errors"
	"maps"
	"strings"
)

// Usage is one read-only Router snapshot for every Time Credit: committed plus
// live uptime per credit (the same quantity as UsedSeconds) and which credits
// have an active HotSpot session.
type Usage struct {
	UsedSeconds map[string]int64
	Active      map[string]bool
}

// UsageSnapshot lists HotSpot users and active sessions in three REST calls
// regardless of how many Time Credits exist. The users list is read on both
// sides of the active list; if an uptime moved (a session ended mid-read) it
// retries, and after three tries returns an error rather than a possibly
// double-counted reading.
func (c *Client) UsageSnapshot(ctx context.Context) (Usage, error) {
	usage := Usage{UsedSeconds: map[string]int64{}, Active: map[string]bool{}}
	for attempt := 0; attempt < 3; attempt++ {
		before, err := c.list(ctx, "/ip/hotspot/user")
		if err != nil {
			return usage, err
		}
		active, err := c.list(ctx, "/ip/hotspot/active")
		if err != nil {
			return usage, err
		}
		after, err := c.list(ctx, "/ip/hotspot/user")
		if err != nil {
			return usage, err
		}
		usage = Usage{UsedSeconds: map[string]int64{}, Active: map[string]bool{}}
		uptimes := userUptimes(after)
		for id, uptime := range uptimes {
			seconds, err := durationSeconds(uptime)
			if err != nil {
				return usage, err
			}
			usage.UsedSeconds[id] = seconds
		}
		for _, entry := range active {
			id, ok := strings.CutPrefix(entry["user"], "op-")
			if !ok {
				continue
			}
			live, err := durationSeconds(entry["uptime"])
			if err != nil {
				return usage, err
			}
			usage.UsedSeconds[id] += live
			usage.Active[id] = true
		}
		if maps.Equal(userUptimes(before), uptimes) {
			return usage, nil
		}
	}
	return Usage{}, errors.New("Router usage changed while reading; try again")
}

// userUptimes maps Time Credit ID to the HotSpot user's committed uptime string.
func userUptimes(users []map[string]string) map[string]string {
	uptimes := map[string]string{}
	for _, user := range users {
		if id, ok := strings.CutPrefix(user["name"], "op-"); ok {
			uptimes[id] = user["uptime"]
		}
	}
	return uptimes
}
