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

A research record. E2E ran this fixture on `chr-lab` and `chr-client` until #39;
the Docker lab now sets up the same VLAN HotSpots, Devices and boards itself
(`DockerLab.vlan_up`, `vlan_board`; [ADR 0012](../../docs/adr/0012-e2e-runs-on-a-docker-lab-per-run.md)).
`vlan_lab.py` and its result file are kept as recorded; nothing runs them.

- Router `ether2` is the existing NIC on `hotspot-lan` (`virbr-hs`). It carries
  tagged VLANs 10 and 20, with interfaces named `opnw-vlan10` / `opnw-vlan20`.
- Each VLAN has its own same-named HotSpot server, profile, DHCP server and pool.
  Networks are `10.5.10.0/24` and `10.5.20.0/24`, gateway `.1`, pools `.100-.200`.
- `chr-client` has `opnw-vlan10` over `lan1` and `opnw-vlan20` over `lan2`,
  with independent DHCP clients, not default routes via the management NIC.

The prerequisite requested DHCP and pinged each VLAN gateway, and dumped the real
`/ip/hotspot/host` rows to
`docs/research/results/2026-10-29-chr-vlan-autoselect/hosts.json`. Observed:
`server=opnw-vlan10` / `opnw-vlan20`, `vlan-id=10` / `20`, and **no
`bridge-port` field** on either row. No bridge-port inference or IP-prefix
mapping is used by the Controller.

`VlanLab.board(vlan, number)` creates an `opnw-board<number>` MACVLAN on a
client VLAN, obtains a real DHCP lease, and pings to establish a Router host
(MACs `02:00:00:41:00:<number>`, optional DHCP option 12 hostname). Fixture-owned
names are `opnw-vlan10`, `opnw-vlan20` and `opnw-board*`; `VlanLab.teardown()`
removes them with authenticated REST callables. Never use the destructive VM
teardown commands above for this fixture.
