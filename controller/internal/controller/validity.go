package controller

import (
	"net/http"
	"strconv"
	"strings"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
)

var validityUnits = []struct {
	name    string
	seconds int64
}{
	{"days", 86400},
	{"hours", 3600},
	{"minutes", 60},
	{"seconds", 1},
}

// validityLabel shows a Validity in its largest whole unit, e.g. "1 day";
// the ledger's maximum is "No limit".
func validityLabel(seconds int64) string {
	if seconds == ledger.MaxValiditySeconds {
		return "No limit"
	}
	for _, unit := range validityUnits {
		if seconds%unit.seconds == 0 {
			value := seconds / unit.seconds
			if value == 1 {
				return "1 " + strings.TrimSuffix(unit.name, "s")
			}
			return strconv.FormatInt(value, 10) + " " + unit.name
		}
	}
	return ""
}

func (s *Server) validitySettingsPage(response http.ResponseWriter, request *http.Request) {
	seconds, err := s.ledger.ValiditySeconds()
	if err != nil {
		http.Error(response, "read Validity settings", http.StatusInternalServerError)
		return
	}
	view := struct {
		Value int64
		Unit  string
		Label string
	}{}
	for _, unit := range validityUnits {
		if seconds%unit.seconds == 0 {
			view.Value, view.Unit, view.Label = seconds/unit.seconds, unit.name, unit.name
			if view.Value == 1 {
				view.Label = strings.TrimSuffix(unit.name, "s")
			}
			break
		}
	}
	s.renderPage(response, request, "validity", Page{Title: "Validity", Section: "Settings", Active: "/admin/validity", Data: view})
}

func (s *Server) saveValiditySettings(response http.ResponseWriter, request *http.Request) {
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	if err := request.ParseForm(); err != nil {
		http.Error(response, "invalid Validity", http.StatusBadRequest)
		return
	}
	value, err := strconv.ParseInt(request.PostForm.Get("value"), 10, 64)
	if err == nil && value > 0 {
		for _, unit := range validityUnits {
			if request.PostForm.Get("unit") != unit.name || value > ledger.MaxValiditySeconds/unit.seconds {
				continue
			}
			if err := s.ledger.SaveValiditySeconds(value * unit.seconds); err != nil {
				http.Error(response, err.Error(), http.StatusBadRequest)
				return
			}
			s.validitySettingsPage(response, request)
			return
		}
	}
	http.Error(response, "choose a whole-number Validity from 1 second to 36500 days and a unit", http.StatusBadRequest)
}
