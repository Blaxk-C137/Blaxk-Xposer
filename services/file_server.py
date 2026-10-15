"""Static file servers for Folder Mode.

Built on the stdlib ThreadingHTTPServer, with the gaps that matter on a
LAN closed: HTTP/1.1 keep-alive, byte ranges (video seeking on phones),
zero-copy sendfile, a fixed MIME table (Windows' registry often maps .js
to text/plain, which breaks module scripts), no-cache revalidation so
edits show up on reload, hidden-file and symlink-escape protection, and
an optional single-page-app fallback.
"""

import email.utils
import functools
import html
import http.server
import io
import os
import socketserver
import stat
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path

from models.service import FolderOptions
from network.socket_utils import apply_bind_policy
from utils.logger import get_logger
from utils.platform_env import IS_WINDOWS

logger = get_logger("fileserver")

LISTEN_HOST_ANY = "0.0.0.0"
THREAD_JOIN_TIMEOUT_SECONDS = 2.0
SERVER_POLL_TIMEOUT_SECONDS = 0.2
DEVICE_ACTIVITY_WINDOW_SECONDS = 300.0
INDEX_FILES = ("index.html", "index.htm")
WELL_KNOWN_DIR = ".well-known"
DEFAULT_MIME_TYPE = "application/octet-stream"
UTF8_TEXT_TYPES = frozenset(
    {"application/javascript", "application/json", "application/manifest+json", "image/svg+xml"}
)
WEB_MIME_TYPES: dict[str, str] = {
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
    ".json": "application/json",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".xml": "application/xml",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".wasm": "application/wasm",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".apk": "application/vnd.android.package-archive",
    ".epub": "application/epub+zip",
}
QUIET_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,
)
LISTING_STYLE = (
    "body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;"
    "background:#f6f5f4;color:#241f31}main{max-width:48rem;margin:0 auto;padding:1rem}"
    "h1{font-size:1.15rem;word-break:break-all}ul{list-style:none;padding:0;margin:0;"
    "background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08)}"
    "li a{display:flex;justify-content:space-between;gap:1rem;padding:.85rem 1rem;"
    "text-decoration:none;color:inherit;border-bottom:1px solid #eee}"
    "li a:active,li a:hover{background:#f0f4fa}.name{word-break:break-all}"
    ".meta{color:#77767b;white-space:nowrap;font-size:.85rem}"
    ".dir .name{font-weight:600;color:#1c71d8}"
    "footer{color:#9a9996;font-size:.8rem;text-align:center;padding:1rem}"
    "@media (prefers-color-scheme:dark){body{background:#1e1e1e;color:#fff}"
    "ul{background:#2a2a2a}li a{border-color:#333}li a:hover{background:#333}"
    ".dir .name{color:#78aeed}}"
)
SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")
SIZE_STEP = 1024


class FileServerError(RuntimeError):
    """Raised when a static file server cannot be started."""


class _FolderRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Serves one request against the server's root with Xposer's rules."""

    protocol_version = "HTTP/1.1"
    # Headers and body go out as separate writes; without TCP_NODELAY every
    # keep-alive response would wait ~40 ms for the client's delayed ACK.
    disable_nagle_algorithm = True
    server: "_FolderHTTPServer"
    _body_range: tuple[int, int] = (0, 0)

    def do_GET(self) -> None:
        """Serve GET, streaming file bodies with sendfile."""
        self.server.record_client(self.client_address[0])
        source = self.send_head()
        if source is None:
            return
        try:
            self._send_body(source)
        finally:
            source.close()

    def do_HEAD(self) -> None:
        """Serve HEAD (headers only)."""
        self.server.record_client(self.client_address[0])
        source = self.send_head()
        if source is not None:
            source.close()

    def handle(self) -> None:
        """Treat clients hanging up mid-transfer (video seeking) as normal."""
        try:
            super().handle()
        except QUIET_CONNECTION_ERRORS:
            self.close_connection = True

    def guess_type(self, path: str | os.PathLike[str]) -> str:
        """MIME type from Xposer's table, never from the OS registry."""
        mime = WEB_MIME_TYPES.get(Path(path).suffix.lower(), DEFAULT_MIME_TYPE)
        if mime.startswith("text/") or mime in UTF8_TEXT_TYPES:
            return f"{mime}; charset=utf-8"
        return mime

    def log_message(self, format: str, *args: object) -> None:
        """Route request logging to xposer's debug channel."""
        logger.debug(f"{self.address_string()} {self.requestline}")

    def send_head(self) -> io.BufferedIOBase | io.BytesIO | None:
        """Headers for the request; returns the body source or None."""
        path = self.translate_path(self.path)
        if self.server.is_forbidden(path):
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        if os.path.isdir(path):
            return self._directory_head(path)
        if not os.path.isfile(path):
            fallback = self.server.spa_index() if self._wants_html() else None
            if fallback is None:
                self.send_error(HTTPStatus.NOT_FOUND, "File not found")
                return None
            path = fallback
        return self._file_head(path)

    def list_directory(self, path: str | os.PathLike[str]) -> io.BytesIO | None:
        """Mobile-friendly folder listing (hidden entries filtered)."""
        try:
            entries = sorted(
                os.scandir(path), key=lambda entry: (not entry.is_dir(), entry.name.lower())
            )
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "Folder not readable")
            return None
        url_path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        rows: list[str] = []
        if url_path not in ("", "/"):
            rows.append(
                '<li class="dir"><a href="../"><span class="name">← Parent folder</span></a></li>'
            )
        for entry in entries:
            if self.server.is_forbidden(entry.path):
                continue
            rows.append(_listing_row(entry))
        if len(rows) == (0 if url_path in ("", "/") else 1):
            rows.append('<li><a><span class="meta">This folder is empty</span></a></li>')
        title = html.escape(url_path or "/")
        page = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{title}</title><style>{LISTING_STYLE}</style></head><body><main>"
            f"<h1>{title}</h1><ul>{''.join(rows)}</ul>"
            f"<footer>Shared with Xposer</footer></main></body></html>"
        ).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        return io.BytesIO(page)

    def _directory_head(self, path: str) -> io.BufferedIOBase | io.BytesIO | None:
        """Redirect to the slash form, serve index.html, or list the folder."""
        parts = urllib.parse.urlsplit(self.path)
        if not parts.path.endswith("/"):
            location = urllib.parse.urlunsplit(
                (parts.scheme, parts.netloc, parts.path + "/", parts.query, parts.fragment)
            )
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        for index in INDEX_FILES:
            candidate = os.path.join(path, index)
            if os.path.isfile(candidate):
                return self._file_head(candidate)
        return self.list_directory(path)

    def _file_head(self, path: str) -> io.BufferedIOBase | None:
        """Send headers for a file, honouring validators and Range."""
        try:
            source = open(path, "rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        try:
            info = os.fstat(source.fileno())
            size = info.st_size
            etag = f'W/"{info.st_mtime_ns:x}-{size:x}"'
            last_modified = self.date_time_string(int(info.st_mtime))
            if self._not_modified(etag, info.st_mtime):
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                source.close()
                return None
            start, length, status = 0, size, HTTPStatus.OK
            byte_range = self._requested_range(size, etag, last_modified)
            if byte_range == "unsatisfiable":
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                source.close()
                return None
            if isinstance(byte_range, tuple):
                start, end = byte_range
                length, status = end - start + 1, HTTPStatus.PARTIAL_CONTENT
            self.send_response(status)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Length", str(length))
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{start + length - 1}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", last_modified)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
        except (OSError, ValueError):
            source.close()
            raise
        self._body_range = (start, length)
        return source

    def _not_modified(self, etag: str, mtime: float) -> bool:
        """True when the client's cached copy is current."""
        if_none_match = self.headers.get("If-None-Match")
        if if_none_match is not None:
            tags = {tag.strip() for tag in if_none_match.split(",")}
            return "*" in tags or etag in tags or etag.removeprefix("W/") in tags
        if_modified_since = self.headers.get("If-Modified-Since")
        if if_modified_since is None:
            return False
        try:
            since = email.utils.parsedate_to_datetime(if_modified_since)
        except (TypeError, ValueError):
            return False
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        return int(mtime) <= int(since.timestamp())

    def _requested_range(
        self, size: int, etag: str, last_modified: str
    ) -> tuple[int, int] | str | None:
        """(start, end) for a single satisfiable range, 'unsatisfiable', or None."""
        header = self.headers.get("Range")
        if header is None or size == 0:
            return None
        if_range = self.headers.get("If-Range")
        if if_range is not None and if_range.strip() not in (etag, last_modified):
            return None
        return parse_byte_range(header, size)

    def _send_body(self, source: io.BufferedIOBase | io.BytesIO) -> None:
        """Write the body; files go through sendfile (zero-copy where supported)."""
        if isinstance(source, io.BytesIO):
            self.wfile.write(source.getvalue())
            return
        start, length = self._body_range
        if length:
            self.connection.sendfile(source, offset=start, count=length)

    def _wants_html(self) -> bool:
        """True for navigations to extension-less paths (SPA client routes)."""
        accept = self.headers.get("Accept", "")
        last_segment = urllib.parse.urlsplit(self.path).path.rsplit("/", 1)[-1]
        return "text/html" in accept and "." not in last_segment


class _FolderHTTPServer(http.server.ThreadingHTTPServer):
    """Threaded HTTP server rooted at one folder."""

    daemon_threads = True
    block_on_close = False

    def __init__(self, address: tuple[str, int], directory: Path, options: FolderOptions) -> None:
        self.root = str(directory)
        self.real_root = os.path.realpath(directory)
        self.options = options
        self._client_lock = threading.Lock()
        self._last_seen: dict[str, float] = {}
        handler = functools.partial(_FolderRequestHandler, directory=self.root)
        super().__init__(address, handler)

    def server_bind(self) -> None:
        """Bind with Xposer's reuse policy and skip the slow getfqdn() lookup."""
        apply_bind_policy(self.socket)
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def handle_error(self, request: object, client_address: tuple[str, int]) -> None:
        """Log unexpected handler errors once instead of printing tracebacks."""
        logger.debug(f"Request from {client_address[0]} failed", exc_info=True)

    def record_client(self, ip: str) -> None:
        """Register activity from a client IP."""
        with self._client_lock:
            self._last_seen[ip] = time.monotonic()

    @property
    def connected_devices(self) -> int:
        """Devices active in the last few minutes."""
        cutoff = time.monotonic() - DEVICE_ACTIVITY_WINDOW_SECONDS
        with self._client_lock:
            return sum(1 for seen in self._last_seen.values() if seen >= cutoff)

    def is_forbidden(self, path: str) -> bool:
        """Hidden files (unless enabled) and anything escaping the root."""
        real = os.path.realpath(path)
        if real != self.real_root and not real.startswith(self.real_root.rstrip(os.sep) + os.sep):
            return True
        if self.options.show_hidden:
            return False
        relative = os.path.relpath(os.path.abspath(path), os.path.abspath(self.root))
        for part in Path(relative).parts:
            if part.startswith(".") and part not in (".", "..", WELL_KNOWN_DIR):
                return True
        return _has_hidden_attribute(path)

    def spa_index(self) -> str | None:
        """Root index.html when the SPA fallback is enabled and present."""
        if not self.options.spa_fallback:
            return None
        index = os.path.join(self.root, INDEX_FILES[0])
        return index if os.path.isfile(index) else None


class _FolderServerInstance:
    """One static file server bound to one service."""

    def __init__(
        self, service_id: str, folder_path: Path, port: int, host: str, options: FolderOptions
    ) -> None:
        self.service_id = service_id
        self._address = (host, port)
        self._root = folder_path
        self._options = options
        self._server: _FolderHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def connected_devices(self) -> int:
        """Devices served recently by this instance."""
        return self._server.connected_devices if self._server else 0

    @property
    def is_alive(self) -> bool:
        """True while the serving thread runs."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Bind and serve; raises FileServerError on failure."""
        if not self._root.is_dir():
            raise FileServerError(f"Serving root is not a directory: {self._root}")
        try:
            self._server = _FolderHTTPServer(self._address, self._root, self._options)
        except OSError as exc:
            raise FileServerError(f"Cannot bind port {self._address[1]}: {exc}") from exc
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": SERVER_POLL_TIMEOUT_SECONDS},
            name=f"xposer-fileserver-{self._address[1]}",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"Serving {self._root} on {self._address[0]}:{self._address[1]}")

    def stop(self) -> None:
        """Shut down the server and join its thread."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
            self._thread = None
        logger.info(f"File server on port {self._address[1]} stopped")


class FileServer:
    """Owns all static file server instances, keyed by service id."""

    def __init__(self) -> None:
        self._instances: dict[str, _FolderServerInstance] = {}
        self._lock = threading.Lock()

    def start_server(
        self,
        service_id: str,
        folder_path: Path,
        port: int,
        options: FolderOptions | None = None,
        host: str = LISTEN_HOST_ANY,
    ) -> None:
        """Serve folder_path over HTTP on host:port."""
        with self._lock:
            if service_id in self._instances:
                raise FileServerError(f"Service {service_id} already has a file server")
            instance = _FolderServerInstance(
                service_id, folder_path, port, host, options or FolderOptions()
            )
            instance.start()
            self._instances[service_id] = instance

    def stop_server(self, service_id: str) -> None:
        """Stop and forget the server registered for service_id, if any."""
        with self._lock:
            instance = self._instances.pop(service_id, None)
        if instance is not None:
            instance.stop()

    def stop_all(self) -> None:
        """Stop every file server instance."""
        with self._lock:
            instances = list(self._instances.values())
            self._instances.clear()
        for instance in instances:
            instance.stop()

    def is_running(self, service_id: str) -> bool:
        """Return True when service_id has a live serving thread."""
        with self._lock:
            instance = self._instances.get(service_id)
        return instance.is_alive if instance else False

    def connected_devices(self, service_id: str) -> int:
        """Devices served recently for service_id, or 0."""
        with self._lock:
            instance = self._instances.get(service_id)
        return instance.connected_devices if instance else 0


def parse_byte_range(header: str, size: int) -> tuple[int, int] | str | None:
    """Parse a single 'bytes=' range against a file of size bytes.

    Returns (start, end) inclusive, 'unsatisfiable', or None to ignore the
    header (malformed or multi-range: the full file is sent instead).
    """
    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    first, separator, last = spec.strip().partition("-")
    if not separator:
        return None
    first, last = first.strip(), last.strip()
    if not first:
        if not last.isdigit() or int(last) == 0:
            return None if not last.isdigit() else "unsatisfiable"
        suffix = min(int(last), size)
        return size - suffix, size - 1
    if not first.isdigit() or (last and not last.isdigit()):
        return None
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size:
        return "unsatisfiable"
    if end < start:
        return None
    return start, end


def _listing_row(entry: os.DirEntry[str]) -> str:
    """One <li> of the folder listing."""
    is_dir = entry.is_dir()
    name = entry.name + ("/" if is_dir else "")
    href = urllib.parse.quote(name)
    try:
        info = entry.stat()
        modified = datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M")
        meta = modified if is_dir else f"{_human_size(info.st_size)} · {modified}"
    except OSError:
        meta = ""
    css = "dir" if is_dir else "file"
    return (
        f'<li class="{css}"><a href="{href}"><span class="name">{html.escape(name)}</span>'
        f'<span class="meta">{html.escape(meta)}</span></a></li>'
    )


def _human_size(size: int) -> str:
    """1536 -> '1.5 KB'."""
    value = float(size)
    for unit in SIZE_UNITS:
        if value < SIZE_STEP or unit == SIZE_UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= SIZE_STEP
    return f"{size} B"


def _has_hidden_attribute(path: str) -> bool:
    """Windows hidden/system attribute (desktop.ini, Thumbs.db…)."""
    if not IS_WINDOWS:
        return False
    try:
        attributes = getattr(os.stat(path), "st_file_attributes", 0)
    except OSError:
        return False
    hidden_mask = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0) | getattr(
        stat, "FILE_ATTRIBUTE_SYSTEM", 0
    )
    return bool(attributes & hidden_mask)
