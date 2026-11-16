package controller

import (
	"database/sql"
	"errors"
	"net/http"
	"strconv"
	"strings"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

func (s *Server) doneCoinWindow(response http.ResponseWriter, request *http.Request) {
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
	noWindow := &portalMessage{"error", "no-coin-window", "No Coin Window for this Device."}
	tc, err := s.currentTimeCredit(request, device)
	if err != nil {
		s.renderPortal(response, request, router, device, http.StatusForbidden, noWindow)
		return
	}
	if err := request.ParseForm(); err != nil {
		s.renderPortal(response, request, router, device, http.StatusBadRequest, msgCoinWindowInvalid)
		return
	}
	id, err := strconv.ParseUint(request.PostForm.Get("window_id"), 10, 32)
	if err != nil || id == 0 {
		s.renderPortal(response, request, router, device, http.StatusBadRequest, msgCoinWindowInvalid)
		return
	}
	if err := s.ledger.CloseCoinWindow(uint32(id), tc.ID); err != nil {
		s.renderPortal(response, request, router, device, http.StatusForbidden, noWindow)
		return
	}
	s.wakeReconciler()
	s.renderPortal(response, request, router, device, http.StatusOK,
		&portalMessage{"success", "coins-closing", "Closing Coin Window; waiting for the Vendo's final coins."})
}

var msgCoinWindowInvalid = &portalMessage{"error", "coin-window-invalid", "Invalid Coin Window."}

func (s *Server) coinSalesPage(response http.ResponseWriter, request *http.Request) {
	sales, err := s.ledger.CoinSales()
	if err != nil {
		http.Error(response, "read coin sales", http.StatusInternalServerError)
		return
	}
	windows, err := s.ledger.CoinWindows()
	if err != nil {
		http.Error(response, "read Coin Windows", http.StatusInternalServerError)
		return
	}
	s.mu.Lock()
	gaps := s.reconciliation.CoinGaps
	s.mu.Unlock()
	s.renderPage(response, request, "coins", Page{Title: "Coin sales", Section: "Coins", Active: "/admin/coins", Data: struct {
		Sales   []ledger.CoinSale
		Windows []ledger.CoinWindow
		Gaps    []ledger.CoinGap
	}{sales, windows, gaps}})
}

type rateTierRow struct {
	ledger.RateTier
	Validity string // as typed in the form: seconds or "no limit"
	Label    string // as shown in the table, e.g. "1 day" or "No limit"
	Points   string // as typed and shown, e.g. "0.1"
}

// ratesPage is Shop › Rate tiers: the Shop's default tiers in the shared editor,
// with the paired Vendos that use them and those with Custom tiers.
func (s *Server) ratesPage(response http.ResponseWriter, request *http.Request) {
	tiers, err := s.ledger.RateTiers()
	if err != nil {
		http.Error(response, "read coin rates", http.StatusInternalServerError)
		return
	}
	s.renderShop(response, request, shopPage{
		Editor: tierEditor{Kind: "rates", Action: "/admin/rates", Rates: rateTierRows(tiers),
			Hint: "Customers get the highest tier their coins reach. A change applies to each Vendo using Shop tiers from its next Coin Window.", SaveLabel: "Save Shop tiers"},
		Title: "Rate tiers", Scope: "Default Rate tiers for this Shop. Every Vendo uses them unless it has its own.",
		ShopSet: "Shop tiers", CustomSet: "Custom tiers", NextUse: "Coin Window", OwnSet: "tiers", Unit: "tier", EditLabel: "Edit tiers",
	}, func(vendo ledger.Vendo) (int, bool) { return len(vendo.RateTiers), vendo.RateTiers != nil })
}

// rateTierRows formats tiers for the rates form and table, Shop or Vendo.
func rateTierRows(tiers []ledger.RateTier) []rateTierRow {
	rows := make([]rateTierRow, len(tiers))
	for i, tier := range tiers {
		rows[i] = rateTierRow{tier, strconv.FormatInt(tier.ValiditySeconds, 10), validityLabel(tier.ValiditySeconds), pointsText(tier.PointsMilli)}
		if tier.ValiditySeconds == ledger.MaxValiditySeconds {
			rows[i].Validity = "no limit"
		}
	}
	return rows
}

// saveRates stores the Shop's Rate tiers from the structured editor's rows or the
// legacy tiers textarea; the editor's hidden action=save is not read.
func (s *Server) saveRates(response http.ResponseWriter, request *http.Request) {
	if err := postedVendoTierFields(request, false); err != nil {
		http.Error(response, err.Error(), http.StatusBadRequest)
		return
	}
	tiers, status, err := s.postedRateTiers(request)
	if err != nil {
		http.Error(response, err.Error(), status)
		return
	}
	if err := s.ledger.SaveRateTiers(tiers); err != nil {
		http.Error(response, err.Error(), http.StatusBadRequest)
		return
	}
	http.Redirect(response, request, "/admin/rates", http.StatusSeeOther)
}

// postedRateTiers reads the form's tiers field, Shop or Vendo, one tier per
// line: pesos=seconds,validity,pause_limit[,points], where validity is seconds
// or "no limit" and points a decimal such as 0.1 (none when omitted). A bare pesos=seconds takes the Shop's Validity and Pause Limit settings. The
// ledger checks the tiers themselves when saving. On error, status is the
// HTTP status to answer with.
func (s *Server) postedRateTiers(request *http.Request) (tiers []ledger.RateTier, status int, err error) {
	if err := request.ParseForm(); err != nil {
		return nil, http.StatusBadRequest, errors.New("invalid coin rates")
	}
	siteValidity, err := s.ledger.ValiditySeconds()
	if err != nil {
		return nil, http.StatusInternalServerError, errors.New("read Validity settings")
	}
	sitePauses, err := s.ledger.PauseSettings()
	if err != nil {
		return nil, http.StatusInternalServerError, errors.New("read Pause settings")
	}
	for _, line := range strings.Split(strings.TrimSpace(request.PostForm.Get("tiers")), "\n") {
		pesos, rest, found := strings.Cut(strings.TrimSpace(line), "=")
		fields := strings.Split(rest, ",")
		if !found || (len(fields) != 1 && len(fields) != 3 && len(fields) != 4) {
			return nil, http.StatusBadRequest, errors.New("enter pesos=seconds,validity,pause limit,points, one tier per line")
		}
		tier := ledger.RateTier{ValiditySeconds: siteValidity, PauseLimit: sitePauses.PauseLimit}
		var pesoErr, secondsErr, validityErr, limitErr, pointsErr error
		tier.Pesos, pesoErr = strconv.ParseInt(strings.TrimSpace(pesos), 10, 64)
		tier.Seconds, secondsErr = strconv.ParseInt(strings.TrimSpace(fields[0]), 10, 64)
		if len(fields) >= 3 {
			if validity := strings.TrimSpace(fields[1]); strings.EqualFold(validity, "no limit") {
				tier.ValiditySeconds = ledger.MaxValiditySeconds
			} else {
				tier.ValiditySeconds, validityErr = strconv.ParseInt(validity, 10, 64)
			}
			tier.PauseLimit, limitErr = strconv.Atoi(strings.TrimSpace(fields[2]))
		}
		if len(fields) == 4 {
			tier.PointsMilli, pointsErr = parsePoints(fields[3])
		}
		if errors.Join(pesoErr, secondsErr, validityErr, limitErr, pointsErr) != nil {
			return nil, http.StatusBadRequest, errors.New("enter whole pesos, seconds, Validity seconds (or no limit), Pause Limit and Points (up to three decimals)")
		}
		tiers = append(tiers, tier)
	}
	return tiers, http.StatusOK, nil
}

func (s *Server) openCoinWindow(response http.ResponseWriter, request *http.Request) {
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	if s.coinBootPending {
		s.renderPortal(response, request, nil, routeros.Device{}, http.StatusServiceUnavailable,
			&portalMessage{"error", "coins-recovering", "Coin Windows are recovering after a restart. Try again in a few seconds."})
		return
	}
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
	choose := &portalMessage{"error", "vendo-choose", "Choose a Vendo serving this network."}
	if err := request.ParseForm(); err != nil {
		s.renderPortal(response, request, router, device, http.StatusBadRequest, choose)
		return
	}
	matches, outOfService, err := s.matchingVendos(device.HotspotServer)
	if err != nil {
		s.renderPortal(response, request, router, device, http.StatusInternalServerError, msgUnavailable)
		return
	}
	if outOfService {
		s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, msgOutOfService)
		return
	}
	if len(matches) == 0 {
		s.renderPortal(response, request, router, device, http.StatusBadRequest, &portalMessage{"error", "no-vendo", "No Vendo serves this network."})
		return
	}
	var id uint32
	if posted := request.PostForm.Get("vendo_id"); posted != "" {
		parsed, parseErr := strconv.ParseUint(posted, 10, 32)
		if parseErr == nil {
			for _, vendo := range matches {
				if vendo.ID == uint32(parsed) {
					id = vendo.ID
				}
			}
		}
	} else if len(matches) == 1 {
		id = matches[0].ID
	}
	if id == 0 {
		s.renderPortal(response, request, router, device, http.StatusBadRequest, choose)
		return
	}
	tc, err := s.currentTimeCredit(request, device)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		s.renderPortal(response, request, router, device, http.StatusInternalServerError, msgUnavailable)
		return
	}
	tc, err = s.ledger.OpenCoinWindow(uint32(id), device.MAC, device.IP, tc.ID)
	if errors.Is(err, ledger.ErrVendoDeactivated) {
		s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, msgOutOfService)
		return
	}
	if errors.Is(err, ledger.ErrVendoBusy) {
		s.renderPortal(response, request, router, device, http.StatusConflict, &portalMessage{"error", "vendo-busy", "Vendo busy. Try again when it is free."})
		return
	}
	if err != nil {
		s.renderPortal(response, request, router, device, http.StatusBadRequest, &portalMessage{"error", "coin-window-failed", "Unable to open a Coin Window."})
		return
	}
	// A busy Vendo must not move a Time Credit. After reserving it, use the
	// same binding/displacement flow as other ownership-proven portal actions.
	tc, err = s.rebind(request, router, tc, device, "cookie")
	if err != nil {
		s.renderPortal(response, request, router, device, http.StatusServiceUnavailable,
			&portalMessage{"error", "coin-window-bind-failed", "Coin Window saved; try Connect again to finish binding this Device."})
		return
	}
	if err := s.setTimeCreditCookie(response, request, tc); err != nil {
		s.renderPortal(response, request, router, device, http.StatusInternalServerError, msgUnavailable)
		return
	}
	s.renderPortal(response, request, router, device, http.StatusOK, &portalMessage{"success", "coins-insert", "Insert coins, then tap Done."})
}
