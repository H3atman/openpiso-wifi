package controller

import (
	"crypto/hmac"
	"errors"
	"net/http"
	"strconv"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

func (s *Server) pauseSettingsPage(response http.ResponseWriter, request *http.Request) {
	settings, err := s.ledger.PauseSettings()
	if err != nil {
		http.Error(response, "read Pause settings", http.StatusInternalServerError)
		return
	}
	s.renderPage(response, request, "pause", Page{Title: "Pause", Section: "Settings", Active: "/admin/pause", Data: settings})
}

func (s *Server) savePauseSettings(response http.ResponseWriter, request *http.Request) {
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	if err := request.ParseForm(); err != nil {
		http.Error(response, "invalid Pause settings", http.StatusBadRequest)
		return
	}
	limit, limitErr := strconv.Atoi(request.PostForm.Get("pause_limit"))
	seconds, secondsErr := strconv.Atoi(request.PostForm.Get("idle_seconds"))
	enabled := request.PostForm.Get("idle_enabled")
	if limitErr != nil || secondsErr != nil || (enabled != "on" && enabled != "off") || limit < 0 || limit > 1000 || seconds < 1 || seconds > 86400 {
		http.Error(response, "choose a Pause Limit of 0–1000 and an idle period of 1–86400 seconds", http.StatusBadRequest)
		return
	}
	settings := ledger.PauseSettings{PauseLimit: limit, IdleEnabled: enabled == "on", IdleSeconds: seconds}
	// Persist the desired settings first; retry/boot can repair a partial Router
	// projection without reporting settings that would be forgotten on restart.
	if err := s.ledger.SavePauseSettings(settings); err != nil {
		http.Error(response, "save Pause settings", http.StatusInternalServerError)
		return
	}
	router, err := s.router()
	if err != nil {
		http.Error(response, "Settings saved; Router temporarily unavailable. Retry Save.", http.StatusServiceUnavailable)
		return
	}
	defer router.Close()
	use, err := s.ledger.HotspotProfileUse()
	if err != nil {
		http.Error(response, "Settings saved; unable to read HotSpot profiles. Retry Save.", http.StatusInternalServerError)
		return
	}
	if err := router.EnsurePauseSettings(request.Context(), settings.IdleEnabled, settings.IdleSeconds, use.TimeCredits, use.Profiles); err != nil {
		http.Error(response, "Settings saved; Router temporarily unavailable. Retry Save.", http.StatusServiceUnavailable)
		return
	}
	// Existing sessions take their policy at login. Restart only on this
	// explicit admin action, using ledger-held passwords, not Router secrets.
	purchases, err := s.ledger.PurchaseState()
	if err != nil {
		http.Error(response, "Settings saved; unable to read Time Credits. Retry Save.", http.StatusInternalServerError)
		return
	}
	for _, tc := range purchases.TimeCredits {
		validity, err := s.ledger.Validity(tc.ID)
		if err != nil {
			http.Error(response, "Settings saved; unable to read Validity. Retry Save.", http.StatusInternalServerError)
			return
		}
		if validity.Expired() {
			if err := router.RemoveTimeCreditSessions(request.Context(), tc.ID); err != nil {
				http.Error(response, "Settings saved; Validity removal pending.", http.StatusServiceUnavailable)
				return
			}
			continue
		}
		active, err := router.TimeCreditActive(request.Context(), tc.ID)
		if err != nil {
			http.Error(response, "Settings saved; Router temporarily unavailable. Retry Save.", http.StatusServiceUnavailable)
			return
		}
		if !active {
			continue
		}
		pause, err := s.ledger.PauseState(tc.ID)
		if err == nil {
			err = router.RemoveTimeCreditSessions(request.Context(), tc.ID)
		}
		if err == nil && !pause.Tapped {
			err = router.LoginDevice(request.Context(), tc.ID, tc.RouterPassword, routeros.Device{MAC: tc.MAC, IP: tc.IP})
		}
		if err != nil {
			http.Error(response, "Settings saved; Router temporarily unavailable. Retry Save.", http.StatusServiceUnavailable)
			return
		}
	}
	s.pauseSettingsPage(response, request)
}

func (s *Server) ownsTimeCredit(request *http.Request, tc ledger.TimeCredit) bool {
	cookie, err := request.Cookie("openpiso_time_credit")
	if err != nil {
		return false
	}
	key, err := s.ledger.InstallKey()
	if err != nil {
		return false
	}
	return hmac.Equal([]byte(cookie.Value), []byte(tc.Token+"."+signToken(key, tc.Token)))
}

// Both actions require the Router-verified current Device. Only Resume requires
// ownership proof: a cookie-free captive popup may Pause, never reclaim time.
func (s *Server) pauseAction(response http.ResponseWriter, request *http.Request) {
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	router, err := s.router()
	if err != nil {
		s.renderPortal(response, request, nil, routeros.Device{}, http.StatusServiceUnavailable, msgUnavailable)
		return
	}
	defer router.Close()
	device, err := router.DeviceAt(request.Context(), remoteAddress(request))
	if err != nil {
		s.renderPortal(response, request, nil, device, http.StatusForbidden, msgNotOnWiFi)
		return
	}
	tc, err := s.currentTimeCredit(request, device)
	if err != nil {
		s.renderPortal(response, request, router, device, http.StatusForbidden, &portalMessage{"error", "no-time-credit", "No Time Credit for this Device."})
		return
	}
	validity, err := s.ledger.Validity(tc.ID)
	if err != nil {
		s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, msgUnavailable)
		return
	}
	if validity.Expired() {
		s.renderPortal(response, request, router, device, http.StatusForbidden, msgValidityEnded)
		return
	}
	if request.URL.Path == "/portal/api/resume" {
		if !s.ownsTimeCredit(request, tc) {
			s.renderPortal(response, request, router, device, http.StatusForbidden,
				&portalMessage{"error", "resume-needs-proof", "Resume needs your signed cookie or Resume Code."})
			return
		}
		// Clear only after a successful login. On failure, the durable tapped
		// intent still prevents a later Connect from resuming implicitly.
		s.bindAndProject(response, request, router, device, tc, "cookie", true)
		return
	}
	if !boundTo(tc, device) {
		s.renderPortal(response, request, router, device, http.StatusForbidden,
			&portalMessage{"error", "rebind-first", "Rebind this Time Credit before taking a Pause."})
		return
	}
	settings, err := s.ledger.PauseSettings()
	if err == nil {
		err = s.ledger.TakePause(tc.ID, settings.PauseLimit)
	}
	if errors.Is(err, ledger.ErrPauseLimit) {
		s.renderPortal(response, request, router, device, http.StatusConflict, &portalMessage{"error", "pause-limit", err.Error()})
		return
	}
	if err == nil {
		err = router.RemoveTimeCreditSessions(request.Context(), tc.ID)
	}
	if err != nil {
		s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, msgPauseLeftOpen)
		return
	}
	// If the Router usage read fails, the state shows it as usage_known false.
	s.renderPortal(response, request, router, device, http.StatusOK, &portalMessage{"success", "paused", "Paused. Tap Resume to continue."})
}
