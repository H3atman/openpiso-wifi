#!/bin/sh
# Router container: boots a stock RouterOS CHR under QEMU/KVM with ether1 on the lab WAN and ether2
# on the lab LAN. Each Docker interface is bridged to a tap; the CHR never sees Docker's addresses.
set -eu

# Docker's own WAN address stays on the bridge so the published ports still land here.
docker_ip=$(ip -o -4 addr show dev wan0 | awk '{print $4}')

qemu-img create -q -f qcow2 -F raw -b /img/chr.img /tmp/disk.qcow2
for n in wan lan; do
  ip link add "br$n" type bridge
  ip tuntap add "tap$n" mode tap
  ip addr flush dev "${n}0"
  ip link set "${n}0" master "br$n"
  ip link set "tap$n" master "br$n"
  ip link set "br$n" up
  ip link set "tap$n" up
done
ip addr add "$docker_ip" dev brwan
ip route del default 2>/dev/null || true  # replies to the published ports stay on-link
# Helper address on the lab WAN: hands ether1 its address like an ISP would (gateway and DNS are the
# internet stand-in) and relays the published ports to the Router.
ip addr add 10.77.7.253/24 dev brwan

dnsmasq --interface=brwan --bind-interfaces --port=0 \
  --dhcp-range=10.77.7.2,10.77.7.2,255.255.255.0,1h --dhcp-option=3,10.77.7.254 --dhcp-option=6,8.8.8.8,1.1.1.1
# Host -> Router REST over the published ports (plain HTTP until the harness enables www-ssl).
socat TCP-LISTEN:8080,fork,reuseaddr TCP:10.77.7.2:80 2>/dev/null &
socat TCP-LISTEN:8443,fork,reuseaddr TCP:10.77.7.2:443 2>/dev/null &

exec qemu-system-x86_64 -enable-kvm -m 256 -display none -serial null -monitor none \
  -drive file=/tmp/disk.qcow2,if=virtio \
  -netdev tap,id=w,ifname=tapwan,script=no,downscript=no -device virtio-net-pci,netdev=w,mac=02:00:00:77:00:02 \
  -netdev tap,id=l,ifname=taplan,script=no,downscript=no -device virtio-net-pci,netdev=l,mac=02:00:00:aa:00:01
