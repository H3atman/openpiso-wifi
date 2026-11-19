package routeros

import (
	"context"
	"errors"
	"fmt"
	"html"
	"net"
	"net/http"
	"net/url"
	"slices"
	"strings"
)

// Check is one requirement of the Router baseline and one item of the Router
// setup checklist. Verify only reads the Router; Fix, when set, is a safe change
// that makes the check met, and FixLabel names it on its button. Server is the
// HotSpot server the check is about, empty for a check on the whole Router.
// Title is the requirement as the Operator reads it.
type Check struct {
	Name     string
	Server   string
	Title    string
	FixLabel string
	Verify   func(context.Context) (Verdict, error)
	Fix      func(context.Context) error
}

// Verdict is a check's result. Reason says why a check is not met. Manual, when
// set, is the steps the Operator takes by hand, because the Fix does not apply.
type Verdict struct {
	Met    bool
	Reason string
	Manual string
}

var met = Verdict{Met: true}

// EnsureBaseline repairs every check on the Router and on each Adopted HotSpot
// server in adopted. It runs when servers are Adopted and on every Controller
// boot. With nothing Adopted it only verifies the Router and changes nothing.
func (c *Client) EnsureBaseline(ctx context.Context, portalURL string, idleSeconds int, adopted []string) error {
	checks, err := c.routerChecks(portalURL, idleSeconds)
	if err != nil {
		return err
	}
	if len(adopted) == 0 {
		checks = checks[:1] // routeros-v7, which has no fix
	}
	if err := repair(ctx, checks); err != nil {
		return err
	}
	if len(adopted) == 0 {
		return nil
	}
	servers, err := c.ListHotspotServers(ctx)
	if err != nil {
		return fmt.Errorf("list HotSpot servers: %w", err)
	}
	for _, name := range adopted {
		index := slices.IndexFunc(servers, func(server HotspotServer) bool { return server.Name == name })
		if index < 0 {
			return fmt.Errorf("Adopted HotSpot server %s is missing on the Router", name)
		}
		if err := repair(ctx, c.serverChecks(name, servers[index].Profile, portalURL)); err != nil {
			return err
		}
	}
	return nil
}

// Checklist is every check Router setup shows, in order: the Router's, then
// each server's in adopted. Showing it only runs Verify; a Fix runs only when
// the Operator asks. Unlike EnsureBaseline, it includes checks boot never
// repairs, such as whether a server is enabled.
func (c *Client) Checklist(ctx context.Context, portalURL string, idleSeconds int, adopted []string) ([]Check, error) {
	checks, err := c.routerChecks(portalURL, idleSeconds)
	if err != nil {
		return nil, err
	}
	servers, err := c.ListHotspotServers(ctx)
	if err != nil {
		return nil, fmt.Errorf("list HotSpot servers: %w", err)
	}
	portal, _ := url.Parse(portalURL) // routerChecks has validated it
	for _, name := range adopted {
		checks = append(checks, c.serverEnabled(name))
		if index := slices.IndexFunc(servers, func(server HotspotServer) bool { return server.Name == name }); index >= 0 {
			checks = append(checks, c.serverChecks(name, servers[index].Profile, portalURL)...)
			checks = append(checks, c.portalReachable(servers[index], portal.Hostname()))
		}
	}
	return checks, nil
}

// repair makes each check met in order: a check that is not met runs its fix,
// and one without a fix that applies fails with its reason.
func repair(ctx context.Context, checks []Check) error {
	for _, check := range checks {
		verdict, err := check.Verify(ctx)
		if err != nil {
			return err
		}
		if verdict.Met {
			continue
		}
		if check.Fix == nil || verdict.Manual != "" {
			return errors.New(verdict.Reason)
		}
		if err := check.Fix(ctx); err != nil {
			return err
		}
	}
	return nil
}

// routerChecks are the checks on the whole Router. portalURL must be an HTTP
// or HTTPS origin with an IP address.
func (c *Client) routerChecks(portalURL string, idleSeconds int) ([]Check, error) {
	portal, err := url.Parse(portalURL)
	if err != nil || portal.Hostname() == "" || (portal.Scheme != "http" && portal.Scheme != "https") {
		return nil, errors.New("portal URL must be an HTTP or HTTPS origin")
	}
	port := portal.Port()
	if port == "" {
		if portal.Scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	}
	if net.ParseIP(portal.Hostname()) == nil {
		return nil, errors.New("the walking skeleton requires an IP address in the portal URL")
	}
	walledGarden := func() map[string]string {
		return map[string]string{"action": "accept", "dst-address": portal.Hostname(), "dst-port": port, "protocol": "tcp"}
	}

	return []Check{
		{
			Name:  "routeros-v7",
			Title: "The Router runs RouterOS v7 and the Controller's own user works",
			Verify: func(ctx context.Context) (Verdict, error) {
				version, err := c.Version(ctx)
				if err != nil {
					return Verdict{}, err
				}
				if !strings.HasPrefix(version, "7.") {
					return Verdict{Reason: fmt.Sprintf("RouterOS v7 is required; Router reports %q", version)}, nil
				}
				return met, nil
			},
		},
		{
			Name:     "base-profiles",
			Title:    "The Controller's two base HotSpot profiles exist with the right settings",
			FixLabel: "Create or correct the profiles",
			Verify: func(ctx context.Context) (Verdict, error) {
				for _, idle := range []bool{false, true} {
					name := ProfileName("", idle)
					verdict, err := c.verdict(ctx, "/ip/hotspot/user/profile?name="+url.QueryEscape(name), "HotSpot user profile "+name, profileValues(nil, idleSeconds, idle))
					if err != nil || !verdict.Met {
						return verdict, err
					}
				}
				return met, nil
			},
			Fix: func(ctx context.Context) error {
				return c.ensureProfilePair(ctx, "", nil, idleSeconds)
			},
		},
		{
			Name:     "walled-garden",
			Title:    "The walled garden lets unpaid Devices reach the Portal",
			FixLabel: "Add the Controller's entry",
			Verify: func(ctx context.Context) (Verdict, error) {
				return c.verdict(ctx, "/ip/hotspot/walled-garden/ip?comment=openpiso-controller", "Controller walled garden entry", walledGarden())
			},
			Fix: func(ctx context.Context) error {
				if err := c.ensureByComment(ctx, "/ip/hotspot/walled-garden/ip", "openpiso-controller", walledGarden()); err != nil {
					return fmt.Errorf("ensure Controller walled garden: %w", err)
				}
				return nil
			},
		},
	}, nil
}

// LoginDirectory is the login page directory the Controller owns. Every Adopted
// server's server profile points at it, so the Router's default login page
// directory, hotspot, is never written.
const LoginDirectory = "openpiso"

// serverChecks are the checks on HotSpot server name, whose server profile is
// profile. Servers that share a server profile share what these checks fix.
func (c *Client) serverChecks(name, profile, portalURL string) []Check {
	profileQuery := "/ip/hotspot/profile?name=" + url.QueryEscape(profile)
	ensureProfile := func(ctx context.Context, values map[string]string) error {
		if err := c.ensureNamed(ctx, "/ip/hotspot/profile", profile, values); err != nil {
			return fmt.Errorf("ensure HotSpot server profile %s: %w", profile, err)
		}
		return nil
	}
	const directory = LoginDirectory
	loginFile := directory + "/login.html"
	loginHTML := loginRedirectHTML(portalURL)

	return []Check{
		{
			Name:     "login-method",
			Server:   name,
			Title:    "The login method is http-pap",
			FixLabel: "Set http-pap",
			Verify: func(ctx context.Context) (Verdict, error) {
				profiles, err := c.list(ctx, profileQuery)
				switch {
				case err != nil:
					return Verdict{}, err
				case len(profiles) == 0:
					return Verdict{Reason: "HotSpot server profile " + profile + " is missing"}, nil
				case profiles[0]["login-by"] != "http-pap":
					return Verdict{Reason: fmt.Sprintf("HotSpot server profile %s logs in by %s, not http-pap only", profile, profiles[0]["login-by"])}, nil
				}
				return met, nil
			},
			Fix: func(ctx context.Context) error {
				return ensureProfile(ctx, map[string]string{"login-by": "http-pap"})
			},
		},
		{
			Name:     "login-page",
			Server:   name,
			Title:    "The login page is the Portal redirect",
			FixLabel: "Write the login page",
			Verify: func(ctx context.Context) (Verdict, error) {
				verdict, err := c.verdict(ctx, profileQuery, "HotSpot server profile "+profile, map[string]string{"html-directory": directory})
				if err != nil || !verdict.Met {
					return verdict, err
				}
				return c.verdict(ctx, "/file?name="+url.QueryEscape(loginFile), "HotSpot login page "+loginFile, map[string]string{"contents": loginHTML})
			},
			Fix: func(ctx context.Context) error {
				if err := ensureProfile(ctx, map[string]string{"html-directory": directory}); err != nil {
					return err
				}
				if err := c.ensureFile(ctx, loginFile, loginHTML); err != nil {
					return fmt.Errorf("ensure HotSpot login redirect: %w", err)
				}
				return nil
			},
		},
	}
}

// serverEnabled checks that HotSpot server name is enabled on an interface that
// is up. Its fix enables the server; an interface that is down is manual, since
// the interface is the Operator's own configuration.
func (c *Client) serverEnabled(name string) Check {
	query := "/ip/hotspot?name=" + url.QueryEscape(name)
	return Check{
		Name:     "server-enabled",
		Server:   name,
		Title:    "The server is enabled on an interface that is up",
		FixLabel: "Enable the server",
		Verify: func(ctx context.Context) (Verdict, error) {
			servers, err := c.list(ctx, query)
			if err != nil {
				return Verdict{}, err
			}
			if len(servers) == 0 {
				return Verdict{Reason: "HotSpot server " + name + " is missing on the Router",
					Manual: "Create HotSpot server " + name + " on the Router again, or restore the Router backup."}, nil
			}
			if servers[0]["disabled"] == "true" {
				return Verdict{Reason: "HotSpot server " + name + " is disabled"}, nil
			}
			iface := servers[0]["interface"]
			interfaces, err := c.list(ctx, "/interface?name="+url.QueryEscape(iface))
			if err != nil {
				return Verdict{}, err
			}
			if len(interfaces) == 0 || interfaces[0]["running"] != "true" {
				return Verdict{Reason: "Interface " + iface + " is down",
					Manual: "On the Router, enable interface " + iface + " and check the cable or access point connected to it."}, nil
			}
			return met, nil
		},
		Fix: func(ctx context.Context) error {
			servers, err := c.list(ctx, query)
			if err != nil {
				return err
			}
			if len(servers) == 0 {
				return fmt.Errorf("HotSpot server %s is missing on the Router", name)
			}
			if err := c.updateChanged(ctx, "/ip/hotspot", servers[0], map[string]string{"disabled": "no"}); err != nil {
				return fmt.Errorf("enable HotSpot server %s: %w", name, err)
			}
			return nil
		},
	}
}

// portalReachable checks that Devices on server reach the Portal at host with
// their own address, which the Portal needs to find them in the HotSpot host
// table. Router-originated packets pass source NAT too, so a ping alone cannot
// tell: the Controller's NAT exemption for the server's network must come before
// every other source NAT rule, and then a ping from the server's own address
// proves the Controller has a route back. The fix adds or moves the exemption
// and never edits the Operator's rules; a missing route back is manual. Not
// repaired at boot, since it can be manual.
func (c *Client) portalReachable(server HotspotServer, host string) Check {
	name, comment := server.Name, "openpiso-controller-"+server.Name
	// serverNetwork is the server interface's address and its network, or a nil network when it has none.
	serverNetwork := func(ctx context.Context) (string, *net.IPNet, error) {
		addresses, err := c.list(ctx, "/ip/address?interface="+url.QueryEscape(server.Interface))
		if err != nil {
			return "", nil, err
		}
		for _, address := range addresses {
			if address["disabled"] != "true" {
				ip, network, err := net.ParseCIDR(address["address"])
				if err != nil {
					return "", nil, fmt.Errorf("read the address of interface %s: %w", server.Interface, err)
				}
				return ip.String(), network, nil
			}
		}
		return "", nil, nil
	}
	exemption := func(network *net.IPNet) map[string]string {
		return map[string]string{"chain": "srcnat", "action": "accept", "src-address": network.String(), "dst-address": host, "disabled": "no"}
	}
	// exemptionAt is the exemption's index in the source NAT rules, -1 when
	// missing, and the index of the first enabled rule of the Operator's above it, or -1.
	exemptionAt := func(rules []map[string]string) (int, int) {
		index := slices.IndexFunc(rules, func(rule map[string]string) bool { return rule["comment"] == comment })
		if index < 0 {
			return -1, -1
		}
		return index, slices.IndexFunc(rules[:index], func(rule map[string]string) bool {
			return rule["dynamic"] != "true" && rule["disabled"] != "true" && !strings.HasPrefix(rule["comment"], "openpiso-controller")
		})
	}
	const rules = "/ip/firewall/nat?chain=srcnat"
	return Check{
		Name:     "portal-reachable",
		Server:   name,
		Title:    "Devices on this server can reach the Portal without source NAT",
		FixLabel: "Add the Controller's NAT exemption",
		Verify: func(ctx context.Context) (Verdict, error) {
			ip, network, err := serverNetwork(ctx)
			if err != nil {
				return Verdict{}, err
			}
			if network == nil {
				return Verdict{Reason: "Interface " + server.Interface + " has no IP address",
					Manual: "Give interface " + server.Interface + " an IP address on the Router, then check again."}, nil
			}
			current, err := c.list(ctx, rules)
			if err != nil {
				return Verdict{}, err
			}
			switch index, above := exemptionAt(current); {
			case index < 0:
				return Verdict{Reason: "Nothing keeps a source NAT rule from rewriting the address of Devices on " + name + " on the way to the Portal"}, nil
			case len(changedFields(current[index], exemption(network))) > 0:
				return Verdict{Reason: "The Controller's NAT exemption for " + name + " has been changed"}, nil
			case above >= 0:
				return Verdict{Reason: fmt.Sprintf("A source NAT %s rule comes before the Controller's NAT exemption for %s and can rewrite Devices' addresses", current[above]["action"], name)}, nil
			}
			var replies []map[string]string
			if err := c.request(ctx, http.MethodPost, "/ping", map[string]string{"address": host, "src-address": ip, "count": "2"}, &replies); err != nil {
				return Verdict{}, err
			}
			if !slices.ContainsFunc(replies, func(reply map[string]string) bool { return reply["status"] == "" && reply["time"] != "" }) {
				return Verdict{Reason: fmt.Sprintf("The Controller at %s does not answer the Router from %s's address %s", host, name, ip),
					Manual: fmt.Sprintf("Give the Controller a route back to %s through the Router, on the Controller itself or on the router it uses as its gateway, then check again.", network)}, nil
			}
			return met, nil
		},
		Fix: func(ctx context.Context) error {
			_, network, err := serverNetwork(ctx)
			if err != nil {
				return err
			}
			if network == nil {
				return fmt.Errorf("interface %s has no IP address", server.Interface)
			}
			if err := c.ensureByComment(ctx, "/ip/firewall/nat", comment, exemption(network)); err != nil {
				return fmt.Errorf("ensure the NAT exemption for %s: %w", name, err)
			}
			current, err := c.list(ctx, rules)
			if err != nil {
				return err
			}
			index, above := exemptionAt(current)
			if index < 0 {
				return fmt.Errorf("the NAT exemption for %s is missing after adding it", name)
			}
			if above < 0 {
				return nil
			}
			// Moving the Controller's own rule leaves the Operator's rules and their order as they are.
			if err := c.request(ctx, http.MethodPost, "/ip/firewall/nat/move", map[string]string{
				"numbers": current[index][".id"], "destination": current[above][".id"],
			}, nil); err != nil {
				return fmt.Errorf("move the NAT exemption for %s above the Operator's rules: %w", name, err)
			}
			return nil
		},
	}
}

// verdict reports whether the first row at query, named what in the reason,
// exists and has values.
func (c *Client) verdict(ctx context.Context, query, what string, values map[string]string) (Verdict, error) {
	items, err := c.list(ctx, query)
	if err != nil {
		return Verdict{}, err
	}
	if len(items) == 0 {
		return Verdict{Reason: what + " is missing"}, nil
	}
	changes := changedFields(items[0], values)
	if len(changes) > 0 {
		fields := make([]string, 0, len(changes))
		for field := range changes {
			fields = append(fields, field)
		}
		slices.Sort(fields)
		return Verdict{Reason: what + " has the wrong " + strings.Join(fields, ", ")}, nil
	}
	return met, nil
}

// loginRedirectHTML is the HotSpot login page that sends a Device to the
// Portal, or offers a retry while the Controller is unreachable.
func loginRedirectHTML(portalURL string) string {
	portalOrigin := html.EscapeString(strings.TrimRight(portalURL, "/"))
	return fmt.Sprintf(`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OpenPiso WiFi</title></head>
<body><main><h1>OpenPiso WiFi</h1><p>Service temporarily unavailable. Please try again shortly.</p><p><a href="$(link-login-only)">Try again</a></p>
<noscript><p>Enable JavaScript to continue when the Controller is available.</p></noscript></main>
<script src="%s/portal/ready.js" data-portal="%s/portal?mac=$(mac-esc)&amp;ip=$(ip)" onload="location.replace(this.dataset.portal)"></script></body></html>`, portalOrigin, portalOrigin)
}
