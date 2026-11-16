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

// pointsText shows Points kept in thousandths as a decimal, e.g. 1100 → "1.1".
func pointsText(milli int64) string {
	text := strconv.FormatInt(milli/1000, 10)
	if fraction := strings.TrimRight(strconv.FormatInt(1000+milli%1000, 10)[1:], "0"); fraction != "" {
		text += "." + fraction
	}
	return text
}

// parsePoints reads a decimal with at most three places, e.g. "0.1" → 100,
// without going through a float.
func parsePoints(text string) (int64, error) {
	whole, fraction, _ := strings.Cut(strings.TrimSpace(text), ".")
	if whole == "" || len(fraction) > 3 || strings.Trim(whole+fraction, "0123456789") != "" {
		return 0, errors.New("enter Points as a number with up to three decimals")
	}
	return strconv.ParseInt(whole+fraction+strings.Repeat("0", 3-len(fraction)), 10, 64)
}

func msgPointsKept(milli int64) *portalMessage {
	return &portalMessage{"warning", "points-kept",
		"Your time expired, but your " + pointsText(milli) + " Points are kept. Insert coins or redeem Points"}
}

var msgResumeCodeExpired = &portalMessage{"error", "resume-code-expired", "This code has expired"}

var msgPointsRowUnknown = &portalMessage{"error", "points-row-unknown", ledger.ErrPointsRowUnknown.Error() + "."}

type pointsTierRow struct {
	ledger.PointsTier
	Points   string // as typed and shown, e.g. "0.5"
	Validity string // as typed in the form: seconds or "no limit"
	Label    string // as shown in the table, e.g. "1 day" or "No limit"
}

// pointsPage is Shop › Points table: the Shop's default Points table in the
// shared editor, with the paired Vendos that use it and those with Custom Points.
func (s *Server) pointsPage(response http.ResponseWriter, request *http.Request) {
	tiers, err := s.ledger.PointsTiers()
	if err != nil {
		http.Error(response, "read the Points table", http.StatusInternalServerError)
		return
	}
	s.renderShop(response, request, shopPage{
		Editor: tierEditor{Kind: "points", Action: "/admin/points", Points: pointsTierRows(tiers),
			Hint: "An empty table means customers cannot redeem Points. A change applies to each Vendo using the Shop table from its next redemption.", SaveLabel: "Save Shop Points table"},
		Title: "Points table", Scope: "Default Points table for this Shop. Every Vendo uses it unless it has its own.",
		ShopSet: "the Shop table", CustomSet: "Custom Points", NextUse: "redemption", OwnSet: "table", Unit: "row", EditLabel: "Edit table",
	}, func(vendo ledger.Vendo) (int, bool) { return len(vendo.PointsTiers), vendo.PointsTiers != nil })
}

// pointsTierRows formats a Points table for its form and list, Shop or Vendo.
func pointsTierRows(tiers []ledger.PointsTier) []pointsTierRow {
	rows := make([]pointsTierRow, len(tiers))
	for i, tier := range tiers {
		rows[i] = pointsTierRow{tier, pointsText(tier.PointsMilli), strconv.FormatInt(tier.ValiditySeconds, 10), validityLabel(tier.ValiditySeconds)}
		if tier.ValiditySeconds == ledger.MaxValiditySeconds {
			rows[i].Validity = "no limit"
		}
	}
	return rows
}

// savePoints stores the Shop's Points table from the structured editor's rows or
// the legacy tiers textarea; the editor's hidden action=save is not read.
func (s *Server) savePoints(response http.ResponseWriter, request *http.Request) {
	if err := postedVendoTierFields(request, true); err != nil {
		http.Error(response, err.Error(), http.StatusBadRequest)
		return
	}
	tiers, err := postedPointsTiers(request)
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadRequest)
		return
	}
	if err := s.ledger.SavePointsTiers(tiers); err != nil {
		http.Error(response, err.Error(), http.StatusBadRequest)
		return
	}
	http.Redirect(response, request, "/admin/points", http.StatusSeeOther)
}

// postedPointsTiers reads the form's tiers field, Shop or Vendo, one row per
// line: points=seconds,validity,pause_limit, where points is a decimal such as
// 0.5 and validity is seconds or "no limit". An empty field is an empty table.
// The ledger checks the rows themselves when saving.
func postedPointsTiers(request *http.Request) ([]ledger.PointsTier, error) {
	if err := request.ParseForm(); err != nil {
		return nil, errors.New("invalid Points table")
	}
	tiers := []ledger.PointsTier{}
	for _, line := range strings.Split(request.PostForm.Get("tiers"), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		points, rest, found := strings.Cut(strings.TrimSpace(line), "=")
		fields := strings.Split(rest, ",")
		if !found || len(fields) != 3 {
			return nil, errors.New("enter points=seconds,validity,pause limit, one row per line")
		}
		var tier ledger.PointsTier
		var pointsErr, secondsErr, validityErr, limitErr error
		tier.PointsMilli, pointsErr = parsePoints(points)
		tier.Seconds, secondsErr = strconv.ParseInt(strings.TrimSpace(fields[0]), 10, 64)
		if validity := strings.TrimSpace(fields[1]); strings.EqualFold(validity, "no limit") {
			tier.ValiditySeconds = ledger.MaxValiditySeconds
		} else {
			tier.ValiditySeconds, validityErr = strconv.ParseInt(validity, 10, 64)
		}
		tier.PauseLimit, limitErr = strconv.Atoi(strings.TrimSpace(fields[2]))
		if errors.Join(pointsErr, secondsErr, validityErr, limitErr) != nil {
			return nil, errors.New("enter Points (up to three decimals), whole seconds, Validity seconds (or no limit) and Pause Limit")
		}
		tiers = append(tiers, tier)
	}
	return tiers, nil
}

// servingPoints is the Points table a Device redeems from (ADR 0006): that of
// the first Vendo serving its HotSpot server, as with rates, or the Shop's
// when none does or that Vendo uses the Shop's.
func servingPoints(vendos []ledger.Vendo, site []ledger.PointsTier) []ledger.PointsTier {
	if len(vendos) > 0 && vendos[0].PointsTiers != nil {
		return vendos[0].PointsTiers
	}
	return site
}

// redeemPoints answers POST /portal/api/points/redeem: the row of the Points
// table with this points_milli and seconds becomes a Top-up of the Device's
// Time Credit, even an expired one still holding Points (ADR 0006). The row
// must be in the table the Device is offered: its serving Vendo's, else the Shop's.
func (s *Server) redeemPoints(response http.ResponseWriter, request *http.Request) {
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
	if err := request.ParseForm(); err != nil {
		s.renderPortal(response, request, router, device, http.StatusBadRequest, msgPointsRowUnknown)
		return
	}
	pointsMilli, pointsErr := strconv.ParseInt(request.PostForm.Get("points_milli"), 10, 64)
	seconds, secondsErr := strconv.ParseInt(request.PostForm.Get("seconds"), 10, 64)
	if pointsErr != nil || secondsErr != nil {
		s.renderPortal(response, request, router, device, http.StatusBadRequest, msgPointsRowUnknown)
		return
	}
	held, err := s.currentTimeCredit(request, device)
	if errors.Is(err, sql.ErrNoRows) {
		s.renderPortal(response, request, router, device, http.StatusConflict, pointsTooLow(0, pointsMilli))
		return
	}
	if err != nil {
		s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, msgUnavailable)
		return
	}
	vendos, _, err := s.matchingVendos(device.HotspotServer)
	site, siteErr := s.ledger.PointsTiers()
	if err = errors.Join(err, siteErr); err != nil {
		s.renderPortal(response, request, router, device, http.StatusServiceUnavailable, msgUnavailable)
		return
	}
	tc, err := s.ledger.RedeemPoints(held.ID, device.MAC, device.IP, servingPoints(vendos, site), pointsMilli, seconds)
	switch {
	case errors.Is(err, ledger.ErrPointsRowUnknown):
		s.renderPortal(response, request, router, device, http.StatusBadRequest, msgPointsRowUnknown)
	case errors.Is(err, ledger.ErrPointsTooLow):
		s.renderPortal(response, request, router, device, http.StatusConflict, pointsTooLow(held.PointsMilli, pointsMilli))
	case err != nil:
		s.renderPortal(response, request, router, device, http.StatusInternalServerError,
			&portalMessage{"error", "points-failed", "Unable to redeem Points. Please try again."})
	default:
		s.finishTopUp(response, request, router, device, tc)
	}
}

func pointsTooLow(balance, cost int64) *portalMessage {
	return &portalMessage{"error", "points-too-low",
		"Not enough Points: you have " + pointsText(balance) + ", and this costs " + pointsText(cost) + "."}
}
