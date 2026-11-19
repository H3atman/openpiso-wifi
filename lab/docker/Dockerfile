# One image for every Docker lab role (Router, Controller host, Device); the command picks the role.
# Busybox ip cannot create bridges or taps (iproute2) and Alpine has no udhcpd (dnsmasq).
FROM alpine:latest
RUN apk add --no-cache qemu-system-x86_64 qemu-img iproute2 curl python3 dnsmasq socat
