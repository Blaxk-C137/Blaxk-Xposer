"""One LAN connection handled by the smart proxy (runs on the asyncio loop).

Per connection:
1. If the device opens with a TLS handshake (it used https://) and the
   local app speaks HTTPS, bytes are passed straight through.
2. Otherwise HTTP/1.x requests are parsed one after another (keep-alive),
   rewritten for the local app, forwarded, and the responses streamed back
   with localhost links translated to LAN addresses.
3. A 101 Switching Protocols reply (WebSocket, HMR) turns the connection
   into a raw two-way pipe.
4. A client that doesn't speak HTTP at all is relayed as raw TCP.
"""

import asyncio
import ssl
import threading
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass

from services.http_message import (
    HTTP_11,
    SUPPORTED_VERSIONS,
    SWITCHING_PROTOCOLS,
    BodyFraming,
    HttpParseError,
    RequestHead,
    ResponseHead,
    classify_request_prefix,
    header_value,
    parse_request_head,
    parse_response_head,
    serialize_request_head,
    serialize_response_head,
    strip_hop_by_hop,
    without_headers,
)
from services.http_rewriter import (
    BodyCodecError,
    BodyPipeline,
    PublicEndpoint,
    RequestContext,
    RewriteMap,
    build_body_pipeline,
    is_rewritable_type,
    normalize_authority,
    rewrite_request_headers,
    rewrite_response_headers,
    unavailable_page,
)
from utils.logger import get_logger

logger = get_logger("proxy")

CHUNK_SIZE = 64 * 1024
MAX_HEAD_BYTES = 256 * 1024
MAX_CHUNK_LINE_BYTES = 8 * 1024
CLIENT_IDLE_TIMEOUT_SECONDS = 120.0
PROTOCOL_SNIFF_TIMEOUT_SECONDS = 0.5
UPSTREAM_CONNECT_TIMEOUT_SECONDS = 5.0
# Reusing an idle upstream connection races the app's own keep-alive
# timeout (Gunicorn: 2 s, Node: 5 s); a fresh loopback connect is ~0.1 ms.
UPSTREAM_REUSE_MAX_IDLE_SECONDS = 1.0
REQUEST_BODY_GRACE_SECONDS = 1.0
DEVICE_ACTIVITY_WINDOW_SECONDS = 300.0
TLS_HANDSHAKE_RECORD = 0x16
CRLF = b"\r\n"
LAST_CHUNK = b"0\r\n\r\n"
PARTIAL_CONTENT = 206
BAD_GATEWAY = 502
UPSTREAM_ALPN = ["http/1.1"]
SESSION_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,
    OSError,
    ssl.SSLError,
    asyncio.IncompleteReadError,
    asyncio.TimeoutError,
    HttpParseError,
    BodyCodecError,
)


class StreamClosed(Exception):
    """The peer closed the connection before a complete message arrived."""


class HeadTooLarge(Exception):
    """A request head exceeded MAX_HEAD_BYTES."""


class UpstreamUnavailable(Exception):
    """No address of the local app accepted a connection."""


class ClientGone(Exception):
    """The client disconnected while its request body was being forwarded."""


@dataclass(frozen=True)
class ProxyStatsSnapshot:
    """Point-in-time copy of one proxy's counters."""

    requests: int
    active_connections: int
    connected_devices: int
    unresolved_ports: frozenset[int]


class ProxyStats:
    """Counters written on the loop thread and read from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests = 0
        self._active = 0
        self._last_seen: dict[str, float] = {}
        self._unresolved: set[int] = set()

    def connection_opened(self, client_ip: str) -> None:
        """Count a new connection and refresh its device's activity."""
        with self._lock:
            self._active += 1
            self._last_seen[client_ip] = time.monotonic()

    def connection_closed(self, client_ip: str) -> None:
        """Count a finished connection."""
        with self._lock:
            self._active = max(0, self._active - 1)
            self._last_seen[client_ip] = time.monotonic()

    def request_served(self, client_ip: str) -> None:
        """Count one forwarded request."""
        with self._lock:
            self._requests += 1
            self._last_seen[client_ip] = time.monotonic()

    def note_unresolved(self, ports: set[int]) -> None:
        """Remember localhost ports the app referenced that are not shared."""
        if not ports:
            return
        with self._lock:
            self._unresolved.update(ports)

    def snapshot(self) -> ProxyStatsSnapshot:
        """Thread-safe copy; devices are those active in the last few minutes."""
        cutoff = time.monotonic() - DEVICE_ACTIVITY_WINDOW_SECONDS
        with self._lock:
            devices = sum(1 for seen in self._last_seen.values() if seen >= cutoff)
            return ProxyStatsSnapshot(
                requests=self._requests,
                active_connections=self._active,
                connected_devices=max(devices, 1 if self._active else 0),
                unresolved_ports=frozenset(self._unresolved),
            )


class ProxyTarget:
    """Per-proxy facts shared by all of its sessions (loop-thread owned)."""

    def __init__(
        self,
        service_name: str,
        local_port: int,
        upstream_hosts: Sequence[str],
        upstream_tls: bool,
        raw_mode: bool,
        listen_host: str,
        listen_port: int,
        smart_enabled: Callable[[], bool],
        rewrite_map: Callable[[], RewriteMap],
    ) -> None:
        self.service_name = service_name
        self.local_port = local_port
        self.upstream_tls = upstream_tls
        self.raw_mode = raw_mode
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.smart_enabled = smart_enabled
        self.rewrite_map = rewrite_map
        self.stats = ProxyStats()
        self.tasks: set[asyncio.Task[None]] = set()
        self._hosts = list(dict.fromkeys(upstream_hosts))
        self._tls_context: ssl.SSLContext | None = None

    def ordered_hosts(self) -> list[str]:
        """Upstream addresses to try, last successful first."""
        return list(self._hosts)

    def remember_host(self, host: str) -> None:
        """Move the address that just worked to the front."""
        if self._hosts and self._hosts[0] == host:
            return
        self._hosts = [host] + [item for item in self._hosts if item != host]

    def tls_context(self) -> ssl.SSLContext:
        """Client TLS context for HTTPS dev servers (self-signed certs accepted)."""
        if self._tls_context is None:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            context.set_alpn_protocols(UPSTREAM_ALPN)
            self._tls_context = context
        return self._tls_context

    def endpoint(self) -> PublicEndpoint:
        """This proxy's public endpoint from the share map, or a local fallback."""
        known = self.rewrite_map().for_local_port(self.local_port)
        if known is not None:
            return known
        return PublicEndpoint(
            local_port=self.local_port,
            public_port=self.listen_port,
            ip_host=self.listen_host,
            upstream_tls=self.upstream_tls,
        )


class BufferedStream:
    """An asyncio reader/writer pair with a push-back buffer."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._buffer = bytearray()
        peer = writer.get_extra_info("peername")
        self.peer_ip = str(peer[0]) if peer else ""

    def buffered(self) -> bytes:
        """Bytes received but not yet consumed."""
        return bytes(self._buffer)

    def buffered_size(self) -> int:
        """Number of bytes received but not yet consumed."""
        return len(self._buffer)

    def push_back(self, data: bytes) -> None:
        """Return consumed bytes to the front of the buffer."""
        self._buffer[:0] = data

    def is_stale(self) -> bool:
        """True when the peer has closed or the transport is shutting down."""
        return self.writer.is_closing() or (not self._buffer and self.reader.at_eof())

    async def fill(self) -> bool:
        """Read at least one more byte into the buffer; False at EOF."""
        chunk = await self.reader.read(CHUNK_SIZE)
        if not chunk:
            return False
        self._buffer += chunk
        return True

    async def read_head(self, limit: int) -> bytes | None:
        """Bytes up to and including the blank line; None on clean EOF."""
        search_from = 0
        while True:
            index = self._buffer.find(b"\r\n\r\n", search_from)
            if index >= 0:
                end = index + 4
                head = bytes(self._buffer[:end])
                del self._buffer[:end]
                return head
            if len(self._buffer) > limit:
                raise HeadTooLarge()
            search_from = max(0, len(self._buffer) - 3)
            if not await self.fill():
                if not self._buffer:
                    return None
                raise StreamClosed("connection closed inside a message head")

    async def read_some(self, max_bytes: int = CHUNK_SIZE) -> bytes:
        """Up to max_bytes; b'' at EOF."""
        if self._buffer:
            data = bytes(self._buffer[:max_bytes])
            del self._buffer[:max_bytes]
            return data
        return await self.reader.read(max_bytes)

    async def read_exactly(self, size: int) -> bytes:
        """Exactly size bytes or StreamClosed."""
        while len(self._buffer) < size:
            if not await self.fill():
                raise StreamClosed("connection closed inside a message body")
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    async def read_line(self, limit: int) -> bytes:
        """One line including its newline, or StreamClosed."""
        search_from = 0
        while True:
            index = self._buffer.find(b"\n", search_from)
            if index >= 0:
                line = bytes(self._buffer[: index + 1])
                del self._buffer[: index + 1]
                return line
            if len(self._buffer) > limit:
                raise HttpParseError("chunk header line too long")
            search_from = len(self._buffer)
            if not await self.fill():
                raise StreamClosed("connection closed inside a chunk header")

    async def send(self, data: bytes) -> None:
        """Write and wait for the transport buffer to drain."""
        self.writer.write(data)
        await self.writer.drain()

    def write_eof(self) -> bool:
        """Half-close our sending side; False if the transport can't (TLS)."""
        try:
            if self.writer.can_write_eof():
                self.writer.write_eof()
                return True
        except (OSError, RuntimeError):
            return True
        return False

    def close(self) -> None:
        """Close the transport without waiting."""
        if not self.writer.is_closing():
            self.writer.close()


async def iter_body(
    stream: BufferedStream, framing: BodyFraming, length: int
) -> AsyncIterator[bytes]:
    """Yield a message body's payload bytes according to its framing."""
    if framing == BodyFraming.LENGTH:
        remaining = length
        while remaining > 0:
            chunk = await stream.read_some(min(CHUNK_SIZE, remaining))
            if not chunk:
                raise StreamClosed("body shorter than Content-Length")
            remaining -= len(chunk)
            yield chunk
    elif framing == BodyFraming.CHUNKED:
        async for chunk in _iter_chunked(stream):
            yield chunk
    elif framing == BodyFraming.UNTIL_CLOSE:
        while True:
            chunk = await stream.read_some(CHUNK_SIZE)
            if not chunk:
                return
            yield chunk


async def _iter_chunked(stream: BufferedStream) -> AsyncIterator[bytes]:
    """Decode chunked transfer coding; trailers are consumed and dropped."""
    while True:
        line = await stream.read_line(MAX_CHUNK_LINE_BYTES)
        size_text = line.split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise HttpParseError(f"invalid chunk size {size_text[:16]!r}") from exc
        if size == 0:
            while (await stream.read_line(MAX_CHUNK_LINE_BYTES)).strip():
                pass
            return
        remaining = size
        while remaining > 0:
            chunk = await stream.read_some(min(CHUNK_SIZE, remaining))
            if not chunk:
                raise StreamClosed("connection closed inside a chunk")
            remaining -= len(chunk)
            yield chunk
        await stream.read_exactly(2)


class BodyWriter:
    """Writes payload bytes with the framing chosen for the receiving side.

    Each chunk goes out as one scatter/gather write (size line, data and
    CRLF together) so small events are a single TCP segment. With
    coalesce=True everything — including a response head passed in — is
    held until finish() and sent at once; that is only used when the whole
    body is already in memory, so nothing waits on the network.
    """

    def __init__(
        self,
        stream: BufferedStream,
        framing: BodyFraming,
        head: bytes = b"",
        coalesce: bool = False,
    ) -> None:
        self._stream = stream
        self._chunked = framing == BodyFraming.CHUNKED
        self._coalesce = coalesce
        self._parts: list[bytes] = [head] if head else []

    async def write(self, data: bytes) -> None:
        """Queue payload bytes and send them unless coalescing."""
        if not data:
            return
        if self._chunked:
            self._parts.extend((f"{len(data):X}\r\n".encode("ascii"), data, CRLF))
        else:
            self._parts.append(data)
        if not self._coalesce:
            await self._flush()

    async def finish(self) -> None:
        """Terminate the body (last-chunk marker for chunked) and send everything."""
        if self._chunked:
            self._parts.append(LAST_CHUNK)
        await self._flush()

    async def _flush(self) -> None:
        """Hand queued parts to the transport in one call and wait for drain."""
        if not self._parts:
            return
        parts, self._parts = self._parts, []
        self._stream.writer.writelines(parts)
        await self._stream.writer.drain()


async def pipe_both(first: BufferedStream, second: BufferedStream) -> None:
    """Relay bytes both ways until both directions finish or one fails."""

    async def pump(source: BufferedStream, sink: BufferedStream) -> None:
        while True:
            data = await source.read_some(CHUNK_SIZE)
            if not data:
                break
            await sink.send(data)
        if not sink.write_eof():
            sink.close()

    tasks = {
        asyncio.ensure_future(pump(first, second)),
        asyncio.ensure_future(pump(second, first)),
    }
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class ProxySession:
    """Serves one accepted LAN connection."""

    def __init__(self, target: ProxyTarget, client: BufferedStream) -> None:
        self._target = target
        self._client = client
        self._upstream: BufferedStream | None = None
        self._upstream_idle_since = 0.0

    async def run(self) -> None:
        """Entry point: choose raw relay or HTTP handling, then clean up."""
        try:
            if self._target.raw_mode:
                await self._relay_raw(upstream_tls=False)
                return
            if not await asyncio.wait_for(self._client.fill(), CLIENT_IDLE_TIMEOUT_SECONDS):
                return
            first_byte = self._client.buffered()[0]
            if first_byte == TLS_HANDSHAKE_RECORD:
                if self._target.upstream_tls:
                    await self._relay_raw(upstream_tls=False)
                return
            if not self._target.smart_enabled() or not await self._speaks_http():
                await self._relay_raw(upstream_tls=self._target.upstream_tls)
                return
            await self._serve_http()
        finally:
            self._drop_upstream()

    async def _speaks_http(self) -> bool:
        """Sniff the first bytes for an HTTP request line."""
        while True:
            verdict = classify_request_prefix(self._client.buffered())
            if verdict is not None:
                return verdict
            try:
                more = await asyncio.wait_for(self._client.fill(), PROTOCOL_SNIFF_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                return False
            if not more:
                return False

    async def _serve_http(self) -> None:
        """Handle requests on this connection until either side stops."""
        first_request = True
        while True:
            try:
                raw_head = await asyncio.wait_for(
                    self._client.read_head(MAX_HEAD_BYTES), CLIENT_IDLE_TIMEOUT_SECONDS
                )
            except HeadTooLarge:
                await self._send_simple(431, "Request Header Fields Too Large")
                return
            except (asyncio.TimeoutError, StreamClosed):
                return
            if raw_head is None:
                return
            try:
                request = parse_request_head(raw_head)
            except HttpParseError:
                await self._send_simple(400, "Bad Request")
                return
            if request.version not in SUPPORTED_VERSIONS:
                if first_request:
                    self._client.push_back(raw_head)
                    await self._relay_raw(upstream_tls=self._target.upstream_tls)
                else:
                    await self._send_simple(505, "HTTP Version Not Supported")
                return
            first_request = False
            if not await self._exchange(request):
                return

    async def _exchange(self, request: RequestHead) -> bool:
        """Forward one request and its response; True to keep the connection."""
        self._target.stats.request_served(self._client.peer_ip)
        try:
            framing, length = request.body_framing()
        except HttpParseError:
            await self._send_simple(400, "Bad Request")
            return False
        context = self._context(request)
        head_bytes = serialize_request_head(
            self._upstream_request(request, context, framing, length)
        )
        forwarded = await self._forward(head_bytes, request, framing, length)
        if forwarded is None:
            return (
                framing == BodyFraming.NONE and request.keep_alive and not self._client.is_stale()
            )
        response, upstream, body_task = forwarded
        try:
            if response.status == SWITCHING_PROTOCOLS:
                await self._switch_protocols(response, upstream, context)
                return False
            keep_client = await self._relay_response(request, response, upstream, context)
            body_complete = await _settle(body_task)
        finally:
            self._target.stats.note_unresolved(context.unresolved_ports)
        response_framing, _ = response.body_framing(request.method)
        reusable = (
            response.keep_alive
            and body_complete
            and not request.is_upgrade
            and response_framing != BodyFraming.UNTIL_CLOSE
        )
        if reusable:
            self._upstream_idle_since = time.monotonic()
        else:
            self._drop_upstream()
        return keep_client and body_complete

    def _context(self, request: RequestHead) -> RequestContext:
        """Rewriting context for one request."""
        target = self._target
        host = (request.header("host") or "").strip()
        if not host:
            host = f"{target.listen_host}:{target.listen_port}"
        return RequestContext(
            endpoint=target.endpoint(),
            client_authority=normalize_authority(host, "http"),
            client_ip=self._client.peer_ip,
            rewrite_map=target.rewrite_map(),
            bridging_tls=target.upstream_tls,
        )

    @staticmethod
    def _upstream_request(
        request: RequestHead, context: RequestContext, framing: BodyFraming, length: int
    ) -> RequestHead:
        """The request as the local app should see it."""
        headers = strip_hop_by_hop(rewrite_request_headers(request.headers, context))
        headers = without_headers(headers, "content-length")
        if framing == BodyFraming.LENGTH:
            headers.append(("Content-Length", str(length)))
        elif framing == BodyFraming.CHUNKED:
            headers.append(("Transfer-Encoding", "chunked"))
        upgrade = request.header("upgrade")
        if request.is_upgrade and upgrade is not None:
            headers.append(("Connection", "Upgrade"))
            headers.append(("Upgrade", upgrade))
        else:
            headers.append(("Connection", "keep-alive"))
        return RequestHead(request.method, request.target, HTTP_11, headers)

    async def _forward(
        self, head_bytes: bytes, request: RequestHead, framing: BodyFraming, length: int
    ) -> tuple[ResponseHead, BufferedStream, "asyncio.Task[None] | None"] | None:
        """Send the request upstream and return the final response head.

        A reused upstream connection may have been closed by the app since
        the last request; bodiless requests are then retried once on a
        fresh connection. Returns None after answering 502 itself, or when
        the client vanished mid-upload.
        """
        has_body = framing != BodyFraming.NONE
        close_after_error = has_body or not request.keep_alive
        for attempt in range(2):
            try:
                upstream, reused = await self._acquire_upstream()
            except UpstreamUnavailable:
                await self._send_unavailable(close=close_after_error)
                return None
            upstream.writer.write(head_bytes)
            body_task = (
                asyncio.ensure_future(self._pump_request_body(upstream, framing, length))
                if has_body
                else None
            )
            try:
                response = await self._await_response_head(upstream, request, body_task)
            except ClientGone:
                self._drop_upstream()
                return None
            except (StreamClosed, HttpParseError, ConnectionError, OSError, ssl.SSLError) as exc:
                if body_task is not None:
                    body_task.cancel()
                self._drop_upstream()
                if reused and not has_body and attempt == 0:
                    logger.debug(f"Stale upstream connection, retrying: {exc!r}")
                    continue
                logger.debug(f"Upstream failed before responding: {exc!r}")
                await self._send_unavailable(close=close_after_error)
                return None
            return response, upstream, body_task
        return None

    async def _await_response_head(
        self,
        upstream: BufferedStream,
        request: RequestHead,
        body_task: "asyncio.Task[None] | None",
    ) -> ResponseHead:
        """Wait for the response head while the request body is still flowing.

        If the client disconnects mid-upload the app would wait forever for
        the rest of the body, so a failed body pump aborts the wait.
        """
        head_task = asyncio.ensure_future(self._read_final_head(upstream, request))
        if body_task is not None:
            await asyncio.wait({head_task, body_task}, return_when=asyncio.FIRST_COMPLETED)
            if not head_task.done() and body_task.done() and body_task.exception() is not None:
                head_task.cancel()
                await asyncio.gather(head_task, return_exceptions=True)
                raise ClientGone()
        return await head_task

    async def _pump_request_body(
        self, upstream: BufferedStream, framing: BodyFraming, length: int
    ) -> None:
        """Copy the client's request body to the app, keeping its framing."""
        writer = BodyWriter(upstream, framing)
        async for chunk in iter_body(self._client, framing, length):
            await writer.write(chunk)
        await writer.finish()

    async def _read_final_head(
        self, upstream: BufferedStream, request: RequestHead
    ) -> ResponseHead:
        """Read response heads, relaying 1xx interim ones, until a final one."""
        while True:
            raw = await upstream.read_head(MAX_HEAD_BYTES)
            if raw is None:
                raise StreamClosed("local app closed the connection without responding")
            response = parse_response_head(raw)
            if not response.is_informational or response.status == SWITCHING_PROTOCOLS:
                return response
            if request.version == HTTP_11:
                interim = ResponseHead(
                    HTTP_11, response.status, response.reason, strip_hop_by_hop(response.headers)
                )
                await self._client.send(serialize_response_head(interim))

    async def _switch_protocols(
        self, response: ResponseHead, upstream: BufferedStream, context: RequestContext
    ) -> None:
        """Relay a 101 reply, then pipe raw bytes (WebSocket frames) both ways."""
        headers = rewrite_response_headers(response.headers, context, body_rewritten=False)
        await self._client.send(
            serialize_response_head(
                ResponseHead(HTTP_11, response.status, response.reason, headers)
            )
        )
        self._upstream = None
        try:
            await pipe_both(self._client, upstream)
        finally:
            upstream.close()

    async def _relay_response(
        self,
        request: RequestHead,
        response: ResponseHead,
        upstream: BufferedStream,
        context: RequestContext,
    ) -> bool:
        """Stream the response to the client; True if the client may continue."""
        framing, length = response.body_framing(request.method)
        pipeline: BodyPipeline | None = None
        if (
            framing != BodyFraming.NONE
            and response.status != PARTIAL_CONTENT
            and is_rewritable_type(response.header("content-type") or "")
        ):
            pipeline = build_body_pipeline(response.header("content-encoding") or "", context)
        revalidated_rewrite = response.status == 304 and context.cached_rewritten
        headers = strip_hop_by_hop(
            rewrite_response_headers(
                response.headers,
                context,
                body_rewritten=pipeline is not None or revalidated_rewrite,
            )
        )
        client_http11 = request.version == HTTP_11
        keep_client = request.keep_alive and client_http11
        if pipeline is not None:
            headers = without_headers(
                headers, "content-length", "content-encoding", "accept-ranges"
            )
            if context.client_accepts_gzip:
                headers.append(("Content-Encoding", "gzip"))
                headers = _add_vary(headers, "Accept-Encoding")
            out_framing = BodyFraming.CHUNKED if client_http11 else BodyFraming.UNTIL_CLOSE
        elif framing in (BodyFraming.CHUNKED, BodyFraming.UNTIL_CLOSE):
            headers = without_headers(headers, "content-length")
            out_framing = BodyFraming.CHUNKED if client_http11 else BodyFraming.UNTIL_CLOSE
        else:
            out_framing = framing
        if out_framing == BodyFraming.UNTIL_CLOSE:
            keep_client = False
        if out_framing == BodyFraming.CHUNKED:
            headers.append(("Transfer-Encoding", "chunked"))
        headers.append(("Connection", "keep-alive" if keep_client else "close"))
        head = serialize_response_head(
            ResponseHead(HTTP_11, response.status, response.reason, headers)
        )
        if out_framing == BodyFraming.NONE:
            await self._client.send(head)
            return keep_client
        if framing == BodyFraming.LENGTH and upstream.buffered_size() >= length:
            writer = BodyWriter(self._client, out_framing, head=head, coalesce=True)
        else:
            await self._client.send(head)
            writer = BodyWriter(self._client, out_framing)
        async for chunk in iter_body(upstream, framing, length):
            await writer.write(pipeline.feed(chunk) if pipeline is not None else chunk)
        if pipeline is not None:
            await writer.write(pipeline.flush())
        await writer.finish()
        return keep_client

    async def _acquire_upstream(self) -> tuple[BufferedStream, bool]:
        """A connection to the local app: (stream, whether it was reused)."""
        upstream = self._upstream
        if upstream is not None:
            idle = time.monotonic() - self._upstream_idle_since
            if idle <= UPSTREAM_REUSE_MAX_IDLE_SECONDS and not upstream.is_stale():
                return upstream, True
            self._drop_upstream()
        self._upstream = await self._connect_upstream(self._target.upstream_tls)
        return self._upstream, False

    async def _connect_upstream(self, use_tls: bool) -> BufferedStream:
        """Connect to the first local address that answers."""
        target = self._target
        last_error: BaseException | None = None
        for host in target.ordered_hosts():
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        host,
                        target.local_port,
                        ssl=target.tls_context() if use_tls else None,
                        server_hostname="localhost" if use_tls else None,
                    ),
                    UPSTREAM_CONNECT_TIMEOUT_SECONDS,
                )
            except (OSError, asyncio.TimeoutError, ssl.SSLError) as exc:
                last_error = exc
                continue
            target.remember_host(host)
            return BufferedStream(reader, writer)
        raise UpstreamUnavailable(f"port {target.local_port}: {last_error!r}")

    async def _relay_raw(self, upstream_tls: bool) -> None:
        """Pipe raw bytes between the client and the local app."""
        try:
            upstream = await self._connect_upstream(upstream_tls)
        except UpstreamUnavailable as exc:
            logger.debug(f"Raw relay could not reach the app: {exc}")
            return
        pending = self._client.buffered()
        try:
            if pending:
                await self._client.read_exactly(len(pending))
                await upstream.send(pending)
            await pipe_both(self._client, upstream)
        finally:
            upstream.close()

    async def _send_unavailable(self, close: bool) -> None:
        """Friendly self-refreshing 502 page while the local app is down."""
        body = unavailable_page(self._target.service_name, self._target.local_port)
        headers = [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("Retry-After", "3"),
            ("Connection", "close" if close else "keep-alive"),
        ]
        head = ResponseHead(HTTP_11, BAD_GATEWAY, "Bad Gateway", headers)
        await self._client.send(serialize_response_head(head) + body)

    async def _send_simple(self, status: int, reason: str) -> None:
        """Plain-text error response followed by closing the connection."""
        body = f"{status} {reason}\n".encode("ascii")
        headers = [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Connection", "close"),
        ]
        await self._client.send(
            serialize_response_head(ResponseHead(HTTP_11, status, reason, headers)) + body
        )

    def _drop_upstream(self) -> None:
        """Close and forget the current upstream connection."""
        if self._upstream is not None:
            self._upstream.close()
            self._upstream = None


async def _settle(body_task: "asyncio.Task[None] | None") -> bool:
    """Wait briefly for request-body forwarding; True when it completed."""
    if body_task is None:
        return True
    try:
        await asyncio.wait_for(asyncio.shield(body_task), REQUEST_BODY_GRACE_SECONDS)
    except asyncio.TimeoutError:
        body_task.cancel()
        return False
    except SESSION_ERRORS + (StreamClosed,):
        return False
    return True


def _add_vary(headers: list[tuple[str, str]], field_name: str) -> list[tuple[str, str]]:
    """Ensure Vary lists field_name."""
    existing = header_value(headers, "vary")
    if existing is None:
        return headers + [("Vary", field_name)]
    if field_name.lower() in (item.strip().lower() for item in existing.split(",")):
        return headers
    merged = without_headers(headers, "vary")
    merged.append(("Vary", f"{existing}, {field_name}"))
    return merged
