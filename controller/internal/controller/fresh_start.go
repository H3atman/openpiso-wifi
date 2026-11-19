package controller

import (
	"context"
	"errors"
	"net/http"
	"net/netip"
	"strings"

	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

// Fresh start: delete the Router's HotSpot tree and create one HotSpot server,
// by default on the old server's interface and network, and Adopt it. It runs
// only while the ledger holds nothing the wipe would destroy, and only while
// the Router backup saved at connect, the undo, is still on the Router.

// freshStartConfirmation is what the Operator types to confirm the wipe.
const freshStartConfirmation = "DELETE"

// freshStartView is the Fresh start card on Router setup. Blocked says why it is
// not offered; Backup is the backup file that covers the wipe, "" when the
// Router no longer holds the one saved at connect.
type freshStartView struct {
	Blocked      string
	Backup       string
	Plan         routeros.FreshStartPlan
	Confirmation string
}

// readFreshStart reads the Fresh start card. It writes nothing.
func (s *Server) readFreshStart(ctx context.Context, router *routeros.Client) (freshStartView, error) {
	view := freshStartView{Confirmation: freshStartConfirmation}
	var err error
	if view.Blocked, err = s.ledger.FreshStartBlocker(); err != nil || view.Blocked != "" {
		return view, err
	}
	if view.Backup, err = s.freshStartBackup(ctx, router); err != nil {
		return view, err
	}
	view.Plan, err = router.PlanFreshStart(ctx)
	return view, err
}

// freshStartBackup is the backup file saved at connect when the Router still
// holds it, else "".
func (s *Server) freshStartBackup(ctx context.Context, router *routeros.Client) (string, error) {
	name, err := s.ledger.RouterBackup()
	if err != nil || name == "" {
		return "", err
	}
	saved, err := router.BackupSaved(ctx, name)
	if err != nil || !saved {
		return "", err
	}
	return name, nil
}

// freshStart serves POST /admin/router/fresh-start with confirm, interface and
// range. Before any Router write it refuses with 400 a missing confirmation, a
// ledger with Time Credits or paired Vendos, a Router without the backup saved
// at connect, and a placement CreateHotspotServer would refuse. Then it forgets
// the Adopted servers, deletes the HotSpot tree, creates the new server, Adopts
// it and brings it to the baseline with its NAT exemption.
func (s *Server) freshStart(response http.ResponseWriter, request *http.Request) {
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	if err := request.ParseForm(); err != nil {
		http.Error(response, "invalid Fresh start", http.StatusBadRequest)
		return
	}
	if strings.TrimSpace(request.PostForm.Get("confirm")) != freshStartConfirmation {
		http.Error(response, "type "+freshStartConfirmation+" to confirm Fresh start", http.StatusBadRequest)
		return
	}
	blocked, err := s.ledger.FreshStartBlocker()
	if err != nil {
		http.Error(response, err.Error(), http.StatusInternalServerError)
		return
	}
	if blocked != "" {
		http.Error(response, blocked, http.StatusBadRequest)
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
	ctx := request.Context()
	backup, err := s.freshStartBackup(ctx, router)
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	if backup == "" {
		http.Error(response, "the Router no longer holds the backup saved when it was connected; connect the Router again to save one", http.StatusBadRequest)
		return
	}
	placement := routeros.Placement{Interface: request.PostForm.Get("interface")}
	var refusal routeros.Refusal
	if err := router.CheckFreshStart(ctx, placement, network); errors.As(err, &refusal) {
		http.Error(response, refusal.Error(), http.StatusBadRequest)
		return
	} else if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	// Forgotten before the wipe, so the ledger never names a server the Router lacks.
	if err := s.ledger.ForgetHotspotServers(); err != nil {
		http.Error(response, err.Error(), http.StatusInternalServerError)
		return
	}
	if err := router.WipeHotspot(ctx); err != nil {
		http.Error(response, "unable to delete the HotSpot configuration; run Fresh start again to finish: "+err.Error(), http.StatusBadGateway)
		return
	}
	name, err := router.CreateHotspotServer(ctx, placement, network)
	if err != nil {
		http.Error(response, "HotSpot configuration deleted; unable to create the HotSpot server, run Fresh start again: "+err.Error(), http.StatusBadGateway)
		return
	}
	if err := s.ledger.AdoptHotspotServers([]string{name}); err != nil {
		http.Error(response, err.Error(), http.StatusInternalServerError)
		return
	}
	if err := s.applyBaseline(ctx, router); err != nil {
		s.mu.Lock()
		s.routerStatus, s.routerError = "error", err.Error()
		s.mu.Unlock()
		http.Error(response, "HotSpot server "+name+" created and Adopted; unable to finish the Router baseline: "+err.Error(), http.StatusBadGateway)
		return
	}
	s.mu.Lock()
	s.routerStatus, s.routerError = "ready", ""
	s.mu.Unlock()
	if err := s.exemptFromNAT(ctx, router, name); err != nil {
		http.Error(response, "HotSpot server "+name+" created and Adopted; unable to add its NAT exemption: "+err.Error(), http.StatusBadGateway)
		return
	}
	http.Redirect(response, request, "/admin", http.StatusSeeOther)
}
