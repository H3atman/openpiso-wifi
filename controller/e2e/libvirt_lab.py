"""The libvirt CHR lab behind run.py's lab interface.

chr-lab is the Router; chr-client hosts every Device and Vendo board. Device 1
is lan1, Device 2 is lan2, and a Device acts through chr-client's /tool/fetch
with source-policy routing. A MAC change switches Device 1 to the lan2 NIC.
"""
import contextlib
import functools
import json
import re
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def router_address(domain="chr-lab") -> str:
    output = subprocess.run(
        ["virsh", "-c", "qemu:///system", "domifaddr", domain],
        timeout=10,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    match = re.search(r"(192\.168\.122\.\d+)/24", output)
    if not match:
        raise AssertionError(f"{domain} has no management address")
    return match.group(1)


def start_client() -> str:
    state = subprocess.run(
        ["virsh", "-c", "qemu:///system", "domstate", "chr-client"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if state != "running":
        subprocess.run(["virsh", "-c", "qemu:///system", "start", "chr-client"], check=True, capture_output=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        output = subprocess.run(
            ["virsh", "-c", "qemu:///system", "domifaddr", "chr-client"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        match = re.search(r"(192\.168\.122\.\d+)/24", output)
        if match:
            address = match.group(1)
            try:
                with urllib.request.urlopen(f"http://{address}/", timeout=1):
                    return address
            except OSError:
                pass
        time.sleep(1)
    raise AssertionError("chr-client has no management address")


def rcon(password: str, *commands: str) -> None:
    result = subprocess.run(
        ["python3", "-I", str(ROOT / "lab/chr/rcon.py"), "chr-lab", password, *commands],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode != 0:
        raise AssertionError(f"Router console command failed: {result.stdout}\n{result.stderr}")


def configure_router_tls(run, address: str, password: str) -> None:
    """Give chr-lab a fresh www-ssl certificate for address and a fresh bootstrap admin.

    run is the loaded run.py module (for router_rest and the bootstrap admin);
    lab/dev/dev.py calls this too."""
    rcon(
        password,
        "/ip service set www-ssl disabled=yes certificate=none",
        '/certificate remove [find where name~"openpiso-e2e"]',
    )
    rcon(
        password,
        '/certificate add name="openpiso-e2e-ca" common-name="openpiso-e2e-ca" key-usage=key-cert-sign,crl-sign',
        "/certificate sign openpiso-e2e-ca",
    )
    rcon(
        password,
        f'/certificate add name="openpiso-e2e-server" common-name="{address}" subject-alt-name="IP:{address}" key-usage=tls-server',
        "/certificate sign openpiso-e2e-server ca=openpiso-e2e-ca",
    )
    rcon(
        password,
        "/ip service set www-ssl disabled=no certificate=openpiso-e2e-server tls-version=only-1.2",
    )

    context = ssl._create_unverified_context()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((address, 443), timeout=2) as raw:
                with context.wrap_socket(raw, server_hostname=address):
                    break
        except OSError:
            time.sleep(0.25)
    else:
        raise AssertionError("Router www-ssl did not become ready")

    existing = run.router_rest(address, "admin", password, f"/user?name={run.ROUTER_ADMIN}")
    for entry in existing:
        run.router_rest(address, "admin", password, f"/user/{entry['.id']}", "DELETE")
    run.router_rest(
        address,
        "admin",
        password,
        "/user",
        "PUT",
        {"name": run.ROUTER_ADMIN, "group": "full", "password": run.ROUTER_ADMIN_PASSWORD},
    )


def configure_device_routing(password: str, portal_url: str) -> None:
    # libvirt installs the host return route without sudo. A temporary third
    # chr-lab NIC is its next hop; only the two throwaway CHR VMs are involved.
    # Never replace source identity with redirect parameters to work around NAT.
    xml = '''<network><name>openpiso-e2e-routing</name><bridge name="virbr-opiso7"/>
      <ip address="10.77.7.1" netmask="255.255.255.0"/>
      <route address="10.5.50.0" prefix="24" gateway="10.77.7.2"/>
      <route address="10.5.10.0" prefix="24" gateway="10.77.7.2"/>
      <route address="10.5.20.0" prefix="24" gateway="10.77.7.2"/>
    </network>'''
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml") as definition:
        definition.write(xml)
        definition.flush()
        # Right after an interrupted run libvirt can still be tearing the old
        # network down, so retry briefly and show virsh's own error.
        for attempt in range(10):
            created = subprocess.run(["virsh", "-c", "qemu:///system", "net-create", definition.name],
                                     capture_output=True, text=True)
            if created.returncode == 0:
                break
            time.sleep(3)
        else:
            raise AssertionError("virsh net-create failed: " + created.stderr.strip())
    subprocess.run(["virsh", "-c", "qemu:///system", "attach-interface", "chr-lab",
                    "network", "openpiso-e2e-routing", "--model", "virtio", "--mac", "52:54:00:07:07:07", "--live"],
                   check=True, capture_output=True)
    rcon(password,
         '/ip address add address=10.77.7.2/24 interface=[/interface ethernet find where mac-address="52:54:00:07:07:07"] comment="openpiso-e2e-routing"',
         f'/ip firewall nat add chain=srcnat src-address=10.5.0.0/16 dst-address={urllib.parse.urlsplit(portal_url).hostname} action=accept place-before=0 comment="openpiso-e2e-routing"')


# A live unplug needs the guest's acknowledgement, so re-send it until the NIC is gone;
# destroying the network under a lingering NIC breaks the next run's domifaddr.
def detach_device_routing() -> None:
    for _ in range(20):
        interfaces = subprocess.run(["virsh", "-c", "qemu:///system", "domiflist", "chr-lab"],
                                    check=True, capture_output=True, text=True, timeout=30).stdout
        if "52:54:00:07:07:07" not in interfaces:
            return
        subprocess.run(["virsh", "-c", "qemu:///system", "detach-interface", "chr-lab", "network",
                        "--mac", "52:54:00:07:07:07", "--live"], capture_output=True, timeout=30)
        time.sleep(3)
    raise RuntimeError("chr-lab kept the routing NIC after detach")


def destroy_device_network() -> None:
    networks = subprocess.run(["virsh", "-c", "qemu:///system", "net-list", "--name"],
                              check=True, capture_output=True, text=True, timeout=30).stdout.splitlines()
    if "openpiso-e2e-routing" in networks:
        subprocess.run(["virsh", "-c", "qemu:///system", "net-destroy", "openpiso-e2e-routing"],
                       check=True, capture_output=True, timeout=30)


def device_fetch(client, device_ip: str, interface: str, table: str, url: str):
    # Probe with a GET before scenario mutations: RouterOS also wraps expected
    # portal errors in HTTP 400, so those requests must not be retried here.
    deadline = time.monotonic() + 30
    detail = "Device address or routing fixture not ready"
    while time.monotonic() < deadline:
        addresses = client(f"/ip/address?interface={interface}")
        leases = client(f"/ip/dhcp-client?interface={interface}")
        routes = client("/ip/route?comment=openpiso-e2e-device")
        rules = client("/routing/rule?comment=openpiso-e2e-device")
        ready = (
            any(entry["address"].split("/")[0] == device_ip and entry.get("invalid") != "true"
                and entry.get("disabled") != "true" for entry in addresses)
            # A lease mid-renewal ("renewing...") keeps its address; long ARM runs hit this.
            and any(entry.get("status", "").startswith(("bound", "renewing")) for entry in leases)
            and any(entry.get("routing-table") == table and entry.get("dst-address") == "0.0.0.0/0"
                    and entry.get("gateway") == f"10.5.50.1%{interface}" and entry.get("active") == "true"
                    for entry in routes)
            and any(entry.get("src-address") == f"{device_ip}/32" and entry.get("table") == table
                    and entry.get("action") == "lookup-only-in-table" and entry.get("disabled") != "true"
                    and entry.get("invalid") != "true" for entry in rules)
        )
        if not ready:
            detail = ("Device address or routing fixture not ready: addresses="
                      f"{[(e['address'], e.get('invalid'), e.get('disabled')) for e in addresses]} "
                      f"leases={[e.get('status') for e in leases]} "
                      f"routes={[(e.get('routing-table'), e.get('gateway'), e.get('active')) for e in routes]} "
                      f"rules={[(e.get('src-address'), e.get('table'), e.get('invalid')) for e in rules]}")
        if ready:
            try:
                return client("/tool/fetch", "POST", {"url": url, "output": "user", "src-address": device_ip})
            except urllib.error.HTTPError as error:
                if error.code != 400:
                    raise
                detail = f"HTTP 400: {error.read().decode(errors='replace')}"
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise AssertionError(f"Device fetch from {device_ip} on {interface} via {table} to {url} failed: {detail}")


class LibvirtLab:
    """The lab interface scenarios use, on chr-lab and chr-client.

    A Device is named by its HotSpot IP. A fetch returns the RouterOS /tool/fetch
    result row ({"data", "http-headers"}); a non-2xx reply raises HTTPError 400
    whose JSON "detail" names the Portal's status. run is the loaded run.py
    module; PORTAL_URL is read from it at call time because ARM mode changes it.
    """

    def __init__(self, run, password: str):
        self.run = run
        self.router_password = password
        self._shared_rule = None
        self._vlan = None

    def running(self, report):
        """The lab for the length of a run: chr-lab and chr-client are long-lived, so nothing to do."""
        return contextlib.nullcontext()

    def own_scenarios(self, opener, report) -> None:
        """Scenarios only this lab runs: none."""

    def non_v7_router(self, module):
        """The RouterOS 6 fixture (module is the loaded non_v7_router.py), served by the harness on the libvirt bridge."""
        return module.NonV7Router("192.168.122.1")

    @functools.cached_property
    def router_address(self) -> str:
        """The Router's REST address, for the harness."""
        return router_address()

    @property
    def router_url(self) -> str:
        """The Router as the Controller reaches it."""
        return f"https://{self.router_address}"

    def _client(self, path, method="GET", body=None):
        return self.run.router_rest(self.client_address, "admin", self.router_password, path, method, body, scheme="http")

    def _nic(self, name, disabled):
        interface = self._client(f"/interface/ethernet?name={name}")[0]
        self._client(f"/interface/ethernet/{interface['.id']}", "PATCH", {"disabled": disabled})

    # Run setup and teardown.

    def new_router_certificate(self) -> None:
        """Give the Router a fresh TLS certificate and bootstrap admin."""
        configure_router_tls(self.run, self.router_address, self.router_password)

    def remove_bootstrap_admin(self) -> None:
        rcon(self.router_password, f'/user remove [find where name="{self.run.ROUTER_ADMIN}"]')

    def stop_router_rest(self) -> None:
        rcon(self.router_password, "/ip service set www-ssl disabled=yes")

    def start_router_rest(self) -> None:
        rcon(self.router_password, "/ip service set www-ssl disabled=no")

    def start(self, evidence: dict) -> None:
        """Bring the Devices' host up and remove stale harness fixtures, recording each removal in evidence."""
        self.client_address = start_client()
        self.run._lab_module.cleanup(self._client, evidence)

    def reset_device(self):
        """Connect Device 1 with a fresh lease, before the routing fixture exists; returns the lease evidence."""
        self._nic("lan1", "no")
        self.renew_lease(verify_portal=False)
        return self._client("/ip/dhcp-client?interface=lan1")

    def route_devices(self) -> None:
        """Let Devices and Vendos reach the Controller with their own source IPs."""
        configure_device_routing(self.router_password, self.run.PORTAL_URL)
        self.client_address = start_client()

    def teardown_steps(self):
        """(name, operation) cleanup steps for the end of the run."""
        password = self.router_password
        return [
            ("bootstrap admin", self.remove_bootstrap_admin),
            ("fixture Router address", lambda: rcon(password, '/ip address remove [find where comment="openpiso-e2e-routing"]')),
            ("fixture source NAT", lambda: rcon(password, '/ip firewall nat remove [find where comment="openpiso-e2e-routing"]')),
            ("fixture Router NIC", detach_device_routing),
            ("fixture network", destroy_device_network),
        ]

    def snapshot(self) -> dict:
        """Failure evidence from the Devices' host and the Router."""
        return self.run._lab_module.snapshot(
            lambda path: self.run.router_rest(router_address("chr-client"), "admin", self.router_password, path, scheme="http"),
            lambda path: self.run.router_rest(router_address(), "admin", self.router_password, path))

    # Devices.

    def device_ip(self, second: bool = False) -> str:
        interface = "lan2" if second else "lan1"
        return self._client(f"/ip/address?interface={interface}")[0]["address"].split("/")[0]

    def second_device_mac(self) -> str:
        return self._client("/interface/ethernet?name=lan2")[0]["mac-address"]

    def fetch(self, device_ip: str, url: str, form=None, cookie: str = "", headers: bool = True):
        """This Device fetches url, POSTing form when given; headers adds the reply's "http-headers"."""
        body = {"url": url, "output": "user-with-headers" if headers else "user", "src-address": device_ip}
        fields = []
        if form is not None:
            body.update({"http-method": "post", "http-data": urllib.parse.urlencode(form)})
            fields.append("Content-Type:application/x-www-form-urlencoded")
        if cookie:
            fields.append("Cookie:" + cookie)
        if fields:
            body["http-header-field"] = ",".join(fields)
        return self._client("/tool/fetch", "POST", body)[-1]

    def fetch_when_ready(self, device_ip: str, url: str):
        """Device 1 GETs url once its lease and routing are ready, retrying refusals; returns every result row."""
        return device_fetch(self._client, device_ip, "lan1", "openpiso-e2e-device", url)

    def unreachable(self, device_ip: str, url: str) -> dict:
        """Assert this Device gets no reply from url within 3s; returns the evidence."""
        # Expected failures must not use device_fetch's readiness/retry loop.
        try:
            result = self._client("/tool/fetch", "POST", {
                "url": url, "output": "user", "src-address": device_ip, "duration": "3s",
            })
        except urllib.error.HTTPError as error:
            return {"url": url, "router_error": json.load(error).get("detail", "")}
        # RouterOS duration-limited fetch can return only 'connecting'
        # progress without raising an error when no HTTP reply arrives.
        assert result and all(row.get("status") != "finished" and not row.get("data") for row in result), result
        return {"url": url, "progress": result}

    def internet(self, device_ip: str) -> tuple[str, str]:
        """Fetch from the Device for checks that expect internet; returns (target, body).

        1.1.1.1 drops HTTP some of the time on the lab network, so fall back to
        8.8.8.8 over HTTPS (plain HTTP there never answers). Checks that expect a
        redirect or a block must keep fetching 1.1.1.1 directly: a fallback could
        make a blocked Device look online.
        """
        try:
            target = "http://1.1.1.1/"
            result = self._client("/tool/fetch", "POST", {"url": target, "output": "user", "src-address": device_ip})
        except OSError:
            target = "https://8.8.8.8/"
            result = self._client("/tool/fetch", "POST", {"url": target, "output": "user", "src-address": device_ip})
        return target, result[-1].get("data", "")

    def ping(self, device_ip: str) -> None:
        """Real traffic from this Device to the HotSpot."""
        self._client("/ping", "POST", {"address": "10.5.50.1", "src-address": device_ip, "count": "1"})

    def stop_background_traffic(self) -> None:
        """Stop the fixture traffic generator, so only the scenario keeps a Device active."""
        for scheduler in self._client("/system/scheduler?name=ping-gen"):
            self._client("/system/scheduler/" + scheduler[".id"], "PATCH", {"disabled": "yes"})

    def redirect(self) -> tuple[str, str]:
        """Connect Device 1 alone; returns its (redirect page, Portal page) and asserts it cannot reach the admin."""
        client = self._client
        portal_url = self.run.PORTAL_URL
        for name, disabled in (("lan1", "no"), ("lan2", "yes")):
            self._nic(name, disabled)

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            addresses = client("/ip/address?interface=lan1")
            if addresses:
                break
            time.sleep(1)
        else:
            raise AssertionError("chr-client lan1 has no HotSpot address")

        routes = client("/ip/route?comment=openpiso-e2e-controller-route")
        for route in routes:
            client(f"/ip/route/{route['.id']}", "DELETE")
        client("/ping", "POST", {"address": "10.5.50.1", "count": "1"})

        device_ip = addresses[0]["address"].split("/")[0]
        tables = client("/routing/table?name=openpiso-e2e-device")
        if not tables:
            client("/routing/table", "PUT", {"name": "openpiso-e2e-device", "fib": "yes"})
        for route in client("/ip/route?comment=openpiso-e2e-device"):
            client(f"/ip/route/{route['.id']}", "DELETE")
        client("/ip/route", "PUT", {
            "dst-address": "0.0.0.0/0", "gateway": "10.5.50.1%lan1",
            "routing-table": "openpiso-e2e-device", "comment": "openpiso-e2e-device",
        })
        for rule in client("/routing/rule?comment=openpiso-e2e-device"):
            client(f"/routing/rule/{rule['.id']}", "DELETE")
        client("/routing/rule", "PUT", {
            "src-address": f"{device_ip}/32", "action": "lookup-only-in-table",
            "table": "openpiso-e2e-device", "comment": "openpiso-e2e-device",
        })
        redirect = device_fetch(client, device_ip, "lan1", "openpiso-e2e-device", "http://1.1.1.1/")
        portal = device_fetch(client, device_ip, "lan1", "openpiso-e2e-device", f"{portal_url}/portal")
        try:
            client("/tool/fetch", "POST", {"url": f"{portal_url}/admin/login", "output": "user", "src-address": device_ip})
        except urllib.error.HTTPError as error:
            detail = json.load(error).get("detail", "")
            assert "403" in detail, detail
        else:
            raise AssertionError("customer Device could reach the admin login")
        return redirect[-1].get("data", ""), portal[-1].get("data", "")

    def renew_lease(self, *, verify_portal: bool = True) -> None:
        """Give Device 1 a fresh HotSpot lease after its paired (static) lease was deleted.

        Unicast renewals are refused by the HotSpot, so the next broadcast rebind for
        a lease the server no longer has gets a new address minutes later, mid-scenario.
        The new lease may differ, so the Device routing rule follows it. Run-start
        cleanup skips the Portal probe because its routing fixture does not exist yet.
        """
        client = self._client
        lease = client("/ip/dhcp-client?interface=lan1")[0]
        client("/ip/dhcp-client/release", "POST", {".id": lease[".id"]})
        deadline = time.monotonic() + 30
        while (lease := client("/ip/dhcp-client?interface=lan1")[0]).get("status") != "bound":
            assert time.monotonic() < deadline, "chr-client lan1 did not get a new lease"
            time.sleep(0.5)
        device_ip = lease["address"].split("/")[0]
        for rule in client("/routing/rule?comment=openpiso-e2e-device"):
            client(f"/routing/rule/{rule['.id']}", "PATCH", {"src-address": f"{device_ip}/32"})
        if verify_portal:
            device_fetch(client, device_ip, "lan1", "openpiso-e2e-device", f"{self.run.PORTAL_URL}/portal")

    def second_device_up(self) -> list[str]:
        """Connect Device 2 beside Device 1, with a source-policy rule each. Returns [Device 1 IP, Device 2 IP]."""
        client = self._client
        self._nic("lan2", "no")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            second = client("/ip/address?interface=lan2")
            if second:
                break
            time.sleep(1)
        assert second, "second Device has no HotSpot address"
        ips = [client("/ip/address?interface=lan1")[0]["address"].split("/")[0], second[0]["address"].split("/")[0]]
        assert ips[0] != ips[1]
        if not client("/routing/table?name=openpiso-e2e-device2"):
            client("/routing/table", "PUT", {"name": "openpiso-e2e-device2", "fib": "yes"})
        if not client("/ip/route?routing-table=openpiso-e2e-device2"):
            client("/ip/route", "PUT", {"dst-address": "0.0.0.0/0", "gateway": "10.5.50.1%lan2",
                   "routing-table": "openpiso-e2e-device2", "comment": "openpiso-e2e-device"})
        # lan1's DHCP address may have changed since the earlier steps (slower guests
        # run longer); rebuild both source-policy rules for the current addresses.
        for rule in client("/routing/rule?comment=openpiso-e2e-device"):
            client("/routing/rule/" + rule[".id"], "DELETE")
        for ip, table in zip(ips, ("openpiso-e2e-device", "openpiso-e2e-device2")):
            client("/routing/rule", "PUT", {"src-address": ip + "/32", "action": "lookup-only-in-table",
                   "table": table, "comment": "openpiso-e2e-device"})
        for ip, interface, table in zip(ips, ("lan1", "lan2"), ("openpiso-e2e-device", "openpiso-e2e-device2")):
            device_fetch(client, ip, interface, table, self.run.PORTAL_URL + "/portal")
            client("/ping", "POST", {"address": "10.5.50.1", "src-address": ip, "interface": interface, "count": "1"})
        return ips

    def wait_device(self, second: bool = False, url=None) -> str:
        """Wait until this Device has a usable lease and can fetch url (default the Portal); returns its IP."""
        url = url or self.run.PORTAL_URL + "/portal"  # PORTAL_URL changes in ARM mode
        client = self._client
        interface = "lan2" if second else "lan1"
        table = "openpiso-e2e-device2" if second else "openpiso-e2e-device"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            leases = client("/ip/dhcp-client?interface=" + interface)
            # As in device_fetch, a renewing lease still owns its usable address.
            bound = next((lease for lease in leases
                          if lease.get("status", "").startswith(("bound", "renewing")) and lease.get("address")), None)
            if bound is not None:
                # DHCP may choose a new IP when the NIC returns. Update only this
                # Device's fixture rule; the Controller still verifies the real host.
                device_ip = bound["address"].split("/")[0]
                rules = [rule for rule in client("/routing/rule?comment=openpiso-e2e-device") if rule["table"] == table]
                assert len(rules) == 1, "Device source-policy fixture must have exactly one rule per table"
                if rules[0]["src-address"] != device_ip + "/32":
                    client("/routing/rule/" + rules[0][".id"], "PATCH", {"src-address": device_ip + "/32"})
                device_fetch(client, device_ip, interface, table, url)
                return device_ip
            time.sleep(1)
        raise AssertionError("Device DHCP lease did not become usable after the NIC switch: " +
                             repr([(lease.get("status"), lease.get("address")) for lease in leases]))

    def change_mac(self) -> str:
        """Device 1 changes MAC and renews; returns its new IP. Undo with restore_mac."""
        # Virtio ignores runtime MAC edits, so Device 1 moves to the lan2 NIC (Device 2's MAC).
        self.leave()
        return self.wait_device(second=True)

    def leave(self) -> None:
        """Device 1 leaves the WiFi. Undo with rejoin."""
        self._nic("lan1", "yes")

    def restore_mac(self) -> None:
        """Device 1 is back on its own MAC; safe to repeat. Call wait_device for its IP."""
        self._nic("lan1", "no")

    rejoin = restore_mac

    def share_ip(self, device_ip: str) -> None:
        """Device 1 leaves and Device 2 sends from device_ip, so the Router sees one IP on two MACs. Undo with unshare_ip."""
        client = self._client
        self._shared_rule = None
        self._shared_rule = next(r for r in client("/routing/rule?comment=openpiso-e2e-device")
                                 if r["src-address"] == device_ip + "/32")
        self.leave()
        # Send from the same IP on the other real MAC. Observe Router host rows,
        # including host-dead-time, rather than inventing the ambiguity in a mock.
        client("/ip/address", "PUT", {"address": device_ip + "/32", "interface": "lan2", "comment": "openpiso-e2e-ambiguous"})
        client("/routing/rule/" + self._shared_rule[".id"], "PATCH", {"table": "openpiso-e2e-device2"})
        client("/ping", "POST", {"address": "10.5.50.1", "src-address": device_ip, "interface": "lan2", "count": "1"})

    def unshare_ip(self) -> None:
        """Give the shared IP back to Device 1 and reconnect it; safe to repeat. Call wait_device for its IP."""
        client = self._client
        if self._shared_rule is not None:
            client("/routing/rule/" + self._shared_rule[".id"], "PATCH", {"table": self._shared_rule["table"]})
        for alias in client("/ip/address?comment=openpiso-e2e-ambiguous"):
            client("/ip/address/" + alias[".id"], "DELETE")
        self.rejoin()

    # Vendo boards.

    def coin_board(self) -> dict:
        """A Vendo board on Device 1's network; returns {"mac", "ip"}. Undo with remove_coin_board."""
        client = self._client
        name = "opnw-coin-board"
        mac = self.run._lab_module.COIN_BOARD_MAC
        if not client("/interface/macvlan?name=" + name):
            client("/interface/macvlan", "PUT", {"name": name, "interface": "lan1", "mac-address": mac})
            client("/ip/dhcp-client", "PUT", {"interface": name, "add-default-route": "no", "use-peer-dns": "no", "disabled": "no"})
            deadline = time.monotonic() + 30
            while not client("/ip/address?interface=" + name):
                assert time.monotonic() < deadline, "coin board DHCP did not bind"
                time.sleep(0.5)
            if not client("/routing/table?name=" + name):
                client("/routing/table", "PUT", {"name": name, "fib": "yes"})
            client("/ip/route", "PUT", {"dst-address": "0.0.0.0/0", "gateway": "10.5.50.1%" + name, "routing-table": name, "comment": name})
        ip = client("/ip/address?interface=" + name)[0]["address"].split("/")[0]
        if not client("/routing/rule?comment=" + name):
            client("/routing/rule", "PUT", {"src-address": ip + "/32", "action": "lookup-only-in-table", "table": name, "comment": name})
        return {"mac": mac, "ip": ip}

    def remove_coin_board(self) -> None:
        client = self._client
        for path, query in (("/routing/rule", "comment=opnw-coin-board"), ("/ip/route", "comment=opnw-coin-board"),
                            ("/ip/dhcp-client", "interface=opnw-coin-board"), ("/interface/macvlan", "name=opnw-coin-board")):
            for row in client(path + "?" + query):
                client(path + "/" + row[".id"], "DELETE")

    def vendo_report(self, ip: str, frame: bytes) -> None:
        """The Vendo board at ip sends this signed Report to the Controller's PORTAL_URL."""
        client = self._client
        url = self.run.PORTAL_URL
        # Check the source-policy route before sending a signed request. A fetch
        # alone can reuse a connection whose route has since become unavailable.
        deadline = time.monotonic() + 30
        detail = 'source-policy route inactive'
        while time.monotonic() < deadline:
            rules = [r for r in client('/routing/rule') if r.get('src-address') == ip + '/32']
            routes = client('/ip/route')
            ready = rules and all(any(r.get('routing-table') == rule['table'] and
                                      r.get('dst-address') == '0.0.0.0/0' and
                                      r.get('active') == 'true' and r.get('disabled') != 'true'
                                      for r in routes) for rule in rules)
            if ready:
                try:
                    client('/tool/fetch', 'POST', {'url': url + '/healthz', 'src-address': ip, 'output': 'none'})
                    break
                except urllib.error.HTTPError as error:
                    if error.code != 400:
                        raise
                    detail = error.read().decode()
            time.sleep(0.5)
        else:
            raise AssertionError(f'Board route from {ip} did not become ready: {detail}')
        # Decode hex escapes on RouterOS, rather than UTF-8 encoding binary via JSON.
        body = ''.join(f'\\{byte:02X}' for byte in frame)
        script = client('/system/script', 'PUT', {
            'name': 'opnw-first-report',
            'source': f'/tool fetch url="{url}/vendo/messages" src-address={ip} http-method=post '
                      f'http-header-field="Content-Type:application/octet-stream" http-data="{body}" output=none',
        })
        try:
            client('/system/script/run', 'POST', {'number': script['.id']})
        except urllib.error.HTTPError as error:
            # PairVendo clears existing connections, including the first report.
            # Assert rejection/acceptance through admin and Router observations.
            print("Board report fetch:", error.read().decode())
        finally:
            client('/system/script/' + script['.id'], 'DELETE')

    # Two HotSpot VLANs (10 and 20), each with a Device; Vendo boards join either.

    def _vlan_fixture(self):
        router = lambda path, method="GET", body=None: self.run.router_rest(self.router_address, "admin", self.router_password, path, method, body)
        return self.run._vlan_module.VlanLab(router, self._client)

    def vlan_up(self) -> None:
        """Add both VLAN HotSpots and connect a Device on each. Undo with vlan_down."""
        self._vlan = self._vlan_fixture()
        self._vlan.setup()

    def vlan_hosts(self) -> list[dict]:
        """Each VLAN Device's Router host rows: [{"vlan", "host_rows"}]."""
        return self._vlan.hosts()

    def vlan_devices(self) -> dict[int, str]:
        """Route each VLAN Device out its own VLAN: {vlan: Device IP}."""
        client = self._client
        ips = {item["vlan"]: item["host_rows"][0]["address"] for item in self._vlan.hosts()}
        for vlan, ip in ips.items():
            name = f"opnw-vlan{vlan}"
            if not client(f"/routing/table?name={name}"):
                client("/routing/table", "PUT", {"name": name, "fib": "yes"})
            client("/ip/route", "PUT", {"dst-address": "0.0.0.0/0", "gateway": f"10.5.{vlan}.1%{name}", "routing-table": name, "comment": name})
            client("/routing/rule", "PUT", {"src-address": ip + "/32", "action": "lookup-only-in-table", "table": name, "comment": name})
        return ips

    def vlan_board(self, vlan: int, number: int, hostname=None) -> dict:
        """Vendo board number (1-3) on this VLAN, sending hostname over DHCP when given; returns {"mac", "ip"}."""
        return self._vlan.board(vlan, number, hostname)

    def vlan_leave(self, vlan: int) -> None:
        """This VLAN's Device leaves the WiFi. Undo with vlan_rejoin."""
        interface = self._client(f"/interface/vlan?name=opnw-vlan{vlan}")[0]
        self._client(f"/interface/vlan/{interface['.id']}", "PATCH", {"disabled": "yes"})

    def vlan_rejoin(self, vlan: int) -> None:
        interface = self._client(f"/interface/vlan?name=opnw-vlan{vlan}")[0]
        self._client(f"/interface/vlan/{interface['.id']}", "PATCH", {"disabled": "no"})

    def vlan_down(self) -> None:
        """Remove the VLAN Devices' and boards' routes, then both VLAN HotSpots."""
        for path in ("/routing/rule", "/ip/route"):
            for row in self._client(path):
                if row.get("comment", "").startswith("opnw-vlan"):
                    self._client(path + "/" + row[".id"], "DELETE")
        # A fresh fixture still removes every VLAN resource; it only restores NIC state it changed.
        fixture, self._vlan = self._vlan or self._vlan_fixture(), None
        fixture.teardown()
