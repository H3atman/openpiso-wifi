"""Someone else's HotSpot on chr-lab: the Router a Shop already runs before it connects a Controller.

setup() turns the lab's HotSpot server hs1 into another system's server (its own login page in the
default directory, a login method the Controller cannot use, users holding time, no Controller
walled garden, NAT exemption, HotSpot profiles or login page) and adds a second server, OTHER, that the Operator does not Adopt.
It never changes VM definitions. teardown_other() removes the second server and setup_other() adds
it again; teardown() also removes the users. Neither restores hs1: every run Adopts it again.
"""

SERVER = "hs1"
SERVER_PROFILE = "hs-prof"
OTHER = "opnw-other"  # interface, address comment, pool, server profile and server
OTHER_VLAN = "30"
OTHER_SUBNET = "10.5.30"
# RouterOS's default login method; the Controller needs http-pap.
LOGIN_BY = "cookie,http-chap"
LOGIN_FILE = "hotspot/login.html"
LOGIN_HTML = "<!doctype html><title>Another vendo</title><p>opnw-existing-login-page</p><form action=\"$(link-login-only)\"></form>"
USERS = {"opnw-existing-1": "1h", "opnw-existing-2": "30m"}  # name: limit-uptime
CONTROLLER_LOGIN_DIRECTORY = "openpiso"


class ExistingHotspotLab:
    def __init__(self, router):
        self.router = router

    def setup(self):
        self.teardown()
        profile = self.router(f"/ip/hotspot/profile?name={SERVER_PROFILE}")[0]
        self.router(f"/ip/hotspot/profile/{profile['.id']}", "PATCH", {"login-by": LOGIN_BY, "html-directory": "hotspot"})
        self._remove("/file", f"name={LOGIN_FILE}")
        self.router("/file", "PUT", {"name": LOGIN_FILE, "contents": LOGIN_HTML})
        for name, limit in USERS.items():
            self.router("/ip/hotspot/user", "PUT", {"name": name, "password": name, "limit-uptime": limit, "server": "all"})
        # A Router that never had a Controller: no walled garden entry, HotSpot profiles, login page or backups of ours.
        self._remove("/ip/hotspot/walled-garden/ip", "comment=openpiso-controller")
        for row in self.router("/ip/firewall/nat"):
            # A NAT exemption of ours; the walled garden's dynamic rules carry its comment openpiso-controller.
            if row.get("comment", "").startswith("openpiso-controller-") and row.get("dynamic") != "true":
                self.router(f"/ip/firewall/nat/{row['.id']}", "DELETE")
        for row in self.router("/ip/hotspot/user"):
            if row["name"].startswith("op-"):  # a Time Credit's user, which holds a profile of ours
                self.router(f"/ip/hotspot/user/{row['.id']}", "DELETE")
        for row in self.router("/ip/hotspot/user/profile"):
            if row["name"].startswith("openpiso-"):
                self.router(f"/ip/hotspot/user/profile/{row['.id']}", "DELETE")
        # A full /file listing includes binary contents that are not UTF-8; files go before their directory.
        for row in sorted(self.router("/file?.proplist=.id,name"), key=lambda row: -len(row["name"])):
            if row["name"].startswith((CONTROLLER_LOGIN_DIRECTORY + "/", "openpiso-")) or row["name"] == CONTROLLER_LOGIN_DIRECTORY:
                self.router(f"/file/{row['.id']}", "DELETE")
        self.setup_other()

    def setup_other(self):
        """Add the second server, OTHER, that the Operator does not Adopt."""
        self.router("/interface/vlan", "PUT", {"name": OTHER, "interface": "ether2", "vlan-id": OTHER_VLAN})
        self.router("/ip/address", "PUT", {"address": f"{OTHER_SUBNET}.1/24", "interface": OTHER, "comment": OTHER})
        self.router("/ip/pool", "PUT", {"name": OTHER, "ranges": f"{OTHER_SUBNET}.100-{OTHER_SUBNET}.200"})
        self.router("/ip/hotspot/profile", "PUT", {"name": OTHER, "hotspot-address": f"{OTHER_SUBNET}.1", "login-by": LOGIN_BY, "html-directory": "hotspot"})
        self.router("/ip/hotspot", "PUT", {"name": OTHER, "interface": OTHER, "profile": OTHER, "address-pool": OTHER, "disabled": "no"})

    def other(self):
        """The second server's own rows and the default login page, to compare before and after."""
        login = self.router(f"/file?name={LOGIN_FILE.replace('/', '%2F')}")
        return {
            "server": self.router(f"/ip/hotspot?name={OTHER}"),
            "server_profile": self.router(f"/ip/hotspot/profile?name={OTHER}"),
            "login_page": [{key: row.get(key) for key in ("name", "contents", "size", "last-modified")} for row in login],
        }

    def users(self):
        return [{key: row.get(key) for key in ("name", "limit-uptime", "profile", "server", "disabled")}
                for row in self.router("/ip/hotspot/user") if row["name"] in USERS]

    def teardown_other(self):
        for path, query in (("/ip/hotspot", f"name={OTHER}"), ("/ip/hotspot/profile", f"name={OTHER}"),
                            ("/ip/pool", f"name={OTHER}"), ("/ip/address", f"comment={OTHER}"),
                            ("/interface/vlan", f"name={OTHER}")):
            self._remove(path, query)

    def teardown(self):
        self.teardown_other()
        for name in USERS:
            self._remove("/ip/hotspot/user", f"name={name}")

    def _remove(self, path, query):
        for row in self.router(f"{path}?{query.replace('/', '%2F')}"):
            self.router(f"{path}/{row['.id']}", "DELETE")
