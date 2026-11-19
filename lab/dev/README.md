# Dev lab

A persistent local lab for fast UI/UX work on the customer portal and the admin panel, without hardware. It runs a dev-build Controller against a stock MikroTik CHR Router, a simulated Vendo, and either scripted or hand-driven phones standing in for the customer Device, all in Docker.

Everything goes through one stdlib Python CLI:

```sh
python3 -I lab/dev/dev.py up      # build, create or reuse the lab, (re)start the Controller
python3 -I lab/dev/dev.py vendo   # coin slot for the dev Vendo
python3 -I lab/dev/dev.py phone   # a Device for your own browser (proxy on 127.0.0.1:18089)
python3 -I lab/dev/dev.py shots   # scripted phones, screenshots
python3 -I lab/dev/dev.py down    # remove the lab and its data
```

`admin-shots --out <dir>` takes admin panel screenshots from fixture data, without a Router.

Open `http://127.0.0.1:18088/admin/login` and sign in with the password in `lab/dev/.run/admin-password` (generated on the first `up`, and kept across `down`). Other state (dev binary, Vendo secret) lives in the gitignored `lab/dev/.run/docker/` and goes with `down`.

## What it is

The E2E Docker lab's compose project ([`lab/docker`](../docker), [ADR 0011](../../docs/adr/0011-e2e-runs-on-a-docker-lab-per-run.md)) under the fixed name `opnwdev`, plus [`lab/docker/dev.yml`](../docker/dev.yml): a stock RouterOS CHR set up by the same code as E2E (`DockerLab.set_up_router`), the Controller and the internet stand-in on the lab WAN, and the dev Vendo board (`opnwdev-vendo`, MAC `02:00:00:0D:00:F0`, hostname `opnw-00f0`) on the lab LAN. E2E labs are `opnwlab-<random>` with Docker-chosen ports, so an E2E run beside it touches nothing of the dev lab.

It needs Docker usable by your user with `/dev/kvm`, Go (on `PATH` or at `~/.local/go/bin/go`) and Python 3, stdlib only. No sudo. The first `up` downloads and verifies the pinned CHR image, as E2E does.

## Commands

**`up`** builds the Controller with `-tags dev` into `lab/dev/.run/docker/`. With no lab, it creates one, sets the Router up from stock, onboards it, pairs the dev Vendo (its first Report comes from its own container) and seeds Coin rates `1=60`, `5=360`. With a lab, it only restarts the Controller on the new build. It returns once the lab is ready; the lab stays up until `down`, across terminals. If the lab is there but not running (after a reboot), `up` asks you to run `down` first. It refuses to create a lab when something already answers on port 18088.

**`vendo`** is an interactive coin slot for the dev Vendo, on stdin, sending every Report from the dev Vendo's container. Type a number of pulses and Enter. It needs an open Coin Window, so click "Insert coin" in the portal first; otherwise it reports that the acceptor is inhibited. `q` quits. Each start is one board boot (the persisted epoch goes up by one), and it releases any Coin Window an earlier run left open. A lock allows only one simulator at a time, so `vendo` and `shots` cannot run together.

**`phone`** makes your desktop browser a Device behind the HotSpot. See [Driving it by hand](#driving-it-by-hand).

**`shots`** runs scripted phones with screenshots. See [The `shots` E2E](#the-shots-e2e). Optionally name profiles to narrow the run: `shots iphone-15`, `shots pixel-7`.

**`down`** removes every `opnwdev-*` container and network, the Controller's data with them, and the dev Vendo's secret. The admin password and the shared images stay.

Idle, the lab uses about 170 MB (the Router about 140 MB, the Controller about 16 MB).

## The UI loop

The Controller is built with `go build -tags dev` (`controller/internal/controller/web_dev.go`). The repository is mounted read-only into the Controller container at its real path, and the dev build reads `web/templates`, `web/static` and `web/portal` from there on every request, with `Cache-Control: no-store`.

| You change | What to do |
| --- | --- |
| Template or CSS/JS | Save, refresh the browser. No restart. |
| Go code | Run `up` again: it rebuilds and restarts the Controller; the data (the container's `/data`) is kept. |

- Read the Controller log with `docker logs -f opnwdev-controller-1`.
- A template syntax error panics the request: the browser sees a dropped connection, and the error is in the Controller log.
- The Portal is at `http://10.77.7.1:18080/portal`, reachable only by Devices behind the HotSpot. View it through `phone` or `shots`.

## Driving it by hand

`phone` starts `opnwdev-phone`, a container on the lab LAN with MAC `02:00:00:0D:00:03` and a lease from the Router, running a small stdlib HTTP proxy ([`phone_proxy.py`](phone_proxy.py)) published on `127.0.0.1:18089`. Every request your browser sends through it leaves from that Device, so the HotSpot redirects it, the Controller sees the Device's MAC and IP, and once it has Time Credit it reaches the internet stand-in. It stays in the foreground; Ctrl+C removes the container and makes the Router forget the Device (lease, host, session), so the next `phone` starts without Time Credit. `down` removes it too. A UI session:

```sh
python3 -I lab/dev/dev.py up      # once
python3 -I lab/dev/dev.py phone   # terminal 1: the proxy Device
python3 -I lab/dev/dev.py vendo   # terminal 2: the coin slot, started before "Insert coin"
firefox --no-remote --new-instance --profile lab/dev/.run/docker/firefox-profile http://google.com/   # terminal 3
```

`http://google.com/` redirects to the Router's login, which hands over to the Portal. Click "Insert coin", type `5` + Enter in the `vendo` terminal, click "Done": `http://google.com/` then shows `openpiso-lab-internet`, the stand-in's page. Edit a template or stylesheet and refresh as usual.

- **The browser profile.** `phone` writes a fresh Firefox profile to `lab/dev/.run/docker/firefox-profile` on every start (so no cookie outlives the Device): every request through the proxy, DNS over HTTPS off, and captive-portal detection checking `http://google.com/` for the stand-in's page, since Firefox's own detection URL does not resolve in the sealed lab. Close that Firefox before the next `phone`. Chromium needs no profile setup: `chromium --user-data-dir="$(mktemp -d)" --proxy-server=http://127.0.0.1:18089 http://google.com/`. Any browser works with a manual HTTP proxy of `127.0.0.1:18089`, as long as its profile is used for nothing else.
- **Start `vendo` before "Insert coin".** On start `vendo` releases any Coin Window that is already open, assuming an earlier run left it. Leave it running until after "Done", so the board reports the window closed.
- **Plain http only.** The proxy tunnels https (CONNECT), but the HotSpot redirects only plain http, and the stand-in serves no https, so https pages fail before and after login. Type `http://` URLs.
- **What is not faithful.** The browser's own captive-portal detection runs through the proxy, not through the OS network stack, so there is no OS "sign in to network" prompt; the redirect and the Portal are the real ones. Firefox's captive-portal notification with the profile above is not verified.

## The `shots` E2E

A customer-journey test through the real HotSpot and the dev Controller. It needs the lab up (`up`).

- **Image:** the first run builds `openpiso-dev-shots:pw1.64.1` on top of `mcr.microsoft.com/playwright/python:v1.64.1-noble`.
- **Network:** each profile is a Playwright container on the lab LAN (1 GB cap) with a fresh random MAC and its own DHCP lease, so it starts as an unpaid Device.
- **Profiles:** `iphone-15` (iPhone 15, WebKit) and `pixel-7` (Pixel 7, Chromium).
- **Steps per profile** (each asserted, each screenshotted):
  1. A plain `http` URL (`http://google.com/`, answered by the internet stand-in) redirects through the HotSpot to the portal.
  2. "Insert coin" opens a Coin Window on the dev Vendo.
  3. The simulated Vendo inserts 5 pesos and the portal shows the coins counted.
  4. "Done" closes the Coin Window, the Controller credits the Time Credit.
  5. The Device reaches the internet.

Artifacts, under `lab/dev/.run/shots/<timestamp>/`:

- `<profile>/NN-<step>.png`: one per step (`portal-via-hotspot-redirect`, `coin-window-open`, `coins-counted`, `time-credited`, `online`), plus `failure` if a step failed.
- `<profile>/result.json`: `result` (`pass` or `fail`), `error` on failure, and the steps with timings.
- `report.json`: all profiles with their MAC, IP, duration and result.

Pass/fail: the command prints `[dev] <profile>: pass` or `fail (<error>)` per profile and exits 1 if any profile failed. A profile times out after 300 s. Host entries, sessions and leases for the test Devices are removed afterwards.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `up` refuses: something answers on port 18088 | Another process (often another dev lab) holds the port. Stop it, or run `down` in the checkout that owns it. |
| `up` asks for `down` | The lab exists but is not running (after a reboot). Run `down`, then `up`. |
| `go build failed` | Check the build output above it; make sure `go` is on `PATH` or at `~/.local/go/bin/go`. |
| `another simulator already runs the dev Vendo (pid N)` | Quit the running `vendo` (or the other `shots`) first. |
| `acceptor inhibited` in `vendo` | Open a Coin Window from the portal ("Insert coin") first. |
| Browser drops the connection on an admin or portal page | A template syntax error. Read the Controller log. |
| `shots` profile fails | Open `<profile>/NN-failure.png` and `result.json` in the run folder; the Controller log shows the server side. |

## Teardown

```sh
python3 -I lab/dev/dev.py down   # every opnwdev-* container and network, the Controller's data, the Vendo secret
rm -rf lab/dev/.run              # the admin password and shots output
docker rmi openpiso-dev-shots:pw1.64.1
```
