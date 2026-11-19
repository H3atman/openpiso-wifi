package controller

import (
	"context"
	"errors"
	"net/http"
	"net/netip"
	"slices"
	"strings"

	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

// Add alongside: create a HotSpot server on an interface that has none, on a
// range the Operator accepts or edits, and Adopt it. Existing HotSpot servers
// are not changed. The Router backup saved at connect is the undo.

// addAlongsideView is the Add alongside card on Router setup: Interfaces are
// those with no HotSpot server, Range the suggested address range.
type addAlongsideView struct {
	Interfaces []string
	Range      string
}

// readAddAlongside reads the free interfaces and a suggested range. It writes nothing.
func readAddAlongside(ctx context.Context, router *routeros.Client) (addAlongsideView, error) {
	interfaces, err := router.FreeInterfaces(ctx)
	if err != nil {
		return addAlongsideView{}, err
	}
	suggested, err := router.SuggestRange(ctx)
	if err != nil {
		return addAlongsideView{}, err
	}
	return addAlongsideView{Interfaces: interfaces, Range: suggested.String()}, nil
}

// addAlongside serves POST /admin/router/add-alongside with interface and range:
// it creates a HotSpot server there, Adopts it and brings it to the baseline. An
// interface that is not free or a range that is not private or overlaps
// anything on the Router is refused with 400 before any Router write.
func (s *Server) addAlongside(response http.ResponseWriter, request *http.Request) {
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	if err := request.ParseForm(); err != nil {
		http.Error(response, "invalid HotSpot server", http.StatusBadRequest)
		return
	}
	network, err := netip.ParsePrefix(strings.TrimSpace(request.PostForm.Get("range")))
	if err != nil {
		http.Error(response, "the address range must look like 10.0.0.0/24", http.StatusBadRequest)
		return
	}
	router, err := s.router()
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	defer router.Close()
	name, err := router.CreateHotspotServer(request.Context(), routeros.Placement{Interface: request.PostForm.Get("interface")}, network)
	var refusal routeros.Refusal
	if errors.As(err, &refusal) {
		http.Error(response, refusal.Error(), http.StatusBadRequest)
		return
	}
	if err != nil {
		http.Error(response, "unable to create the HotSpot server: "+err.Error(), http.StatusBadGateway)
		return
	}
	if err := s.ledger.AdoptHotspotServers([]string{name}); err != nil {
		http.Error(response, err.Error(), http.StatusInternalServerError)
		return
	}
	if err := s.applyBaseline(request.Context(), router); err != nil {
		s.mu.Lock()
		s.routerStatus, s.routerError = "error", err.Error()
		s.mu.Unlock()
		http.Error(response, "HotSpot server "+name+" created and Adopted; unable to finish the Router baseline: "+err.Error(), http.StatusBadGateway)
		return
	}
	s.mu.Lock()
	s.routerStatus, s.routerError = "ready", ""
	s.mu.Unlock()
	if err := s.exemptFromNAT(request.Context(), router, name); err != nil {
		http.Error(response, "HotSpot server "+name+" created and Adopted; unable to add its NAT exemption: "+err.Error(), http.StatusBadGateway)
		return
	}
	http.Redirect(response, request, "/admin", http.StatusSeeOther)
}

// exemptFromNAT runs the portal-reachable fix for a server the Controller just
// created: the Controller's NAT exemption for its network, which only adds a
// Controller-owned rule. Without it a masquerade rewrites its Devices' addresses
// and the Portal cannot find them.
func (s *Server) exemptFromNAT(ctx context.Context, router *routeros.Client, server string) error {
	checks, err := s.checklist(ctx, router, []string{server})
	if err != nil {
		return err
	}
	index := slices.IndexFunc(checks, func(check routeros.Check) bool { return check.Name == "portal-reachable" })
	if index < 0 {
		return errors.New("HotSpot server " + server + " is missing on the Router")
	}
	return checks[index].Fix(ctx)
}
