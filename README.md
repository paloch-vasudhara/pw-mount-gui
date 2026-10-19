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

## Python setup

```
python -m venv .venv
.venv/Scripts/pip install -r gateway/requirements.txt -r analysis/requirements.txt -r requirements-dev.txt
```

Checks: `pytest`, `ruff check .`; in `web/`: `pnpm lint`, `pnpm typecheck`, `pnpm generate`, `pnpm test`.
