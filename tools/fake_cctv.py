"""T5.7 fake mount camera for the CCTV tests and the browser check (never a real camera).

    python tools/fake_cctv.py 18740 [--user cam --password secret] [--no-hls] [--fps 10]

Serves, on 127.0.0.1:<port>:
  /mjpeg            multipart/x-mixed-replace JPEG stream, ~10 fps, a moving marker + a clock
  /snapshot.jpg     one JPEG
  /hls/index.m3u8   HLS (+ segments) from ffmpeg's test pattern, only when ffmpeg is on PATH
  /__mode/<mode>    test hook: none (normal) | stream (the MJPEG / HLS fail with 503, the snapshot
                    still works) | all (everything answers 503); returns the mode set
With --user / --password every route except /__mode wants Basic auth.
"""

from __future__ import annotations

import argparse
import base64
import io
import math
import shutil
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw

W, H = 640, 360
BOUNDARY = "fakecctvframe"


def render_frame(t: float) -> bytes:
    """One JPEG: dark scene, a marker moving along a loop, the wall clock."""
    img = Image.new("RGB", (W, H), (18, 22, 30))
    d = ImageDraw.Draw(img)
    for x in range(0, W, 80):
        d.line([(x, 0), (x, H)], fill=(34, 40, 52))
    for y in range(0, H, 80):
        d.line([(0, y), (W, y)], fill=(34, 40, 52))
    x = int((t * 90) % (W - 40)) + 20
    y = int(H / 2 + (H / 3) * math.sin(t * 1.7))
    d.ellipse([x - 14, y - 14, x + 14, y + 14], fill=(255, 120, 30), outline=(255, 255, 255))
    d.text((12, 12), "FAKE CCTV", fill=(255, 255, 255))
    clock = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    d.text((12, H - 24), clock, fill=(200, 220, 255))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=70)
    return out.getvalue()


class FakeCamera:
    def __init__(self, user: str | None, password: str | None, fps: float, hls: bool) -> None:
        self.user, self.password, self.fps = user, password, fps
        self.mode = "none"
        self.hls_dir: Path | None = None
        self.ffmpeg: subprocess.Popen | None = None
        self.notice: str | None = None
        self._tmp: tempfile.TemporaryDirectory | None = None
        if hls:
            self._start_hls()

    def _start_hls(self) -> None:
        exe = shutil.which("ffmpeg")
        if exe is None:
            self.notice = "ffmpeg not on PATH: HLS skipped (MJPEG and snapshot only)"
            return
        self._tmp = tempfile.TemporaryDirectory(prefix="fake_cctv_hls_")
        self.hls_dir = Path(self._tmp.name)
        cmd = [
            exe, "-hide_banner", "-loglevel", "error", "-re",
            "-f", "lavfi", "-i", f"testsrc=size={W}x{H}:rate=10",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-g", "20",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "6",
            "-hls_flags", "delete_segments+omit_endlist",
            str(self.hls_dir / "index.m3u8"),
        ]  # fmt: skip
        self.ffmpeg = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        if self.ffmpeg.poll() is not None:
            self.notice = "ffmpeg could not start the test pattern: HLS skipped"
            self.hls_dir = None

    def close(self) -> None:
        if self.ffmpeg is not None and self.ffmpeg.poll() is None:
            self.ffmpeg.terminate()
            self.ffmpeg.wait(timeout=5)
        if self._tmp is not None:
            self._tmp.cleanup()


def make_handler(cam: FakeCamera):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a) -> None:  # quiet
            pass

        def _send(self, code: int, ctype: str, body: bytes, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            if cam.user is None:
                return True
            want = "Basic " + base64.b64encode(f"{cam.user}:{cam.password}".encode()).decode()
            if self.headers.get("Authorization") == want:
                return True
            challenge = {"WWW-Authenticate": 'Basic realm="cam"'}
            self._send(401, "text/plain", b"login required", challenge)
            return False

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path.startswith("/__mode/"):
                mode = path.rsplit("/", 1)[1]
                if mode in ("none", "stream", "all"):
                    cam.mode = mode
                return self._send(200, "text/plain", cam.mode.encode())
            if not self._authed():
                return
            if cam.mode == "all" or (cam.mode == "stream" and path != "/snapshot.jpg"):
                return self._send(503, "text/plain", b"unavailable")
            if path == "/snapshot.jpg":
                return self._send(200, "image/jpeg", render_frame(time.time()))
            if path == "/mjpeg":
                return self._mjpeg()
            if path.startswith("/hls/") and cam.hls_dir is not None:
                f = cam.hls_dir / path[len("/hls/") :]
                if f.parent == cam.hls_dir and f.is_file():
                    ctype = "application/vnd.apple.mpegurl" if f.suffix == ".m3u8" else "video/mp2t"
                    return self._send(200, ctype, f.read_bytes())
            self._send(404, "text/plain", b"not found")

        def _mjpeg(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.end_headers()
            try:
                while cam.mode == "none":
                    jpg = render_frame(time.time())
                    head = (
                        f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpg)}\r\n\r\n"
                    )
                    self.wfile.write(head.encode() + jpg + b"\r\n")
                    self.wfile.flush()
                    time.sleep(1.0 / cam.fps)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    return Handler


def make_server(
    port: int,
    user: str | None = None,
    password: str | None = None,
    fps: float = 10.0,
    hls: bool = False,
) -> tuple[ThreadingHTTPServer, FakeCamera]:
    cam = FakeCamera(user, password, fps, hls)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cam))
    server.daemon_threads = True
    return server, cam


def main() -> None:
    ap = argparse.ArgumentParser(description="Fake mount camera (MJPEG, snapshot, optional HLS)")
    ap.add_argument("port", type=int, help="throwaway port, e.g. 18740")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--no-hls", action="store_true", help="do not start the ffmpeg test pattern")
    args = ap.parse_args()
    server, cam = make_server(args.port, args.user, args.password, args.fps, hls=not args.no_hls)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"fake camera: {base}/mjpeg  {base}/snapshot.jpg")
    if cam.hls_dir is not None:
        print(f"             {base}/hls/index.m3u8")
    elif not args.no_hls:
        print(f"notice: {cam.notice}")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        cam.close()


if __name__ == "__main__":
    main()
