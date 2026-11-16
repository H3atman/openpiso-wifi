package controller

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
	"unicode"
)

const adminSessionLifetime = 30 * 24 * time.Hour

func isAdminPath(value string) bool {
	return value == "/admin" || strings.HasPrefix(value, "/admin/")
}

// localAdminNext accepts only local admin paths, never an authority or traversal.
func localAdminNext(value string) string {
	parsed, err := url.Parse(value)
	if err != nil || parsed.IsAbs() || parsed.Host != "" || parsed.Opaque != "" || parsed.Fragment != "" {
		return ""
	}
	if !strings.HasPrefix(value, "/") || !isAdminPath(parsed.Path) || strings.Contains(parsed.Path, "\\") || strings.ContainsFunc(value, unicode.IsControl) || strings.ContainsFunc(parsed.Path, unicode.IsControl) {
		return ""
	}
	for _, segment := range strings.Split(parsed.Path, "/") {
		if segment == "." || segment == ".." {
			return ""
		}
	}
	return value
}

func loginDestination(value string) string {
	if next := localAdminNext(value); next != "" {
		return next
	}
	return "/admin/dashboard"
}

type loginView struct {
	Next   string
	Notice string
	Error  string
	Locked bool
}

func (s *Server) renderLogin(response http.ResponseWriter, request *http.Request, status int, message string) {
	next := localAdminNext(request.FormValue("next"))
	view := loginView{Next: next, Notice: "Log in to continue.", Error: message}
	if parsed, err := url.Parse(next); err == nil && next != "" {
		for _, item := range platformNav() {
			if item.Href == parsed.Path {
				view.Notice = "Log in to continue to " + item.Label
				break
			}
			for _, sub := range item.Subs {
				if sub.Href == parsed.Path {
					view.Notice = "Log in to continue to " + sub.Label
					break
				}
			}
		}
	}
	if request.URL.Query().Get("logged_out") == "1" {
		view.Notice = "You've logged out."
	}
	s.mu.Lock()
	failure := s.failures[remoteAddress(request)]
	s.mu.Unlock()
	remaining := time.Until(failure.blocked)
	if remaining > 0 {
		minutes := int((remaining + time.Minute - 1) / time.Minute)
		unit := "minutes"
		if minutes == 1 {
			unit = "minute"
		}
		view.Locked = true
		view.Error = fmt.Sprintf("Try again in %d %s", minutes, unit)
	} else if message != "" && failure.count >= maxLoginFailures-2 && failure.count < maxLoginFailures {
		view.Error += fmt.Sprintf(" %d tries left before a 5-minute lock", maxLoginFailures-failure.count)
	}
	var out bytes.Buffer
	if err := pageTemplate("login").ExecuteTemplate(&out, "login", Page{Title: "Log in", Data: view}); err != nil {
		http.Error(response, "render login", http.StatusInternalServerError)
		return
	}
	response.Header().Set("Content-Type", "text/html; charset=utf-8")
	response.WriteHeader(status)
	response.Write(out.Bytes())
}

func (s *Server) loginPage(response http.ResponseWriter, request *http.Request) {
	valid, err := s.hasAdminSession(request)
	if err != nil {
		http.Error(response, "read admin session", http.StatusInternalServerError)
		return
	}
	if valid {
		http.Redirect(response, request, loginDestination(request.URL.Query().Get("next")), http.StatusSeeOther)
		return
	}
	s.renderLogin(response, request, http.StatusOK, "")
}

func (s *Server) login(response http.ResponseWriter, request *http.Request) {
	if err := request.ParseForm(); err != nil {
		s.renderLogin(response, request, http.StatusUnauthorized, "Wrong password.")
		return
	}
	authenticated, locked := s.checkAdminPassword(request, request.PostForm.Get("password"))
	if !authenticated {
		status := http.StatusUnauthorized
		// The attempt that reaches the limit is still a wrong-password response.
		if locked {
			status = http.StatusTooManyRequests
		}
		s.renderLogin(response, request, status, "Wrong password.")
		return
	}
	tokenBytes := make([]byte, 32)
	if _, err := rand.Read(tokenBytes); err != nil {
		http.Error(response, "create admin session", http.StatusInternalServerError)
		return
	}
	token := base64.RawURLEncoding.EncodeToString(tokenBytes)
	now := time.Now()
	if err := s.ledger.CreateAdminSession(token, now, now.Add(adminSessionLifetime)); err != nil {
		http.Error(response, "store admin session", http.StatusInternalServerError)
		return
	}
	http.SetCookie(response, &http.Cookie{Name: "openpiso_admin", Value: token, Path: "/admin", HttpOnly: true, SameSite: http.SameSiteStrictMode, MaxAge: int(adminSessionLifetime / time.Second)})
	http.Redirect(response, request, loginDestination(request.PostForm.Get("next")), http.StatusSeeOther)
}

func (s *Server) logout(response http.ResponseWriter, request *http.Request) {
	cookie, err := request.Cookie("openpiso_admin")
	if err == nil {
		if err := s.ledger.DeleteAdminSession(cookie.Value); err != nil {
			http.Error(response, "delete admin session", http.StatusInternalServerError)
			return
		}
	}
	http.SetCookie(response, &http.Cookie{Name: "openpiso_admin", Path: "/admin", HttpOnly: true, SameSite: http.SameSiteStrictMode, MaxAge: -1})
	http.Redirect(response, request, "/admin/login?logged_out=1", http.StatusSeeOther)
}

// Both browser login and flash-time Basic auth share this source-address lockout.
func (s *Server) checkAdminPassword(request *http.Request, password string) (authenticated, locked bool) {
	address := remoteAddress(request)
	now := time.Now()
	s.mu.Lock()
	defer s.mu.Unlock()
	failure := s.failures[address]
	if now.Before(failure.blocked) {
		return false, true
	}
	if !failure.blocked.IsZero() {
		failure = loginFailures{}
	}
	if !s.ledger.AuthenticateAdmin(password) {
		failure.count++
		if failure.count >= maxLoginFailures {
			failure.blocked = now.Add(5 * time.Minute)
		}
		s.failures[address] = failure
		return false, false
	}
	delete(s.failures, address)
	return true, false
}

func (s *Server) authenticateAdminPassword(response http.ResponseWriter, request *http.Request, password string) bool {
	ok, locked := s.checkAdminPassword(request, password)
	if !ok {
		if locked {
			http.Error(response, "too many login attempts", http.StatusTooManyRequests)
		} else {
			http.Error(response, "invalid password", http.StatusUnauthorized)
		}
	}
	return ok
}

func (s *Server) hasAdminSession(request *http.Request) (bool, error) {
	cookie, err := request.Cookie("openpiso_admin")
	if err != nil {
		return false, nil
	}
	return s.ledger.AdminSessionValid(cookie.Value, time.Now())
}

func (s *Server) authorizeAdmin(response http.ResponseWriter, request *http.Request) bool {
	valid, err := s.hasAdminSession(request)
	if err != nil {
		http.Error(response, "read admin session", http.StatusInternalServerError)
		return false
	}
	if !valid {
		denyAdmin(response, request)
	}
	return valid
}

// htmx fragments return a 401 with a full-page redirect to the current page's login.
func denyAdmin(response http.ResponseWriter, request *http.Request) {
	next := localAdminNext(request.URL.RequestURI())
	if request.Header.Get("HX-Request") == "true" {
		next = ""
		if current, err := url.Parse(request.Header.Get("HX-Current-URL")); err == nil && current.Opaque == "" {
			next = localAdminNext(current.RequestURI())
		}
	}
	target := "/admin/login"
	if next != "" {
		target += "?next=" + url.QueryEscape(next)
	}
	if request.Header.Get("HX-Request") == "true" {
		response.Header().Set("HX-Redirect", target)
	} else if request.Method == http.MethodGet {
		http.Redirect(response, request, target, http.StatusSeeOther)
		return
	}
	http.Error(response, "admin login required", http.StatusUnauthorized)
}

func (s *Server) requireAdmin(next http.HandlerFunc) http.HandlerFunc {
	return func(response http.ResponseWriter, request *http.Request) {
		if s.authorizeAdmin(response, request) {
			next(response, request)
		}
	}
}
