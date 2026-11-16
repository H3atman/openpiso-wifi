package controller

import (
	"database/sql"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

func boundTo(tc ledger.TimeCredit, device routeros.Device) bool {
	return tc.MAC == device.MAC && tc.IP == device.IP
}

// rebind removes the old and displaced Time Credits' sessions before atomically
// moving the ledger bindings. The caller holds voucherMu through the new login.
func (s *Server) rebind(request *http.Request, router *routeros.Client, tc ledger.TimeCredit, device routeros.Device, via string) (ledger.TimeCredit, error) {
	ids, err := s.ledger.DeviceTimeCreditIDs(device.MAC, device.IP)
	if err != nil {
		return tc, err
	}
	displaced := false
	for _, id := range ids {
		if id == tc.ID {
			continue
		}
		if err := router.RemoveTimeCreditSessions(request.Context(), id); err != nil {
			return tc, err
		}
		displaced = true
	}
	if !boundTo(tc, device) {
		if err := router.RemoveTimeCreditSessions(request.Context(), tc.ID); err != nil {
			return tc, err
		}
	} else if !displaced {
		return tc, nil
	}
	return s.ledger.Rebind(tc.ID, tc.MAC, tc.IP, device.MAC, device.IP, via)
}

// bindAndProject Rebinds tc when its proof (cookie or Resume Code) came from a
// different Device, then uses the journaled projection and tapped-Pause guard.
// Only explicit Resume actions may clear a tapped Pause after Router login.
func (s *Server) bindAndProject(response http.ResponseWriter, request *http.Request, router *routeros.Client, device routeros.Device, tc ledger.TimeCredit, via string, resume bool) {
	validity, err := s.ledger.Validity(tc.ID)
	if err != nil {
		s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, msgUnavailable)
		return
	}
	if validity.Expired() {
		if tc.PointsMilli > 0 {
			// Its Points outlive Validity (ADR 0006): bind it here, logged out,
			// so the Device's next purchase carries them.
			if tc, err = s.rebind(request, router, tc, device, via); err != nil {
				s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, msgConnectFailed)
				return
			}
			if err := s.setTimeCreditCookie(response, request, tc); err != nil {
				s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, msgUnavailable)
				return
			}
			s.renderPortal(response, request, router, device, http.StatusOK, msgPointsKept(tc.PointsMilli))
			return
		}
		// Reject stale ownership proof before removing or displacing any Device.
		message := msgValidityEnded
		if via == "resume-code" {
			message = msgResumeCodeExpired
		}
		s.renderPortal(response, request, router, device, http.StatusForbidden, message)
		return
	}
	if tc, err = s.rebind(request, router, tc, device, via); err != nil {
		s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, msgConnectFailed)
		return
	}
	s.loginTimeCredit(response, request, router, device, tc, resume)
}

var msgResumeCodeInvalid = &portalMessage{"error", "resume-code-invalid", "Resume Code invalid. Enter the eight characters shown after your purchase."}

// recoverTimeCredit redeems a Resume Code from the Router-verified Device.
// Guesses are limited per Device MAC, separately from Voucher attempts.
func (s *Server) recoverTimeCredit(response http.ResponseWriter, request *http.Request) {
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
	failure := s.resumeFailures[device.MAC]
	now := time.Now()
	if now.Before(failure.blocked) {
		s.renderPortal(response, request, router, device, http.StatusTooManyRequests, &portalMessage{"error", "too-many-attempts", "Too many Resume Code attempts. Try again in five minutes."})
		return
	}
	if !failure.blocked.IsZero() {
		delete(s.resumeFailures, device.MAC)
		failure = loginFailures{}
	}
	fail := func(message *portalMessage) {
		failure.count++
		if failure.count >= maxLoginFailures {
			failure.blocked = now.Add(5 * time.Minute)
		}
		s.resumeFailures[device.MAC] = failure
		s.renderPortal(response, request, router, device, http.StatusBadRequest, message)
	}
	if err := request.ParseForm(); err != nil {
		fail(msgResumeCodeInvalid)
		return
	}
	code := strings.ToUpper(strings.TrimSpace(request.PostForm.Get("code")))
	if len(code) != 8 || strings.Trim(code, ledger.VoucherAlphabet) != "" {
		fail(msgResumeCodeInvalid)
		return
	}
	tc, err := s.ledger.TimeCreditByResumeCode(code)
	if errors.Is(err, sql.ErrNoRows) {
		fail(&portalMessage{"error", "resume-code-unknown", "Resume Code not found. Check the code shown after your purchase."})
		return
	}
	if err != nil {
		s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, msgUnavailable)
		return
	}
	delete(s.resumeFailures, device.MAC)
	s.bindAndProject(response, request, router, device, tc, "resume-code", true)
}
