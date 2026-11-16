# A Vendo serves a HotSpot server, learned on first contact

Operators coming from KiTifi and JuanFi expect "auto select": a customer who taps Insert Coin gets the Vendo on their own network, with no picker. Both do it with a lookup the Operator maintains, keyed by the HotSpot server or interface the customer landed on ([research](../research/vendo-auto-select-by-vlan.md)). We want the same result with nothing typed. So each Vendo stores the one RouterOS HotSpot server it serves, the Controller learns it from the Router, and Insert Coin resolves the Vendo on the Controller from the Device's HotSpot server.

**Flashing is pairing.** A flash script reads the board's MAC, asks the Controller (admin-authenticated) for a new Vendo with that MAC, and writes the returned Vendo ID, secret, Controller URL and the DHCP hostname `opnw-<last 4 hex of MAC>` onto the board. The Vendo is stored with no IP and no HotSpot server yet.

**First contact finishes it.** The board's first signed report reaches the Controller like any unpaid HotSpot host. The Controller looks the sender up in `/ip/hotspot/host` by source IP, which is the same lookup `DeviceAt` does for Devices. If the MAC matches the Vendo and the HMAC verifies, it stores the IP and the host's `server`, then installs the static lease, bypass and firewall rules that pairing installs today. The hostname is only a label for people reading the Router or the admin list. It grants nothing.

**Insert Coin picks the Vendo.** The Controller reads the Device's HotSpot server in the same Router call that already identifies the Device, then:

1. If exactly one Vendo is paired at the site, it uses that Vendo.
2. If exactly one paired Vendo serves the Device's HotSpot server, it uses that Vendo.
3. If several do, the Portal shows a picker limited to those Vendos, and the Controller rejects any other ID.
4. If none do, the Portal says no Vendo serves this network. There is no default Vendo.

The Coin Window stays on its reserved Vendo until it closes, even if the Device moves to another network. In admin, the Operator can change a Vendo's HotSpot server, for example when the board sits on a management network but serves a customer one.

## Considered Options

- **A browser-side lookup in the Portal, as JuanFi does.** Rejected because the browser would pick hardware, a stale page would keep an old choice after the Device moved, and a lookup table edited by hand has no checks. JuanFi falls back to a default IP and lets duplicate entries win silently.
- **Re-derive the board's network from DHCP leases on every coin press.** Rejected because it puts lease, DHCP-server and interface joins on the hot path. Learning the network once and letting the Operator override it is simpler and gives the same answer in the usual layout.
- **Match customer IP prefixes.** Rejected because it needs a prefix map typed by hand, and HotSpot address translation breaks naive prefix matching.
- **Trust the `opnw-` hostname to adopt boards automatically.** Rejected because any LAN client can set that hostname. Trust comes from the secret written at flash time.
- **Key on VLAN ID.** Rejected for now because one HotSpot server per VLAN interface makes the server name the same key, and the server name is also present on untagged layouts.

## Consequences

- **[ADR 0003](0003-router-is-sole-meter.md) holds.** Picking a Vendo never touches metering. Time bought still flows through the Coin Window and Top-up as before.
- **Identity is unchanged.** The HotSpot server selects hardware only. It never selects a Time Credit, and Rebind and Resume Code still work across networks.
- **[ADR 0004](0004-portal-is-operator-editable-static-files-over-json-api.md):** the Insert Coin call no longer requires a Vendo ID. It sends one only when the Portal shows the picker, so the Portal contract changes.
- **The pair endpoint changes.** It takes a MAC only, and the IP comes from first contact. The manual MAC-plus-IP form can go once the flash script exists.
- **A board must reach the Controller before it is paired.** This already holds because unpaid HotSpot hosts can reach the Controller ([ADR 0001](0001-mikrotik-enforces-controller-off-path.md)).
- **A single HotSpot server bridged across several VLANs cannot tell them apart.** Operators who want auto select run one HotSpot server per VLAN interface, which is how KiTifi and JuanFi sites are already wired. The CHR lab must confirm that `server` is present on hosts in that layout before this is built.

## Amendment (2026-11-02): Deactivated Vendos

An Operator can Deactivate a paired Vendo ([research](../research/vendo-list-management.md) §5.3). Rule 1 still counts **paired** Vendos, not active ones: if exactly one Vendo is paired, Insert Coin uses it only while it is active, and otherwise no Vendo serves. Rules 2–4 match over active Vendos only. So deactivating one of two Vendos leaves its network with no Vendo, instead of sending every network to the remaining one.
