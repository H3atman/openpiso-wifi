package routeros

import (
	"context"
	"fmt"
	"net/url"
	"slices"
	"strconv"
)

// A new VLAN for a HotSpot server, Add alongside's default placement: VLAN
// interface <parent>-vlan<id> on an Ethernet, bridge or bond interface. The
// Controller never creates one on a bridge with VLAN filtering on, which would
// also need a bridge VLAN entry, and a wrong entry can cut off access to the
// Router; the Operator gets the manual steps instead.

// VLANParents lists, sorted, the interfaces a new VLAN can go on: Ethernet,
// bridge and bond interfaces that are neither bridge ports nor dynamic. under
// is the one under the first HotSpot server that has one (the parent of its
// VLAN when it is on a VLAN), else the first listed.
func (c *Client) VLANParents(ctx context.Context) (parents []string, under string, err error) {
	interfaces, err := c.list(ctx, "/interface")
	if err != nil {
		return nil, "", err
	}
	for _, row := range interfaces {
		if row["dynamic"] != "true" && row["slave"] != "true" && slices.Contains([]string{"ether", "bridge", "bond"}, row["type"]) {
			parents = append(parents, row["name"])
		}
	}
	slices.Sort(parents)
	servers, err := c.ListHotspotServers(ctx)
	if err != nil {
		return nil, "", err
	}
	vlans, err := c.list(ctx, "/interface/vlan")
	if err != nil {
		return nil, "", err
	}
	for _, server := range servers {
		iface := server.Interface
		if index := slices.IndexFunc(vlans, func(vlan map[string]string) bool { return vlan["name"] == iface }); index >= 0 {
			iface = vlans[index]["interface"]
		}
		if slices.Contains(parents, iface) {
			return parents, iface, nil
		}
	}
	if len(parents) > 0 {
		under = parents[0]
	}
	return parents, under, nil
}

// SuggestVLAN is the lowest VLAN ID not in use on parent, from 2: VLAN 1 is
// the untagged default on most switches and access points.
func (c *Client) SuggestVLAN(ctx context.Context, parent string) (int, error) {
	vlans, err := c.list(ctx, "/interface/vlan?interface="+url.QueryEscape(parent))
	if err != nil {
		return 0, err
	}
	for id := 2; id <= 4094; id++ {
		if !slices.ContainsFunc(vlans, func(vlan map[string]string) bool { return vlan["vlan-id"] == strconv.Itoa(id) }) {
			return id, nil
		}
	}
	return 0, fmt.Errorf("every VLAN ID is in use on %s", parent)
}

// VLANIDs maps each VLAN interface on the Router to its VLAN ID.
func (c *Client) VLANIDs(ctx context.Context) (map[string]int, error) {
	vlans, err := c.list(ctx, "/interface/vlan")
	if err != nil {
		return nil, err
	}
	ids := make(map[string]int, len(vlans))
	for _, vlan := range vlans {
		if id, err := strconv.Atoi(vlan["vlan-id"]); err == nil {
			ids[vlan["name"]] = id
		}
	}
	return ids, nil
}

// checkVLAN refuses VLAN id on parent before any write and returns the VLAN
// interface's name, and whether it is already there from a create that failed
// midway: id must be a VLAN ID, parent one of VLANParents and not a bridge with
// VLAN filtering on, and id not in use on parent.
func (c *Client) checkVLAN(ctx context.Context, parent string, id int) (string, bool, error) {
	if id < 1 || id > 4094 {
		return "", false, Refusal(fmt.Sprintf("VLAN ID %d is not from 1 to 4094", id))
	}
	parents, _, err := c.VLANParents(ctx)
	if err != nil {
		return "", false, err
	}
	if !slices.Contains(parents, parent) {
		return "", false, Refusal(fmt.Sprintf("interface %s is missing, a bridge port, or not an Ethernet, bridge or bond interface", parent))
	}
	iface := fmt.Sprintf("%s-vlan%d", parent, id)
	bridges, err := c.list(ctx, "/interface/bridge?name="+url.QueryEscape(parent))
	if err != nil {
		return "", false, err
	}
	if len(bridges) > 0 && bridges[0]["vlan-filtering"] == "true" {
		return "", false, Refusal(fmt.Sprintf("Bridge %[1]s has VLAN filtering on, and a wrong bridge VLAN entry can cut off access to the Router, "+
			"so the Controller does not create the VLAN there. On the Router, add VLAN %[2]d to the bridge, tagged on the bridge itself "+
			"and on the ports to your access points: /interface bridge vlan add bridge=%[1]s tagged=%[1]s,<access point ports> vlan-ids=%[2]d. "+
			"Then add its interface: /interface vlan add name=%[3]s interface=%[1]s vlan-id=%[2]d. "+
			"Then create the HotSpot server on interface %[3]s, an interface with no HotSpot server.", parent, id, iface))
	}
	vlans, err := c.list(ctx, "/interface/vlan")
	if err != nil {
		return "", false, err
	}
	exists := false
	for _, vlan := range vlans {
		same := vlan["interface"] == parent && vlan["vlan-id"] == strconv.Itoa(id)
		switch {
		case vlan["name"] == iface && same:
			exists = true
		case vlan["name"] == iface:
			return "", false, Refusal(fmt.Sprintf("interface %s already exists, as VLAN %s on %s", iface, vlan["vlan-id"], vlan["interface"]))
		case same:
			return "", false, Refusal(fmt.Sprintf("VLAN %d is already in use on %s, by interface %s", id, parent, vlan["name"]))
		}
	}
	return iface, exists, nil
}
