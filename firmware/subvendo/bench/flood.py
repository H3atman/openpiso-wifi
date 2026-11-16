#!/usr/bin/env python3
"""UDP flood for the isr_loopback Ethernet / WiFi load.

  python3 bench/flood.py <board-ip> [--port 9000] [--pps 2000] [--size 512] [--seconds 120]

Start it when the sketch prints `SINK load=eth ip=... port=...`; the sketch waits 15 s before it
starts the pulses. --seconds must cover the three width cases of that load (about 70 s by default).
Stdlib only. Prints packets sent and the achieved rate; the sketch's `load_ops` field is the packets
the board actually received, so compare the two.
"""
import argparse
import socket
import time


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ip")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--pps", type=int, default=2000, help="target packets per second")
    ap.add_argument("--size", type=int, default=512, help="payload bytes")
    ap.add_argument("--seconds", type=int, default=120)
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload = bytes(args.size)
    t0 = time.monotonic()
    sent = 0
    while (now := time.monotonic()) - t0 < args.seconds:
        # Paced against the start time so a slow sendto does not lower the long-run rate.
        if sent >= (now - t0) * args.pps:
            time.sleep(0.0005)
            continue
        sock.sendto(payload, (args.ip, args.port))
        sent += 1
    dt = time.monotonic() - t0
    print(f"sent={sent} seconds={dt:.1f} pps={sent / dt:.0f} bytes_per_s={sent * args.size / dt:.0f}")


if __name__ == "__main__":
    main()
