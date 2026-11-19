package controller

import (
	"context"
	"net/http"
	"slices"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

// Router setup: connect a Router, then Use existing Adopts the HotSpot servers
// the Operator ticks (ADR 0011). Only Adopted servers are ever changed.

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
	Error     string
}

// useExistingView is the review before Adopting Servers.
type useExistingView struct {
	Servers   []routeros.HotspotServer
	Directory string
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
		view.Version, view.Available, view.Servers, err = s.readRouterSetup(request.Context(), view.Adopted)
		if err != nil {
			view.Error = err.Error()
		}
	}
	s.renderPage(response, request, "admin", Page{Title: "Router setup", Section: "Settings", Active: "/admin", Data: view})
}

// readRouterSetup reads the Router's version and its HotSpot servers not in adopted.
func (s *Server) readRouterSetup(ctx context.Context, adopted []string) (string, []routeros.HotspotServer, int, error) {
	router, err := s.router()
	if err != nil {
		return "", nil, 0, err
	}
	defer router.Close()
	ctx, cancel := context.WithTimeout(ctx, routerSetupTimeout)
	defer cancel()
	version, err := router.Version(ctx)
	if err != nil {
		return "", nil, 0, err
	}
	servers, err := router.ListHotspotServers(ctx)
	if err != nil {
		return version, nil, 0, err
	}
	available := slices.DeleteFunc(slices.Clone(servers), func(server routeros.HotspotServer) bool {
		return slices.Contains(adopted, server.Name)
	})
	return version, available, len(servers), nil
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
	s.renderPage(response, request, "use_existing", Page{Title: "Use existing", Section: "Settings", Active: "/admin",
		Data: useExistingView{Servers: servers, Directory: routeros.LoginDirectory}})
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
