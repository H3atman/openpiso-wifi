# Dev lab

A persistent local lab for fast UI/UX work on the customer portal and the admin panel, without hardware. It runs a dev-build Controller against the shared `chr-lab` Router (a MikroTik CHR VM, HotSpot `10.5.50.1/24`), a simulated Vendo, and either scripted or hand-driven phones standing in for the customer Device.

Everything goes through one stdlib Python CLI:

```sh
python3 -I lab/dev/dev.py {up,vendo,phone,shots,down}
```

State lives in the gitignored `lab/dev/.run/` (Controller binary, data, logs, admin password, Vendo secret, screenshots).

There are two labs behind the same commands. `--lab docker` (below) is a Docker lab of its own that needs no sudo, no libvirt and no `chr-lab`, and runs beside E2E. Without it, the commands use the libvirt lab described in the rest of this file, which goes away with #39.

## The Docker dev lab (`--lab docker`)

```sh
python3 -I lab/dev/dev.py --lab docker up      # build, create or reuse the lab, (re)start the Controller
python3 -I lab/dev/dev.py --lab docker vendo   # coin slot for the dev Vendo
python3 -I lab/dev/dev.py --lab docker phone   # a Device for your own browser (proxy on 127.0.0.1:18089)
python3 -I lab/dev/dev.py --lab docker shots   # scripted phones, screenshots
python3 -I lab/dev/dev.py --lab docker down    # remove the lab and its data
```

Open `http://127.0.0.1:18088/admin/login` and sign in with the password in `lab/dev/.run/admin-password` (generated on the first `up` of either lab, and kept across `down`).

**What it is.** The E2E Docker lab's compose project ([`lab/docker`](../docker)) under the fixed name `opnwdev`, plus [`lab/docker/dev.yml`](../docker/dev.yml): a stock RouterOS CHR set up by the same code as E2E (`DockerLab.set_up_router`), the Controller and the internet stand-in on the lab WAN, and the dev Vendo board (`opnwdev-vendo`, MAC `02:00:00:0D:00:F0`, hostname `opnw-00f0`) on the lab LAN. E2E labs are `opnwlab-<random>` with Docker-chosen ports, so an E2E run beside it touches nothing of the dev lab. It needs Docker usable by your user with `/dev/kvm`, Go and Python 3. The first `up` downloads and verifies the pinned CHR image, as E2E does.

**`up`** builds the Controller with `-tags dev` into `lab/dev/.run/docker/`. With no lab, it creates one, sets the Router up from stock, onboards it, pairs the dev Vendo (its first Report comes from its own container) and seeds Coin rates `1=60`, `5=360`. With a lab, it only restarts the Controller on the new build. It returns once the lab is ready; the lab stays up until `down`, across terminals. If the lab is there but not running (after a reboot), `up` asks you to run `down` first. It refuses to create a lab when something already answers on port 18088.

**The UI loop.** The repository is mounted read-only into the Controller container at its real path, and the dev build reads `web/templates`, `web/static` and `web/portal` from there: save, refresh. After a Go change run `up` again: it rebuilds and restarts the Controller, and the data (the container's `/data`) is kept. Read the Controller log with `docker logs -f opnwdev-controller-1`.

**The Portal** is at `http://10.77.7.1:18080/portal`, reachable only by Devices behind the HotSpot. View it in your own browser through `phone`, or through `shots`.

**`phone`** makes your desktop browser a Device behind the HotSpot, with no sudo and no extra browser. It starts `opnwdev-phone`, a container on the lab LAN with MAC `02:00:00:0D:00:03` and a lease from the Router, running a small stdlib HTTP proxy ([`phone_proxy.py`](phone_proxy.py)) published on `127.0.0.1:18089`. Every request your browser sends through it leaves from that Device, so the HotSpot redirects it, the Controller sees the Device's MAC and IP, and once it has Time Credit it reaches the internet stand-in. It stays in the foreground; Ctrl+C removes the container and makes the Router forget the Device (lease, host, session), so the next `phone` starts without Time Credit. `down` removes it too. A UI session:

```sh
python3 -I lab/dev/dev.py --lab docker up      # once
python3 -I lab/dev/dev.py --lab docker phone   # terminal 1: the proxy Device
python3 -I lab/dev/dev.py --lab docker vendo   # terminal 2: the coin slot, started before "Insert coin"
firefox --no-remote --new-instance --profile lab/dev/.run/docker/firefox-profile http://google.com/   # terminal 3
```

`http://google.com/` redirects to the Router's login, which hands over to the Portal. Click "Insert coin", type `5` + Enter in the `vendo` terminal, click "Done": `http://google.com/` then shows `openpiso-lab-internet`, the stand-in's page. Edit a template or stylesheet and refresh as usual.

- **The browser profile.** `phone` writes a fresh Firefox profile to `lab/dev/.run/docker/firefox-profile` on every start (so no cookie outlives the Device): every request through the proxy, DNS over HTTPS off, and captive-portal detection checking `http://google.com/` for the stand-in's page, since Firefox's own detection URL does not resolve in the sealed lab. Close that Firefox before the next `phone`. Chromium needs no profile setup: `chromium --user-data-dir="$(mktemp -d)" --proxy-server=http://127.0.0.1:18089 http://google.com/`. Any browser works with a manual HTTP proxy of `127.0.0.1:18089`, as long as its profile is used for nothing else.
- **Start `vendo` before "Insert coin".** On start `vendo` releases any Coin Window that is already open, assuming an earlier run left it. Leave it running until after "Done", so the board reports the window closed.
- **Plain http only.** The proxy tunnels https (CONNECT), but the HotSpot redirects only plain http, and the stand-in serves no https, so https pages fail before and after login. Type `http://` URLs.
- **What is not faithful.** The browser's own captive-portal detection runs through the proxy, not through the OS network stack, so there is no OS "sign in to network" prompt; the redirect and the Portal are the real ones. Firefox's captive-portal notification with the profile above is not verified.

**`vendo`** works as below, sending every Report from the dev Vendo's container. **`shots`** works as below, with each phone a Playwright container on the lab LAN (1 GB cap) that takes its own DHCP lease, and with `http://google.com/` (answered by the internet stand-in) as the plain http URL. Screenshots go to `lab/dev/.run/shots/<timestamp>/`.

**`down`** removes every `opnwdev-*` container and network, the Controller's data with them, and the dev Vendo's secret. The admin password and the shared images stay.

Idle, the lab uses about 170 MB (the Router about 140 MB, the Controller about 16 MB).

## Prerequisites

- **`chr-lab` and `hotspot-lan`**, created once by `./lab/chr/wizard.sh` (see [`lab/chr/README.md`](../chr/README.md)). Needs libvirt, KVM and your user in the `libvirt` group. `up` starts the VM if it is off.
- **`lab/chr/.env`** with the CHR password, written by that wizard. The lab reads it from this checkout, or falls back to the main checkout when you work in a worktree. It is never printed.
- **Go**, on `PATH` or at `~/.local/go/bin/go`.
- **Docker**, usable by your user, for `shots` (the Playwright image is pulled and extended on first run).
- **Python 3**, stdlib only.
- **sudo and Firefox**, only for `phone`.

## Quick start

Terminal 1: build and run the Controller. This stays in the foreground and streams its log.

```sh
python3 -I lab/dev/dev.py up
```

When it prints "Dev lab is up", open `http://127.0.0.1/admin/login` and log in with the admin password. It is generated on the first `up`: it is printed once and kept in `lab/dev/.run/admin-password`.

Terminal 2: run the customer journey on scripted phones.

```sh
python3 -I lab/dev/dev.py shots
```

Screenshots land in `lab/dev/.run/shots/<timestamp>/`. To drive it by hand instead, use `phone` for the browser and `vendo` (in a third terminal) for the coin slot.

## The UI dev loop

The Controller is built with `go build -tags dev` (`controller/internal/controller/web_dev.go`). That build reads `web/templates` and `web/static` from the source tree on every request and sends `Cache-Control: no-store`.

| You change | What to do |
| --- | --- |
| Template (`web/templates`) or CSS/JS (`web/static`) | Save, refresh the browser. No restart. |
| Go code | Ctrl+C in the `up` terminal, run `up` again (it rebuilds; data is kept). |

Notes:

- A template syntax error panics the request: the browser sees a dropped connection, and the error is in the Controller log (the `up` terminal, or `lab/dev/.run/controller.log`).
- The dev binary hard-codes the source path at build time. It must run on the machine that built it, so use `up` rather than copying `lab/dev/.run/openpiso-dev` elsewhere.
- The admin panel is reachable from the host. The portal at `http://192.168.122.1/portal` is only reachable by Devices behind the HotSpot, so view it through `phone` or `shots`.

## Commands

### `up`

Makes the lab healthy, then runs the Controller in the foreground. Ctrl+C stops the Controller only; `chr-lab` stays up. It refuses to start if something already answers on `127.0.0.1:80`.

In order, it:

1. Starts `chr-lab` if it is off and waits for REST.
2. Checks the Router's `www-ssl` certificate and regenerates it if needed.
3. Turns on `/ip dns allow-remote-requests` on the Router and leaves it on (the HotSpot answers Device DNS through it).
4. Adds the return-route fixture: libvirt network `openpiso-dev-routing` (`virbr-opdev`) plus a third `chr-lab` NIC, so the Controller sees Devices' real IPs.
5. Builds the Controller with `-tags dev` and starts it on `0.0.0.0:80`, with data in `lab/dev/.run/data` and `-portal-url http://192.168.122.1`. Ports below 1024 need `cap_net_bind_service`, and a rebuild clears it, so when the dev binary lacks it `up` grants it with `sudo setcap` and may ask for your sudo password.
6. Onboards the Router if the Controller does not already show it as ready. It drops the `openpiso-controller` user first, so it self-heals after a libvirt E2E run.
7. Pairs one dev Vendo (id and secret kept in `lab/dev/.run/vendo.json`) and seeds Coin rates `1=60`, `5=360` if there are none.

### `vendo`

An interactive coin slot for the dev Vendo, on stdin. Type a number of pulses and Enter. It needs an open Coin Window, so click "Insert coin" in the portal first; otherwise it reports that the acceptor is inhibited. `q` quits.

Each start is one board boot (the persisted epoch goes up by one), and it releases any Coin Window an earlier run left open. A lock allows only one simulator at a time, so `vendo` and `shots` cannot run together.

### `phone`

Firefox as a Device behind the HotSpot: a netns `devphone` with a veth on `virbr-hs`, a reserved lease, and the browser opening `http://neverssl.com`. It asks for sudo. Close Firefox to return. Not tested end to end yet.

### `shots`

Scripted phones with screenshots. See [The `shots` E2E](#the-shots-e2e). Optionally name profiles to narrow the run: `shots iphone-15`, `shots pixel-7`.

### `down`

Removes the `shots` containers, the macvlan network, the lab's DHCP leases and the routing fixture. `chr-lab` is left running. `down --vm` also asks it to shut down.

## The `shots` E2E

A customer-journey test through the real HotSpot and the dev Controller. It needs `up` running, and no sudo.

- **Image:** the first run builds `openpiso-dev-shots:pw1.64.1` on top of `mcr.microsoft.com/playwright/python:v1.64.1-noble`.
- **Network:** each profile is a container on a macvlan network over `virbr-hs`, with a fresh random MAC and its own IP, so it starts as an unpaid Device.
- **Profiles:** `iphone-15` (iPhone 15, WebKit) and `pixel-7` (Pixel 7, Chromium).
- **Steps per profile** (each asserted, each screenshotted):
  1. A plain `http` URL redirects through the HotSpot to the portal.
  2. "Insert coin" opens a Coin Window on the dev Vendo.
  3. The simulated Vendo inserts 5 pesos and the portal shows the coins counted.
  4. "Done" closes the Coin Window, the Controller credits the Time Credit.
  5. The Device reaches the internet.

Artifacts, under `lab/dev/.run/shots/<timestamp>/`:

- `<profile>/NN-<step>.png`: one per step (`portal-via-hotspot-redirect`, `coin-window-open`, `coins-counted`, `time-credited`, `online`), plus `failure` if a step failed.
- `<profile>/result.json`: `result` (`pass` or `fail`), `error` on failure, and the steps with timings.
- `report.json`: all profiles with their MAC, IP, duration and result.

Pass/fail: the command prints `[dev] <profile>: pass` or `fail (<error>)` per profile and exits 1 if any profile failed. A profile times out after 300 s. Host entries, sessions and leases for the test Devices are removed afterwards.

## Sharing chr-lab with libvirt E2E

The default `make e2e` runs on a Docker lab of its own and does not touch this lab. The libvirt E2E (`make -C controller e2e LAB=libvirt`) uses the same Router and the same host route. Never run both.

1. `python3 -I lab/dev/dev.py down`, then `make -C controller e2e LAB=libvirt`.
2. Afterwards `up` recovers the lab: it re-onboards the Router if needed, and a fresh Vendo pairing deletes stale `openpiso-vendo-*` Router rules.

## Network plan

HotSpot LAN `10.5.50.0/24`, DHCP pool `.10-.200`. A Device IP must be inside the pool: the HotSpot remaps other addresses and the Controller then cannot find the Device. Each lab Device gets a static lease (comment `openpiso-dev-device`).

| Name | MAC | IP |
| --- | --- | --- |
| Router (HotSpot) | | `10.5.50.1` |
| Controller (host, as Devices see it) | | `192.168.122.1:80` |
| Controller (admin, from the host) | | `127.0.0.1:80` |
| dev Vendo (in the pool; bypassed once paired) | `02:00:00:0D:00:F0` | `10.5.50.20` |
| `phone` Device | `02:00:00:0d:00:03` | `10.5.50.190` |
| `shots` Devices | random `02:de:...` per run | `10.5.50.150-169`, random per run |
| Return-route NIC on `chr-lab` | `52:54:00:07:08:07` | `10.77.8.2` (host side `10.77.8.1`, `virbr-opdev`) |

Libvirt E2E uses `.201`, `.202` and `.241`, so it does not overlap.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `lab/chr/.env with CHR_PASSWORD not found` | Run `./lab/chr/wizard.sh`, or make sure the main checkout has `lab/chr/.env`. |
| `a Controller already answers on 127.0.0.1:80` | Stop the other Controller (often a previous `up`) and run `up` again. |
| `go build failed` | Check the build output above it; make sure `go` is on `PATH` or at `~/.local/go/bin/go`. |
| `setcap failed: the Controller cannot bind port 80 ...` | Run `up` again and enter your sudo password; `setcap` (libcap) must be installed and `lab/dev` must not be on a `nosuid` mount. |
| `no Controller on 127.0.0.1:80: run up first` (from `shots`) | Start `up` in another terminal and wait for "Dev lab is up". |
| `another simulator already runs the dev Vendo (pid N)` | Quit the running `vendo` (or the other `shots`) first. |
| `acceptor inhibited` in `vendo` | Open a Coin Window from the portal ("Insert coin") first. |
| Browser drops the connection on an admin or portal page | A template syntax error. Read the Controller log. |
| `shots` profile fails | Open `<profile>/NN-failure.png` and `result.json` in the run folder; the Controller log shows the server side. |
| Lab broken after libvirt E2E | Run `up` again; it re-onboards the Router and re-pairs the Vendo. |

## Teardown

```sh
python3 -I lab/dev/dev.py down --vm        # containers, network, leases, routing fixture, then VM shutdown
rm -rf lab/dev/.run                         # data, Vendo secret, admin password
docker rmi openpiso-dev-shots:pw1.64.1
sudo ip netns del devphone; sudo ip link del vDP-br; sudo rm -r /etc/netns/devphone   # only if you used `phone`
```
