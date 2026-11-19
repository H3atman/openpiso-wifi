"""HTTP proxy for `dev.py --lab docker phone`: runs in the phone container on the lab LAN, so a browser
pointed at it is that Device behind the HotSpot. Plain http requests go out as the Device and their
replies (the HotSpot's redirect included) come back unchanged; CONNECT tunnels https. Stdlib only.

Run: python3 -I phone_proxy.py PORT
"""

import socket
import sys
import threading
import urllib.parse

HOP_BY_HOP = {"connection", "proxy-connection", "keep-alive", "proxy-authorization", "te", "trailer", "upgrade"}


def connect(host: str, port: int) -> socket.socket:
    """IPv4 only: the lab LAN has no IPv6, and musl's resolver fails a name whose AAAA query the
    Router answers SERVFAIL even when its A query succeeded."""
    family, kind, proto, _, address = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0]
    upstream = socket.socket(family, kind, proto)
    upstream.settimeout(10)
    try:
        upstream.connect(address)
    except OSError:
        upstream.close()
        raise
    upstream.settimeout(None)
    return upstream


def pipe(source: socket.socket, sink: socket.socket) -> None:
    try:
        while data := source.recv(65536):
            sink.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (source, sink):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def relay(client: socket.socket, upstream: socket.socket, first: bytes) -> None:
    """Send first upstream, then copy both ways until either side closes."""
    upstream.sendall(first)
    threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
    pipe(upstream, client)


def handle(client: socket.socket) -> None:
    """One request per connection: the upstream request says Connection: close, so the reply ends the
    connection and the browser opens a new one for its next request."""
    with client:
        head = b""
        while b"\r\n\r\n" not in head:
            data = client.recv(65536)
            if not data:
                return
            head += data
        head, _, body = head.partition(b"\r\n\r\n")
        request, *lines = head.decode("latin-1").split("\r\n")
        method, target, version = request.split(" ", 2)
        try:
            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                upstream = connect(host, int(port))
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                with upstream:
                    relay(client, upstream, body)
                return
            url = urllib.parse.urlsplit(target)
            upstream = connect(url.hostname, url.port or 80)
        except (OSError, ValueError) as error:
            client.sendall(f"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
                           f"phone proxy: {target}: {error}\n".encode())
            return
        path = urllib.parse.urlunsplit(("", "", url.path or "/", url.query, ""))
        kept = [line for line in lines if line.partition(":")[0].strip().lower() not in HOP_BY_HOP]
        first = "\r\n".join([f"{method} {path} {version}", *kept, "Connection: close", "", ""]).encode("latin-1")
        with upstream:
            relay(client, upstream, first + body)


def main() -> None:
    server = socket.create_server(("0.0.0.0", int(sys.argv[1])), backlog=64)
    print(f"phone proxy listening on {sys.argv[1]}", flush=True)
    while True:
        client, _ = server.accept()
        threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
