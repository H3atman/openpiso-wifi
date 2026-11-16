"""Repeatable two-VLAN RouterOS fixture; never changes VM definitions."""
import time
import importlib.util
from pathlib import Path

_lab_spec = importlib.util.spec_from_file_location("e2e_lab", Path(__file__).with_name("e2e_lab.py"))
_lab_module = importlib.util.module_from_spec(_lab_spec)
_lab_spec.loader.exec_module(_lab_module)


class VlanLab:
    def __init__(self, router, client):
        self.router = router
        self.client = client
        self.disabled = {}

    def setup(self):
        for vlan, nic in ((10, "lan1"), (20, "lan2")):
            name = f"opnw-vlan{vlan}"
            subnet = f"10.5.{vlan}"
            self.router("/interface/vlan", "PUT", {"name": name, "interface": "ether2", "vlan-id": str(vlan)})
            self.router("/ip/address", "PUT", {"address": f"{subnet}.1/24", "interface": name, "comment": name})
            self.router("/ip/pool", "PUT", {"name": name, "ranges": f"{subnet}.100-{subnet}.200"})
            self.router("/ip/dhcp-server/network", "PUT", {"address": f"{subnet}.0/24", "gateway": f"{subnet}.1", "dns-server": f"{subnet}.1", "comment": name})
            self.router("/ip/dhcp-server", "PUT", {"name": name, "interface": name, "address-pool": name, "disabled": "no"})
            self.router("/ip/hotspot/profile", "PUT", {"name": name, "hotspot-address": f"{subnet}.1", "login-by": "http-pap"})
            self.router("/ip/hotspot", "PUT", {"name": name, "interface": name, "profile": name, "address-pool": name, "disabled": "no"})
            interface = self.client(f"/interface?name={nic}")[0]
            self.disabled[interface['.id']] = interface["disabled"]
            self.client(f"/interface/{interface['.id']}", "PATCH", {"disabled": "no"})
            self.client("/interface/vlan", "PUT", {"name": name, "interface": nic, "vlan-id": str(vlan)})
            self.client("/ip/dhcp-client", "PUT", {"interface": name, "add-default-route": "no", "use-peer-dns": "no", "disabled": "no"})

    def hosts(self):
        evidence = []
        for vlan in (10, 20):
            name = f"opnw-vlan{vlan}"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                addresses = self.client(f"/ip/address?interface={name}")
                if addresses:
                    break
                time.sleep(1)
            else:
                raise AssertionError(f"{name}: DHCP did not bind")
            ip = addresses[0]["address"].split("/")[0]
            self.client("/ping", "POST", {"address": f"10.5.{vlan}.1", "src-address": ip, "interface": name, "count": "1"})
            hosts = self.router(f"/ip/hotspot/host?address={ip}")
            evidence.append({"vlan": vlan, "host_rows": hosts})
        return evidence

    def board(self, vlan, number, hostname=None):
        name = f"opnw-board{number}"
        mac = _lab_module.BOARD_MACS[number - 1]
        self.client("/interface/macvlan", "PUT", {"name": name, "interface": f"opnw-vlan{vlan}", "mac-address": mac})
        options = "hostname,clientid"
        if hostname:
            option = name + "-hostname"
            self.client("/ip/dhcp-client/option", "PUT", {"name": option, "code": "12", "value": "'" + hostname + "'"})
            options = option + ",clientid"
        self.client("/ip/dhcp-client", "PUT", {"dhcp-options": options, "interface": name, "add-default-route": "no", "use-peer-dns": "no", "disabled": "no"})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            addresses = self.client(f"/ip/address?interface={name}")
            if addresses:
                ip = addresses[0]["address"].split("/")[0]
                self.client("/ping", "POST", {"address": f"10.5.{vlan}.1", "src-address": ip, "interface": name, "count": "1"})
                # Route through the MACVLAN itself: the parent VLAN would send
                # the right source IP with the wrong Ethernet source MAC.
                table = f"opnw-vlan{vlan}-board{number}"
                if not self.client("/routing/table?name=" + table):
                    self.client("/routing/table", "PUT", {"name": table, "fib": "yes"})
                self.client("/ip/route", "PUT", {"dst-address": "0.0.0.0/0", "gateway": f"10.5.{vlan}.1%{name}", "routing-table": table, "comment": table})
                self.client("/routing/rule", "PUT", {"src-address": ip + "/32", "action": "lookup-only-in-table", "table": table, "comment": table})
                return {"mac": mac, "ip": ip}
            time.sleep(1)
        raise AssertionError(f"{name}: DHCP did not bind")

    def teardown(self):
        for row in self.client("/interface/macvlan"):
            if not row["name"].startswith("opnw-board"):
                continue
            for lease in self.client(f"/ip/dhcp-client?interface={row['name']}"):
                self.client(f"/ip/dhcp-client/{lease['.id']}", "DELETE")
            self.client(f"/interface/macvlan/{row['.id']}", "DELETE")
        for row in self.client("/ip/dhcp-client/option"):
            if row["name"].startswith("opnw-board"):
                self.client(f"/ip/dhcp-client/option/{row['.id']}", "DELETE")
        for vlan in (20, 10):
            name = f"opnw-vlan{vlan}"
            for path, query in (("/ip/dhcp-client", f"interface={name}"), ("/interface/vlan", f"name={name}")):
                for row in self.client(f"{path}?{query}"):
                    self.client(f"{path}/{row['.id']}", "DELETE")
            for path, query in (("/ip/hotspot", f"name={name}"), ("/ip/dhcp-server/lease", f"server={name}"), ("/ip/dhcp-server", f"name={name}"),
                                ("/ip/hotspot/profile", f"name={name}"), ("/ip/dhcp-server/network", f"comment={name}"),
                                ("/ip/address", f"comment={name}"), ("/ip/pool", f"name={name}"), ("/interface/vlan", f"name={name}")):
                for row in self.router(f"{path}?{query}"):
                    self.router(f"{path}/{row['.id']}", "DELETE")
        for interface, disabled in self.disabled.items():
            self.client(f"/interface/{interface}", "PATCH", {"disabled": disabled})
