package routeros

import (
	"context"
	"encoding/binary"
	"fmt"
	"net/http"
	"net/netip"
	"net/url"
	"slices"
	"strconv"
	"strings"
)

// Creating a HotSpot server (ADR 0011): Add alongside, on a free interface or a
// new VLAN, and later Fresh start, place a server, then CreateHotspotServer builds its network and
// the server itself. Every step adds only what is missing, so a step that fails
// midway is finished by running it again.

// Refusal is an error returned before any Router write, because the request
// itself cannot work; its message is the reason shown to the Operator.
type Refusal string

func (r Refusal) Error() string { return string(r) }

// Placement is where a new HotSpot server goes: Interface, an interface with
// no HotSpot server on it, or, when VLAN is set, a new VLAN with that ID on
// Parent (vlan.go).
type Placement struct {
	Interface string
	Parent    string
	VLAN      int
}

// privateRanges are the IPv4 private address blocks (RFC 1918).
var privateRanges = []netip.Prefix{
	netip.MustParsePrefix("10.0.0.0/8"),
	netip.MustParsePrefix("172.16.0.0/12"),
	netip.MustParsePrefix("192.168.0.0/16"),
}

// FreeInterfaces lists, sorted, the interfaces a new HotSpot server can go on:
// those with no HotSpot server, leaving out bridge ports, dynamic interfaces,
// the loopback and the interface the Controller reaches the Router through.
func (c *Client) FreeInterfaces(ctx context.Context) ([]string, error) {
	return c.freeInterfaces(ctx, func(HotspotServer) bool { return false })
}

// freeInterfaces is FreeInterfaces, but an interface whose HotSpot servers are
// all waived counts as free.
func (c *Client) freeInterfaces(ctx context.Context, waived func(HotspotServer) bool) ([]string, error) {
	interfaces, refusals, err := c.interfaceRefusals(ctx, waived)
	var free []string
	for _, name := range interfaces {
		if refusals[name] == "" {
			free = append(free, name)
		}
	}
	slices.Sort(free)
	return free, err
}

// interfaceRefusals lists the Router's interfaces and says, for each one a new
// HotSpot server cannot go on, why: a bridge port, dynamic, the loopback, the
// interface the Controller reaches the Router through, or one with a HotSpot
// server that waived does not accept.
func (c *Client) interfaceRefusals(ctx context.Context, waived func(HotspotServer) bool) ([]string, map[string]string, error) {
	rows, err := c.list(ctx, "/interface")
	if err != nil {
		return nil, nil, err
	}
	servers, err := c.ListHotspotServers(ctx)
	if err != nil {
		return nil, nil, err
	}
	addresses, err := c.list(ctx, "/ip/address")
	if err != nil {
		return nil, nil, err
	}
	management := ""
	for _, address := range addresses {
		if strings.HasPrefix(address["address"], c.baseURL.Hostname()+"/") {
			management = address["interface"]
		}
	}
	interfaces, refusals := make([]string, 0, len(rows)), map[string]string{}
	for _, row := range rows {
		name := row["name"]
		interfaces = append(interfaces, name)
		switch index := slices.IndexFunc(servers, func(server HotspotServer) bool { return server.Interface == name && !waived(server) }); {
		case row["slave"] == "true":
			refusals[name] = "is a bridge port"
		case row["dynamic"] == "true":
			refusals[name] = "is dynamic"
		case row["type"] == "loopback":
			refusals[name] = "is the loopback"
		case name == management:
			refusals[name] = "carries the Controller's connection to the Router"
		case index >= 0:
			refusals[name] = "already has a HotSpot server, " + servers[index].Name
		}
	}
	return interfaces, refusals, nil
}

// SuggestRange is the next free private range the size of the first HotSpot
// server's address range (a /24 when there is none), overlapping no address or
// route on the Router: in the private block holding that range first, then in
// the other blocks.
func (c *Client) SuggestRange(ctx context.Context) (netip.Prefix, error) {
	taken, err := c.takenRanges(ctx)
	if err != nil {
		return netip.Prefix{}, err
	}
	base := netip.MustParsePrefix("10.0.0.0/24")
	servers, err := c.ListHotspotServers(ctx)
	if err != nil {
		return netip.Prefix{}, err
	}
	if len(servers) > 0 {
		if prefix, ok, err := c.interfaceRange(ctx, servers[0].Interface); err != nil {
			return netip.Prefix{}, err
		} else if ok && prefix.Bits() <= 30 && private(prefix) {
			base = prefix
		}
	}
	// Walk each private block in steps of base's size: the block holding base
	// from base on, then the others from their start.
	first := slices.IndexFunc(privateRanges, func(block netip.Prefix) bool { return block.Contains(base.Addr()) })
	for i := range privateRanges {
		block := privateRanges[(first+i)%len(privateRanges)]
		if block.Bits() > base.Bits() {
			continue // a range bigger than the block
		}
		start, size := uint32of(block.Addr()), uint32(1)<<(32-base.Bits())
		count := uint32(1) << (base.Bits() - block.Bits())
		offset := uint32(0)
		if i == 0 {
			offset = (uint32of(base.Addr()) - start) / size
		}
		for step := uint32(0); step < count; step++ {
			candidate := netip.PrefixFrom(addrOf(start+(offset+step)%count*size), base.Bits())
			if !slices.ContainsFunc(taken, candidate.Overlaps) {
				return candidate, nil
			}
		}
	}
	return netip.Prefix{}, fmt.Errorf("no free /%d range is left in the private blocks 10.0.0.0/8, 172.16.0.0/12 and 192.168.0.0/16", base.Bits())
}

// CheckRange refuses network for a HotSpot server on iface before any write: it
// must be a private IPv4 range big enough for Devices that overlaps no address
// or route on the Router, other than iface's own address on that same range.
func (c *Client) CheckRange(ctx context.Context, iface string, network netip.Prefix) error {
	if !network.Addr().Is4() || network.Bits() > 30 {
		return Refusal(fmt.Sprintf("%s is not an IPv4 range of at least 4 addresses", network))
	}
	if network != network.Masked() {
		return Refusal(fmt.Sprintf("%s is not a range start; did you mean %s?", network, network.Masked()))
	}
	if !private(network) {
		return Refusal(fmt.Sprintf("%s is not a private range; use one inside 10.0.0.0/8, 172.16.0.0/12 or 192.168.0.0/16", network))
	}
	addresses, err := c.list(ctx, "/ip/address")
	if err != nil {
		return err
	}
	for _, row := range addresses {
		prefix, err := netip.ParsePrefix(row["address"])
		switch {
		case err != nil || (prefix.Masked() == network && row["interface"] == iface): // reused as it is
		case row["interface"] == iface:
			return Refusal(fmt.Sprintf("interface %s already has address %s; use its range %s", iface, row["address"], prefix.Masked()))
		case prefix.Overlaps(network):
			return Refusal(fmt.Sprintf("%s overlaps address %s on %s", network, row["address"], row["interface"]))
		}
	}
	routes, err := c.list(ctx, "/ip/route")
	if err != nil {
		return err
	}
	for _, row := range routes {
		if prefix, err := netip.ParsePrefix(row["dst-address"]); err == nil && prefix.Bits() > 0 && prefix.Overlaps(network) &&
			!(prefix == network && row["gateway"] == iface) {
			return Refusal(fmt.Sprintf("%s overlaps route %s via %s", network, row["dst-address"], row["gateway"]))
		}
	}
	return nil
}

// CreateHotspotServer creates a HotSpot server at placement on network and
// returns its name, openpiso-<interface>: the VLAN interface for a new VLAN,
// the interface's address, pool, DHCP server and DHCP network where missing, a
// server profile that logs in by http-pap from the Controller's login page
// directory, and the server. Adopting the server writes its login page.
//
// It refuses before any write what checkPlacement refuses. Run again with the
// same placement after any failure, including a lost response once the server
// was made, it finishes what is missing and returns the same name.
//
// Ownership: the Controller builds only on objects that are its own leftovers
// from an earlier create of this server. Where RouterOS gives the object a
// comment (VLAN, address, address pool, DHCP server, DHCP network), its own
// carry the server's name as their comment. Server profiles and HotSpot servers
// have no comment field, so the server profile of that name is its own when no
// HotSpot server but the one being created uses it, and the HotSpot server of
// that name is its own when it is on that interface with that server profile.
func (c *Client) CreateHotspotServer(ctx context.Context, placement Placement, network netip.Prefix) (string, error) {
	iface, name, err := c.checkPlacement(ctx, placement, network, false)
	if err != nil {
		return "", err
	}
	if placement.VLAN != 0 {
		if err := c.ensureNamed(ctx, "/interface/vlan", iface, map[string]string{
			"interface": placement.Parent, "vlan-id": strconv.Itoa(placement.VLAN), "comment": name,
		}); err != nil {
			return "", fmt.Errorf("create VLAN %d on %s: %w", placement.VLAN, placement.Parent, err)
		}
	}
	gateway, pool, err := c.ensureNetwork(ctx, iface, name, network)
	if err != nil {
		return "", err
	}
	if err := c.ensureNamed(ctx, "/ip/hotspot/profile", name, map[string]string{
		"hotspot-address": gateway.String(), "login-by": "http-pap", "html-directory": LoginDirectory,
	}); err != nil {
		return "", fmt.Errorf("create HotSpot server profile %s: %w", name, err)
	}
	if err := c.ensureNamed(ctx, "/ip/hotspot", name, map[string]string{
		"interface": iface, "profile": name, "address-pool": pool, "disabled": "no",
	}); err != nil {
		return "", fmt.Errorf("create HotSpot server %s: %w", name, err)
	}
	return name, nil
}

// checkPlacement refuses, before any write, a create at placement on network
// and returns the server's interface and name: a VLAN checkVLAN refuses, an
// interface a new server cannot go on, a range CheckRange refuses, or a name
// the create would use held by an object that is not the Controller's own (see
// CreateHotspotServer). wiped is Fresh start's check before its wipe, which
// removes every HotSpot server and server profile, so those are waived.
func (c *Client) checkPlacement(ctx context.Context, placement Placement, network netip.Prefix, wiped bool) (string, string, error) {
	iface, exists := placement.Interface, true
	if placement.VLAN != 0 {
		var err error
		if iface, exists, err = c.checkVLAN(ctx, placement.Parent, placement.VLAN); err != nil {
			return "", "", err
		}
	}
	name := "openpiso-" + iface
	own := func(server HotspotServer) bool {
		return wiped || (server.Name == name && server.Interface == iface && server.Profile == name)
	}
	if exists {
		interfaces, refusals, err := c.interfaceRefusals(ctx, own)
		if err != nil {
			return "", "", err
		}
		if !slices.Contains(interfaces, iface) {
			return "", "", Refusal(fmt.Sprintf("interface %s is missing", iface))
		}
		if reason := refusals[iface]; reason != "" {
			return "", "", Refusal(fmt.Sprintf("interface %s %s", iface, reason))
		}
	}
	if err := c.CheckRange(ctx, iface, network); err != nil {
		return "", "", err
	}
	foreign := func(what, detail string) error {
		return Refusal(fmt.Sprintf("%s %s is already on the Router and is not the Controller's%s; rename it there, or place the new server elsewhere", what, name, detail))
	}
	for _, path := range []struct{ path, what string }{{"/ip/pool", "address pool"}, {"/ip/dhcp-server", "DHCP server"}} {
		rows, err := c.list(ctx, path.path+"?name="+url.QueryEscape(name))
		if err != nil {
			return "", "", err
		}
		if len(rows) > 0 && rows[0]["comment"] != name {
			return "", "", foreign(path.what, "")
		}
	}
	if wiped {
		return iface, name, nil
	}
	servers, err := c.ListHotspotServers(ctx)
	if err != nil {
		return "", "", err
	}
	for _, server := range servers {
		switch {
		case server.Name == name && !own(server):
			return "", "", foreign("HotSpot server", fmt.Sprintf(" (it is on %s with server profile %s)", server.Interface, server.Profile))
		case server.Profile == name && server.Name != name:
			return "", "", foreign("HotSpot server profile", ", it serves HotSpot server "+server.Name)
		}
	}
	return iface, name, nil
}

// ensureNetwork gives iface what Devices need to join it, adding only what is
// missing and never changing what is there: an address on network (its first
// host), a DHCP server named name with a pool of the other hosts, and a DHCP
// network, each with comment name. It returns the interface's gateway address
// and the DHCP server's pool.
func (c *Client) ensureNetwork(ctx context.Context, iface, name string, network netip.Prefix) (netip.Addr, string, error) {
	gateway := network.Addr().Next()
	addresses, err := c.list(ctx, "/ip/address?interface="+url.QueryEscape(iface))
	if err != nil {
		return netip.Addr{}, "", err
	}
	if len(addresses) > 0 {
		prefix, err := netip.ParsePrefix(addresses[0]["address"])
		if err != nil {
			return netip.Addr{}, "", fmt.Errorf("read address of %s: %w", iface, err)
		}
		gateway, network = prefix.Addr(), prefix.Masked()
	} else if err := c.request(ctx, http.MethodPut, "/ip/address", map[string]string{
		"address": netip.PrefixFrom(gateway, network.Bits()).String(), "interface": iface, "comment": name,
	}, nil); err != nil {
		return netip.Addr{}, "", fmt.Errorf("add address on %s: %w", iface, err)
	}

	servers, err := c.list(ctx, "/ip/dhcp-server?interface="+url.QueryEscape(iface))
	if err != nil {
		return netip.Addr{}, "", err
	}
	pool := name
	if len(servers) > 0 && servers[0]["address-pool"] != "" && servers[0]["address-pool"] != "static-only" {
		pool = servers[0]["address-pool"]
	}
	pools, err := c.list(ctx, "/ip/pool?name="+url.QueryEscape(pool))
	if err != nil {
		return netip.Addr{}, "", err
	}
	if len(pools) == 0 {
		if err := c.request(ctx, http.MethodPut, "/ip/pool", map[string]string{"name": pool, "ranges": poolRanges(network, gateway), "comment": name}, nil); err != nil {
			return netip.Addr{}, "", fmt.Errorf("add address pool %s: %w", pool, err)
		}
	}
	if len(servers) == 0 {
		if err := c.request(ctx, http.MethodPut, "/ip/dhcp-server", map[string]string{
			"name": name, "interface": iface, "address-pool": pool, "disabled": "no", "comment": name,
		}, nil); err != nil {
			return netip.Addr{}, "", fmt.Errorf("add DHCP server on %s: %w", iface, err)
		}
	}

	if found, err := c.dhcpNetwork(ctx, network); err != nil {
		return netip.Addr{}, "", err
	} else if !found {
		if err := c.request(ctx, http.MethodPut, "/ip/dhcp-server/network", map[string]string{
			"address": network.String(), "gateway": gateway.String(), "dns-server": gateway.String(), "comment": name,
		}, nil); err != nil {
			return netip.Addr{}, "", fmt.Errorf("add DHCP network %s: %w", network, err)
		}
	}
	return gateway, pool, nil
}

// serverNetwork checks that server's interface has an address, a DHCP server
// with its pool, and a DHCP network for the address. Its fix creates the
// missing pieces on the range the server already uses, or a suggested one.
func (c *Client) serverNetwork(server HotspotServer) Check {
	iface := server.Interface
	return Check{
		Name:     "server-network",
		Server:   server.Name,
		Title:    "The interface has an address, a DHCP server and a pool",
		FixLabel: "Create what is missing",
		Verify: func(ctx context.Context) (Verdict, error) {
			network, ok, err := c.interfaceRange(ctx, iface)
			if err != nil {
				return Verdict{}, err
			}
			if !ok {
				return Verdict{Reason: "Interface " + iface + " has no address"}, nil
			}
			servers, err := c.list(ctx, "/ip/dhcp-server?interface="+url.QueryEscape(iface))
			if err != nil {
				return Verdict{}, err
			}
			if len(servers) == 0 {
				return Verdict{Reason: "Interface " + iface + " has no DHCP server"}, nil
			}
			if servers[0]["disabled"] == "true" {
				return Verdict{Reason: "DHCP server " + servers[0]["name"] + " is disabled",
					Manual: "On the Router, enable DHCP server " + servers[0]["name"] + "."}, nil
			}
			pool := servers[0]["address-pool"]
			if pool == "" || pool == "static-only" {
				return Verdict{Reason: "DHCP server " + servers[0]["name"] + " gives only static leases",
					Manual: "On the Router, give DHCP server " + servers[0]["name"] + " an address pool on " + network.String() + "."}, nil
			}
			pools, err := c.list(ctx, "/ip/pool?name="+url.QueryEscape(pool))
			if err != nil {
				return Verdict{}, err
			}
			if len(pools) == 0 {
				return Verdict{Reason: "DHCP server " + servers[0]["name"] + " has no address pool"}, nil
			}
			found, err := c.dhcpNetwork(ctx, network)
			if err != nil {
				return Verdict{}, err
			}
			if !found {
				return Verdict{Reason: "No DHCP network gives Devices on " + network.String() + " a gateway"}, nil
			}
			return met, nil
		},
		Fix: func(ctx context.Context) error {
			network, err := c.serverRange(ctx, server)
			if err != nil {
				return err
			}
			if err := c.CheckRange(ctx, iface, network); err != nil {
				return err
			}
			_, _, err = c.ensureNetwork(ctx, iface, "openpiso-"+iface, network)
			return err
		},
	}
}

// serverRange is the range server uses: its interface's address range, else
// the DHCP network holding its server profile's hotspot-address, else a
// suggested range.
func (c *Client) serverRange(ctx context.Context, server HotspotServer) (netip.Prefix, error) {
	if network, ok, err := c.interfaceRange(ctx, server.Interface); err != nil || ok {
		return network, err
	}
	profiles, err := c.list(ctx, "/ip/hotspot/profile?name="+url.QueryEscape(server.Profile))
	if err != nil {
		return netip.Prefix{}, err
	}
	if len(profiles) > 0 {
		if address, err := netip.ParseAddr(profiles[0]["hotspot-address"]); err == nil {
			networks, err := c.list(ctx, "/ip/dhcp-server/network")
			if err != nil {
				return netip.Prefix{}, err
			}
			for _, row := range networks {
				if prefix, err := netip.ParsePrefix(row["address"]); err == nil && prefix.Contains(address) {
					return prefix.Masked(), nil
				}
			}
		}
	}
	return c.SuggestRange(ctx)
}

// interfaceRange is the range of iface's first address, and whether it has one.
func (c *Client) interfaceRange(ctx context.Context, iface string) (netip.Prefix, bool, error) {
	addresses, err := c.list(ctx, "/ip/address?interface="+url.QueryEscape(iface))
	if err != nil || len(addresses) == 0 {
		return netip.Prefix{}, false, err
	}
	prefix, err := netip.ParsePrefix(addresses[0]["address"])
	if err != nil {
		return netip.Prefix{}, false, fmt.Errorf("read address of %s: %w", iface, err)
	}
	return prefix.Masked(), true, nil
}

// dhcpNetwork reports whether a DHCP network covers network.
func (c *Client) dhcpNetwork(ctx context.Context, network netip.Prefix) (bool, error) {
	rows, err := c.list(ctx, "/ip/dhcp-server/network")
	if err != nil {
		return false, err
	}
	return slices.ContainsFunc(rows, func(row map[string]string) bool {
		prefix, err := netip.ParsePrefix(row["address"])
		return err == nil && prefix.Bits() <= network.Bits() && prefix.Contains(network.Addr())
	}), nil
}

// takenRanges are the ranges of every address and route on the Router, but the
// default route.
func (c *Client) takenRanges(ctx context.Context) ([]netip.Prefix, error) {
	var taken []netip.Prefix
	for _, source := range []struct{ path, field string }{{"/ip/address", "address"}, {"/ip/route", "dst-address"}} {
		rows, err := c.list(ctx, source.path)
		if err != nil {
			return nil, err
		}
		for _, row := range rows {
			if prefix, err := netip.ParsePrefix(row[source.field]); err == nil && prefix.Bits() > 0 {
				taken = append(taken, prefix.Masked())
			}
		}
	}
	return taken, nil
}

func uint32of(address netip.Addr) uint32 { return binary.BigEndian.Uint32(address.AsSlice()) }

func addrOf(value uint32) netip.Addr {
	return netip.AddrFrom4([4]byte(binary.BigEndian.AppendUint32(nil, value)))
}

// private reports whether network lies wholly inside one private block.
func private(network netip.Prefix) bool {
	return slices.ContainsFunc(privateRanges, func(block netip.Prefix) bool {
		return block.Bits() <= network.Bits() && block.Contains(network.Addr())
	})
}

// poolRanges are network's hosts but gateway, in RouterOS pool syntax.
func poolRanges(network netip.Prefix, gateway netip.Addr) string {
	first := network.Masked().Addr().Next()
	last := addrOf(uint32of(network.Masked().Addr()) + uint32(1)<<(32-network.Bits()) - 2)
	var ranges []string
	if gateway.Compare(first) > 0 {
		ranges = append(ranges, first.String()+"-"+gateway.Prev().String())
	}
	if gateway.Compare(last) < 0 {
		ranges = append(ranges, gateway.Next().String()+"-"+last.String())
	}
	return strings.Join(ranges, ",")
}
