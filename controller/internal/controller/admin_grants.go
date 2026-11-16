package controller

import (
	"database/sql"
	"errors"
	"net/http"
	"strconv"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

// The admin POST actions that change the ledger: dismissing a coin report in the
// Audit inbox and granting time to a Time Credit.

func adminActionError(response http.ResponseWriter, err error) {
	status, message := http.StatusInternalServerError, "unable to record admin action"
	switch {
	case errors.Is(err, ledger.ErrGrantInvalid), errors.Is(err, ledger.ErrReviewInvalid):
		status, message = http.StatusBadRequest, err.Error()
	case errors.Is(err, ledger.ErrAlreadyResolved), errors.Is(err, ledger.ErrTimeCreditExpired):
		status, message = http.StatusConflict, err.Error()
	case errors.Is(err, sql.ErrNoRows):
		status, message = http.StatusNotFound, "Time Credit or audit row not found"
	}
	http.Error(response, message, status)
}

func (s *Server) resolveAudit(response http.ResponseWriter, request *http.Request) {
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	if err := request.ParseForm(); err != nil || request.PostForm.Get("action") != "dismiss" {
		http.Error(response, "choose dismissal or resolve with an admin grant", http.StatusBadRequest)
		return
	}
	auditID, err := strconv.ParseInt(request.PostForm.Get("audit_id"), 10, 64)
	if err != nil {
		http.Error(response, ledger.ErrReviewInvalid.Error(), http.StatusBadRequest)
		return
	}
	if err := s.ledger.DismissAudit(auditID, request.PostForm.Get("reason")); err != nil {
		adminActionError(response, err)
		return
	}
	response.Write([]byte("Audit dismissed. Resolution recorded. Return to /admin/time-credits?tab=audit to review."))
}

func (s *Server) adminGrant(response http.ResponseWriter, request *http.Request) {
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	if err := request.ParseForm(); err != nil {
		http.Error(response, "invalid grant", http.StatusBadRequest)
		return
	}
	seconds, err := strconv.ParseInt(request.PostForm.Get("seconds"), 10, 64)
	if err != nil {
		http.Error(response, ledger.ErrGrantInvalid.Error(), http.StatusBadRequest)
		return
	}
	var auditID int64
	if value := request.PostForm.Get("audit_id"); value != "" {
		auditID, err = strconv.ParseInt(value, 10, 64)
		if err != nil || auditID < 1 {
			http.Error(response, ledger.ErrReviewInvalid.Error(), http.StatusBadRequest)
			return
		}
	}
	tc, err := s.ledger.GrantTime(request.PostForm.Get("time_credit_id"), seconds, request.PostForm.Get("reason"), request.PostForm.Get("request_id"), auditID)
	if err != nil {
		adminActionError(response, err)
		return
	}
	router, err := s.router()
	if err == nil {
		defer router.Close()
		_, err = s.projectRouterTimeCredit(request.Context(), router, tc, routeros.Device{MAC: tc.MAC, IP: tc.IP}, false)
	}
	if err != nil {
		// The credit is already durable. Do not invite another grant: the
		// journal retries this absolute target on boot/poll.
		response.Write([]byte("Grant recorded. Router projection pending; do not grant again."))
		return
	}
	response.Write([]byte("Grant recorded and applied to the Router."))
}
