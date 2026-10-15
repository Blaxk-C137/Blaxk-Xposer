"""HTTP/1.x message heads: parsing, serialising and body-framing rules.

Only what a forwarding proxy needs: request/response heads as ordered
header lists (order and casing are preserved), and the RFC 9112 rules for
where a message body ends.
"""

import re
from dataclasses import dataclass
from enum import Enum

SUPPORTED_VERSIONS = frozenset({"HTTP/1.0", "HTTP/1.1"})
HTTP_11 = "HTTP/1.1"
HTTP_10 = "HTTP/1.0"
HEAD_TERMINATOR = b"\r\n\r\n"
HEADER_ENCODING = "latin-1"
METHOD_PATTERN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "proxy-authenticate",
        "proxy-authorization",
    }
)
NO_BODY_STATUSES = frozenset({204, 304})
FIRST_INFORMATIONAL = 100
FIRST_SUCCESS = 200
SWITCHING_PROTOCOLS = 101
MAX_METHOD_LENGTH = 32
SPACE_BYTE = 0x20
METHOD_BYTES = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ-_")
MAX_REQUEST_LINE_BYTES = 64 * 1024
REQUEST_LINE_VERSIONS = (b" HTTP/1.1", b" HTTP/1.0", b" HTTP/2.0")


class HttpParseError(Exception):
    """Bytes that are not a valid HTTP/1.x message head."""


class BodyFraming(Enum):
    """How the end of a message body is determined."""

    NONE = "none"
    LENGTH = "length"
    CHUNKED = "chunked"
    UNTIL_CLOSE = "until_close"


Headers = list[tuple[str, str]]


def header_value(headers: Headers, name: str) -> str | None:
    """First value of a header (case-insensitive), or None."""
    wanted = name.lower()
    for key, value in headers:
        if key.lower() == wanted:
            return value
    return None


def header_values(headers: Headers, name: str) -> list[str]:
    """Every value of a header, in order."""
    wanted = name.lower()
    return [value for key, value in headers if key.lower() == wanted]


def connection_tokens(headers: Headers) -> set[str]:
    """Lower-cased tokens of every Connection header."""
    tokens: set[str] = set()
    for value in header_values(headers, "connection"):
        tokens.update(token.strip().lower() for token in value.split(",") if token.strip())
    return tokens


def strip_hop_by_hop(headers: Headers) -> Headers:
    """Remove hop-by-hop headers, including any the Connection header names."""
    named = connection_tokens(headers)
    return [
        (key, value)
        for key, value in headers
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() not in named
    ]


def without_headers(headers: Headers, *names: str) -> Headers:
    """Copy of headers minus every header in names."""
    dropped = {name.lower() for name in names}
    return [(key, value) for key, value in headers if key.lower() not in dropped]


@dataclass
class RequestHead:
    """Request line plus headers."""

    method: str
    target: str
    version: str
    headers: Headers

    def header(self, name: str) -> str | None:
        """First value of a header, or None."""
        return header_value(self.headers, name)

    @property
    def keep_alive(self) -> bool:
        """True when the client wants the connection kept open."""
        return _keep_alive(self.version, self.headers)

    @property
    def is_upgrade(self) -> bool:
        """True for protocol-upgrade requests such as WebSocket handshakes."""
        return "upgrade" in connection_tokens(self.headers) and self.header("upgrade") is not None

    @property
    def expects_continue(self) -> bool:
        """True when the client waits for 100 Continue before sending its body."""
        return (self.header("expect") or "").strip().lower() == "100-continue"

    def body_framing(self) -> tuple[BodyFraming, int]:
        """Where this request's body ends; raises HttpParseError if ambiguous."""
        transfer_encoding = self.header("transfer-encoding")
        if transfer_encoding is not None:
            if _is_chunked(transfer_encoding):
                return BodyFraming.CHUNKED, 0
            raise HttpParseError("request Transfer-Encoding must end with chunked")
        length = _content_length(self.headers)
        if length is None or length == 0:
            return BodyFraming.NONE, 0
        return BodyFraming.LENGTH, length


@dataclass
class ResponseHead:
    """Status line plus headers."""

    version: str
    status: int
    reason: str
    headers: Headers

    def header(self, name: str) -> str | None:
        """First value of a header, or None."""
        return header_value(self.headers, name)

    @property
    def keep_alive(self) -> bool:
        """True when the server will keep the connection open."""
        return _keep_alive(self.version, self.headers)

    @property
    def is_informational(self) -> bool:
        """True for 1xx interim responses."""
        return FIRST_INFORMATIONAL <= self.status < FIRST_SUCCESS

    def body_framing(self, request_method: str) -> tuple[BodyFraming, int]:
        """Where this response's body ends, given the request method."""
        if (
            request_method.upper() == "HEAD"
            or self.is_informational
            or self.status in NO_BODY_STATUSES
        ):
            return BodyFraming.NONE, 0
        transfer_encoding = self.header("transfer-encoding")
        if transfer_encoding is not None:
            if _is_chunked(transfer_encoding):
                return BodyFraming.CHUNKED, 0
            return BodyFraming.UNTIL_CLOSE, 0
        length = _content_length(self.headers)
        if length is None:
            return BodyFraming.UNTIL_CLOSE, 0
        if length == 0:
            return BodyFraming.NONE, 0
        return BodyFraming.LENGTH, length


def classify_request_prefix(data: bytes) -> bool | None:
    """Does a connection's first data start like 'GET / HTTP/1.1'?

    True: an HTTP request line. False: some other protocol. None: too few
    bytes to tell yet. Methods must be upper-case (every browser and HTTP
    client sends them so) and the line must end with an HTTP version, so
    text protocols such as memcached's 'get key' are not mistaken for HTTP.
    """
    for index, byte in enumerate(data[: MAX_METHOD_LENGTH + 1]):
        if byte == SPACE_BYTE:
            if index == 0:
                return False
            break
        if byte not in METHOD_BYTES:
            return False
    else:
        return None if len(data) <= MAX_METHOD_LENGTH else False
    newline = data.find(b"\n")
    if newline < 0:
        return None if len(data) < MAX_REQUEST_LINE_BYTES else False
    return data[:newline].rstrip(b"\r").endswith(REQUEST_LINE_VERSIONS)


def parse_request_head(raw: bytes) -> RequestHead:
    """Parse request line and headers; raises HttpParseError."""
    start_line, headers = _split_head(raw)
    parts = start_line.split(" ")
    if len(parts) != 3:
        raise HttpParseError(f"malformed request line: {start_line[:80]!r}")
    method, target, version = parts
    if not METHOD_PATTERN.match(method) or not target:
        raise HttpParseError(f"malformed request line: {start_line[:80]!r}")
    if not version.startswith("HTTP/"):
        raise HttpParseError(f"not an HTTP version: {version[:20]!r}")
    return RequestHead(method=method, target=target, version=version, headers=headers)


def parse_response_head(raw: bytes) -> ResponseHead:
    """Parse status line and headers; raises HttpParseError."""
    start_line, headers = _split_head(raw)
    version, _, rest = start_line.partition(" ")
    status_text, _, reason = rest.partition(" ")
    if not version.startswith("HTTP/1.") or not status_text.isdigit() or len(status_text) != 3:
        raise HttpParseError(f"malformed status line: {start_line[:80]!r}")
    return ResponseHead(version=version, status=int(status_text), reason=reason, headers=headers)


def serialize_request_head(head: RequestHead) -> bytes:
    """Wire bytes for a request head."""
    lines = [f"{head.method} {head.target} {head.version}"]
    lines.extend(f"{key}: {value}" for key, value in head.headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode(HEADER_ENCODING, errors="replace")


def serialize_response_head(head: ResponseHead) -> bytes:
    """Wire bytes for a response head."""
    status_line = f"{head.version} {head.status} {head.reason}".rstrip()
    lines = [status_line]
    lines.extend(f"{key}: {value}" for key, value in head.headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode(HEADER_ENCODING, errors="replace")


def _split_head(raw: bytes) -> tuple[str, Headers]:
    """Split a head into its start line and (name, value) pairs."""
    text = raw.decode(HEADER_ENCODING)
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise HttpParseError("empty message head")
    headers: Headers = []
    for line in lines[1:]:
        if line[:1] in (" ", "\t") and headers:
            name, value = headers[-1]
            headers[-1] = (name, f"{value} {line.strip()}")
            continue
        name, separator, value = line.partition(":")
        if not separator or not name or name != name.strip():
            raise HttpParseError(f"malformed header line: {line[:80]!r}")
        headers.append((name, value.strip()))
    return lines[0], headers


def _is_chunked(transfer_encoding: str) -> bool:
    """True when the final transfer coding is chunked."""
    codings = [coding.strip().lower() for coding in transfer_encoding.split(",")]
    return bool(codings) and codings[-1] == "chunked"


def _content_length(headers: Headers) -> int | None:
    """Content-Length as an int; conflicting or invalid values raise."""
    values = header_values(headers, "content-length")
    if not values:
        return None
    numbers = {item.strip() for value in values for item in value.split(",")}
    if len(numbers) != 1:
        raise HttpParseError("conflicting Content-Length values")
    text = numbers.pop()
    if not text.isdigit():
        raise HttpParseError(f"invalid Content-Length: {text[:20]!r}")
    return int(text)


def _keep_alive(version: str, headers: Headers) -> bool:
    """Persistent-connection rule shared by requests and responses."""
    tokens = connection_tokens(headers)
    if "close" in tokens:
        return False
    if version == HTTP_11:
        return True
    return "keep-alive" in tokens
