package controller

import "github.com/H3atman/openpiso-wifi/controller/internal/ledger"

// matchingVendos returns the active Vendos that may take Insert Coin on a
// HotSpot server (ADR 0005 and its Deactivate amendment). A lone paired Vendo
// serves the whole site while it is active. Otherwise only an explicit HotSpot
// server match over active Vendos selects hardware; an unknown network has no
// default. outOfService reports that no active Vendo matched only because the
// Vendo that would have served is Deactivated.
func (s *Server) matchingVendos(server string) (matches []ledger.Vendo, outOfService bool, err error) {
	vendos, err := s.ledger.Vendos()
	if err != nil {
		return nil, false, err
	}
	paired := []ledger.Vendo{}
	deactivated := false
	for _, vendo := range vendos {
		if vendo.Status != "paired" {
			continue
		}
		paired = append(paired, vendo)
		if server != "" && vendo.HotspotServer == server {
			if vendo.Active {
				matches = append(matches, vendo)
			} else {
				deactivated = true
			}
		}
	}
	if len(paired) == 1 {
		if paired[0].Active {
			return paired, false, nil
		}
		return nil, true, nil
	}
	return matches, len(matches) == 0 && deactivated, nil
}
