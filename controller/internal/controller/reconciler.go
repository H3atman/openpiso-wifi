package controller

import (
	"context"
	"errors"
	"net/http"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

type reconciliationState struct {
	LastPoll string                   `json:"last_poll"`
	Error    string                   `json:"error"`
	Active   []routeros.ActiveSession `json:"active"`
	CoinGaps []ledger.CoinGap         `json:"coin_gaps"`
}

func (s *Server) intentsPage(response http.ResponseWriter, request *http.Request) {
	intents, err := s.ledger.CreditIntents()
	if err != nil {
		http.Error(response, "read credit intents", http.StatusInternalServerError)
		return
	}
	s.renderPage(response, request, "intents", Page{Title: "Credit intents", Section: "Coins", Active: "/admin/intents", Data: intents})
}

func (s *Server) wakeReconciler() {
	select {
	case s.reconcileWake <- struct{}{}:
	default:
	}
}

func (s *Server) runReconciler(ctx context.Context) {
	s.reconcile(ctx)
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.reconcile(ctx)
		case <-s.reconcileWake:
			s.reconcile(ctx)
		}
	}
}

func (s *Server) reconcile(parent context.Context) {
	// The same lock orders purchases, Pause, Rebind and Router projections.
	// An online Device with no pending intent is never removed/re-logged.
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	ctx, cancel := context.WithTimeout(parent, 30*time.Second)
	defer cancel()
	state := reconciliationState{Active: []routeros.ActiveSession{}, CoinGaps: []ledger.CoinGap{}}
	defer func() {
		s.mu.Lock()
		s.reconciliation = state
		s.mu.Unlock()
	}()
	if err := s.ledger.ExpireTimeCredits(); err != nil {
		state.Error = "Validity expiry pending"
		return
	}
	validity, err := s.ledger.Validities()
	if err != nil {
		state.Error = "Validity state unavailable"
		return
	}
	if err := s.ledger.ReconcileCoinWindows(s.coinBootPending); err != nil {
		state.Error = "Coin Window settlement pending"
		return
	}
	s.coinBootPending = false
	state.CoinGaps, err = s.ledger.CoinGaps()
	if err != nil {
		state.Error = "Coin total reconciliation unavailable"
		return
	}
	if len(state.CoinGaps) > 0 {
		state.Error = "Vendo coin total gap; inspect coin reconciliation"
	}
	router, err := s.router()
	if err != nil {
		state.Error = "Router temporarily unavailable"
		return
	}
	defer router.Close()
	for _, v := range validity {
		if v.ExpiredAt == 0 || v.RouterRemoved {
			continue
		}
		if err := router.RemoveTimeCredit(ctx, v.TimeCreditID); err != nil {
			state.Error = "Validity Router removal pending"
			continue
		}
		if err := s.ledger.FinishValidityRemoval(v.TimeCreditID); err != nil {
			state.Error = "Validity Router removal status unavailable"
		}
	}
	// Re-copy HotSpot profiles the Operator may have edited on the Router, and
	// drop copy pairs nothing uses any more.
	if err := s.reconcileProfileCopies(ctx, router); err != nil {
		state.Error = "HotSpot profile copies pending"
	}
	state.Active, err = router.ActiveSessions(ctx)
	if err != nil {
		state.Error = "Router active list unavailable"
		return
	}
	state.LastPoll = time.Now().UTC().Format(time.RFC3339)
	windows, err := s.ledger.CoinWindows()
	if err != nil {
		state.Error = "Coin Windows unavailable"
		return
	}
	for _, window := range windows {
		if window.Status != "open" {
			continue
		}
		device, err := router.DeviceAt(ctx, window.IP)
		if errors.Is(err, routeros.ErrDeviceNotFound) || (err == nil && device.MAC != window.MAC) {
			if err := s.ledger.CloseCoinWindow(window.ID, window.TimeCreditID); err != nil {
				state.Error = "Coin Window departure close pending"
			}
		} else if err != nil {
			state.Error = "Coin Window Device lookup unavailable"
		}
	}
	intents, err := s.ledger.CreditIntents()
	if err != nil {
		state.Error = "Credit intents unavailable"
		return
	}
	for _, intent := range intents {
		if !intent.Pending {
			continue
		}
		tc, err := s.ledger.TimeCreditByID(intent.TimeCreditID)
		if err == nil {
			_, err = s.projectRouterTimeCredit(ctx, router, tc, routeros.Device{MAC: tc.MAC, IP: tc.IP}, false)
		}
		if err != nil {
			state.Error = "Router projection failed; retry pending"
			if err := s.ledger.FinishCreditIntent(intent, err); err != nil {
				state.Error = "Credit intent status unavailable"
			}
		}
	}
	// Pause is a durable stop intent independent of bought-time changes. Finish
	// interrupted stops, including after a crash between TakePause and removal.
	pauses, err := s.ledger.Pauses()
	if err != nil {
		state.Error = "Pause state unavailable"
		return
	}
	for _, pause := range pauses {
		if pause.Tapped {
			if err := router.RemoveTimeCreditSessions(ctx, pause.TimeCreditID); err != nil {
				state.Error = "Pause removal pending"
			}
		}
	}
}

func (s *Server) reconcileProfileCopies(ctx context.Context, router *routeros.Client) error {
	settings, err := s.ledger.PauseSettings()
	if err != nil {
		return err
	}
	use, err := s.ledger.HotspotProfileUse()
	if err != nil {
		return err
	}
	if err := router.EnsureProfileCopies(ctx, use.Profiles, settings.IdleSeconds); err != nil {
		return err
	}
	return router.RemoveUnusedProfileCopies(ctx, use.Profiles)
}

// projectRouterTimeCredit is an absolute, retryable projection. Disconnecting an online
// Device commits its usage and refreshes the Router countdown on the next login.
// No usage or remaining-time value is persisted; only the Router owns that clock.
func (s *Server) projectRouterTimeCredit(ctx context.Context, router *routeros.Client, tc ledger.TimeCredit, device routeros.Device, resume bool) (used int64, projectionErr error) {
	// RouterOS limit-uptime=0 means unlimited, not an unpaid reservation.
	if tc.BoughtSeconds == 0 {
		return 0, nil
	}
	validity, err := s.ledger.Validity(tc.ID)
	if err != nil {
		return 0, err
	}
	if validity.Expired() {
		return 0, ledger.ErrTimeCreditExpired
	}
	intent, err := s.ledger.CreditIntent(tc.ID)
	if err != nil {
		return 0, err
	}
	defer func() {
		if err := s.ledger.FinishCreditIntent(intent, projectionErr); err != nil && projectionErr == nil {
			projectionErr = err
		}
	}()
	settings, err := s.ledger.PauseSettings()
	if err != nil {
		return 0, err
	}
	pause, err := s.ledger.PauseState(tc.ID)
	if err != nil {
		return 0, err
	}
	family, err := s.ledger.TimeCreditProfile(tc.ID)
	if err != nil {
		return 0, err
	}
	if family != "" {
		if err := router.EnsureProfileCopies(ctx, []string{family}, settings.IdleSeconds); err != nil {
			return 0, err
		}
	}
	if err := router.EnsureTimeCredit(ctx, tc.ID, tc.RouterPassword, tc.BoughtSeconds, routeros.ProfileName(family, settings.IdleEnabled)); err != nil {
		return 0, err
	}
	if err := router.RemoveTimeCreditSessions(ctx, tc.ID); err != nil {
		return 0, err
	}
	used, err = router.UsedSeconds(ctx, tc.ID)
	if err != nil {
		return 0, err
	}
	if used >= tc.BoughtSeconds || (pause.Tapped && !resume) || tc.MAC == "" || tc.IP == "" {
		return used, nil
	}
	// Never log a stale binding onto an IP now held by another Device.
	verified, err := router.DeviceAt(ctx, device.IP)
	if errors.Is(err, routeros.ErrDeviceNotFound) {
		return used, nil // projected; the absent Device may explicitly Connect later
	}
	if err != nil {
		return 0, err
	}
	if verified.MAC != device.MAC {
		return used, nil
	}
	if err := router.LoginDevice(ctx, tc.ID, tc.RouterPassword, verified); err != nil {
		return 0, err
	}
	return used, nil
}
