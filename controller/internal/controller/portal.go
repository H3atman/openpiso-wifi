package controller

import (
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

// portalMessage is the one line of feedback a Portal response carries. Kind is
// success, warning or error; Code is stable for the Portal's JS; Text is for people.
type portalMessage struct {
	Kind string `json:"kind"`
	Code string `json:"code"`
	Text string `json:"text"`
}

var (
	msgUnavailable   = &portalMessage{"error", "unavailable", "Service temporarily unavailable. Please try again later."}
	msgNotOnWiFi     = &portalMessage{"error", "not-on-wifi", "Connect through the WiFi to see your time or redeem a Voucher."}
	msgDeviceJoining = &portalMessage{"warning", "device-joining", "Your Device is still joining the WiFi. Try again in a few seconds."}
	msgConnectFailed = &portalMessage{"error", "connect-failed", "Service temporarily unavailable. Your bought time is saved; try Connect again."}
	msgOnline        = &portalMessage{"success", "online", "You are online."}
	msgTimeUp        = &portalMessage{"warning", "time-up", "Your time has run out. Enter another Voucher."}
	msgValidityEnded = &portalMessage{"warning", "validity-ended", ledger.ErrTimeCreditExpired.Error()}
	msgPauseLeftOpen = &portalMessage{"error", "pause-unfinished", "Service temporarily unavailable. Retry Pause to finish stopping access."}
	msgOutOfService  = &portalMessage{"error", "vendo-out-of-service", "The coin slot for this network is out of service."}
)

// portalTimeCredit is the customer's view of the Time Credit bound to their Device.
type portalTimeCredit struct {
	Bought     int64 `json:"bought"`
	Remaining  int64 `json:"remaining"`
	UsageKnown bool  `json:"usage_known"`
	ValidUntil int64 `json:"valid_until"` // Unix seconds
	Expired    bool  `json:"expired"`
	// Paused is a tapped Pause. An Idle Pause shows only as !Online.
	Paused     bool `json:"paused"`
	PausesLeft int  `json:"pauses_left"`
	PauseLimit int  `json:"pause_limit"`
	// ResumeCode is withheld while tapped-Paused: a cookie-free visit must not gain proof to Resume.
	ResumeCode string `json:"resume_code,omitempty"`
	Online     bool   `json:"online"`
	// PointsMilli is the Points balance in thousandths. It outlives Validity.
	PointsMilli int64 `json:"points_milli"`
}

type portalCoinWindow struct {
	ID              uint32 `json:"id"`
	VendoID         uint32 `json:"vendo_id"`
	Status          string `json:"status"`
	Coins           int64  `json:"coins"`
	CreditedSeconds int64  `json:"credited_seconds"`
	ClosesAt        int64  `json:"closes_at"` // Unix seconds an open window idles shut; 0 once Done
}

// portalVendo is one Vendo the Device can use, with the rates it charges.
type portalVendo struct {
	ID    uint32            `json:"id"`
	Rates []ledger.RateTier `json:"rates"`
}

// portalState is the body of every /portal/api/ response.
type portalState struct {
	TimeCredit *portalTimeCredit `json:"time_credit"`
	// Rebind: the signed cookie owns a Time Credit bound to another Device.
	// Only POST /portal/api/connect moves it; the state reveals nothing about it.
	Rebind bool `json:"rebind"`
	// Rates are those of the Vendo serving the Device (the first of several),
	// or the site's when none does.
	Rates []ledger.RateTier `json:"rates"`
	// Points is the Points table this Device can redeem from (ADR 0006): its
	// serving Vendo's, or the site's.
	Points     []ledger.PointsTier `json:"points"`
	Vendos     []portalVendo       `json:"vendos"`
	CoinWindow *portalCoinWindow   `json:"coin_window"`
	Message    *portalMessage      `json:"message"`
}

// siteOffer is the Portal state every Device sees before its own: the site's
// rates and Points table, and no Vendos.
func (s *Server) siteOffer() portalState {
	state := portalState{Points: []ledger.PointsTier{}, Vendos: []portalVendo{}}
	state.Rates, _ = s.ledger.RateTiers()
	if points, err := s.ledger.PointsTiers(); err == nil {
		state.Points = points
	}
	return state
}

// readPortalState reads the Device's Portal state from the ledger and the
// Router, the only meter. It writes nothing to either. It returns the bound Time
// Credit it showed (zero if none) and the HTTP status of the read.
func (s *Server) readPortalState(request *http.Request, router *routeros.Client, device routeros.Device) (portalState, ledger.TimeCredit, int) {
	state := s.siteOffer()
	site := state.Rates
	vendos, _, _ := s.matchingVendos(device.HotspotServer)
	for _, vendo := range vendos {
		rates := vendo.RateTiers
		if rates == nil {
			rates = site
		}
		state.Vendos = append(state.Vendos, portalVendo{vendo.ID, rates})
	}
	state.Rates = site
	if len(state.Vendos) > 0 {
		state.Rates = state.Vendos[0].Rates
	}
	state.Points = servingPoints(vendos, state.Points)
	tc, err := s.currentTimeCredit(request, device)
	if errors.Is(err, sql.ErrNoRows) {
		return state, ledger.TimeCredit{}, http.StatusOK
	}
	if err != nil {
		state.Message = msgUnavailable
		return state, ledger.TimeCredit{}, http.StatusServiceUnavailable
	}
	if !boundTo(tc, device) {
		state.Rebind = true
		return state, ledger.TimeCredit{}, http.StatusOK
	}
	view, err := s.timeCreditView(tc)
	if err != nil {
		state.Message = msgUnavailable
		return state, ledger.TimeCredit{}, http.StatusServiceUnavailable
	}
	state.TimeCredit = &view
	if windows, err := s.ledger.CoinWindows(); err == nil {
		for _, w := range windows {
			if w.TimeCreditID == tc.ID && w.ClosedAt == 0 {
				state.CoinWindow = &portalCoinWindow{w.ID, w.VendoID, w.Status, w.Coins, w.CreditedSeconds, 0}
				if w.Status == "open" {
					state.CoinWindow.ClosesAt = (w.LastCoinAt + ledger.CoinWindowIdle.Milliseconds()) / 1000
				}
			}
		}
	}
	if view.Expired {
		state.Message = msgValidityEnded
		if tc.PointsMilli > 0 {
			state.Message = msgPointsKept(tc.PointsMilli)
		}
		return state, tc, http.StatusOK
	}
	if tc.BoughtSeconds == 0 {
		return state, tc, http.StatusOK
	}
	used, err := router.UsedSeconds(request.Context(), tc.ID)
	if err != nil {
		state.Message = msgConnectFailed
		return state, tc, http.StatusOK
	}
	view.Remaining, view.UsageKnown = tc.RemainingSeconds(used), true
	if view.Remaining == 0 {
		state.Message = msgTimeUp
		return state, tc, http.StatusOK
	}
	if view.Online, err = router.TimeCreditActive(request.Context(), tc.ID); err != nil {
		state.Message = msgUnavailable
	}
	return state, tc, http.StatusOK
}

// timeCreditView reads a Time Credit's Validity and Pause from the ledger; the
// Router-metered fields are left to readPortalState.
func (s *Server) timeCreditView(tc ledger.TimeCredit) (portalTimeCredit, error) {
	view := portalTimeCredit{Bought: tc.BoughtSeconds, PointsMilli: tc.PointsMilli}
	validity, err := s.ledger.Validity(tc.ID)
	if err != nil {
		return view, err
	}
	view.ValidUntil, view.Expired = validity.ValidUntil, validity.Expired()
	if view.Expired {
		view.UsageKnown = true
		return view, nil
	}
	pause, err := s.ledger.PauseState(tc.ID)
	if err != nil {
		return view, err
	}
	view.PauseLimit = pause.PauseLimit
	view.PausesLeft = max(0, pause.PauseLimit-pause.TappedCount)
	view.Paused = pause.Tapped
	if !pause.Tapped {
		view.ResumeCode, err = s.ledger.ResumeCode(tc.ID)
	}
	return view, err
}

// renderPortal answers a Portal action with the Device's fresh state, message
// (when set) replacing the state's own. Without a Router or a verified Device the
// state holds only the site offer.
func (s *Server) renderPortal(response http.ResponseWriter, request *http.Request, router *routeros.Client, device routeros.Device, status int, message *portalMessage) {
	state := s.siteOffer()
	if router != nil && device.MAC != "" {
		state, _, _ = s.readPortalState(request, router, device)
	}
	if message != nil {
		state.Message = message
	}
	writePortal(response, status, state)
}

func writePortal(response http.ResponseWriter, status int, state portalState) {
	response.Header().Set("Content-Type", "application/json")
	response.WriteHeader(status)
	json.NewEncoder(response).Encode(state)
}

func signToken(key []byte, token string) string {
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(token))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

// A signed cookie proves ownership of an unexpired Time Credit, or an expired
// one still holding Points, even on a new Device; otherwise, the Device's
// current binding identifies the Time Credit.
// Callers compare the binding with the verified Device and Rebind only on POST.
func (s *Server) currentTimeCredit(request *http.Request, device routeros.Device) (ledger.TimeCredit, error) {
	if cookie, err := request.Cookie("openpiso_time_credit"); err == nil {
		parts := strings.Split(cookie.Value, ".")
		if len(parts) == 2 {
			key, err := s.ledger.InstallKey()
			if err != nil {
				return ledger.TimeCredit{}, err
			}
			if hmac.Equal([]byte(parts[1]), []byte(signToken(key, parts[0]))) {
				tc, err := s.ledger.TimeCreditByToken(parts[0])
				if err == nil {
					validity, err := s.ledger.Validity(tc.ID)
					if err != nil {
						return ledger.TimeCredit{}, err
					}
					if !validity.Expired() || tc.PointsMilli > 0 {
						return tc, nil
					}
				} else if !errors.Is(err, sql.ErrNoRows) {
					return tc, err
				}
			}
		}
	}
	return s.ledger.TimeCreditByDevice(device.MAC, device.IP)
}

func (s *Server) setTimeCreditCookie(response http.ResponseWriter, request *http.Request, tc ledger.TimeCredit) error {
	key, err := s.ledger.InstallKey()
	if err != nil {
		return err
	}
	http.SetCookie(response, &http.Cookie{Name: "openpiso_time_credit", Value: tc.Token + "." + signToken(key, tc.Token), Path: "/portal", HttpOnly: true,
		SameSite: http.SameSiteLaxMode, Secure: request.TLS != nil, MaxAge: 30 * 24 * 60 * 60})
	return nil
}

// portalStateRead answers GET /portal/api/state. It never logs in, Rebinds or
// finishes a Pause: those happen only through POST /portal/api/connect.
func (s *Server) portalStateRead(response http.ResponseWriter, request *http.Request) {
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	router, err := s.router()
	if err != nil {
		s.renderPortal(response, request, nil, routeros.Device{}, http.StatusOK, msgUnavailable)
		return
	}
	defer router.Close()
	device, err := router.DeviceAt(request.Context(), remoteAddress(request))
	if errors.Is(err, routeros.ErrDeviceNotReady) {
		s.renderPortal(response, request, nil, device, http.StatusServiceUnavailable, msgDeviceJoining)
		return
	}
	if err != nil {
		s.renderPortal(response, request, nil, device, http.StatusOK, msgNotOnWiFi)
		return
	}
	state, tc, status := s.readPortalState(request, router, device)
	// Refresh ownership after falling back from an expired cookie. The cookie is
	// idempotent and touches neither ledger nor Router; a tapped-Paused visit never gets one.
	if view := state.TimeCredit; view != nil && !view.Paused && !view.Expired && view.Bought > 0 {
		if err := s.setTimeCreditCookie(response, request, tc); err != nil {
			s.renderPortal(response, request, nil, device, http.StatusServiceUnavailable, msgUnavailable)
			return
		}
	}
	writePortal(response, status, state)
}

func (s *Server) connectTimeCredit(response http.ResponseWriter, request *http.Request) {
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	router, err := s.router()
	if err != nil {
		s.renderPortal(response, request, nil, routeros.Device{}, http.StatusServiceUnavailable, msgUnavailable)
		return
	}
	defer router.Close()
	device, err := router.DeviceAt(request.Context(), remoteAddress(request))
	if errors.Is(err, routeros.ErrDeviceNotReady) {
		s.renderPortal(response, request, nil, device, http.StatusServiceUnavailable, msgDeviceJoining)
		return
	}
	if err != nil {
		s.renderPortal(response, request, nil, device, http.StatusForbidden, msgNotOnWiFi)
		return
	}
	tc, err := s.currentTimeCredit(request, device)
	if err != nil {
		s.renderPortal(response, request, router, device, http.StatusForbidden, &portalMessage{"error", "no-time-credit", "No Time Credit for this Device. Enter a Voucher."})
		return
	}
	s.bindAndProject(response, request, router, device, tc, "cookie", false)
}

// loginTimeCredit logs a bound Time Credit in on the Router. Reconnecting after
// an Idle Pause is this login; a tapped Pause first needs an explicit Resume.
func (s *Server) loginTimeCredit(response http.ResponseWriter, request *http.Request, router *routeros.Client, device routeros.Device, tc ledger.TimeCredit, resume bool) {
	view, err := s.timeCreditView(tc)
	if err != nil {
		s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, msgUnavailable)
		return
	}
	if view.Expired {
		s.renderPortal(response, request, router, device, http.StatusForbidden, msgValidityEnded)
		return
	}
	if view.Paused && !resume {
		// Finish any interrupted Pause before reporting it as stopped.
		if err := router.RemoveTimeCreditSessions(request.Context(), tc.ID); err != nil {
			s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, msgPauseLeftOpen)
			return
		}
		s.renderPortal(response, request, router, device, http.StatusForbidden,
			&portalMessage{"error", "paused-needs-resume", "Tapped Pause needs a tap on Resume and your signed cookie or Resume Code."})
		return
	}
	failed := func() {
		message := msgConnectFailed
		if view.Paused {
			message = &portalMessage{"error", "resume-failed", "Service temporarily unavailable. Your bought time is saved; try Resume again."}
		}
		s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, message)
	}
	used, err := s.projectRouterTimeCredit(request.Context(), router, tc, device, resume)
	if err != nil {
		failed()
		return
	}
	if tc.RemainingSeconds(used) == 0 {
		s.renderPortal(response, request, router, device, http.StatusOK, nil)
		return
	}
	active, err := router.TimeCreditActive(request.Context(), tc.ID)
	if err != nil || !active {
		failed()
		return
	}
	if resume {
		if err := s.ledger.ResumePause(tc.ID); err != nil {
			if err := router.RemoveTimeCreditSessions(request.Context(), tc.ID); err != nil {
				s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, msgPauseLeftOpen)
				return
			}
			failed()
			return
		}
	}
	if err := s.setTimeCreditCookie(response, request, tc); err != nil {
		failed()
		return
	}
	s.renderPortal(response, request, router, device, http.StatusOK, msgOnline)
}
