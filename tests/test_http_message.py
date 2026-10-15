"""Unit tests for HTTP/1.x head parsing and framing rules."""

import pytest

from services.http_message import (
    BodyFraming,
    HttpParseError,
    RequestHead,
    ResponseHead,
    classify_request_prefix,
    connection_tokens,
    parse_request_head,
    parse_response_head,
    serialize_request_head,
    serialize_response_head,
    strip_hop_by_hop,
)


def test_parse_request_preserves_order_and_case():
    head = parse_request_head(
        b"GET /a?b=1 HTTP/1.1\r\nHost: x:1\r\nX-Thing: A\r\nx-thing: B\r\n\r\n"
    )
    assert (head.method, head.target, head.version) == ("GET", "/a?b=1", "HTTP/1.1")
    assert head.headers == [("Host", "x:1"), ("X-Thing", "A"), ("x-thing", "B")]
    assert head.header("x-THING") == "A"


def test_folded_header_lines_are_joined():
    head = parse_request_head(b"GET / HTTP/1.1\r\nX-Long: one\r\n two\r\n\r\n")
    assert head.header("x-long") == "one two"


@pytest.mark.parametrize(
    "raw",
    [
        b"GARBAGE\r\n\r\n",
        b"GET /\r\n\r\n",
        b"G=T / HTTP/1.1\r\n\r\n",
        b"GET / HTTP/1.1\r\nNoColon\r\n\r\n",
        b"GET / FTP/1\r\n\r\n",
    ],
)
def test_malformed_requests_raise(raw):
    with pytest.raises(HttpParseError):
        parse_request_head(raw)


def test_request_framing():
    assert parse_request_head(b"GET / HTTP/1.1\r\n\r\n").body_framing() == (BodyFraming.NONE, 0)
    length = parse_request_head(b"POST / HTTP/1.1\r\nContent-Length: 12\r\n\r\n")
    assert length.body_framing() == (BodyFraming.LENGTH, 12)
    chunked = parse_request_head(
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\nContent-Length: 5\r\n\r\n"
    )
    assert chunked.body_framing() == (BodyFraming.CHUNKED, 0)
    with pytest.raises(HttpParseError):
        parse_request_head(
            b"POST / HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n"
        ).body_framing()
    with pytest.raises(HttpParseError):
        parse_request_head(b"POST / HTTP/1.1\r\nTransfer-Encoding: gzip\r\n\r\n").body_framing()


def test_response_framing_rules():
    def framing(raw: bytes, method: str = "GET") -> tuple[BodyFraming, int]:
        return parse_response_head(raw).body_framing(method)

    assert framing(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n") == (BodyFraming.LENGTH, 3)
    assert framing(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n", "HEAD") == (BodyFraming.NONE, 0)
    assert framing(b"HTTP/1.1 304 Not Modified\r\n\r\n") == (BodyFraming.NONE, 0)
    assert framing(b"HTTP/1.1 204 No Content\r\n\r\n") == (BodyFraming.NONE, 0)
    assert framing(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n") == (
        BodyFraming.CHUNKED,
        0,
    )
    assert framing(b"HTTP/1.1 200 OK\r\n\r\n") == (BodyFraming.UNTIL_CLOSE, 0)
    assert framing(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n") == (BodyFraming.NONE, 0)


def test_keep_alive_rules():
    assert parse_request_head(b"GET / HTTP/1.1\r\n\r\n").keep_alive is True
    assert parse_request_head(b"GET / HTTP/1.1\r\nConnection: close\r\n\r\n").keep_alive is False
    assert parse_request_head(b"GET / HTTP/1.0\r\n\r\n").keep_alive is False
    assert (
        parse_request_head(b"GET / HTTP/1.0\r\nConnection: Keep-Alive\r\n\r\n").keep_alive is True
    )


def test_upgrade_and_expect_detection():
    head = parse_request_head(
        b"GET /ws HTTP/1.1\r\nConnection: keep-alive, Upgrade\r\nUpgrade: websocket\r\n"
        b"Expect: 100-continue\r\n\r\n"
    )
    assert head.is_upgrade is True
    assert head.expects_continue is True


def test_strip_hop_by_hop_includes_connection_named_headers():
    headers = [
        ("Connection", "close, X-Secret"),
        ("X-Secret", "1"),
        ("Keep-Alive", "5"),
        ("Accept", "*/*"),
    ]
    assert strip_hop_by_hop(headers) == [("Accept", "*/*")]
    assert connection_tokens(headers) == {"close", "x-secret"}


def test_serialize_roundtrip():
    request = RequestHead("POST", "/x", "HTTP/1.1", [("Host", "a"), ("Content-Length", "0")])
    assert parse_request_head(serialize_request_head(request)) == request
    response = ResponseHead("HTTP/1.1", 404, "Not Found", [("Content-Length", "0")])
    assert parse_response_head(serialize_response_head(response)) == response


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"GET / HTTP/1.1\r\nHost: x\r\n", True),
        (b"OPTIONS * HTTP/1.1\r\n", True),
        (b"PRI * HTTP/2.0\r\n\r\nSM\r\n", True),
        (b"M-SEARCH * HTTP/1.1\r\n", True),
        (b"GET /very/long/path", None),
        (b"GE", None),
        (b"get key\r\n", False),
        (b"GET key\r\n", False),
        (b"\x16\x03\x01", False),
        (b"PING\r\n", False),
        (b"A" * 40, False),
        (b" GET", False),
    ],
)
def test_classify_request_prefix(data, expected):
    assert classify_request_prefix(data) is expected
