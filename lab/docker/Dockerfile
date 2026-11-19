# One image for every Docker lab role (Router, Controller host, internet stand-in, Device); the command
# picks the role. The harness tags it by this file's content, so a change here builds a new image.
# Busybox ip cannot create bridges or taps (iproute2) and Alpine has no udhcpd (dnsmasq).
FROM alpine:3.24.4
RUN apk add --no-cache qemu-system-x86_64 qemu-img iproute2 curl python3 dnsmasq socat
