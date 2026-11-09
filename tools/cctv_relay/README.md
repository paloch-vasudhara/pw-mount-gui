# CCTV relay (RTSP -> HLS)

A browser cannot play RTSP, the usual IP-camera stream. A small relay on the mount PC (or any
PC the gateway can reach) republishes it as HLS; the gateway then proxies that HLS to the web
app (T5.7). Cameras that already offer MJPEG or HLS do not need the relay: put their URL in
Settings directly.

## Install (MediaMTX)

1. Download the Windows build from <https://github.com/bluenviron/mediamtx/releases> and unpack
   it into its own folder, e.g. `C:\mediamtx\`.
2. Copy `mediamtx.yml` from this folder over the one in that folder.
3. Replace `rtsp://USER:PASSWORD@CAMERA-IP:554/PATH` with the camera's RTSP URL (the camera's
   manual or its ONVIF / web page gives the path). The login goes inside the URL here; this copy
   of the file is the only place it lives.
4. Run `mediamtx.exe`. For start-up with Windows use Task Scheduler ("At log on") or NSSM; keep
   it bound to loopback as the template does.

go2rtc works the same way (`streams: mount: rtsp://...`, HLS at `/api/stream.m3u8?src=mount`);
the gateway only needs the HLS playlist URL.

## Connect it

In the web app: Station Settings > CCTV: Enable, Type **HLS**, Stream URL
`http://127.0.0.1:8888/mount/index.m3u8`. MediaMTX serves HLS without a login on loopback, so
Username / Password stay empty. Save; the status line goes Connecting > Live.

## Check it

From the repo root, with no flags it reads the URL, username and password the gateway has stored:

    .venv\Scripts\python tools\cctv_relay\check_stream.py

or against any stream directly (the password comes from the `CCTV_PASSWORD` environment
variable, never from the command line):

    .venv\Scripts\python tools\cctv_relay\check_stream.py --url http://127.0.0.1:8888/mount/index.m3u8 --type hls

It fetches the stream once and prints the format, resolution and frame rate (MJPEG) or the
playlist details and segment size (HLS). It never prints the URL path, the query or any
credential: only `scheme://host:port`.

## Notes

- HLS adds 2-6 s of delay (segment length x segment count). That is fine for watching the mount
  from a distance; shorten `hlsSegmentDuration` if it matters.
- Browsers need H.264. If the camera sends H.265, set it to H.264 in the camera's settings (or
  transcode with ffmpeg).
- The relay only ever reads from the camera, and so does the gateway.
