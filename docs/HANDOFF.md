# Session handoff — updated 2026-10-22 (T3.1 verified)

Read this first, then `docs/BUILD_PLAN.md` (§7 tracker + **resume point**, §6 task specs, §8 logs/findings), then `docs/logic_sheet.txt` as needed.

## Where we are

- **Phases 0–2 COMPLETE and committed:** Phase 1 `52a3e3a`; T2.1 `053ef61`; Phase 2 `26b18f0`.
  - Phase 2 delivered: Sky Viewer stars, sky colour, satellites (analysis chunks), filters + Labels toggle, nearest-pick, selection + Track Info card, 30-day stale-TLE cutoff, local flags/icons, panel scaling.
- **Phase 3, UNCOMMITTED:**
  - **T3.1a ✅ (verified 10-21):**
    - `gateway/control.py`: the only PWI4 writer; serialized commands, STOP preempts and comes from any tab; single control lock.
    - `gateway/commands.py`: strict schemas.
    - `gateway/audit.py`: JSONL in `%LOCALAPPDATA%\pw-mount-controludit`, `PW_AUDIT_DIR`, 90 days.
    - Link-loss via uvicorn protocol pings (0.2/0.4 s): abort 0.21–0.22 s, silent loss 0.69 s.
    - Start-up and shutdown stops.
    - `/dev/control` page.
  - **T3.1b ✅ (verified 10-22):**
    - `gateway/safety.py`: site gate on PWI4's site (O-SITE resolved); Lim EL / Sun target checks (closes P14); Sun path check; the user's Sun shortcut; monitor; recovery.
    - Monitor false-stop bug fixed (validated gotos are watched by a 2° path corridor + actual limits; the predictive look-ahead only for tracking and unvalidated motion).
    - P15: `goto_altaz` sends `tracking_off` first.
    - Sim suite 23/23 twice; independent re-gate passed (BUILD_PLAN §8).
    - Next: user check via `/dev/control` → commit → T3.2.
  - **The resume point is in BUILD_PLAN §7.**
- **User decisions (10-21) for Phase 3:**
  - PWI4 site for safety maths + refuse motion on site mismatch;
  - single control lock (STOP from anyone);
  - protocol ping link-loss;
  - refuse Sun-crossing slews until D-SUN;
  - **Sun shortcut:** skip Sun checks when `sun_alt + 15 < min(Lim EL, mount alt)`;
  - HOME (≈85° el) refused when its path crosses the Sun until T3.5;
  - audit kept 90 days;
  - stale-TLE badge → T4.3.
- **Satellite DB:** `POSTGRESQL_DSN` in the repo-root `.env` (git-ignored). **Never read or print `.env`.** Gate scripts use `analysis.db.make_engine()` (read-only session) and print counts only.
- **PWI4 is in Simulator mode** (sim_guard OK on 10-21). The sim was left **disconnected**. `PWI4.cfg` SHA-256 `B714321830FB166AC4DB38BEE51CB7AC18560A63E6C1782AAA9F09F15A1A42F0`; check it is unchanged after every sim session.

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
- Skyfield + Unix time: never `ts.utc(1970,1,1,0,0,t_unix)` (it counts leap seconds → 27 s off); use `ts.from_datetime(datetime.fromtimestamp(t, timezone.utc))`.
- Sim/WS test clients: never block the event loop (wrap REST calls in `asyncio.to_thread`) and use `max_queue=None`. Otherwise the client stops answering pings and the gateway (correctly) drops it as link loss.
- PWI4 `goto_alt_az` keeps tracking on (P15); `mount/stop` ends tracking, but its own HTTP response still says tracking (stale).
- Sim tests that keep clear of the REAL Sun depend on the time of day and the sim's start pose: use the "never closer than the start, end ≥ 35° clear" rule (`safe_target`). The Phase-1 test can leave the sim near the morning Sun.
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
