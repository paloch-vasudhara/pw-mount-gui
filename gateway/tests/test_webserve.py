"""T6.2a: the gateway serves the built web app and proxies its non-own /api/* to analysis.

The web dir is a tmp tree; analysis is a fake httpx transport. No sockets.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import webserve
from gateway.app import create_app
from gateway.config import Settings

from .conftest import FakePwi4

TOKEN = "t" * 43
OWN = "http://127.0.0.1:8400"


@pytest.fixture()
def web_dir(tmp_path):
    root = tmp_path / "web"
    (root / "_nuxt").mkdir(parents=True)
    (root / "_nuxt" / "builds").mkdir()
    (root / "icons").mkdir()
    (root / "index.html").write_text("<html>INDEX</html>")
    (root / "200.html").write_text("<html>SPA200</html>")
    (root / "_nuxt" / "app.js").write_text("console.log(1)")
    (root / "_nuxt" / "app.css").write_text("a{}")
    (root / "_nuxt" / "builds" / "latest.json").write_text("{}")
    (root / "icons" / "x.svg").write_text("<svg/>")
    (root / "data.json").write_text("{}")
    (tmp_path / "secret.txt").write_text("OUTSIDE")
    return root


class Analysis:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.response = lambda req: httpx.Response(200, json={"ok": True})
        self.down = False

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused http://127.0.0.1:8410/secret")
        self.requests.append(request)
        self.bodies.append(await request.aread())
        r = self.response(request)
        # a real transport hands over an unread stream (the proxy relays it chunk by chunk)
        return httpx.Response(
            r.status_code, headers=r.headers, stream=httpx.ByteStream(r.content), request=request
        )

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


def make(tmp_path, web_dir, analysis, **kw):
    settings = Settings(
        port=8400,
        token=TOKEN,
        settings_path=tmp_path / "s.json",
        poll_enabled=False,
        web_dir=web_dir,
        **kw,
    )
    app = create_app(
        settings, transport=FakePwi4().transport, analysis_transport=analysis.transport
    )
    return TestClient(app, base_url=OWN)


@pytest.fixture()
def analysis():
    return Analysis()


@pytest.fixture()
def client(tmp_path, web_dir, analysis):
    with make(tmp_path, web_dir, analysis) as c:
        yield c


# ---------------------------------------------------------------- static


def test_root_and_spa_fallback(client):
    r = client.get("/")
    assert r.status_code == 200 and "INDEX" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-type"].startswith("text/html")
    r = client.get("/dev/health")  # deep route, no extension: Nuxt's SPA fallback
    assert r.status_code == 200 and "SPA200" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/200.html").status_code == 200


def test_spa_fallback_index_when_no_200(tmp_path, web_dir, analysis):
    (web_dir / "200.html").unlink()
    with make(tmp_path, web_dir, analysis) as c:
        assert "INDEX" in c.get("/some/route").text


def test_assets_types_and_cache(client):
    r = client.get("/_nuxt/app.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/_nuxt/app.css").headers["content-type"].startswith("text/css")
    r = client.get("/icons/x.svg")
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["cache-control"] == "no-cache"
    assert client.get("/data.json").headers["content-type"].startswith("application/json")
    # the un-hashed build manifest must not be cached forever
    assert client.get("/_nuxt/builds/latest.json").headers["cache-control"] == "no-cache"


def test_head_and_post(client):
    r = client.head("/_nuxt/app.js")
    assert r.status_code == 200 and r.content == b""
    assert client.post("/nothing-here").status_code in (404, 405)


def test_missing_asset_404_and_no_listing(client):
    assert client.get("/_nuxt/nope.js").status_code == 404
    assert client.get("/missing.png").status_code == 404
    # a directory is not listed: no index.html inside -> SPA fallback (no listing), never a dir
    r = client.get("/icons")
    assert "x.svg" not in r.text


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/%2E%2E%2Fsecret.txt",
        "/..%2fsecret.txt",
        "/_nuxt/../../secret.txt",
        "/_nuxt/%2e%2e/%2e%2e/secret.txt",
        "/..\\secret.txt",
        "/%5c..%5csecret.txt",
        "/icons/..%5c..%5csecret.txt",
        "/%00",
        "/C:/Windows/win.ini",
    ],
)
def test_traversal_never_leaves_dir(client, path):
    r = client.get(path)
    assert "OUTSIDE" not in r.text and "[fonts]" not in r.text
    assert r.status_code in (200, 404)
    if r.status_code == 200:  # only ever the SPA page
        assert "SPA200" in r.text


def test_own_routes_keep_priority(client):
    r = client.get("/api/mount/health")
    assert r.json()["service"] == "gateway"
    assert (
        client.get("/api/mount/session", headers={"sec-fetch-site": "same-origin"}).json()["token"]
        == TOKEN
    )
    with client.websocket_connect(
        "/api/mount/ws", headers={"origin": OWN, "host": "127.0.0.1:8400"}
    ) as ws:
        ws.send_json({"type": "auth", "token": TOKEN})
        assert ws.receive_json()["type"] == "hello"


def test_unknown_gateway_path_is_404_not_proxied(client, analysis):
    assert client.get("/api/mount/nope").status_code == 404
    assert client.get("/api/mount/health/x").status_code == 404
    assert client.post("/api/mount/health").status_code == 405  # wrong method on a real route
    assert analysis.requests == []


def test_no_web_dir_hint(tmp_path, analysis):
    with make(tmp_path, None, analysis) as c:
        r = c.get("/")
        assert r.status_code == 404 and "pnpm generate" in r.json()["detail"]
        assert c.get("/api/mount/health").status_code == 200
        assert c.get("/other").status_code == 404
        assert c.get("/api/health").status_code == 200  # proxy still works
    with make(tmp_path, tmp_path / "does-not-exist", analysis) as c:
        assert c.get("/").status_code == 404


def test_bad_host_still_400(client):
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 400


# ---------------------------------------------------------------- proxy


def test_get_with_query(client, analysis):
    analysis.response = lambda req: httpx.Response(200, json={"n": 1}, headers={"x-a": "b"})
    r = client.get("/api/sats/search?q=ISS&limit=5")
    assert r.status_code == 200 and r.json() == {"n": 1} and r.headers["x-a"] == "b"
    req = analysis.requests[-1]
    assert req.url == "http://127.0.0.1:8410/api/sats/search?q=ISS&limit=5"
    assert req.method == "GET"


def test_post_json_and_status_passthrough(client, analysis):
    analysis.response = lambda req: httpx.Response(422, json={"detail": "bad"})
    r = client.post("/api/iod", json={"a": [1, 2]})
    assert r.status_code == 422 and r.json() == {"detail": "bad"}
    assert analysis.bodies[-1] == b'{"a":[1,2]}'
    assert analysis.requests[-1].headers["content-type"] == "application/json"
    client.delete("/api/astrometry/job/j1")
    assert analysis.requests[-1].method == "DELETE"


def test_multipart_upload_streamed(client, analysis):
    blob = b"\x00\x01" * (2 * 1024 * 1024)  # 4 MB
    analysis.response = lambda req: httpx.Response(200, json={"image_id": "i"})
    r = client.post("/api/astrometry/image", files={"file": ("a.fits", blob)})
    assert r.status_code == 200
    assert blob in analysis.bodies[-1]
    assert analysis.requests[-1].headers["content-type"].startswith("multipart/form-data")


def test_upload_too_large_413(tmp_path, web_dir, analysis, monkeypatch):
    monkeypatch.setattr(webserve, "MAX_BODY_BYTES", 1000)
    with make(tmp_path, web_dir, analysis) as c:
        r = c.post("/api/astrometry/image", content=b"x" * 5000)  # Content-Length known
        assert r.status_code == 413
        assert analysis.requests == []  # refused before anything went upstream

        def chunks():  # no Content-Length: counted while streaming
            for _ in range(10):
                yield b"y" * 500

        r = c.post("/api/astrometry/image", content=chunks())
        assert r.status_code == 413
        assert c.post("/api/iod", content=b"z" * 900).status_code == 200


def test_response_streamed(client, analysis):
    analysis.response = lambda req: httpx.Response(
        200, content=b"abcdef", headers={"content-type": "application/x-test"}
    )
    r = client.get("/api/sky/big")
    assert r.content == b"abcdef" and r.headers["content-type"] == "application/x-test"


def test_header_filtering(client, analysis):
    analysis.response = lambda req: httpx.Response(
        200,
        json={},
        headers={
            "connection": "close",
            "keep-alive": "timeout=5",
            "server": "uvicorn-up",
            "x-keep": "1",
        },
    )
    r = client.get(
        "/api/health",
        headers={
            "x-mount-token": TOKEN,
            "cookie": "s=1",
            "authorization": "Bearer z",
            "referer": OWN + "/",
            "origin": OWN,
            "accept": "application/json",
            "accept-encoding": "identity",
            "range": "bytes=0-1",
            "x-random": "no",
            "proxy-authorization": "p",
            "te": "trailers",
            "upgrade": "h2c",
        },
    )
    assert r.status_code == 200 and r.headers["x-keep"] == "1"
    assert "keep-alive" not in r.headers and r.headers.get("server") != "uvicorn-up"
    sent = analysis.requests[-1].headers
    for h in (
        "x-mount-token",
        "cookie",
        "authorization",
        "referer",
        "origin",
        "x-random",
        "proxy-authorization",
        "te",
        "upgrade",
    ):
        assert h not in sent
    assert sent["host"] == "127.0.0.1:8410"  # httpx's own, never the browser's
    assert sent["accept"] == "application/json" and sent["range"] == "bytes=0-1"


def test_analysis_down_503_no_leak(tmp_path, web_dir, analysis):
    analysis.down = True
    with make(tmp_path, web_dir, analysis) as c:
        r = c.get("/api/health")
        assert r.status_code == 503 and r.json() == {"detail": "Analysis service unavailable"}
        assert "8410" not in r.text and "refused" not in r.text
        r = c.post("/api/iod", json={})
        assert r.status_code == 503


def test_cross_site_refused(client, analysis):
    assert client.get("/api/health", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/health", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert client.get("/api/health", headers={"sec-fetch-site": "same-site"}).status_code == 403
    assert (
        client.options("/api/health", headers={"origin": "https://evil.example"}).status_code == 403
    )
    assert analysis.requests == []
    assert client.get("/api/health", headers={"origin": OWN}).status_code == 200
    assert client.get("/api/health", headers={"origin": "http://127.0.0.1:3200"}).status_code == 200
    assert client.get("/api/health", headers={"sec-fetch-site": "same-origin"}).status_code == 200


def test_redirects(client, analysis):
    analysis.response = lambda req: httpx.Response(
        307, headers={"location": "http://127.0.0.1:8410/api/sats/?q=1"}
    )
    r = client.get("/api/sats?q=1", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/api/sats/?q=1"
    assert len(analysis.requests) == 1  # never followed
    analysis.response = lambda req: httpx.Response(307, headers={"location": "/api/x/"})
    assert client.get("/api/x", follow_redirects=False).headers["location"] == "/api/x/"
    for loc in (
        "http://evil.example/api/x",
        "/elsewhere",
        "//evil.example/api/x",
        "http://127.0.0.1:8410/etc",
    ):
        analysis.response = lambda req, loc=loc: httpx.Response(302, headers={"location": loc})
        r = client.get("/api/x", follow_redirects=False)
        assert r.status_code == 502 and "evil" not in r.text


def test_proxy_path_guards(client, analysis):
    assert client.get("/api/%2e%2e/etc").status_code == 404
    assert client.get("/api/a%5cb").status_code == 404
    assert client.get("/api/transform").status_code in (
        404,
        405,
    )  # own route, never proxied
    assert analysis.requests == []
