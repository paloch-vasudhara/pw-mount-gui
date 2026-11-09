"""Fetch the configured CCTV stream once and report its format, resolution and frame rate.

    python tools/cctv_relay/check_stream.py                      # the gateway's stored settings
    python tools/cctv_relay/check_stream.py --url URL --type hls [--user NAME]
                                                                 # password: env CCTV_PASSWORD

Never prints the URL path / query or any credential: only scheme://host:port.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402

from gateway.cctv import CameraAuth, UnitParser, boundary_of, jpeg_of  # noqa: E402
from gateway.settings import SettingsStore, cctv_url_error  # noqa: E402

SAMPLE_S = 4.0


def where(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc.rsplit('@', 1)[-1]}"


def jpeg_size(data: bytes) -> str:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return f"{im.width}x{im.height}"
    except Exception:
        return "unknown (Pillow missing or bad JPEG)"


def check_mjpeg(client: httpx.Client, url: str) -> int:
    with client.stream("GET", url) as r:
        if r.status_code != 200:
            print(f"FAIL: the camera answered HTTP {r.status_code}")
            return 1
        ct = r.headers.get("content-type", "")
        boundary = boundary_of(ct)
        if boundary is None:
            print(f"FAIL: not an MJPEG stream (content-type {ct.split(';')[0] or 'missing'})")
            return 1
        parser, frames, first, t0 = UnitParser(boundary), 0, None, time.monotonic()
        for chunk in r.iter_bytes():
            for unit in parser.feed(chunk):
                frames += 1
                first = first or jpeg_of(unit)
            if time.monotonic() - t0 > SAMPLE_S:
                break
        dt = time.monotonic() - t0
    if not frames:
        print("FAIL: no frames arrived")
        return 1
    print("format: MJPEG (multipart/x-mixed-replace)")
    print(f"resolution: {jpeg_size(first)}")
    print(f"frame rate: {frames / dt:.1f} fps ({frames} frames in {dt:.1f} s)")
    return 0


def get_text(client: httpx.Client, url: str) -> str:
    r = client.get(url)
    r.raise_for_status()
    return r.text


def check_hls(client: httpx.Client, url: str) -> int:
    text = get_text(client, url)
    if not text.lstrip("﻿").startswith("#EXTM3U"):
        print("FAIL: not an HLS playlist")
        return 1
    print("format: HLS")
    media_url = url
    variants = re.findall(r"#EXT-X-STREAM-INF:([^\n]*)\n(\S+)", text)
    if variants:
        res = [m.group(1) for a, _ in variants if (m := re.search(r"RESOLUTION=(\d+x\d+)", a))]
        print(f"variants: {len(variants)}" + (f" ({', '.join(res)})" if res else ""))
        media_url = urljoin(url, variants[0][1])
        text = get_text(client, media_url)
    segs = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    durs = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", text)]
    live = "#EXT-X-ENDLIST" not in text
    print(f"playlist: {len(segs)} segments, {'live' if live else 'VOD'}")
    if durs:
        print(f"segment length: {sum(durs) / len(durs):.1f} s (delay about {sum(durs):.0f} s)")
    if segs:
        r = client.get(urljoin(media_url, segs[-1]))
        r.raise_for_status()
        print(f"last segment: {len(r.content) // 1024} KiB")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--url", help="default: the gateway's stored CCTV URL")
    ap.add_argument("--type", choices=("mjpeg", "hls"), help="default: the stored type")
    ap.add_argument("--user", help="default: the stored username")
    a = ap.parse_args()
    if a.url:
        url, kind, user = a.url, a.type or ("hls" if ".m3u8" in a.url else "mjpeg"), a.user or ""
        password = os.environ.get("CCTV_PASSWORD", "")
    else:
        store = SettingsStore()
        c = store.current.cctv
        url, kind, user = c.url, a.type or c.type, a.user if a.user is not None else c.username
        try:
            password = store.cctv_secrets.get() or ""
        except Exception:
            password = ""
        if not url:
            print("No CCTV URL stored: configure it in Station Settings or pass --url")
            return 2
    if (why := cctv_url_error(url)) is not None:
        print(f"FAIL: {why}")
        return 2
    print(f"camera: {where(url)} ({kind})")
    auth = CameraAuth(user, password) if user else None
    try:
        with httpx.Client(auth=auth, timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            return check_mjpeg(client, url) if kind == "mjpeg" else check_hls(client, url)
    except httpx.TimeoutException:
        print("FAIL: Camera timed out")
    except httpx.HTTPStatusError as exc:
        print(f"FAIL: the camera answered HTTP {exc.response.status_code}")
    except httpx.HTTPError:
        print("FAIL: Camera unreachable")
    return 1


if __name__ == "__main__":
    sys.exit(main())
