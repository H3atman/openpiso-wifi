# The Controller is written in Go

The Controller ships as a single static Go binary built for armv7 and arm64. The cheapest supported boards (Orange Pi Zero/One/PC, Allwinner H2+/H3) are 32-bit armv7 with as little as 256 MB RAM: Bun has no armv7 build, and Node is too heavy for that memory floor. Go gives static cross-compiled binaries, a cgo-free SQLite driver, and a low memory footprint with less build friction than Rust. The portal UI may still be written in TypeScript, but it is prebuilt into static assets served by the Go binary; no JS runtime runs on the device.
