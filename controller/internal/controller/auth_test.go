package controller

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"net/url"
	"path/filepath"
	"strings"
	"testing"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
)

func authHandler(t *testing.T) (http.Handler, *ledger.Ledger) {
	t.Helper()
	dir := t.TempDir()
	store, err := ledger.Open(filepath.Join(dir, "ledger.db"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.InitializeAdmin("secret"); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(func() { cancel(); store.Close() })
	return Handler(ctx, store, "http://portal", "", "", dir, "dev", netip.MustParsePrefix("192.168.88.0/24")), store
}

func authRequest(handler http.Handler, method, target, body string) *httptest.ResponseRecorder {
	request := httptest.NewRequest(method, target, strings.NewReader(body))
	request.RemoteAddr = "192.168.88.2:1234"
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	return response
}

func TestLoginSessionAndLogout(t *testing.T) {
	handler, store := authHandler(t)
	next := "/admin/sales?type=coin&page=2"
	wrong := authRequest(handler, "POST", "/admin/login", url.Values{"password": {"wrong"}, "next": {next}}.Encode())
	if wrong.Code != 401 || !strings.Contains(wrong.Body.String(), "Wrong password.") || !strings.Contains(wrong.Body.String(), "continue to Sales Inventory") {
		t.Fatalf("wrong password: %d %s", wrong.Code, wrong.Body.String())
	}
	login := authRequest(handler, "POST", "/admin/login", "password=secret")
	cookies := login.Result().Cookies()
	if len(cookies) != 1 || cookies[0].MaxAge != 30*24*60*60 || !cookies[0].HttpOnly {
		t.Fatalf("session cookie: %+v", cookies)
	}
	second := authRequest(handler, "POST", "/admin/login", "password=secret").Result().Cookies()[0]
	// A fresh Handler has no shared in-memory session state.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	restarted := Handler(ctx, store, "http://portal", "", "", t.TempDir(), "dev", netip.MustParsePrefix("192.168.88.0/24"))
	requestWithCookie := func(method, target string, cookie *http.Cookie) *httptest.ResponseRecorder {
		request := httptest.NewRequest(method, target, nil)
		request.RemoteAddr = "192.168.88.2:1234"
		request.AddCookie(cookie)
		response := httptest.NewRecorder()
		restarted.ServeHTTP(response, request)
		return response
	}
	already := requestWithCookie("GET", "/admin/login?next="+url.QueryEscape(next), cookies[0])
	if already.Code != 303 || already.Header().Get("Location") != next {
		t.Fatalf("session after restart: %d %s", already.Code, already.Body.String())
	}
	logout := requestWithCookie("POST", "/admin/logout", cookies[0])
	if logout.Code != 303 || logout.Result().Cookies()[0].MaxAge != -1 {
		t.Fatalf("logout: %+v", logout.Result())
	}
	notice := authRequest(restarted, "GET", logout.Header().Get("Location"), "")
	if !strings.Contains(notice.Body.String(), "logged out.") {
		t.Fatal("missing logout notice")
	}
	if response := requestWithCookie("GET", "/admin/login", cookies[0]); response.Code != 200 {
		t.Fatalf("logged-out session: %d", response.Code)
	}
	if response := requestWithCookie("GET", "/admin/login", second); response.Code != 303 {
		t.Fatalf("other browser session: %d", response.Code)
	}
}

func TestLoginLockout(t *testing.T) {
	handler, _ := authHandler(t)
	for attempt := 1; attempt <= 5; attempt++ {
		response := authRequest(handler, "POST", "/admin/login", "password=wrong")
		if response.Code != 401 {
			t.Fatalf("attempt %d: %d", attempt, response.Code)
		}
		if attempt == 3 && !strings.Contains(response.Body.String(), "2 tries left before a 5-minute lock") {
			t.Fatal("missing remaining tries")
		}
		if attempt == 5 && (!strings.Contains(response.Body.String(), "Try again in 5 minutes") || !strings.Contains(response.Body.String(), "disabled")) {
			t.Fatal("missing locked form")
		}
	}
	response := authRequest(handler, "GET", "/admin/login", "")
	if !strings.Contains(response.Body.String(), "Try again in 5 minutes") {
		t.Fatal("lock missing on GET")
	}
	response = authRequest(handler, "POST", "/admin/login", "password=secret")
	if response.Code != 429 {
		t.Fatalf("locked login: %d", response.Code)
	}
}

func TestLoginNext(t *testing.T) {
	handler, _ := authHandler(t)
	for _, next := range []string{"https://evil.example/admin", "//evil.example/admin", "/portal", "/administrator", "/admin/../portal", "/admin\\evil", "/admin\r\nLocation: bad", ""} {
		t.Run(next, func(t *testing.T) {
			response := authRequest(handler, "POST", "/admin/login", url.Values{"password": {"secret"}, "next": {next}}.Encode())
			if response.Code != http.StatusSeeOther || response.Header().Get("Location") != "/admin/dashboard" {
				t.Fatalf("status %d, location %q", response.Code, response.Header().Get("Location"))
			}
		})
	}
	next := "/admin/sales?type=coin&page=2"
	response := authRequest(handler, "POST", "/admin/login", url.Values{"password": {"secret"}, "next": {next}}.Encode())
	if response.Header().Get("Location") != next {
		t.Fatalf("location %q", response.Header().Get("Location"))
	}
}

func TestRootNetworkRedirect(t *testing.T) {
	handler, _ := authHandler(t)
	for _, test := range []struct{ address, target string }{{"192.168.88.2:1234", "/admin/dashboard"}, {"10.5.50.2:1234", "/portal"}} {
		request := httptest.NewRequest("GET", "/", nil)
		request.RemoteAddr = test.address
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != http.StatusSeeOther || response.Header().Get("Location") != test.target {
			t.Fatalf("%s: status %d, location %q", test.address, response.Code, response.Header().Get("Location"))
		}
	}
}

func TestUnauthenticatedAdmin(t *testing.T) {
	handler, _ := authHandler(t)
	for _, test := range []struct {
		method, path, current, location string
		status                          int
	}{
		{"GET", "/admin/sales?type=coin", "", "/admin/login?next=%2Fadmin%2Fsales%3Ftype%3Dcoin", 303},
		{"GET", "/admin/not-a-route", "", "/admin/login?next=%2Fadmin%2Fnot-a-route", 303},
		{"POST", "/admin/rates", "", "", 401},
		{"GET", "/admin/fragments/dashboard", "http://controller/admin/sales?type=coin", "/admin/login?next=%2Fadmin%2Fsales%3Ftype%3Dcoin", 401},
		{"GET", "/admin/fragments/dashboard", "https://evil.example/portal", "/admin/login", 401},
	} {
		request := httptest.NewRequest(test.method, test.path, nil)
		request.RemoteAddr = "192.168.88.2:1234"
		if test.current != "" {
			request.Header.Set("HX-Request", "true")
			request.Header.Set("HX-Current-URL", test.current)
		}
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		header := "Location"
		if test.current != "" {
			header = "HX-Redirect"
		}
		if response.Code != test.status || response.Header().Get(header) != test.location {
			t.Errorf("%s %s: status %d, %s %q", test.method, test.path, response.Code, header, response.Header().Get(header))
		}
	}
}
