package routeros

import (
	"context"
	"fmt"
	"slices"
	"strings"
)

func (c *Client) HotspotServers(ctx context.Context) ([]string, error) {
	rows, err := c.list(ctx, "/ip/hotspot")
	if err != nil {
		return nil, err
	}
	servers := make([]string, 0, len(rows))
	for _, row := range rows {
		servers = append(servers, row["name"])
	}
	slices.Sort(servers)
	return servers, nil
}

// HotspotServer is one HotSpot server on the Router and its server profile.
type HotspotServer struct {
	Name      string
	Interface string
	Profile   string
}

// ListHotspotServers lists the Router's HotSpot servers by name.
func (c *Client) ListHotspotServers(ctx context.Context) ([]HotspotServer, error) {
	rows, err := c.list(ctx, "/ip/hotspot")
	if err != nil {
		return nil, err
	}
	servers := make([]HotspotServer, 0, len(rows))
	for _, row := range rows {
		servers = append(servers, HotspotServer{Name: row["name"], Interface: row["interface"], Profile: row["profile"]})
	}
	slices.SortFunc(servers, func(a, b HotspotServer) int { return strings.Compare(a.Name, b.Name) })
	return servers, nil
}

// CheckAdoptable refuses adopting names, added to the already adopted ones,
// before any Router write: each must be a server on the Router, and its server
// profile, which Adopting changes, must not also serve a server left out.
func (c *Client) CheckAdoptable(ctx context.Context, names, adopted []string) error {
	servers, err := c.ListHotspotServers(ctx)
	if err != nil {
		return fmt.Errorf("list HotSpot servers: %w", err)
	}
	managed := append(slices.Clone(adopted), names...)
	for _, name := range names {
		index := slices.IndexFunc(servers, func(server HotspotServer) bool { return server.Name == name })
		if index < 0 {
			return fmt.Errorf("the Router has no HotSpot server %s", name)
		}
		for _, other := range servers {
			if other.Profile == servers[index].Profile && !slices.Contains(managed, other.Name) {
				return fmt.Errorf("HotSpot server %s shares server profile %s with %s; tick both, or give %s its own server profile", name, other.Profile, other.Name, other.Name)
			}
		}
	}
	return nil
}
