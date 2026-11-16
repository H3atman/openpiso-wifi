package controller

import (
	"context"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

// routerStatusTimeout bounds the Router read so a dead Router cannot stall the 10-second poll.
const routerStatusTimeout = 4 * time.Second

// dashboardRouter is the Router System Status card and the JSON router block:
// each metric or null, plus Unreachable when the Router could not be read.
type dashboardRouter struct {
	routeros.Status
	Unreachable bool `json:"unreachable"`
}

// readRouterStatus reads the Router for the Dashboard. Any failure, including an
// unconfigured Router, is reported as unreachable rather than failing the page.
func (s *Server) readRouterStatus() dashboardRouter {
	router, err := s.router()
	if err != nil {
		return dashboardRouter{Unreachable: true}
	}
	defer router.Close()
	ctx, cancel := context.WithTimeout(context.Background(), routerStatusTimeout)
	defer cancel()
	status, err := router.Status(ctx)
	if err != nil {
		return dashboardRouter{Unreachable: true}
	}
	return dashboardRouter{Status: status}
}

// Rows lays out the card, the same shape as the Controller card.
func (r dashboardRouter) Rows() []statusRow {
	return []statusRow{
		{"CPU", formatPercent(r.CPUPercent), loadBar(r.CPUPercent)},
		{"Memory", formatFreeOf(r.MemoryFreeBytes, r.MemoryTotalBytes), usedBar(r.MemoryFreeBytes, r.MemoryTotalBytes)},
		{"Disk", formatFreeOf(r.DiskFreeBytes, r.DiskTotalBytes), usedBar(r.DiskFreeBytes, r.DiskTotalBytes)},
		{Label: "Temperature", Value: formatCelsius(r.TemperatureC)},
		{Label: "Uptime", Value: formatUptime(r.UptimeSeconds)},
	}
}
