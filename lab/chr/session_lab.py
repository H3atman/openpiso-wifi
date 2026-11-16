"""L5-L8 session-control lab: controller-style REST against chr-lab, client = chr-client VM.

Usage: python3 -I lab/chr/session_lab.py OUT_DIR   (env: CHR_PASSWORD, CHR_WAN_IP, CLIENT_IP)
"""
import base64, json, os, sys, time, urllib.request, urllib.error

OUT = sys.argv[1]
PW, RTR, CLI = os.environ["CHR_PASSWORD"], os.environ["CHR_WAN_IP"], os.environ["CLIENT_IP"]
AUTH = "Basic " + base64.b64encode(f"admin:{PW}".encode()).decode()
# CHR virtio NICs ignore MAC changes, so the client has one NIC per MAC and
# "changing MAC" means enabling the other NIC (only one enabled at a time).
MAC1, MAC2 = "02:00:00:00:0C:01", "02:00:00:00:0C:02"
NIC = {MAC1: ("*5", "lan1"), MAC2: ("*6", "lan2")}
CUR = MAC1
T0 = time.time()
LOG = open(os.path.join(OUT, "transcript.log"), "a")


def log(msg):
    line = f"[{time.time() - T0:7.1f}s] {msg}"
    print(line, flush=True)
    LOG.write(line + "\n"); LOG.flush()


def rest(host, path, method="GET", body=None):
    req = urllib.request.Request(f"http://{host}/rest{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": AUTH, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": json.loads(e.read() or b"{}").get("detail")}


r = lambda *a, **k: rest(RTR, *a, **k)
c = lambda *a, **k: rest(CLI, *a, **k)


def snap(case, label):
    d = os.path.join(OUT, case); os.makedirs(d, exist_ok=True)
    data = {n: r(f"/ip/hotspot/{n}") for n in ("active", "host", "user")}
    for n, v in data.items():
        json.dump(v, open(os.path.join(d, f"{label}-{n}.json"), "w"), indent=1)
    act = [(e["user"], e["mac-address"], e["address"], e.get("uptime"), e.get("session-time-left"))
           for e in data["active"]]
    usr = [(e["name"], e.get("uptime"), e.get("limit-uptime")) for e in data["user"] if e["name"].startswith("s-")]
    log(f"{case}/{label}: active={act} users={usr}")
    return data


def active_for(user):
    return [e for e in r("/ip/hotspot/active") if e["user"] == user]


def remove_active(user):
    for e in active_for(user):
        log(f"remove active {e['.id']} {e['user']} {e['mac-address']} -> {r('/ip/hotspot/active/' + e['.id'], 'DELETE')}")


def client_ip():
    for _ in range(30):
        a = c(f"/ip/address?interface={NIC[CUR][1]}")
        if a and isinstance(a, list):
            return a[0]["address"].split("/")[0]
        time.sleep(1)
    raise SystemExit("client has no LAN address")


def login(user, mac, expect_ok=True):
    res = _login(user, mac)
    if expect_ok and res != []:
        raise SystemExit(f"login failed, aborting so later cases aren't bogus: {res}")
    return res


def _login(user, mac):
    ip = client_ip()
    for _ in range(20):  # host entry must exist first
        if any(h["mac-address"] == mac for h in r("/ip/hotspot/host")):
            break
        c("/ping", "POST", {"address": "10.5.50.1", "count": "1"}); time.sleep(1)
    res = r("/ip/hotspot/active/login", "POST", {"user": user, "password": user, "mac-address": mac, "ip": ip})
    log(f"active login {user} {mac} {ip} -> {res}")
    return res


def traffic(on):
    s = c("/system/scheduler?name=ping-gen")[0]
    c(f"/system/scheduler/{s['.id']}", "PATCH", {"disabled": "no" if on else "yes"})
    log(f"client traffic {'ON' if on else 'OFF'}")


def set_mac(mac):
    global CUR
    for m, (iid, _) in NIC.items():
        if m != mac:
            c(f"/interface/ethernet/{iid}", "PATCH", {"disabled": "yes"})
    CUR = mac
    res = c(f"/interface/ethernet/{NIC[mac][0]}", "PATCH", {"disabled": "no"})
    assert res.get("mac-address") == mac and res.get("disabled") == "false", res
    time.sleep(4)
    log(f"client MAC -> {mac} ({NIC[mac][1]}), ip {client_ip()}")


def lan(enabled):
    res = c(f"/interface/ethernet/{NIC[CUR][0]}", "PATCH", {"disabled": "no" if enabled else "yes"})
    assert res.get("disabled") == ("false" if enabled else "true"), res
    log(f"client {NIC[CUR][1]} {'UP' if enabled else 'DOWN (departed)'}")


def set_profile(**kw):
    p = r("/ip/hotspot/user/profile?name=sess")[0]
    r(f"/ip/hotspot/user/profile/{p['.id']}", "PATCH", kw)
    log(f"profile sess <- {kw}")


def set_user(name, **kw):
    u = r(f"/ip/hotspot/user?name={name}")[0]
    r(f"/ip/hotspot/user/{u['.id']}", "PATCH", kw)
    log(f"user {name} <- {kw}")


def poll_logout(user, secs, step=2):
    t = time.time()
    while time.time() - t < secs:
        if not active_for(user):
            dt = time.time() - t
            log(f"{user} logged out after {dt:.0f}s")
            return dt
        time.sleep(step)
    log(f"{user} still active after {secs}s")
    return None


def hotspot_log(case):
    entries = [e for e in r("/log") if "hotspot" in e.get("topics", "") or "script" in e.get("topics", "")]
    json.dump(entries, open(os.path.join(OUT, case, "log.json"), "w"), indent=1)
    for e in entries[-8:]:
        log(f"  log: {e.get('time')} {e.get('topics')} {e.get('message')}")


# ── setup ──
log("setup")
for path in ("/ip/hotspot/active",):
    for e in r(path):
        r(f"{path}/{e['.id']}", "DELETE")
if not r("/ip/hotspot/user/profile?name=sess"):
    r("/ip/hotspot/user/profile", "PUT", {
        "name": "sess", "shared-users": "1", "idle-timeout": "none", "keepalive-timeout": "none",
        "on-logout": ':log info ("onlogout user=" . $user . " mac=" . $"mac-address" . " cause=" . $cause)'})
for name, limit in (("s-rebind", "1h"), ("s-idle", "1h"), ("s-limit", "1m"), ("s-keep", "1h")):
    for u in r(f"/ip/hotspot/user?name={name}"):  # fresh counters each run
        r(f"/ip/hotspot/user/{u['.id']}", "DELETE")
    r("/ip/hotspot/user", "PUT", {"name": name, "password": name, "profile": "sess", "limit-uptime": limit})
set_mac(MAC1); lan(True); traffic(True)

# ── L5: MAC rebind under the same user ──
log("== L5 rebind")
login("s-rebind", MAC1); time.sleep(20)
snap("L5", "1-active-mac1")
set_mac(MAC2); time.sleep(3)
snap("L5", "2-after-mac-change")
log("L5a: login new MAC WITHOUT removing old active")
login("s-rebind", MAC2, expect_ok=False)
snap("L5", "3-login-without-remove")
log("L5b: remove old active, then login new MAC")
remove_active("s-rebind"); time.sleep(1)
snap("L5", "4-after-remove")
login("s-rebind", MAC2); time.sleep(20)
snap("L5", "5-rebound-mac2")
remove_active("s-rebind"); time.sleep(1)
snap("L5", "6-paused")
hotspot_log("L5")

# ── L6: idle-timeout ──
log("== L6 idle-timeout=40s")
set_profile(**{"idle-timeout": "40s", "keepalive-timeout": "none"})
login("s-idle", MAC2)
log("L6 control: traffic on for 70s")
polled = poll_logout("s-idle", 70)
snap("L6", "1-after-70s-traffic")
traffic(False); t_off = time.time()
snap("L6", "2-traffic-off")
poll_logout("s-idle", 150)
log(f"L6 logout {time.time() - t_off:.0f}s after traffic stopped")
snap("L6", "3-after-logout")
hotspot_log("L6")
traffic(True)

# ── L7: change limit-uptime during an active session ──
log("== L7 live limit change")
set_profile(**{"idle-timeout": "none", "keepalive-timeout": "none"})
login("s-limit", MAC2); time.sleep(30)
snap("L7", "1-before-raise")
set_user("s-limit", **{"limit-uptime": "3m"}); time.sleep(3)
snap("L7", "2-after-raise")
out = poll_logout("s-limit", 60)
snap("L7", "3-past-old-limit")
log("L7b: lower limit below consumed uptime while active")
if active_for("s-limit"):
    set_user("s-limit", **{"limit-uptime": "30s"})
    poll_logout("s-limit", 30, step=1)
snap("L7", "4-after-lower")
hotspot_log("L7")

# ── L8: keepalive-timeout ──
log("== L8 keepalive-timeout=30s")
set_profile(**{"idle-timeout": "none", "keepalive-timeout": "30s"})
login("s-keep", MAC2); traffic(False)
log("L8 control: present but no traffic for 70s")
poll_logout("s-keep", 70)
snap("L8", "1-present-idle-70s")
lan(False); t_dep = time.time()
poll_logout("s-keep", 120)
log(f"L8 logout {time.time() - t_dep:.0f}s after client departed")
snap("L8", "2-after-depart")
hotspot_log("L8")
lan(True); traffic(True)
set_profile(**{"idle-timeout": "none", "keepalive-timeout": "none"})
log("done")
