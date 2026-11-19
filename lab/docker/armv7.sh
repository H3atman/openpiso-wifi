#!/bin/sh
# ARMv7 guest container (controller/e2e/arm_vm.py ArmContainer): boots the pinned Ubuntu armhf guest
# under QEMU with TCG (no KVM for ARM on an x86 host) and 256 MiB, its one NIC bridged through a tap
# onto the lab WAN, where cloud-init gives it the Controller's lab address 10.77.7.1. The serial
# console is the container's log.
set -eu

# Docker's own address stays on the bridge so the published ports still land here.
docker_ip=$(ip -o -4 addr show dev eth0 | awk '{print $4}')

qemu-img create -q -f qcow2 -F qcow2 -b /img/guest.img /tmp/guest.qcow2 4G
xorriso -as mkisofs -quiet -output /tmp/seed.iso -volid cidata -joliet -rock \
  /seed/user-data /seed/meta-data /seed/network-config

ip link add br0 type bridge
ip tuntap add tap0 mode tap
ip addr flush dev eth0
ip link set eth0 master br0
ip link set tap0 master br0
ip link set br0 up
ip link set tap0 up
ip addr add "$docker_ip" dev br0
ip route del default 2>/dev/null || true  # replies to the published ports stay on-link
# Helper address on the lab WAN (the Router container has .253, the stand-in .254). The host's SSH
# and admin HTTP arrive from it, so it is the Controller's admin network.
ip addr add 10.77.7.252/24 dev br0
socat TCP-LISTEN:2222,fork,reuseaddr TCP:10.77.7.1:22,bind=10.77.7.252 2>/dev/null &
socat TCP-LISTEN:18080,fork,reuseaddr TCP:10.77.7.1:18080,bind=10.77.7.252 2>/dev/null &

# tb-size bounds TCG's translation cache, so the container's memory is the guest's 256 MiB plus a
# known overhead.
exec qemu-system-arm -M virt -cpu cortex-a15 -smp 1 -m 256 -accel tcg,tb-size=64 \
  -display none -monitor none -serial stdio \
  -kernel /img/vmlinuz -initrd /img/initrd -append "console=ttyAMA0 root=LABEL=cloudimg-rootfs rw" \
  -drive file=/tmp/guest.qcow2,if=virtio,format=qcow2 \
  -drive file=/tmp/seed.iso,if=virtio,format=raw,readonly=on \
  -netdev tap,id=n,ifname=tap0,script=no,downscript=no -device virtio-net-pci,netdev=n,mac=02:00:00:77:00:01 \
  -device virtio-rng-pci
