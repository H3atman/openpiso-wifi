package controller

import (
	"cmp"
	"net/http"
	"slices"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
)

// shopPage is the dot of a Shop page, Rate tiers or Points table (shop.html):
// the Shop's set in the shared tier editor, the paired Vendos behind the
// coverage pills, and the words that differ between the two pages.
type shopPage struct {
	Editor    tierEditor
	Title     string
	Scope     string // the line under the title
	ShopSet   string // "Shop tiers": what the Shop pill's Vendos use
	CustomSet string // "Custom tiers": what the Custom pill's Vendos have
	NextUse   string // "Coin Window": when a Shop change reaches a Vendo
	OwnSet    string // "tiers": what a Custom Vendo has of its own
	Unit      string // "tier": counted on each Custom row, "s" added when not 1
	EditLabel string // the Custom row's link to the Vendo's tab
	Shop      []shopVendo
	Custom    []shopVendo
}

// shopVendo is one row of a coverage pill's list.
type shopVendo struct {
	ID       uint32
	Name     string // "Vendo N" when unnamed
	Location string
	Status   string // Online, Offline or Deactivated
	Size     int    // rows in the Vendo's own set; Custom rows only
}

// renderShop fills page's lists from the paired Vendos, split by own, and
// renders it as the Shop page at page.Editor.Action.
func (s *Server) renderShop(response http.ResponseWriter, request *http.Request, page shopPage, own func(ledger.Vendo) (int, bool)) {
	vendos, err := s.ledger.Vendos()
	if err != nil {
		http.Error(response, "read Vendos", http.StatusInternalServerError)
		return
	}
	page.Shop, page.Custom = shopLists(vendos, time.Now(), own)
	s.renderPage(response, request, "shop", Page{Title: page.Title, Section: "Shop", Active: page.Editor.Action, Data: page})
}

// shopLists splits paired Vendos, Deactivated ones included, by whether own
// reports a set of their own (and its size), each list sorted by name.
// Unpaired and not yet paired Vendos are left out.
func shopLists(vendos []ledger.Vendo, now time.Time, own func(ledger.Vendo) (int, bool)) (shop, custom []shopVendo) {
	for _, vendo := range vendos {
		if vendo.Status != "paired" {
			continue
		}
		row := shopVendo{ID: vendo.ID, Name: vendo.DisplayName(), Location: vendo.Location, Status: vendoStatus(vendo, vendoReporting(vendo, now))}
		if n, ok := own(vendo); ok {
			row.Size = n
			custom = append(custom, row)
		} else {
			shop = append(shop, row)
		}
	}
	byName := func(a, b shopVendo) int { return cmp.Compare(a.Name, b.Name) }
	slices.SortStableFunc(shop, byName)
	slices.SortStableFunc(custom, byName)
	return shop, custom
}
