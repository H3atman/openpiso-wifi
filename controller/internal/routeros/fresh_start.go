package routeros

import (
	"context"
	"fmt"
	"net/http"
	"net/netip"
	"net/url"
	"slices"
)

// Fresh start (ADR 0011): delete the Router's HotSpot tree, then create one
// HotSpot server with CreateHotspotServer. Nothing outside the tree is deleted:
// addresses, pools, DHCP, bridges, routes and firewall rules stay, so the new
// server reuses the old interface's network as it is.

// hotspotTree is every path Fresh start deletes from, in the order it deletes:
// active sessions before their users, servers before their server profiles and
// users before their HotSpot profiles. RouterOS's default rows and dynamic rows
// (made by the HotSpot itself) are never deleted, nor counted.
var hotspotTree = []string{
	"/ip/hotspot/active",
	"/ip/hotspot/user",
	"/ip/hotspot/ip-binding",
	"/ip/hotspot/walled-garden",
	"/ip/hotspot/walled-garden/ip",
	"/ip/hotspot",
	"/ip/hotspot/profile",
	"/ip/hotspot/user/profile",
}

// HotspotCounts is what Fresh start deletes, as the Operator is shown it.
type HotspotCounts struct {
	Servers, Users, Active, WalledGarden int
}

// FreshStartPlan is what Fresh start would delete and where its new server
// goes: Interfaces are those free once the old servers are gone, Interface the
// default (the first old server's), Range that interface's own range when it
// has an address, else a suggested one.
type FreshStartPlan struct {
	Counts     HotspotCounts
	Interfaces []string
	Interface  string
	Range      netip.Prefix
}

// PlanFreshStart reads the Fresh start plan. It writes nothing.
func (c *Client) PlanFreshStart(ctx context.Context) (FreshStartPlan, error) {
	var plan FreshStartPlan
	rows := map[string]int{}
	for _, path := range hotspotTree {
		found, err := c.hotspotRows(ctx, path)
		if err != nil {
			return plan, err
		}
		rows[path] = len(found)
	}
	plan.Counts = HotspotCounts{Servers: rows["/ip/hotspot"], Users: rows["/ip/hotspot/user"], Active: rows["/ip/hotspot/active"],
		WalledGarden: rows["/ip/hotspot/walled-garden"] + rows["/ip/hotspot/walled-garden/ip"]}
	free, err := c.FreeInterfaces(ctx)
	if err != nil {
		return plan, err
	}
	servers, err := c.ListHotspotServers(ctx)
	if err != nil {
		return plan, err
	}
	plan.Interfaces = free
	for _, server := range servers {
		plan.Interfaces = append(plan.Interfaces, server.Interface)
	}
	slices.Sort(plan.Interfaces)
	plan.Interfaces = slices.Compact(plan.Interfaces)
	switch {
	case len(servers) > 0:
		plan.Interface = servers[0].Interface
	case len(free) > 0:
		plan.Interface = free[0]
	default:
		return plan, nil
	}
	network, ok, err := c.interfaceRange(ctx, plan.Interface)
	if err != nil {
		return plan, err
	}
	if !ok {
		if network, err = c.SuggestRange(ctx); err != nil {
			return plan, err
		}
	}
	plan.Range = network
	return plan, nil
}

// CheckFreshStart refuses, before any write, a Fresh start that could not
// create its server: placement must be an interface free once the old servers
// are gone or a new VLAN checkVLAN accepts, and network a range CheckRange
// accepts there.
func (c *Client) CheckFreshStart(ctx context.Context, placement Placement, network netip.Prefix) error {
	iface := placement.Interface
	if placement.VLAN != 0 {
		name, exists, err := c.checkVLAN(ctx, placement.Parent, placement.VLAN)
		if err != nil {
			return err
		}
		if iface = name; !exists {
			return c.CheckRange(ctx, iface, network)
		}
	}
	plan, err := c.PlanFreshStart(ctx)
	if err != nil {
		return err
	}
	if !slices.Contains(plan.Interfaces, iface) {
		return Refusal(fmt.Sprintf("interface %s is missing, is a bridge port or carries the Controller's connection to the Router", iface))
	}
	return c.CheckRange(ctx, iface, network)
}

// WipeHotspot deletes the HotSpot tree (hotspotTree) and nothing else. A wipe
// that fails midway is finished by running it again.
func (c *Client) WipeHotspot(ctx context.Context) error {
	for _, path := range hotspotTree {
		rows, err := c.hotspotRows(ctx, path)
		if err != nil {
			return err
		}
		for _, row := range rows {
			if err := c.request(ctx, http.MethodDelete, path+"/"+row[".id"], nil, nil); err != nil {
				return fmt.Errorf("delete %s %s: %w", path, row[".id"], err)
			}
		}
	}
	return nil
}

// BackupSaved reports whether the Router still holds backup file name.
func (c *Client) BackupSaved(ctx context.Context, name string) (bool, error) {
	files, err := c.list(ctx, "/file?name="+url.QueryEscape(name))
	return len(files) > 0, err
}

// hotspotRows are path's rows Fresh start deletes: all but RouterOS's default
// and dynamic ones.
func (c *Client) hotspotRows(ctx context.Context, path string) ([]map[string]string, error) {
	rows, err := c.list(ctx, path)
	return slices.DeleteFunc(rows, func(row map[string]string) bool {
		return row["default"] == "true" || row["dynamic"] == "true"
	}), err
}
