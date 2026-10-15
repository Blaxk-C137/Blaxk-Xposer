"""Unit tests for the pure rewriting rules."""

import gzip
import random
import zlib

import pytest

from services.http_rewriter import (
    BodyPipeline,
    DeflateDecoder,
    GzipDecoder,
    GzipEncoder,
    PublicEndpoint,
    RequestContext,
    RewriteMap,
    StreamingUrlRewriter,
    accepts_gzip,
    build_body_pipeline,
    is_rewritable_type,
    normalize_authority,
    restore_cookie_names,
    restore_if_none_match,
    rewrite_csp,
    rewrite_request_headers,
    rewrite_response_headers,
    rewrite_set_cookie,
    rewrite_text_urls,
    unavailable_page,
    version_etag,
)

FRONT = PublicEndpoint(
    local_port=5173, public_port=5173, ip_host="192.168.1.5", name_host="shop.local"
)
BACK = PublicEndpoint(local_port=8000, public_port=18000, ip_host="192.168.1.5")
SECURE = PublicEndpoint(
    local_port=5443,
    public_port=5443,
    ip_host="192.168.1.5",
    public_scheme="https",
    upstream_tls=True,
)
SHARE_MAP = RewriteMap([FRONT, BACK, SECURE], version=7)


def context(
    authority: str = "192.168.1.5:5173", endpoint: PublicEndpoint = FRONT, **extra: object
) -> RequestContext:
    return RequestContext(
        endpoint=endpoint,
        client_authority=authority,
        client_ip="192.168.1.20",
        rewrite_map=SHARE_MAP,
        **extra,
    )


# ---- URL translation ----


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("http://localhost:8000/api", "http://192.168.1.5:18000/api"),
        ("http://127.0.0.1:8000", "http://192.168.1.5:18000"),
        ("ws://localhost:5173/hmr", "ws://192.168.1.5:5173/hmr"),
        ("https://localhost:5443/x", "https://192.168.1.5:5443/x"),
        ("wss://localhost:5443/x", "wss://192.168.1.5:5443/x"),
        ("ws://localhost:5443/x", "wss://192.168.1.5:5443/x"),
        ("http:\\/\\/localhost:8000\\/y", "http:\\/\\/192.168.1.5:18000\\/y"),
        ("http://[::1]:8000/", "http://192.168.1.5:18000/"),
        ("http://0.0.0.0:8000/", "http://192.168.1.5:18000/"),
        ("http://localhost:80001/", "http://localhost:80001/"),
        ("http://localhost.example.com:8000/", "http://localhost.example.com:8000/"),
        ("http://localhost/", "http://localhost/"),
    ],
)
def test_text_url_translation(text, expected):
    assert rewrite_text_urls(text, context()) == expected


def test_own_port_keeps_the_link_the_device_used():
    ctx = context("shop.local:5173")
    assert rewrite_text_urls("https://localhost:5173/a", ctx) == "http://shop.local:5173/a"


def test_custom_link_devices_get_custom_links_for_other_apps():
    named_back = PublicEndpoint(8000, 18000, "192.168.1.5", "api.local")
    ctx = RequestContext(FRONT, "shop.local:5173", "1.1.1.1", RewriteMap([FRONT, named_back], 1))
    assert rewrite_text_urls("http://localhost:8000/v1", ctx) == "http://api.local:18000/v1"
    ip_ctx = RequestContext(
        FRONT, "192.168.1.5:5173", "1.1.1.1", RewriteMap([FRONT, named_back], 1)
    )
    assert rewrite_text_urls("http://localhost:8000/v1", ip_ctx) == "http://192.168.1.5:18000/v1"


def test_unshared_ports_are_reported_not_changed():
    ctx = context()
    assert rewrite_text_urls("http://localhost:4000/q", ctx) == "http://localhost:4000/q"
    assert ctx.unresolved_ports == {4000}


def test_streaming_rewriter_matches_one_shot_for_any_chunking():
    body = (
        b'fetch("http://localhost:8000/api");\n'
        b"const ws = new WebSocket('ws://localhost:5173/');\n"
        b'{"u":"http:\\/\\/127.0.0.1:8000\\/x"} http://localhost:4000 end http://localhost:8000'
    ) * 20
    whole = StreamingUrlRewriter(context())
    expected = whole.feed(body) + whole.flush()
    assert b"localhost:8000" not in expected
    generator = random.Random(1234)
    for _ in range(200):
        rewriter = StreamingUrlRewriter(context())
        output = b""
        position = 0
        while position < len(body):
            size = generator.randint(1, 40)
            output += rewriter.feed(body[position : position + size])
            position += size
        output += rewriter.flush()
        assert output == expected


def test_streaming_rewriter_does_not_hold_back_plain_events():
    rewriter = StreamingUrlRewriter(context())
    assert rewriter.feed(b"data: hello world\n\n") == b"data: hello world\n\n"
    assert rewriter.feed(b"data: see http://loc") == b"data: see "
    assert rewriter.feed(b"alhost:8000/x\n\n") == b"http://192.168.1.5:18000/x\n\n"


# ---- request headers ----


def test_request_headers_for_custom_link():
    ctx = context("shop.local:5173")
    headers = rewrite_request_headers(
        [
            ("Host", "shop.local:5173"),
            ("Origin", "http://shop.local:5173"),
            ("Referer", "http://shop.local:5173/cart?id=1"),
            ("Accept-Encoding", "gzip, deflate, br"),
            ("X-Forwarded-For", "10.0.0.9"),
        ],
        ctx,
    )
    assert headers[0] == ("Host", "localhost:5173")
    assert ("Origin", "http://localhost:5173") in headers
    assert ("Referer", "http://localhost:5173/cart?id=1") in headers
    assert ("Accept-Encoding", "identity") in headers
    assert ("X-Forwarded-For", "10.0.0.9, 192.168.1.20") in headers
    assert ctx.client_accepts_gzip is True
    assert ctx.origin_swap is not None and ctx.origin_swap.public == "http://shop.local:5173"


def test_origin_of_another_shared_app_maps_to_that_app():
    ctx = context("192.168.1.5:18000", endpoint=BACK)
    headers = rewrite_request_headers([("Origin", "http://192.168.1.5:5173")], ctx)
    assert ("Origin", "http://localhost:5173") in headers


def test_origin_of_https_app_maps_to_https_local_origin():
    ctx = context("192.168.1.5:5443", endpoint=SECURE, bridging_tls=True)
    headers = rewrite_request_headers([("Origin", "http://192.168.1.5:5443")], ctx)
    assert ("Origin", "https://localhost:5443") in headers


def test_same_origin_via_unknown_alias_maps_to_own_app():
    ctx = context("my-laptop.local:5173")
    headers = rewrite_request_headers([("Origin", "http://my-laptop.local:5173")], ctx)
    assert ("Origin", "http://localhost:5173") in headers


def test_null_and_foreign_origins_untouched():
    ctx = context()
    headers = rewrite_request_headers([("Origin", "null"), ("Referer", "https://google.com/")], ctx)
    assert ("Origin", "null") in headers
    assert ("Referer", "https://google.com/") in headers
    assert ctx.origin_swap is None


def test_default_port_origins_are_normalized():
    assert normalize_authority("Example.local", "http") == "example.local:80"
    assert normalize_authority("[::1]", "https") == "[::1]:443"
    assert normalize_authority("[::1]:8443", "https") == "[::1]:8443"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("gzip, br", True),
        ("br;q=1.0, gzip;q=0.5", True),
        ("gzip;q=0", False),
        ("br", False),
        ("", False),
    ],
)
def test_accepts_gzip(header, expected):
    assert accepts_gzip(header) is expected


# ---- validators ----


def test_etag_versioning_roundtrip():
    assert version_etag('"abc"', 3) == 'W/"abc.xp3s"'
    assert version_etag('W/"abc"', 3) == 'W/"abc.xp3w"'
    assert restore_if_none_match('W/"abc.xp3s"', 3) == ('"abc"', True)
    assert restore_if_none_match('W/"abc.xp3w", "other"', 3) == ('W/"abc", "other"', True)
    assert restore_if_none_match('W/"abc.xp2s"', 3) == (None, False)
    assert restore_if_none_match("*", 3) == ("*", False)


# ---- response headers ----


def test_response_headers_location_cors_and_dropped_headers():
    ctx = context("shop.local:5173")
    rewrite_request_headers([("Origin", "http://shop.local:5173")], ctx)
    headers = rewrite_response_headers(
        [
            ("Location", "http://localhost:5173/login"),
            ("Access-Control-Allow-Origin", "http://localhost:5173"),
            ("Strict-Transport-Security", "max-age=1"),
            ("Alt-Svc", 'h3=":443"'),
            ("Link", "<http://localhost:8000/font.woff2>; rel=preload"),
        ],
        ctx,
        body_rewritten=False,
    )
    assert ("Location", "http://shop.local:5173/login") in headers
    assert ("Access-Control-Allow-Origin", "http://shop.local:5173") in headers
    assert ("Link", "<http://192.168.1.5:18000/font.woff2>; rel=preload") in headers
    assert all(name not in ("Strict-Transport-Security", "Alt-Svc") for name, _ in headers)


def test_rewritten_bodies_get_revalidating_cache_headers():
    headers = rewrite_response_headers(
        [
            ("ETag", '"x"'),
            ("Last-Modified", "Mon"),
            ("Cache-Control", "max-age=31536000, immutable"),
        ],
        context(),
        body_rewritten=True,
    )
    assert ("ETag", 'W/"x.xp7s"') in headers
    assert ("Cache-Control", "no-cache") in headers
    assert all(name != "Last-Modified" for name, _ in headers)


def test_csp_rewrite_and_mixed_content_directives():
    value = (
        "default-src 'self'; connect-src ws://localhost:5173 http://localhost:8000; "
        "upgrade-insecure-requests"
    )
    bridged = rewrite_csp(value, context(bridging_tls=True))
    assert "connect-src ws://192.168.1.5:5173 http://192.168.1.5:18000" in bridged
    assert "upgrade-insecure-requests" not in bridged
    assert "upgrade-insecure-requests" in rewrite_csp(value, context())


@pytest.mark.parametrize(
    ("cookie", "bridging", "expected"),
    [
        ("sid=1; Domain=localhost; Path=/", False, "sid=1; Path=/"),
        ("sid=1; Domain=.localhost; HttpOnly", False, "sid=1; HttpOnly"),
        ("sid=1; Domain=example.com", False, "sid=1; Domain=example.com"),
        ("sid=1; Secure; SameSite=None", False, "sid=1; Secure; SameSite=None"),
        ("sid=1; Secure; SameSite=None; Partitioned", True, "sid=1; SameSite=Lax"),
        ("__Secure-a=1; Secure", True, "XpSecure__a=1"),
        ("__Host-b=2; Path=/; Secure", True, "XpHost__b=2; Path=/"),
    ],
)
def test_set_cookie_rewrites(cookie, bridging, expected):
    assert rewrite_set_cookie(cookie, bridging) == expected


def test_cookie_names_restored_for_the_app():
    assert (
        restore_cookie_names("XpSecure__a=1; sid=2; XpHost__b=3")
        == "__Secure-a=1; sid=2; __Host-b=3"
    )
    assert restore_cookie_names("plain=1") == "plain=1"


# ---- bodies ----


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("text/html; charset=utf-8", True),
        ("application/javascript", True),
        ("text/event-stream", True),
        ("application/vnd.api+json", True),
        ("image/svg+xml", True),
        ("image/png", False),
        ("application/octet-stream", False),
        ("", False),
    ],
)
def test_rewritable_types(content_type, expected):
    assert is_rewritable_type(content_type) is expected


def test_pipeline_gzip_in_plain_out_and_gzip_out():
    payload = b"API http://localhost:8000/x " * 100
    compressed = gzip.compress(payload)
    plain = build_body_pipeline("gzip", context())
    assert plain is not None
    out = (
        b"".join(plain.feed(compressed[i : i + 17]) for i in range(0, len(compressed), 17))
        + plain.flush()
    )
    assert out == payload.replace(b"localhost:8000", b"192.168.1.5:18000")
    ctx = context()
    ctx.client_accepts_gzip = True
    recompressing = build_body_pipeline("", ctx)
    assert recompressing is not None
    packed = recompressing.feed(payload) + recompressing.flush()
    assert gzip.decompress(packed) == payload.replace(b"localhost:8000", b"192.168.1.5:18000")


def test_pipeline_refuses_unknown_encodings():
    assert build_body_pipeline("br", context()) is None


def test_deflate_decoder_accepts_zlib_and_raw():
    payload = b"hello deflate " * 50
    wrapped = zlib.compress(payload)
    raw_encoder = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = raw_encoder.compress(payload) + raw_encoder.flush()
    for data in (wrapped, raw):
        decoder = DeflateDecoder()
        assert decoder.feed(data) + decoder.flush() == payload


def test_gzip_encoder_sync_flushes_each_chunk():
    encoder = GzipEncoder()
    first = encoder.feed(b"data: one\n\n")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    assert decoder.decompress(first) == b"data: one\n\n"


def test_pipeline_flush_cascades_through_stages():
    pipeline = BodyPipeline([GzipDecoder(), StreamingUrlRewriter(context())])
    compressed = gzip.compress(b"end http://localhost:8000")
    assert pipeline.feed(compressed) + pipeline.flush() == b"end http://192.168.1.5:18000"


def test_unavailable_page_escapes_name():
    page = unavailable_page("<script>", 3000).decode()
    assert "&lt;script&gt;" in page
    assert "<script>" not in page
    assert "3000" in page
