package routeros

import (
	"context"
	"net/http"
	"strconv"
)

// Status is the Router's health for the Dashboard. Each field is nil when the
// Router does not report it (a CHR has no temperature sensor). The JSON tags are
// the Dashboard JSON's router block.
type Status struct {
	CPUPercent       *int     `json:"cpu_percent"`
	MemoryFreeBytes  *int64   `json:"memory_free_bytes"`
	MemoryTotalBytes *int64   `json:"memory_total_bytes"`
	DiskFreeBytes    *int64   `json:"disk_free_bytes"`
	DiskTotalBytes   *int64   `json:"disk_total_bytes"`
	TemperatureC     *float64 `json:"temperature_c"`
	UptimeSeconds    *int64   `json:"uptime_seconds"`
}

// temperatureNames are the /system/health rows that carry a temperature, best first.
var temperatureNames = []string{"temperature", "cpu-temperature", "board-temperature1"}

// Status reads /system/resource (CPU, memory, disk, uptime; all string values in
// REST JSON) and the temperature from /system/health. A failed resource read is an
// error, meaning the Router is unreachable; health trouble only leaves the
// temperature nil.
func (c *Client) Status(ctx context.Context) (Status, error) {
	var resource map[string]string
	if err := c.request(ctx, http.MethodGet, "/system/resource", nil, &resource); err != nil {
		return Status{}, err
	}
	var status Status
	if value, err := strconv.Atoi(resource["cpu-load"]); err == nil {
		status.CPUPercent = &value
	}
	status.MemoryFreeBytes = int64Field(resource, "free-memory")
	status.MemoryTotalBytes = int64Field(resource, "total-memory")
	status.DiskFreeBytes = int64Field(resource, "free-hdd-space")
	status.DiskTotalBytes = int64Field(resource, "total-hdd-space")
	if seconds, err := durationSeconds(resource["uptime"]); err == nil && resource["uptime"] != "" {
		status.UptimeSeconds = &seconds
	}

	// RouterOS v7 health is name/value/type rows; some boards answer with an empty list.
	if rows, err := c.list(ctx, "/system/health"); err == nil {
		values := map[string]string{}
		for _, row := range rows {
			values[row["name"]] = row["value"]
		}
		for _, name := range temperatureNames {
			if celsius, err := strconv.ParseFloat(values[name], 64); err == nil {
				status.TemperatureC = &celsius
				break
			}
		}
	}
	return status, nil
}

func int64Field(resource map[string]string, key string) *int64 {
	value, err := strconv.ParseInt(resource[key], 10, 64)
	if err != nil {
		return nil
	}
	return &value
}
