# One image for every Docker lab role (Router, Controller host, ARMv7 guest host, internet stand-in,
# Device); the command picks the role. The harness tags it by this file's content, so a change here
# builds a new image. Busybox ip cannot create bridges or taps (iproute2) and Alpine has no udhcpd
# (dnsmasq). qemu-system-arm and xorriso (its cloud-init seed) are for the ARMv7 guest (armv7.sh).
FROM alpine:3.24.4
RUN apk add --no-cache qemu-system-x86_64 qemu-system-arm qemu-img iproute2 curl python3 dnsmasq socat xorriso
