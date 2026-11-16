//go:build dev

package controller

import (
	"html/template"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
)

// Dev build (-tags dev): web/templates, web/static and web/portal are read from the source
// tree on every request, so edits show on browser refresh without a restart.
// The path is fixed at build time, so run the binary on the machine that built it.
var (
	webDir      = filepath.Join(filepath.Dir(sourceFile()), "web")
	templateFS  = os.DirFS(filepath.Join(webDir, "templates"))
	staticFS    = os.DirFS(filepath.Join(webDir, "static"))
	portalFS    = os.DirFS(filepath.Join(webDir, "portal"))
	staticFiles = http.StripPrefix("/admin/static/", http.FileServerFS(staticFS))
	portalFiles = http.StripPrefix("/portal/", http.FileServerFS(portalFS))
)

const (
	assetVersion       = "dev"
	staticCacheControl = "no-store"
)

func sourceFile() string {
	_, file, _, _ := runtime.Caller(0)
	return file
}

// pageTemplate re-parses from disk on every call. A template syntax error
// panics, which net/http logs and answers with a dropped connection.
func pageTemplate(name string) *template.Template { return parsePages()[name] }
