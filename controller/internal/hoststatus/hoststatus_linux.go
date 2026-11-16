// Package hoststatus reports the Controller machine's health for the Dashboard.
// Linux only, stdlib only, no cgo (ADR 0002). A read is a handful of small /proc
// and /sys files plus one statfs, cheap enough for a 256 MB board at a 10s poll.
package hoststatus

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
)

// Status is one reading. Every field is optional: nil when the machine cannot
// report it (null in JSON). Sizes are bytes.
type Status struct {
	CPUPercent    *float64 `json:"cpu_percent"` // nil on the first reading after start
	MemoryFree    *uint64  `json:"memory_free_bytes"`
	MemoryTotal   *uint64  `json:"memory_total_bytes"`
	DiskFree      *uint64  `json:"disk_free_bytes"`
	DiskTotal     *uint64  `json:"disk_total_bytes"`
	TemperatureC  *float64 `json:"temperature_c"`  // kernel thermal zone; nil when there is none
	UptimeSeconds *float64 `json:"uptime_seconds"` // machine uptime, not process uptime
}

// Reader takes readings and remembers the previous CPU counters so CPU % is the
// load since the last reading. Safe for concurrent use.
type Reader struct {
	dataDir string // the filesystem holding the ledger database

	mu        sync.Mutex
	prevTotal uint64
	prevIdle  uint64
	hasPrev   bool
}

// New returns a Reader whose disk figures describe the filesystem holding dataDir.
func New(dataDir string) *Reader {
	return &Reader{dataDir: dataDir}
}

// Read takes a reading. Fields that cannot be read stay nil.
func (r *Reader) Read() Status {
	var status Status
	status.CPUPercent = r.cpuPercent()
	status.MemoryFree, status.MemoryTotal = memory()
	var fs syscall.Statfs_t
	if syscall.Statfs(r.dataDir, &fs) == nil {
		block := uint64(fs.Bsize)
		free, total := fs.Bavail*block, fs.Blocks*block
		status.DiskFree, status.DiskTotal = &free, &total
	}
	status.TemperatureC = temperature()
	if fields := strings.Fields(readFile("/proc/uptime")); len(fields) > 0 {
		if seconds, err := strconv.ParseFloat(fields[0], 64); err == nil {
			status.UptimeSeconds = &seconds
		}
	}
	return status
}

// cpuPercent is the busy share of the time since the previous call, from the
// aggregate "cpu" line of /proc/stat. Nil on the first call or an unreadable file.
func (r *Reader) cpuPercent() *float64 {
	line, _, _ := strings.Cut(readFile("/proc/stat"), "\n")
	fields := strings.Fields(line)
	if len(fields) < 5 || fields[0] != "cpu" {
		return nil
	}
	// user nice system idle iowait irq softirq steal; guest time is already in user.
	var total, idle uint64
	for i, field := range fields[1:] {
		if i >= 8 {
			break
		}
		value, err := strconv.ParseUint(field, 10, 64)
		if err != nil {
			return nil
		}
		total += value
		if i == 3 || i == 4 {
			idle += value
		}
	}

	r.mu.Lock()
	defer r.mu.Unlock()
	prevTotal, prevIdle, hadPrev := r.prevTotal, r.prevIdle, r.hasPrev
	r.prevTotal, r.prevIdle, r.hasPrev = total, idle, true
	if !hadPrev || total <= prevTotal || idle < prevIdle {
		return nil
	}
	percent := 100 * float64(total-prevTotal-(idle-prevIdle)) / float64(total-prevTotal)
	return &percent
}

// memory returns MemAvailable (what a new process could use, cache excluded) and MemTotal.
func memory() (free, total *uint64) {
	for _, line := range strings.Split(readFile("/proc/meminfo"), "\n") {
		name, rest, ok := strings.Cut(line, ":")
		if !ok || (name != "MemAvailable" && name != "MemTotal") {
			continue
		}
		fields := strings.Fields(rest) // "<n> kB"
		if len(fields) == 0 {
			continue
		}
		kib, err := strconv.ParseUint(fields[0], 10, 64)
		if err != nil {
			continue
		}
		size := kib * 1024
		if name == "MemAvailable" {
			free = &size
		} else {
			total = &size
		}
	}
	if free == nil || total == nil {
		return nil, nil
	}
	return free, total
}

// temperature is the first readable thermal zone in °C (the kernel reports millidegrees).
func temperature() *float64 {
	zones, _ := filepath.Glob("/sys/class/thermal/thermal_zone*/temp")
	for _, zone := range zones {
		milli, err := strconv.ParseFloat(strings.TrimSpace(readFile(zone)), 64)
		if err == nil {
			celsius := milli / 1000
			return &celsius
		}
	}
	return nil
}

// readFile returns a small file's text, or "" when unreadable.
func readFile(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return string(data)
}
