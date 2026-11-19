package routeros

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/netip"
	"net/url"
	"strings"
)

// PairVendo installs isolation before enabling the MAC+IP HotSpot bypass.
// Rules are owned by their stable Vendo ID, so interrupted operations are retryable.
func (c *Client) PairVendo(ctx context.Context, id uint32, mac, ip, portalURL string) error {
	portal, err := url.Parse(portalURL)
	if err != nil || portal.Hostname() == "" {
		return errors.New("invalid Controller URL")
	}
	port := portal.Port()
	if port == "" {
		port = "80"
		if portal.Scheme == "https" {
			port = "443"
		}
	}
	address, err := netip.ParseAddr(ip)
	if err != nil || !address.Is4() {
		return errors.New("Vendo needs an IPv4 address")
	}
	networks, err := c.list(ctx, "/ip/dhcp-server/network")
	if err != nil {
		return err
	}
	onLAN := false
	for _, network := range networks {
		prefix, err := netip.ParsePrefix(network["address"])
		if err == nil && prefix.Contains(address) && address != prefix.Masked().Addr() && ip != network["gateway"] {
			// The next address outside the prefix follows its broadcast address.
			onLAN = prefix.Contains(address.Next())
			if onLAN {
				break
			}
		}
	}
	if !onLAN {
		return errors.New("Vendo address must be a host address on a configured DHCP network")
	}
	comment := fmt.Sprintf("openpiso-vendo-%d", id)
	chain := fmt.Sprintf("op-vendo-%d", id)
	// The MAC match also confines a spoof using a different source IP. Input is
	// blocked except DHCP; forwarding allows only the fixed IP to Controller HTTP.
	for _, rule := range []struct {
		suffix string
		values map[string]string
	}{
		{"controller", map[string]string{"chain": chain, "action": "accept", "src-address": ip, "dst-address": portal.Hostname(), "protocol": "tcp", "dst-port": port}},
		{"drop", map[string]string{"chain": chain, "action": "drop"}},
		{"forward", map[string]string{"chain": "forward", "action": "jump", "jump-target": chain, "src-mac-address": mac}},
		{"input", map[string]string{"chain": "input", "action": "drop", "src-mac-address": mac}},
		{"dhcp", map[string]string{"chain": "input", "action": "accept", "src-mac-address": mac, "protocol": "udp", "dst-port": "67"}},
	} {
		name := comment + "-" + rule.suffix
		rule.values["disabled"] = "no"
		if err := c.ensureByComment(ctx, "/ip/firewall/filter", name, rule.values); err != nil {
			return fmt.Errorf("isolate Vendo: %w", err)
		}
		// Move the entry in the built-in chains ahead of existing accepts (and
		// FastTrack). Within the private chain, allow must precede its final drop.
		if rule.values["chain"] == "forward" || rule.values["chain"] == "input" {
			if err := c.moveVendoFilterFirst(ctx, "/ip/firewall/filter", name); err != nil {
				return err
			}
		}
	}

	// Vendo protocol uses IPv4; a copied MAC must not escape over IPv6.
	for _, chain := range []string{"input", "forward"} {
		name := comment + "-ipv6-" + chain
		if err := c.ensureByComment(ctx, "/ipv6/firewall/filter", name, map[string]string{
			"chain": chain, "action": "drop", "src-mac-address": mac, "disabled": "no",
		}); err != nil {
			return err
		}
		if err := c.moveVendoFilterFirst(ctx, "/ipv6/firewall/filter", name); err != nil {
			return err
		}
	}

	sourceIPs := map[string]bool{ip: true}
	leases, err := c.list(ctx, "/ip/dhcp-server/lease")
	if err != nil {
		return err
	}
	var lease map[string]string
	for _, entry := range leases {
		if entry["address"] != ip && !strings.EqualFold(entry["mac-address"], mac) {
			continue
		}
		if !strings.EqualFold(entry["mac-address"], mac) || (entry["dynamic"] != "true" && entry["comment"] != comment) {
			return errors.New("Vendo address or MAC has a conflicting DHCP lease")
		}
		if lease != nil {
			return errors.New("Vendo has multiple DHCP leases")
		}
		lease = entry
		sourceIPs[entry["address"]] = true
	}
	if lease != nil {
		if lease["dynamic"] == "true" {
			if err := c.request(ctx, http.MethodPost, "/ip/dhcp-server/lease/make-static", map[string]string{"numbers": lease[".id"]}, nil); err != nil {
				return err
			}
		}
		if err := c.request(ctx, http.MethodPatch, "/ip/dhcp-server/lease/"+lease[".id"], map[string]string{
			"mac-address": mac, "address": ip, "comment": comment, "disabled": "no",
		}, nil); err != nil {
			return err
		}
	} else {
		server, err := c.vendoDHCPServer(ctx, address)
		if err != nil {
			return err
		}
		if err := c.ensureByComment(ctx, "/ip/dhcp-server/lease", comment, map[string]string{
			"server": server, "mac-address": mac, "address": ip, "disabled": "no",
		}); err != nil {
			return err
		}
	}

	// Clear previous authenticated/FastTracked access before giving a bypass.
	active, err := c.list(ctx, "/ip/hotspot/active")
	if err != nil {
		return err
	}
	for _, entry := range active {
		if strings.EqualFold(entry["mac-address"], mac) {
			if err := c.request(ctx, http.MethodDelete, "/ip/hotspot/active/"+entry[".id"], nil, nil); err != nil {
				return err
			}
		}
	}
	hosts, err := c.list(ctx, "/ip/hotspot/host")
	if err != nil {
		return err
	}
	for _, host := range hosts {
		if strings.EqualFold(host["mac-address"], mac) {
			sourceIPs[host["address"]] = true
			sourceIPs[host["to-address"]] = true
		}
	}
	delete(sourceIPs, "")
	connections, err := c.list(ctx, "/ip/firewall/connection")
	if err != nil {
		return err
	}
	for _, entry := range connections {
		for _, field := range []string{"src-address", "dst-address", "reply-src-address", "reply-dst-address"} {
			address, _, _ := strings.Cut(entry[field], ":")
			if sourceIPs[address] {
				if err := c.request(ctx, http.MethodDelete, "/ip/firewall/connection/"+entry[".id"], nil, nil); err != nil {
					return err
				}
				break
			}
		}
	}
	if err := c.ensureByComment(ctx, "/ip/hotspot/ip-binding", comment, map[string]string{
		"mac-address": mac, "address": ip, "to-address": ip, "type": "bypassed", "disabled": "no",
	}); err != nil {
		return err
	}
	// Force already-present hosts to re-evaluate the new binding on traffic.
	hosts, err = c.list(ctx, "/ip/hotspot/host")
	if err != nil {
		return err
	}
	for _, host := range hosts {
		if strings.EqualFold(host["mac-address"], mac) {
			if err := c.request(ctx, http.MethodDelete, "/ip/hotspot/host/"+host[".id"], nil, nil); err != nil {
				return err
			}
		}
	}
	return nil
}

// vendoDHCPServer names the DHCP server for a Vendo's new static lease: the
// only one on the Router, or else the one on the interface whose Router
// address covers the Vendo, as with a DHCP server per HotSpot server.
func (c *Client) vendoDHCPServer(ctx context.Context, vendo netip.Addr) (string, error) {
	servers, err := c.list(ctx, "/ip/dhcp-server")
	if err != nil {
		return "", err
	}
	if len(servers) == 1 {
		return servers[0]["name"], nil
	}
	addresses, err := c.list(ctx, "/ip/address")
	if err != nil {
		return "", err
	}
	interfaces := make(map[string]bool)
	for _, entry := range addresses {
		prefix, err := netip.ParsePrefix(entry["address"])
		if err == nil && entry["disabled"] != "true" && prefix.Masked().Contains(vendo) {
			interfaces[entry["interface"]] = true
		}
	}
	var matches []string
	for _, server := range servers {
		if interfaces[server["interface"]] {
			matches = append(matches, server["name"])
		}
	}
	if len(matches) != 1 {
		return "", errors.New("Vendo needs an existing DHCP lease when no single DHCP server serves its network")
	}
	return matches[0], nil
}

func (c *Client) moveVendoFilterFirst(ctx context.Context, path, comment string) error {
	filters, err := c.list(ctx, path)
	if err != nil {
		return err
	}
	for _, filter := range filters {
		if filter["comment"] == comment && filters[0][".id"] != filter[".id"] {
			return c.request(ctx, http.MethodPost, path+"/move", map[string]string{
				"numbers": filter[".id"], "destination": filters[0][".id"],
			}, nil)
		}
	}
	return nil
}

func (c *Client) UnpairVendo(ctx context.Context, id uint32) error {
	comment := fmt.Sprintf("openpiso-vendo-%d", id)
	// Remove the bypass first, and keep isolation until the lease is removed.
	for _, path := range []string{"/ip/hotspot/ip-binding", "/ip/dhcp-server/lease", "/ip/firewall/filter", "/ipv6/firewall/filter"} {
		entries, err := c.list(ctx, path)
		if err != nil {
			return err
		}
		for _, entry := range entries {
			owned := entry["comment"] == comment
			if path == "/ip/firewall/filter" || path == "/ipv6/firewall/filter" {
				owned = strings.HasPrefix(entry["comment"], comment+"-")
			}
			if owned {
				if err := c.request(ctx, http.MethodDelete, path+"/"+entry[".id"], nil, nil); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

// VendoHostnames returns DHCP labels only; hostnames never authorize adoption.
func (c *Client) VendoHostnames(ctx context.Context) (map[string]string, error) {
	leases, err := c.list(ctx, "/ip/dhcp-server/lease")
	if err != nil {
		return nil, err
	}
	names := make(map[string]string)
	for _, lease := range leases {
		if lease["host-name"] != "" {
			names[strings.ToUpper(lease["mac-address"])] = lease["host-name"]
		}
	}
	return names, nil
}
