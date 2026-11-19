"""Tiny HTTPS RouterOS 6 fixture for the onboarding refusal only.

DockerLab runs this file in one of the lab's containers: `python3 -I non_v7_router.py HOST DIR` prints the address, answers each line on stdin
with the requests seen so far as JSON, and serves until stdin closes.
"""

import http.server
import json
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def certificate(directory: Path, host: str) -> None:
    """Write a throwaway cert.pem and key.pem for host into directory."""
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=openpiso-non-v7", "-addext", f"subjectAltName=IP:{host}",
        "-keyout", str(directory / "key.pem"), "-out", str(directory / "cert.pem"),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class NonV7Router:
    """Serves on every interface; host is the address the Controller reaches it at. With directory,
    it uses the cert.pem and key.pem already there."""

    def __init__(self, host: str, directory: Path | None = None):
        self.host = host
        self.given = directory

    def __enter__(self):
        self.directory = None
        directory = self.given
        if directory is None:
            self.directory = tempfile.TemporaryDirectory(prefix="openpiso-non-v7-")
            directory = Path(self.directory.name)
            certificate(directory, self.host)
        self.requests = []
        requests = self.requests

        class Handler(http.server.BaseHTTPRequestHandler):
            def respond(self):
                requests.append((self.command, self.path))
                if self.command == "GET" and self.path == "/rest/system/resource":
                    body = json.dumps({"version": "6.49.18"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(500, "onboarding must stop at the version check")

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = respond

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(directory / "cert.pem", directory / "key.pem")
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.address = f"https://{self.host}:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        if self.directory is not None:
            self.directory.cleanup()


if __name__ == "__main__":
    with NonV7Router(sys.argv[1], Path(sys.argv[2])) as router:
        print(router.address, flush=True)
        for _ in sys.stdin:
            print(json.dumps(router.requests), flush=True)
