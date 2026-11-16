package controller

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/update"
)

// The Controller's side of the self-update (internal/update): it reads the root helper's
// status.json and asks for an update by creating the request file in its data directory. It
// never fetches, verifies or installs anything itself.

// staleApplying is when an applying marker is taken to be left by a helper that died mid-apply.
const staleApplying = 15 * time.Minute

// updateNotice is the update part of the Dashboard's Controller card.
type updateNotice struct {
	Available string          `json:"available,omitempty"` // a published version newer than this build
	Pending   bool            `json:"pending"`             // requested or being applied
	Problem   *update.Attempt `json:"problem,omitempty"`   // the last apply, when it rolled back or failed
}

// readUpdateNotice reads status.json; nil when the root helper is not installed (dev runs).
func (s *Server) readUpdateNotice() *updateNotice {
	status, err := update.ReadStatus(s.updateStatus)
	if err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			log.Printf("read update status: %s", err)
		}
		return nil
	}
	notice := &updateNotice{}
	if update.Newer(status.Available, s.version) {
		notice.Available = status.Available
	}
	_, err = os.Lstat(filepath.Join(s.dataDir, update.RequestName))
	notice.Pending = err == nil || (status.Applying != nil && time.Since(status.Applying.At) < staleApplying)
	if last := status.LastApply; last != nil && last.Outcome != update.Installed {
		notice.Problem = last
	}
	return notice
}

// requestUpdate serves POST /admin/update: it creates the request file the openpiso-update.path
// unit watches, then shows the page that waits for the Controller to come back.
func (s *Server) requestUpdate(response http.ResponseWriter, request *http.Request) {
	notice := s.readUpdateNotice()
	if notice == nil || (notice.Available == "" && !notice.Pending) {
		http.Error(response, "no update available", http.StatusConflict)
		return
	}
	if !notice.Pending {
		file, err := os.OpenFile(filepath.Join(s.dataDir, update.RequestName), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
		if err != nil && !errors.Is(err, os.ErrExist) {
			http.Error(response, "request the update", http.StatusInternalServerError)
			return
		}
		if err == nil {
			file.Close()
		}
		notice.Pending = true
	}
	s.renderPage(response, request, "update", Page{Title: "Updating the Controller", Section: "Main", Active: "/admin/dashboard", Data: notice})
}

// updateState serves GET /admin/update/state, polled by the updating page while the Controller
// restarts: {"version": running build, "pending": bool}.
func (s *Server) updateState(response http.ResponseWriter, _ *http.Request) {
	notice := s.readUpdateNotice()
	response.Header().Set("Content-Type", "application/json")
	json.NewEncoder(response).Encode(map[string]any{"version": s.version, "pending": notice != nil && notice.Pending})
}
