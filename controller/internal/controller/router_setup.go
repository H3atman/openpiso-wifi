package controller

import (
	"context"
	"net/http"
	"slices"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

// Router setup: connect a Router, then Use existing Adopts the HotSpot servers
// the Operator ticks (ADR 0011). Only Adopted servers are ever changed. The
// checklist reads each requirement on the Router and fixes one on request.

// routerSetupTimeout bounds the Router reads behind the Router setup page.
const routerSetupTimeout = 10 * time.Second

// routerSetupView is the Router setup page. Available are the Router's HotSpot
// servers not yet Adopted; Error says why the Router could not be read.
type routerSetupView struct {
	Connected bool
	Address   string
	Version   string
	Adopted   []string
	Available []routeros.HotspotServer
	Servers   int // HotSpot servers on the Router
	Checklist []checkGroup
	Error     string

	AddAlongside addAlongsideView
	CarryVLANs   []carryVLAN
	FreshStart   freshStartView
}

// useExistingView is the review before Adopting Servers, with the ticked
// servers' checklist as it reads before anything is fixed.
type useExistingView struct {
	Servers   []routeros.HotspotServer
	Directory string
	Checklist []checkGroup
}

// checkGroup is the checklist of the Router (Server empty) or of one HotSpot server.
type checkGroup struct {
	Server string
	Items  []checkItem
}

// checkItem is one checklist item as read. Fixable says the page offers its
// one-click fix (POST /admin/router/fix with check and server).
type checkItem struct {
	routeros.Verdict
	Name, Server, Title, FixLabel string
	Fixable                       bool
}

// admin serves GET /admin, the Router setup page.
func (s *Server) admin(response http.ResponseWriter, request *http.Request) {
	var view routerSetupView
	config, configured, err := s.ledger.RouterConfig()
	if err != nil {
		http.Error(response, "read Router configuration", http.StatusInternalServerError)
		return
	}
	if configured {
		view.Connected, view.Address = true, config.Address
		view.Adopted, err = s.ledger.AdoptedHotspotServers()
		if err != nil {
			http.Error(response, err.Error(), http.StatusInternalServerError)
			return
		}
		if err := s.readRouterSetup(request.Context(), &view); err != nil {
			view.Error = err.Error()
		}
	}
	s.renderPage(response, request, "admin", Page{Title: "Router setup", Section: "Settings", Active: "/admin", Data: view})
}

// readRouterSetup reads the Router's version, its HotSpot servers not yet
// Adopted and the checklist of the Router and each Adopted server. It writes nothing.
func (s *Server) readRouterSetup(ctx context.Context, view *routerSetupView) error {
	router, err := s.router()
	if err != nil {
		return err
	}
	defer router.Close()
	ctx, cancel := context.WithTimeout(ctx, routerSetupTimeout)
	defer cancel()
	if view.Version, err = router.Version(ctx); err != nil {
		return err
	}
	servers, err := router.ListHotspotServers(ctx)
	if err != nil {
		return err
	}
	view.Servers = len(servers)
	view.Available = slices.DeleteFunc(slices.Clone(servers), func(server routeros.HotspotServer) bool {
		return slices.Contains(view.Adopted, server.Name)
	})
	if view.AddAlongside, err = readAddAlongside(ctx, router); err != nil {
		return err
	}
	if view.CarryVLANs, err = s.readCarryVLANs(ctx, router, servers, view.Adopted); err != nil {
		return err
	}
	if view.FreshStart, err = s.readFreshStart(ctx, router); err != nil {
		return err
	}
	view.Checklist, err = s.readChecklist(ctx, router, view.Adopted, true)
	return err
}

// checklist is every Router setup check on the Router and on each of servers:
// the Router client's checks, then, per server, whether a Device has reached
// the Portal from it, which only the Controller can tell.
func (s *Server) checklist(ctx context.Context, router *routeros.Client, servers []string) ([]routeros.Check, error) {
	settings, err := s.ledger.PauseSettings()
	if err != nil {
		return nil, err
	}
	checks, err := router.Checklist(ctx, s.portalURL, settings.IdleSeconds, servers)
	if err != nil {
		return nil, err
	}
	for _, server := range servers {
		checks = append(checks, routeros.Check{
			Name: "device-seen", Server: server, Title: "A Device has been sent to the Portal from this server",
			Verify: func(context.Context) (routeros.Verdict, error) {
				arrived, err := s.ledger.PortalArrived(server)
				if err != nil || arrived {
					return routeros.Verdict{Met: arrived}, err
				}
				return routeros.Verdict{Reason: "No Device has reached the Portal from " + server + " yet; join it with a phone and open any website"}, nil
			},
		})
	}
	return checks, nil
}

// readChecklist verifies the checklist of servers, grouped with the Router's
// first. A check the Router cannot answer reads not met. fixes offers the
// one-click fix of each unmet item that has one.
func (s *Server) readChecklist(ctx context.Context, router *routeros.Client, servers []string, fixes bool) ([]checkGroup, error) {
	checks, err := s.checklist(ctx, router, servers)
	if err != nil {
		return nil, err
	}
	groups := []checkGroup{{}}
	for _, server := range servers {
		groups = append(groups, checkGroup{Server: server})
	}
	for _, check := range checks {
		verdict, err := check.Verify(ctx)
		if err != nil {
			verdict = routeros.Verdict{Reason: "Unable to read the Router: " + err.Error()}
		}
		item := checkItem{Verdict: verdict, Name: check.Name, Server: check.Server, Title: check.Title, FixLabel: check.FixLabel,
			Fixable: fixes && err == nil && !verdict.Met && check.Fix != nil && verdict.Manual == ""}
		group := &groups[slices.IndexFunc(groups, func(group checkGroup) bool { return group.Server == check.Server })]
		group.Items = append(group.Items, item)
	}
	return groups, nil
}

// fixCheck serves POST /admin/router/fix with check and server (empty for the
// Router): it runs that one item's fix, then shows Router setup, which reads
// the state the fix reached. Only the Router and Adopted servers are fixed.
func (s *Server) fixCheck(response http.ResponseWriter, request *http.Request) {
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	if err := request.ParseForm(); err != nil {
		http.Error(response, "invalid checklist item", http.StatusBadRequest)
		return
	}
	name, server := request.PostForm.Get("check"), request.PostForm.Get("server")
	adopted, err := s.ledger.AdoptedHotspotServers()
	if err != nil {
		http.Error(response, err.Error(), http.StatusInternalServerError)
		return
	}
	if server != "" && !slices.Contains(adopted, server) {
		http.Error(response, "HotSpot server "+server+" is not Adopted", http.StatusBadRequest)
		return
	}
	router, err := s.router()
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	defer router.Close()
	var servers []string
	if server != "" {
		servers = []string{server}
	}
	checks, err := s.checklist(request.Context(), router, servers)
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	index := slices.IndexFunc(checks, func(check routeros.Check) bool { return check.Name == name && check.Server == server })
	if index < 0 {
		http.Error(response, "no checklist item "+name, http.StatusBadRequest)
		return
	}
	check := checks[index]
	verdict, err := check.Verify(request.Context())
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	if !verdict.Met {
		if check.Fix == nil || verdict.Manual != "" {
			http.Error(response, verdict.Reason+": this item has no one-click fix", http.StatusBadRequest)
			return
		}
		if err := check.Fix(request.Context()); err != nil {
			http.Error(response, "unable to fix "+name+": "+err.Error(), http.StatusBadGateway)
			return
		}
	}
	http.Redirect(response, request, "/admin", http.StatusSeeOther)
}

// useExisting serves GET /admin/router/use-existing?server=…, which names what
// Adopting the ticked servers changes. It changes nothing.
func (s *Server) useExisting(response http.ResponseWriter, request *http.Request) {
	names := request.URL.Query()["server"]
	if len(names) == 0 {
		http.Error(response, "tick at least one HotSpot server", http.StatusBadRequest)
		return
	}
	router, err := s.router()
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	defer router.Close()
	adopted, err := s.ledger.AdoptedHotspotServers()
	if err != nil {
		http.Error(response, err.Error(), http.StatusInternalServerError)
		return
	}
	if err := router.CheckAdoptable(request.Context(), names, adopted); err != nil {
		http.Error(response, err.Error(), http.StatusBadRequest)
		return
	}
	servers, err := router.ListHotspotServers(request.Context())
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	servers = slices.DeleteFunc(servers, func(server routeros.HotspotServer) bool { return !slices.Contains(names, server.Name) })
	checklist, err := s.readChecklist(request.Context(), router, names, false)
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	s.renderPage(response, request, "use_existing", Page{Title: "Use existing", Section: "Settings", Active: "/admin",
		Data: useExistingView{Servers: servers, Directory: routeros.LoginDirectory, Checklist: checklist[1:]}})
}

// adoptServers serves POST /admin/router/adopt: it records the ticked servers as
// Adopted, then brings the Router and each Adopted server to the baseline.
func (s *Server) adoptServers(response http.ResponseWriter, request *http.Request) {
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	if err := request.ParseForm(); err != nil {
		http.Error(response, "invalid HotSpot servers", http.StatusBadRequest)
		return
	}
	names := request.PostForm["server"]
	if len(names) == 0 {
		http.Error(response, "tick at least one HotSpot server", http.StatusBadRequest)
		return
	}
	router, err := s.router()
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	defer router.Close()
	adopted, err := s.ledger.AdoptedHotspotServers()
	if err != nil {
		http.Error(response, err.Error(), http.StatusInternalServerError)
		return
	}
	if err := router.CheckAdoptable(request.Context(), names, adopted); err != nil {
		http.Error(response, err.Error(), http.StatusBadRequest)
		return
	}
	// Recorded before the Router changes, so a failed repair is retried at boot.
	if err := s.ledger.AdoptHotspotServers(names); err != nil {
		http.Error(response, err.Error(), http.StatusInternalServerError)
		return
	}
	if err := s.applyBaseline(request.Context(), router); err != nil {
		s.mu.Lock()
		s.routerStatus, s.routerError = "error", err.Error()
		s.mu.Unlock()
		http.Error(response, "Servers Adopted; unable to finish the Router baseline: "+err.Error(), http.StatusBadGateway)
		return
	}
	s.mu.Lock()
	s.routerStatus, s.routerError = "ready", ""
	s.mu.Unlock()
	http.Redirect(response, request, "/admin", http.StatusSeeOther)
}

// applyBaseline repairs the Router and every Adopted server, then applies the
// Pause settings to the Controller's profiles and Time Credit users.
func (s *Server) applyBaseline(ctx context.Context, router *routeros.Client) error {
	adopted, err := s.ledger.AdoptedHotspotServers()
	if err != nil {
		return err
	}
	settings, err := s.ledger.PauseSettings()
	if err != nil {
		return err
	}
	if err := router.EnsureBaseline(ctx, s.portalURL, settings.IdleSeconds, adopted); err != nil {
		return err
	}
	use, err := s.ledger.HotspotProfileUse()
	if err != nil {
		return err
	}
	return router.EnsurePauseSettings(ctx, settings.IdleEnabled, settings.IdleSeconds, use.TimeCredits, use.Profiles)
}
