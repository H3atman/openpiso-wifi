#!/usr/bin/env python3
"""Log a bench sketch's serial output and collect its RESULT lines.

  .venv/bin/python bench/capture.py <test> [--port /dev/ttyUSB0] [--send CHAR] [--until REGEX]

- Raw log: bench/out/<test>-<UTC timestamp>.log, one line per serial line, prefixed with the host UTC time.
- Every `RESULT ...` line is appended to bench/out/results.md as a table row (utc | test | log | result).
- flash_ring soak: the host remembers the last `V <n>` line printed before each boot. Its RESULT line gets
  last_printed=<n> and printed_minus_restored=<n - restored>. A value > 0 means a value was printed
  but its flash write did not survive: a durability violation (the sketch writes flash before printing).
- --send CHAR is typed when the sketch prints `CMD?` (flash_ring: t = timing, w = wipe + soak).
- Stops on --until (default: a sketch's DONE line, or the flash_id / flash_ring timing result) or Ctrl-C.
  If the port disappears (board powered through the adapter), it waits and reopens it.
"""
import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import serial

OUT = Path(__file__).resolve().parent / "out"
DEFAULT_UNTIL = r"^(DONE|SEQUENCE DONE)|^RESULT (flash_id|flash_ring_timing) "


def utc() -> datetime:
    return datetime.now(timezone.utc)


def open_port(port: str, baud: int) -> serial.Serial:
    while True:
        try:
            return serial.Serial(port, baud, timeout=0.2)
        except serial.SerialException:
            time.sleep(0.5)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("test", help="name used in the log file and results.md, e.g. flash_ring")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--send", help="character(s) to send when the sketch prints CMD?")
    ap.add_argument("--until", default=DEFAULT_UNTIL, help="stop when a line matches this regex")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    start = utc()
    log_path = OUT / f"{args.test}-{start:%Y%m%dT%H%M%SZ}.log"
    results = OUT / "results.md"
    until = re.compile(args.until)
    results_lines: list[str] = []
    last_v: int | None = None  # last "V <n>" value seen
    prev_v: int | None = None  # last_v at the start of the current boot
    ser = open_port(args.port, args.baud)
    print(f"logging {args.port} -> {log_path} (Ctrl-C to stop)", file=sys.stderr)

    with log_path.open("w") as log:

        def emit(text: str) -> None:
            log.write(f"{utc().isoformat(timespec='milliseconds')} {text}\n")
            log.flush()
            print(text)

        buf = b""
        try:
            while True:
                try:
                    buf += ser.read(256)
                except (serial.SerialException, OSError):
                    emit("# port lost, waiting to reopen")
                    ser.close()
                    ser = open_port(args.port, args.baud)
                    emit("# port reopened")
                    continue
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").strip("\r")
                    if line.startswith("BOOT "):
                        prev_v = last_v
                    m = re.match(r"V (\d+) ", line)
                    if m:
                        last_v = int(m.group(1))
                    if line.startswith("RESULT flash_ring_soak ") and prev_v is not None:
                        restored = int(re.search(r"restored=(\d+)", line).group(1))
                        line += f" last_printed={prev_v} printed_minus_restored={prev_v - restored}"
                    if line.startswith("RESULT "):
                        results_lines.append(line)
                    emit(line)
                    if line.startswith("CMD?") and args.send:
                        ser.write(args.send.encode())
                    if until.search(line):
                        raise KeyboardInterrupt
        except KeyboardInterrupt:
            pass

    if results_lines:
        new = not results.exists()
        with results.open("a") as f:
            if new:
                f.write("| utc | test | log | result |\n|---|---|---|---|\n")
            for r in results_lines:
                f.write(f"| {start:%Y-%m-%dT%H:%M:%SZ} | {args.test} | {log_path.name} | `{r}` |\n")
        print(f"appended {len(results_lines)} RESULT line(s) to {results}", file=sys.stderr)
    else:
        print("no RESULT line seen; nothing appended to results.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
