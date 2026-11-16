package routeros

import (
	"context"
	"slices"
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
