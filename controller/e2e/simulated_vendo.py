"""Vendo-initiated binary protocol client, shared by the real-CHR E2E scenarios.

Only a paired key is needed, not Router credentials. Direct total assignment in
protocol tests represents durable board reports, including stale/forged traffic;
insert() models the acceptor gate for Coin Window scenarios.
"""

import argparse
import hmac
import json
import secrets
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path

LENGTHS = {1: 25, 2: 21, 3: 21, 4: 17, 5: 25}


def sign(tag: int, fields, key: bytes) -> bytes:
    message = bytes([tag]) + struct.pack(">" + "I" * len(fields), *fields)
    assert len(message) == LENGTHS[tag]
    return message + hmac.digest(key, message, "sha256")


def decode_frames(body: bytes, key: bytes):
    frames = []
    while body:
        length = LENGTHS[body[0]]
        message, mac = body[:length], body[length:length + 32]
        assert len(mac) == 32 and hmac.compare_digest(mac, hmac.digest(key, message, "sha256"))
        frames.append((message[0], struct.unpack(">" + "I" * ((length - 1) // 4), message[1:])))
        body = body[length + 32:]
    return frames


def assert_firmware_vectors():
    key = bytes.fromhex("030a11181f262d343b424950575e656c737a81888f969da4abb2b9c0c7ced5dc")
    vectors = [
        (1, (1, 2, 300, 7, 0xA1B2C3D4, 9),
         "0100000001000000020000012c00000007a1b2c3d400000009",
         "29120c1cab1314363b4b8f14a60c298c67a032ec8971c03ed7ab0effd6469aa4"),
        (2, (1, 7, 45000, 0xA1B2C3D4, 9),
         "0200000001000000070000afc8a1b2c3d400000009",
         "92b10e528311f2aa03989a9a257dcbd709d2dc7cc4f6cc23452697a2a7e3e411"),
        (3, (1, 2, 300, 0xA1B2C3D4, 9),
         "0300000001000000020000012ca1b2c3d400000009",
         "9bf26a8c2e43dbf4ac2cc10b79938329c378fae534f0b230c99c1b1462a4eacf"),
    ]
    for tag, fields, message, mac in vectors:
        assert sign(tag, fields, key).hex() == message + mac
    # #21 has not supplied close/closed vectors yet. These fix #15's layouts;
    # firmware-owned HMAC literals will be added when that ticket publishes them.
    assert sign(4, (1, 7, 0xA1B2C3D4, 9), key)[:17].hex() == "040000000100000007a1b2c3d400000009"
    assert sign(5, (1, 2, 300, 7, 0xA1B2C3D4, 9), key)[:25].hex() == "0500000001000000020000012c00000007a1b2c3d400000009"


class SimulatedVendo:
    def __init__(self, url: str, vendo_id: int, key: bytes, epoch: int = 1, timeout: float = 3):
        self.url = url.rstrip("/") + "/vendo/messages"
        self.timeout = timeout
        self.id = vendo_id
        self.key = key
        self.epoch = epoch
        self.total = 0
        self.window_id = 0
        self.nonce = secrets.randbits(32)
        self.rseq = 0
        self.last_ack = 0.0
        self.expires_at = 0.0
        self.closing = False

    def frame(self, tag: int = 1) -> bytes:
        self.rseq = (self.rseq + 1) & 0xFFFFFFFF
        return sign(tag, (self.id, self.epoch, self.total, self.window_id, self.nonce, self.rseq), self.key)

    def post(self, frame: bytes, ignore_ack: bool = False):
        request = urllib.request.Request(self.url, data=frame, headers={"Content-Type": "application/octet-stream"}, method="POST")
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        with response:
            body = response.read()
            if not ignore_ack:
                self.receive(body)
            return response.status, body

    def receive(self, body: bytes):
        for tag, fields in decode_frames(body, self.key):
            assert fields[0] == self.id
            if tag == 3:
                _, epoch, total, nonce, rseq = fields
                if (epoch, total, nonce, rseq) == (self.epoch, self.total, self.nonce, self.rseq):
                    self.last_ack = time.monotonic()
                    if self.closing:
                        self.window_id = 0
                        self.closing = False
            elif tag == 2:
                _, window_id, expires_ms, nonce, rseq = fields
                if (nonce, rseq) == (self.nonce, self.rseq) and not self.closing:
                    self.window_id = window_id
                    self.expires_at = time.monotonic() + min(expires_ms, 600000) / 1000
            elif tag == 4:
                _, window_id, nonce, rseq = fields
                if (nonce, rseq, window_id) == (self.nonce, self.rseq, self.window_id):
                    self.closing = True
                    self.expires_at = 0
            else:
                raise AssertionError(f"unexpected Controller message tag {tag}")

    def insert(self, pulses: int):
        now = time.monotonic()
        if not self.window_id or self.closing or now >= self.expires_at or now - self.last_ack > 6:
            raise ValueError("Vendo acceptor is inhibited")
        self.total = (self.total + pulses) & 0xFFFFFFFF

    def heartbeat(self):
        if self.window_id and time.monotonic() >= self.expires_at:
            self.closing = True
        if self.closing:
            time.sleep(0.3)  # Drain the board's window gap before the final total.
        return self.post(self.frame(5 if self.closing else 1))


def main():
    parser = argparse.ArgumentParser(description="Run a paired simulated Vendo heartbeat (Ctrl-C to stop).")
    parser.add_argument("--url", required=True)
    parser.add_argument("--id", type=int, required=True)
    parser.add_argument("--key-file", type=Path, required=True, help="private file containing the 64 hex digits from pairing")
    parser.add_argument("--epoch", type=int, default=1)
    args = parser.parse_args()
    key = bytes.fromhex(args.key_file.read_text().strip())
    if len(key) != 32:
        parser.error("key file must contain a 32-byte secret")
    vendo = SimulatedVendo(args.url, args.id, key, args.epoch)
    while True:
        status, _ = vendo.heartbeat()
        print(json.dumps({"status": status, "total": vendo.total, "window_id": vendo.window_id}))
        time.sleep(2 if time.monotonic() - vendo.last_ack < 2 else 1)


if __name__ == "__main__":
    main()
