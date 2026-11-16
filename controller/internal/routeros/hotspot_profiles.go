package routeros

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"strings"
)

// copiedProfileFields are the only fields a HotSpot profile copy takes from
// the Operator's Router profile. Session timeouts and on-login/on-logout
// scripts would end or alter sessions the Router meters, so they never copy.
var copiedProfileFields = []string{
	"rate-limit", "address-list", "incoming-filter", "outgoing-filter",
	"incoming-packet-mark", "outgoing-packet-mark", "parent-queue", "transparent-proxy",
}

// ProfileName is the Controller's own Router profile for a Time Credit in the
// family of HotSpot profile p: openpiso-[p-]idle or openpiso-[p-]no-idle.
// An empty p is the base family.
func ProfileName(p string, idle bool) string {
	name := "openpiso-"
	if p != "" {
		name += p + "-"
	}
	if idle {
		return name + "idle"
	}
	return name + "no-idle"
}

// HotspotProfiles lists the Router's HotSpot user profiles a Vendo can give
// its coin customers: all but the Controller's own openpiso-* profiles.
func (c *Client) HotspotProfiles(ctx context.Context) ([]string, error) {
	rows, err := c.list(ctx, "/ip/hotspot/user/profile")
	if err != nil {
		return nil, err
	}
	profiles := make([]string, 0, len(rows))
	for _, row := range rows {
		if !strings.HasPrefix(row["name"], "openpiso-") {
			profiles = append(profiles, row["name"])
		}
	}
	slices.Sort(profiles)
	return profiles, nil
}

// EnsureProfileCopies (re)copies each HotSpot profile p into its idle and
// no-idle pair. If p was deleted on the Router, existing copies keep their
// last copied fields and only the forced ones are reapplied.
func (c *Client) EnsureProfileCopies(ctx context.Context, profiles []string, idleSeconds int) error {
	for _, p := range profiles {
		sources, err := c.list(ctx, "/ip/hotspot/user/profile?name="+url.QueryEscape(p))
		if err != nil {
			return err
		}
		copied := map[string]string{}
		if len(sources) == 1 {
			for _, field := range copiedProfileFields {
				if value, ok := sources[0][field]; ok {
					copied[field] = value
				}
			}
		}
		if err := c.ensureProfilePair(ctx, p, copied, idleSeconds); err != nil {
			return err
		}
	}
	return nil
}

// ensureProfilePair writes family p's two profiles: the copied fields plus the
// forced ones that keep one Device per Time Credit and the site Idle Pause.
func (c *Client) ensureProfilePair(ctx context.Context, p string, copied map[string]string, idleSeconds int) error {
	for _, idle := range []bool{false, true} {
		values := map[string]string{"shared-users": "1", "idle-timeout": "none", "keepalive-timeout": "none", "add-mac-cookie": "no"}
		if idle {
			values["idle-timeout"] = strconv.Itoa(idleSeconds) + "s"
		}
		for field, value := range copied {
			values[field] = value
		}
		name := ProfileName(p, idle)
		if err := c.ensureNamed(ctx, "/ip/hotspot/user/profile", name, values); err != nil {
			return fmt.Errorf("ensure HotSpot user profile %s: %w", name, err)
		}
	}
	return nil
}

// RemoveUnusedProfileCopies deletes copy profiles of HotSpot profiles no
// longer in use: not in inUse, and no HotSpot user is on them.
func (c *Client) RemoveUnusedProfileCopies(ctx context.Context, inUse []string) error {
	keep := map[string]bool{ProfileName("", false): true, ProfileName("", true): true}
	for _, p := range inUse {
		keep[ProfileName(p, false)], keep[ProfileName(p, true)] = true, true
	}
	users, err := c.list(ctx, "/ip/hotspot/user")
	if err != nil {
		return err
	}
	for _, user := range users {
		keep[user["profile"]] = true
	}
	profiles, err := c.list(ctx, "/ip/hotspot/user/profile")
	if err != nil {
		return err
	}
	for _, profile := range profiles {
		name := profile["name"]
		if keep[name] || !strings.HasPrefix(name, "openpiso-") || !strings.HasSuffix(name, "-idle") {
			continue
		}
		if err := c.request(ctx, http.MethodDelete, "/ip/hotspot/user/profile/"+profile[".id"], nil, nil); err != nil {
			return fmt.Errorf("remove HotSpot user profile %s: %w", name, err)
		}
	}
	return nil
}
