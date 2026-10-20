# pw-mount-control — Build Plan & Progress Tracker

> Status: **APPROVED 2026-10-19** — build starts at T0.1 after the user's baseline commit.
> Spec: [logic_sheet.txt](logic_sheet.txt) (readable: [logic_sheet.html](logic_sheet.html)). Rule IDs (G1, A1d, H4 …) below refer to it.
> Concepts: `../concept/*.png` · 3D prototype: `../scope-pointer/` · PWI4 wrapper: `../pwi4/pwi4_client.py` · Reference repo (read/copy only): `C:\Users\soar\Desktop\DELV\ssa-toolkit`

---

## 1. Working method (fable-supervise)

Each task `Tn.m` runs the same loop. The supervisor (main session) owns this file, the firewall and verification.

| Step | Who | Output |
|---|---|---|
| 1. Design | Opus designer agent | Task spec in §6: files touched, contract, invariants it must keep, verification steps |
| 2. Delegate | Supervisor → Sonnet worker | Tight brief; the same worker is resumed with SendMessage; fresh worker only on a session break (with a written handoff) |
| 3. Implement | Sonnet worker | Additive change, no commits |
| 4. Verify | Supervisor, independently | Re-runs the gate checks in §5 itself; never accepts "done" on trust |
| 5. Record | Supervisor | Updates §7 tracker + §8 logs + project memory; reports to the user |
| 6. User check | User | Phase-end checklist (§5); the user commits |

**Non-negotiables:** no agent commits (the user commits) · ssa-toolkit is never modified · throwaway ports only, every server torn down after a check · user hand-styling is never reverted · this file + the logic sheet are the source of truth · at ~90–95 % of the session budget: no new briefs, PAUSE in-flight agents at a compiling state, write the resume point here + memory, stop.

**PWI4 testing:** mount-control work is tested against **PWI4 in simulation mode** (user-confirmed, O3). Never point automated tests at a PWI4 instance connected to the real mount; real-mount sessions are user-run checks only.

---

## 2. Invariants (the firewall)

Every phase gate re-checks the invariants it touches.

| ID | Invariant | How it is checked |
|---|---|---|
| I1 | ssa-toolkit is untouched | `git -C ../ssa-toolkit status --porcelain` empty and HEAD equal to the baseline in §8 |
| I2 | Only the gateway talks to PWI4 | grep: no PWI4 host/port outside `gateway/`; PWI4 bound to localhost |
| I3 | Link loss ⇒ mount stopped (G4) | PWI4-sim test: kill the socket mid-slew/jog/track → stop within ≤ 1 s; stays STOPPED after reconnect |
| I4 | Limits are enforced server-side (G1–G3) | Gateway tests send forged commands below Lim EL / inside the Sun disc → refused; continuous monitor stops at the limit |
| I5 | Analysis can never stall the mount loop | Separate process; telemetry rate measured while a long analysis job runs (no drop below the target rate) |
| I6 | No secrets reach the browser | API key never in any response/WS frame (grep of captured traffic) |
| I7 | Local endpoint hardening | Origin + Host checks, per-launch token; a forged-origin WS connection is rejected |
| I8 | Live data only (G6) | No decayed object in any list/search/viewer result |
| I9 | No Java in the app | No orekit/JVM import anywhere in `gateway/` or `analysis/` (O1) |

---

## 3. Architecture (agreed)

```
 Mount PC (always)                                   Analysis backend (configurable URL)
 ┌──────────────────────────────────────────┐        ┌───────────────────────────────────┐
 │ gateway/  FastAPI (127.0.0.1)            │  HTTP  │ analysis/  FastAPI "analysis-lite"│
 │  • serves the built web app (web/)       │ ─────▶ │  • sky layers (python-sgp4, numpy)│
 │  • WS: telemetry ↓ / commands ↑ (acks)   │ proxy  │  • passes · IOD (sgp4/skyfield)   │
 │  • PWI4 poller 5–10 Hz → state + Sun/Moon│ /api/* │  • astrometry (ported pipeline)   │
 │  • safety engine (Lim EL, Sun, D-SUN)    │        │  • satellite DB queries           │
 │  • track workflow (A1d), settings file   │        └───────────────────────────────────┘
 │  • audit log                             │         web-app: remote server (+ DB)
 └───────────────┬──────────────────────────┘         standalone: local process; DB on server
                 │ HTTP localhost:8220
              PWI4 (sim mode for tests) ── L-350 mount
```

Repo layout (planned): `web/` (Nuxt 4 + shadcn-vue + Tailwind 4 + Pinia, Barlow bundled locally) · `gateway/` · `analysis/` · `tools/` (data prep, test helpers) · `data/` (HIP) · `docs/`.

---

## 4. Resolved items & designs

| ID | Item | Resolution |
|---|---|---|
| O1 | Analysis engine | **python-`sgp4` + skyfield + numpy, no orekit/Java.** Benchmark (§8): ~40× faster than orekit; positions identical to < 0.2 mm, az/el within 4″ (FOV ≈ 3°). Astrometry pipeline ported from ssa-toolkit `photometry/`. |
| O2 | Slew rate / acceleration / keyhole limits | Read from PWI4 at connect (`mount.axisN.max_velocity_degs_per_sec`, `acceleration_degs_per_sec_sqr`, `min/max_mech_position_degs`); used by D-SUN timing. Simulator: 20 °/s, 20 °/s². |
| O3 | PWI4 simulator | Use **PWI4's built-in sim mode**. A small stub may be added only where a test needs fault injection PWI4-sim cannot produce (e.g. a failing connect step). |
| O4 | Optical "Visible Duration" | Above Lim EL **and** illuminated **and** Sun below the horizon (0°) at the observer. |
| O5 | Star magnitude cutoff | Start at V ≤ 6.5, kept as an **adjustable parameter** (config, re-generate star data). |
| O6 | Gibbs ↔ Herrick-Gibbs | Herrick-Gibbs when the angle between position vectors < 3°, else Gibbs. |
| O7 | D-SUN clearance margin | +1° → detour circle radius 16°. |

### D-SUN — Sun avoidance path planner (H5 / G2) — approved design

Implemented in **T3.5**.

1. **Model the path.** Each axis moves independently (PWI4 per-axis slew), so a slew is a curve in (az, alt) over time — sample it at the axis rates read from PWI4 (O2). Object tracks are their predicted (az, alt, t) path. The Sun position is evaluated at each sample time.
2. **Detect.** The path violates when any sample's angular distance to the Sun < 15°.
3. **Detour (slews).** Project the Sun and the path into a local tangent plane centred on the Sun. Generate two candidate detours — clockwise and counter-clockwise — along a circle of radius 16° (15° + O7 margin) between the entry and exit points. Discard a candidate that dips below Lim EL or crosses the zenith keyhole. Pick the cheaper by total axis travel time. Execute as a PWI4 custom path (alt/az points with timestamps).
4. **Detour (object tracks).** Split the track into allowed segments. For a blocked interval [t_in, t_out]: leave the object at t_in, follow the detour arc to the exit point, arrive before t_out, then resume following. Status stays TRACKING; the card shows "avoiding Sun".
5. **No valid detour** (both candidates blocked) → refuse the command (G7 reason), or stop at the disc edge if already moving (G3).
6. **Verification:** pure-function unit tests (straight slews through the disc, near-zenith Sun, Sun near Lim EL, passes crossing the disc), then PWI4-sim runs.

---

## 5. Phases

Each phase ends with a **supervisor gate** (independent checks) and a **user check** (things only you can judge). Tasks within a phase run in order unless marked ∥. Order: the Sky Viewer is built before the control path so control tests can be driven from it.

### Phase 0 — Foundations
| Task | Scope | Gate |
|---|---|---|
| T0.1 | Scaffold `web/` (copy ssa-toolkit Nuxt config, tokens, shadcn-vue setup; Barlow local), `gateway/`, `analysis/` skeletons; Python env/requirements (no Java); lint/typecheck scripts; record I1 baseline | `pnpm lint` + typecheck clean; both FastAPI apps start on throwaway ports; I1, I9 |
| T0.2 | Port shared widgets: AngleField/DmsTriad/DdInput (LEGACY chip behaviour, H2), FormatSelect, SegToggle, NumberField, DateTimeField/AppCalendar (H1), dialog shell; dev harness page | Widget harness screenshots vs ssa-toolkit legacy behaviour (chips, caps, scroller) |
| T0.3 | PWI4 sim-mode test harness: scripts that drive PWI4-sim through `pwi4_client` (connect, enable, home, goto, tracking, follow_tle, custom path) and capture `/status`; fault-injection stub only if needed | Harness runs end-to-end against PWI4-sim; findings (watchdog, rates) logged in §8 |

**User check:** widget feel (chips, scrolling, typing limits) matches ssa-toolkit.

### Phase 1 — Read path & status
| Task | Scope | Gate |
|---|---|---|
| T1.1 | Gateway core: PWI4 poller, state machine (G8), Sun/Moon (skyfield), WS hub (snapshot broadcast, drop-old for slow clients), local hardening (I7) | Unit tests for state mapping; I2, I7; sustained telemetry rate |
| T1.2 | Settings: file store on the gateway, Settings modal (A2b-iii) incl. Timezone, Lim EL, write-only key, PWI4 mismatch warning | Save/cancel round-trip; mismatch warning at ±0.01° / ±100 m; I6 |
| T1.3 | Web: dashboard layout shell, WS client store (reconnect, stale-data display), Mount Status dashboard panel (A4i) | Live values from PWI4-sim; stale/disconnected states |
| T1.4 | Port the 3D module (scope-pointer → Vue component) + 15° Sun disc + Lim EL ring; Detailed Mount Status modal (A4ii) | Screenshot vs Detailed Info.png; idle = no rendering |

**User check:** dashboard status and detailed modal read correctly against PWI4-sim.

### Phase 2 — Sky Viewer
| Task | Scope | Gate |
|---|---|---|
| T2.1 | Star data prep (HIP → V ≤ 6.5 JSON, cutoff adjustable, O5); sky chart canvas: orientation, grid, cardinals, Lim EL dashed ring + dimming, Sun/Moon, mount pointing circle, UTC/Local clock | Screenshot vs Main Dashboard 1.png (no star labels) |
| T2.2 ∥ | Analysis: vectorised SGP4 class layers in 30-min chunks, DB query changes (exclude decayed, G6; latest-500 default for search); web: chunk cache, refresh at 10 min remaining, eviction | Full-catalogue chunk time on the mount PC; I8; I5 under load |
| T2.3 | Filters tree (A2a), nearest-pointer hover, label hover disabled, selection/focus/deselect, Track Info cards in read-only form (A1a values, A1b disabled) | Picking tests in clumps; filter parent/child semantics; 1 Hz card values |

**User check:** performance with the full catalogue on the mount PC; picking in GEO clumps.

### Phase 3 — Control path & safety
| Task | Scope | Gate |
|---|---|---|
| T3.1 | Command protocol (ids/acks), control lock, audit log, link-loss stop (G4), **start-up stop (G4)**, safety engine v1 = **refuse/stop** at Lim EL & Sun (G1–G3) | I3, I4 tests incl. forged commands; audit entries |
| T3.2 | Connection sequence (A5a) with BUSY/FAULT, failed-step message, confirmation; Home (HOMING) | PWI4-sim: success path + failure handling |
| T3.3 | Pointing mode STOP/TRACK (A5b); H4 transform helper (explicit time) | H4 unit tests vs astropy reference ≤ 1″; sidereal track on PWI4-sim |
| T3.4 | Go To modal (A5c-iv) and Jog modal (A5c-iii): dead-man jog, serialized steps, arrow keys, sidereal nudge, disabled during object track | I3 for jog; red-box/disable rules; step serialization |
| T3.5 | **D-SUN** (§4) for slews/goto/jog (safety engine v2) | Planner unit tests (§4 step 6); PWI4-sim slews through the Sun |

**User check:** full control session on PWI4-sim, driven from the dashboard and Sky Viewer (connect/disconnect, home, goto, jog/step, STOP, link-loss stop by closing the tab); then the user's own first real-mount session.

### Phase 4 — Tracking
| Task | Scope | Gate |
|---|---|---|
| T4.1 | TRACK OBJECT button rules (A1): above 0°, pass reaches Lim EL, Sun | Rule table tests |
| T4.2 | Gateway track workflow (A1d): pass check, pre-position wait at the Lim EL crossing, follow_tle handoff, stop at Lim EL, D-SUN for track segments | PWI4-sim passes: normal, rising-below-limit, peak-below-limit (refused), through-the-Sun |
| T4.3 | Card pinning/stacking rules (A1e) + Track Target modal (A5c-ii): 500-cap search, latest-500 default, filters, aircraft tab disabled | A1e scenario table walked in e2e |

**User check:** satellite track on PWI4-sim, then on the real mount (pre-position → follow → stop at Lim EL).

### Phase 5 — Analysis modals
| Task | Scope | Gate |
|---|---|---|
| T5.1 | Pass Analysis lite (A2b-i), optical visibility per O4, live [Track] button | Pass list vs ssa-toolkit for the same inputs (tolerance) |
| T5.2 | Astrometry lite (A2b-iv): upload, epoch/exposure autofill, detect streaks, click-edit, ≥ 10 streaks + target, astrometry.net only, background job pill | Sample images from ssa-toolkit docs; I6 |
| T5.3 | IOD lite (A2b-ii): Gauss → Gibbs/HG (O6), coarse TLE, row rules, H4 at row epoch, overwrite confirmations, readable failures | Known-orbit test vectors (synthetic obs from a TLE → recovered elements) |

**User check:** an end-to-end IOD from uploaded images.

### Phase 6 — Hardening & packaging
| Task | Scope | Gate |
|---|---|---|
| T6.1 | Security pass (rate limits, message size caps, schema validation, token handling), gateway-crash warning → PWI4 handoff (G4 note) | I6, I7 re-run; fuzzed WS messages |
| T6.2 | Deployment modes: web-app (remote analysis URL + server token) and standalone (local analysis + pywebview window); one config switch | Both modes start from a clean checkout following the README |
| T6.3 | Full regression run on PWI4-sim + real-hardware acceptance checklist | All invariants I1–I9 green |

**User check:** final acceptance on the mount PC.

---

## 6. Task specs

_Filled in by the designer at the start of each task (files touched, contract, invariants, verification steps)._

### T0.1 — Scaffold (spec, 2026-10-19)

**Goal:** an empty-but-working skeleton of all three services, with tooling, so every later task only adds code.

**Ports (dev):** web 3200 · gateway 8400 · analysis 8410 (avoid ssa-toolkit 8000/3100 and PWI4 8220). Every service must accept a port override (env/arg) so the supervisor can verify on throwaway ports.

**web/** — Nuxt 4, `ssr: false`, `srcDir: 'app/'`, modules `@pinia/nuxt`, `shadcn-nuxt`, `@nuxt/eslint`; Tailwind v4 via `@tailwindcss/vite` **with Preflight** (full `@import "tailwindcss"` — unlike ssa-toolkit, no legacy CSS here).
- shadcn-vue initialised with **style `lyra`** (sharp/boxy — user choice), base colour neutral, CSS variables, lucide icons; aliases as ssa-toolkit's `components.json`. Add primitives: button, dialog, select, popover, native-select, checkbox, input.
- Font: **Barlow** bundled locally via `@fontsource/barlow` (400/500/600/700), set as the sans font.
- Design tokens (dark only, first pass from the concept renders; refined in T1.3): page/panel bg `#252629`, sky disc `#0d1116`, panel border `#626262`, control border `#848484`, section-title chip bg `#7f7f7f`, text `#e8e8e8`, muted text `#979898`, label slate `#6f8796`, accent green border `#5d9f60`, accent green text `#71af74`, primary button `#277d2a`→`#4fa152` gradient, disabled `#7f7f7f`, danger `#e05252`, grid line `#4d575d`. Map them onto the shadcn CSS variables (`--background`, `--border`, `--primary`, …) plus app tokens. Radius 0.
- Dev proxy (Nitro `devProxy`, same prefix handling as ssa-toolkit `nuxt.config.ts`): `/api/mount` → gateway, `/api` → analysis. Targets from env `NUXT_GATEWAY_URL` / `NUXT_ANALYSIS_URL` (defaults `http://127.0.0.1:8400`, `http://127.0.0.1:8410`).
- `app/pages/index.vue`: dashboard **shell only** — the 3-column layout of Main Dashboard 1.png with five empty `SectionFrame` panels (TRACK INFO, FILTERS, SKY VIEWER, MOUNT STATUS, MOUNT CONTROL). `SectionFrame.vue` = bordered box with the title chip on the top-left edge as in the renders.
- `app/pages/dev/health.vue` (dev-only, unlinked): calls `/api/mount/health` and `/api/health`, shows OK/FAIL for each.
- Scripts: `dev` (port 3200), `build`, `generate`, `lint`, `typecheck` (vue-tsc), `test` (vitest, one trivial test).

**gateway/** — Python package, FastAPI. `GET /api/mount/health` → `{"ok": true, "service": "gateway", "version": "0.1.0"}`. `python -m gateway [--port N]` runs uvicorn on **127.0.0.1** only. `requirements.txt`: fastapi, uvicorn[standard], pydantic, httpx, skyfield. Test with TestClient.

**analysis/** — same shape. `GET /api/health` → `{"ok": true, "service": "analysis", "version": "0.1.0"}`; `python -m analysis [--port N]`; `requirements.txt`: fastapi, uvicorn[standard], pydantic, numpy, sgp4, skyfield. **No orekit / JPype (I9).**

**Repo root:** `requirements-dev.txt` (pytest, ruff, httpx); `pyproject.toml` with ruff + pytest config (testpaths `gateway/tests`, `analysis/tests`); dev venv at `.venv` (Python 3.12); `.gitignore` additions: `.venv/`, `node_modules/`, `.nuxt/`, `.output/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`; root `README.md` with how to run the three dev processes.

**Must not touch:** `scope-pointer/`, `pwi4/`, `concept/`, `data/`, `docs/` (supervisor-owned), and anything in ssa-toolkit (read/copy only). No commits.

**Gate (supervisor re-runs):** `pnpm install` / `lint` / `typecheck` / `generate` clean · `pytest` + `ruff check` clean · gateway + analysis start on throwaway ports and answer health · web dev on a throwaway port with env-overridden proxy targets → `/dev/health` shows both OK · screenshot of the shell vs Main Dashboard 1.png layout · I1 (ssa-toolkit clean @ `84f2ded`) · I9 (no orekit/jpype import) · all throwaway servers torn down.

### T0.2 — Shared widgets (spec, 2026-10-19)

**Goal:** the input widgets every modal needs (H1, H2), ported from ssa-toolkit and restyled to this app, plus the behaviour the Nuxt port is missing.

**Copy from ssa-toolkit `web/app/` @ `84f2ded`** (add a one-line provenance comment at the top of each copied file):
- `utils/dms.ts`, `utils/angle-format.ts`, `utils/datetime-fields.ts`; `composables/useSignGroup.ts`
- `components/widgets/`: SpinnerColumn, DdInput, DmsTriad, AngleField, FormatSelect, SegToggle, NumberField, DateTimeField, DateTimePopover, AppCalendar
- `components/ui/calendar` via `shadcn-vue add calendar` (reka-lyra)
- only the CSS rules these widgets use from `assets/css/widgets.css` → `web/app/assets/css/widgets.css`, re-tokenised to this app's palette (Barlow, square corners, 13 px form-row text)
- unit tests: `dms-core`, `angle-format`, `datetime-fields`, `widget-dd-input`, `widget-dms-triad`, `widget-angle-field`, `widget-number-field`, `widget-seg-toggle`, `widget-datetime-field`, `widget-calendar`, `widget-calendar-tz` (+ `@vue/test-utils`, `happy-dom`)

**New behaviour (this app):**
1. **Sign chip for signed angle kinds (El, Dec)** — closes ssa-toolkit gap UX-3 in the chip shape (renders: "+ 51 | 24 | 36.980", "+ 71.20"). A "+"/"−" chip at the front of the field, same look/position as the N/S, E/W cardinal chip, in both DD and DMS modes. Click toggles; typing `-` toggles; arrow/wheel stepping through zero flips it live (same semantics as NumberField `signed`). `modelValue` stays the signed decimal degrees. Unsigned kinds (Az, RA) unchanged.
2. **Range caps:** a narrower range per field (e.g. Goto local El 0…90, M9). Clamped when typing, stepping and wheeling. With min ≥ 0 the chip shows "+" and does not toggle.
3. **`invalid` prop:** red value text + red border (Goto render, G7 limits).
4. **Altitude field:** NumberField + FormatSelect units FT / M / KM; model in metres; switching unit converts the display, not the model.
5. **DateTimeField:** seconds precision only (no fractional seconds); picker button the same height as the fields (H1).
6. **`utils/format.ts` `fmtThousands()`:** single-space thousand separator, with a test.

**Harness:** `app/pages/dev/widgets.vue` (dev-only, unlinked) showing Az (DD/DMS), El signed (DD/DMS), El capped 0–90, Dec (DMS), RA (HMS), Lat (N/S chip), Lon (E/W chip), Altitude ft/m/km, NumberField signed, SegToggle, FormatSelect, DateTimeField — each with a live JSON readout of its model and a `data-testid`.

**Must not touch:** `SectionFrame.vue`, `pages/index.vue` (supervisor-edited), `scope-pointer/`, `pwi4/`, `concept/`, `data/`, `docs/`, ssa-toolkit. No commits.

**Gate (supervisor re-runs):** lint (0 errors) · typecheck · vitest (ported + new) · generate · CDP interaction tests on the harness (typing limits, wheel/arrow stepping, chip toggle and zero-crossing, caps, DD↔DMS↔HMS round-trip, unit switching, datetime typing + picker) · harness screenshot · I1.

### T0.3 — PWI4 sim-mode harness (spec, 2026-10-19)

**Goal:** a repeatable script set that drives **PWI4 in simulation mode** through `pwi4/pwi4_client.py`, records how PWI4 actually behaves, and answers the open facts later tasks depend on (status semantics, homing/stop timing, follow_tle accuracy, custom paths, offsets/jog, watchdog, poll latency).

**Environment facts (supervisor-checked 2026-10-19):** PWI4 4.1.0 on `127.0.0.1:8220`, "Allow external connections: False". Active mount device `Simulator.Mount` (`%USERPROFILE%\Documents\PlaneWave Instruments\PWI4\Settings\PWI4.cfg` → `Devices.Mount = Simulator.Mount`). Simulator: alt-az, 20 °/s max velocity, 20 °/s² accel, axis0 −300…300°, axis1 −40…91°, park az 180 / alt 45. PWI4 site 14.3479°N 100.5656°E 12 m. Status has **no** simulation flag.

**Location:** `tools/pwi4_harness/` (Python, repo `.venv`). Import the client from `pwi4/pwi4_client.py` via a path insert — **do not modify `pwi4/`**.

**Safety guard (mandatory, first thing every entry point does):** `sim_guard.assert_simulator()`
- reads `PWI4.cfg` (path overridable by env `PWI4_CFG`) and requires `Devices.Mount = Simulator.Mount`;
- requires PWI4 reachable on 127.0.0.1:8220;
- otherwise raises and sends **no** command. Unit-test the guard with temp cfg files (sim → pass, `Elmo.L350.Mount` → refuse, missing file → refuse).
- Never write any PWI4 settings file; never change the PWI4 site.

**Scenarios** (each returns the mount to a safe state — stop, tracking off — even on failure):
1. Connect → enable axes → find_home: record status fields over time, homing duration, which fields signal "homing" (none may exist — record that).
2. Goto alt/az (e.g. 180/45 then 90/30): slew duration, `is_slewing` transitions, final error vs commanded.
3. STOP mid-slew: time from command to `is_slewing=false` and velocities ≈ 0.
4. Goto RA/Dec J2000 + tracking on/off: `is_tracking` behaviour; does goto_ra_dec start tracking by itself?
5. `follow_tle` with a synthetic GEO TLE placed near the site meridian (generate with python-sgp4's exporter): pointing vs our own sgp4 + skyfield az/el for the same instants (this cross-checks our analysis engine against PWI4).
6. Custom path: determine the accepted `coord_type` values and point format for an alt/az path (PWI4 docs/error messages); run a short path and record tracking accuracy.
7. Offsets as jog/step: `mount_offset` axis rate (deg/s) start → reset (measure stop latency); `add_arcsec` step (measure completion); behaviour while tracking (sidereal nudge, N8/M8).
8. **Watchdog (O3):** start an axis rate offset, then send nothing (and don't poll) for 10 s → does PWI4 stop on its own? Then stop it.
9. Status polling: `/status` latency mean/p95/max at 10 Hz for 30 s.
10. Disconnect: disable axes → disconnect; leave PWI4 in the state the user had it (mount disconnected).

**Output:** `tools/pwi4_harness/out/<UTC timestamp>/report.json` + `report.md` (raw captures + findings per scenario). `out/` git-ignored.

**Tests:** guard unit tests run in the normal `pytest`; PWI4-sim scenarios are marked `pwi4sim` and **skipped unless** env `PWI4_SIM=1`.

**Must not touch:** `pwi4/`, `web/`, `scope-pointer/`, `concept/`, `data/`, `docs/`, PWI4 settings files, ssa-toolkit. No commits.

**Gate (supervisor re-runs):** pytest (default: guard tests pass, sim scenarios skipped) · ruff · guard refusal demo with a non-sim cfg · supervisor runs the full harness against PWI4-sim and checks the report · mount left disconnected · I1, I2.

### T1.1 — Gateway core (spec, 2026-10-20)

**Goal:** a read-only gateway that polls PWI4, turns raw status into a clean mount snapshot with the G8 state, adds Sun/Moon, and streams it to the browser over a hardened local WebSocket. **No mount commands in T1.1** (control is Phase 3) — the gateway must not call any PWI4 endpoint other than `/status`.

**Modules (`gateway/`):**
- `config.py` — env-driven settings: `PW_GATEWAY_PORT` (8400), `PW_PWI4_URL` (`http://127.0.0.1:8220`), `PW_POLL_HZ` (10), `PW_ALLOWED_ORIGINS` (default `http://127.0.0.1:3200,http://localhost:3200` + the gateway's own origin), `PW_EPHEMERIS` (default `data/ephemeris/de421.bsp`).
- `pwi4_io.py` — async status fetch with one keep-alive `httpx.AsyncClient`; parse with `pwi4/pwi4_client.py`'s parser (`PWI4.status_text_to_dict` / `parse_status`, imported via path insert; `pwi4/` stays unmodified). Timeout ≈ 0.5 s. Unreachable → typed "unreachable" result, never an exception up the loop.
- `snapshot.py` — pydantic `MountSnapshot` (schema below) built from parsed status + previous snapshot.
- `state.py` — pure G8 state function (rules below), unit-tested with fixtures.
- `ephem.py` — skyfield Sun/Moon topocentric alt/az, Moon illumination, own Sun distance to the pointing; computed at ≤ 1 Hz and cached between polls. Ephemeris file local (offline use): add `tools/fetch_ephemeris.py` that downloads `de421.bsp` into `data/ephemeris/` (git-ignored).
- `poller.py` — one asyncio task: poll at `PW_POLL_HZ`, build snapshot, publish to the hub; records poll latency. Must never block on clients.
- `hub.py` — WebSocket fan-out: per-client queue of size 1 (latest snapshot wins, older dropped); connect/disconnect hooks (T3.1 will attach the link-loss stop here).
- `security.py` — Host check, Origin check, per-launch token (rules below).
- `app.py` — FastAPI app with a lifespan that starts/stops the poller; routes below.

**Site for T1.1:** PWI4's own `site.*` values (`site.source = "pwi4"`). T1.2 switches to the Settings location and adds the mismatch warning.

**Snapshot schema (`MountSnapshot`):**
`seq`, `ts_utc`, `state` (G8), `state_reason` (str|null), `pwi4 {reachable, version, latency_ms}`, `site {lat, lon, height_m, source}`, `time {jd, mjd, lmst_hours}`, `mount {connected, az, alt, ra_j2000_h, dec_j2000_deg, ra_apparent_h, dec_apparent_deg, target_ra_apparent_h, target_dec_apparent_deg, is_slewing_raw, is_tracking_raw, axis0_wrap_min_deg, distance_to_sun_deg_pwi4}`, `axes [ {enabled, position_deg, velocity_deg_s (derived from positions/timestamps — PWI4 sim velocity fields are 0, P12), rms_arcsec, dist_to_target_arcsec, min_deg, max_deg, max_vel_deg_s, accel_deg_s2} ×2 ]`, `sun {alt, az}`, `moon {alt, az, illumination}`, `sun_distance_deg` (ours, pointing → Sun), `hour_angle_h` (LMST − RA apparent, wrapped ±12).

**State rules (G8, pure function of current + recent snapshots):**
1. PWI4 unreachable → `DISCONNECTED`, reason "PWI4 not running / not reachable".
2. `mount.is_connected = false` → `DISCONNECTED`, reason "mount not connected".
3. Connected and any axis disabled → `FAULT`, reason "axis N disabled".
4. `is_slewing_raw` and measurable motion (any axis |v| > 0.05 °/s within the last 0.3 s) → `SLEWING`. **`is_slewing_raw` with positions frozen ≥ 0.3 s → not slewing** (P2: the flag lags a stop by up to 1.65 s).
5. Else `is_tracking_raw` → `TRACKING` (P3: SLEWING already took precedence).
6. Else `STOPPED`.
`HOMING` and `BUSY` are set only by the command layer (Phase 3) via an override hook — provide the hook (`state.set_override(state, reason) / clear_override()`), unused in T1.1.

**Routes:**
- `GET /api/mount/health` (unchanged).
- `GET /api/mount/status` → latest snapshot JSON (debug / fallback).
- `GET /api/mount/session` → `{token}`; only when the Host check passes and the request is same-origin (`Sec-Fetch-Site` is `same-origin` or `none`, or Origin is in the allowlist).
- `WS /api/mount/ws` → Origin must be in the allowlist (missing Origin = reject); the client's **first message** must be `{"type":"auth","token":…}` within 2 s, else close code **4401**. Then server sends `{"type":"hello", …}` and a `{"type":"status","seq":…,"data":MountSnapshot}` stream at the poll rate. Envelope for all messages: `{type, seq, ts, data}`.

**Security (I7):** Host header must be `127.0.0.1:<port>` / `localhost:<port>` (else 400 — DNS-rebinding guard). Token = 32 random bytes (urlsafe) per process start; never in URLs or logs. No CORS middleware. Bind 127.0.0.1 only (already).

**Web side (minimal, for verification only):** `web/app/composables/useMountSocket.ts` (fetch token from `/api/mount/session`, open WS, reconnect with backoff, expose the latest snapshot + connection state) and a dev page `web/app/pages/dev/mount.vue` showing the live snapshot JSON + state. Dev proxy must forward the WS (`/api/mount` already proxied — confirm WS upgrade works through Nitro's devProxy; if not, document the fix). The real Mount Status panel is T1.3.

**Tests:**
- Unit (always run): status-text fixtures → snapshot; state table incl. P2 (flag true + frozen positions → not SLEWING after 0.3 s) and P3; derived velocity; Host/Origin/token rules (bad Host → 400, bad/missing Origin → WS rejected, no/late/wrong auth → 4401, good → hello + status); hub drop-old behaviour with a slow client; poller against a fake PWI4 via `httpx.MockTransport` incl. unreachable → DISCONNECTED and recovery.
- `pwi4sim` (only with `PWI4_SIM=1`, guarded by `tools/pwi4_harness/sim_guard`): run the gateway against PWI4-sim for 10 s → ≥ 9.5 Hz snapshots; drive a goto + STOP with `pwi4_client` **from the test** (the gateway itself still sends nothing) and assert the gateway's states go STOPPED → SLEWING → STOPPED with the STOPPED transition ≤ 0.5 s after motion stops (P2 rule).

**Must not touch:** `pwi4/`, `scope-pointer/`, `concept/`, `docs/`, existing widgets/pages other than adding the two new web files + proxy fix, ssa-toolkit. No commits.

**Gate (supervisor re-runs):** pytest (unit) + ruff + web lint/typecheck/test · gateway on a throwaway port: forged Host → 400, forged-Origin WS rejected, no-token WS → 4401 · PWI4-sim run: snapshot rate ≥ 9.5 Hz, states through a goto/STOP driven from a separate script, P2 rule observed · browser: `/dev/mount` through the web dev proxy shows live snapshots · gateway sends **only `/status`** to PWI4 (check PWI4's `HTTPServer.txt` log for the run) · I1, I2, I5 (gateway unaffected while analysis busy — trivial now, note), I9.

### T1.2 — Station Settings (spec, 2026-10-20)

**Goal:** persistent station settings owned by the gateway, the Settings modal (A2b-iii, Settings.png), the PWI4 site-mismatch warning, and the gateway switching its site (Sun/Moon/LMST) from PWI4's values to the Settings location.

**Gateway — store (`gateway/settings.py`):**
- File: `%LOCALAPPDATA%\pw-mount-control\settings.json` (override env `PW_SETTINGS_PATH`). Atomic write (temp file + `os.replace`). Schema-versioned (`"version": 1`).
- Fields: `location {lat_deg, lon_deg, alt_m, timezone (IANA)}`, `hardware {mount: "PW L-350", ota: "Celestron RASA11", lim_el_deg}`, `providers {astrometry: "astrometry.net", satellite_db: "DELV Aerospace"}`, `display {lat_format, lon_format, alt_unit}` (remembers the user's DD/DMS and FT/M/KM choices).
- Validation (pydantic): lat −90…90, lon −180…180, alt −500…9000 m, timezone valid via `zoneinfo` (add `tzdata` to requirements — Windows has no system tz database), lim_el 0…90.
- First run (no file): seed location from PWI4's site if reachable, else N 13.7862° E 101.4639° 40.8 m (Settings.png values); timezone = the PC's zone (sent by the browser on first save; gateway default `UTC` until then); lim_el 15.
- **API key (secret):** stored in the Windows Credential Manager via `keyring` (service `pw-mount-control`, user `astrometry.net`), behind a small `SecretStore` interface with an in-memory implementation for tests. Never written to `settings.json`, never logged, never returned — responses only carry `api_key_set: bool`.

**Gateway — API (all require the WS token in header `X-Mount-Token` **and** pass the Host/Origin checks; no token → 401):**
- `GET /api/mount/settings` → settings + `api_key_set`.
- `PUT /api/mount/settings` → validate, save, apply live; body may include `api_key`: string = set, `""` = clear, absent/null = keep. Response = saved settings + `site_check` (below).
- `GET /api/mount/site-check` → current comparison against PWI4.
- `site_check` = `{pwi4_reachable, ok, lat_diff_deg, lon_diff_deg, alt_diff_m, tolerance {lat_lon_deg: 0.01, alt_m: 100}}` (`ok` = within tolerance; null diffs when PWI4 unreachable). The gateway never writes the site to PWI4.

**Gateway — snapshot changes:** `site` now comes from Settings (`site.source = "settings"`), so Sun/Moon/LMST work even when PWI4 is down. Add `limits {lim_el_deg, sun_keepout_deg: 15}` and `site_check` (recomputed on each successful PWI4 fetch; the UI shows it on Save now and on Connect in T3.2).

**Web — Settings modal (`components/settings/StationSettingsDialog.vue`):**
- Layout per Settings.png: four fieldset groups with legends — Location (left top), Providers (right top), Hardware (left bottom), ADS-B (right bottom, disabled "Section awaiting protocol template") — and [CANCEL] / [SAVE SETTINGS] (green). Vertical separator between labels and form in Location.
- Location: Latitude/Longitude = `AngleField` with N/S and E/W cardinal chips + DD/DMS format selector; Altitude = `AltitudeField` (FT/M/KM); **Timezone** = searchable dropdown of `Intl.supportedValuesOf('timeZone')`, defaulting to the PC zone.
- Hardware: Mount / OTA = fixed single-entry dropdowns; **Lim EL [ °]** = numbers only, capped 0–90.
- Providers: Astrometry / Satellite DB fixed dropdowns; **API key** = password input, placeholder shows "•••• set" when `api_key_set`, empty means keep; a small "clear key" action.
- Cancel / ✕ = close without saving. Save = PUT → on success close, **unless** `site_check.ok` is false: then stay open showing an amber warning with the differences ("Location differs from PWI4's site: Δlat …°, Δlon …°, Δalt … m") and a Close button. Validation errors in red on the field; Save disabled while invalid.
- Opened from a minimal **Tools** group in the FILTERS panel (SATELLITE PATH ANALYSIS, INITIAL ORBIT DETERMINATION — present but disabled until Phase 5 — and STATION SETTINGS), matching the render.
- `composables/useGatewayApi.ts`: REST helper that gets the token from `/api/mount/session` (time-limited fetch, like `useMountSocket`) and sends `X-Mount-Token`.

**Tests:** Python — store round-trip + atomic write, validation edges, first-run seeding (PWI4 up / down), secret never in file/response/log (grep the written file and responses), token required (401), Origin/Host enforced, `site_check` tolerance edges (0.0099 vs 0.0101°, 99 vs 101 m), snapshot site source switch. Web (vitest) — dialog validation, Save disabled when invalid, payload shape (api_key omitted when untouched).

**Must not touch:** `pwi4/`, `scope-pointer/`, `concept/`, `docs/`, ssa-toolkit; never write PWI4 settings or site. No commits.

**Gate (supervisor re-runs):** pytest + ruff + web gates · browser: open STATION SETTINGS, change lat in DMS + altitude in FT + timezone, Save → reload → values persist; Cancel discards; API key never appears in DOM/network responses/settings.json; mismatch warning shown against PWI4-sim's site and absent when equal; snapshot `site.source = settings`, Sun/Moon present with PWI4 down · screenshot vs Settings.png · I1, I6, I7.

### T1.3 — Dashboard shell + Mount Status panel + palette pass (spec, 2026-10-20)

**Goal:** one app-wide telemetry store, the dashboard MOUNT STATUS panel (A4-i) driven by it, and the palette corrected against the renders.

**Palette pass (re-sampled from Main Dashboard 1.png with dominant-colour counts, 2026-10-20):**
- Section title chip: **transparent background, 1 px border `#626262` (same as the panel border), text `#848484`**, bold uppercase. Size stays as the user reduced it (13 px). The chip sits on the panel's top-left corner with its border shared with the panel border, as in the render.
- Main text `#d9d9d9` (replaces `#e8e8e8`); captions/labels in panels `#d9d9d9`; value-box border `#848484`; status/positive values green `#5d9f60`; button "active" green text `#71af74` (unchanged); page/panel bg `#252629`.
- Update the tokens in `tailwind.css` (not per-component colours) and re-check `/dev/widgets` + the Settings dialog still look right.

**Telemetry store (`web/app/stores/mount.ts`, Pinia):** wraps the existing `useMountSocket` logic so the app opens **one** gateway socket. Exposes `snapshot`, `connection` (`connecting|open|closed`), `linkStale` (socket not open, or the last status message older than 1.5 s by wall clock), and a `display` getter with formatted fields. `/dev/mount` switches to the store.

**Display rules (pure functions in `web/app/utils/mount-format.ts`, unit-tested):**
- Azimuth `204.63` (2 decimals, no sign); Elevation `+ 58.32` / `− 3.10` (sign, space, 2 decimals, U+2212 minus); Sun Distance `136.17°`.
- `null` value or `linkStale` → `—` shown dimmed.
- Status text = G8 state. When the gateway link itself is down (`linkStale`), Status shows `DISCONNECTED` with reason "gateway not reachable".
- Colours per A4-i: Status **green on TRACKING**, `#d9d9d9` otherwise; Sun Distance **green when > 15°** (`limits.sun_keepout_deg`), `#d9d9d9` otherwise.
- The state reason (G7) appears as a tooltip on the Status value.

**MOUNT STATUS panel (`components/mount/MountStatusPanel.vue`), layout per the render (crop: panel ≈ 450 × 560 px at 1920 × 1080):**
- Top: a 3D slot (`<div data-testid="mount-3d-slot">`, ~60 % of the panel height, empty dark area for now — T1.4 fills it).
- Two outlined value boxes side by side (Azimuth, Elevation) with captions `AZIMUTH (°)` / `ELEVATION (°)` below; value text bold, larger (≈ 20 px at 1920 width).
- Rows `Status` / `Sun Distance`, label left, value right-aligned.
- A small rounded-pill `…` button centred at the bottom → emits `open-details` (the detailed modal is T1.4; for now it opens nothing and has a "Details — coming in T1.4" tooltip).
- Panel content must not overflow at 1280 × 800.

**Must not touch:** gateway code (no backend changes in T1.3), `pwi4/`, `scope-pointer/`, `concept/`, `docs/`, ssa-toolkit; widgets only through tokens. No commits.

**Tests:** vitest for the formatters/colour rules (incl. null, negative elevation, sun at exactly 15°, stale) and the store's staleness logic (fake timers).

**Gate (supervisor re-runs):** web gates · browser with a live gateway + PWI4-sim: values match PWI4's GUI, Status colours for STOPPED/TRACKING, Sun Distance colour, stale handling when the gateway is killed and recovery after restart · screenshot of the dashboard vs Main Dashboard 1.png (chip style, panel crop) at 1920 × 1080 and 1280 × 800 · the T1.2 PWI4 site-mismatch warning shown against the real sim.

### T1.4 — 3D mount view + Detailed Mount Status modal (spec, 2026-10-20)

**Goal:** port the `scope-pointer/` three.js prototype into the app as a reusable Vue component fed by the telemetry store, show it in the MOUNT STATUS panel slot and in the Detailed Mount Status modal (A4-ii, Detailed Info.png), and apply the red-warning rules.

**3D component (`components/mount/MountHemisphere3D.vue` + `app/lib/hemisphere3d/*.ts`):**
- Port the **current** `scope-pointer/src` (coords, config, scene/{hemisphere, mount, celestial, pointer}) faithfully — the user refined it on 2026-10-16 (e.g. `FAR_SIDE_DIM` far-side depth cue); treat its visual choices as hand-styling, don't "clean them up". `three` from npm, pinned (same major/minor the prototype uses, 0.169.x), not the CDN. Drop the mock control panel / mock source — inputs now come from the store.
- Inputs: mount az/alt (store snapshot; smoothed toward each 10 Hz report like the prototype's `DISPLAY_TAU`), Sun alt/az, Moon alt/az + illumination, `limits.lim_el_deg`, `limits.sun_keepout_deg` (15 — replaces the prototype's 10°), target marker from `target_ra/dec_apparent` converted to alt/az with LMST + site latitude (prototype's `haDecToAltAz`), shown only while SLEWING.
- New: a dashed **Lim EL ring** on the dome at `alt = lim_el_deg` (matches the planned Sky Viewer ring). Pointer arrow/marker turn red inside the Sun keep-out (prototype behaviour, now 15°).
- Mount not connected / stale: hide arrow + markers, keep the mount model at its last pose (or the prototype default pose before any data), dim nothing else.
- Keep **render-on-demand** (frame only on data change, smoothing in progress, or orbit interaction). Expose the rendered-frame count as `data-frames` on the canvas wrapper for the gate.
- Two sizes: `compact` (dashboard slot — fills the panel's 3D area, orbit interaction kept) and `expanded` (modal). Each instance owns its renderer and **fully disposes** it on unmount (geometries, materials, textures, renderer, `forceContextLoss`) — opening/closing the modal repeatedly must not leak WebGL contexts.

**Detailed Mount Status modal (`components/mount/MountDetailsDialog.vue`)**, opened by the panel's `…` button; layout per Detailed Info.png:
- Left: expanded 3D view (black backdrop). Right: **Parameters** fieldset — Modified Julian Date, Local Sidereal Time, Azimuth, Elevation, R.A. (J2000), Declination (J2000), Hour Angle, Mount Status, Axis 0 Status, Axis 1 Status, Axis 0 RMS, Axis 1 RMS, Sun Distance (fixes the render's duplicated "Axis 0 Status" label).
- Formats: MJD `61 333.19885` (5 decimals, single-space thousands); LST `HH:MM:SS`; Az `320.500°`, El `39.302°` (3 decimals, signed El); RA `16ʰ 25ᵐ 43.32ˢ` (superscript h/m/s as in the render); Dec `+50° 08′ 39.30″`; Hour Angle `03ʰ 12ᵐ 20.11ˢ` signed; axis status = green dot "Enabled" / grey dot "Disabled"; RMS `0.560″`; Sun Distance `82.270°`. Null → dimmed `—`.
- Below Parameters: **Connection / Pointing Mode / Commands** groups rendered as in the render but **disabled** (tooltip "Mount control arrives in Phase 3").
- ✕ closes.

**Red-warning rules (A4-i, user 2026-10-20)** in `utils/mount-format.ts` + tests, used by the panel and the modal: Status green on TRACKING, **red on FAULT**, `#d9d9d9` otherwise; Sun Distance green when > keep-out, **red when ≤ keep-out**. Add a `--danger`-based tone (token already exists).

**Must not touch:** gateway code, `pwi4/`, `scope-pointer/` (read-only source for the port), `concept/`, `docs/`, ssa-toolkit. No commits.

**Gate (supervisor, with PWI4-sim running — includes carried T1.2/T1.3 live checks):** web gates · browser vs live gateway + PWI4-sim: 3D pointer follows PWI4 gotos (compare with PWI4 GUI az/alt), target marker during a slew, Sun disc 15° + Lim EL ring visible, idle → `data-frames` stops increasing · panel values vs PWI4 GUI (T1.3 carry) · FAULT red (disable an axis in PWI4) · modal parameters vs PWI4 (MJD/LST/RA/Dec/HA/axes) · open/close modal 10× without WebGL context warnings · screenshots vs Main Dashboard 1.png (panel) and Detailed Info.png (modal) · T1.2 site-mismatch warning against the real sim (carry).

### T1.5 — Phase 1 UI polish (spec, 2026-10-20, user-approved adjustment list)

**Goal:** the user's Phase 1 review items A (Station Settings geometry) and B (Mount Status panel text on small/scaled screens). Web only.

**A. Station Settings modal (`components/settings/StationSettingsDialog.vue` styles; shared widgets untouched unless scoped under `.station-dialog`).** One row grid in every section: `[label column L] [format 60] 2px │1px 2px [field 162]` → the full control span after the label column is **227 px**.
- A1: one shared label column width L (~100 px, enough for "Satellite DB"/"Longitude") in all sections. The left edge of the Lat/Lon/Alt format selectors = left edge of Mount, OTA, Lim EL (Hardware) and Astrometry, API key, Satellite DB (Providers, same offset inside its own fieldset).
- A2: Lat/Lon/Alt format/unit selectors 70 → **60 px** (`margin-right: 2px` stays).
- A3: exactly **2 px gap · 1 px separator · 2 px gap** between format selector and every value control in Location (Lat/Lon DMS suites and DD box, Altitude, Timezone). Separator position derived from L + 60 + 2, not a magic number.
- A4: every Location value control after the separator is **162 px** wide (= DMS suite 56+42+64; measure the suite — if it is wider, use the measured width throughout). Timezone clips long names.
- A5: Mount, OTA, Astrometry, API key, Satellite DB = **227 px**. **Lim EL stays 90 px** (user decision), left-aligned at the format-selector edge.
- A5c: both grid columns equal; modal width shrinks to fit (≈ 790 px: 2 × (L + 227 + fieldset padding/borders) + gap + body padding); single-column breakpoint moved down to match. No change to behaviour, validation, testids or the warning/error texts.

**B. MOUNT STATUS panel text scale (`components/mount/MountStatusPanel.vue`).** Size text from the panel's own width (CSS container query / `cqw` with `clamp`). At the panel width it has on a 1920×1080 @100 % screen (~430 px wide) the result must be **identical to today** (Az/El values 20 px, box min-w 100 × h 32, captions 14 px, Status/Sun rows 16 px, Az–El gap 65 px). At ~290 px (1280×720 CSS = 1080p @150 %) about: values ~15 px, box ~80 × 26, captions ~12 px, rows ~13 px, gap ~45 px; floors keep text readable (captions ≥ 11 px, rows ≥ 12 px). Details modal, title chips and the 3D view unchanged.

**Must not touch:** gateway, `pwi4/`, `docs/`, ssa-toolkit, other widgets' global CSS (scope overrides). No commits.

**Gate (supervisor):** web gates · real-browser geometry measurement of every A item (left edges, widths, 2|1|2 spacing, modal width) · screenshots of the panel at 1920×1080 DPR 1 (must equal pre-change computed sizes) and 1280×720 DPR 1.5.

---

## 7. Progress tracker

Status: ⬜ not started · 🟦 design · 🟨 implementing · 🟪 verifying · ✅ done · ⏸ paused/partial

| Task | Status | Design | Impl | Verified | User check | Notes |
|---|---|---|---|---|---|---|
| T0.1 | ✅ | ✅ 10-19 | ✅ 10-19 | ✅ 10-19 | — (phase-end) | `reka-lyra`; ports 3200/8400/8410; title-chip colours → T1.3 |
| T0.2 | ✅ | ✅ 10-19 | ✅ 10-19 | ✅ 10-19 | phase-end | 2 browser bugs found at gate, fixed + regression-tested |
| T0.3 | ✅ | ✅ 10-19 | ✅ 10-19 | ✅ 10-19 | ✅ (Phase 0 approved) | findings → §8 PWI4 behaviour |
| T1.1 | ✅ | ✅ 10-20 | ✅ 10-20 | ✅ 10-20 | phase-end | read-only verified (PWI4 log: 379/379 `GET /status`); 10 Hz up/down; STOPPED 0.09–0.13 s after STOP |
| T1.2 | ✅ | ✅ 10-20 | ✅ 10-20 | ✅ 10-20 | phase-end | dialog 1000 px like render; key secrecy + keyring live ✓; mismatch warning vs real PWI4-sim to be shown at next sim session |
| T1.3 | ✅ | ✅ 10-20 | ✅ 10-20 | ✅ 10-20 | phase-end | chips/palette fixed; live vs PWI4 GUI + T1.2 mismatch warning carried to the T1.4 sim gate |
| T1.4 | ✅ | ✅ 10-20 | ✅ 10-20 | ✅ 10-20 | **Phase 1 user check pending** | live vs PWI4-sim ✓ (values, FAULT red, sun-zone red, TRACKING green, target marker, modal ×10, idle frames); carried T1.2 mismatch + T1.3 values ✓ |
| T1.5 | ✅ | ✅ 10-20 | ✅ 10-20 | ✅ 10-20 | user check pending | Settings: label col 100, fmt 60, 2│1│2, fields 162, full span 227, Lim EL 90, modal 788 (1-col < 820); panel clamp/cqw: 1920 identical, 1280@1.5 → 14/80×26/11/12/45 (panel 258 px → floors) |
| T2.1 | ⬜ | | | | | O5 adjustable |
| T2.2 | ⬜ | | | | | |
| T2.3 | ⬜ | | | | | |
| T3.1 | ⬜ | | | | | |
| T3.2 | ⬜ | | | | | |
| T3.3 | ⬜ | | | | | |
| T3.4 | ⬜ | | | | | |
| T3.5 | ⬜ | | | | | D-SUN |
| T4.1 | ⬜ | | | | | |
| T4.2 | ⬜ | | | | | |
| T4.3 | ⬜ | | | | | |
| T5.1 | ⬜ | | | | | O4 |
| T5.2 | ⬜ | | | | | |
| T5.3 | ⬜ | | | | | O6 |
| T6.1 | ⬜ | | | | | |
| T6.2 | ⬜ | | | | | |
| T6.3 | ⬜ | | | | | |

**Resume point:** **Phase 1 complete and accepted by the user (T1.1–T1.5 + follow-up polish), committed by the user as the Phase 1 checkpoint** (if `git log` still shows `7d0bbdb` on top, the commit is still pending). Open item O-SITE (§8 T1.5 log) is to be resolved before Phase 3. Next: **Phase 2 / T2.1** (star data + sky chart canvas) — write its spec first.

---

## 8. Logs

### Decision log
| Date | Ref | Decision |
|---|---|---|
| 2026-10-19 | Stack | Nuxt 4 + shadcn-vue (same as ssa-toolkit); font Barlow; single-space thousand separator |
| 2026-10-19 | Arch | Gateway (PWI4, safety, WS) separate from analysis; gateway always on the mount PC and serves the app |
| 2026-10-19 | G1–G8 | Limits, continuous Sun check, limit-reached stop, disconnection stop, one track at a time, live data only — logic sheet Rev 2 |
| 2026-10-19 | Review | C1–C8, M1–M10, Q1–Q10, N0–N7, U1–U3 folded into logic sheet Rev 2 |
| 2026-10-19 | O1–O7 | Resolved (§4); D-SUN design approved |
| 2026-10-19 | Order | Sky Viewer (Phase 2) before control path (Phase 3) so control tests run from the Sky Viewer |
| 2026-10-19 | Plan | Build plan approved |
| 2026-10-20 | T1.4 | Elevation / Hour Angle show a sign only when negative (U+2212), as in the render; Dec always signed | render parity | supervisor (user may change) |
| 2026-10-20 | T1.4 | 3D Moon phase from the Sun/Moon geometry (prototype), not the gateway's `moon.illumination` | the drawing can't disagree with itself | supervisor |
| 2026-10-20 | T1.4 | Modal MJD shows 5 decimals (`61 333.33261`); render shows an integer | more useful live; single-space thousands kept | supervisor (user may change) |
| 2026-10-20 | T1.4 | `test_snapshot_from_status_text` compared hour angle unwrapped → failed whenever LMST − RA > 12 h; fixed to compare the wrapped value (code was correct) | clock-dependent test bug from T1.1 | supervisor |
| 2026-10-20 | T1.2 | shadcn `DialogContent`/`DialogScrollContent` props declared explicitly | Nuxt's Vite SFC compiler can't resolve reka-ui's `DialogContentProps` extends → whole page broke when the dialog was imported (browser-only; vitest passed) | supervisor |
| 2026-10-20 | T1.2 | `PW_SECRET_STORE=memory` env for dev/test runs | keeps throwaway runs out of the real Windows Credential Manager | supervisor |
| 2026-10-20 | T1.1 | `useMountSocket`: session fetch time-limited (3 s), socket-open timeout (5 s), backoff cap 5 s | user run-through: page never reconnected after a gateway restart — the Nitro dev proxy hangs (no reply) while its backend is down, and the un-timed fetch stalled reconnection forever. Browser-verified: reconnects ~2 s after restart | supervisor |
| 2026-10-20 | T1.1 | Snapshot `time` (jd/mjd/lmst) computed by the gateway from its clock + site longitude; PWI4's values used only when connected and agreeing (≤ 1 s / 0.01 h). Pointing-derived fields + axis rms/dist are `null` when the mount is disconnected or data stale | PWI4 reports zeros when the mount is disconnected (MJD −2400000.5 seen live) | supervisor |
| 2026-10-20 | T1.1 | Publish ticker decoupled from PWI4 fetch; `pwi4.age_ms`; > 1 s stale → DISCONNECTED "PWI4 not responding"; connect timeout 0.25 s, retry 1 s when unreachable | Windows connect to a closed localhost port waits the full timeout (~0.5 s) → stream fell to ~2 Hz | supervisor |
| 2026-10-20 | T1.1 | WebSocket can't pass Nitro devProxy (HTTP only) → in dev the page connects straight to the gateway (`runtimeConfig.public.gatewayWs`); token fetch stays proxied; prod = same-origin (gateway serves the app) |
| 2026-10-20 | A4-i | **Red warnings** (user): Status red on FAULT; Sun Distance red when ≤ 15° (green > 15°). Implemented in T1.4 |
| 2026-10-20 | G4 / T3.1 | **Start-up stop** (user): on every gateway start, if any motion is active (slewing, tracking, path/TLE follow, offset rate) the gateway stops the mount first — mitigates P1 (no PWI4 watchdog) after a crash/restart |
| 2026-10-19 | T0.1 | Section title tags reduced (text-lg → 13 px, tighter padding) per user review |
| 2026-10-19 | UI kit | shadcn-vue **Lyra** style (user; the React preset code can't be applied to shadcn-vue, so the style is set directly); Tailwind Preflight ON |

### Benchmarks
**O1 — python-sgp4 vs orekit** (2026-10-19, this PC; synthetic TLE set 80 % LEO / 10 % MEO / 10 % GEO; one 30-min chunk at 10 s = 181 samples/satellite; az/el for site 13.786°N 101.464°E)

| Engine | Satellites | Time | Note |
|---|---|---|---|
| python-sgp4 2.25 (SatrecArray) + numpy az/el | 20 000 | **2.2 s** (parse 0.23 + propagate 1.36 + az/el 0.62) | measured |
| python-sgp4 | 2 000 | 0.21 s | measured |
| orekit (orekit310 env), TLEPropagator + TopocentricFrame | 500 | 2.3 s | measured → ~92 s for 20 000 |

Agreement (first 50 satellites × 7 epochs): TEME position diff max 1.4e-7 km; az/el separation median 0.35″, max 3.8″ (simplified GMST rotation, no polar motion) — negligible against the ≈ 3° FOV. Scripts kept outside the repo (session scratchpad); re-create in `tools/bench/` if needed.

### PWI4 behaviour (T0.3 harness, PWI4 4.1.0 Simulator.Mount, runs 20261019T100304Z worker + 20261019T101115Z supervisor)

| # | Finding | Consequence for the build |
|---|---|---|
| P1 | **No watchdog**: a 0.5 °/s axis-rate offset kept running through 10 s of client silence (moved 4.88° vs 4.87° expected) | G4 known limit confirmed. The gateway's own link-loss stop is the only safeguard; T3.1 to decide a gateway-start policy (e.g. stop any motion found at gateway start-up) |
| P2 | STOP halts motion within ~0.12 s (position frozen, 0 drift after 1 s), but `is_slewing` can stay true up to 1.65 s | State machine (T1.1) and I3 judge "stopped" from position/velocity, not the `is_slewing` flag |
| P3 | `is_tracking` reads true while slewing | G8 mapping: SLEWING takes precedence over TRACKING |
| P4 | `goto_ra_dec_j2000` starts tracking by itself (J2000 error < 0.2″); tracking flag flips in ~15 ms | A5c Goto celestial → TRACKING needs no extra command |
| P5 | `follow_tle` argument order is **(name, line1, line2)** (TLE-lines-first is rejected "Unable to parse TLE") | Gateway wraps the client call with named args |
| P6 | `follow_tle` vs our sgp4+skyfield: GEO mean 0.005°, LEO pass (65° peak) mean 0.012°, max 0.038°, time lag 0 s; acquire 10–16 s | O1 engine agrees with PWI4 to < 0.04° (FOV ≈ 3°). Pre-position (A1d) must allow ~15 s acquisition |
| P7 | Custom path: `coord_type` **altaz** (c0 = az, c1 = alt, deg) or **raw** only; points `jd,c0,c1` (UTC JD); > 2 points; tracking mean ≈ 0.01°, max ≈ 0.03° | D-SUN detours and aircraft paths use `altaz` paths |
| P8 | `custom_path/apply` with default `update_wrap` re-sets the axis0 wrap range → up to ~200–360° unwinding slews; `update_wrap=0` stays put. `follow_tle` also changes the wrap range | Gateway **always** passes `update_wrap=0`; reads `axis0_wrap_range_min_degs` + axis position before every move |
| P9 | `goto_alt_az` chooses the axis0 target by the wrap range, not the nearest representation (observed a ~450° / 27 s unwind) | Gateway manages wrap explicitly (set wrap min so the nearest representation is chosen) — T3.1/T3.4; D-SUN timing must use the actual axis path |
| P10 | **Offsets (rate / add_arcsec) only move the mount while it is tracking**; on an idle mount they only change bookkeeping. While tracking: rate exact (0.1668 vs 0.1667 °/s), stop_rate ≈ 0.12 s, 120″ step ≈ 0.23 s | T3.4: jog/step while TRACKING = offsets (sidereal nudge ✓); jog/step while STOPPED needs another mechanism (goto_alt_az increments or a short altaz custom path) — design in T3.4 |
| P11 | `/status` at 10 Hz: mean 18 ms, p95 27 ms, max 30 ms, 0 > 100 ms | 10 Hz poll is comfortable (T1.1) |
| P12 | Simulator only: `find_home` is a no-op (no motion, no homing field); velocity fields always 0 | HOMING detection and velocity fields must be verified on the real mount (Phase 3 user session); gateway derives velocity from position |
| P14 | **PWI4 accepts below-horizon targets**: `goto_ra_dec_j2000` to a target ~11.7 h from the meridian drove the sim mount to its −40° mechanical altitude limit and kept slewing/tracking there (2026-10-20, T1.4 gate) | The gateway's G1 Lim EL check (T3.1) is the only guard — validate every goto/track target before sending, and refuse below Lim EL |
| P13 | Slews approach exponentially (slew time constant 0.5): 90° ≈ 9.5 s, 180°-ish ≈ 13.8 s | D-SUN slew-time model uses vmax/accel + the approach constant |

Harness note: `08_watchdog.explicit_stop_latency_s` is mis-measured in the supervisor run (includes the silent period); conclusion unaffected — fix opportunistically.

### Deviation log
_Differences between the as-built app and the logic sheet / concept renders, with reasons._

| Date | Task | Deviation | Reason | Approved |
|---|---|---|---|---|
| 2026-10-19 | T0.1 | `components.json` style is `reka-lyra`, not `lyra` | shadcn-vue registry only serves `reka-lyra` (`lyra` 404s) | supervisor (naming only) |
| 2026-10-19 | T0.1 | Section title chip rendered as grey fill + dark text; renders show light text in an outlined box | first-pass token sampled the wrong pixel | to fix in T1.3 palette pass |
| 2026-10-19 | T0.1 | 8 lint warnings (`vue/require-default-prop`) left in CLI-generated `components/ui/*` | generated primitives kept as-is | supervisor |
| 2026-10-19 | T0.3 | Extra scenarios: LEO-pass follow_tle comparison, custom path with and without `update_wrap`; watchdog run on a tracking mount | GEO can't show time lag; offsets only act while tracking (P10) | supervisor |
| 2026-10-19 | T0.2 | `dms.ts` `stepBox`: seconds spinner crosses zero in reverse (−0°0′0.1″ ↑ → +0°0′0.0″) | avoids a negative-zero dead step before the chip flips; ported dms tests still pass | supervisor |
| 2026-10-19 | T0.2 | DateTimeField / popover: whole seconds only; `secondsStep`, `compactSeconds`, `stacked` props removed | H1 (seconds precision); stacked layout unused here | supervisor |
| 2026-10-19 | T0.2 | Typing `-` over a fully selected signed value sets the chip negative and replaces the value (not toggle) | parity with legacy in-field sign typing; toggle-only when not all selected | supervisor |
| 2026-10-19 | T0.2 | Angle/number fields commit on blur, not on Enter | legacy parity; IOD row-level Enter (A2b-ii) handled at row level in T5.3 | supervisor |
| 2026-10-19 | T0.1 | No `robots: false` routeRule for `/dev/*` | fails typecheck without a robots module; page is unlinked | supervisor |

### Baselines
| Item | Value |
|---|---|
| ssa-toolkit HEAD at plan approval | `84f2ded` (re-confirmed clean at T0.1 start) |
| pw-mount-control baseline commit | `4e72ee0` (2026-10-19) |

### Session log
| Date | Session | Summary |
|---|---|---|
| 2026-10-19 | Planning | Concepts reviewed; logic sheet Rev 2 + HTML; plan drafted, reviewed, O1 benchmarked, approved |
| 2026-10-20 | T1.4 | 3D view (scope-pointer port, three 0.169.0) + Details modal + red rules by worker. Supervisor gate vs live PWI4-sim driven by `tools/pwi4_harness/simdrv.py` (sim-guarded): panel 180.00/+45.00 STOPPED matches PWI4; modal MJD/LST/RA/Dec/HA/axes match PWI4 within the ~2 s sampling gap; SLEWING + target marker mid-slew; FAULT red (axis 0 disabled, tooltip "axis 0 disabled"); 5° from the Sun → Sun Distance 4.88° red + red arrow; TRACKING green; modal open/close ×10 → no WebGL warnings, 1 canvas left; idle STOPPED → `data-frames` flat; T1.2 mismatch warning live ("Δlat +1.0000°…"). Found P14 (PWI4 accepts below-horizon targets). Fixed a clock-dependent T1.1 test. Sim mount left disconnected |
| 2026-10-20 | T1.3 | Telemetry store + MOUNT STATUS panel + palette (chips now outlined light text) by worker; supervisor: web gates (vitest 225), screenshots 1920/1280 vs render (TRACKING green, Sun green, stale → dimmed — + DISCONNECTED, recovery), removed the unused `useMountSocket` wrapper. Live PWI4-sim comparison deferred to T1.4 (PWI4 not running) |
| 2026-10-20 | T1.2 | User: settings OK; UI sizing/alignment tweaks to come later |
| 2026-10-20 | T1.2 | Settings by worker; gate found dialog ~1870 px wide (unlayered CSS beat Tailwind max-w) + UTC-means-unset heuristic → fixed (1000 px modal, fixed field widths, visible separator; first-run `timezone: null`). Supervisor: Chrome round trip (DMS/FT/Bangkok/LimEL persisted, cancel discards), key absent from DOM/file/responses, 401/403/422, snapshot site=settings with Sun while PWI4 down, KeyringSecretStore live vs Windows Credential Manager (throwaway entry); pytest 84, vitest 212 |
| 2026-10-20 | T1.1 | User run-through on /dev/mount passed (8 checks) after the reconnect fix |
| 2026-10-20 | T1.1 | Gateway core by worker; supervisor gate found 2 issues (≈2 Hz stream when PWI4 down; bogus time/pointing when mount disconnected), both fixed + tested. Final: pytest 50 pass/13 skip, ruff, web gates; security probes (Host 400, session 403, WS Origin 403, auth 4401, token ok); PWI4-sim 9.98 Hz, STOPPED→SLEWING→STOPPED, STOPPED 0.13 s after STOP; PWI4 HTTP log only `/status`; time vs skyfield 0.09 s |
| 2026-10-19 | Wrap-up | Browser-gate driver + O1 bench scripts moved into `tools/`; `docs/HANDOFF.md` written; Phase 0 committed at the user's request |
| 2026-10-19 | Phase 0 | User check: widgets approved |
| 2026-10-19 | T0.3 | PWI4-sim harness by worker (+ LEO pass and `update_wrap` variants). Supervisor: pytest 8 pass/11 skip, ruff clean, guard refuses real-mount + missing cfg (exit 2, no command sent), own full run 10/10 ok, mount left disconnected + axes disabled. Findings P1–P13 recorded |
| 2026-10-19 | T0.2 | Widgets ported by worker (199 vitest). Supervisor browser gate (CDP, real keys/mouse) found 2 bugs the happy-dom tests missed: '-' over selection appended digits; picker Next re-seeded to the date pane (watcher on a fresh `seedFields` object). Fixed with regression tests; re-verified in Chrome. Note: my first run mis-read Enter/Arrow results — test driver lacked a key handler; fixed and re-run |
| 2026-10-19 | T0.1 | Scaffold by Sonnet worker; supervisor gate re-run: lint (0 err/8 warn), typecheck, generate, vitest, pytest, ruff, health direct + via proxy on 18401/18411/13201, loopback-only binds, Barlow loaded, shell screenshot ≈ Main Dashboard 1 layout, I1 clean @84f2ded, I9 clean, all throwaway servers killed |

---

### T1.5 log (2026-10-20)
- User Phase 1 functional check passed; UI review list A1–A5c + B approved (Lim EL stays 90 px — user).
- Worker implemented; supervisor re-measured in headless Chrome: every Settings control at the spec'd x/width (label 0/100, fmt 100/60, separator 162, value 165/162 in DD/DMS/M/FT, full span 100/227, Lim EL 100/90), columns 361/361, modal 788. Panel 1920×1080: 20/100×32/14/16/65 (unchanged); 1280×720 @1.5: 14/80×26/11/12/45.
- Deviation: the panel at 1280 CSS px is 258 px wide (spec estimated ~290), so the floors apply there.
- Gates: lint 0 errors, typecheck 0, vitest 245/245. `tools/browser_gate/cdp.mjs` `size` step gained an optional DPR (`[w, h, dpr]`).
- Follow-up review (user, same day), done by supervisor: (1) Providers rows now sit on the Location row pitch (Satellite DB level with Altitude, 4th row blank; both fieldsets 182 px) — the "clear key" link moved inside the API key box (left, the key text is right-aligned), shown only while a key is set and the box is empty; (2) all dropdowns (`FormatSelect`, `TimezoneSelect`) wrap the value in `.fmt-select-text` (shrinkable flex child with ellipsis), so long values stop at the 20 px right padding instead of running under the triangle (measured: long timezone ends 21 px from the box edge). Gates re-run: lint 0 errors, typecheck 0, vitest 245/245.
- **Open item O-SITE (user, 10-20): Details-modal LST / HA differ from PWI4's.** Diagnosed: not J2000 drift (HA = LMST − *apparent* RA, the same RA PWI4 uses) but the **site mismatch**. LMST comes from the Settings longitude (E 101.4639°) vs PWI4's site (E 100.5656°): Δ 0.898° = 3 m 36 s of sidereal time. PWI4's LMST is only adopted when within 0.01 h, so it is rejected and both LST and HA are offset by 3 m 36 s. Minor, separate: HA uses LMST with apparent RA (strictly LAST; equation of the equinoxes ≤ ~1 s). **Resolve before Phase 3:** decide one authoritative site (the user aligns PWI4's site and Settings by hand, since we never write PWI4 settings). Options to decide then: block motion commands while the site check fails; show a mismatch badge on the dashboard/modal; switch HA to LAST. The Sky Viewer (T2.x) draws with the Settings site, so keep test data consistent.
- Incident: the worker ran `taskkill /F /IM chrome.exe` to clear its stuck headless Chrome, which also closes any user Chrome windows. Future briefs: kill by PID only.

## 9. Known limitations & risks

- **Gateway crash (G4 note):** nothing can send a stop if the gateway process dies; mitigated by a UI warning that hands the user off to PWI4.
- **Sky Viewer scale:** "no item limit" is the main performance risk; the O1 benchmark puts the propagation side at ~2 s per 30-min chunk for 20k objects, so the remaining risk is rendering (T2.2/T2.3 measure it).
- **D-SUN** is novel logic driving hardware; it ships only after planner unit tests + PWI4-sim runs + a supervised real-mount test.
- **Aircraft / ADS-B** is deferred; UI is present but disabled.
