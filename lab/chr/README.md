# CHR HotSpot identity lab
A throwaway RouterOS CHR VM plus two network-namespace clients, answering Q2 in
`docs/research/hotspot-identity-lab.md` (cases L1-L4: does the HotSpot cookie survive a MAC change?).
Run: `./lab/chr/wizard.sh` (needs sudo, libvirt, KVM; re-runnable, finished steps are skipped).
Creates: network `hotspot-lan` (bridge `virbr-hs`), VM `chr-lab`, image `/var/lib/libvirt/images/chr-lab.qcow2`,
namespaces `hsA`/`hsB` (veths `vA-br`/`vB-br`), gitignored `lab/chr/.env` (CHR password), `lab/chr/hotspot.rsc`,
results in `docs/research/results/<date>-chr-cookie-mac/`.
Teardown:
- `virsh -c qemu:///system destroy chr-lab; virsh -c qemu:///system undefine chr-lab`
- `virsh -c qemu:///system net-destroy hotspot-lan; virsh -c qemu:///system net-undefine hotspot-lan`
- `sudo ip netns del hsA; sudo ip netns del hsB; sudo ip link del vA-br; sudo ip link del vB-br`
- `sudo rm /var/lib/libvirt/images/chr-lab.qcow2`

## Session-control lab (L5-L8)
Answers L5-L8 in `docs/research/mikrotik-controller.md` §8. The controller is simulated by `lab/chr/session_lab.py` (REST `active login` / `active remove`); the client is a second CHR VM, `chr-client`, with two NICs on `hotspot-lan`.
- **Client VM, no sudo:** with `chr-lab` shut off, `virsh -c qemu:///system vol-clone --pool default chr-lab.qcow2 chr-client.qcow2`. `virt-install` `chr-client` with only the `default` network, mac `52:54:00:c1:1e:01`.
- **Strip the cloned config** with `lab/chr/rcon.py` (serial console). The new NIC appears as ether3 and the old LAN config points at missing interfaces. Remove hotspot/users/profiles/dhcp-server/pool/nat/addresses/dhcp-clients; rename the ether1 default-name to `mgmt`; add a dhcp-client on mgmt; set `discover-interface-list=none`.
- **Attach two NICs on hotspot-lan:** `virsh -c qemu:///system attach-interface chr-client --type network --source hotspot-lan --model virtio --mac 02:00:00:00:0c:01 --live --config`, and the same with `0c:02`.
- **Over REST:** name them `lan1` (`*5`) and `lan2` (`*6`, disabled); add dhcp-clients with `add-default-route=no`; add the route `1.1.1.1/32` via `10.5.50.1`; add the scheduler `ping-gen` (`interval=2s`, `on-event="/ping 1.1.1.1 count=1"`, disabled).
- **Why two NICs:** CHR virtio NICs silently ignore `mac-address` changes, from both REST and the console. A MAC change is done by switching which NIC is enabled.
- **Run:** `set -a; . lab/chr/.env; set +a; CLIENT_IP=<mgmt ip> python3 -I lab/chr/session_lab.py <out dir>`
- **Teardown:** `virsh -c qemu:///system destroy chr-client; virsh -c qemu:///system undefine chr-client --remove-all-storage`

## VLAN auto-select lab (#41)

`make -C controller e2e` owns the existing `chr-lab` and `chr-client` exclusively.
It runs the prerequisite before application scenarios, using `vlan_lab.py`:

- Router `ether2` is the existing NIC on `hotspot-lan` (`virbr-hs`). It carries
  tagged VLANs 10 and 20, with interfaces named `opnw-vlan10` / `opnw-vlan20`.
- Each VLAN has its own same-named HotSpot server, profile, DHCP server and pool.
  Networks are `10.5.10.0/24` and `10.5.20.0/24`, gateway `.1`, pools `.100-.200`.
- `chr-client` has `opnw-vlan10` over `lan1` and `opnw-vlan20` over `lan2`,
  with independent DHCP clients, not default routes via the management NIC.
- The untagged `hs1` / `10.5.50.0/24` lab remains intact for the older scenarios.
  No VM is destroyed, undefined or recreated; `chr-client` remains running.

The prerequisite requests DHCP and pings each VLAN gateway, dumps the real
`/ip/hotspot/host` rows to
`docs/research/results/2026-10-29-chr-vlan-autoselect/hosts.json`, and stops if
`server` is missing or wrong. Observed: `server=opnw-vlan10` / `opnw-vlan20`,
`vlan-id=10` / `20`, and **no `bridge-port` field** on either row. No bridge-port
inference or IP-prefix mapping is used by the Controller.

The application scenario recreates this fixture after the legacy scenarios.
`VlanLab.board(vlan, number)` creates an `opnw-board<number>` MACVLAN on that
client VLAN, obtains a real DHCP lease, and pings to establish a Router host.
MACs are `02:00:00:41:00:<number>`. `board(vlan, number, hostname=...)`
can advertise DHCP option 12 on its initial lease; the #42 scenario uses
`opnw-0c01`. Pairing POSTs the MAC only, then sends a signed binary Report from
the board IP. A per-board source-policy route uses the **MACVLAN** gateway, not
its parent VLAN interface, so both IP and Ethernet source MAC identify the board.

`controller/e2e/flash_pairing.py` uses a temporary RouterOS script to send the
Report with `/tool fetch src-address=<board IP>`. Hex-escaped script literals
construct the binary payload on RouterOS, avoiding JSON text/UTF-8 corruption of
the HMAC. The script is removed in `finally`; no sudo or VM changes are needed.
Admin state, rejection audit, static lease, bypass, IPv4/IPv6 isolation and the
hostname in admin are asserted against the real Controller and Router. Later
protocol simulation reports may still originate on the harness host, after
first-contact adoption. The legacy Coin Window scenarios also use a real
untagged MACVLAN for their Vendo rather than a made-up MAC/IP.

For a focused run after building the Controller:

```sh
python3 -I controller/e2e/run.py --binary controller/bin/openpiso --only pairing --report /tmp/openpiso-flash-pairing.json
```

Client source-policy tables route VLAN Device HTTP through the appropriate
VLAN gateway. The temporary `openpiso-e2e-routing` network installs host return
routes for all three `/24`s; the lab-only source-NAT exemption covers
`10.5.0.0/16` to the Controller only. Existing `openpiso-dev-routing` conflicts
with the return route: finish `lab/dev` use and run its documented `down`
before E2E. Do not stop either VM.

Every fixture use has a `finally` teardown: unpair simulated boards, remove
scenario source rules/routes, then remove board MACVLANs and DHCP clients,
VLAN DHCP clients, HotSpot servers, DHCP leases/servers/networks, profiles,
addresses, pools and VLAN interfaces. Original physical NIC enabled states
are restored. The harness removes its temporary routing NIC/network on exit.
After an interrupted process, use `VlanLab.teardown()` with authenticated REST
callables before setup; only `opnw-vlan10`, `opnw-vlan20` and `opnw-board*`
are fixture-owned. Never use the destructive VM teardown commands above for
this scenario.
