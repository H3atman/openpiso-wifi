package routeros

import (
	"context"
	"errors"
	"fmt"
	"html"
	"net"
	"net/url"
	"slices"
	"strings"
)

// Check is one requirement of the Router baseline. Verify only reads the
// Router; Fix, when set, is a safe change that makes the check met. Server is
// the HotSpot server the check is about, empty for a check on the whole Router.
type Check struct {
	Name   string
	Server string
	Verify func(context.Context) (Verdict, error)
	Fix    func(context.Context) error
}

// Verdict is a check's result. Reason says why a check is not met.
type Verdict struct {
	Met    bool
	Reason string
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

// repair makes each check met in order: a check that is not met runs its fix,
// and one without a fix fails with its reason.
func repair(ctx context.Context, checks []Check) error {
	for _, check := range checks {
		verdict, err := check.Verify(ctx)
		if err != nil {
			return err
		}
		if verdict.Met {
			continue
		}
		if check.Fix == nil {
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
			Name: "routeros-v7",
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
			Name: "base-profiles",
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
			Name: "walled-garden",
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
			Name:   "login-method",
			Server: name,
			Verify: func(ctx context.Context) (Verdict, error) {
				return c.verdict(ctx, profileQuery, "HotSpot server profile "+profile, map[string]string{"login-by": "http-pap"})
			},
			Fix: func(ctx context.Context) error {
				return ensureProfile(ctx, map[string]string{"login-by": "http-pap"})
			},
		},
		{
			Name:   "login-page",
			Server: name,
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
