package controller

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/netip"
	"strings"
	"sync"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/hoststatus"
	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
	"github.com/H3atman/openpiso-wifi/controller/internal/update"
)

const maxLoginFailures = 5

type loginFailures struct {
	count   int
	blocked time.Time
}

type pendingRouter struct {
	address     string
	username    string
	password    string
	certificate routeros.Certificate
}

type Server struct {
	vendoMu         sync.Mutex
	ledger          *ledger.Ledger
	portalURL       string
	voucherMu       sync.Mutex
	voucherFailures map[string]loginFailures
	resumeFailures  map[string]loginFailures
	mu              sync.Mutex
	failures        map[string]loginFailures
	pending         map[string]pendingRouter
	routerStatus    string
	routerError     string
	reconciliation  reconciliationState
	reconcileWake   chan struct{}
	coinBootPending bool
	routes          []route
	usage           usageCache         // shared Router usage snapshot for the admin UI
	host            *hoststatus.Reader // Controller machine health for the Dashboard
	version         string             // the running build, shown on the Dashboard
	dataDir         string             // holds the update request file
	updateStatus    string             // the root helper's status.json
}

type authentication string

const adminSession authentication = "admin-session"
const adminPairAuth authentication = "admin-session-or-basic"

type route struct {
	Method         string           `json:"method"`
	Path           string           `json:"path"`
	StateChanging  bool             `json:"state_changing"`
	Authentication authentication   `json:"authentication"`
	Handler        http.HandlerFunc `json:"-"`
}

func Handler(ctx context.Context, store *ledger.Ledger, portalURL, routerStatus, routerError, dataDir, version string, adminNetwork netip.Prefix) http.Handler {
	server := &Server{
		ledger:          store,
		reconcileWake:   make(chan struct{}, 1),
		coinBootPending: true,
		portalURL:       portalURL,
		host:            hoststatus.New(dataDir),
		version:         version,
		dataDir:         dataDir,
		updateStatus:    update.StatusPath,
		voucherFailures: make(map[string]loginFailures),
		resumeFailures:  make(map[string]loginFailures),
		failures:        make(map[string]loginFailures),
		pending:         make(map[string]pendingRouter),
		routerStatus:    routerStatus,
		routerError:     routerError,
	}
	server.routes = []route{
		{Method: http.MethodPost, Path: "/admin/logout", StateChanging: true, Authentication: adminSession, Handler: server.logout},
		{Method: http.MethodGet, Path: "/healthz", Authentication: "none", Handler: server.health},
		{Method: http.MethodGet, Path: "/portal/ready.js", Authentication: "none", Handler: server.portalReady},
		{Method: http.MethodGet, Path: "/portal", Authentication: "none", Handler: server.portalPage},
		{Method: http.MethodGet, Path: "/portal/{file}", Authentication: "none", Handler: server.portalAsset},
		{Method: http.MethodGet, Path: "/portal/api/state", Authentication: "router-device", Handler: server.portalStateRead},
		{Method: http.MethodPost, Path: "/portal/api/coins/open", StateChanging: true, Authentication: "router-device", Handler: server.openCoinWindow},
		{Method: http.MethodPost, Path: "/portal/api/coins/done", StateChanging: true, Authentication: "router-device", Handler: server.doneCoinWindow},
		{Method: http.MethodGet, Path: "/admin/coins", Authentication: adminSession, Handler: server.coinSalesPage},
		{Method: http.MethodGet, Path: "/admin/rates", Authentication: adminSession, Handler: server.ratesPage},
		{Method: http.MethodPost, Path: "/admin/rates", StateChanging: true, Authentication: adminSession, Handler: server.saveRates},
		{Method: http.MethodGet, Path: "/admin/points", Authentication: adminSession, Handler: server.pointsPage},
		{Method: http.MethodPost, Path: "/admin/points", StateChanging: true, Authentication: adminSession, Handler: server.savePoints},
		{Method: http.MethodPost, Path: "/portal/api/points/redeem", StateChanging: true, Authentication: "router-device", Handler: server.redeemPoints},
		{Method: http.MethodPost, Path: "/vendo/messages", StateChanging: true, Authentication: "vendo-hmac", Handler: server.vendoMessage},
		{Method: http.MethodPost, Path: "/portal/api/redeem", StateChanging: true, Authentication: "router-device", Handler: server.redeemVoucher},
		{Method: http.MethodPost, Path: "/portal/api/connect", StateChanging: true, Authentication: "router-device", Handler: server.connectTimeCredit},
		{Method: http.MethodPost, Path: "/portal/api/recover", StateChanging: true, Authentication: "router-device", Handler: server.recoverTimeCredit},
		{Method: http.MethodPost, Path: "/portal/api/pause", StateChanging: true, Authentication: "router-device", Handler: server.pauseAction},
		{Method: http.MethodPost, Path: "/portal/api/resume", StateChanging: true, Authentication: "signed-cookie-and-router-device", Handler: server.pauseAction},
		{Method: http.MethodGet, Path: "/admin/pause", Authentication: adminSession, Handler: server.pauseSettingsPage},
		{Method: http.MethodPost, Path: "/admin/pause", StateChanging: true, Authentication: adminSession, Handler: server.savePauseSettings},
		{Method: http.MethodGet, Path: "/admin/validity", Authentication: adminSession, Handler: server.validitySettingsPage},
		{Method: http.MethodPost, Path: "/admin/validity", StateChanging: true, Authentication: adminSession, Handler: server.saveValiditySettings},
		{Method: http.MethodGet, Path: "/admin/portal", Authentication: adminSession, Handler: server.portalEditorPage},
		{Method: http.MethodGet, Path: "/admin/voucher-template", Authentication: adminSession, Handler: server.voucherEditorPage},
		{Method: http.MethodGet, Path: "/admin/voucher-template/preview", Authentication: adminSession, Handler: server.voucherPreview},
		{Method: http.MethodPost, Path: "/admin/voucher-template/draft", StateChanging: true, Authentication: adminSession, Handler: server.saveVoucherDraft},
		{Method: http.MethodPost, Path: "/admin/voucher-template/shop-name", StateChanging: true, Authentication: adminSession, Handler: server.saveShopName},
		{Method: http.MethodPost, Path: "/admin/voucher-template/publish", StateChanging: true, Authentication: adminSession, Handler: server.voucherSlotAction(store.PublishVoucher)},
		{Method: http.MethodPost, Path: "/admin/voucher-template/revert", StateChanging: true, Authentication: adminSession, Handler: server.voucherSlotAction(store.RevertVoucher)},
		{Method: http.MethodPost, Path: "/admin/voucher-template/reset", StateChanging: true, Authentication: adminSession, Handler: server.voucherSlotAction(store.ResetVoucher)},
		{Method: http.MethodGet, Path: "/admin/portal/default/{file}", Authentication: adminSession, Handler: downloadDefaultPortal},
		{Method: http.MethodGet, Path: "/admin/portal/preview", Authentication: adminSession, Handler: server.portalPreview},
		{Method: http.MethodPost, Path: "/admin/portal/draft", StateChanging: true, Authentication: adminSession, Handler: server.savePortalDraft},
		{Method: http.MethodGet, Path: "/admin/portal/draft/{image}", Authentication: adminSession, Handler: server.draftPortalImage},
		{Method: http.MethodPost, Path: "/admin/portal/images/{image}", StateChanging: true, Authentication: adminSession, Handler: server.uploadPortalImage},
		{Method: http.MethodPost, Path: "/admin/portal/publish", StateChanging: true, Authentication: adminSession, Handler: server.portalSlotAction(store.PublishPortal)},
		{Method: http.MethodPost, Path: "/admin/portal/revert", StateChanging: true, Authentication: adminSession, Handler: server.portalSlotAction(store.RevertPortal)},
		{Method: http.MethodPost, Path: "/admin/portal/reset", StateChanging: true, Authentication: adminSession, Handler: server.portalSlotAction(store.ResetPortal)},
		{Method: http.MethodGet, Path: "/admin/intents", Authentication: adminSession, Handler: server.intentsPage},
		{Method: http.MethodGet, Path: "/admin/dashboard", Authentication: adminSession, Handler: server.dashboardPage},
		{Method: http.MethodGet, Path: "/admin/sales", Authentication: adminSession, Handler: server.salesPage},
		{Method: http.MethodGet, Path: "/admin/sales/export", Authentication: adminSession, Handler: server.salesPage},
		{Method: http.MethodPost, Path: "/admin/grants", StateChanging: true, Authentication: adminSession, Handler: server.adminGrant},
		{Method: http.MethodPost, Path: "/admin/audit/resolve", StateChanging: true, Authentication: adminSession, Handler: server.resolveAudit},
		{Method: http.MethodGet, Path: "/admin/vouchers", Authentication: adminSession, Handler: server.voucherBatchPage},
		{Method: http.MethodGet, Path: "/admin/vouchers/export", Authentication: adminSession, Handler: server.exportVoucherBatch},
		{Method: http.MethodPost, Path: "/admin/vouchers", StateChanging: true, Authentication: adminSession, Handler: server.createVoucherBatch},
		{Method: http.MethodPost, Path: "/admin/vouchers/delete", StateChanging: true, Authentication: adminSession, Handler: server.deleteVoucherBatch},
		{Method: http.MethodGet, Path: "/admin/login", Authentication: "none", Handler: server.loginPage},
		{Method: http.MethodPost, Path: "/admin/login", StateChanging: true, Authentication: "admin-password", Handler: server.login},
		{Method: http.MethodGet, Path: "/admin", Authentication: adminSession, Handler: server.admin},
		{Method: http.MethodGet, Path: "/admin/state", Authentication: adminSession, Handler: server.state},
		{Method: http.MethodGet, Path: "/admin/routes", Authentication: adminSession, Handler: server.routeList},
		{Method: http.MethodGet, Path: "/admin/static/", Authentication: "none", Handler: staticHandler},
		{Method: http.MethodGet, Path: "/admin/search", Authentication: adminSession, Handler: server.search},
		{Method: http.MethodGet, Path: "/admin/time-credits", Authentication: adminSession, Handler: server.timeCreditsPage},
		{Method: http.MethodGet, Path: "/admin/time-credits/list", Authentication: adminSession, Handler: server.timeCreditsList},
		{Method: http.MethodGet, Path: "/admin/time-credits/detail", Authentication: adminSession, Handler: server.timeCreditsDetail},
		{Method: http.MethodGet, Path: "/admin/fragments/dashboard", Authentication: adminSession, Handler: server.dashboardFragment},
		{Method: http.MethodGet, Path: "/admin/logs", Authentication: adminSession, Handler: server.logsPage},
		{Method: http.MethodGet, Path: "/admin/logs/feed", Authentication: adminSession, Handler: server.logsFeed},
		{Method: http.MethodGet, Path: "/admin/vendos", Authentication: adminSession, Handler: server.vendos},
		{Method: http.MethodPost, Path: "/admin/vendos/pair", StateChanging: true, Authentication: adminPairAuth, Handler: server.vendoPair},
		{Method: http.MethodPost, Path: "/admin/vendos/unpair", StateChanging: true, Authentication: adminSession, Handler: server.vendoUnpair},
		{Method: http.MethodPost, Path: "/admin/vendos/edit", StateChanging: true, Authentication: adminSession, Handler: server.vendoEdit},
		{Method: http.MethodPost, Path: "/admin/vendos/active", StateChanging: true, Authentication: adminSession, Handler: server.vendoActive},
		{Method: http.MethodPost, Path: "/admin/vendos/rates", StateChanging: true, Authentication: adminSession, Handler: server.vendoRates},
		{Method: http.MethodPost, Path: "/admin/vendos/points", StateChanging: true, Authentication: adminSession, Handler: server.vendoPoints},
		{Method: http.MethodPost, Path: "/admin/vendos/coins-out", StateChanging: true, Authentication: adminSession, Handler: server.vendoCoinsOut},
		{Method: http.MethodPost, Path: "/admin/router/probe", StateChanging: true, Authentication: adminSession, Handler: server.routerProbe},
		{Method: http.MethodPost, Path: "/admin/update", StateChanging: true, Authentication: adminSession, Handler: server.requestUpdate},
		{Method: http.MethodGet, Path: "/admin/update/state", Authentication: adminSession, Handler: server.updateState},
		{Method: http.MethodPost, Path: "/admin/router/confirm", StateChanging: true, Authentication: adminSession, Handler: server.routerConfirm},
		{Method: http.MethodGet, Path: "/admin/router/use-existing", Authentication: adminSession, Handler: server.useExisting},
		{Method: http.MethodPost, Path: "/admin/router/adopt", StateChanging: true, Authentication: adminSession, Handler: server.adoptServers},
		{Method: http.MethodPost, Path: "/admin/router/add-alongside", StateChanging: true, Authentication: adminSession, Handler: server.addAlongside},
		{Method: http.MethodPost, Path: "/admin/router/fix", StateChanging: true, Authentication: adminSession, Handler: server.fixCheck},
		{Method: http.MethodPost, Path: "/admin/router/forget", StateChanging: true, Authentication: adminSession, Handler: server.forgetServer},
		{Method: http.MethodPost, Path: "/admin/router/fresh-start", StateChanging: true, Authentication: adminSession, Handler: server.freshStart},
	}

	go server.runReconciler(ctx)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /{$}", func(response http.ResponseWriter, request *http.Request) {
		target := "/portal"
		address, err := netip.ParseAddr(remoteAddress(request))
		if err == nil && adminNetwork.Contains(address.Unmap()) {
			target = "/admin/dashboard"
		}
		http.Redirect(response, request, target, http.StatusSeeOther)
	})
	for _, registered := range server.routes {
		handler := registered.Handler
		if registered.Authentication == adminSession && registered.Method != http.MethodGet {
			handler = server.requireAdmin(handler)
		} else if registered.Authentication == adminPairAuth {
			handler = server.requirePairAdmin(handler)
		}
		mux.HandleFunc(registered.Method+" "+registered.Path, handler)
	}
	return http.NewCrossOriginProtection().Handler(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		response.Header().Set("Cache-Control", "no-store")
		response.Header().Set("X-Content-Type-Options", "nosniff")
		if strings.HasPrefix(request.URL.Path, "/portal") {
			response.Header().Set("Content-Security-Policy", portalCSP)
			// Preview scripts can self-navigate despite connect-src 'none'. Only
			// fetch may reach the API; headerless RouterOS/Python clients still work.
			if _, present := request.Header["Sec-Fetch-Dest"]; strings.HasPrefix(request.URL.Path, "/portal/api/") && present && request.Header.Get("Sec-Fetch-Dest") != "empty" {
				http.Error(response, "Portal API accepts fetch requests only", http.StatusForbidden)
				return
			}
		}
		if strings.HasPrefix(request.URL.Path, "/admin") {
			response.Header().Set("Content-Security-Policy", adminCSP)
			address, err := netip.ParseAddr(remoteAddress(request))
			if err != nil || !adminNetwork.Contains(address.Unmap()) {
				http.Error(response, "management network only", http.StatusForbidden)
				return
			}
		}
		// Guard unknown admin GETs too, before ServeMux canonicalization or dispatch.
		if (request.Method == http.MethodGet || request.Method == http.MethodHead) && isAdminPath(request.URL.Path) && request.URL.Path != "/admin/login" && !strings.HasPrefix(request.URL.Path, "/admin/static/") {
			if !server.authorizeAdmin(response, request) {
				return
			}
		}
		limit := int64(64 << 10)
		if request.URL.Path == "/admin/portal/draft" || request.URL.Path == "/admin/voucher-template/draft" {
			limit = 1 << 20 // three files of up to 64 KB each, form-encoded; the save check sizes each
		}
		if strings.HasPrefix(request.URL.Path, "/admin/portal/images/") {
			limit = 1 << 20 // the upload reads up to its slot's cap and refuses anything longer
		}
		request.Body = http.MaxBytesReader(response, request.Body, limit)
		mux.ServeHTTP(response, request)
	}))
}

// The Router-hosted fallback redirects only after this non-cached script loads.
// No credential, login or credit mutation is performed on this path.
func (s *Server) portalReady(response http.ResponseWriter, _ *http.Request) {
	response.Header().Set("Content-Type", "application/javascript; charset=utf-8")
	response.Write([]byte("/* Controller available */"))
}

func (s *Server) health(response http.ResponseWriter, _ *http.Request) {
	response.WriteHeader(http.StatusNoContent)
}

// Flash scripts use Basic auth only on the provisioning endpoint. It shares
// the password login rate limit and management-network/Origin restrictions.
func (s *Server) requirePairAdmin(next http.HandlerFunc) http.HandlerFunc {
	return func(response http.ResponseWriter, request *http.Request) {
		username, password, ok := request.BasicAuth()
		if !ok {
			s.requireAdmin(next)(response, request)
			return
		}
		if username != "admin" {
			password = ""
		}
		response.Header().Set("WWW-Authenticate", `Basic realm="OpenPiso"`)
		if !s.authenticateAdminPassword(response, request, password) {
			return
		}
		next(response, request)
	}
}

func (s *Server) routerProbe(response http.ResponseWriter, request *http.Request) {
	if err := request.ParseForm(); err != nil {
		http.Error(response, "invalid Router details", http.StatusBadRequest)
		return
	}
	address := request.FormValue("address")
	username := request.FormValue("username")
	password := request.FormValue("password")
	if username == "" || password == "" {
		http.Error(response, "Router admin username and password are required", http.StatusBadRequest)
		return
	}
	certificate, err := routeros.ProbeCertificate(request.Context(), address)
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	cookie, _ := request.Cookie("openpiso_admin")
	s.mu.Lock()
	s.pending[cookie.Value] = pendingRouter{
		address:     address,
		username:    username,
		password:    password,
		certificate: certificate,
	}
	s.mu.Unlock()

	s.renderPage(response, request, "certificate", Page{Title: "Confirm Router certificate", Section: "Settings", Active: "/admin", Data: certificate})
}

func (s *Server) routerConfirm(response http.ResponseWriter, request *http.Request) {
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	cookie, _ := request.Cookie("openpiso_admin")
	s.mu.Lock()
	pending, ok := s.pending[cookie.Value]
	s.mu.Unlock()
	if !ok {
		http.Error(response, "read the Router certificate before confirming it", http.StatusConflict)
		return
	}
	admin, err := routeros.NewClient(pending.address, pending.username, pending.password, pending.certificate)
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	config, configured, err := s.ledger.RouterConfig()
	if err != nil {
		http.Error(response, "read Router configuration", http.StatusInternalServerError)
		return
	}
	existingSecret := ""
	if configured && config.Address == pending.address {
		existingSecret = config.APISecret
	}
	provisioned, err := routeros.Onboard(request.Context(), admin, existingSecret)
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	if err := s.ledger.SaveRouterConfig(ledger.RouterConfig{
		Address:     provisioned.Address,
		Username:    provisioned.Username,
		APISecret:   provisioned.APISecret,
		Certificate: provisioned.Certificate.DER,
		Fingerprint: provisioned.Certificate.Fingerprint,
	}); err != nil {
		http.Error(response, err.Error(), http.StatusInternalServerError)
		return
	}
	// Fresh start runs only while this backup is still on the Router.
	if err := s.ledger.SaveRouterBackup(provisioned.Backup); err != nil {
		http.Error(response, err.Error(), http.StatusInternalServerError)
		return
	}
	s.mu.Lock()
	delete(s.pending, cookie.Value)
	s.routerStatus = "ready"
	s.routerError = ""
	s.mu.Unlock()

	// Connecting changes no HotSpot server; Router setup goes on with Use existing.
	http.Redirect(response, request, "/admin", http.StatusSeeOther)
}

func (s *Server) state(response http.ResponseWriter, _ *http.Request) {
	database, err := s.ledger.DatabaseState()
	if err != nil {
		http.Error(response, "read Controller state", http.StatusInternalServerError)
		return
	}
	vendos, err := s.ledger.Vendos()
	if err != nil {
		http.Error(response, "read Vendos", http.StatusInternalServerError)
		return
	}
	vendoProtocol, err := s.ledger.VendoProtocolState()
	if err != nil {
		http.Error(response, "read Vendo protocol state", http.StatusInternalServerError)
		return
	}
	windows, err := s.ledger.CoinWindows()
	if err != nil {
		http.Error(response, "read Coin Windows", http.StatusInternalServerError)
		return
	}
	rates, err := s.ledger.RateTiers()
	if err != nil {
		http.Error(response, "read coin rates", http.StatusInternalServerError)
		return
	}
	pointsTiers, err := s.ledger.PointsTiers()
	if err != nil {
		http.Error(response, "read the Points table", http.StatusInternalServerError)
		return
	}
	coinSales, err := s.ledger.CoinSales()
	if err != nil {
		http.Error(response, "read coin sales", http.StatusInternalServerError)
		return
	}
	state := map[string]any{"database": database, "vendos": vendos, "vendo_protocol": vendoProtocol,
		"coin_windows": windows, "rate_tiers": rates, "points_tiers": pointsTiers, "coin_sales": coinSales}
	purchases, err := s.ledger.PurchaseState()
	if err != nil {
		http.Error(response, "read purchases", http.StatusInternalServerError)
		return
	}
	pauseSettings, err := s.ledger.PauseSettings()
	if err != nil {
		http.Error(response, "read Pause settings", http.StatusInternalServerError)
		return
	}
	pauses, err := s.ledger.Pauses()
	if err != nil {
		http.Error(response, "read Pauses", http.StatusInternalServerError)
		return
	}
	intents, err := s.ledger.CreditIntents()
	if err != nil {
		http.Error(response, "read credit intents", http.StatusInternalServerError)
		return
	}
	validitySeconds, err := s.ledger.ValiditySeconds()
	if err != nil {
		http.Error(response, "read Validity settings", http.StatusInternalServerError)
		return
	}
	validity, err := s.ledger.Validities()
	if err != nil {
		http.Error(response, "read Validity", http.StatusInternalServerError)
		return
	}
	s.mu.Lock()
	reconciliation := s.reconciliation
	s.mu.Unlock()
	review, err := s.ledger.AuditReview()
	if err != nil {
		http.Error(response, "read audit review", http.StatusInternalServerError)
		return
	}
	portal, err := s.ledger.PortalState()
	if err != nil {
		http.Error(response, "read the Custom Portal", http.StatusInternalServerError)
		return
	}
	state["portal"] = portal
	state["audit_review"] = review
	state["purchases"] = purchases
	state["pause_settings"] = pauseSettings
	state["pauses"] = pauses
	state["validity_seconds"] = validitySeconds
	state["validity"] = validity
	state["credit_intents"] = intents
	state["reconciliation"] = reconciliation
	config, configured, err := s.ledger.RouterConfig()
	if err != nil {
		http.Error(response, "read Controller state", http.StatusInternalServerError)
		return
	}
	if configured {
		s.mu.Lock()
		status := s.routerStatus
		routerError := s.routerError
		s.mu.Unlock()
		adopted, err := s.ledger.AdoptedHotspotServers()
		if err != nil {
			http.Error(response, "read Controller state", http.StatusInternalServerError)
			return
		}
		routerState := map[string]any{
			"address":                 config.Address,
			"adopted_hotspot_servers": adopted,
			"configured":              true,
			"fingerprint":             config.Fingerprint,
			"status":                  status,
			"username":                config.Username,
		}
		if routerError != "" {
			routerState["error"] = routerError
		}
		state["router"] = routerState
	}
	response.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(response).Encode(state); err != nil {
		http.Error(response, "encode Controller state", http.StatusInternalServerError)
	}
}

func (s *Server) routeList(response http.ResponseWriter, _ *http.Request) {
	response.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(response).Encode(s.routes); err != nil {
		http.Error(response, "encode route list", http.StatusInternalServerError)
	}
}

func remoteAddress(request *http.Request) string {
	host, _, err := net.SplitHostPort(request.RemoteAddr)
	if err == nil {
		return host
	}
	return request.RemoteAddr
}
