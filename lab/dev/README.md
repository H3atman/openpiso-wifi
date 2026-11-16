# Dev lab

A persistent local lab for fast UI/UX work on the customer portal and the admin panel, without hardware. It runs a dev-build Controller against the shared `chr-lab` Router (a MikroTik CHR VM, HotSpot `10.5.50.1/24`), a simulated Vendo, and either scripted or hand-driven phones standing in for the customer Device.

Everything goes through one stdlib Python CLI:

```sh
python3 -I lab/dev/dev.py {up,vendo,phone,shots,down}
```

State lives in the gitignored `lab/dev/.run/` (Controller binary, data, logs, admin password, Vendo secret, screenshots).

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
6. Onboards the Router if the Controller does not already show it as ready. It drops the `openpiso-controller` user first, so it self-heals after a `make e2e` run.
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

## Sharing chr-lab with `make e2e`

The lab and `make -C controller e2e` use the same Router and the same host route. Never run both.

1. `python3 -I lab/dev/dev.py down`, then `make -C controller e2e`.
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

`make e2e` uses `.201`, `.202` and `.241`, so it does not overlap.

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
| Lab broken after `make e2e` | Run `up` again; it re-onboards the Router and re-pairs the Vendo. |

## Teardown

```sh
python3 -I lab/dev/dev.py down --vm        # containers, network, leases, routing fixture, then VM shutdown
rm -rf lab/dev/.run                         # data, Vendo secret, admin password
docker rmi openpiso-dev-shots:pw1.64.1
sudo ip netns del devphone; sudo ip link del vDP-br; sudo rm -r /etc/netns/devphone   # only if you used `phone`
```
