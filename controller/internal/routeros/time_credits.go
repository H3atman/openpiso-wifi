package routeros

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
)

var ErrDeviceNotFound = errors.New("Device not found on the Router; connect through the WiFi")

type Device struct {
	HotspotServer string
	MAC           string
	IP            string
}

// ErrDeviceNotReady means the Router cannot unambiguously identify the Device
// yet (for example right after a MAC change). The caller should keep all
// ledger state and ask the customer to retry.
var ErrDeviceNotReady = errors.New("Device is still joining the WiFi; try again in a few seconds")

// DeviceAt uses the transport peer, never browser-supplied redirect parameters.
// The Controller must be routed to the HotSpot LAN without source NAT.
func (c *Client) DeviceAt(ctx context.Context, address string) (Device, error) {
	hosts, err := c.list(ctx, "/ip/hotspot/host?address="+url.QueryEscape(address))
	if err != nil {
		return Device{}, err
	}
	if len(hosts) == 0 {
		return Device{}, ErrDeviceNotFound
	}
	if len(hosts) != 1 || hosts[0]["address"] != address {
		// host-dead-time is not proof of which MAC sent this HTTP request.
		return Device{}, ErrDeviceNotReady
	}
	mac, err := net.ParseMAC(hosts[0]["mac-address"])
	if err != nil {
		return Device{}, errors.New("Router returned an invalid Device MAC")
	}
	return Device{MAC: strings.ToUpper(mac.String()), IP: address, HotspotServer: hosts[0]["server"]}, nil
}

// EnsureTimeCredit projects the absolute bought time onto the Time Credit's
// Router profile (see ProfileName), without resetting uptime.
func (c *Client) EnsureTimeCredit(ctx context.Context, id, password string, boughtSeconds int64, profile string) error {
	return c.ensureNamed(ctx, "/ip/hotspot/user", "op-"+id, map[string]string{
		"password": password, "profile": profile, "limit-uptime": strconv.FormatInt(boughtSeconds, 10) + "s",
	})
}

// LoginDevice restricts the HotSpot user to the bound MAC and rechecks its host
// entry immediately before login. A disappearing host is retryable, never a
// reason to invent an IP/MAC or reset the user's accumulated uptime.
func (c *Client) LoginDevice(ctx context.Context, id, password string, device Device) error {
	users, err := c.list(ctx, "/ip/hotspot/user?name="+url.QueryEscape("op-"+id))
	if err != nil {
		return err
	}
	if len(users) != 1 {
		return errors.New("Time Credit is not yet available on the Router")
	}
	if users[0]["mac-address"] != device.MAC {
		if err := c.request(ctx, http.MethodPatch, "/ip/hotspot/user/"+users[0][".id"], map[string]string{"mac-address": device.MAC}, nil); err != nil {
			return err
		}
	}
	current, err := c.DeviceAt(ctx, device.IP)
	if err != nil {
		return err
	}
	if current.MAC != device.MAC || current.IP != device.IP {
		return ErrDeviceNotReady
	}
	err = c.request(ctx, http.MethodPost, "/ip/hotspot/active/login", map[string]string{
		"user": "op-" + id, "password": password, "mac-address": device.MAC, "ip": device.IP,
	}, nil)
	if err != nil && strings.Contains(err.Error(), "unknown host IP") {
		return fmt.Errorf("%w: %v", ErrDeviceNotReady, err)
	}
	return err
}

// RemoveTimeCreditSessions commits live uptime before a fresh login. Raising
// limit-uptime alone does not extend an already-running Router countdown.
func (c *Client) RemoveTimeCreditSessions(ctx context.Context, id string) error {
	active, err := c.list(ctx, "/ip/hotspot/active?user="+url.QueryEscape("op-"+id))
	if err != nil {
		return err
	}
	for _, entry := range active {
		if err := c.request(ctx, http.MethodDelete, "/ip/hotspot/active/"+entry[".id"], nil, nil); err != nil {
			return err
		}
	}
	return nil
}

// RemoveTimeCredit is an idempotent Validity projection. Stop active access
// before deleting the HotSpot user; a retry also succeeds if it is already gone.
func (c *Client) RemoveTimeCredit(ctx context.Context, id string) error {
	if err := c.RemoveTimeCreditSessions(ctx, id); err != nil {
		return err
	}
	users, err := c.list(ctx, "/ip/hotspot/user?name="+url.QueryEscape("op-"+id))
	if err != nil {
		return err
	}
	for _, user := range users {
		if err := c.request(ctx, http.MethodDelete, "/ip/hotspot/user/"+user[".id"], nil, nil); err != nil {
			return err
		}
	}
	return nil
}

// UsedSeconds includes both committed user uptime and live active-entry uptime.
// Re-read around the active snapshot so a session ending mid-read cannot double
// count or lose its just-committed uptime.
func (c *Client) UsedSeconds(ctx context.Context, id string) (int64, error) {
	path := "/ip/hotspot/user?name=" + url.QueryEscape("op-"+id)
	for attempt := 0; attempt < 3; attempt++ {
		users, err := c.list(ctx, path)
		if err != nil {
			return 0, err
		}
		if len(users) != 1 {
			return 0, errors.New("Time Credit is not yet available on the Router")
		}
		active, err := c.list(ctx, "/ip/hotspot/active?user="+url.QueryEscape("op-"+id))
		if err != nil {
			return 0, err
		}
		after, err := c.list(ctx, path)
		if err != nil {
			return 0, err
		}
		if len(after) != 1 {
			return 0, errors.New("Time Credit is not available on the Router")
		}
		if users[0]["uptime"] != after[0]["uptime"] {
			continue
		}
		used, err := durationSeconds(users[0]["uptime"])
		if err != nil {
			return 0, err
		}
		for _, entry := range active {
			live, err := durationSeconds(entry["uptime"])
			if err != nil {
				return 0, err
			}
			used += live
		}
		return used, nil
	}
	return 0, errors.New("Router usage changed while reading; try again")
}

func durationSeconds(value string) (int64, error) {
	var total int64
	for value != "" {
		end := strings.IndexAny(value, "wdhms")
		if end <= 0 {
			return 0, fmt.Errorf("invalid Router uptime %q", value)
		}
		number, err := strconv.ParseInt(value[:end], 10, 64)
		if err != nil || number < 0 {
			return 0, errors.New("invalid Router uptime")
		}
		unit := map[byte]int64{'w': 604800, 'd': 86400, 'h': 3600, 'm': 60, 's': 1}[value[end]]
		if number > (1<<63-1-total)/unit {
			return 0, errors.New("Router uptime overflow")
		}
		total += number * unit
		value = value[end+1:]
	}
	return total, nil
}

func (c *Client) TimeCreditActive(ctx context.Context, id string) (bool, error) {
	active, err := c.list(ctx, "/ip/hotspot/active?user="+url.QueryEscape("op-"+id))
	return len(active) > 0, err
}

// EnsurePauseSettings updates profiles and existing Time Credit users without
// touching active sessions. The caller applies changed policy to live Devices.
// families maps a Time Credit ID to its HotSpot profile family (absent is the
// base family); profiles lists every HotSpot profile in use, to re-copy. Each
// user moves only between its family's idle and no-idle profiles.
func (c *Client) EnsurePauseSettings(ctx context.Context, idleEnabled bool, idleSeconds int, families map[string]string, profiles []string) error {
	if err := c.ensureProfilePair(ctx, "", nil, idleSeconds); err != nil {
		return err
	}
	if err := c.EnsureProfileCopies(ctx, profiles, idleSeconds); err != nil {
		return err
	}
	users, err := c.list(ctx, "/ip/hotspot/user")
	if err != nil {
		return err
	}
	for _, user := range users {
		id, ok := strings.CutPrefix(user["name"], "op-")
		profile := ProfileName(families[id], idleEnabled)
		if !ok || user["profile"] == profile {
			continue
		}
		if err := c.request(ctx, http.MethodPatch, "/ip/hotspot/user/"+user[".id"], map[string]string{"profile": profile}, nil); err != nil {
			return err
		}
	}
	return nil
}

type ActiveSession struct {
	TimeCreditID  string `json:"time_credit_id"`
	MAC           string `json:"mac"`
	IP            string `json:"ip"`
	UptimeSeconds int64  `json:"uptime_seconds"`
	Server        string `json:"server"` // the HotSpot server the Device is online on
}

// ActiveSessions is a read-only snapshot; polling never commits or resets usage.
func (c *Client) ActiveSessions(ctx context.Context) ([]ActiveSession, error) {
	entries, err := c.list(ctx, "/ip/hotspot/active")
	if err != nil {
		return nil, err
	}
	sessions := []ActiveSession{}
	for _, entry := range entries {
		if !strings.HasPrefix(entry["user"], "op-") {
			continue
		}
		seconds, err := durationSeconds(entry["uptime"])
		if err != nil {
			return nil, err
		}
		sessions = append(sessions, ActiveSession{TimeCreditID: strings.TrimPrefix(entry["user"], "op-"), MAC: entry["mac-address"], IP: entry["address"], UptimeSeconds: seconds, Server: entry["server"]})
	}
	return sessions, nil
}
