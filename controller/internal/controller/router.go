package controller

import (
	"errors"

	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

var errRouterNotConfigured = errors.New("Router not configured")

func (s *Server) router() (*routeros.Client, error) {
	config, configured, err := s.ledger.RouterConfig()
	if err != nil {
		return nil, err
	}
	if !configured {
		return nil, errRouterNotConfigured
	}
	return routeros.NewClient(config.Address, config.Username, config.APISecret, routeros.Certificate{DER: config.Certificate, Fingerprint: config.Fingerprint})
}
