"""Tiny HTTPS RouterOS 6 fixture for the onboarding refusal only."""

import http.server
import json
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path


class NonV7Router:
    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="openpiso-non-v7-")
        cert = Path(self.directory.name) / "cert.pem"
        key = Path(self.directory.name) / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=openpiso-non-v7", "-addext", "subjectAltName=IP:192.168.122.1",
            "-keyout", str(key), "-out", str(cert),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        context.load_cert_chain(cert, key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.address = f"https://192.168.122.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()
