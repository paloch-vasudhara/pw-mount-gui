# Session handoff — 2026-10-19 → next session

Read this first, then `docs/BUILD_PLAN.md` (§7 tracker + resume point, §8 logs), then `docs/logic_sheet.txt` as needed.

## Where we are

- **Phase 0 (Foundations) is COMPLETE and verified**: T0.1 scaffold, T0.2 shared widgets, T0.3 PWI4-sim harness. User approved the Phase 0 check (widgets).
- **Next task: T1.1 — Gateway core** (Phase 1 "Read path & status"). Not started; no spec written yet.
- Commit at end of this session: _see `git log -1`_ ("Phase 0 complete …").

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
| `web/app/pages/index.vue` | Dashboard shell: 5 empty `SectionFrame` panels | T0.1 |
| `web/app/pages/dev/{health,widgets}.vue` | Dev-only harness pages (unlinked) | T0.1/T0.2 |
| `gateway/` | FastAPI, `GET /api/mount/health`, `python -m gateway [--port]`, loopback only | skeleton |
| `analysis/` | FastAPI, `GET /api/health`, `python -m analysis [--port]`, no orekit | skeleton |
| `tools/pwi4_harness/` | PWI4 **simulator-only** harness (10 scenarios) + `sim_guard` | T0.3 |
| `tools/browser_gate/` | Headless-Chrome CDP driver used for UI gates (see its README) | supervisor tool |
| `tools/bench/` | O1 sgp4-vs-orekit benchmark scripts | one-off |

## How to run

```
# Python (repo-root venv, Python 3.12)
.venv\Scripts\python -m pytest          # 8 pass, 11 PWI4-sim tests skipped by default
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

Gate expectations today: lint 0 errors (11 warnings, all generated `components/ui/*`), typecheck 0, vitest 199/199, generate OK, pytest 8 pass / 11 skip, ruff clean.

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
| Section title chip colours: renders show light text in an outlined box; build has grey fill + dark text (size already reduced) | T1.3 palette pass |
| PWI4 has **no watchdog** (P1) → propose "gateway stops any motion it finds at start-up" policy — ask the user | T3.1 |
| "Stopped" must be judged from position/velocity; `is_slewing` lags up to 1.65 s (P2); SLEWING beats TRACKING (P3) | T1.1 state machine |
| Offsets only move a **tracking** mount (P10) → jog/step while STOPPED needs another mechanism | T3.4 design |
| Always `update_wrap=0`; manage axis0 wrap explicitly; `goto_alt_az` may unwind the long way (P8, P9) | T3.1/T3.4, D-SUN |
| `follow_tle(name, line1, line2)` order (P5); acquisition 10–16 s (P6) | T4.2 |
| HOMING detection + velocity fields unverifiable in the sim (P12) | Phase 3 real-mount session |
| Harness metric `08_watchdog.explicit_stop_latency_s` mis-measured | opportunistic fix |
| Fields commit on blur (legacy parity), not Enter; IOD rows need row-level Enter | T5.3 |

## T1.1 kickoff checklist

1. Re-read BUILD_PLAN §2 (I2, I3, I5, I7), §3, and §8 findings P1–P3, P11.
2. Write the T1.1 spec into §6: PWI4 poller (10 Hz, async, own process), status model, state machine G8 (incl. P2/P3 rules, DISCONNECTED when PWI4 unreachable), Sun/Moon via skyfield, WS hub (snapshot broadcast, drop-old for slow clients, message envelope), localhost hardening (Origin + Host checks, per-launch token), tests (unit + PWI4-sim marked `pwi4sim`).
3. Brief a Sonnet worker; verify independently (unit tests, sim run, forged-origin WS rejected, sustained telemetry rate).
