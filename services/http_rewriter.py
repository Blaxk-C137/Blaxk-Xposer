"""Pure rewriting rules that make localhost dev servers work for LAN devices.

A dev server believes it lives at http://localhost:PORT. A phone reaches it
as http://192.168.1.5:PORT or http://myapp.local:PORT. Unless both views
are translated, dev servers reject the request (Host/Origin checks), the
frontend calls the phone's own localhost for its API, and backends refuse
the LAN origin via CORS. Everything here is a pure function over strings
and bytes so every rule is unit-testable without sockets.
"""

import re
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from html import escape

LOCAL_COOKIE_DOMAINS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
SECURE_COOKIE_PREFIX = "__Secure-"
HOST_COOKIE_PREFIX = "__Host-"
RENAMED_SECURE_PREFIX = "XpSecure__"
RENAMED_HOST_PREFIX = "XpHost__"
DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
MAX_PORT_DIGITS = 5
GZIP_WBITS = 16 + zlib.MAX_WBITS
ZLIB_HEADER_BYTES = 2
RECOMPRESS_LEVEL = 5
ERROR_PAGE_REFRESH_SECONDS = 3
URL_HEADERS = frozenset({"location", "content-location", "refresh", "link"})
CSP_HEADERS = frozenset({"content-security-policy", "content-security-policy-report-only"})
DROPPED_RESPONSE_HEADERS = frozenset({"strict-transport-security", "alt-svc"})
MIXED_CONTENT_DIRECTIVES = frozenset({"upgrade-insecure-requests", "block-all-mixed-content"})
REWRITABLE_MEDIA_TYPES = frozenset(
    {
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "application/json",
        "application/x-ndjson",
        "application/xml",
        "application/xhtml+xml",
        "application/manifest+json",
    }
)
CACHE_LIFETIME_DIRECTIVES = ("max-age", "s-maxage", "immutable")

_LOCAL_URL_TEXT = (
    r"(?P<scheme>https?|wss?):(?P<slashes>//|\\/\\/)"
    r"(?P<host>localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0):(?P<port>\d{1,5})(?!\d)"
)
LOCAL_URL_PATTERN = re.compile(_LOCAL_URL_TEXT)
LOCAL_URL_PATTERN_BYTES = re.compile(_LOCAL_URL_TEXT.encode("ascii"))
LOCAL_HOST_MARKERS = (b"localhost", b"127.0.0.1", b"[::1]", b"0.0.0.0")
# Every literal a match can start with; a chunk's tail is held back only
# while it could still grow into one of these followed by a port.
URL_LITERALS: tuple[bytes, ...] = tuple(
    f"{scheme}:{slashes}{host}:".encode("ascii")
    for scheme in ("http", "https", "ws", "wss")
    for slashes in ("//", "\\/\\/")
    for host in ("localhost", "127.0.0.1", "[::1]", "0.0.0.0")
)
URL_START_BYTES = frozenset(literal[0] for literal in URL_LITERALS)
STREAM_HOLDBACK_BYTES = max(len(literal) for literal in URL_LITERALS) + MAX_PORT_DIGITS
VERSIONED_ETAG_PATTERN = re.compile(
    r'^W/"(?P<opaque>[^"]*)\.xp(?P<version>\d+)(?P<strength>[sw])"$'
)
ORIGIN_PATTERN = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://(?P<authority>[^/?#]+)")


@dataclass(frozen=True)
class PublicEndpoint:
    """How LAN devices reach one shared local port."""

    local_port: int
    public_port: int
    ip_host: str
    name_host: str | None = None
    public_scheme: str = "http"
    upstream_tls: bool = False

    @property
    def local_origin(self) -> str:
        """The origin the local app believes it is served from."""
        scheme = "https" if self.upstream_tls else "http"
        return f"{scheme}://localhost:{self.local_port}"

    def public_authority(self, prefer_name: bool) -> str:
        """host:port a LAN device should use, preferring the custom link if asked."""
        host = self.name_host if prefer_name and self.name_host else self.ip_host
        return f"{host}:{self.public_port}"

    def public_authorities(self) -> tuple[str, ...]:
        """Every host:port that reaches this endpoint from the LAN (lower-case)."""
        hosts = [self.ip_host] + ([self.name_host] if self.name_host else [])
        return tuple(f"{host.lower()}:{self.public_port}" for host in hosts)


class RewriteMap:
    """Immutable lookup of every shared local port and its public address."""

    __slots__ = ("version", "_by_local", "_by_public")

    def __init__(self, endpoints: Iterable[PublicEndpoint] = (), version: int = 0) -> None:
        self.version = version
        self._by_local: dict[int, PublicEndpoint] = {}
        self._by_public: dict[str, PublicEndpoint] = {}
        for endpoint in endpoints:
            self._by_local[endpoint.local_port] = endpoint
            for authority in endpoint.public_authorities():
                self._by_public[authority] = endpoint

    @property
    def endpoints(self) -> tuple[PublicEndpoint, ...]:
        """Every endpoint, ordered by local port."""
        return tuple(self._by_local[port] for port in sorted(self._by_local))

    def for_local_port(self, port: int) -> PublicEndpoint | None:
        """Endpoint sharing local port, if that port is shared."""
        return self._by_local.get(port)

    def for_public_authority(self, authority: str) -> PublicEndpoint | None:
        """Endpoint reached through a LAN host:port, if any."""
        return self._by_public.get(authority.lower())

    def same_endpoints(self, endpoints: Iterable[PublicEndpoint]) -> bool:
        """True when endpoints describe exactly this map's content."""
        return set(endpoints) == set(self._by_local.values())


@dataclass(frozen=True)
class OriginSwap:
    """An Origin the proxy translated, remembered to translate CORS back."""

    public: str
    local: str


@dataclass
class RequestContext:
    """Facts about one request that the rewriting rules need."""

    endpoint: PublicEndpoint
    client_authority: str
    client_ip: str
    rewrite_map: RewriteMap
    bridging_tls: bool = False
    client_accepts_gzip: bool = False
    cached_rewritten: bool = False
    origin_swap: OriginSwap | None = None
    unresolved_ports: set[int] = field(default_factory=set)

    @property
    def uses_custom_link(self) -> bool:
        """True when the device reached us through the .local name."""
        name = self.endpoint.name_host
        return bool(name) and self.client_authority.lower().startswith(f"{name.lower()}:")


# ---- URL translation ----


def public_url_prefix(scheme: str, slashes: str, port: int, context: RequestContext) -> str | None:
    """Public replacement for '<scheme>://localhost:<port>', or None if unshared."""
    if port == context.endpoint.local_port:
        authority, public_scheme = context.client_authority, "http"
    else:
        endpoint = context.rewrite_map.for_local_port(port)
        if endpoint is None:
            return None
        authority = endpoint.public_authority(context.uses_custom_link)
        public_scheme = endpoint.public_scheme
    if scheme.lower().startswith("ws"):
        public_scheme = "wss" if public_scheme == "https" else "ws"
    return f"{public_scheme}:{slashes}{authority}"


def rewrite_text_urls(text: str, context: RequestContext) -> str:
    """Translate localhost URLs inside a header value or short text."""

    def replace(match: re.Match[str]) -> str:
        port = int(match["port"])
        replacement = public_url_prefix(match["scheme"], match["slashes"], port, context)
        if replacement is None:
            context.unresolved_ports.add(port)
            return match.group(0)
        return replacement

    return LOCAL_URL_PATTERN.sub(replace, text)


class StreamingUrlRewriter:
    """Translates localhost URLs in a body that arrives in arbitrary chunks.

    Only a tail that could still grow into a URL (such as 'http://loc') is
    held back until the next chunk, so streamed events are forwarded
    immediately; flush() releases whatever is left at the end.
    """

    def __init__(self, context: RequestContext) -> None:
        self._context = context
        self._pending = b""

    def feed(self, chunk: bytes) -> bytes:
        """Accept more body bytes; return whatever is safe to emit now."""
        data = self._pending + chunk if self._pending else chunk
        output, consumed = self._rewrite(data, _viable_tail_start(data))
        self._pending = data[consumed:]
        return output

    def flush(self) -> bytes:
        """Emit the held-back tail at end of body."""
        data, self._pending = self._pending, b""
        output, _ = self._rewrite(data, len(data))
        return output

    def _rewrite(self, data: bytes, limit: int) -> tuple[bytes, int]:
        """Rewrite matches starting before limit; return (output, bytes consumed)."""
        if not any(marker in data for marker in LOCAL_HOST_MARKERS):
            return data[:limit], limit
        pieces: list[bytes] = []
        position = 0
        for match in LOCAL_URL_PATTERN_BYTES.finditer(data, 0, limit + STREAM_HOLDBACK_BYTES):
            if match.start() >= limit:
                break
            port = int(match["port"])
            replacement = public_url_prefix(
                match["scheme"].decode("ascii"),
                match["slashes"].decode("ascii"),
                port,
                self._context,
            )
            if replacement is None:
                self._context.unresolved_ports.add(port)
                continue
            pieces.append(data[position : match.start()])
            pieces.append(replacement.encode("ascii"))
            position = match.end()
        consumed = max(position, limit)
        pieces.append(data[position:consumed])
        return b"".join(pieces), consumed


def _viable_tail_start(data: bytes) -> int:
    """Index where a possibly-incomplete URL begins at the end of data, else len(data)."""
    window_start = max(0, len(data) - STREAM_HOLDBACK_BYTES)
    for index in range(window_start, len(data)):
        if data[index] in URL_START_BYTES and _could_become_url(data[index:]):
            return index
    return len(data)


def _could_become_url(tail: bytes) -> bool:
    """True if tail is a prefix of a localhost URL, or one missing its last digits."""
    for literal in URL_LITERALS:
        if literal.startswith(tail):
            return True
        if tail.startswith(literal):
            digits = tail[len(literal) :]
            return digits.isdigit() and len(digits) <= MAX_PORT_DIGITS
    return False


# ---- request headers ----


def rewrite_request_headers(
    headers: list[tuple[str, str]], context: RequestContext
) -> list[tuple[str, str]]:
    """Make a LAN request look like the developer's own browser sent it.

    Host becomes localhost:PORT (passes Vite/webpack/Django host checks),
    Origin and Referer move to the matching local origin (passes CORS and
    Next.js cross-site checks), and Xposer's ETag versions are removed.
    Framing and hop-by-hop headers are the proxy session's business.
    """
    result: list[tuple[str, str]] = []
    forwarded_for: str | None = None
    for name, value in headers:
        key = name.lower()
        if key == "host":
            continue
        if key == "origin":
            value = _translate_origin(value, context)
        elif key == "referer":
            value = _translate_referer(value, context)
        elif key == "cookie":
            value = restore_cookie_names(value)
        elif key == "accept-encoding":
            context.client_accepts_gzip = accepts_gzip(value)
            continue
        elif key == "x-forwarded-for":
            forwarded_for = value
            continue
        elif key == "if-none-match":
            restored, versioned = restore_if_none_match(value, context.rewrite_map.version)
            context.cached_rewritten = context.cached_rewritten or versioned
            if restored is None:
                continue
            value = restored
        result.append((name, value))
    result.insert(0, ("Host", f"localhost:{context.endpoint.local_port}"))
    chain = f"{forwarded_for}, {context.client_ip}" if forwarded_for else context.client_ip
    result.append(("X-Forwarded-For", chain))
    result.append(("Accept-Encoding", "identity"))
    return result


def _translate_origin(value: str, context: RequestContext) -> str:
    """Map a LAN origin onto the local origin of whichever app it belongs to."""
    local = _local_origin_for(value, context)
    if local is None:
        return value
    context.origin_swap = OriginSwap(public=value, local=local)
    return local


def _translate_referer(value: str, context: RequestContext) -> str:
    """Swap the origin part of a Referer the same way Origin is swapped."""
    match = ORIGIN_PATTERN.match(value)
    if match is None:
        return value
    local = _local_origin_for(match.group(0), context)
    return value if local is None else local + value[match.end() :]


def _local_origin_for(origin: str, context: RequestContext) -> str | None:
    """Local origin behind a public origin string, or None if it is not ours."""
    match = ORIGIN_PATTERN.match(origin.strip())
    if match is None:
        return None
    authority = normalize_authority(match["authority"], match["scheme"].lower())
    endpoint = context.rewrite_map.for_public_authority(authority)
    if endpoint is None and authority == context.client_authority.lower():
        endpoint = context.endpoint
    return endpoint.local_origin if endpoint is not None else None


def normalize_authority(authority: str, scheme: str) -> str:
    """'host' or 'host:port' → lower-case 'host:port' with the default port filled in."""
    authority = authority.lower()
    if authority.startswith("["):
        has_port = "]:" in authority
    else:
        has_port = ":" in authority
    if has_port:
        return authority
    return f"{authority}:{DEFAULT_PORTS.get(scheme, DEFAULT_PORTS['http'])}"


def accepts_gzip(accept_encoding: str) -> bool:
    """True when the client lists gzip without q=0."""
    for item in accept_encoding.split(","):
        coding, _, params = item.strip().partition(";")
        if coding.strip().lower() != "gzip":
            continue
        quality = params.replace(" ", "").lower()
        return quality not in ("q=0", "q=0.0", "q=0.00", "q=0.000")
    return False


def restore_cookie_names(cookie_header: str) -> str:
    """Undo the cookie-prefix renaming applied when bridging HTTPS to http."""
    if RENAMED_SECURE_PREFIX not in cookie_header and RENAMED_HOST_PREFIX not in cookie_header:
        return cookie_header
    pairs = []
    for pair in cookie_header.split(";"):
        stripped = pair.strip()
        if stripped.startswith(RENAMED_SECURE_PREFIX):
            stripped = SECURE_COOKIE_PREFIX + stripped[len(RENAMED_SECURE_PREFIX) :]
        elif stripped.startswith(RENAMED_HOST_PREFIX):
            stripped = HOST_COOKIE_PREFIX + stripped[len(RENAMED_HOST_PREFIX) :]
        pairs.append(stripped)
    return "; ".join(pairs)


def version_etag(etag: str, version: int) -> str:
    """Tag a rewritten body's ETag with the rewrite-map version."""
    weak = etag.startswith("W/")
    opaque = (etag[2:] if weak else etag).strip().strip('"')
    return f'W/"{opaque}.xp{version}{"w" if weak else "s"}"'


def restore_if_none_match(value: str, version: int) -> tuple[str | None, bool]:
    """Turn versioned ETags back into the app's own; drop stale ones.

    Returns (header value or None to drop it, whether a current versioned
    tag was present). A body rewritten under an older map may contain
    outdated links, so its validator is dropped and the app sends a fresh
    copy.
    """
    if value.strip() == "*":
        return value, False
    kept: list[str] = []
    versioned = False
    for item in (part.strip() for part in value.split(",")):
        match = VERSIONED_ETAG_PATTERN.match(item)
        if match is None:
            if item:
                kept.append(item)
            continue
        if int(match["version"]) != version:
            continue
        versioned = True
        opaque = match["opaque"]
        kept.append(f'W/"{opaque}"' if match["strength"] == "w" else f'"{opaque}"')
    return (", ".join(kept) if kept else None), versioned


# ---- response headers ----


def rewrite_response_headers(
    headers: list[tuple[str, str]], context: RequestContext, body_rewritten: bool
) -> list[tuple[str, str]]:
    """Point redirects/links back at LAN addresses and fix CORS and cookies."""
    result: list[tuple[str, str]] = []
    for name, value in headers:
        key = name.lower()
        if key in DROPPED_RESPONSE_HEADERS:
            continue
        if key in URL_HEADERS:
            value = rewrite_text_urls(value, context)
        elif key in CSP_HEADERS:
            value = rewrite_csp(value, context)
        elif key == "access-control-allow-origin":
            value = _restore_allowed_origin(value, context)
        elif key == "set-cookie":
            value = rewrite_set_cookie(value, context.bridging_tls)
        elif body_rewritten and key == "etag":
            value = version_etag(value, context.rewrite_map.version)
        elif body_rewritten and key == "last-modified":
            continue
        elif body_rewritten and key == "cache-control":
            value = _revalidating_cache_control(value)
        result.append((name, value))
    return result


def _restore_allowed_origin(value: str, context: RequestContext) -> str:
    """Report the browser's real origin back when the app allowed the local one."""
    swap = context.origin_swap
    if swap is not None and value.strip() == swap.local:
        return swap.public
    return value


def _revalidating_cache_control(value: str) -> str:
    """Rewritten bodies depend on the share map, so they must be revalidated."""
    lowered = value.lower()
    if any(directive in lowered for directive in CACHE_LIFETIME_DIRECTIVES):
        return "no-cache"
    return value


def rewrite_csp(value: str, context: RequestContext) -> str:
    """Translate localhost sources; drop https-forcing directives when bridging."""
    value = rewrite_text_urls(value, context)
    if not context.bridging_tls:
        return value
    directives = [part.strip() for part in value.split(";")]
    kept = [d for d in directives if d and d.split()[0].lower() not in MIXED_CONTENT_DIRECTIVES]
    return "; ".join(kept)


def rewrite_set_cookie(value: str, bridging_tls: bool) -> str:
    """Make a cookie the app set for localhost stick on the LAN address.

    Domain=localhost would be rejected by the phone's browser, and when the
    app is HTTPS but the phone uses http://, Secure cookies and the
    __Secure-/__Host- prefixes would be rejected too.
    """
    name_value, *attributes = value.split(";")
    kept: list[str] = []
    for attribute in attributes:
        key, _, attr_value = attribute.strip().partition("=")
        key = key.lower()
        if key == "domain" and attr_value.strip().lstrip(".").lower() in LOCAL_COOKIE_DOMAINS:
            continue
        if bridging_tls and key in ("secure", "partitioned"):
            continue
        if bridging_tls and key == "samesite" and attr_value.strip().lower() == "none":
            kept.append(" SameSite=Lax")
            continue
        kept.append(attribute)
    if bridging_tls:
        stripped = name_value.lstrip()
        if stripped.startswith(SECURE_COOKIE_PREFIX):
            name_value = RENAMED_SECURE_PREFIX + stripped[len(SECURE_COOKIE_PREFIX) :]
        elif stripped.startswith(HOST_COOKIE_PREFIX):
            name_value = RENAMED_HOST_PREFIX + stripped[len(HOST_COOKIE_PREFIX) :]
    return ";".join([name_value, *kept])


# ---- bodies ----


def is_rewritable_type(content_type: str) -> bool:
    """True for text-like bodies that may embed localhost URLs."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    if not media_type:
        return False
    return (
        media_type.startswith("text/")
        or media_type in REWRITABLE_MEDIA_TYPES
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
    )


class BodyCodecError(Exception):
    """A compressed body from the local app could not be decoded."""


class GzipDecoder:
    """Streaming gzip decoder."""

    def __init__(self) -> None:
        self._decoder = zlib.decompressobj(GZIP_WBITS)

    def feed(self, chunk: bytes) -> bytes:
        """Decode a chunk."""
        try:
            return self._decoder.decompress(chunk)
        except zlib.error as exc:
            raise BodyCodecError(f"invalid gzip body: {exc}") from exc

    def flush(self) -> bytes:
        """Decode whatever remains buffered."""
        return self._decoder.flush()


class DeflateDecoder:
    """Streaming 'deflate' decoder accepting zlib-wrapped and raw streams.

    RFC 9110 says 'deflate' means zlib-wrapped, but some servers send raw
    deflate; the wrapping is detected from the first bytes.
    """

    def __init__(self) -> None:
        self._decoder = zlib.decompressobj()
        self._bytes_seen = 0
        self._detected = False

    def feed(self, chunk: bytes) -> bytes:
        """Decode a chunk."""
        try:
            output = self._decoder.decompress(chunk)
        except zlib.error as exc:
            if self._detected:
                raise BodyCodecError(f"invalid deflate body: {exc}") from exc
            self._detected = True
            self._decoder = zlib.decompressobj(-zlib.MAX_WBITS)
            try:
                return self._decoder.decompress(chunk)
            except zlib.error as raw_exc:
                raise BodyCodecError(f"invalid deflate body: {raw_exc}") from raw_exc
        self._bytes_seen += len(chunk)
        if self._bytes_seen >= ZLIB_HEADER_BYTES:
            self._detected = True
        return output

    def flush(self) -> bytes:
        """Decode whatever remains buffered."""
        return self._decoder.flush()


class GzipEncoder:
    """Streaming gzip encoder that flushes per chunk so streams stay live."""

    def __init__(self, level: int = RECOMPRESS_LEVEL) -> None:
        self._encoder = zlib.compressobj(level, zlib.DEFLATED, GZIP_WBITS)

    def feed(self, chunk: bytes) -> bytes:
        """Compress a chunk and sync-flush it (keeps SSE/HMR responsive)."""
        if not chunk:
            return b""
        return self._encoder.compress(chunk) + self._encoder.flush(zlib.Z_SYNC_FLUSH)

    def flush(self) -> bytes:
        """Finish the gzip stream."""
        return self._encoder.flush(zlib.Z_FINISH)


class BodyPipeline:
    """Chains decoder → URL rewriter → encoder stages over a streamed body."""

    def __init__(
        self, stages: list[GzipDecoder | DeflateDecoder | StreamingUrlRewriter | GzipEncoder]
    ) -> None:
        self._stages = stages

    def feed(self, chunk: bytes) -> bytes:
        """Push a chunk through every stage."""
        for stage in self._stages:
            if not chunk:
                return b""
            chunk = stage.feed(chunk)
        return chunk

    def flush(self) -> bytes:
        """Flush each stage, feeding its tail through the stages after it."""
        data = b""
        for stage in self._stages:
            data = (stage.feed(data) if data else b"") + stage.flush()
        return data


def build_body_pipeline(content_encoding: str, context: RequestContext) -> BodyPipeline | None:
    """Pipeline for a rewritable body, or None if its encoding can't be handled."""
    coding = content_encoding.strip().lower()
    stages: list[GzipDecoder | DeflateDecoder | StreamingUrlRewriter | GzipEncoder] = []
    if coding in ("gzip", "x-gzip"):
        stages.append(GzipDecoder())
    elif coding == "deflate":
        stages.append(DeflateDecoder())
    elif coding not in ("", "identity"):
        return None
    stages.append(StreamingUrlRewriter(context))
    if context.client_accepts_gzip:
        stages.append(GzipEncoder())
    return BodyPipeline(stages)


# ---- friendly error page ----


def unavailable_page(service_name: str, local_port: int) -> bytes:
    """Self-refreshing page shown while the local app is not answering."""
    name = escape(service_name or f"Port {local_port}")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{ERROR_PAGE_REFRESH_SECONDS}">
<title>{name} is starting…</title>
<style>
body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;
min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#f6f5f4;color:#241f31}}
main{{max-width:28rem;padding:2rem;text-align:center}}
h1{{font-size:1.4rem;margin:0 0 .5rem}}
p{{line-height:1.5;color:#5e5c64}}
.dot{{display:inline-block;width:.6rem;height:.6rem;border-radius:50%;
background:#e5a50a;margin-right:.4rem}}
@media (prefers-color-scheme:dark){{body{{background:#1e1e1e;color:#fff}}p{{color:#c0bfbc}}}}
</style></head>
<body><main>
<h1><span class="dot"></span>{name} isn't answering yet</h1>
<p>The app on port {local_port} of the shared computer is not running or
is still starting. This page retries every {ERROR_PAGE_REFRESH_SECONDS} seconds.</p>
<p><small>Shared with Xposer</small></p>
</main></body></html>
""".encode()
