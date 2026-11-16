"""Shared E2E Device identities, fixture cleanup and failure evidence for the CHR lab."""
import datetime


DEVICE_MAC = "02:00:00:00:0C:01"
CHANGED_DEVICE_MAC = "02:00:00:00:0C:02"
UNKNOWN_VENDO_MAC = "02:00:00:00:0C:03"
COIN_BOARD_MAC = "02:00:00:00:0C:04"
BROWSER_MACS = tuple(f"02:00:00:00:0E:{number:02X}" for number in range(1, 5))
BOARD_MACS = tuple(f"02:00:00:41:00:{number:02X}" for number in range(1, 4))
# Shared with run-start cleanup: include every Device used by any run mode.
DEVICE_MACS = (DEVICE_MAC, CHANGED_DEVICE_MAC, UNKNOWN_VENDO_MAC, COIN_BOARD_MAC, *BROWSER_MACS, *BOARD_MACS)


def cleanup(client, evidence):
    """Remove only named/commented harness fixtures, keeping successful removals on failure."""
    evidence.update(device_macs=list(DEVICE_MACS), removed=[])

    def owned(row):
        return any(row.get(field, "").startswith(("opnw-", "openpiso-e2e-"))
                   for field in ("name", "comment"))

    def remove(path, row):
        client(f"{path}/{row['.id']}", "DELETE")
        evidence["removed"].append({"path": path, "row": row})

    # Rules and routes must go before their tables or source interfaces disappear.
    for path in ("/routing/rule", "/ip/route", "/routing/table"):
        for row in client(path):
            if owned(row):
                remove(path, row)

    interface_rows = {
        path: [row for row in client(path) if row["name"].startswith(("opnw-", "openpiso-e2e-"))]
        for path in ("/interface/macvlan", "/interface/vlan")
    }
    interfaces = {row["name"] for rows in interface_rows.values() for row in rows}
    for row in client("/ip/dhcp-client"):
        if row.get("interface") in interfaces:
            remove("/ip/dhcp-client", row)
    for row in client("/ip/dhcp-client/option"):
        if owned(row):
            remove("/ip/dhcp-client/option", row)
    for path, rows in interface_rows.items():
        for row in rows:
            remove(path, row)


def snapshot(client, router):
    """Keep each read's rows or error, without hiding the run's original failure."""
    evidence = {"taken_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "device_macs": list(DEVICE_MACS), "chr_client": {}, "hotspot_router": {}}

    def read(destination, name, operation):
        try:
            destination[name] = {"rows": operation()}
        except Exception as error:
            destination[name] = {"error": f"{type(error).__name__}: {error}",
                                 "notes": list(getattr(error, "__notes__", []))}

    for name, path in (("addresses", "/ip/address"), ("dhcp_clients", "/ip/dhcp-client"),
                       ("routing_rules", "/routing/rule"), ("routes", "/ip/route"),
                       ("routing_tables", "/routing/table"), ("macvlan_interfaces", "/interface/macvlan")):
        read(evidence["chr_client"], name, lambda: client(path))
    for name, path in (("dhcp_leases", "/ip/dhcp-server/lease"), ("hotspot_hosts", "/ip/hotspot/host")):
        read(evidence["hotspot_router"], name,
             lambda: [row for row in router(path) if row.get("mac-address", "").upper() in DEVICE_MACS])
    return evidence
