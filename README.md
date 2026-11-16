# pw-mount-control

Mount control web app for the PlaneWave L-350 (PWI4). See `docs/BUILD_PLAN.md`.

| Service | Dir | Dev port | Run |
|---|---|---|---|
| web (Nuxt 4) | `web/` | 3200 | `cd web && pnpm install && pnpm dev` |
| gateway (FastAPI) | `gateway/` | 8400 | `python -m gateway [--port N]` |
| analysis (FastAPI) | `analysis/` | 8410 | `python -m analysis [--port N]` |

Ports can also be set with `PW_GATEWAY_PORT` / `PW_ANALYSIS_PORT`. The web dev proxy targets come from
`NUXT_GATEWAY_URL` / `NUXT_ANALYSIS_URL` (defaults `http://127.0.0.1:8400` / `http://127.0.0.1:8410`):
`/api/mount/*` goes to the gateway, `/api/*` to analysis.

## Production mode on one port

The gateway serves the built web app and forwards every other `/api/*` request to the analysis
service, so the browser only talks to http://127.0.0.1:8400 (no Nuxt dev server, no Node at run time):

```
cd web && pnpm install && pnpm generate      # once: builds web/.output/public
python -m analysis                           # 127.0.0.1:8410 (loopback only, no auth of its own)
python -m gateway                            # 127.0.0.1:8400
```

Open http://127.0.0.1:8400. `PW_WEB_DIR` points the gateway at another build directory (default
`web/.output/public`); `PW_ANALYSIS_URL` at another analysis address. Without a build, `/` answers
with a hint and the dev setup (`pnpm dev` on 3200) works as before. The static files carry
`nosniff` / `no-referrer` / `DENY` framing headers; no CSP is set yet (for T6.1).

## Launcher

One click starts analysis + gateway (+ MediaMTX when set up), waits until they are healthy, opens the
app in its own window (pywebview / Edge WebView2) on the gateway address, and closing the window
stops everything it started (the gateway gets CTRL_BREAK first, so its shutdown stops the mount).

```
.venv/Scripts/pip install -r launcher/requirements.txt     # pywebview, once
python -m launcher [--browser] [--console-log] [--gateway-port N] [--analysis-port N]
"tools/package/PW Mount Control (dev).cmd"                 # same, no console window
```

- Needs the built web app (`web/.output/public`, or `PW_WEB_DIR`). Ports 8400 / 8410, else the next
  free one in 8401-8419 / 8411-8429 (a busy port is never killed).
- `--browser`: opens the default browser instead (also the fallback when WebView2 is missing); a small
  window stays open, closing it stops everything. A second launch focuses the running window.
- MediaMTX starts only when Settings has CCTV enabled with type HLS, `relay/mediamtx.exe` +
  `mediamtx.yml` exist (or `%LOCALAPPDATA%\pw-mount-control\launcher.json` `{"mediamtx": "<folder>"}`)
  and 127.0.0.1:8888 is free.
- Logs: `%LOCALAPPDATA%\pw-mount-control\logs\` (`launcher.log`, `gateway.out.log`,
  `analysis.out.log`, `mediamtx.out.log`; previous runs kept as `.1`-`.4`). Window geometry:
  `%LOCALAPPDATA%\pw-mount-control\window.json`.
- Tests: `pytest launcher/tests`.

## Python setup

```
python -m venv .venv
.venv/Scripts/pip install -r gateway/requirements.txt -r analysis/requirements.txt -r requirements-dev.txt
```

Checks: `pytest`, `ruff check .`; in `web/`: `pnpm lint`, `pnpm typecheck`, `pnpm generate`, `pnpm test`.
