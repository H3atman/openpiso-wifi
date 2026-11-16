package controller

import (
	"context"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
	"github.com/H3atman/openpiso-wifi/controller/internal/vendoprotocol"
)

// vendoOnline is how recent the last authenticated report must be for a Vendo
// to read Online: five missed 2 s heartbeats (§0 #20 of the Vendo List design).
const vendoOnline = 10 * time.Second

// vendoRow is one Vendo List row: the ledger Vendo plus what the page derives.
type vendoRow struct {
	ledger.Vendo
	Hostname    string
	StatusLabel string   // Online, Offline, Waiting for first contact, Deactivated, Unpairing or Unpaired
	LastSeen    string   // UTC, empty before the first authenticated report
	Servers     []string // HotSpot server choices: the Router's, plus the current one if it is gone
	Profiles    []string // HotSpot profile choices: the Router's, plus the current one if it is gone
	ProfileGone bool     // the Router no longer has HotspotProfile; its copies keep working, and Details warns
	RouterDown  bool     // the server and profile choices could not be loaded, so the selects are disabled
	Error       string   // why the row's last action failed; only the htmx row swap shows it
	Sales       ledger.VendoSales
	CoinsOutAt  string          // UTC date and time of the last Coins Out, empty if none
	UpSince     string          // UTC boot time while the Vendo is reporting, else empty
	Rates       []rateTierRow   // the Rates tab: the Vendo's own tiers, or the site's while RateTiers is nil
	Points      []pointsTierRow // the Points tab: the Vendo's own table, or the site's while PointsTiers is nil
	OwnPoints   bool            // the Vendo has its own Points table, which may be empty
	// OnlineDevices counts the Devices online on this Vendo's HotSpot server,
	// so Vendos sharing a server show the same number.
	OnlineDevices int
}

func newVendoRow(vendo ledger.Vendo, sales ledger.VendoSales, hostname string, servers, profiles []string, siteRates []ledger.RateTier, sitePoints []ledger.PointsTier, routerDown bool, onlineDevices int, now time.Time) vendoRow {
	row := vendoRow{Vendo: vendo, Sales: sales, Hostname: hostname, Servers: servers, Profiles: profiles, RouterDown: routerDown, OnlineDevices: onlineDevices}
	row.Rates = rateTierRows(siteRates)
	if vendo.RateTiers != nil {
		row.Rates = rateTierRows(vendo.RateTiers)
	}
	row.OwnPoints = vendo.PointsTiers != nil
	row.Points = pointsTierRows(servingPoints([]ledger.Vendo{vendo}, sitePoints))
	if at, err := time.Parse(time.RFC3339Nano, sales.LastCoinsOut.RecordedAt); err == nil {
		row.CoinsOutAt = at.UTC().Format("2006-01-02 15:04 UTC")
	}
	if vendo.HotspotServer != "" && !slices.Contains(servers, vendo.HotspotServer) {
		row.Servers = append(slices.Clip(servers), vendo.HotspotServer)
	}
	if vendo.HotspotProfile != "" && !slices.Contains(profiles, vendo.HotspotProfile) {
		row.Profiles = append(slices.Clip(profiles), vendo.HotspotProfile)
		row.ProfileGone = !routerDown
	}
	if seen, err := time.Parse(time.RFC3339Nano, vendo.LastContact); err == nil {
		row.LastSeen = seen.UTC().Format("2006-01-02 15:04:05 UTC")
	}
	reporting := vendoReporting(vendo, now)
	if booted, err := time.Parse(time.RFC3339Nano, vendo.BootedAt); err == nil && reporting && vendo.Status == "paired" {
		row.UpSince = booted.UTC().Format("2006-01-02 15:04:05 UTC")
	}
	row.StatusLabel = vendoStatus(vendo, reporting)
	return row
}

// vendoReporting reports whether the Vendo's last authenticated report is recent enough to read Online.
func vendoReporting(vendo ledger.Vendo, now time.Time) bool {
	seen, err := time.Parse(time.RFC3339Nano, vendo.LastContact)
	return err == nil && now.Sub(seen) < vendoOnline
}

// vendoStatus is the Vendo's status label on the Vendo List and Shop pages.
func vendoStatus(vendo ledger.Vendo, reporting bool) string {
	switch {
	case vendo.Status == "provisioned":
		return "Waiting for first contact"
	case vendo.Status == "unpairing":
		return "Unpairing"
	case vendo.Status == "unpaired":
		return "Unpaired"
	case !vendo.Active:
		return "Deactivated"
	case reporting:
		return "Online"
	default:
		return "Offline"
	}
}

// vendoRows reads every Vendo List row. routerDown means the Router's HotSpot
// servers, profiles, hostnames or active sessions could not be read, so rows show without them.
func (s *Server) vendoRows(ctx context.Context) (rows []vendoRow, routerDown bool, err error) {
	vendos, err := s.ledger.Vendos()
	if err != nil {
		return nil, false, err
	}
	sales, err := s.ledger.VendoSales()
	if err != nil {
		return nil, false, err
	}
	siteRates, err := s.ledger.RateTiers()
	if err != nil {
		return nil, false, err
	}
	sitePoints, err := s.ledger.PointsTiers()
	if err != nil {
		return nil, false, err
	}
	var servers, profiles []string
	var hostnames map[string]string
	online := map[string]int{} // Devices online per HotSpot server
	client, err := s.router()
	if err == nil {
		defer client.Close()
		servers, err = client.HotspotServers(ctx)
		if err == nil {
			profiles, err = client.HotspotProfiles(ctx)
		}
		if err == nil {
			hostnames, err = client.VendoHostnames(ctx)
		}
		if err == nil {
			var sessions []routeros.ActiveSession
			sessions, err = client.ActiveSessions(ctx)
			for _, session := range sessions {
				online[session.Server]++
			}
		}
	}
	routerDown = err != nil
	now := time.Now()
	rows = make([]vendoRow, 0, len(vendos))
	for _, vendo := range vendos {
		rows = append(rows, newVendoRow(vendo, sales[vendo.ID], hostnames[vendo.MAC], servers, profiles, siteRates, sitePoints, routerDown, online[vendo.HotspotServer], now))
	}
	return rows, routerDown, nil
}

func (s *Server) vendos(response http.ResponseWriter, request *http.Request) {
	rows, routerDown, err := s.vendoRows(request.Context())
	if err != nil {
		http.Error(response, "read Vendos", http.StatusInternalServerError)
		return
	}
	message := ""
	if routerDown {
		message = "Router unavailable; HotSpot server and profile choices cannot be loaded."
	}
	s.renderPage(response, request, "vendos", Page{Title: "Vendo List", Section: "Main", Active: "/admin/vendos", Data: struct {
		Vendos  []vendoRow
		Message string
	}{rows, message}})
}

// vendoEdit saves the Edit dialog: Name and Location always, and the HotSpot
// server and profile when the form carries them. The dialog disables those
// selects while the Router is unreachable, so the browser leaves them out and
// they are kept. Saving a profile re-copies it into the Controller's pair.
func (s *Server) vendoEdit(response http.ResponseWriter, request *http.Request) {
	if err := request.ParseForm(); err != nil {
		http.Error(response, "invalid Vendo details", http.StatusBadRequest)
		return
	}
	id, err := strconv.ParseUint(request.PostForm.Get("id"), 10, 32)
	if err != nil || id == 0 {
		http.Error(response, "invalid Vendo ID", http.StatusBadRequest)
		return
	}
	name := strings.TrimSpace(request.PostForm.Get("name"))
	location := strings.TrimSpace(request.PostForm.Get("location"))
	for _, field := range []struct{ label, value string }{{"name", name}, {"location", location}} {
		if !utf8.ValidString(field.value) || utf8.RuneCountInString(field.value) > 80 {
			http.Error(response, "Vendo "+field.label+" must be valid text of at most 80 characters", http.StatusBadRequest)
			return
		}
	}
	s.vendoMu.Lock()
	defer s.vendoMu.Unlock()
	vendo, err := s.ledger.Vendo(uint32(id))
	if errors.Is(err, ledger.ErrVendoNotFound) || (err == nil && vendo.Status == "unpaired") {
		http.Error(response, "unknown Vendo", http.StatusNotFound)
		return
	}
	if err != nil {
		http.Error(response, "read Vendo", http.StatusInternalServerError)
		return
	}
	server := vendo.HotspotServer
	if request.PostForm.Has("hotspot_server") {
		server = request.PostForm.Get("hotspot_server")
		if server != vendo.HotspotServer && vendo.Status != "paired" {
			http.Error(response, "the HotSpot server can change only on a paired Vendo", http.StatusBadRequest)
			return
		}
	}
	profile := vendo.HotspotProfile
	if request.PostForm.Has("hotspot_profile") {
		profile = request.PostForm.Get("hotspot_profile")
	}
	newServer := server != vendo.HotspotServer && server != ""
	copyProfile := request.PostForm.Has("hotspot_profile") && profile != ""
	if newServer || copyProfile {
		client, err := s.router()
		if err != nil {
			http.Error(response, "Router unavailable", http.StatusServiceUnavailable)
			return
		}
		defer client.Close()
		if newServer {
			servers, err := client.HotspotServers(request.Context())
			if err != nil {
				http.Error(response, "read HotSpot servers", http.StatusBadGateway)
				return
			}
			if !slices.Contains(servers, server) {
				http.Error(response, "choose a HotSpot server from the Router", http.StatusBadRequest)
				return
			}
		}
		if copyProfile {
			// An unchanged profile still saves after it is deleted on the Router.
			if profile != vendo.HotspotProfile {
				profiles, err := client.HotspotProfiles(request.Context())
				if err != nil {
					http.Error(response, "read HotSpot profiles", http.StatusBadGateway)
					return
				}
				if !slices.Contains(profiles, profile) {
					http.Error(response, "choose a HotSpot profile from the Router", http.StatusBadRequest)
					return
				}
			}
			settings, err := s.ledger.PauseSettings()
			if err != nil {
				http.Error(response, "read Pause settings", http.StatusInternalServerError)
				return
			}
			if err := client.EnsureProfileCopies(request.Context(), []string{profile}, settings.IdleSeconds); err != nil {
				http.Error(response, "copy HotSpot profile to the Router", http.StatusBadGateway)
				return
			}
		}
	}
	if err := s.ledger.EditVendo(vendo.ID, name, location, server, profile); err != nil {
		http.Error(response, "save Vendo", http.StatusInternalServerError)
		return
	}
	http.Redirect(response, request, "/admin/vendos?vendo="+strconv.FormatUint(rowVendoID(request), 10), http.StatusSeeOther)
}

// vendoRates saves the Edit dialog's Rates tab: action=customize copies the
// site Rate tiers into the Vendo, save sets its tiers (the /admin/rates form
// and rules), and site drops them so its next Coin Window uses the site's.
func (s *Server) vendoRates(response http.ResponseWriter, request *http.Request) {
	var tiers []ledger.RateTier
	s.vendoTableTab(response, request, "Save tiers", "Use Shop tiers", func() (status int, err error) {
		if err := postedVendoTierFields(request, false); err != nil {
			return http.StatusBadRequest, err
		}
		tiers, status, err = s.postedRateTiers(request)
		return status, err
	}, func(id uint32, action string) error {
		switch action {
		case "customize":
			return s.ledger.CustomizeVendoRates(id)
		case "site":
			return s.ledger.UseSiteRates(id)
		}
		return s.ledger.SaveVendoRateTiers(id, tiers)
	})
}

// vendoPoints saves the Edit dialog's Points tab like vendoRates:
// action=customize copies the site Points table into the Vendo, save sets its
// table (the /admin/points form and rules), and site drops it so its Devices
// redeem from the site's.
func (s *Server) vendoPoints(response http.ResponseWriter, request *http.Request) {
	var tiers []ledger.PointsTier
	s.vendoTableTab(response, request, "Save Points table", "Use Shop Points table", func() (int, error) {
		if err := postedVendoTierFields(request, true); err != nil {
			return http.StatusBadRequest, err
		}
		var err error
		tiers, err = postedPointsTiers(request)
		return http.StatusBadRequest, err
	}, func(id uint32, action string) error {
		switch action {
		case "customize":
			return s.ledger.CustomizeVendoPoints(id)
		case "site":
			return s.ledger.UseSitePoints(id)
		}
		return s.ledger.SaveVendoPointsTiers(id, tiers)
	})
}

// vendoTableTab is the request side shared by the Rates and Points tabs. It
// checks the Vendo ID and action (customize, save or site; saveLabel and
// resetLabel name the tab's Save and reset buttons), runs parse for a save, then
// under vendoMu runs persist for a Vendo that is not unpaired and redirects to
// the Vendo List. A failed save is the Operator's input, so its error is shown
// as a 400.
func (s *Server) vendoTableTab(response http.ResponseWriter, request *http.Request, saveLabel, resetLabel string, parse func() (int, error), persist func(id uint32, action string) error) {
	id := rowVendoID(request)
	if id == 0 {
		http.Error(response, "invalid Vendo ID", http.StatusBadRequest)
		return
	}
	action := request.PostForm.Get("action")
	switch action {
	case "customize", "site":
	case "save":
		if status, err := parse(); err != nil {
			http.Error(response, err.Error(), status)
			return
		}
	default:
		http.Error(response, "choose Customize for this Vendo, "+saveLabel+" or "+resetLabel, http.StatusBadRequest)
		return
	}
	s.vendoMu.Lock()
	defer s.vendoMu.Unlock()
	vendo, err := s.ledger.Vendo(uint32(id))
	if errors.Is(err, ledger.ErrVendoNotFound) || (err == nil && vendo.Status == "unpaired") {
		http.Error(response, "unknown Vendo", http.StatusNotFound)
		return
	}
	if err != nil {
		http.Error(response, "read Vendo", http.StatusInternalServerError)
		return
	}
	if err := persist(vendo.ID, action); err != nil {
		if action == "save" {
			http.Error(response, err.Error(), http.StatusBadRequest)
		} else {
			http.Error(response, "save Vendo", http.StatusInternalServerError)
		}
		return
	}
	http.Redirect(response, request, "/admin/vendos?vendo="+strconv.FormatUint(rowVendoID(request), 10), http.StatusSeeOther)
}

// vendoRowSwap answers a Vendo List row action: htmx gets the Vendo's
// re-rendered row with message as its error (htmx 4 swaps any status but
// 204/304, so errors are row HTML too); a plain form post gets the error text,
// or a redirect back to the Vendo List on success.
func (s *Server) vendoRowSwap(response http.ResponseWriter, request *http.Request, id uint64, status int, message string) {
	if request.Header.Get("HX-Request") != "true" {
		if status == http.StatusOK {
			http.Redirect(response, request, "/admin/vendos?vendo="+strconv.FormatUint(rowVendoID(request), 10), http.StatusSeeOther)
		} else {
			http.Error(response, message, status)
		}
		return
	}
	response.Header().Set("Content-Type", "text/html; charset=utf-8")
	rows, _, err := s.vendoRows(request.Context())
	if err != nil {
		status, message = http.StatusInternalServerError, "read Vendos"
	}
	response.WriteHeader(status)
	for _, row := range rows {
		if uint64(row.ID) == id {
			row.Error = message
			renderFragment(response, "vendos", "vendo_row", row)
			return
		}
	}
	renderFragment(response, "vendos", "vendo_row_error", struct {
		ID    uint64
		Error string
	}{id, message})
}

// rowVendoID reads the row action's Vendo ID, 0 if it is invalid.
func rowVendoID(request *http.Request) uint64 {
	if err := request.ParseForm(); err != nil {
		return 0
	}
	id, err := strconv.ParseUint(request.PostForm.Get("id"), 10, 32)
	if err != nil {
		return 0
	}
	return id
}

// vendoActive Deactivates (active=0) or Reactivates (active=1) a paired Vendo
// from the row's power button.
func (s *Server) vendoActive(response http.ResponseWriter, request *http.Request) {
	id := rowVendoID(request)
	if id == 0 {
		s.vendoRowSwap(response, request, 0, http.StatusBadRequest, "invalid Vendo ID")
		return
	}
	active := request.PostForm.Get("active")
	if active != "0" && active != "1" {
		s.vendoRowSwap(response, request, id, http.StatusBadRequest, "choose Deactivate or Reactivate")
		return
	}
	s.vendoMu.Lock()
	vendo, err := s.ledger.Vendo(uint32(id))
	if err == nil && vendo.Status == "paired" {
		err = s.ledger.SetVendoActive(vendo.ID, active == "1")
	}
	s.vendoMu.Unlock()
	switch {
	case errors.Is(err, ledger.ErrVendoNotFound) || (err == nil && vendo.Status == "unpaired"):
		s.vendoRowSwap(response, request, id, http.StatusNotFound, "unknown Vendo")
	case err != nil:
		s.vendoRowSwap(response, request, id, http.StatusInternalServerError, "save Vendo")
	case vendo.Status != "paired":
		s.vendoRowSwap(response, request, id, http.StatusConflict, "only a paired Vendo can be Deactivated or Reactivated")
	default:
		s.vendoRowSwap(response, request, id, http.StatusOK, "")
	}
}

// vendoCoinsOut records a Coins Out from the coin button beside Current Sales.
// The form carries the intake journal row its confirmed amount was computed
// through, so coins arriving after the confirmation stay in Current Sales.
// Unpaired Vendos keep this action, so their last coins can be collected.
func (s *Server) vendoCoinsOut(response http.ResponseWriter, request *http.Request) {
	id := rowVendoID(request)
	if id == 0 {
		s.vendoRowSwap(response, request, 0, http.StatusBadRequest, "invalid Vendo ID")
		return
	}
	through, err := strconv.ParseInt(request.PostForm.Get("through"), 10, 64)
	if err != nil {
		s.vendoRowSwap(response, request, id, http.StatusBadRequest, "invalid Coins Out")
		return
	}
	_, err = s.ledger.Vendo(uint32(id))
	if err == nil {
		err = s.ledger.RecordCoinsOut(uint32(id), through)
	}
	switch {
	case errors.Is(err, ledger.ErrVendoNotFound):
		s.vendoRowSwap(response, request, id, http.StatusNotFound, "unknown Vendo")
	case errors.Is(err, ledger.ErrCoinsOutAhead):
		s.vendoRowSwap(response, request, id, http.StatusBadRequest, "invalid Coins Out")
	case errors.Is(err, ledger.ErrNothingToCollect):
		s.vendoRowSwap(response, request, id, http.StatusConflict, "No coins since the last Coins Out")
	case err != nil:
		s.vendoRowSwap(response, request, id, http.StatusInternalServerError, "save Coins Out")
	default:
		s.vendoRowSwap(response, request, id, http.StatusOK, "")
	}
}

func (s *Server) vendoPair(response http.ResponseWriter, request *http.Request) {
	if err := request.ParseForm(); err != nil {
		http.Error(response, "invalid Vendo details", http.StatusBadRequest)
		return
	}
	mac, err := net.ParseMAC(request.FormValue("mac"))
	if err != nil || len(mac) != 6 || mac[0]&1 != 0 || mac.String() == "00:00:00:00:00:00" {
		http.Error(response, "a unicast six-byte MAC is required", http.StatusBadRequest)
		return
	}
	s.vendoMu.Lock()
	defer s.vendoMu.Unlock()
	vendo, err := s.ledger.PrepareVendo(strings.ToUpper(mac.String()))
	if errors.Is(err, ledger.ErrVendoConflict) {
		http.Error(response, err.Error(), http.StatusConflict)
		return
	}
	if err != nil {
		http.Error(response, "prepare Vendo pairing", http.StatusInternalServerError)
		return
	}
	response.Header().Set("Content-Type", "application/json")
	response.WriteHeader(http.StatusCreated)
	if err := json.NewEncoder(response).Encode(map[string]any{"id": vendo.ID, "secret": hex.EncodeToString(vendo.Secret), "controller_url": s.portalURL}); err != nil {
		return
	}
}

func (s *Server) vendoUnpair(response http.ResponseWriter, request *http.Request) {
	if err := request.ParseForm(); err != nil {
		http.Error(response, "invalid Vendo ID", http.StatusBadRequest)
		return
	}
	id, err := strconv.ParseUint(request.FormValue("id"), 10, 32)
	if err != nil || id == 0 {
		http.Error(response, "invalid Vendo ID", http.StatusBadRequest)
		return
	}
	s.vendoMu.Lock()
	defer s.vendoMu.Unlock()
	vendo, err := s.ledger.Vendo(uint32(id))
	if errors.Is(err, ledger.ErrVendoNotFound) {
		http.Error(response, "unknown Vendo", http.StatusNotFound)
		return
	}
	if err != nil {
		http.Error(response, "read Vendo", http.StatusInternalServerError)
		return
	}
	if vendo.Status == "provisioned" || vendo.Status == "unpaired" {
		if err := s.ledger.SetVendoStatus(vendo.ID, "unpaired"); err != nil {
			http.Error(response, "disable Vendo", http.StatusInternalServerError)
			return
		}
		unpaired(response, request)
		return
	}
	if err := s.ledger.SetVendoStatus(vendo.ID, "unpairing"); err != nil {
		http.Error(response, "disable Vendo", http.StatusInternalServerError)
		return
	}
	client, err := s.router()
	if errors.Is(err, errRouterNotConfigured) {
		err = errors.New("connect a Router before pairing a Vendo")
	}
	if err == nil {
		err = client.UnpairVendo(request.Context(), vendo.ID)
	}
	if err != nil {
		http.Error(response, err.Error()+"; Vendo disabled, retry unpairing to clean up", http.StatusBadGateway)
		return
	}
	if err := s.ledger.SetVendoStatus(vendo.ID, "unpaired"); err != nil {
		http.Error(response, "commit Vendo unpairing", http.StatusInternalServerError)
		return
	}
	unpaired(response, request)
}

// unpaired answers a finished Unpair. The Edit dialog's form is a browser
// navigation, so it returns to the Vendo List; scripted callers keep 204.
func unpaired(response http.ResponseWriter, request *http.Request) {
	if strings.Contains(request.Header.Get("Accept"), "text/html") {
		http.Redirect(response, request, "/admin/vendos?vendo="+strconv.FormatUint(rowVendoID(request), 10), http.StatusSeeOther)
		return
	}
	response.WriteHeader(http.StatusNoContent)
}

func (s *Server) vendoMessage(response http.ResponseWriter, request *http.Request) {
	// Inbound messages have 25 message bytes and a 32-byte MAC. One extra
	// byte detects oversized bodies without buffering the entire request.
	frame, err := io.ReadAll(io.LimitReader(request.Body, 58))
	if err != nil {
		s.rejectVendo(response, 0, "bad_frame", http.StatusBadRequest)
		return
	}
	var claimedID uint32
	if len(frame) >= 5 {
		claimedID = binary.BigEndian.Uint32(frame[1:5])
	}
	contentType, _, err := mime.ParseMediaType(request.Header.Get("Content-Type"))
	if err != nil || contentType != "application/octet-stream" {
		s.rejectVendo(response, claimedID, "wrong_content_type", http.StatusUnsupportedMediaType)
		return
	}
	if len(frame) > 0 && frame[0] != vendoprotocol.Report && frame[0] != vendoprotocol.Closed {
		s.rejectVendo(response, claimedID, "wrong_type", http.StatusBadRequest)
		return
	}
	report, err := vendoprotocol.Parse(frame)
	if err != nil {
		s.rejectVendo(response, claimedID, "bad_frame", http.StatusBadRequest)
		return
	}
	s.vendoMu.Lock()
	defer s.vendoMu.Unlock()
	vendo, err := s.ledger.Vendo(report.VendoID)
	if errors.Is(err, ledger.ErrVendoNotFound) {
		s.rejectVendo(response, report.VendoID, "unknown_vendo", http.StatusNotFound)
		return
	}
	if err != nil {
		http.Error(response, "read Vendo", http.StatusInternalServerError)
		return
	}
	if vendo.Status != "paired" && vendo.Status != "provisioned" && vendo.Status != "pending" {
		s.rejectVendo(response, report.VendoID, "unpaired_vendo", http.StatusUnauthorized)
		return
	}
	if err := vendoprotocol.Verify(frame, vendo.Secret); err != nil {
		s.rejectVendo(response, report.VendoID, "bad_hmac", http.StatusUnauthorized)
		return
	}
	if vendo.Status == "provisioned" || vendo.Status == "pending" {
		if report.Tag != vendoprotocol.Report {
			s.rejectVendo(response, report.VendoID, "first_contact_requires_report", http.StatusUnauthorized)
			return
		}
		client, err := s.router()
		if err != nil {
			http.Error(response, "Router unavailable", http.StatusServiceUnavailable)
			return
		}
		defer client.Close()
		ip := remoteAddress(request)
		if vendo.Status == "provisioned" {
			device, err := client.DeviceAt(request.Context(), ip)
			if err != nil || !strings.EqualFold(device.MAC, vendo.MAC) || device.HotspotServer == "" {
				s.rejectVendo(response, report.VendoID, "first_contact_host_mismatch", http.StatusUnauthorized)
				return
			}
			if err := s.ledger.StageVendoPairing(vendo.ID, ip, device.HotspotServer); err != nil {
				if errors.Is(err, ledger.ErrVendoConflict) {
					s.rejectVendo(response, report.VendoID, "first_contact_ip_conflict", http.StatusConflict)
				} else {
					http.Error(response, "save Vendo network identity", http.StatusInternalServerError)
				}
				return
			}
			vendo.IP = ip
		} else if ip != vendo.IP {
			s.rejectVendo(response, report.VendoID, "first_contact_host_mismatch", http.StatusUnauthorized)
			return
		}
		// Pairing clears the board's own connection. Finish the verified Router
		// projection even if that disconnect cancels the first Report request.
		ctx, cancel := context.WithTimeout(context.WithoutCancel(request.Context()), 30*time.Second)
		defer cancel()
		if err := client.PairVendo(ctx, vendo.ID, vendo.MAC, vendo.IP, s.portalURL); err != nil {
			http.Error(response, "Router pairing pending; retry report", http.StatusBadGateway)
			return
		}
		if err := s.ledger.SetVendoStatus(vendo.ID, "paired"); err != nil {
			http.Error(response, "commit Vendo pairing; retry report", http.StatusInternalServerError)
			return
		}
	}
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	if s.coinBootPending {
		http.Error(response, "Coin Window recovery pending", http.StatusServiceUnavailable)
		return
	}
	if err := s.ledger.RecordVendoReport(request.Context(), report); err != nil {
		if errors.Is(err, ledger.ErrVendoDisabled) {
			s.rejectVendo(response, report.VendoID, "unpaired_vendo", http.StatusUnauthorized)
			return
		}
		http.Error(response, "commit Vendo report", http.StatusInternalServerError)
		return
	}
	ack, err := (vendoprotocol.Message{
		Tag: vendoprotocol.Ack, VendoID: report.VendoID, Epoch: report.Epoch,
		Total: report.Total, BootNonce: report.BootNonce, RSeq: report.RSeq,
	}).Sign(vendo.Secret)
	if err != nil {
		http.Error(response, "sign Vendo ack", http.StatusInternalServerError)
		return
	}
	// Ack always echoes this report, including a stale resend. Commands use
	// the newest pair; the Router projection is left to the Reconciler.
	command, err := s.ledger.CoinWindowCommand(report.VendoID)
	if err != nil {
		http.Error(response, "read Coin Window command", http.StatusInternalServerError)
		return
	}
	if command != nil {
		frame, err := command.Sign(vendo.Secret)
		if err != nil {
			http.Error(response, "sign Coin Window command", http.StatusInternalServerError)
			return
		}
		ack = append(ack, frame...)
	}
	if report.Tag == vendoprotocol.Closed {
		s.wakeReconciler()
	}
	response.Header().Set("Content-Type", "application/octet-stream")
	response.Write(ack)
}

func (s *Server) rejectVendo(response http.ResponseWriter, id uint32, reason string, status int) {
	if err := s.ledger.RejectVendo(id, reason); err != nil {
		http.Error(response, "audit rejected Vendo message", http.StatusInternalServerError)
		return
	}
	http.Error(response, reason, status)
}
