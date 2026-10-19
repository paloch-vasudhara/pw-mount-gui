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
| T3.1 | Command protocol (ids/acks), control lock, audit log, link-loss stop (G4), safety engine v1 = **refuse/stop** at Lim EL & Sun (G1–G3) | I3, I4 tests incl. forged commands; audit entries |
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

---

## 7. Progress tracker

Status: ⬜ not started · 🟦 design · 🟨 implementing · 🟪 verifying · ✅ done · ⏸ paused/partial

| Task | Status | Design | Impl | Verified | User check | Notes |
|---|---|---|---|---|---|---|
| T0.1 | ✅ | ✅ 10-19 | ✅ 10-19 | ✅ 10-19 | — (phase-end) | `reka-lyra`; ports 3200/8400/8410; title-chip colours → T1.3 |
| T0.2 | ✅ | ✅ 10-19 | ✅ 10-19 | ✅ 10-19 | phase-end | 2 browser bugs found at gate, fixed + regression-tested |
| T0.3 | ✅ | ✅ 10-19 | ✅ 10-19 | ✅ 10-19 | ✅ (Phase 0 approved) | findings → §8 PWI4 behaviour |
| T1.1 | ⬜ | | | | | |
| T1.2 | ⬜ | | | | | |
| T1.3 | ⬜ | | | | | |
| T1.4 | ⬜ | | | | | |
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

**Resume point:** Phase 0 complete and committed. Next **T1.1** (gateway core) — start from `docs/HANDOFF.md`.

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
| 2026-10-19 | Wrap-up | Browser-gate driver + O1 bench scripts moved into `tools/`; `docs/HANDOFF.md` written; Phase 0 committed at the user's request |
| 2026-10-19 | Phase 0 | User check: widgets approved |
| 2026-10-19 | T0.3 | PWI4-sim harness by worker (+ LEO pass and `update_wrap` variants). Supervisor: pytest 8 pass/11 skip, ruff clean, guard refuses real-mount + missing cfg (exit 2, no command sent), own full run 10/10 ok, mount left disconnected + axes disabled. Findings P1–P13 recorded |
| 2026-10-19 | T0.2 | Widgets ported by worker (199 vitest). Supervisor browser gate (CDP, real keys/mouse) found 2 bugs the happy-dom tests missed: '-' over selection appended digits; picker Next re-seeded to the date pane (watcher on a fresh `seedFields` object). Fixed with regression tests; re-verified in Chrome. Note: my first run mis-read Enter/Arrow results — test driver lacked a key handler; fixed and re-run |
| 2026-10-19 | T0.1 | Scaffold by Sonnet worker; supervisor gate re-run: lint (0 err/8 warn), typecheck, generate, vitest, pytest, ruff, health direct + via proxy on 18401/18411/13201, loopback-only binds, Barlow loaded, shell screenshot ≈ Main Dashboard 1 layout, I1 clean @84f2ded, I9 clean, all throwaway servers killed |

---

## 9. Known limitations & risks

- **Gateway crash (G4 note):** nothing can send a stop if the gateway process dies; mitigated by a UI warning that hands the user off to PWI4.
- **Sky Viewer scale:** "no item limit" is the main performance risk; the O1 benchmark puts the propagation side at ~2 s per 30-min chunk for 20k objects, so the remaining risk is rendering (T2.2/T2.3 measure it).
- **D-SUN** is novel logic driving hardware; it ships only after planner unit tests + PWI4-sim runs + a supervised real-mount test.
- **Aircraft / ADS-B** is deferred; UI is present but disabled.
