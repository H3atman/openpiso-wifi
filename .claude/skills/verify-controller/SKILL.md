---
name: verify-controller
description: Runs the openpiso Controller for real on its own Docker lab (stock CHR Router, a Device behind the HotSpot) and keeps the replies as evidence. Use to settle a claim about what the Controller's admin routes, Portal API, ledger or Router writes actually do, before saying a change works.
---

# verify-controller

`verify.py` brings up a real Controller binary built from this checkout on a throwaway lab `opnwlab-<6hex>`, the same compose project and Router setup the E2E harness uses (`controller/e2e/docker_lab.py`). You drive it with any command through `exec`. Every path below is relative to the repo root; run everything from there.

```sh
V=".claude/skills/verify-controller/verify.py"
```

## Launch

```sh
python3 -I $V up          # stdout: the lab name, e.g. opnwlab-06253e; progress on stderr
```

`up` builds `controller/bin/openpiso` (`make -C controller build`), starts router/controller/internet containers, takes the stock CHR to the HotSpot baseline, starts the Controller with a fresh admin password, logs in, onboards the Router, Adopts `hs1`, seeds Coin rates `1=60, 5=360`, and leases one Device (`<lab>-device`, MAC `02:00:00:0E:00:01`). It returns only when that Device reaches the Portal's `/healthz`, so a 0 exit means the lab is ready to drive. Warm cache: about 35 s. If any step fails, `up` removes the lab it created.

Labs use Docker-assigned ports and their own names, so several worktrees, an E2E run and the fixed `opnwdev` dev lab (`make dev`) run side by side.

Lasting changes from a run:
- In the checkout (all gitignored): `controller/bin/openpiso`, `controller/e2e/__pycache__/`, `lab/chr/__pycache__/`.
- Outside: the run dir `~/.local/state/openpiso-verify/<lab>/` (or under `$XDG_STATE_HOME`). The first run on a machine also downloads the CHR image into `~/.cache/openpiso/chr/` and builds the Docker image `opnwlab-image:<hash>`; both are shared with E2E and kept.
- The ledger lives on the controller container's tmpfs and goes with the lab.

## Doctor

```sh
python3 -I $V doctor
```

Read-only; starts nothing. Checks Docker, `/dev/kvm`, Go, the cached CHR image and the lab image, then lists every `opnwlab-*` lab on the machine, marking the ones this helper owns (they have a run dir). A lab without a run dir belongs to an E2E run: leave it alone. There are no outside keys to check: the app needs none.

## Drive

```sh
python3 -I $V exec <lab> -- <command...>
```

The command runs on the host, exit status passed through, with:

| Variable | What it is |
|---|---|
| `OPNW_URL` | Controller base URL from the host (admin routes, `/healthz`) |
| `OPNW_JAR` | Netscape cookie jar with a logged-in admin session (`curl -b`); the cookie's path is `/admin`, so it rides only on `/admin/...` |
| `OPNW_ADMIN_PASSWORD_FILE` | admin password (0600); read it from the file, never print it |
| `OPNW_DEVICE`, `OPNW_DEVICE_IP`, `OPNW_DEVICE_MAC` | the leased Device container |
| `OPNW_PORTAL` | Portal base URL as the Device sees it |
| `OPNW_ROUTER`, `OPNW_ROUTER_PASSWORD` | Router REST `host:port` (HTTPS, self-signed: `curl -k -u admin:...`) and the lab constant admin password |
| `OPNW_OUT` | the run dir; write evidence here |
| `OPNW_LAB` | the lab name |

Who reaches what:
- **Admin routes**: from the host as the signed-in admin (`curl -b "$OPNW_JAR" "$OPNW_URL/admin/state"`). The route table is `GET /admin/routes`; `/admin/state` is the JSON readback of the ledger.
- **Portal API** (`/portal/api/*`): only from the Device, because the Controller identifies a Device by its address in the Router's HotSpot hosts. A host curl is not a Device. Drive it as `docker exec "$OPNW_DEVICE" curl -sS --data 'k=v' "$OPNW_PORTAL/portal/api/redeem"`. Every reply, refusals included, is one portal-state JSON; assert on `message.code`, not `message.text`.
- **Router**: RouterOS REST at `https://$OPNW_ROUTER/rest/...`, e.g. `/ip/hotspot/user?name=op-<Time Credit ID>`.
- **Out of reach**: the Vendo wire (`/vendo/messages`; no paired Vendo on this lab), a second Device, Pause/Rebind across Devices, the update helper. Add a Device with `docker_lab.Lab.device` + `device_lease` in a script of your own if a claim needs one.

Put a multi-call flow in a script and run it through `exec`, as the shipped scripts do. A script that changes something meant to outlast it (rates, settings) restores it on exit (`trap`); records the Controller refuses to delete, such as a redeemed Voucher, stay until `down`.

## Evidence

Everything lands in `$OPNW_OUT` (`~/.local/state/openpiso-verify/<lab>/`): `lab.json` (URLs, Device, binary, `git_head`), `cookies.txt`, `admin-password`, plus what your commands write. Each shipped script writes a subdirectory of JSON replies and prints one `PASS`/`FAIL` line. Report that line, the run dir path and the `git_head`; quote a reply with `jq` filters, not whole files.

A run proves what the Controller's HTTP surface returns and what it writes to the ledger and the Router. It does not prove the screens (use `lab/dev/dev.py shots` or the E2E browser groups), the real Vendo board, timing of the Router's metering over minutes, or the ARMv7 build.

## Cleanup

```sh
python3 -I $V down <lab>
docker ps -a --format '{{.Names}}' | grep '^<lab>-'; docker network ls --format '{{.Name}}' | grep '^<lab>-'   # both print nothing
```

`down` accepts only `opnwlab-<6hex>` names and removes that lab's containers and networks. The run dir survives; delete it by hand when the evidence is no longer wanted.

## Feature map

| Area | Entry point | Needs | Proven |
|---|---|---|---|
| Lab up, Router onboarded, `hs1` Adopted, Device on the Portal | `verify.py up` | Docker, `/dev/kvm`, Go, CHR cache | 2026-11-23: 2 clean runs, 34 s warm; Device reaches Portal `/healthz`; `down` left no containers or networks |
| Admin and Portal state readback | `state_readback.sh` | lab up | 2026-11-23: 1 run; Router `ready`, Adopted `hs1`, Portal rates `1=60,5=360` |
| Voucher generate → Device redeem → ledger + Router | `voucher_redeem.sh` | lab up, Device with no Time Credit yet (run on a fresh lab; a rerun on the same lab is untested) | 2026-11-23: 1 run; `bought` 600, one Time Credit for the Device, HotSpot user `limit-uptime` `10m`, CSV `redeemed_at` set |
| Coin Window + Vendo Report → Time Credit | `/portal/api/coins/open`, `/vendo/messages` | a paired Vendo (`SimulatedVendo`, `controller/e2e/simulated_vendo.py`) | no: the helper pairs no Vendo |
| Pause / Resume, Rebind, Resume Code | `/portal/api/{pause,resume,recover}` | a second Device for Rebind | no: not yet scripted |
| Update helper | `openpiso update check\|apply` | a signed Release | no: out of the lab's reach |
