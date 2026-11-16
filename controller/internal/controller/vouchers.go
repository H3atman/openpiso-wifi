package controller

import (
	"database/sql"
	"encoding/csv"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

type voucherBatchView struct {
	Query           string
	QueryShort      bool
	Matches         []ledger.VoucherMatch
	MoreMatches     bool
	Batch           ledger.VoucherBatch
	Vouchers        []ledger.Voucher
	Sellers         []string
	Batches         []ledger.VoucherBatch
	Tiers           []ledger.RateTier
	Form            url.Values
	Error           string
	Generated       string
	MissingTier     string
	ValiditySeconds int64
	PauseLimit      int
}

func (s *Server) voucherBatchPage(response http.ResponseWriter, request *http.Request) {
	view := voucherBatchView{Form: request.URL.Query(), Error: request.URL.Query().Get("error"), Generated: request.URL.Query().Get("generated")}
	var err error
	if id := request.URL.Query().Get("batch"); id != "" {
		view.Batch, view.Vouchers, err = s.ledger.VoucherBatch(id)
		if errors.Is(err, sql.ErrNoRows) {
			http.NotFound(response, request)
			return
		}
	} else {
		view.Query = strings.TrimSpace(view.Form.Get("q"))
		view.QueryShort = view.Query != "" && utf8.RuneCountInString(view.Query) < 3
		if view.Query != "" && !view.QueryShort {
			view.Matches, view.MoreMatches, err = s.ledger.SearchVouchers(view.Query)
		}
		if err == nil {
			view.Batches, err = s.ledger.VoucherBatches()
		}
		if err == nil {
			view.Sellers, err = s.ledger.VoucherSellers()
		}
		if err == nil {
			view.Tiers, err = s.ledger.RateTiers()
		}
		if err == nil {
			view.ValiditySeconds, err = s.ledger.ValiditySeconds()
		}
		if err == nil {
			var pauses ledger.PauseSettings
			pauses, err = s.ledger.PauseSettings()
			view.PauseLimit = pauses.PauseLimit
		}
	}
	if err != nil {
		http.Error(response, "read Voucher Batches", http.StatusInternalServerError)
		return
	}
	if view.Batch.ID != "" {
		s.printVoucherBatch(response, view.Batch, view.Vouchers)
		return
	}
	// Keep a removed tier visible when redisplaying a refused request.
	if selection := view.Form.Get("tier"); selection != "" && selection != "custom" {
		view.MissingTier = selection
		for _, tier := range view.Tiers {
			if selection == strconv.FormatInt(tier.Pesos, 10) {
				view.MissingTier = ""
				break
			}
		}
	}
	s.renderPage(response, request, "vouchers", Page{Title: "Vouchers", Section: "Main", Active: "/admin/vouchers", Data: view})
}

// postedVoucherBatch accepts a Shop tier's price as its stable selection key.
// Custom limits are filled by the ledger in the mint transaction.
func postedVoucherBatch(request *http.Request, tiers []ledger.RateTier) (ledger.VoucherBatch, int, error) {
	var batch ledger.VoucherBatch
	if err := request.ParseForm(); err != nil {
		return batch, 0, errors.New("invalid Voucher Batch")
	}
	count, err := strconv.Atoi(request.PostForm.Get("count"))
	if err != nil || count < 1 || count > 1000 {
		return batch, 0, errors.New("choose 1–1000 Vouchers")
	}
	batch.Seller = request.PostForm.Get("seller")
	batch.CodePreset = request.PostForm.Get("preset")
	batch.CodePrefix = request.PostForm.Get("prefix")
	batch.CodeLength = 8
	if request.PostForm.Has("length") {
		batch.CodeLength, err = strconv.Atoi(request.PostForm.Get("length"))
		if err != nil || batch.CodeLength < 4 || batch.CodeLength > 10 {
			return batch, 0, errors.New("choose a code length from 4 to 10")
		}
	}
	if err := batch.ValidateCodeFormat(count); err != nil {
		return batch, 0, err
	}
	selection := request.PostForm.Get("tier")
	if selection != "" && selection != "custom" {
		for _, tier := range tiers {
			if selection == strconv.FormatInt(tier.Pesos, 10) {
				price := tier.Pesos
				batch.Seconds, batch.Pesos = tier.Seconds, &price
				batch.TierLabel = fmt.Sprintf("₱%d tier", price)
				batch.ValiditySeconds, batch.PauseLimit = tier.ValiditySeconds, tier.PauseLimit
				return batch, count, nil
			}
		}
		return batch, 0, errors.New("Rate tier no longer exists; choose a Shop Rate tier or Custom")
	}
	batch.Seconds, err = strconv.ParseInt(request.PostForm.Get("seconds"), 10, 64)
	if err != nil || batch.Seconds < 1 || batch.Seconds > 31536000 {
		return batch, 0, errors.New("choose 1–31536000 seconds")
	}
	price, err := strconv.ParseInt(request.PostForm.Get("pesos"), 10, 64)
	if err != nil || price < 0 || price > 1000000 {
		return batch, 0, errors.New("choose 0–1000000 pesos; Custom price is required")
	}
	batch.Pesos = &price
	return batch, count, nil
}

func (s *Server) createVoucherBatch(response http.ResponseWriter, request *http.Request) {
	tiers, err := s.ledger.RateTiers()
	if err != nil {
		http.Error(response, "read Shop Rate tiers", http.StatusInternalServerError)
		return
	}
	batch, count, err := postedVoucherBatch(request, tiers)
	if err != nil {
		values := url.Values{"error": {err.Error()}}
		for _, field := range []string{"tier", "seconds", "pesos", "count", "preset", "length", "prefix", "seller"} {
			if request.PostForm.Has(field) {
				values.Set(field, request.PostForm.Get(field))
			}
		}
		http.Redirect(response, request, "/admin/vouchers?"+values.Encode(), http.StatusSeeOther)
		return
	}
	batch, _, err = s.ledger.CreateVoucherBatch(batch, count)
	if err != nil {
		http.Error(response, "generate Voucher Batch", http.StatusInternalServerError)
		return
	}
	http.Redirect(response, request, "/admin/vouchers?generated="+url.QueryEscape(batch.ID), http.StatusSeeOther)
}

func (s *Server) deleteVoucherBatch(response http.ResponseWriter, request *http.Request) {
	err := s.ledger.DeleteVoucherBatch(request.URL.Query().Get("batch"))
	switch {
	case errors.Is(err, sql.ErrNoRows):
		http.NotFound(response, request)
	case errors.Is(err, ledger.ErrVoucherBatchRedeemed):
		http.Error(response, err.Error(), http.StatusConflict)
	case err != nil:
		http.Error(response, "delete Voucher Batch", http.StatusInternalServerError)
	default:
		http.Redirect(response, request, "/admin/vouchers", http.StatusSeeOther)
	}
}

func (s *Server) exportVoucherBatch(response http.ResponseWriter, request *http.Request) {
	batch, vouchers, err := s.ledger.VoucherBatch(request.URL.Query().Get("batch"))
	if errors.Is(err, sql.ErrNoRows) {
		http.NotFound(response, request)
		return
	}
	if err != nil {
		http.Error(response, "read Voucher Batch", http.StatusInternalServerError)
		return
	}
	response.Header().Set("Content-Type", "text/csv; charset=utf-8")
	response.Header().Set("Content-Disposition", fmt.Sprintf(`attachment; filename="vouchers-%d.csv"`, batch.Number))
	writer := csv.NewWriter(response)
	writer.Write([]string{"code", "time", "pesos", "redeemed_at"})
	price := ""
	if batch.Pesos != nil {
		price = strconv.FormatInt(*batch.Pesos, 10)
	}
	for _, voucher := range vouchers {
		writer.Write([]string{voucher.Code, strconv.FormatInt(batch.Seconds, 10), price, voucher.RedeemedAt})
	}
	writer.Flush()
}

func (s *Server) redeemVoucher(response http.ResponseWriter, request *http.Request) {
	// Serialize the ledger -> Router projection sequence as well as rate checks.
	// The ledger independently owns single-use atomicity; Router writes are never
	// made from inside a database transaction.
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	unused := &portalMessage{"error", "unavailable", "Service temporarily unavailable. Voucher not used."}
	router, err := s.router()
	if err != nil {
		s.renderPortal(response, request, nil, routeros.Device{}, http.StatusServiceUnavailable, unused)
		return
	}
	defer router.Close()
	device, err := router.DeviceAt(request.Context(), remoteAddress(request))
	if errors.Is(err, routeros.ErrDeviceNotReady) {
		s.renderPortal(response, request, nil, device, http.StatusServiceUnavailable,
			&portalMessage{"warning", "device-joining", "Your Device is still joining the WiFi. Try again in a few seconds; Voucher not used."})
		return
	}
	if err != nil {
		s.renderPortal(response, request, nil, device, http.StatusForbidden,
			&portalMessage{"error", "not-on-wifi", "Device not found on the Router. Connect through the WiFi; Voucher not used."})
		return
	}
	failure := s.voucherFailures[device.MAC]
	now := time.Now()
	if now.Before(failure.blocked) {
		s.renderPortal(response, request, router, device, http.StatusTooManyRequests,
			&portalMessage{"error", "too-many-attempts", "Too many Voucher attempts. Try again in five minutes."})
		return
	}
	if !failure.blocked.IsZero() {
		delete(s.voucherFailures, device.MAC)
		failure = loginFailures{}
	}
	fail := func(status int, message *portalMessage) {
		failure.count++
		if failure.count >= 5 {
			failure.blocked = now.Add(5 * time.Minute)
		}
		s.voucherFailures[device.MAC] = failure
		s.renderPortal(response, request, router, device, status, message)
	}
	invalid := &portalMessage{"error", "voucher-invalid", "Voucher invalid. Enter the letters and digits printed on it."}
	if err := request.ParseForm(); err != nil {
		fail(http.StatusBadRequest, invalid)
		return
	}
	code := strings.TrimSpace(request.PostForm.Get("code"))
	if len(code) < 4 || len(code) > 16 || strings.Trim(code, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789") != "" {
		fail(http.StatusBadRequest, invalid)
		return
	}
	existing, err := s.currentTimeCredit(request, device)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, unused)
		return
	}
	tc, err := s.ledger.RedeemVoucher(code, device.MAC, device.IP, existing.ID)
	if errors.Is(err, ledger.ErrVoucherUsed) {
		fail(http.StatusConflict, &portalMessage{"error", "voucher-used", err.Error()})
		return
	}
	if errors.Is(err, ledger.ErrVoucherUnknown) {
		fail(http.StatusBadRequest, &portalMessage{"error", "voucher-unknown", err.Error()})
		return
	}
	if err != nil {
		s.renderPortal(response, request, router, device, http.StatusInternalServerError,
			&portalMessage{"error", "voucher-failed", "Unable to redeem Voucher. Please try again."})
		return
	}
	delete(s.voucherFailures, device.MAC)
	// Failed Voucher attempts must never move a Time Credit or remove access.
	s.finishTopUp(response, request, router, device, tc)
}

// finishTopUp answers a Portal Top-up (a Voucher or Points) that the ledger
// has committed. A purchase is not consent to Resume: it applies the absolute
// Router target but keeps a tapped Pause, and reports the credited time as
// success. Otherwise it binds the Time Credit here and logs the Device in.
func (s *Server) finishTopUp(response http.ResponseWriter, request *http.Request, router *routeros.Client, device routeros.Device, tc ledger.TimeCredit) {
	pause, err := s.ledger.PauseState(tc.ID)
	if err == nil && pause.Tapped {
		tc, err = s.rebind(request, router, tc, device, "cookie")
		if err == nil {
			_, err = s.projectRouterTimeCredit(request.Context(), router, tc, device, false)
		}
		if err == nil {
			s.renderPortal(response, request, router, device, http.StatusOK,
				&portalMessage{"success", "time-added-paused", "Time added. Resume to connect."})
			return
		}
	}
	if err != nil {
		s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, msgConnectFailed)
		return
	}
	s.bindAndProject(response, request, router, device, tc, "cookie", false)
}
