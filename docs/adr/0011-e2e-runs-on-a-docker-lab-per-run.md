# E2E runs on a throwaway Docker lab per run, with Linux containers as Devices

E2E used to run on one shared libvirt lab: the `chr-lab` Router VM and a second
CHR, `chr-client`, as the customer Device, guarded by a lock. One run at a time,
an interrupted run left Router state the next one had to clean, and the dev lab
had to come down before E2E could run. Spec
[#25](https://github.com/H3atman/openpiso-wifi/issues/25) moved E2E and the dev
lab to Docker, and #39 deleted the libvirt path.

## Decision

Every E2E run (`make -C controller e2e`, `e2e-armv7`, `e2e-armv7-update`) creates
its own lab as a compose project, `opnwlab-<random>`, and removes it at the end
(`--keep` leaves it up). The lab is:

- the Router: stock RouterOS CHR (pinned version and checksum in
  `controller/e2e/docker_lab.py`) under QEMU/KVM in a container, set up from zero
  over REST on every run. It stays a real CHR: never a fake Router.
- Devices: Linux containers with their own MAC and a DHCP lease from the Router.
  They replace the second CHR.
- Vendos: `controller/e2e/simulated_vendo.py` in a container with its own MAC,
  lease and `opnw-XXXX` hostname, sending every Report from its own address.
- Browsers: Playwright containers on the lab LAN as Devices.
- The Controller: the host-built binary in a container on the lab WAN, or the
  ARMv7 guest under QEMU (TCG) in a container in its place.
- An internet stand-in answering as `1.1.1.1` and `8.8.8.8`; the lab is sealed.

Every lab uses the same addresses on its own private networks. The host reaches
the Router and Controller only through per-lab ports on `127.0.0.1`. The dev lab
is the same compose project, long-lived, under the fixed name `opnwdev`
(`lab/dev/dev.py`).

## Why

- **Isolation.** A run starts from a stock Router and leaves nothing behind, so
  an earlier or interrupted run cannot change its result, and the dev lab no
  longer conflicts with E2E.
- **Parallel runs.** Each worktree, or subagent, verifies its own branch without
  a lock or a queue.
- **Stronger Device scenarios.** A Linux Device changes its MAC for real (a CHR
  virtio NIC ignores a MAC change, so the old lab switched NICs) and keeps the
  whole reply to a refused request.
- **Setup.** Docker, `/dev/kvm` and Python, without sudo, libvirt or a wizard.

It does not reduce memory. Each container has a hard cap (Router 320 MB,
browser 1 GB, ARMv7 guest 512 MB, others 128 MB), so a runaway process is
killed instead of pushing the host into swap. A lab without browsers costs
about 170 MB, and parallel labs add up.

## Verified

- Every scenario group, ported one at a time, ran on the Docker lab with the
  same step names as its committed libvirt report (#27-#35); the full run is 89
  steps (`controller/e2e/reports/docker-walking-skeleton.summary.json`).
- Two labs ran side by side with the same subnets without interfering.
- HotSpot servers on VLAN interfaces and IPv6 on the LAN (#26).
- The dev lab: `up`, `shots`, the proxy `phone` and `vendo` (#36, #37).
- The ARMv7 guest in a container on the lab WAN: boot, install and the full
  run within the 64 MiB Controller RSS target, and the signed-release update
  check (#38; `docker-armv7*.summary.json`).

## What a Linux Device does not exercise

A CHR Device made its requests with RouterOS `/tool/fetch`. That client drops
the body of a non-2xx reply, so Device-side assertions on the libvirt lab were by
status only. A Linux Device's fetch keeps the reply, and the harness checks the
Portal's message where it has one. The flip side: nothing in E2E now runs a
RouterOS HTTP client as a customer. Real phones remain the check for what real
clients do ([`lab/phones`](../../lab/phones)).

## Vendos

Vendos stay `simulated_vendo.py` containers for now. A firmware-sim Vendo,
running the real subvendo board logic in the Vendo container, comes later, and
only once every pairing and Coin Window scenario passes on the Docker lab with
the simulator.

## Known limits

- **Rootless Docker is untested.** The lab is verified on rootful Docker only.
- **Firewall rules lag.** The Router applies firewall rules about 1 s after
  they are written, as on pairing, so a Device can still get a reply just after.
  Device checks that expect a block retry for up to 10 s and count the early
  replies (`DockerLab.unreachable`).
- **ARMv7 under TCG is slow.** The guest boots in about 8 minutes and the full
  ARMv7 run takes about 42 minutes; the boot timeout defaults to 1800 s.

## Removing the old libvirt lab

Nothing removes the old VMs automatically, and no tool in this repo runs these
commands. They are **manual**, for a contributor who no longer needs the
research labs (`lab/chr/README.md` still uses `chr-lab` and `chr-client` to
reproduce its results). Check each name before running it:

```sh
# The customer Device VM and its disk
virsh -c qemu:///system destroy chr-client
virsh -c qemu:///system undefine chr-client --remove-all-storage
# The Router VM, its network and disk
virsh -c qemu:///system destroy chr-lab
virsh -c qemu:///system undefine chr-lab
virsh -c qemu:///system net-destroy hotspot-lan
virsh -c qemu:///system net-undefine hotspot-lan
sudo rm /var/lib/libvirt/images/chr-lab.qcow2
# Leftovers of interrupted runs (transient networks; errors mean they are gone)
virsh -c qemu:///system net-destroy openpiso-e2e-routing
virsh -c qemu:///system net-destroy openpiso-dev-routing
virsh -c qemu:///system list --name | grep '^openpiso-armv7-'   # destroy each one listed
# Research and dev-phone namespaces, and the macvlan networks on virbr-hs
sudo ip netns del hsA; sudo ip netns del hsB; sudo ip link del vA-br; sudo ip link del vB-br
sudo ip netns del devphone; sudo ip link del vDP-br; sudo rm -r /etc/netns/devphone
docker network rm openpiso-e2e-hs openpiso-dev-hs
# Files: the lab lock, the old ARM artifact cache, and the CHR password
rm -f /tmp/openpiso-chr-lab-$(id -u).lock
rm -r controller/e2e/.cache
rm lab/chr/.env   # only if lab/chr/wizard.sh will not be run again
```

## Follow-ups

- **Firmware-sim Vendo**, tracked in issue #41. Gate: every pairing and Coin
  Window scenario passes on the Docker lab with `simulated_vendo.py`.
- **The lab in CI on GitHub Actions**, tracked in issue #42. Gate: the lab is
  verified locally and its run time is known; the runner needs `/dev/kvm` for
  the Router.

## Alternatives considered

- **Keep the shared libvirt lab.** Parallel work would keep queueing to verify,
  and leftover state would keep reaching the next run.
- **Keep a CHR as the Device inside Docker.** It would keep the fake MAC change
  and the status-only refusals, for a second VM per lab.
- **A fake Router.** Scenarios must prove behaviour against real RouterOS.
