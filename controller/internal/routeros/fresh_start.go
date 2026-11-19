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
	// The wipe removes every HotSpot server, so only that waives an interface.
	free, err := c.freeInterfaces(ctx, func(HotspotServer) bool { return true })
	if err != nil {
		return plan, err
	}
	plan.Interfaces = free
	servers, err := c.ListHotspotServers(ctx)
	if err != nil {
		return plan, err
	}
	if index := slices.IndexFunc(servers, func(server HotspotServer) bool { return slices.Contains(plan.Interfaces, server.Interface) }); index >= 0 {
		plan.Interface = servers[index].Interface
	} else if len(plan.Interfaces) > 0 {
		plan.Interface = plan.Interfaces[0]
	} else {
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
// create its server: it applies CreateHotspotServer's checks, waiving only
// those the wipe settles (a HotSpot server, or its server profile, already on
// the interface or holding the name).
func (c *Client) CheckFreshStart(ctx context.Context, placement Placement, network netip.Prefix) error {
	_, _, err := c.checkPlacement(ctx, placement, network, true)
	return err
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

// hotspotPlaceholder is the comment on the disabled walled garden entry RouterOS
// adds with the HotSpot to mark where rules go, as it does in the firewall.
const hotspotPlaceholder = "place hotspot rules here"

// hotspotRows are path's rows Fresh start deletes: all but RouterOS's default,
// dynamic and placeholder ones.
func (c *Client) hotspotRows(ctx context.Context, path string) ([]map[string]string, error) {
	rows, err := c.list(ctx, path)
	return slices.DeleteFunc(rows, func(row map[string]string) bool {
		return row["default"] == "true" || row["dynamic"] == "true" || row["comment"] == hotspotPlaceholder
	}), err
}
