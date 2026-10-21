# Session handoff — updated 2026-10-20 (end of day, Phase 1 checkpoint)

Read this first, then `docs/BUILD_PLAN.md` (§7 tracker + **resume point**, §6 task specs, §8 logs/findings), then `docs/logic_sheet.txt` as needed.

## Where we are

- **Phase 0 COMPLETE** (commit `7d0bbdb`).
- **Phase 1 COMPLETE, accepted by the user 2026-10-20.** The user commits it as the Phase 1 checkpoint; check `git log`, and if `7d0bbdb` is still on top the commit is pending.
  - **T1.1 Gateway core**: read-only PWI4 poller (`GET /status` only), G8 state machine, Sun/Moon (skyfield, local `de421.bsp`), 10 Hz publish ticker decoupled from fetching, `pwi4.age_ms` staleness, gateway-computed time, pointing null when disconnected, hardened WS (Origin allowlist + first-message token, 4401) and Host check.
  - **T1.2 Station Settings**: `%LOCALAPPDATA%\pw-mount-control\settings.json` (`PW_SETTINGS_PATH`), API key in Windows Credential Manager (`PW_SECRET_STORE=memory` for tests), REST settings + site-check (`X-Mount-Token`), first-run `timezone: null` = PC zone, site mismatch warning.
  - **T1.3 Mount Status panel**: Pinia store + single socket with auto-reconnect after gateway restarts (time-limited fetch/open, 5 s backoff cap), stale display, palette pass.
  - **T1.4 3D view + Details modal**: scope-pointer port (three 0.169.0), render-on-demand, full WebGL disposal, red rules (FAULT, Sun ≤ 15°).
  - **T1.5 UI polish** (the user's review): Settings row grid via CSS vars `--ss-label 100 / --ss-fmt 60 / --ss-field 162`, separator 2│1│2, span 227, Lim EL 90, modal 788, Providers rows on the Location pitch ("clear key" inside the API key box); dropdown values ellipsize before the triangle (`.fmt-select-text`); Mount Status panel text scales with panel width (container query + clamp, unchanged at 1920, floors on the 150 % laptop).
- **Open item O-SITE (resolve before Phase 3):** Details-modal LST/HA differ from PWI4 by 3 m 36 s. The cause is that the Settings longitude (E 101.4639°) differs from PWI4's site (E 100.5656°); it is not J2000 drift (HA uses apparent RA). See BUILD_PLAN §8 T1.5 log for the options.
- **Phase 1 committed `52a3e3a`.**
- **T2.1 Sky Viewer chart ✅ (verified 10-21, UNCOMMITTED):**
  - stars from `web/public/data/stars.json` (re-generate with `tools/build_star_data.py --vmag X`);
  - `components/sky/{SkyChart,SkyClock}.vue` with three render-on-demand canvases (base / stars 1 Hz / overlay), `data-geom` + `data-frames-*` test hooks;
  - gateway `site.timezone` in the snapshot.
  - **T2.1b ✅:** sky colour follows the Sun (test hook `window.__skyTestSunAlt` + `sky-test-refresh` event), broken-ring marker, Settings save flow (disabled when unchanged; mismatch warns first, second SAVE accepts; new read-only `POST /api/mount/site-check`).
  - The user check of T2.1b is pending; the live sim goto follow was confirmed clean by the user.
- **Next: T2.2.** Write its spec (analysis service: vectorised SGP4 per orbit class in 30-min chunks, DB query changes G6; web chunk cache, refresh at 10 min remaining, eviction; one canvas per class above `stars`).
- Gate tooling: `tools/browser_gate/cdp.mjs` (`size` takes an optional DPR: `[w, h, 1.5]`) + `tools/pwi4_harness/simdrv.py` (sim-guarded driver). Only drive the sim to above-horizon targets (P14).
- Decisions logged 10-20: G4 **start-up stop** (implement in T3.1); A4 **red warnings**; Lim EL field stays 90 px.

## Gotchas learned this phase

- Nitro `devProxy` is HTTP-only (no WS) and **hangs forever when its backend is down** → in dev the browser WS goes straight to the gateway (`runtimeConfig.public.gatewayWs`); every fetch through the proxy must be time-limited.
- Windows: connecting to a closed localhost port waits the full timeout (≈0.5 s) — never couple a periodic loop to a connect attempt.
- PWI4 reports zeros (JD 0, az/alt 0) when the mount is disconnected.
- shadcn `DialogContent`/`DialogScrollContent` needed explicit props (Nuxt SFC compiler can't resolve reka-ui's extended prop types) — browser-only failure; vitest passed.
- Nuxt auto-import mis-parses tuple-typed `export const X: [number, number]` in `app/utils/`.
- The user often runs `pnpm dev` (3200) + gateway (8400): never touch them; throwaway Nuxt needs `NUXT_IGNORE_LOCK=1`; throwaway gateway: temp `PW_SETTINGS_PATH`, `PW_SECRET_STORE=memory`, `PW_ALLOWED_ORIGINS=<web origin>`.
- Ephemeris `data/ephemeris/de421.bsp` is local-only: run `tools/fetch_ephemeris.py` on a fresh checkout.
- Workers must kill processes **by PID only** (a worker once ran `taskkill /IM chrome.exe`, which closes the user's Chrome too).
- This PC's system time zone is **UTC**, so "Local Time" with a null timezone equals UTC here. Test local time with an explicit zone.
- Browser-gate `eval` strings: never use regex literals (they break in the JSON step file); use `JSON.stringify(innerText)`.
- Python on this Windows box can't see Git Bash `/tmp` — use the session scratchpad or repo-relative paths.

## What exists (repo map)

| Path | What | Status |
|---|---|---|
| `docs/logic_sheet.txt` / `.html` | Functional spec, Rev 2 (rule IDs G1–G8, A1–A5, H1–H5) | source of truth |
| `docs/BUILD_PLAN.md` | Phases, task specs (§6), tracker (§7), logs/findings (§8) | source of truth |
| `docs/archive/logic_sheet_rev1.txt` | Original sheet | archive |
| `concept/*.png` | UI concept renders (`ui-concept.psd` local only, git-ignored) | reference |
| `scope-pointer/` | Original three.js 3D mount prototype (to be ported in T1.4) | prototype |
| `pwi4/pwi4_client.py` | PlaneWave PWI4 HTTP wrapper — **do not modify**; import via path insert | vendor |
| `data/` | HIP catalogue (`hip_main.txt` local only, git-ignored), `bright_stars.csv` | data |
| `web/` | Nuxt 4 + shadcn-vue (`reka-lyra` style), Tailwind 4 with Preflight, Barlow font, Pinia | T0.1/T0.2 |
| `web/app/components/widgets/` | Ported ssa-toolkit widgets + sign chip, caps, `invalid`, AltitudeField | T0.2 |
| `web/app/pages/index.vue` | Dashboard: 5 `SectionFrame` panels; MOUNT STATUS live, Tools group | T0.1/T1.3 |
| `web/app/components/{mount,settings}/`, `stores/mount.ts`, `lib/hemisphere3d/` | Mount Status panel, Details modal, 3D view, Station Settings | T1.2–T1.5 |
| `web/app/pages/dev/{health,widgets,mount}.vue` | Dev-only harness pages (unlinked) | T0.1/T0.2/T1.1 |
| `gateway/` | FastAPI read-only gateway: poller, state, snapshot, hub (WS), security, settings | T1.1/T1.2 |
| `analysis/` | FastAPI, `GET /api/health`, `python -m analysis [--port]`, no orekit | skeleton |
| `tools/pwi4_harness/` | PWI4 **simulator-only** harness (10 scenarios) + `sim_guard` | T0.3 |
| `tools/browser_gate/` | Headless-Chrome CDP driver used for UI gates (see its README) | supervisor tool |
| `tools/bench/` | O1 sgp4-vs-orekit benchmark scripts | one-off |

## How to run

```
# Python (repo-root venv, Python 3.12)
.venv\Scripts\python -m pytest          # 84 pass, 13 PWI4-sim tests skipped by default
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m gateway         # :8400
.venv\Scripts\python -m analysis        # :8410

# Web (pnpm 12, Node 24)
cd web
pnpm dev                                 # :3200, proxies /api/mount → :8400, /api → :8410
pnpm lint | pnpm typecheck | pnpm test | pnpm generate

# PWI4 simulator harness (PWI4 must be open with Simulator.Mount)
set PWI4_SIM=1 && .venv\Scripts\python tools\pwi4_harness\run_harness.py   # ~7 min, report in tools/pwi4_harness/out/
```

Gate expectations today: lint 0 errors (11 warnings, all generated `components/ui/*`), typecheck 0, vitest 245/245, pytest 84 pass / 13 skip, ruff clean. Fresh checkout: run `tools/fetch_ephemeris.py` first.

## Working method (user-mandated: /fable-supervise)

- Supervisor (main session) designs each task (spec into BUILD_PLAN §6), delegates labour to a **Sonnet worker** agent with a self-contained brief, then **independently re-verifies** (re-run gates + browser/sim checks) before marking ✅. Record in §7/§8 + memory, report to the user.
- Workers: no commits, throwaway ports only (kill and confirm), never modify ssa-toolkit, `pwi4/`, `docs/` (supervisor-owned).
- **The user commits** (this session's commit was made at the user's explicit request).
- Never point automated tests at a PWI4 connected to the real mount. `sim_guard` requires `Devices.Mount = Simulator.Mount` in `%USERPROFILE%\Documents\PlaneWave Instruments\PWI4\Settings\PWI4.cfg`.
- UI gates must include a **real-browser** run (`tools/browser_gate`): happy-dom unit tests missed two real bugs in T0.2.

## Environment facts

- Ports: web 3200 · gateway 8400 · analysis 8410 · PWI4 8220 · avoid ssa-toolkit 8000/3100. Throwaway ports used so far: 134xx/184xx/132xx.
- PWI4 4.1.0, external connections disabled. Simulator: alt-az, 20 °/s, 20 °/s², axis0 −300…300°, axis1 −40…91°. PWI4 site = 14.3479°N 100.5656°E 12 m (differs from the logic-sheet default — the T1.2 mismatch warning should catch it).
- ssa-toolkit (read/copy only): `C:\Users\soar\Desktop\DELV\ssa-toolkit`, baseline HEAD `84f2ded`, must stay clean (I1).
- Chrome: `C:/Program Files/Google/Chrome/Application/chrome.exe`. orekit env (benchmark only): `C:\Users\soar\miniforge3\envs\orekit310`.

## Pitfalls seen

- First `nuxt dev` load can fail with Vite "504 Outdated Optimize Dep" — reload once.
- Kill servers by port: `netstat -ano | grep ":PORT .*LISTENING"` → `taskkill //PID <pid> //T //F`.
- reka-ui selects/popovers need real mouse events in tests; `element.click()` won't select.
- Python printing non-ASCII on this console needs `PYTHONIOENCODING=utf-8`.
- Use absolute paths with `git -C` (shell cwd changes between calls).

## Carried-forward items (don't lose these)

| Item | Where it lands |
|---|---|
| PWI4 has **no watchdog** (P1) → user decided: gateway stops any motion it finds at start-up (G4) | T3.1 |
| PWI4 accepts below-horizon targets (P14) → gateway Lim EL check is the only guard | T3.1 |
| O-SITE: one authoritative site (Settings vs PWI4); LST/HA offset; maybe block motion on mismatch, HA via LAST | before Phase 3 |
| User's later UI sizing/alignment tweaks | as they come |
| "Stopped" must be judged from position/velocity; `is_slewing` lags up to 1.65 s (P2); SLEWING beats TRACKING (P3) | T1.1 state machine |
| Offsets only move a **tracking** mount (P10) → jog/step while STOPPED needs another mechanism | T3.4 design |
| Always `update_wrap=0`; manage axis0 wrap explicitly; `goto_alt_az` may unwind the long way (P8, P9) | T3.1/T3.4, D-SUN |
| `follow_tle(name, line1, line2)` order (P5); acquisition 10–16 s (P6) | T4.2 |
| HOMING detection + velocity fields unverifiable in the sim (P12) | Phase 3 real-mount session |
| Harness metric `08_watchdog.explicit_stop_latency_s` mis-measured | opportunistic fix |
| Fields commit on blur (legacy parity), not Enter; IOD rows need row-level Enter | T5.3 |

## T2.1 kickoff checklist

1. Re-read BUILD_PLAN §5 Phase 2 rows, logic sheet Sky Viewer rules, O5 (magnitude limit adjustable), and concept renders for the Sky Viewer panel.
2. Write the T2.1 spec into §6: star data prep from `data/hip_main.txt` (local, git-ignored; V ≤ 6.5 default, adjustable) → compact asset; sky chart canvas (alt-az projection, horizon, cardinal points, Lim EL ring, Sun keep-out), fed by the store's time/site; render-on-demand like the 3D view.
3. Brief a Sonnet worker (kill processes by PID only), then verify: gates, real-browser screenshots at 1920 and 1280@1.5, star positions spot-checked against skyfield for the Settings site.
