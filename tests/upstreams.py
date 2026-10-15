"""Throwaway local servers that behave like the dev apps people share.

They reproduce the behaviours that broke the old byte-pipe proxy: Host
checks (Vite/webpack/Django), Origin-checked WebSockets (HMR), CORS
allow-lists on backends, localhost URLs in JS, Domain=localhost cookies,
gzip bodies, streaming responses, HTTPS dev servers and IPv6-only binds.
"""

import base64
import gzip
import hashlib
import http.server
import json
import socket
import ssl
import threading
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CERT_FILE = FIXTURES / "localhost-cert.pem"
KEY_FILE = FIXTURES / "localhost-key.pem"
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
SSE_RELEASE_TIMEOUT_SECONDS = 5.0
ECHO_CONTENT_TYPE = "application/x-xposer-echo"


class DevAppHandler(http.server.BaseHTTPRequestHandler):
    """Routes mimicking a dev server; behaviour configured on the server object."""

    protocol_version = "HTTP/1.1"
    server: "DevAppServer"

    def log_message(self, format: str, *args: object) -> None:
        """Keep test output clean."""

    def do_HEAD(self) -> None:
        """HEAD mirrors GET headers."""
        self._route(head_only=True)

    def do_GET(self) -> None:
        """Dispatch GET routes."""
        self._route(head_only=False)

    def do_POST(self) -> None:
        """Echo upload size and digest (supports chunked bodies)."""
        body = self._read_body()
        self._json({"length": len(body), "sha256": hashlib.sha256(body).hexdigest()})

    def do_OPTIONS(self) -> None:
        """CORS preflight honouring the backend's allow-list."""
        origin = self.headers.get("Origin", "")
        self.send_response(204)
        if origin in self.server.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _route(self, head_only: bool) -> None:
        """Pick a behaviour by path."""
        self.server.connections.add(self.client_address)
        if (
            self.server.require_local_host
            and self.headers.get("Host") != f"localhost:{self.server.port}"
        ):
            self._text(
                f"Blocked request. This host ({self.headers.get('Host')}) is not allowed.",
                status=403,
            )
            return
        path = self.path.split("?", 1)[0]
        routes = {
            "/echo": self._echo,
            "/js": self._js,
            "/redirect": self._redirect,
            "/cookie": self._cookie,
            "/cors": self._cors,
            "/gzip": self._gzip,
            "/sse": self._sse,
            "/etag": self._etag,
            "/close": self._close_delimited,
            "/csp": self._csp,
            "/big": self._big,
        }
        handler = routes.get(path, self._echo)
        handler(head_only) if path in ("/echo", "/js") else handler()

    def _echo(self, head_only: bool = False) -> None:
        """Report what the app received."""
        self._json(
            {
                "path": self.path,
                "host": self.headers.get("Host"),
                "origin": self.headers.get("Origin"),
                "referer": self.headers.get("Referer"),
                "cookie": self.headers.get("Cookie"),
                "accept_encoding": self.headers.get("Accept-Encoding"),
                "forwarded_for": self.headers.get("X-Forwarded-For"),
                "if_none_match": self.headers.get("If-None-Match"),
            },
            head_only=head_only,
        )

    def _js(self, head_only: bool = False) -> None:
        """Script with hard-coded localhost URLs, like a frontend's config."""
        backend = self.server.backend_port
        body = (
            f'const API = "http://localhost:{backend}/api";\n'
            f'const HMR = "ws://localhost:{self.server.port}/hmr";\n'
            f'const JSON_URL = "http:\\/\\/127.0.0.1:{backend}\\/x";\n'
            f'const OTHER = "http://localhost:{self.server.unshared_port}/y";\n'
        )
        self._text(body, content_type="text/javascript", etag='"js-v1"', head_only=head_only)

    def _redirect(self) -> None:
        """Absolute redirect to localhost, as frameworks generate from Host."""
        self.send_response(302)
        self.send_header("Location", f"http://localhost:{self.server.port}/target?x=1")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cookie(self) -> None:
        """Cookies a localhost HTTPS app typically sets."""
        self.send_response(200)
        self.send_header("Set-Cookie", "sid=abc; Domain=localhost; Path=/; HttpOnly")
        self.send_header("Set-Cookie", "__Host-csrf=t0k; Path=/; Secure; SameSite=None")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def _cors(self) -> None:
        """Backend that only trusts the frontend's localhost origin."""
        origin = self.headers.get("Origin", "")
        payload = json.dumps({"origin_seen": origin}).encode()
        self.send_response(200)
        if origin in self.server.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", ECHO_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _gzip(self) -> None:
        """Compressed body sent even though identity was requested."""
        raw = f"see http://localhost:{self.server.backend_port}/api for data".encode() * 50
        body = gzip.compress(raw)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self) -> None:
        """Event stream whose second event waits for the test's go-ahead."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self._chunk(b"data: first\n\n")
        self.server.sse_release.wait(SSE_RELEASE_TIMEOUT_SECONDS)
        self._chunk(b"data: second\n\n")
        self.wfile.write(b"0\r\n\r\n")

    def _etag(self) -> None:
        """Conditional GET support with a strong validator."""
        if self.headers.get("If-None-Match") == '"v1"':
            self.send_response(304)
            self.send_header("ETag", '"v1"')
            self.end_headers()
            return
        self._text(f"link http://localhost:{self.server.port}/self", etag='"v1"')

    def _close_delimited(self) -> None:
        """HTTP/1.0-style body ended by closing the connection."""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(f"closing body http://localhost:{self.server.port}/z".encode())
        self.close_connection = True

    def _csp(self) -> None:
        """CSP that forces https and allows a localhost backend."""
        self.send_response(200)
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'self'; connect-src http://localhost:{self.server.backend_port}; "
            "upgrade-insecure-requests",
        )
        self.send_header("Strict-Transport-Security", "max-age=63072000")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _big(self) -> None:
        """Large binary body, to check passthrough integrity."""
        body = bytes(range(256)) * 4096
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, object], head_only: bool = False) -> None:
        """Send a JSON report in a type the proxy never rewrites.

        Echoed localhost values would otherwise be translated back to LAN
        addresses on the way out (correctly), hiding what the app saw.
        """
        self._text(json.dumps(payload), content_type=ECHO_CONTENT_TYPE, head_only=head_only)

    def _text(
        self,
        text: str,
        status: int = 200,
        content_type: str = "text/plain",
        etag: str | None = None,
        head_only: bool = False,
    ) -> None:
        """Send a text body with Content-Length."""
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _chunk(self, data: bytes) -> None:
        """Write one chunk of a chunked body and flush."""
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _read_body(self) -> bytes:
        """Request body with Content-Length or chunked framing."""
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            data = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip(), 16)
                if size == 0:
                    while self.rfile.readline().strip():
                        pass
                    return bytes(data)
                data += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length)


class DevAppServer(http.server.ThreadingHTTPServer):
    """Threaded dev-app stand-in with knobs for each scenario."""

    daemon_threads = True
    block_on_close = False

    def __init__(self, host: str = "127.0.0.1", tls: bool = False) -> None:
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        super().__init__((host, 0), DevAppHandler)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(CERT_FILE, KEY_FILE)
            self.socket = context.wrap_socket(self.socket, server_side=True)
        self.port = int(self.server_address[1])
        self.backend_port = 0
        self.unshared_port = 0
        self.require_local_host = False
        self.allowed_origins: set[str] = set()
        self.sse_release = threading.Event()
        self.connections: set[tuple[str, int]] = set()
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def close(self) -> None:
        """Stop serving and release the port."""
        self.sse_release.set()
        self.shutdown()
        self.server_close()


class WebSocketEchoServer:
    """Accepts a WebSocket handshake only from the app's own localhost origin,
    then echoes raw bytes — enough to prove the proxy pipes frames both ways."""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self.port = int(self._socket.getsockname()[1])
        self.seen_origins: list[str] = []
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def close(self) -> None:
        """Stop accepting."""
        self._socket.close()

    def _accept_loop(self) -> None:
        """Serve each connection on its own thread."""
        while True:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        """Handshake, then echo."""
        with connection:
            request = b""
            while b"\r\n\r\n" not in request:
                data = connection.recv(4096)
                if not data:
                    return
                request += data
            head, _, rest = request.partition(b"\r\n\r\n")
            headers = {}
            for line in head.decode("latin-1").split("\r\n")[1:]:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            origin = headers.get("origin", "")
            self.seen_origins.append(origin)
            if origin != f"http://localhost:{self.port}":
                connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            accept = base64.b64encode(
                hashlib.sha1((headers["sec-websocket-key"] + WEBSOCKET_GUID).encode()).digest()
            ).decode()
            connection.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode()
            )
            if rest:
                connection.sendall(rest)
            try:
                while True:
                    data = connection.recv(4096)
                    if not data:
                        return
                    connection.sendall(data)
            except OSError:
                return


class BannerServer:
    """Server-first protocol (like SSH/SMTP): greets, then echoes."""

    BANNER = b"220 banner-server ready\r\n"

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self.port = int(self._socket.getsockname()[1])
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def close(self) -> None:
        """Stop accepting."""
        self._socket.close()

    def _accept_loop(self) -> None:
        """Greet and echo on each connection."""
        while True:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        """Banner, then echo until closed (probes hang up abruptly: fine)."""
        with connection:
            try:
                connection.sendall(self.BANNER)
                while True:
                    data = connection.recv(4096)
                    if not data:
                        return
                    connection.sendall(data)
            except OSError:
                return


def free_port(host: str = "127.0.0.1") -> int:
    """A port that is free right now on host."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def websocket_handshake(host: str, port: int, request_host: str, origin: str) -> socket.socket:
    """Open a socket and send a WebSocket upgrade request (response unread)."""
    sock = socket.create_connection((host, port), timeout=5)
    key = base64.b64encode(b"0123456789abcdef").decode()
    sock.sendall(
        (
            f"GET /hmr HTTP/1.1\r\nHost: {request_host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n\r\n"
        ).encode()
    )
    return sock


def read_head(sock: socket.socket) -> bytes:
    """Read until the end of a response head."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


class ScriptedServer:
    """Answers any first message with a fixed reply (or stays silent).

    reply=b"-ERR unknown\r\n" mimics Redis, b"HTTP/1.1 400 ..." mimics a
    web server rejecting garbage, None mimics a line-buffered server that
    waits for a newline.
    """

    def __init__(self, reply: bytes | None, echo_after: bool = False) -> None:
        self._reply = reply
        self._echo_after = echo_after
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self.port = int(self._socket.getsockname()[1])
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def close(self) -> None:
        """Stop accepting."""
        self._socket.close()

    def _accept_loop(self) -> None:
        """Serve each connection on its own thread."""
        while True:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        """Reply once to the first data, then optionally echo."""
        with connection:
            try:
                connection.settimeout(5)
                if not connection.recv(4096):
                    return
                if self._reply is None:
                    connection.recv(4096)
                    return
                connection.sendall(self._reply)
                while self._echo_after:
                    data = connection.recv(4096)
                    if not data:
                        return
                    connection.sendall(data)
            except OSError:
                return
