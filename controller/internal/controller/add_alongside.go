package controller

import (
	"context"
	"errors"
	"net/http"
	"net/netip"
	"net/url"
	"slices"
	"strconv"
	"strings"

	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

// Add alongside: create a HotSpot server on a new VLAN (the default) or on an
// interface that has none, on a range the Operator accepts or edits, and Adopt
// it. Existing HotSpot servers
// are not changed. The Router backup saved at connect is the undo.

// addAlongsideView is the Add alongside card on Router setup: Interfaces are
// those with no HotSpot server, Range the suggested address range, Parents
// those a new VLAN can go on, with Parent and VLAN the suggested parent and ID.
// Error says why they could not be read, which the card alone shows.
type addAlongsideView struct {
	Interfaces []string
	Range      string
	Parents    []string
	Parent     string
	VLAN       int
	Error      string
}

// carryVLAN tells the Operator to carry VLAN to an SSID: Server is an Adopted
// HotSpot server on that VLAN that no Device has reached the Portal from yet.
type carryVLAN struct {
	Server string
	VLAN   int
}

// readAddAlongside reads the free interfaces and a suggested range. It writes nothing.
func readAddAlongside(ctx context.Context, router *routeros.Client) (addAlongsideView, error) {
	interfaces, err := router.FreeInterfaces(ctx)
	if err != nil {
		return addAlongsideView{}, err
	}
	suggested, err := router.SuggestRange(ctx)
	if err != nil {
		return addAlongsideView{}, err
	}
	view := addAlongsideView{Interfaces: interfaces, Range: suggested.String()}
	if view.Parents, view.Parent, err = router.VLANParents(ctx); err != nil || view.Parent == "" {
		return view, err
	}
	view.VLAN, err = router.SuggestVLAN(ctx, view.Parent)
	return view, err
}

// readCarryVLANs lists the Adopted servers among servers that sit on a VLAN and
// that no Device has reached the Portal from yet (checklist item 9). It writes nothing.
func (s *Server) readCarryVLANs(ctx context.Context, router *routeros.Client, servers []routeros.HotspotServer, adopted []string) ([]carryVLAN, error) {
	ids, err := router.VLANIDs(ctx)
	if err != nil {
		return nil, err
	}
	var carry []carryVLAN
	for _, server := range servers {
		id, ok := ids[server.Interface]
		if !ok || !slices.Contains(adopted, server.Name) {
			continue
		}
		if arrived, err := s.ledger.PortalArrived(server.Name); err != nil {
			return nil, err
		} else if !arrived {
			carry = append(carry, carryVLAN{Server: server.Name, VLAN: id})
		}
	}
	return carry, nil
}

// formPlacement reads the create step's placement from a form: placement=vlan
// with parent and vlan (its ID), else interface.
func formPlacement(form url.Values) (routeros.Placement, error) {
	if form.Get("placement") != "vlan" {
		return routeros.Placement{Interface: form.Get("interface")}, nil
	}
	id, err := strconv.Atoi(strings.TrimSpace(form.Get("vlan")))
	if err != nil || id < 1 {
		return routeros.Placement{}, errors.New("the VLAN ID must be a number from 1 to 4094")
	}
	return routeros.Placement{Parent: form.Get("parent"), VLAN: id}, nil
}

// addAlongside serves POST /admin/router/add-alongside with a placement (see
// formPlacement) and range: it creates a HotSpot server there, Adopts it and
// brings it to the baseline. An interface that is not free, a VLAN ID in use on
// its parent, a parent bridge with VLAN filtering on (with the manual steps), or
// a range that is not private or overlaps anything on the Router is refused
// with 400 before any Router write.
func (s *Server) addAlongside(response http.ResponseWriter, request *http.Request) {
	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	if err := request.ParseForm(); err != nil {
		http.Error(response, "invalid HotSpot server", http.StatusBadRequest)
		return
	}
	network, err := netip.ParsePrefix(strings.TrimSpace(request.PostForm.Get("range")))
	if err != nil {
		http.Error(response, "the address range must look like 10.0.0.0/24", http.StatusBadRequest)
		return
	}
	placement, err := formPlacement(request.PostForm)
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadRequest)
		return
	}
	router, err := s.router()
	if err != nil {
		http.Error(response, err.Error(), http.StatusBadGateway)
		return
	}
	defer router.Close()
	name, err := router.CreateHotspotServer(request.Context(), placement, network)
	var refusal routeros.Refusal
	if errors.As(err, &refusal) {
		http.Error(response, refusal.Error(), http.StatusBadRequest)
		return
	}
	if err != nil {
		http.Error(response, "unable to create the HotSpot server: "+err.Error(), http.StatusBadGateway)
		return
	}
	s.finishAdopting(response, request, router, []string{name}, "HotSpot server "+name+" created and Adopted", true)
}

// exemptFromNAT runs the portal-reachable fix for a server the Controller just
// created: the Controller's NAT exemption for its network, which only adds a
// Controller-owned rule. Without it a masquerade rewrites its Devices' addresses
// and the Portal cannot find them.
func (s *Server) exemptFromNAT(ctx context.Context, router *routeros.Client, server string) error {
	checks, err := s.checklist(ctx, router, []string{server})
	if err != nil {
		return err
	}
	index := slices.IndexFunc(checks, func(check routeros.Check) bool { return check.Name == "portal-reachable" })
	if index < 0 {
		return errors.New("HotSpot server " + server + " is missing on the Router")
	}
	return checks[index].Fix(ctx)
}
