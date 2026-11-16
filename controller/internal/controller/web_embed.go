//go:build !dev

package controller

import (
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"html/template"
	"io/fs"
	"net/http"
)

//go:embed web/templates web/static web/portal
var webFS embed.FS

var (
	templateFS = mustSub("web/templates")
	staticFS   = mustSub("web/static")
	portalFS   = mustSub("web/portal")
	// assetVersion busts the immutable cache: it changes whenever an embedded asset does.
	assetVersion = hashFS(staticFS)
	pages        = parsePages()
	staticFiles  = http.StripPrefix("/admin/static/", http.FileServerFS(staticFS))
	portalFiles  = http.StripPrefix("/portal/", http.FileServerFS(portalFS))
)

const staticCacheControl = "public, max-age=31536000, immutable"

// pageTemplate returns a template parsed once at startup.
func pageTemplate(name string) *template.Template { return pages[name] }

func mustSub(dir string) fs.FS {
	sub, err := fs.Sub(webFS, dir)
	if err != nil {
		panic(err)
	}
	return sub
}

func hashFS(fsys fs.FS) string {
	sum := sha256.New()
	err := fs.WalkDir(fsys, ".", func(name string, entry fs.DirEntry, err error) error {
		if err != nil || entry.IsDir() {
			return err
		}
		content, err := fs.ReadFile(fsys, name)
		sum.Write([]byte(name))
		sum.Write(content)
		return err
	})
	if err != nil {
		panic(err)
	}
	return hex.EncodeToString(sum.Sum(nil))[:10]
}
