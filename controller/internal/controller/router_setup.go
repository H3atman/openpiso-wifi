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
// checklist reads each requirement on the Router and fixes one on request, an
// Adopted server deleted on the Router can be forgotten, and Next steps lists
// the Shop's remaining setup tasks.

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
	NextSteps    []nextStep // shown once a HotSpot server is Adopted
}

// nextStep is one remaining Shop setup task, read from the ledger and linking
// to the page where the Operator does it. Router setup adds no form for it.
type nextStep struct {
	Name, Title, Href string
	Done              bool
}

// readNextSteps lists the Shop's remaining setup tasks, done or not done.
func (s *Server) readNextSteps() ([]nextStep, error) {
	steps, err := s.ledger.NextSteps()
	if err != nil {
		return nil, err
	}
	return []nextStep{
		{Name: "shop-settings", Title: "Save the Shop settings", Href: "/admin/pause", Done: steps.ShopSettings},
		{Name: "rate-tiers", Title: "Set your Rate tiers", Href: "/admin/rates", Done: steps.RateTiers},
		{Name: "vendo-paired", Title: "Pair a Vendo", Href: "/admin/vendos", Done: steps.VendoPaired},
		{Name: "voucher-batch", Title: "Generate a first Voucher Batch", Href: "/admin/vouchers", Done: steps.VoucherBatch},
	}, nil
}

// useExistingView is the review before Adopting Servers, with the ticked
// servers' checklist as it reads before anything is fixed.
type useExistingView struct {
	Servers   []routeros.HotspotServer
	Directory string
	Checklist []checkGroup
}

// checkGroup is the checklist of the Router (Server empty) or of one HotSpot
// server. Missing marks an Adopted server the Router no longer has, which the
// page offers to forget (POST /admin/router/forget).
type checkGroup struct {
	Server  string
	Items   []checkItem
	Missing bool
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
		if len(view.Adopted) > 0 {
			if view.NextSteps, err = s.readNextSteps(); err != nil {
				http.Error(response, err.Error(), http.StatusInternalServerError)
				return
			}
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
	// The creating cards show their own failure, so the rest of the page stays.
	if view.AddAlongside, err = readAddAlongside(ctx, router); err != nil {
		view.AddAlongside = addAlongsideView{Error: err.Error()}
	}
	if view.CarryVLANs, err = s.readCarryVLANs(ctx, router, servers, view.Adopted); err != nil {
		return err
	}
	if view.FreshStart, err = s.readFreshStart(ctx, router); err != nil {
		view.FreshStart = freshStartView{Error: err.Error()}
	}
	if view.Checklist, err = s.readChecklist(ctx, router, view.Adopted, true); err != nil {
		return err
	}
	for i := range view.Checklist {
		group := &view.Checklist[i]
		group.Missing = group.Server != "" && !slices.ContainsFunc(servers, func(server routeros.HotspotServer) bool { return server.Name == group.Server })
	}
	return nil
}

// forgetServer serves POST /admin/router/forget with server: an Adopted server
// deleted on the Router by hand stops being Adopted, so the baseline no longer
// refuses it. It writes nothing to the Router, and a server the Router still
// has is refused: releasing a server is out of scope (ADR 0011).
func (s *Server) forgetServer(response http.ResponseWriter, request *http.Request) {
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	if err := request.ParseForm(); err != nil {
		http.Error(response, "invalid HotSpot server", http.StatusBadRequest)
		return
	}
	name := request.PostForm.Get("server")
	adopted, err := s.ledger.AdoptedHotspotServers()
	if err != nil {
		http.Error(response, err.Error(), http.StatusInternalServerError)
		return
	}
	if !slices.Contains(adopted, name) {
		http.Error(response, "HotSpot server "+name+" is not Adopted", http.StatusBadRequest)
		return
	}
	router, err := s.router()
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	defer router.Close()
	servers, err := router.ListHotspotServers(request.Context())
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	if slices.ContainsFunc(servers, func(server routeros.HotspotServer) bool { return server.Name == name }) {
		http.Error(response, "HotSpot server "+name+" is on the Router; only a server missing on the Router can be forgotten", http.StatusBadRequest)
		return
	}
	if err := s.ledger.ForgetHotspotServer(name); err != nil {
		http.Error(response, err.Error(), http.StatusInternalServerError)
		return
	}
	http.Redirect(response, request, "/admin", http.StatusSeeOther)
}

// checklist is every Router setup check on the Router and on each of servers
// (Adopted, or being Adopted): the Router client's checks, then, per server,
// whether a Device has reached the Portal from it, which only the Controller
// can tell.
func (s *Server) checklist(ctx context.Context, router *routeros.Client, servers []string) ([]routeros.Check, error) {
	settings, err := s.ledger.PauseSettings()
	if err != nil {
		return nil, err
	}
	adopted, err := s.ledger.AdoptedHotspotServers()
	if err != nil {
		return nil, err
	}
	checks, err := router.Checklist(ctx, s.portalURL, settings.IdleSeconds, servers, append(adopted, servers...))
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
	s.finishAdopting(response, request, router, names, "Servers Adopted", false)
}

// finishAdopting ends Use existing, Add alongside and Fresh start: it Adopts
// names, brings the Router and every Adopted server to the baseline and, for a
// server the Controller just created (exempt, names its one server), adds its
// NAT exemption, then shows Router setup. done says what has happened when a
// step fails.
func (s *Server) finishAdopting(response http.ResponseWriter, request *http.Request, router *routeros.Client, names []string, done string, exempt bool) {
	// Recorded before the Router changes, so a failed repair is retried at boot.
	if err := s.ledger.AdoptHotspotServers(names); err != nil {
		http.Error(response, err.Error(), http.StatusInternalServerError)
		return
	}
	if err := s.applyBaseline(request.Context(), router); err != nil {
		s.mu.Lock()
		s.routerStatus, s.routerError = "error", err.Error()
		s.mu.Unlock()
		http.Error(response, done+"; unable to finish the Router baseline: "+err.Error(), http.StatusBadGateway)
		return
	}
	s.mu.Lock()
	s.routerStatus, s.routerError = "ready", ""
	s.mu.Unlock()
	if exempt {
		if err := s.exemptFromNAT(request.Context(), router, names[0]); err != nil {
			http.Error(response, done+"; unable to add its NAT exemption: "+err.Error(), http.StatusBadGateway)
			return
		}
	}
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
