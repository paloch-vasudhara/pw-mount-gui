"""T5.7 mount CCTV: settings fields, tickets, MJPEG / snapshot / HLS through the gateway.

The camera is a fake httpx transport (no sockets, no ffmpeg); one test drives tools/fake_cctv.py
over a real loopback socket. Camera URL, login and tickets must never show up in a response, a
log record or the audit.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import cctv as cctv_mod
from gateway.app import create_app
from gateway.audit import AuditLog
from gateway.cctv import (
    CctvService,
    MjpegHub,
    TicketBook,
    UnitParser,
    boundary_of,
    hls_path_ok,
    rewrite_playlist,
)
from gateway.config import Settings
from gateway.settings import (
    MemorySecretStore,
    SettingsStore,
    StationSettings,
    cctv_url_error,
)

from .conftest import FakePwi4
from .test_settings import AUTH, TOKEN, body

CAM = "http://cam.example:8080"  # a host that would give itself away in any leak
URL_MJPEG = f"{CAM}/video/mjpeg"
URL_HLS = f"{CAM}/live/stream/index.m3u8"
USER, PASSWORD = "camop7", "S3CRET-CAM-PASSWORD"
BOUNDARY = "frameboundary"
MJPEG_CT = f"multipart/x-mixed-replace; boundary={BOUNDARY}"


def jpeg(n: int) -> bytes:
    return b"\xff\xd8" + bytes([n]) * 40 + b"\xff\xd9"


def unit(n: int, boundary: str = BOUNDARY) -> bytes:
    j = jpeg(n)
    head = f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(j)}\r\n\r\n"
    return head.encode() + j + b"\r\n"


class Chunks(httpx.AsyncByteStream):
    """A finite (or endless) response body that notes when the proxy closed it."""

    def __init__(self, chunks: list[bytes], endless: bool = False) -> None:
        self.chunks, self.endless = chunks, endless
        self.closed = False

    async def __aiter__(self):
        for c in self.chunks:
            await asyncio.sleep(0)
            yield c
        while self.endless:
            await asyncio.sleep(0.01)
            yield b""

    async def aclose(self) -> None:
        self.closed = True


class FakeCamera:
    """The camera behind httpx.MockTransport: a route table + a record of every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.auth_sent: list[str] = []  # Authorization as each request arrived (httpx mutates)
        self.routes: dict[str, object] = {}  # path -> httpx.Response | callable(request)
        self.default: object | None = None
        self.raise_exc: Exception | None = None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.auth_sent.append(request.headers.get("authorization", ""))
        if self.raise_exc is not None:
            raise self.raise_exc
        r = self.routes.get(request.url.path, self.default)
        if r is None:
            return httpx.Response(404, text="nope")
        return r(request) if callable(r) else r

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def mjpeg_response(units: list[bytes], endless: bool = False) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": MJPEG_CT}, stream=Chunks(units, endless=endless)
    )


def make(tmp_path, cam, kind="mjpeg", url=None, user=USER, password=PASSWORD, configure=True):
    secrets = MemorySecretStore("ASTRO-KEY-999")
    cctv_secrets = MemorySecretStore()
    settings = Settings(
        port=8400, token=TOKEN, settings_path=tmp_path / "s.json", poll_enabled=False
    )
    app = create_app(
        settings,
        transport=FakePwi4().transport,
        secrets=secrets,
        cctv_transport=cam.transport,
        cctv_secrets=cctv_secrets,
    )
    c = TestClient(app, base_url="http://127.0.0.1:8400")
    if configure:
        r = c.put(
            "/api/mount/settings",
            headers=AUTH,
            json=body(
                cctv={
                    "enabled": True,
                    "type": kind,
                    "url": url or (URL_MJPEG if kind == "mjpeg" else URL_HLS),
                    "username": user,
                },
                cctv_password=password,
            ),
        )
        assert r.status_code == 200, r.text
    return c, app, cctv_secrets


def ticket(c) -> str:
    r = c.post("/api/mount/cctv/ticket", headers=AUTH)
    assert r.status_code == 200
    return r.json()["ticket"]


# ------------------------------------------------------------------ settings


@pytest.mark.parametrize(
    "bad",
    [
        "rtsp://cam.example/stream",
        "ftp://cam.example/x",
        "cam.example/stream",
        "http://",
        "http://user:pw@cam.example/x",
        "http://user@cam.example/x",
        "http://cam.example/x y",
        "http://cam.example:99999/x",
        "http://cam.example/x#frag",
    ],
)
def test_url_rules_refuse_and_never_echo(tmp_path, bad):
    c, _, _ = make(tmp_path, FakeCamera(), configure=False)
    r = c.put("/api/mount/settings", headers=AUTH, json=body(cctv={"enabled": True, "url": bad}))
    assert r.status_code == 422
    assert (bad == "http://" or bad not in r.text) and "cam.example" not in r.text
    assert cctv_url_error(bad)


@pytest.mark.parametrize("good", ["http://10.0.0.5/mjpeg", "https://cam.example:8443/a/b.m3u8?x=1"])
def test_url_rules_accept(tmp_path, good):
    c, _, _ = make(tmp_path, FakeCamera(), configure=False)
    r = c.put("/api/mount/settings", headers=AUTH, json=body(cctv={"enabled": True, "url": good}))
    assert r.status_code == 200
    assert cctv_url_error(good) is None


def test_settings_web_view_is_write_only(tmp_path):
    c, _, cctv_secrets = make(tmp_path, FakeCamera())
    r = c.get("/api/mount/settings", headers=AUTH)
    d = r.json()["cctv"]
    assert d["configured"] is True and d["type"] == "mjpeg" and d["enabled"] is True
    assert d["url_set"] and d["username_set"] and d["password_set"]
    assert d["status"] in ("Connecting", "Live", "Unreachable: Camera unreachable")
    for secret in (URL_MJPEG, "cam.example", USER, PASSWORD):
        assert secret not in r.text
    # the password lives in its own secret store, never in the file; the URL is not secret
    assert cctv_secrets.get() == PASSWORD
    assert PASSWORD not in (tmp_path / "s.json").read_text(encoding="utf-8")


def test_password_write_only_semantics_and_own_key(tmp_path):
    c, app, cctv_secrets = make(tmp_path, FakeCamera())
    store = app.state.store
    # absent keeps (and the url / username survive a PUT that omits them)
    put = body(cctv={"enabled": True, "type": "mjpeg"})
    assert c.put("/api/mount/settings", headers=AUTH, json=put).status_code == 200
    assert cctv_secrets.get() == PASSWORD
    assert store.current.cctv.url == URL_MJPEG and store.current.cctv.username == USER
    # the astrometry key is a different secret
    assert store.secrets.get() == "ASTRO-KEY-999"
    # empty clears
    assert c.put("/api/mount/settings", headers=AUTH, json={**put, "cctv_password": ""}).status_code
    assert cctv_secrets.get() is None
    assert c.get("/api/mount/settings", headers=AUTH).json()["cctv"]["password_set"] is False


def test_configured_needs_enabled_and_url(tmp_path):
    c, _, _ = make(tmp_path, FakeCamera(), configure=False)
    cc = c.get("/api/mount/settings", headers=AUTH).json()["cctv"]
    assert cc["configured"] is False and cc["status"] == "Not configured"
    c.put("/api/mount/settings", headers=AUTH, json=body(cctv={"enabled": True}))
    assert c.get("/api/mount/cctv/status", headers=AUTH).json()["configured"] is False
    c.put(
        "/api/mount/settings",
        headers=AUTH,
        json=body(cctv={"enabled": False, "url": URL_MJPEG}),
    )
    assert c.get("/api/mount/cctv/status", headers=AUTH).json()["configured"] is False


def test_store_roundtrip_and_old_file(tmp_path):
    p = tmp_path / "old.json"
    old = StationSettings().model_dump()
    old.pop("cctv")
    p.write_text(json.dumps(old), encoding="utf-8")
    st = SettingsStore(p, MemorySecretStore(), MemorySecretStore())
    assert st.current.cctv.configured is False and st.load_error is None


# ------------------------------------------------------------------ status + ticket


def test_status_route(tmp_path):
    cam = FakeCamera()
    cam.default = mjpeg_response([unit(1)])
    c, _, _ = make(tmp_path, cam, configure=False)
    assert c.get("/api/mount/cctv/status").status_code == 401
    s = c.get("/api/mount/cctv/status", headers=AUTH).json()
    assert s == {
        "configured": False,
        "type": "mjpeg",
        "state": "not_configured",
        "error": None,
        "viewers": 0,
    }


def test_ticket_requires_auth_and_config(tmp_path):
    c, _, _ = make(tmp_path, FakeCamera(), configure=False)
    assert c.post("/api/mount/cctv/ticket", headers=AUTH).status_code == 409
    c2, _, _ = make(tmp_path / "x", FakeCamera())
    assert c2.post("/api/mount/cctv/ticket").status_code == 401
    r = c2.post("/api/mount/cctv/ticket", headers={**AUTH, "origin": "http://evil.example"})
    assert r.status_code == 403
    r = c2.post("/api/mount/cctv/ticket", headers=AUTH)
    assert r.json()["expires_in"] == 60 and len(r.json()["ticket"]) > 20


def test_ticket_expiry_binding_and_renewal():
    now = [100.0]
    book = TicketBook("session-a", lambda: now[0])
    t = book.issue()
    assert book.check(t)
    now[0] += 59
    assert book.check(t)  # used: renewed for another 60 s
    now[0] += 59
    assert book.check(t)
    now[0] += 61
    assert not book.check(t)  # idle past 60 s
    assert not book.check(t)  # and gone
    t2 = book.issue()
    now[0] += 61
    assert not book.check(t2)  # never used: expired
    t3 = book.issue()
    assert not book.check(t3, "session-b")  # bound to the session
    assert not book.check(None) and not book.check("") and not book.check("nope")


def test_ticket_not_valid_across_gateways(tmp_path):
    cam = FakeCamera()
    cam.default = mjpeg_response([unit(1)])
    c1, _, _ = make(tmp_path / "a", cam)
    t = ticket(c1)
    # another launch has another token, hence another session
    secrets = MemorySecretStore()
    app2 = create_app(
        Settings(port=8400, token="u" * 43, settings_path=tmp_path / "b.json", poll_enabled=False),
        transport=FakePwi4().transport,
        secrets=secrets,
        cctv_transport=cam.transport,
        cctv_secrets=MemorySecretStore(),
    )
    c2 = TestClient(app2, base_url="http://127.0.0.1:8400")
    assert c2.get(f"/api/mount/cctv/snapshot?t={t}").status_code == 401


def test_stream_routes_need_a_ticket(tmp_path):
    cam = FakeCamera()
    cam.default = mjpeg_response([unit(1)])
    c, _, _ = make(tmp_path, cam)
    for path in ("mjpeg", "snapshot", "hls/entry.m3u8"):
        assert c.get(f"/api/mount/cctv/{path}").status_code == 401
        assert c.get(f"/api/mount/cctv/{path}?t=bogus").status_code == 401
        # the token header alone is not a ticket
        assert c.get(f"/api/mount/cctv/{path}", headers=AUTH).status_code == 401
    assert not cam.requests  # nothing reached the camera
    t = ticket(c)
    assert (
        c.get(
            f"/api/mount/cctv/snapshot?t={t}", headers={"sec-fetch-site": "cross-site"}
        ).status_code
        == 403
    )


# ------------------------------------------------------------------ MJPEG


def test_boundary_and_parser():
    assert boundary_of('multipart/x-mixed-replace;boundary="abc"') == b"abc"
    assert boundary_of("multipart/x-mixed-replace; boundary=--myb") == b"--myb"
    assert boundary_of("image/jpeg") is None and boundary_of("multipart/x-mixed-replace") is None
    p = UnitParser(b"b")
    data = b"junk" + unit(1, "b") + unit(2, "b") + unit(3, "b") + b"--b--\r\n"
    out = []
    for i in range(0, len(data), 7):  # split anywhere, even through a delimiter
        out += p.feed(data[i : i + 7])
    assert [u[-6:-2] for u in out] == [jpeg(n)[-4:] for n in (1, 2, 3)]  # the closer is dropped


def test_mjpeg_passthrough_keeps_content_type_and_frames(tmp_path):
    cam = FakeCamera()
    cam.default = mjpeg_response([unit(1), unit(2), unit(3), unit(4)])
    c, _, _ = make(tmp_path, cam)
    r = c.get(f"/api/mount/cctv/mjpeg?t={ticket(c)}")
    assert r.status_code == 200
    assert r.headers["content-type"] == MJPEG_CT
    assert r.headers["cache-control"] == "no-store"
    assert jpeg(1) in r.content and jpeg(2) in r.content and jpeg(3) in r.content
    assert all(q.method == "GET" for q in cam.requests)
    for leak in (CAM, "cam.example", USER, PASSWORD):
        assert leak not in r.text and leak not in str(r.headers)


def test_mjpeg_wrong_type_and_unconfigured(tmp_path):
    cam = FakeCamera()
    c, _, _ = make(tmp_path, cam, kind="hls")
    assert c.get(f"/api/mount/cctv/mjpeg?t={ticket(c)}").status_code == 409
    c2, _, _ = make(tmp_path / "x", cam, configure=False)
    assert c2.get("/api/mount/cctv/mjpeg?t=zzz").status_code == 401


def test_viewer_cap_over_http(tmp_path, monkeypatch):
    cam = FakeCamera()
    cam.default = mjpeg_response([unit(1)])
    c, _, _ = make(tmp_path, cam)
    t = ticket(c)
    monkeypatch.setattr(cctv_mod, "MAX_VIEWERS", 0)
    r = c.get(f"/api/mount/cctv/mjpeg?t={t}")
    assert r.status_code == 429 and "viewers" in r.json()["detail"]


def _service(tmp_path, cam, clock=None) -> CctvService:
    store = SettingsStore(tmp_path / "svc.json", MemorySecretStore(), MemorySecretStore())
    s = store.current.model_copy(deep=True)
    s.cctv.enabled, s.cctv.url = True, URL_MJPEG
    store.save(s)
    kw = {"clock": clock} if clock else {}
    return CctvService(store, AuditLog(tmp_path / "audit"), "tok", cam.transport, **kw)


def test_hub_two_viewers_one_upstream_closed_with_the_last(tmp_path):
    async def run():
        cam = FakeCamera()
        stream = Chunks([unit(i) for i in range(1, 4)], endless=True)
        cam.default = httpx.Response(200, headers={"content-type": MJPEG_CT}, stream=stream)
        svc = _service(tmp_path, cam)
        hub: MjpegHub = svc.hub
        a, b = hub.join(), hub.join()
        assert a is not None and b is not None and hub.join() is None  # the cap is 2
        assert await hub.wait_open() == MJPEG_CT
        got_a = await asyncio.wait_for(a.queue.get(), 2)
        got_b = await asyncio.wait_for(b.queue.get(), 2)
        assert got_a.startswith(b"--" + BOUNDARY.encode()) and got_b == got_a
        assert len(cam.requests) == 1  # one upstream connection for both viewers
        assert svc.status()["viewers"] == 2 and svc.state() == "live"
        hub.leave(a)
        await asyncio.sleep(0.05)
        assert not stream.closed  # still one viewer
        assert hub.join() is not None  # a slot is free again
        hub.leave(b)
        for v in list(hub.viewers):
            hub.leave(v)
        await asyncio.sleep(0.1)
        assert stream.closed  # last viewer gone: the camera connection is closed
        assert hub.task is None and not hub.connected
        await svc.aclose()

    asyncio.run(run())


def test_hub_drops_a_viewer_that_stops_reading(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(cctv_mod, "VIEWER_QUEUE_UNITS", 3)
        cam = FakeCamera()
        cam.default = mjpeg_response([unit(i) for i in range(1, 30)], endless=True)
        svc = _service(tmp_path, cam)
        hub = svc.hub
        slow, fast = hub.join(), hub.join()
        await hub.wait_open()
        seen = 0
        for _ in range(20):
            await asyncio.wait_for(fast.queue.get(), 2)
            seen += 1
        assert slow.dropped and slow not in hub.viewers and not fast.dropped
        assert seen == 20  # the fast viewer was not stalled by the slow one
        last = None
        while not slow.queue.empty():
            last = slow.queue.get_nowait()
        assert last is None  # the dropped viewer's stream ends
        hub.leave(fast)
        await svc.aclose()

    asyncio.run(run())


def test_late_viewer_gets_the_current_frame_at_once(tmp_path):
    async def run():
        cam = FakeCamera()
        cam.default = mjpeg_response([unit(1), unit(2), unit(3)], endless=True)
        svc = _service(tmp_path, cam)
        a = svc.hub.join()
        await svc.hub.wait_open()
        await asyncio.wait_for(a.queue.get(), 2)
        b = svc.hub.join()
        first = b.queue.get_nowait()
        assert first.startswith(b"--" + BOUNDARY.encode())
        await svc.aclose()

    asyncio.run(run())


# ------------------------------------------------------------------ snapshot


def test_snapshot_from_mjpeg_and_from_a_jpeg_url(tmp_path):
    cam = FakeCamera()
    cam.default = mjpeg_response([unit(7), unit(8), unit(9)])
    c, _, _ = make(tmp_path, cam)
    r = c.get(f"/api/mount/cctv/snapshot?t={ticket(c)}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content == jpeg(7)
    cam2 = FakeCamera()
    cam2.default = httpx.Response(200, headers={"content-type": "image/jpeg"}, content=jpeg(5))
    c2, _, _ = make(tmp_path / "y", cam2)
    r = c2.get(f"/api/mount/cctv/snapshot?t={ticket(c2)}")
    assert r.content == jpeg(5)


# ------------------------------------------------------------------ errors


def _snap_error(tmp_path, cam):
    c, _, _ = make(tmp_path, cam)
    r = c.get(f"/api/mount/cctv/snapshot?t={ticket(c)}")
    return c, r


def test_readable_errors_without_leaks(tmp_path):
    cases = [
        (httpx.Response(401, text="no"), "Camera rejected the login"),
        (httpx.Response(302, headers={"location": "http://other.example/"}), "redirected"),
        (httpx.Response(200, headers={"content-type": "text/html"}, text="<b>hi</b>"), "format"),
        (httpx.Response(500, text="boom"), "HTTP 500"),
    ]
    for i, (resp, word) in enumerate(cases):
        cam = FakeCamera()
        cam.default = resp
        _, r = _snap_error(tmp_path / str(i), cam)
        assert r.status_code == 502 and word in r.json()["detail"], r.text
        for leak in (CAM, "cam.example", PASSWORD, "other.example"):
            assert leak not in r.text
    cam = FakeCamera()
    cam.raise_exc = httpx.ConnectError(f"refused {URL_MJPEG} {PASSWORD}")
    c, r = _snap_error(tmp_path / "ce", cam)
    assert r.status_code == 502 and r.json()["detail"] == "Camera unreachable"
    assert PASSWORD not in r.text and "cam.example" not in r.text
    s = c.get("/api/mount/cctv/status", headers=AUTH).json()
    assert s["state"] == "unreachable" and s["error"] == "Camera unreachable"
    line = c.get("/api/mount/settings", headers=AUTH).json()["cctv"]["status"]
    assert line == "Unreachable: Camera unreachable"
    cam.raise_exc = httpx.ReadTimeout("slow")
    r = c.get(f"/api/mount/cctv/snapshot?t={ticket(c)}")
    assert r.json()["detail"] == "Camera timed out"


def test_mjpeg_open_failure_is_a_readable_502(tmp_path):
    cam = FakeCamera()
    cam.default = httpx.Response(401, text="no")
    c, _, _ = make(tmp_path, cam)
    r = c.get(f"/api/mount/cctv/mjpeg?t={ticket(c)}")
    assert r.status_code == 502 and r.json()["detail"] == "Camera rejected the login"


# ------------------------------------------------------------------ upstream auth


def _basic(user, pw) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def test_basic_auth_server_side_only(tmp_path):
    cam = FakeCamera()

    def route(request):
        if request.headers.get("authorization") == _basic(USER, PASSWORD):
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=jpeg(1))
        return httpx.Response(401, headers={"www-authenticate": 'Basic realm="cam"'})

    cam.default = route
    c, _, _ = make(tmp_path, cam)
    r = c.get(f"/api/mount/cctv/snapshot?t={ticket(c)}")
    assert r.status_code == 200 and r.content == jpeg(1)
    assert "authorization" not in r.headers
    # later requests are preemptive: one request, not two
    n = len(cam.requests)
    c.get(f"/api/mount/cctv/snapshot?t={ticket(c)}")
    assert len(cam.requests) == n + 1


def test_wrong_password_is_a_rejected_login(tmp_path):
    cam = FakeCamera()
    cam.default = lambda request: (
        httpx.Response(200, headers={"content-type": "image/jpeg"}, content=jpeg(1))
        if request.headers.get("authorization") == _basic(USER, "right")
        else httpx.Response(401, headers={"www-authenticate": 'Basic realm="cam"'})
    )
    c, _, _ = make(tmp_path, cam)
    r = c.get(f"/api/mount/cctv/snapshot?t={ticket(c)}")
    assert r.status_code == 502 and r.json()["detail"] == "Camera rejected the login"


def test_digest_auth_never_sends_basic(tmp_path):
    cam = FakeCamera()
    challenge = 'Digest realm="cam", nonce="abc123", qop="auth", algorithm=MD5'

    def route(request):
        a = request.headers.get("authorization", "")
        if a.startswith("Digest ") and f'username="{USER}"' in a:
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=jpeg(2))
        return httpx.Response(401, headers={"www-authenticate": challenge})

    cam.default = route
    c, _, _ = make(tmp_path, cam)
    r = c.get(f"/api/mount/cctv/snapshot?t={ticket(c)}")
    assert r.status_code == 200 and r.content == jpeg(2)
    sent = cam.auth_sent
    assert sent[0] == "" and sent[1].startswith("Digest ")
    assert not any(s.startswith("Basic ") for s in sent)
    assert PASSWORD not in "".join(sent)  # a digest response never carries the password itself


def test_no_username_no_credentials(tmp_path):
    cam = FakeCamera()
    cam.default = httpx.Response(200, headers={"content-type": "image/jpeg"}, content=jpeg(1))
    c, _, _ = make(tmp_path, cam, user="")
    assert c.get(f"/api/mount/cctv/snapshot?t={ticket(c)}").status_code == 200
    assert all("authorization" not in q.headers for q in cam.requests)


# ------------------------------------------------------------------ HLS

MASTER = f"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000
{CAM}/live/stream/high/index.m3u8?tok=abc
#EXT-X-STREAM-INF:BANDWIDTH=1
http://evil.example/steal.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2
../../etc/passwd.m3u8
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="audio/a.m3u8"
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:41
#EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#EXT-X-MAP:URI="init.mp4"
#EXTINF:2.000000,
seg41.ts
#EXTINF:2.000000,
seg42.ts?v=2
"""


def test_rewrite_playlist_uris():
    out = rewrite_playlist(MASTER, URL_HLS, URL_HLS, "TKT")
    lines = out.splitlines()
    assert "/api/mount/cctv/hls/low/index.m3u8?t=TKT" in lines
    assert "/api/mount/cctv/hls/high/index.m3u8?t=TKT&q=tok%3Dabc" in lines
    assert (
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="/api/mount/cctv/hls/audio/a.m3u8?t=TKT"' in lines
    )
    # outside the base / another host: pointed at the refusing path, never at the original
    assert lines.count("/api/mount/cctv/hls/_blocked?t=TKT") == 2
    assert "evil.example" not in out and "etc/passwd" not in out and "cam.example" not in out
    out = rewrite_playlist(MEDIA, f"{CAM}/live/stream/low/index.m3u8", URL_HLS, "T")
    assert 'URI="/api/mount/cctv/hls/low/key.bin?t=T"' in out
    assert "/api/mount/cctv/hls/low/seg42.ts?t=T&q=v%3D2" in out


@pytest.mark.parametrize(
    "path,ok",
    [
        ("index.m3u8", True),
        ("low/seg1.ts", True),
        ("../x", False),
        ("a/../../x", False),
        ("a//b", False),
        ("/abs", False),
        ("a\\b", False),
        ("%2e%2e/x", False),
        ("a/%2e%2e/b", False),
        ("a?b", False),
        ("", False),
        ("a\x00b", False),
    ],
)
def test_hls_path_rules(path, ok):
    assert hls_path_ok(path) is ok


def hls_camera() -> FakeCamera:
    cam = FakeCamera()
    cam.routes = {
        "/live/stream/index.m3u8": httpx.Response(
            200, headers={"content-type": "text/html"}, text=MEDIA
        ),
        "/live/stream/seg41.ts": httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"\x47" + b"\x00" * 187
        ),
        "/live/stream/low/index.m3u8": httpx.Response(200, text=MEDIA),
    }
    return cam


def test_hls_proxy_playlist_and_segment(tmp_path):
    cam = hls_camera()
    c, _, _ = make(tmp_path, cam, kind="hls")
    t = ticket(c)
    r = c.get(f"/api/mount/cctv/hls/entry.m3u8?t={t}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.apple.mpegurl"
    assert f"/api/mount/cctv/hls/seg41.ts?t={t}" in r.text
    assert f"{CAM}" not in r.text and "cam.example" not in r.text
    seg = c.get(f"/api/mount/cctv/hls/seg41.ts?t={t}")
    assert seg.status_code == 200 and seg.content[:1] == b"\x47"
    # the camera cannot make us serve html: the type comes from our own table
    assert seg.headers["content-type"] == "video/mp2t"
    assert seg.headers["content-security-policy"] == "sandbox"
    sub = c.get(f"/api/mount/cctv/hls/low/index.m3u8?t={t}")
    assert sub.status_code == 200 and "low/seg41.ts" in sub.text
    assert all(q.method == "GET" for q in cam.requests)


def test_hls_is_not_an_open_proxy(tmp_path):
    cam = hls_camera()
    cam.default = httpx.Response(200, text="SECRET OUTSIDE")
    c, _, _ = make(tmp_path, cam, kind="hls")
    t = ticket(c)
    n = len(cam.requests)
    attempts = [
        "../secret",
        "%2e%2e/secret",
        "..%2fsecret",
        "a/../../secret",
        "%252e%252e/secret",
        "_blocked",
        "http://evil.example/x",
        "//evil.example/x",
        "..\\secret",
        "a%5C..%5Csecret",
    ]
    for p in attempts:
        r = c.get(f"/api/mount/cctv/hls/{p}?t={t}")
        assert r.status_code in (404, 422, 502) or "SECRET OUTSIDE" not in r.text, p
        assert "SECRET OUTSIDE" not in r.text, p
    for q in cam.requests[n:]:
        assert q.url.host == "cam.example" and q.url.path.startswith("/live/stream/"), q.url
        assert ".." not in q.url.path
    # a request for an absolute URL in the query cannot redirect the host either
    r = c.get(f"/api/mount/cctv/hls/seg41.ts?t={t}&url=http://evil.example/")
    assert all(q.url.host == "cam.example" for q in cam.requests)
    # a redirect from the camera is not followed
    cam.routes["/live/stream/seg41.ts"] = httpx.Response(
        302, headers={"location": "http://evil.example/steal"}
    )
    r = c.get(f"/api/mount/cctv/hls/seg41.ts?t={t}")
    assert r.status_code == 502 and "evil.example" not in r.text
    assert all(q.url.host == "cam.example" for q in cam.requests)


def test_hls_limits_and_format(tmp_path, monkeypatch):
    cam = hls_camera()
    c, _, _ = make(tmp_path, cam, kind="hls")
    t = ticket(c)
    monkeypatch.setattr(cctv_mod, "HLS_SEGMENT_MAX", 100)
    cam.routes["/live/stream/big.ts"] = httpx.Response(200, content=b"x" * 500)
    r = c.get(f"/api/mount/cctv/hls/big.ts?t={t}")
    assert r.status_code == 502 and r.json()["detail"] == "Camera response too large"
    cam.routes["/live/stream/index.m3u8"] = httpx.Response(200, text="<html>not a playlist")
    r = c.get(f"/api/mount/cctv/hls/entry.m3u8?t={t}")
    assert r.status_code == 502 and r.json()["detail"] == "Stream format not recognised"
    cam.routes["/live/stream/index.m3u8"] = httpx.Response(401)
    r = c.get(f"/api/mount/cctv/hls/entry.m3u8?t={t}")
    assert r.json()["detail"] == "Camera rejected the login"


def test_hls_route_wrong_type(tmp_path):
    c, _, _ = make(tmp_path, hls_camera(), kind="mjpeg")
    assert c.get(f"/api/mount/cctv/hls/entry.m3u8?t={ticket(c)}").status_code == 409


def test_hls_config_query_forwarded_to_the_camera(tmp_path):
    cam = hls_camera()
    cam.routes["/live/stream/index.m3u8"] = httpx.Response(200, text=MEDIA)
    cam.routes["/live/stream/seg41.ts"] = httpx.Response(200, content=b"seg")
    c, _, _ = make(tmp_path, cam, kind="hls", url=URL_HLS + "?cam=7")
    t = ticket(c)
    c.get(f"/api/mount/cctv/hls/entry.m3u8?t={t}")
    c.get(f"/api/mount/cctv/hls/seg41.ts?t={t}")
    assert [q.url.query.decode() for q in cam.requests] == ["cam=7", "cam=7"]


# ------------------------------------------------------------------ leak hygiene


def test_nothing_secret_in_logs_audit_or_responses(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    cam = FakeCamera()
    cam.default = lambda request: (
        httpx.Response(200, headers={"content-type": "image/jpeg"}, content=jpeg(1))
        if request.headers.get("authorization") == _basic(USER, PASSWORD)
        else httpx.Response(401, headers={"www-authenticate": 'Basic realm="cam"'})
    )
    c, app, _ = make(tmp_path, cam)
    t = ticket(c)
    outs = [
        c.get(f"/api/mount/cctv/snapshot?t={t}"),
        c.get("/api/mount/cctv/status", headers=AUTH),
        c.get("/api/mount/settings", headers=AUTH),
    ]
    cam.raise_exc = httpx.ConnectError(f"{URL_MJPEG} {PASSWORD}")
    c.put("/api/mount/settings", headers=AUTH, json=body(cctv={"enabled": True}))  # reset result
    outs.append(c.get(f"/api/mount/cctv/snapshot?t={t}"))
    outs.append(c.get(f"/api/mount/cctv/snapshot?t=wrong{t}"))
    audit_text = "".join(
        p.read_text(encoding="utf-8") for p in (tmp_path / "audit").glob("*.jsonl")
    )
    logs = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (CAM, "cam.example", USER, PASSWORD, t):
        for o in outs:
            assert secret not in o.text and secret not in str(o.headers), secret
        assert secret not in audit_text, secret
        assert secret not in logs, secret
    assert "cctv_state" in audit_text  # the state changes themselves are audited, minus the URL


def test_uvicorn_access_log_redacts_the_ticket():
    cctv_mod.redact_ticket_logs()
    cctv_mod.redact_ticket_logs()  # idempotent
    lg = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, cctv_mod._TicketFilter) for f in lg.filters) == 1
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/api/mount/cctv/mjpeg?t=SECRETTICKET", "1.1", 200), None,
    )  # fmt: skip
    for f in lg.filters:
        f.filter(rec)
    assert "SECRETTICKET" not in rec.getMessage() and "?t=***" in rec.getMessage()


def test_only_get_methods_reach_the_camera(tmp_path):
    cam = hls_camera()
    cam.default = mjpeg_response([unit(1), unit(2)])
    c, _, _ = make(tmp_path, cam)
    t = ticket(c)
    c.get(f"/api/mount/cctv/mjpeg?t={t}")
    c.get(f"/api/mount/cctv/snapshot?t={t}")
    assert cam.requests and {q.method for q in cam.requests} == {"GET"}
    # the camera routes themselves are GET-only (no way to relay other methods)
    for method in ("post", "put", "delete"):
        assert getattr(c, method)(f"/api/mount/cctv/mjpeg?t={t}").status_code == 405


# ------------------------------------------------------------------ against tools/fake_cctv.py


def _load_fake():
    spec = importlib.util.spec_from_file_location(
        "fake_cctv", Path(__file__).resolve().parents[2] / "tools" / "fake_cctv.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_against_the_fake_camera_over_loopback(tmp_path):
    fake = _load_fake()
    server, cam = fake.make_server(0, user="cam", password="pw", hls=False)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        port = server.server_address[1]
        settings = Settings(
            port=8400, token=TOKEN, settings_path=tmp_path / "s.json", poll_enabled=False
        )
        app = create_app(
            settings,
            transport=FakePwi4().transport,
            secrets=MemorySecretStore(),
            cctv_secrets=MemorySecretStore(),
        )
        c = TestClient(app, base_url="http://127.0.0.1:8400")

        def configure(password):
            r = c.put(
                "/api/mount/settings",
                headers=AUTH,
                json=body(
                    cctv={
                        "enabled": True,
                        "type": "mjpeg",
                        "url": f"http://127.0.0.1:{port}/mjpeg",
                        "username": "cam",
                    },
                    cctv_password=password,
                ),
            )
            assert r.status_code == 200

        configure("wrong")
        r = c.get(f"/api/mount/cctv/snapshot?t={ticket(c)}")
        assert r.status_code == 502 and r.json()["detail"] == "Camera rejected the login"
        configure("pw")
        r = c.get(f"/api/mount/cctv/snapshot?t={ticket(c)}")
        assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
        assert c.get("/api/mount/cctv/status", headers=AUTH).json()["state"] == "live"
    finally:
        server.shutdown()
        server.server_close()
        cam.close()
