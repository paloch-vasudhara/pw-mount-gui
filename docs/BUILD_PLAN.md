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

**Test tiers (user-approved 2026-10-27; replaces "full gates on every sub-task"):**
- **Why:** T3.3b took a whole 5 h session. The cause was not long tests but repetition: the full sim suite after every change and ×2 at the end, chasing flaky tests in untouched areas, open-ended measuring, three jobs in one brief, the supervisor repeating a full run, and the report only at the end.
- **Tier 1, each sub-task (worker).** The brief lists the exact commands.
  - Unit tests: the test files of the touched modules while working, then the full non-sim pytest (~3 min) **once** at the end.
  - Sim: **only the new or changed sim tests, once.** A second run only if a timing result is within 20 % of its limit. **Never the full sim suite.**
  - Web: typecheck + lint + the touched files' vitest.
  - **A flaky test outside the task's area:** note it, don't debug it. It goes on the flaky list in §8.
  - **Measurement work:** 30–45 min timebox, then report the best result.
  - **Brief timebox ~90 min:** stop and report at the limit.
  - **Progress notes** go to `scratchpad/<task>/report.md` as the worker goes (survives crashes / usage limits).
- **Tier 2, each sub-task (supervisor, ~20–30 min):**
  - review the diff against the spec;
  - re-run the task's new or changed sim tests once;
  - a small independent spot-check script for the task's key numbers;
  - a real browser **only if the UI changed** (one scripted pass at 1920 and 1280×600 @1.5);
  - no full independent sim run.
- **Tier 3, end of a main phase (T3, T4, …):**
  - full sim suite ×2, the PWI4 harness, a full browser pass of the phase's screens;
  - the flaky list is fixed or tightened here, slow sim tests are trimmed here;
  - then the user's phase check.
- **Sizing:** split mixed sub-tasks into separate small briefs (e.g. b1 / b2 / b3) so one stuck part doesn't hold the others.
- **Sim-test areas (name the rows in a brief):**

| Area | Sim test file |
|---|---|
| Tracking / TRACK / H4 vs PWI4 | `gateway/tests/test_track_pwi4sim.py` |
| Safety (Lim EL, Sun, monitor, refraction frame) | `gateway/tests/test_safety_pwi4sim.py` |
| Sequences (connect / disconnect / home, homed guard) | `gateway/tests/test_sequences_pwi4sim.py` |
| Control (lock, link loss, start-up stop, protocol) | `gateway/tests/test_control_pwi4sim.py` |
| Stall guard | `gateway/tests/test_stall_pwi4sim.py` |
| Poller / state machine | `gateway/tests/test_gateway_pwi4sim.py` |
| Harness / sim_guard | `tools/pwi4_harness/tests/` |

- **Flaky list** (fix at the phase end): **empty after the Phase 4 end (10-29)**; both entries below fixed (see the "Phase 4 end, Tier 3" log). History: `test_jog.py::test_keepalive_timeout_stops_a_stopped_origin_jog` (0.297 s vs a 0.3 s floor once under parallel load, T4.2b 10-28; passes alone); `test_track_exec.py::test_pass_sequence_phases_snapshot_monitor_audit_end` (failed once in a combined track_exec + jog run 10-29, then 6/6 + 10/10 green). Fixed 10-29: `test_late_arrival_replans_once_with_a_short_goto` (the fake mount's park jump read as a fast descent by the monitor → 'Cancelled: Below station limit elevation'; the test now waits for a still mount; 10/10).

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
| T4.3 | Card pinning/stacking rules (A1e) + Track Target modal (A5c-ii): 500-cap search, latest-500 default, filters, aircraft tab disabled; **stale-TLE badge** (TLE age > 30 d, user 10-21) on search results and on the Track Info card, also used by Pass Analysis results (T5.1) | A1e scenario table walked in e2e |

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

### T2.1 — Star data + Sky Viewer chart canvas + clock (spec, 2026-10-21)

**Goal:** the SKY VIEWER panel (A3, A3a; Main Dashboard 1.png) shows the live local sky: grid, cardinals, stars, Lim EL ring with dimming, Sun/Moon, the mount pointing circle, and the UTC/Local clock. No satellites (T2.2), no filters/picking (T2.3).

**Star data (O5):** `tools/build_star_data.py [--vmag 6.5]` reads `data/hip_main.txt` (git-ignored; pipe-separated: H1 HIP, H5 Vmag, H8/H9 RA/Dec deg ICRS at J1991.25, H12/H13 pmRA·cosδ / pmDE mas/yr). It skips rows with no position or no Vmag, keeps V ≤ cutoff, and applies proper motion linearly to J2000.0. It writes `web/public/data/stars.json` (**committed**, since the catalogue isn't): `{"source":"Hipparcos (ESA 1997)","vmag_max":6.5,"epoch":"J2000","count":N,"stars":[[ra_deg, dec_deg, vmag], ...]}`, rounded to 4 dp RA/Dec and 2 dp V, sorted by V (brightest first). Changing the cutoff = re-run the script. Unit test for the parser on a few fixture lines.

**Gateway (only change):** add `site.timezone` (the Settings IANA zone, `null` = PC zone) to the snapshot so the clock can show Local Time. Read path only, tests updated.

**Sky maths (`web/app/utils/sky-chart.ts`, pure, unit-tested):**
- `projXY(az, el, cx, cy, R)`: azimuthal equidistant, `r = R·(1 − el/90)`, **N up, E left, W right**. Same formula as ssa-toolkit `utils/observer-hemisphere.ts` `projXY` @84f2ded, parameterised by size.
- LMST from the client clock + Settings longitude (the same IAU 1982 GMST as `gateway/snapshot.py` `lmst_hours_from`), so the chart keeps running between telemetry messages.
- Precess J2000 → date (IAU 1976 precession angles ζ, z, θ), then equatorial → alt/az for the site latitude.
  - No nutation, aberration or refraction: the error is < 0.02° vs skyfield apparent positions above 10° elevation, invisible at chart scale.
  - Vectorised over `Float64Array`s, no per-star allocation.
- Tests: alt/az of 5 stars (Polaris, Vega, Sirius, Canopus, Altair) at a fixed UTC + site, against skyfield values the worker generates once into a fixture (tolerance 0.05°). Projection tests: N top, E left, zenith centre, horizon at R.

**Component (`components/sky/SkyChart.vue` + `SkyClock.vue`, in the SKY VIEWER panel of `pages/index.vue`):**
- Square chart, the largest that fits the panel above the clock; DPR-aware canvases; redraws on resize (ResizeObserver).
- **Layered canvases, render on demand:**
  - `base`: disc, grid, cardinals, ring labels, Lim EL ring. Redrawn on resize or a Lim EL change.
  - `stars`: recomputed + redrawn at **1 Hz** and on resize / site change.
  - `overlay`: Sun, Moon, Sun keep-out outline, mount circle. Redrawn when those values change.
  - T2.2 will add one canvas per satellite class above `stars`.
  - Expose per-layer frame counts as `data-frames-base/stars/overlay` on the wrapper.
- **Look (match the render):**
  - Dark navy sky disc (no daylight tint); outer horizon ring light grey.
  - Elevation rings every 15° with `15°…75°` labels along az 22.5°.
  - N/S/E/W axes and diagonal lines; 8 cardinal labels outside the rim (N, NE, E, SE, S, SW, W, NW) in the panel's Barlow.
  - **Stars:** white dots, size and alpha by magnitude, **no labels** (A3a overrides the render).
- **Lim EL:** dashed ring at `limits.lim_el_deg`. Stars between the horizon and Lim EL are drawn dimmed (alpha × 0.35). Below the horizon: not drawn.
- **Sun / Moon** from the snapshot (`sun`, `moon` + `illumination`):
  - Sun: an amber disc.
  - Moon: a grey disc with a lit fraction.
  - Sun keep-out: the 15° circle (`limits.sun_keepout_deg`) around the Sun, drawn as a thin red dashed outline, sampled as a small circle in the projection (it is not a circle on the chart).
  - Not drawn below the horizon.
- **Mount pointing:** the "blue separated circle" (inner dot + gapped ring, accent blue) at the snapshot `mount.az/alt`. Hidden when null or the link is stale.
- **Props for T2.3's filters:** `showStars` and `showBodies` (default true).
- **No data yet / link stale:**
  - Stars keep running from the last-known site.
  - Sun/Moon and pointing hide while the link is stale.
  - Before any site is known: grid only, plus a muted "Waiting for gateway" note.
- **Clock (`SkyClock.vue`, A3):** `YYYY-MM-DD | HH : MM : SS` (time bold), PC clock, 1 Hz. A small clickable tag under it, `Time UTC` ↔ `Local Time`.
  - Local Time uses `site.timezone`, or the PC zone when that is null.
  - The choice is remembered per viewer in localStorage, wrapped in try/catch.

**Must not touch:** `pwi4/`, ssa-toolkit (read/copy only), `docs/`, other panels, PWI4 writes (gateway stays `GET /status`-only). No commits.

**Gate (supervisor):**
- Gates: web lint/typecheck/vitest; pytest + ruff.
- Star data:
  - the count matches an independent count of `hip_main.txt` at V ≤ 6.5;
  - a re-run with `--vmag 5` produces fewer stars.
- **Real browser vs live throwaway gateway (+ PWI4-sim):**
  - star pixel positions of bright stars match skyfield alt/az for the Settings site (≤ 1 px at 1920);
  - E left;
  - the pointing circle follows a sim goto;
  - Sun/Moon placed per the snapshot;
  - the Lim EL ring moves when Lim EL changes;
  - the stars layer redraws at about 1 Hz and the base layer stays flat;
  - the clock toggle works and survives a reload.
- Screenshots at 1920 and 1280 @1.5 vs Main Dashboard 1.png.

### T2.1b — User review polish: sky colour, pointing marker, Settings save flow (spec, 2026-10-21)

**1. Sky colour follows the Sun (SkyChart base + stars):**
- Disc colour from the Sun's altitude, using the ssa-toolkit `skyColor` stop table (`utils/observer-hemisphere.ts` @84f2ded): 5° `[235,242,255]`, 0° `[220,140,100]`, −6° `[40,30,70]`, −12° `[13,17,23]`.
  - The **daytime end is bluer** (user), e.g. `[170,205,245]` at ≥ 20° and `[205,225,250]` at 5°. The stop table lives in `lib/sky/theme.ts` so it is easy to tune.
  - Night keeps today's dark navy.
- Sun altitude: computed client-side from the site + clock (port ssa `sunElevationApprox`), so it works while the link is stale. Quantise to 0.5°; the base layer redraws only when the colour changes.
- Grid, ring labels and the Lim EL ring stay readable on a light disc: their colours interpolate toward darker tones as the sky gets brighter.
- **Stars fade with the brightening sky:**
  - limiting magnitude ≈ 6.5 at Sun ≤ −18°, ≈ 4 at −6°, ≈ 1 at 0°;
  - no stars at Sun ≥ +5°;
  - plus an alpha factor that falls with sky brightness.
  - Unit-test the colour and limiting-magnitude functions.

**2. Pointing marker:** replace the ring + centre dot with a **broken ring**: 4 arcs with gaps at N/E/S/W, no centre dot, so whatever is being tracked stays visible inside. Same blue, at about today's ring radius, slightly heavier line.

**3. Station Settings save flow (T1.2 follow-up):**
- **SAVE SETTINGS is disabled when nothing changed** compared with the loaded settings. A typed API key or "clear key" counts as a change.
- **The warning never replaces the buttons.** CANCEL and SAVE SETTINGS stay in every state; CLOSE is gone.
- **Mismatch flow:** clicking SAVE first checks the *proposed* location against PWI4 **without saving**.
  - Mismatch → show the warning and save nothing.
  - Any edit to a Location field (lat, lon, alt, and their formats) clears the warning and the acknowledgement.
  - Clicking SAVE again with the warning showing accepts it: PUT, then close.
  - No mismatch, or PWI4 unreachable → PUT and close at once.
  - The check runs whenever the proposed location mismatches PWI4, whichever field changed.
- **Gateway:** new `POST /api/mount/site-check` (auth `X-Mount-Token`).
  - Body: a Location `{lat_deg, lon_deg, alt_m}`.
  - Returns `compute_site_check(proposed, pwi4_site)`.
  - Read-only: no settings write, no PWI4 call beyond the poller's cached `/status` site.
  - Bad input → 422.
- Save errors (422/network) show under the form; the buttons stay as they are.

**Must not touch:** `pwi4/`, ssa-toolkit, `docs/`. No commits.

**Gate (supervisor):**
- Code checks: web and Python test suites.
- **Sky colour:** browser screenshots with injected/clock-shifted Sun altitudes (+30°, +2°, −4°, −10°, −20°). Night is unchanged from T2.1; daytime is blue with no stars; twilight is graded.
- **Marker:** the broken ring, with the centre clear.
- **Settings flow:** unchanged form → SAVE disabled. Mismatching edit → SAVE shows the warning and the file is unchanged. Editing lat clears the warning. SAVE twice → saved and closed. CANCEL always present.
- **New endpoint:** the gateway's PWI4 request log still shows only `GET /status`.

### T2.2 — Satellite layers (spec, 2026-10-21; user decisions same day)

**User decisions (10-21):**
- **DB:** the satellite DB is reached through `POSTGRESQL_DSN` in the repo-root `.env` (git-ignored; same variable as ssa-toolkit). It points to the local DB now and to a server DB later. The Settings "Satellite DB" dropdown will later select between servers: provider id → DSN variable, with `DELV Aerospace` → `POSTGRESQL_DSN` for now.
- **Live viewer scope:** **active payloads only** (`object_type = 'PAYLOAD' AND is_active AND decay_epoch IS NULL`). Other object types stay reachable through search (Track Target / Pass Analysis) but are never drawn as live tracks.
- **Hardware:** this PC is the mount PC, so timing is measured here.
- **Hard requirements:**
  - the first load (all classes visible) populates quickly;
  - the calculation never stalls mount operations, monitoring or commands.

**Rules for all T2.2 work:**
- The DSN is a secret: never print, log, return or commit it, and never read it outside the code path that connects.
- DB access is **read-only** (SELECT only).

#### T2.2a — Analysis backend (`analysis/`)

- **Catalogue (`analysis/catalog.py`):**
  - SQLAlchemy Core + psycopg2, read-only SELECTs on `satellites`.
  - Load `.env` with python-dotenv (repo root); a missing DSN gives a clear 503.
  - Live set = active payloads (decision above) mapped to layers: **LEO** = LEO + SSO, **MEO** = MEO, **GEO** = GEO + HEO; UNKNOWN excluded.
  - Hold the TLEs per layer in memory as an sgp4 `SatrecArray`. Refresh every 6 h (and on demand via an endpoint) without blocking requests.
  - Log counts only.
- **Search (`GET /api/sats/search?q=&orbit_class=&object_type=&limit=500`):**
  - Any object type, never decayed (G6/I8).
  - With no `q`, returns the **latest 500** (highest NORAD id first).
  - Returns `norad, name, cospar, country, object_type, orbit_class, tle_epoch`.
  - `GET /api/sats/{norad}` returns the same fields plus the TLE lines, or 404 when decayed.
- **Sky chunk (`POST /api/sky/chunk`):**
  - Body: `{layer: "leo"|"meo"|"geo", site: {lat, lon, alt_m}, t0_unix, duration_s}`.
  - Steps: LEO 10 s, MEO 60 s, GEO 300 s.
  - `t0` is snapped down to the step grid; `duration_s` is 1800 for a full chunk, or 120 for the quick **head** chunk.
  - Vectorised SGP4 (`SatrecArray.sgp4` over all objects × all times) → TEME → ECEF (GMST rotation) → topocentric az/el for the site, all numpy. No orekit/Java (I9).
  - **Only above-horizon spans are returned** (alt > −2°, plus one sample of margin either side), so LEO data shrinks about 20×.
  - **Binary response** (`application/octet-stream`), documented in the module docstring:
    - a little-endian `uint32` header length, then a JSON header `{layer, t0_unix, step_s, n_steps, norad:[...]}`;
    - then typed arrays: segments `Int32[obj_idx, start_step, n]` and samples `Float32[az, alt]`.
    - The client decodes it without per-sample objects.
  - A server-side cache keyed by (layer, site rounded to 1e-4°, t0, duration) keeps recent chunks for 10 min, so tabs and repeats don't recompute.
- **`GET /api/sky/objects?layer=`:** `[norad, name, cospar, country, orbit_class]` for the live set (T2.3 uses it for labels/cards).
- **Never stall anything (I5):**
  - Chunks are computed in a `ProcessPoolExecutor(max_workers=1)` that owns its own catalogue copy, so the API loop stays free.
  - On Windows the analysis process and its pool run at **BELOW_NORMAL priority** (ctypes `SetPriorityClass`), so the gateway always wins the CPU.
  - `/api/health` must answer in < 100 ms during a chunk computation.
- **Tests:**
  - A fixture catalogue (a handful of TLEs: ISS-like LEO, an SSO, a GPS MEO, two GEO, one HEO, plus a decayed row and a non-payload row) behind the same catalogue interface. Use SQLite via SQLAlchemy, or an in-memory fake; no real DB in automated tests.
  - Chunk az/el vs skyfield `EarthSatellite` topocentric alt/az at 5 instants: ≤ 0.02° (LEO) / 0.01° (MEO/GEO).
  - Decayed and non-payload rows never appear in a chunk or `objects` (I8). Search excludes decayed but includes non-payloads.
  - Binary round-trip.
  - Above-horizon trimming correct at the rim.
  - Health latency while a chunk runs.

**Gate T2.2a (supervisor), against the real local DB (read-only):**
- Unit tests, pytest and ruff.
- Live payload counts per layer.
- **Timings on this PC:**
  - cold catalogue load;
  - head chunk and full chunk per layer;
  - all three full chunks.
  - Targets: **full chunks for all three layers ≤ 5 s warm**, **head chunks ≤ 1 s**.
- Response sizes.
- Positions of a few real objects vs skyfield.
- No decayed object in any output (I8), checked against the DB.
- **I5:** gateway publish rate stays ≥ 9.5 Hz and `/api/mount/status` p95 < 50 ms while the three full chunks compute.

#### T2.2b — Web satellite layers

- **Cache (`stores/sky.ts` + pure `utils/sky-chunks.ts`)**, per layer:
  - first fetch the **head** chunk (120 s) and draw at once, then the full 30-min chunk;
  - when coverage left < 10 min, fetch the next chunk in the background;
  - evict chunks whose end < now;
  - flush and refetch on a site change (> 1e-4°).
  - AbortController on flush; retry with backoff (max 30 s) on errors.
  - Hidden layers are still fetched (A3a).
  - The scheduler is a pure, unit-tested function, tested with fake timers.
- **Drawing (in `SkyChart.vue`):**
  - Three canvases (LEO, MEO, GEO) between `stars` and `overlay`.
  - Redraw cadence LEO 1 s, MEO 5 s, GEO 20 s, staggered so they don't land on the same frame.
  - Position = interpolation between the bracketing samples in unit-vector space (handles az wrap).
  - Objects not in a segment at time t are not drawn.
  - Colours: port ssa-toolkit `utils/orbit-class.ts` `orbitClassColor` @84f2ded.
  - Dot about 2 px; dimmed (× 0.35) below Lim EL. Satellites are **not** faded by daylight (they are data, not optical).
  - No labels/hover (T2.3).
  - Props `showLeo/showMeo/showGeo` (default true) for T2.3.
  - Gate hooks: `data-frames-leo/meo/geo`, `data-sats-leo/meo/geo` (drawn counts).
  - No per-frame allocation; chunk decode happens once.
- **Main-thread budget:** no long task > 50 ms in steady state, and a chunk decode ≤ 30 ms. If a full LEO decode exceeds that, decode in a Web Worker.

**Gate T2.2b (supervisor):**
- Web gates and unit tests.
- **Real browser vs throwaway gateway + analysis + the local DB:**
  - time from page load to first satellites drawn: **target ≤ 3 s** with a warm catalogue; report cold too;
  - drawn positions of a few real objects vs skyfield ≤ 1.5 px at 1920;
  - long tasks measured with PerformanceObserver;
  - frame cadences per layer;
  - the next-chunk fetch happens at about 10 min remaining (fake-timer unit test, plus one observed request with a clock-offset test hook);
  - I5 re-checked with the browser running: gateway 10 Hz and the dashboard Mount Status still updating smoothly.
- Screenshots vs Main Dashboard 2.png (satellites only, no labels yet).

### T2.3 — Filters, hover/pick, selection/focus, labels, Track Info card (read-only) (spec, 2026-10-21)

**Goal:** the Sky Viewer becomes interactive (A2a, A3a) and the TRACK INFO panel shows the selected satellite's card (A1a, A1c) with live 1 Hz values. Tracking itself is Phase 4: the TRACK OBJECT button is shown but disabled.

**Reference:** ssa-toolkit (read/copy only, @84f2ded):
- `components/panels/observer/ObserverHemisphereCanvas.vue`: tooltip content, selection ring, focus dim 0.22, labels;
- `assets/css/observer-hemisphere.css`;
- `components/common/FlagBadge.vue`.
- Renders: `concept/Main Dashboard 1.png` / `2.png` (filters, labels, selection ring) and `Track Cards.png`.

**1. Filters panel (A2a)**, `components/filters/VisibilityFilters.vue` in the FILTERS panel above Tools:
- **Tree:**
  - **Satellites** › Low Earth Satellites (LEO+SSO), Medium Earth Satellites (MEO), Geosynchronous Satellites (GEO+HEO);
  - **Aircrafts**: present, disabled, tooltip "No ADS-B source configured";
  - **Celestial Objects** › Planetary Bodies (Sun, Moon), Stars.
- **Parent semantics:** unchecking a parent hides all its children but **keeps the children's own checks**; children render greyed while the parent is off. Child visible = parent on AND child on.
- Defaults: everything on (except the disabled Aircrafts). Persisted per viewer in localStorage (try/catch).
- Drives SkyChart's `showLeo/showMeo/showGeo/showStars/showBodies`.
- Hidden layers keep being fetched (A3a); only drawing stops.
- Pure helper `utils/filters.ts` with unit tests for the semantics.

**2. Hover — nearest object (A3a change vs ssa):**
- On pointer move over the chart, the hovered object is the **closest drawn marker** to the pointer within a pick radius (about 10 px, scaled), among visible layers and above the horizon. It is never the topmost one.
- Labels are not hit targets, so **hovering a label does nothing**.
- Implementation: each layer draw stores its screen positions + object indices in reusable typed arrays; picking scans them (O(n), no allocation).
- Cursor: pointer when something is hovered.
- **Tooltip** (ssa format), following the pointer and kept inside the chart:
  - first line: **name** (NORAD);
  - second line: `Az x.xx° El ±x.xx° | range km | class | ● Illuminated/Umbra`;
  - plus **`TLE age N.N d`**, added for the user's fast-mover investigation (O-FAST).
  - Az/El come from the chunk at once; range, illumination and TLE age come from the state endpoint (item 6), cached per NORAD for 1 s.

**3. Selection / focus / deselect (A3a):**
- **Select:** click → select the nearest hovered object. Click anywhere that is not an object → deselect. The card × also deselects.
- **Focused mode:** the selected object gets a **white ring** (render) and its label is always drawn, bold. All other satellites dim to **0.22** (ssa `.ov-sat.dim`). Stars and bodies are unchanged. No path is drawn (lightweight).
- **Selection state** lives in a Pinia store `stores/selection.ts` (`selectedNorad` + metadata), so Track Info and the chart share it.
- **Persistence:** the selection survives chunk refreshes. If the object's layer is hidden or it sets, the selection stays (no ring drawn); the card stays.

**4. Labels (ssa labelling):**
- Name drawn to the right of the marker (ssa: bold small font, white with a dark halo).
- To keep hundreds of LEO labels readable, labels are **decluttered greedily** each draw. Priority: selected > hovered > GEO > MEO > LEO. A label whose box overlaps an already-placed label (or a placed marker) is skipped.
- Label widths are measured once per object (cache).
- Labels draw on their own canvas at the LEO cadence; budget ≤ 3 ms per draw at 1920.
- Labels are never hit targets (item 2).

**5. Track Info card (A1a, A1c)**, `components/track/TrackCard.vue` in the TRACK INFO panel:
- Nothing selected → centred muted "No object selected".
- Card per `Track Cards.png`:
  - satellite icon, **name + flag** (port `FlagBadge`; DB `country_code`);
  - `COSPAR (NORAD)` + a **class chip** coloured per the ssa `ORBIT_CLASS_COLOR` with the DB orbit class (LEO/SSO/MEO/GEO/HEO);
  - × button; separator;
  - rows: Azimuth `x.xx°`, Elevation signed `±x.xx°`, Range `x xxx km` (`.0f`, single-space thousands), Illumination `● Illuminated` (yellow) / `● Umbra` (grey) / `● Below Horizon` (grey, when alt < 0);
  - values refresh at **1 Hz** from the state endpoint.
- **TRACK OBJECT** button: full width, rendered in the disabled (grey) style, tooltip "Object tracking arrives in Phase 4".
- Aircraft card (A1b): not built until ADS-B exists.
- Card states/stacking (A1e) come with tracking in Phase 4.

**6. Analysis: `POST /api/sky/state`:**
- Body `{norad, site:{lat,lon,alt_m}, t_unix?}`. Returns `{norad, name, cospar, country, object_type, orbit_class, az, alt, range_km, illuminated, tle_epoch, tle_age_days}`.
- Any non-decayed object, so it also serves Phase 4/5 search results. Decayed or unknown → 404.
- Computed in the main process (one object; must answer in < 20 ms). skyfield `EarthSatellite` + `is_sunlit` with the local `data/ephemeris/de421.bsp`; az/alt geometric (same convention as the chunks).
- Tests: values vs a direct skyfield computation; 404 for decayed (fixture).

**7. Stale-TLE cutoff for the live viewer (user decision 10-21, O-FAST):**
- The live set (sky layers, chunks, `/api/sky/objects`) additionally requires `tle_epoch >= now − 30 days`.
- Constant `STALE_TLE_DAYS = 30` in the analysis config; evaluated at each catalogue (re)load.
- Search, `GET /api/sats/{norad}` and `/api/sky/state` are **not** filtered (stale objects stay searchable and selectable); they return `tle_age_days` so Phase 4/5 can badge them.
- Tests: a fixture row with an old epoch is absent from chunks/objects but present in search.

**Must not touch:** `pwi4/`, ssa-toolkit, `docs/`, the gateway (no change needed). No commits.

**Gate (supervisor):**
- Python and web gates; unit tests.
- **Stale cutoff:** the four O-FAST outliers (63490, 51844, 68224, 51657) and every live-set object with TLE age > 30 d are absent from all chunks/objects, and still found by search.
- **Real browser, throwaway servers + the local DB:**
  - filter semantics: parent off keeps the children's checks, children greyed, layers hidden; persistence across reload;
  - **nearest picking inside a GEO clump**: synthetic pointer positions next to known marker pixels pick the nearest NORAD, never the topmost;
  - a label hover picks nothing;
  - select → white ring, others dimmed, card shows correct values vs skyfield (az/el/range) and the illumination state; values update at 1 Hz;
  - click empty → deselect, card "No object selected";
  - tooltip content incl. TLE age;
  - label declutter cost ≤ 3 ms, no long tasks;
  - Mount Status still 10 msg/s.
- Screenshots vs Main Dashboard 2.png and Track Cards.png at 1920 and 1280 @1.5.

### T2.3b — Labels toggle + FILTERS panel scaling (spec, 2026-10-21, user review)

**Context:** LEO labels flicker because declutter decisions change every 100 ms. The user chose a simpler fix than sticky placement: a **Labels** toggle. The user's screenshot (windowed browser at 150 %, about 1280 × 600 CSS px) shows the FILTERS content overflowing.
- Measured: content needs 443 px in a 403 px panel; the Tools group spills out and the page grows to 661 px with a scrollbar.
- At 1280 × 720 it fits with only 13 px to spare.
- Cause: the filter rows use fixed sizes (the tree is 263 px tall at every size).

**1. Labels toggle (`stores/filters.ts`, `VisibilityFilters.vue`, `SkyChart.vue`):**
- A top-level **"Labels"** checkbox as the last item of the Visibility tree. No children. Default on, persisted with the other filters.
- **Off:** no labels are drawn. Exceptions:
  - the **hovered** object's label still draws, and the tooltip works as now;
  - the **selected** object keeps its ring and label.
- **On:** behaviour unchanged.
- Unit test the semantics.

**2. FILTERS panel scaling** (same method as the Mount Status panel, T1.5):
- `container-type: size` on the FILTERS panel body.
- **Scaled from the panel's height** with `clamp(min, N·cqh, max)`: the Visibility tree font, row spacing, checkbox size, tree indent, gaps between groups, legend sizes, and the **Tools** group (legend, button height and font, gaps).
- **Max = today's values**, so the 1920 × 1080 layout is **identical** (measure before/after).
- **Minimums:** text ≥ 11 px, buttons ≥ 22 px tall.
- **Layout:** a flex column. Visibility at the top; **Tools anchored to the bottom** (`margin-top: auto`), never overlapping or pushed out of the panel.
- **Fallback:** when even the minimums don't fit, the Visibility tree scrolls inside its own box (thin scrollbar). Tools stays fully visible and the page never scrolls.
- **Page level:** at 1280 × 600 and 1280 × 720 (DPR 1.5) and 1920 × 1080, `document.scrollHeight == innerHeight` and no panel overflows its frame. If another panel (e.g. Track Info) overflows at 600, give it the same no-overflow treatment and report it.

**Must not touch:** `pwi4/`, ssa-toolkit, `docs/`, `analysis/`, the gateway. No commits.

**Gate (supervisor):**
- Web gates.
- Labels off → labels-drawn = 0; hover shows only that label + tooltip; the selected label remains.
- Measurements at 1920 × 1080 (unchanged from before), 1280 × 720 @1.5 and 1280 × 600 @1.5: no overflow; Tools bottom inside the panel; no page scroll; font sizes per size.
- Screenshots at all three sizes.

### T3.1 — Control path core + safety engine v1 (spec, 2026-10-21; user decisions same day)

**User decisions (10-21), all review recommendations accepted:**
- **O-SITE resolved:** all safety maths uses **PWI4's site** (what PWI4 actually points with). **Motion commands are refused while `site_check.ok == false`** (G7 reason "Station location differs from PWI4's site"); the user aligns PWI4 and Settings once by hand.
- **Control lock:** one controlling tab. The first tab to send a command takes control; other tabs are view-only with "Take control", and a takeover does not stop anything. **STOP is accepted from any authenticated tab, always.**
- **Link loss:** detected by gateway-driven WebSocket protocol ping/pong (not JS timers, which background tabs throttle). Silent loss is detected in ≤ 1 s; a socket close immediately. Edge "sleeping tabs" can sleep a background control tab (a correct stop, but unexpected): document "exclude localhost from sleeping tabs"; the standalone window (T6.2) avoids it.
- **Limit monitor:** a predictive stop (look-ahead about 0.5 s on the measured velocity) so overshoot stays small. Refusing bad targets remains the primary guard. When the mount is already inside a limit, moves heading **away** from it are allowed (recovery).
- **Sun crossing before D-SUN:** v1 predicts each slew's two-axis path and **refuses** any slew passing within 15° of the Sun (not only targets inside the disc).
- **Sun shortcut (user):** when the whole Sun disc is below Lim EL, the Sun path check is skipped for every motion. Condition: `sun_alt + 15° < min(Lim EL, current mount alt)`; the `current alt` term keeps it correct for a recovery move starting below Lim EL.
- **HOME (T3.2):** home is at about 85° elevation. The Sun path check applies; a HOME whose path crosses the disc is **refused until D-SUN (T3.5)** detours it. The shortcut applies.
- **Azimuth wrap (P8/P9):** before every goto the gateway sets the axis-0 wrap minimum so the nearest representation is taken, which makes the path predictable. Verify on the sim that this is runtime-only (`PWI4.cfg` unchanged; read-only check).
- **Audit log:** `%LOCALAPPDATA%\pw-mount-control\audit\YYYY-MM-DD.jsonl`, kept for 90 days.
- **Start-up stop (G4, 10-20):** before accepting any command. Verify whether PWI4 `mount/stop` also ends tracking; if not, also send tracking-off.

**Split:** T3.1a (command core, lock, link-loss, start-up stop, audit), then T3.1b (safety engine v1). Each is gated on **PWI4-sim**; the user launches PWI4 in Simulator mode for the gates.

#### T3.1a — Command core, control lock, link-loss stop, start-up stop, audit (`gateway/`)

- **First PWI4 writes:**
  - all PWI4 write calls live in **one module** (`gateway/control.py`), the only place allowed to write; I2 grep check (no PWI4 calls outside `gateway/`, writes only in `control.py`).
  - Never write PWI4 settings; never set the PWI4 site.
- **Protocol** on the existing authenticated WS:
  - client sends `{"type":"cmd","id":<uuid>,"cmd":...,"args":{...}}`;
  - gateway answers `{"type":"ack","id","ok","reason"}`;
  - pydantic-strict schemas, message size cap, unknown or invalid input → refused with a reason (G7).
  - **Commands in T3.1:** `take_control`, `release_control`, `stop`, `goto_altaz{alt,az}`, `goto_radec_j2000{ra_h,dec_deg}`, `tracking{on}`. (Connect/home/jog/goto UI come in T3.2–T3.4.)
- **Execution:**
  - one serialized command worker (asyncio), so PWI4 writes never overlap and never block the poll/publish loops; time-limited PWI4 calls;
  - **STOP preempts:** immediate, cancels anything pending.
  - Every non-STOP command requires the lock (refused otherwise: "Another window has control").
- **Lock:** the holder is a WS connection. The status broadcast gains `control: {holder: "you"|"other"|"none"}` per client.
- **Link-loss stop (G4, I3):**
  - when the lock holder's connection closes or misses pings (≤ 1 s) and motion is active (slewing / tracking / path or TLE follow / offset rate) → stop (+ tracking off if needed), audit, release the lock.
  - Nothing resumes on reconnect. Non-holder viewers disconnecting never stop anything.
- **Start-up stop (G4):** at gateway start, before the WS accepts commands: read status; if motion is active → stop, confirm STOPPED, audit. Also stop motion on **graceful gateway shutdown**.
- **Audit (`gateway/audit.py`):** JSONL per day. Entries: ts, client id (connection), cmd, args, result, reason, plus events (lock taken/released, link-loss stop, start-up stop, shutdown stop). Never secrets. 90-day cleanup at start.
- **Dev page `web/app/pages/dev/control.vue`** (unlinked, like `/dev/mount`): lock status + Take control, STOP, goto alt/az, goto RA/Dec, tracking on/off, last acks, live state. A store/client API extension in `stores/mount.ts` / `utils/mount-socket.ts` (send cmd, await ack with timeout).
- **Tests:**
  - unit tests with FakePwi4 (recorded requests): schema refusals, lock rules, STOP from a non-holder, preemption, link-loss path, start-up stop, audit lines.
  - `pwi4sim`-marked tests: stop latency; kill the holder socket mid-slew and mid-track → STOPPED within ≤ 1 s and stays STOPPED after reconnect (I3); start-up stop with a slewing/tracking sim; whether `mount/stop` ends tracking.

#### T3.1b — Safety engine v1 (`gateway/safety.py`)

- **Site gate:** motion commands refused while `site_check.ok == false`. All alt/az and Sun maths uses PWI4's site.
- **Target checks (refuse, G1/G7):**
  - `goto_altaz`: alt in [Lim EL, 90], az valid.
  - `goto_radec_j2000`: converted to topocentric alt/az now with skyfield (PWI4 site; J2000 → apparent of date). Refuse when below Lim EL. Refusing below the horizon guards P14.
  - Sun distance of the target > 15°.
  - `tracking on`: refused when the current pointing is below Lim EL or within 15° of the Sun.
- **Slew path prediction:**
  - axis model from PWI4 status (max velocity, acceleration per axis; slew time constant P13);
  - axis-0 wrap chosen nearest (decision above), sampled about every 0.1 s;
  - refuse when any sample is within 15° of the Sun. Skipped under the Sun shortcut.
  - The same path model is reused by D-SUN (T3.5) and HOME (T3.2).
- **Continuous monitor (10 Hz, every mode, G2/G3):**
  - predictive: `alt + v_alt·0.5 s < Lim EL` while descending → stop; predicted Sun distance < 15° while closing → stop. Sun checks skipped under the shortcut.
  - Covers sidereal tracking drifting down or into the Sun, and any motion started outside the app.
  - After a limit stop the state is STOPPED and commands that would push further are refused.
- **Recovery:** when already inside a limit, a goto whose target is outside every limit and whose path does not go deeper is allowed.
- **G7 reasons:** "Below station limit elevation (Lim EL x°)", "Within 15° of the Sun", "Slew path passes within 15° of the Sun", "Station location differs from PWI4's site", "PWI4 not running / not reachable", "Mount not connected".
- **Tests:**
  - unit tests on the pure functions: target checks, path predictor vs recorded sim slews (P13 timing), the Sun shortcut, recovery, predictive monitor.
  - Forged commands (below Lim EL, inside the Sun disc, path crossing the Sun, site mismatch) → refused (I4).
  - `pwi4sim`: a sidereal track of a setting target stops at Lim EL; a slew toward the limit stops with overshoot measured; refusals leave no PWI4 write (request log).

**Gate (supervisor, PWI4-sim):**
- pytest / ruff / web gates.
- PWI4 request log: writes only from `control.py`, never settings or site.
- I3 kill-socket runs; I4 forged commands.
- Start-up stop.
- Overshoot at Lim EL.
- Audit entries.
- `PWI4.cfg` unchanged before/after (wrap min runtime-only).
- Dev page round trip in a real browser.

### T3.2 — Connection sequence (A5a) + HOME + MOUNT CONTROL panel (spec, 2026-10-22; user decisions same day)

**User decisions (10-22):**
- **Home pose for the Sun path check:** alt **85°**, az **180°**. Stored in the settings file (`hardware.home_alt_deg` / `home_az_deg`, defaults 85 / 180, editable by hand afterwards). Not on the Settings screen.
- **HOME is blocked when its path crosses the 15° Sun disc** (until D-SUN, T3.5). The Sun shortcut applies (`sun_alt + 15 < min(Lim EL, mount alt)` → no Sun check).
  - **During CONNECT:** finish connected **without homing**, axes enabled, STOPPED. Warning "Home skipped: path passes within 15° of the Sun". The user can press HOME MOUNT later.
  - **During DISCONNECT:** ask once, "Home is blocked by the Sun; disconnect without homing?". Yes → disable the axes and disconnect; No → nothing changes.
- **PWI4 start timeout:** 60 s (may be lowered to 30 s later). PWI4 path auto-detected (`C:\Program Files (x86)\PlaneWave Instruments\PlaneWave Interface 4\PWI4.exe`, then `C:\Program Files\...`); override with `PW_PWI4_EXE`.

**Gateway (`control.py` sequences + `safety.py` home check):**
- **New commands** (all require the lock; STOP is always accepted and **cancels a running sequence**):
  - `connect`:
    1. start PWI4 if `/status` is unreachable (detached `Popen`; never kill PWI4), then poll until it answers, ≤ 60 s;
    2. `mount/connect`;
    3. `mount/enable` axes 0 and 1;
    4. HOME (Sun-checked; skipped with the warning when blocked).
  - `disconnect`:
    1. stop any motion;
    2. HOME (Sun-checked; when blocked the client must confirm, via `args.skip_home=true` on a second command; the first returns `ok:false` with reason code `home_blocked_confirm`);
    3. `mount/disable` axes 0 and 1;
    4. `mount/disconnect`.
    - PWI4 stays open.
  - `home`: the Sun path check, then `mount/find_home`.
  - Steps already satisfied (PWI4 running, mount already connected, axes already enabled) are passed through, not repeated.
- **State:**
  - **BUSY** (`state.set_override`) for the whole connect/disconnect sequence, with `state_reason` = the current step;
  - **HOMING** while a home runs. Completion = motion seen and then stationary for 1 s, or no motion within 3 s (the simulator's `find_home` is a no-op, P12), or a 180 s timeout → failure.
  - The snapshot gains `sequence: {name, step, step_index, steps, status: running|done|failed|cancelled, error, warning}`.
- **Failure rule (A5a):**
  - stop at the failing step; report "<step> failed: <reason>";
  - leave the mount safe: stop motion, **disable both axes** if connected;
  - no rollback of earlier steps.
  - Time-limited PWI4 calls (a failed or timed-out call = a failed step).
- **Safety during HOME:** refuse when the predicted path (both predictor variants) from the current pose to the home pose passes ≤ 15° from the Sun. While HOMING, the monitor checks actual limits only (actual Sun distance ≤ 15° → stop), not the corridor, because the real homing path is unknown until the real-mount session.
- **Audit:** sequence start, each step, done/failed/cancelled, the skipped-home warning, the disconnect-without-home confirmation.
- **Tests:**
  - FakePwi4 unit tests: the full success path for both sequences; a failure injected at each step (axes left disabled, no rollback, the reason names the step); STOP mid-sequence; the PWI4-start step with a fake launcher and an unreachable → reachable transition, and the 60 s timeout (clock injected); home Sun-blocked in connect (skip + warning) and in disconnect (confirm flow); idempotent steps; lock rules.
  - `pwi4sim` tests (`sim_guard`): connect from disconnected → connected, enabled, STOPPED; disconnect → disconnected with axes disabled; home (no-op on the sim) completes; BUSY seen during the sequences.

**Web — MOUNT CONTROL panel (`components/mount/MountControlPanel.vue`, concept/Main Dashboard 1.png):**
- **Connection group:** segmented `[|DISCONNECTED|][ CONNECT ]` / `[ DISCONNECT ][|CONNECTED|]` (A5a).
  - A status line under it: the current step while BUSY ("Connecting: enable axes…"), the failed step in red, the warning in amber.
  - DISCONNECT asks for confirmation once (dialog); the Sun-blocked home asks the second question.
- **Pointing Mode group** (`STOP` / `TRACK`): laid out per the render, disabled until T3.3 (tooltip).
- **Commands group:** **HOME MOUNT** enabled (only when connected and not BUSY); TRACK TARGET, JOG and GOTO disabled with "Arrives in T3.3/T3.4/T4".
- **Control lock:** when another window holds control, show "Controlled by another window" + **Take control**; when no one holds it, the first command takes it. Refusal reasons (acks) show on the status line (G7).
- **Sizing:** follows the dashboard scaling approach (container-relative, max = 1920 sizes), no overflow at 1280×600 @1.5.

**Must not touch:** `pwi4/`, ssa-toolkit, `docs/`, `analysis/`. Never write PWI4 settings or site; the PWI4 executable is only ever *started*, never stopped. No commits.

**Gate (supervisor, PWI4-sim):**
- pytest / ruff / web gates.
- Sim: connect → disconnect → connect sequence with BUSY/step updates; home on the sim; Sun-blocked home with a pinned test Sun (skip on connect; confirm on disconnect); STOP mid-sequence.
- Failure handling: unit evidence.
- Real browser: the MOUNT CONTROL panel at 1920 and 1280×600 @1.5.
- I2 grep (writes only in `control.py`); `PWI4.cfg` hash unchanged.
- **User check:** close PWI4 → CONNECT starts it.

### T3.3 — Pointing mode STOP/TRACK (A5b) + H4 transform helper (spec, 2026-10-22; user decisions Q1–Q3 same day, Q4 confirmed)

**Already in place from T3.1:**
- the `tracking {on}` command, gated by G1/G3 (current alt ≥ Lim EL, outside the Sun disc unless the Sun shortcut applies);
- `stop` (`mount/stop` + `_confirm_stopped`, which sends `tracking_off` when tracking persists);
- the monitor's predictive look-ahead for tracking (a sidereal track drifting into the Sun disc or below Lim EL is stopped).

T3.3 turns this into the A5b user feature and adds H4.

**User decisions (10-22):**
- **Q1, TRACK = parity with PWI4's Track:** TRACK sends `mount/tracking_on`.
  - The user tested on PWI4: after a satellite track, a coordinate goto or a star/object goto stopped mid-slew, Track tracks the RA/Dec of the current pointing.
  - The sim gate re-confirms this (after each kind of stopped goto, `tracking_on` holds the current RA/Dec and does not return to the old target).
- **Q2, H4 lives in the gateway** (skyfield) + `POST /api/transform`, because the app is heading to a standalone build. No browser-side port.
- **Q3, the not-homed guard (the "homed" flag belongs to one connect session):**
  - **Set** when a connect session starts: the gateway's CONNECT, or the gateway finding a mount already connected (default, Q4).
  - **Cleared** by a completed home (the sim's no-op home completes too, so the sim is NOT exempt and the guard is testable there) or by the user's **Proceed anyway** override.
  - **Stays cleared** until the next connect session; DISCONNECT ends the session. A failed, timed-out or cancelled home leaves the guard on.
  - **Warning:** shown after a successful CONNECT that ended without a home (Sun-blocked skip). It is a dialog: "Mount not homed; positions may be wrong. [Home mount] [Proceed anyway]".
    - Home mount = the normal HOME, which can still be Sun-blocked (same reason text).
    - Proceed anyway = override, audited `homed_override`.
    - Closing the dialog leaves the guard on: the panel shows a **NOT HOMED** chip, and the status line offers the same two actions.
  - **While on:** GOTO / TRACK / JOG are refused with reason `Mount not homed`. HOME, STOP and DISCONNECT are always allowed.
  - In the snapshot as `homed: {state: "homed"|"not_homed"|"overridden", since}`.
- **Q4 (user-confirmed 10-22):** when the gateway starts, or reconnects to PWI4, and finds the mount already connected without its own CONNECT, the gateway cannot know whether the mount was homed. Default: treat it as a new connect session → **not homed** + the warning.

**H4 (`gateway/transforms.py`):**
- `altaz_to_radec_j2000(alt, az, t, site)` and `radec_j2000_to_altaz(ra_h, dec, t, site)`.
- `t` is **explicit** (UTC datetime, no default "now" inside the helper). The site is passed in: the PWI4 site, per O-SITE.
- Convention (P17): J2000 ↔ topocentric apparent alt/az with precession, nutation and aberration, **no refraction**. This matches the existing `Ephem.radec_j2000_altaz`, which is refactored to call H4.
- The inverse must undo aberration correctly (a plain `from_altaz(...).radec()` can leave up to ~20″). Round-trip ≤ 0.1″ is a test.
- **Endpoint:** `POST /api/transform` `{dir: "altaz_to_radec"|"radec_to_altaz", t_utc, values}` → result. Read-only, no lock, token required (like the other REST calls), validated input (alt −90…90, az 0…360, ra 0…24, dec −90…90, t within ±50 years).
- **Tests:**
  - against **astropy** (dev-only dependency in `requirements-dev.txt`; never imported by runtime code): ≤ 1″ on a grid of sites (lat −60…+70), times (2000…2040), directions (alt 5…89, all az, both directions);
  - round-trip ≤ 0.1″;
  - a PWI4-sim cross-check (`sim_guard`): PWI4's reported alt/az vs H4 of PWI4's reported J2000 at the status timestamp. Record the difference only; the sim has no pointing model.

**TRACK / STOP (A5b), gateway:**
- **TRACK** = `tracking {on}` → `mount/tracking_on` (Q1).
  - Uses the same G1/G3 gate as T3.1.
  - Refused while BUSY / HOMING / SLEWING, or while an object track is running (T4; not reachable yet).
  - Ends TRACKING; the monitor takes over with its look-ahead.
- **STOP:** unchanged (it already holds the az/el of the moment and ends tracking).
  - Add a pwi4sim assertion: after STOP from TRACKING, `is_tracking` is false within 1 s **and the az/el stays put** (drift ≤ 0.01° over 3 s). This closes the "`mount/stop` stale `is_tracking`" item for the sim.
- **Sidereal accuracy on the sim:** TRACK for 60 s; RA/Dec J2000 drift ≤ 1″ and alt/az changing at the sidereal rate (vs H4).
- **Audit:** `track_start` (ra, dec, alt, az), `track_refused` (reason), and the existing stop entries.

**Web, MOUNT CONTROL panel Pointing Mode group:**
- Segmented control: `[|STOPPED|][ TRACK ]` / `[ STOP ][|TRACKING|]`.
  - TRACKING shows for any tracking state (sidereal now, object tracks later).
  - SLEWING: STOP is enabled and TRACK disabled ("Slewing").
- **TRACK disabled reasons** (G7, shown as a tooltip and on the status line): not connected; axes disabled; BUSY / HOMING; slewing; `Below Lim EL (x°)`; `Inside the Sun disc (x° from the Sun)`; `Mount not homed` (Q3); another window has control.
  - The client pre-check uses the snapshot (alt, `sun_distance_deg`, Sun shortcut); the gateway's refusal stays authoritative.
- **STOP is always enabled** whenever PWI4 is reachable; it takes control if no one holds it, as today.
- The Details modal gets this for free (the same `MountControlPanel`).

**Must not touch:** `pwi4/`, ssa-toolkit, `docs/`, `analysis/`. Never write PWI4 settings or site. No commits.

**Gate (supervisor):**
- pytest / ruff / web gates; H4 vs astropy ≤ 1″ (independent spot check by the supervisor).
- Sim:
  - TRACK from STOPPED → TRACKING, drift ≤ 1″ over 60 s;
  - STOP holds az/el;
  - TRACK refused below Lim EL and inside the Sun disc (pinned test Sun) with the right reasons;
  - the Q1 measurement (`tracking_on` after a goto + STOP: does it return to the old target?).
- Real browser: the Pointing Mode group at 1920 and 1280×600 @1.5, the disabled-reason tooltips, and the dashboard + Details modal in step.
- I2 grep (writes only in `control.py`).

### T3.3b — Refraction parity, PWI4's wrap turn kept, stall guard (spec, 2026-10-22, user decisions on P18/P19/P19a/Q5)

Runs after the user commits T3.2 + T3.3, before T3.4.

**User decisions (10-22):**
- **P18, refraction parity:** H4 and every local alt/az the app uses or sends must be in the same frame as PWI4's: refracted (observed) alt/az. A Go To Az/El must equal what the user would type into PWI4 directly.
- **P19, PWI4's unwinding turn is intended:** PWI4 normalises axis 0 into its wrap window to keep the mount's turning room, so further motion does not run it into a physical limit. That turn is correct on a homed mount, and is fine as long as it lands on the right RA/Dec.
  - **Revert the T3.3 wrap centring before `tracking_on`.** TRACK sends `tracking_on` as is.
  - The sim test changes from "holds the pose" to "after any turn, settles TRACKING on the RA/Dec of the stop moment (≤ 5″)".
  - The T3.1 wrap handling for gotos (`_prepare_wrap`, P8/P9) is untouched.
- **P19a, stall guard (new safety rule):** the mount is commanded to move (slewing, tracking or homing; or a high axis error / RMS) but an axis position does not change for 2–3 s → the mount has run into a physical limit.
  - The gateway **stops the mount automatically**.
  - It warns: "Mount stalled (axis N not moving); it may be at a physical limit. Home the mount before further use."
  - It audits `stall_stop`.

**Supervisor reading (the user may correct):**
- **Both axes** are watched (user-confirmed), with the per-axis rule below.
- After a stall stop the not-homed guard is set with reason `stalled` → GOTO / TRACK / JOG are refused until a **completed home**. **Proceed anyway is not offered** for a stall (the user said home before further use); a DISCONNECT + CONNECT starts a normal session (not homed + the usual dialog).
- **Detection:** expected motion is
  - (a) SLEWING / HOMING, or
  - (b) TRACKING with the axis's commanded rate above a threshold, or
  - (c) the axis error / RMS above a threshold,
  - and in each case the position is unchanged (≤ 1″) for 2.5 s.
  - Thresholds come from the sim and are re-tuned in the real-mount session.
  - It must not fire on a slow sidereal track near the pole of an axis: compare against the commanded rate, not a fixed speed.
- **Test on the sim:** P14 showed the sim drives to its −40° mechanical altitude limit and keeps "slewing / tracking" there. The test drives it there with its own client (`sim_guard`) and expects the gateway to stop it and set the guard. Plus FakePwi4 unit tests for both axes, a sidereal track (no false positive) and the thresholds.

**Q5 (user, 10-22): same model as PWI4.** Fit PWI4's own correction on the sim (reported alt vs unrefracted H4 over 5…89° alt) and use the same formula and constants, target ≤ 2″ vs PWI4. No temperature / pressure fields. Re-check in the real-mount session.

**P19a detail (user, 10-22):**
- Altitude is monitored too, "just in case". It rarely matters (the Lim EL guard; the mount practically never over-pitches).
- **The altitude axis usually reaches its target first and then stays still for the rest of the slew.** A still axis is only a stall when its **own distance to its target point is still large** (per-axis target difference), not just because the mount is SLEWING.
- So the per-axis rule is: the axis is commanded (its target difference > threshold, or its commanded rate > threshold), its position is unchanged (≤ 1″) for 2.5 s → stall.
- The worker must find the per-axis target-difference fields in `/status` and use them.

**H4 / safety changes (P18):**
- `radec_j2000_to_altaz` returns **refracted** alt/az, and `altaz_to_radec_j2000` takes refracted input.
- An `unrefracted` flag is kept for internal use.
- Safety compares Lim EL and the Sun disc in the same refracted frame as the mount's reported alt/az: the Sun is refracted too.
- `/api/transform` documents the frame.
- Tests:
  - vs astropy with the matching refraction model;
  - round trip ≤ 0.1″;
  - the PWI4-sim cross-check tightened from ≤ 90″ to ≤ 2–5″.

### T3.4 — Go To modal (A5c-iv) + Jog modal (A5c-iii) (spec, 2026-10-26; user decisions Q6–Q9 same day)

**User decisions (10-26):**
- **Q6:** the dry-run check, as recommended (`POST /api/mount/check`; SLEW disabled before the click with the gateway's exact reason).
- **Q7:** the modal stays open; the status line follows the slew, as recommended.
- **Q8, the user's variant of (b):** while STOPPED, **no `tracking_on`**. While a direction is held the gateway sets that **axis's rate directly**; on release, a keep-alive timeout or a disconnection, the axis rate goes **back to zero**.
  - **Caution for the worker:** P10 measured that `mount/offset axisN_set_rate_arcsec_per_sec` does not move an idle mount on the sim (bookkeeping only).
  - The first step of T3.4 is therefore a sim probe for a direct axis-rate path on a stopped mount: the offset rate with tracking off, any `/mount/` endpoint in the PWI4 4.1 API that sets an axis velocity, `goto_coord_pair` / raw custom path at a rate, etc. Re-read `pwi4/pwi4_client.py` and PWI4's HTTP API list.
  - If no direct rate path exists without `tracking_on`, **stop and ask the user** before falling back to (a) rolling gotos. Don't fall back silently.
  - STEP while stopped = the same axis-rate mechanism for a computed duration, or a single `goto_alt_az` to pose + step if the rate path is imprecise. The worker measures both and reports.
- **Intent (user, 10-27): parity with PWI4's own jog** (Commands → Jog, the Move Mount window: arrows held at a speed, max 10°/s), **plus** the extra of jogging while sidereal tracking (tracking continues at the new RA/Dec). The Q8 wording is the user's best guess at how PWI4 does it; **PWI4's actual behaviour wins**.
  - The T3.4b probe therefore records `/status` at 10 Hz while the user jogs in PWI4's Move Mount window on the sim (stopped and tracking), to see what PWI4 itself does: tracking flag, offsets, setpoint velocity, target fields, what happens on release.
  - It also tries the API candidates (offset axis rate with tracking off, etc.).
  - The jog is built to reproduce what PWI4 does; any mismatch goes to the user.
- - **Q8 final (user, 10-27): option A** (see P20): from STOPPED `tracking_on` + axis rate offset while held, rate zero + stop on release, wrap-window check first; while TRACKING, offsets only. Provisional until the real-mount session.
- **User check 10-27 (T3.4d):** JOG mode always ends STOPPED (from TRACK too); STEP from TRACKING always ends TRACKING (tracking re-enabled after the step if needed, wrap check first).
- **T3.4e (user, 10-27): option B wrap handling for azimuth jogs** (see the decision log): slide the wrap window during the jog; prompt Turn 360° / Stay only at the mechanical limit; the turn is checked like a goto now, D-SUN in T3.5.
- **Q9:** speeds are **mount-axis rates** (°/s of each axis), like PWI4's Move Mount.

**Renders:** `concept/Goto Modal.png`, `concept/Job Modal.png`. Both modals open from the MOUNT CONTROL Commands group (GO TO, JOG). The guards apply to both: not homed / stalled, control lock, BUSY.

**Go To modal (A5c-iv):**
- **Two groups: Local Angular (Azimuth, Elevation) and Celestial (RA, Dec J2000).**
  - Each value has the H2 format selector (DD/DMS, HMS/DD) and uses the existing triad widgets (`AngleField`, `DmsTriad`, `FormatSelect`).
  - The group last edited is **active** (green border) and is sent.
  - The other group shows the pair computed by H4 through `/api/transform` at the current time, dimmed, refreshed at 1 Hz (a celestial target moves in az/el), refracted like PWI4 (P18).
  - Editing the dimmed group makes it the active one.
- **Validation:**
  - Both pairs are checked against Lim EL and the 15° Sun disc. When invalid, the elevation box(es) turn red, SLEW TO COORDINATES is disabled (grey), and the status line gives the reason, e.g. "Target coordinates is below the station limit horizon".
  - Local Elevation is capped 0–90° (scrolling cannot go below 0°).
  - The status line reads "Ready" when valid.
- **Send:**
  - Local active → `goto_altaz` → ends STOPPED.
  - Celestial active → `goto_radec_j2000` → ends TRACKING (P4).
  - Allowed during an object track (replaces it; T4).
  - The gateway's refusal (including a Sun-crossing path, until D-SUN) shows on the status line.
- **Q6, the dry-run check (recommended):** a read-only gateway check, `POST /api/mount/check {cmd, args}`, runs the same safety verdict as the real command (target + predicted path vs the Sun) without sending anything. The modal calls it on every input change and once a second, so SLEW is disabled *before* the click with the gateway's exact reason (Sun-crossing path included). The alternative is client-side target checks only, with path refusals appearing after the click.
- **Q7, modal behaviour after SLEW (recommended):** the modal stays open and the status line follows "Slewing…" → "Arrived (STOPPED)" / "Tracking". The alternative is closing it on a successful send.

**Jog modal (A5c-iii):**
- **JOG / STEP segmented control.**
  - **JOG:** the mount turns at the chosen speed while a direction is held. Speeds 0.5 / 1.0 / 2.0 / 5.0 / 10.0 °/s (default 5.0) via a −/+ stepper; max 10°/s (P17).
  - **STEP:** one step per press. Steps 1′ / 5′ / 10′ / 30′ / 1° (default 10′). Further steps are blocked until the current one completes.
  - ▲▼ = elevation ±, ◀▶ = azimuth −/+. The Az/El boxes are read-only live values.
  - Keyboard: arrow keys, hold = jog (JOG mode), press = step (STEP mode).
- **While sidereal TRACKING:** jog/step nudge the pointing through PWI4 offsets (`axisN_set_rate_arcsec_per_sec` / `axisN_add_arcsec`, P10). Tracking continues at the new RA/Dec. The speed is the axis rate.
- **While STOPPED, a plain move. Q8, the mechanism** (P10: offsets do not move an idle mount):
  - **(a) recommended: rolling `goto_alt_az` targets.** While held, the gateway re-targets 1–2 s ahead along the direction at the chosen speed every ~0.2 s; on release, STOP. Steps = a single `goto_alt_az` to pose + step. It never enters tracking, so it never triggers PWI4's wrap normalisation turn (P19). `_prepare_wrap` keeps the nearest representation (P8/P9), and every target is validated like a goto. Speed is approximate (PWI4's 0.5 s approach constant, P13); the worker measures it.
  - (b) A temporary `tracking_on` + axis rate offset, then `tracking_off` on release. The rates are exact and the stop takes 0.12 s, **but `tracking_on` can make PWI4 turn up to 360° to normalise the wrap (P19)**: unacceptable as a jog side effect unless the pose is known to be inside the window.
  - (c) A short `altaz` custom path (P7, `update_wrap=0`) extended while held. Precise, but more moving parts.
- **Dead-man (I3 for jog):** the client sends `jog {dir, speed}` and then a keep-alive every 100 ms while held. The gateway stops the jog when keep-alives stop for > 300 ms, on release, on link loss (existing) and on blur / visibility-hidden in the browser.
- **Limits G1–G3:**
  - a jog/step toward Lim EL or into the Sun disc is refused;
  - while jogging, the monitor's predictive look-ahead stops the mount early enough to stay above Lim EL / outside the disc (stopping distance at 10°/s with the axis acceleration + latency);
  - the stall guard (T3.3b) stays active.
- During an object track: disabled (T4).
- **Q9, speed meaning (recommended):** the axis rate (°/s of each mount axis, as PWI4's own Move Mount window). The alternative is on-sky rate (azimuth scaled by 1/cos el).

**Gateway:**
- New commands `jog` (start/keep-alive/stop, strict schema, lock), `step` (dir, size) and `homed`-guarded motion.
- The dry-run check endpoint (Q6).
- Audit `jog_start` / `jog_stop` (reason: release / keep-alive timeout / limit / link) and `step`.
- Tests: FakePwi4 units for the keep-alive timeout, step serialization, limit refusals and the nudge vs plain choice; sim tests for jog speed accuracy (measured), stop latency on release and on a keep-alive loss, a step size accuracy, and a jog toward Lim EL stopped above it.

**Gate:**
- unit + sim tests;
- a real browser at 1920 and 1280×600 @1.5 against `concept/Goto Modal.png` / `Job Modal.png`;
- hold-to-jog with the mouse and with the keyboard; tab blur stops the jog;
- red elevation boxes and the disabled SLEW with reasons;
- a step while one is running is ignored.

### T3.5 — D-SUN Sun-avoidance routing (spec, 2026-10-27)

**User decisions (10-27):**
- **Q10, HOME:** route HOME through D-SUN **only when the mount is homed in this connect session** (e.g. a HOME or a disconnect after an earlier home). At connect, or whenever not homed / stalled, the pose cannot be trusted (P16c), so today's skip-with-warning (connect) / confirm (disconnect) stays.
- **Q11, Go To:** SLEW stays enabled. The status line reads "Path detours around the Sun (+N s)" before the click and "Avoiding the Sun" while routing.
- **Q12, State:** SLEWING with `state_reason` "Avoiding the Sun". The final state is the goto's normal one.

**Goal:** a slew whose predicted path passes within 15° of the Sun is **routed around the disc** instead of refused (§4 "D-SUN"). This applies to `goto_altaz`, `goto_radec_j2000`, the T3.4e `wrap_turn` and HOME. A clear slew is unchanged: it stays a plain PWI4 goto. Jog and step keep refuse/stop (manual motion gets no detours).

**What stays as it is:**
- a **target** inside the disc or below Lim EL is still refused (`sun_target` / `below_lim_el`);
- the Sun shortcut;
- the recovery rule: a start inside the disc that never gets closer is allowed with no detour;
- the not-homed / stalled guard, the lock, link loss, STOP and the monitor.

**Planner (pure, `gateway/dsun.py`):**
1. **Nominal route.** The axis-space line from the start `(axis0, alt)` to the target `(tgt0, alt)`, with axis 0 unwrapped as `axis0_target` / `target0` gives it. For `wrap_turn` this is the 360° sweep. Sample it densely (≤ 0.25° steps).
2. **Detect.** Find each contiguous interval where the Sun separation is below **R = 16°** (15° + O7 margin). The Sun is fixed at plan time; the Sun moves ≤ 0.2° over a 60 s route, which the 1° margin covers. When no sample is below 15° on the predictor's paths, there is no detour: the slew is a plain goto, as today.
3. **Detour.** For each blocked interval:
   - take the entry point P_in and the exit point P_out where the nominal route crosses the 16° circle;
   - project them into the azimuthal-equidistant tangent plane centred on the Sun;
   - build two candidates along the 16° circle from P_in to P_out, **clockwise and counter-clockwise**, sampled every ≤ 1° of arc;
   - back-project the points to alt/az, then to axis 0 continuous with the route (no 360° jumps).
4. **Discard a candidate** if any point is:
   - below `Lim EL + 0.5°`;
   - above the **keyhole**, `alt > 88°` or above axis 1's `max_mech`, whichever is lower;
   - outside axis 0's mechanical range;
   - or would need an axis rate above `0.8 × vmax` at the chosen route speed (this covers the high-alt azimuth speed-up).
5. **Pick** the candidate with the shorter route duration. With several blocked intervals (rare), choose per interval.
6. **Timing.** Assign timestamps along the whole route (nominal parts + arcs):
   - path speed = `min(cruise, 0.8 × vmax)` per axis;
   - accelerate and decelerate at the reported accel at the start, the end and sharp corners (> 30° heading change);
   - `cruise` comes from the T3.5a measurement (default 5°/s);
   - points every ≤ 0.5 s, plus a lead-in point = the current pose at `t0 + LEAD` (LEAD from T3.5a, default 2 s).
7. **No valid candidate** for an interval → refuse with `R_NO_DETOUR` = "Slew path passes within 15° of the Sun; no safe detour" (code `sun_no_detour`).
8. **Output.** `Route(points=[(t_rel, az, alt, axis0)], duration_s, detour_deg, side, intervals)` plus a corridor polyline in axis space for the monitor.
9. **Unit tests (§4 step 6):**
   - straight slews through the disc (both sides valid, the cheaper one picked);
   - Sun near the zenith (the over-zenith side discarded);
   - Sun near Lim EL (the low side discarded);
   - both sides blocked → refusal;
   - a `wrap_turn` sweep at the Sun's altitude;
   - the high-alt rate constraint;
   - every route point ≥ 16° − 0.05 from the Sun, ≥ Lim EL, and continuous in axis 0;
   - timing monotonic, with axis rates ≤ 0.8 × vmax.

**Execution (gateway):**
- **Safety:** where `_check_motion` / `check_home` return `R_PATH_SUN` today, it calls the planner instead.
  - On a route: the command is allowed and the engine stores a `RoutedGoto(route, final)` for the command id, like `_plans`.
  - On no route: `R_NO_DETOUR`.
  - **The planner returns None** (its straight axis-space line is clear, but the predictor's per-axis dog-leg crosses the disc: about 20 % of the flagged random slews, T3.5b check) → call it again with `force=True` and fly the straight line as a route with no detour. **Never fall back to a plain goto** once the predictor has flagged the slew.
  - The dry run returns `ok: true, code: "sun_detour", detour: {duration_s, side}`, so the modal can say so.
- **Executor (`ControlService`, a route job in the command worker; STOP / link loss / safety stop cancel it like any job):**
  1. `tracking_off`;
  2. set the wrap window so the whole route's axis-0 range is inside it (P8/P9);
  3. `custom_path/new altaz` → `add_point_list` (absolute JD = now + LEAD + t_rel) → `apply(update_wrap=0)`;
  4. wait until the route end time (+ settle) while the monitor watches;
  5. the final command:
     - `goto_altaz` → `goto_alt_az(target)` + `tracking_off`, ending STOPPED;
     - `goto_radec_j2000` → `goto_ra_dec_j2000`, a short final slew that ends TRACKING. The route's last point is the target's alt/az at the route end time;
     - `wrap_turn` → the existing final goto;
     - HOME → `find_home` (below).
  - On any PWI4 error: STOP, then refuse with the error.
- **Monitor:**
  - While a route runs, the monitor uses the validated rules (actual Lim EL, actual Sun ≤ 15° → stop) with the route's corridor (2° tolerance) instead of the predictor's paths;
  - The mount stops if it leaves the corridor;
  - The stall guard stays active (a frozen axis on a path counts as commanded).
- **State:**
  - `SLEWING`, `state_reason` "Avoiding the Sun";
  - snapshot `route: {active, side, t_end}`;
  - audit `sun_detour` (start, target, side, duration, detour_deg), `sun_detour_done` / `sun_detour_stopped`.
- **HOME (Q10):**
  - When the home path is Sun-blocked **and a home completed in this connect session** (Proceed anyway does not count: an override does not make the pose trustworthy), a D-SUN route takes the mount to the home pose, then `find_home` runs with the existing homing watch.
  - Not homed / overridden / stalled → today's behaviour (skip with a warning at connect, confirm at disconnect, refuse a standalone HOME with the Sun reason).

**Web:**
- **Go To modal:** with `code: "sun_detour"`, SLEW stays enabled and the status line reads "Path detours around the Sun (+N s)". `sun_no_detour` is shown as a refusal, but not with the red box, like `sun_path` today.
- The status line / panel shows "Avoiding the Sun" while routing.
- The Jog modal's Turn 360° prompt shows the same detour note.

**Briefs:**

| Brief | Kind | Scope | Timebox |
|---|---|---|---|
| **T3.5a** | sim measurement, no product code | Custom `altaz` path behaviour on the PWI4 sim, needed by the executor:<ul><li>acquisition when the first point is the current pose at now + LEAD (LEAD 1 / 2 / 4 s);</li><li>following error vs path speed (2 / 5 / 10 / 15°/s) incl. a 16° arc and a 45° corner;</li><li>what happens after the last point (does it hold, stop or extrapolate?);</li><li>the `is_slewing` / `is_tracking` / target fields during the path;</li><li>STOP latency during a path;</li><li>`update_wrap=0` with a route crossing az 0/360 and an axis-0 range beyond 360;</li><li>`goto_alt_az` / `goto_ra_dec_j2000` right after the path ends.</li></ul>Output: `scratchpad/t35a/report.md` + raw JSON; finding P21 | 45 min |
| **T3.5b** ∥ T3.5a | pure code + unit tests | `gateway/dsun.py` planner + `gateway/tests/test_dsun.py`, no I/O and no wiring; parameters (cruise, LEAD, keyhole, rate fraction) as module constants | 90 min |
| **T3.5c** | gateway wiring + sim tests | safety / control / monitor / snapshot / audit / HOME integration; FakePwi4 unit tests; `test_dsun_pwi4sim.py` (pinned Sun): a local goto through the disc routed and arrived ≥ 15° all the way; a celestial goto routed then TRACKING; a `wrap_turn` routed; HOME routed; no-detour refusal; STOP mid-route stops within 0.3 s; monitor corridor stop | 90 min |
| **T3.5d** | web | Go To / Jog prompt texts for `sun_detour` / `sun_no_detour`, "Avoiding the Sun" status; vitest | 45 min |

**Gate (Tier 2 per brief; Tier 3 at the Phase 3 end):**
- the planner's unit tests;
- a sim slew through the pinned Sun: minimum measured separation ≥ 15° (target ≥ 15.5°), arrival within 0.01°, route time within 30 % of the plan;
- the real browser for T3.5d only.
- **Real-mount checklist:** custom-path following on the L-350; the homing path vs the home-pose route.

### T4.1 — TRACK OBJECT rules: pass check + live button verdict (spec, 2026-10-28)

**Goal:** the gateway decides whether a satellite can be tracked right now (A1a button rules + A1d step 1), and the Track Info card shows that verdict live: TRACK OBJECT enabled, or disabled with the reason (G7). The track workflow itself (pre-position, `follow_tle`, stop at Lim EL, D-SUN for track segments) is T4.2; in T4.1 an enabled button does nothing yet.

**User decisions (10-28):**
- **Q13 TLE source:** the gateway fetches the TLE from the analysis service by NORAD ID and caches it. The browser only ever sends the NORAD ID.
- **Q14 Sun at start:** sheet rule kept. Disabled while the object is within 15° of the Sun. A pass that crosses the Sun later stays trackable (D-SUN detour in T4.2).
- **Q15 minimum time:** disabled when less than **30 s** of the pass remains above Lim EL (PWI4 needs 10–16 s to acquire, P6).
- **Q16 stale TLE (> 30 d):** trackable; the stale badge (T4.3) warns. The verdict returns `tle_age_days`.

**1. TLE client, `gateway/tle.py`:**
- New setting `PW_ANALYSIS_URL` (default `http://127.0.0.1:8410`) in `gateway/config.py`.
- `TleSource.get(norad)` → `GET {analysis}/api/sats/{norad}` → `{name, line1, line2, epoch, tle_age_days}`; sync httpx, timeout 2 s.
- Cache 10 min per NORAD; a 404 is cached for 30 s; a failure is not cached.
- Errors: 404 → "Satellite not found or decayed" (G6); timeout / connection error / 5xx → "Satellite data unavailable".
- Tests use an injected `httpx.MockTransport` and never the user's 8410.
- In web-app mode (remote analysis + server token) the auth is T6.2's job.

**2. Pass maths, `gateway/passes.py` (pure, no I/O):**
- `compute_pass(line1, line2, site, lim_el, t_unix, sun_fn) -> PassInfo`. The site is PWI4's (O-SITE). skyfield `EarthSatellite` (python-sgp4 underneath, P6 agreement < 0.04°) with **geometric** alt/az: same as the card and the chunks, slightly stricter than PWI4's refracted altitude (P18, the safe direction).
- **The current pass:**
  - search back up to 12 h for the rise (alt crosses 0° upward) and forward up to 12 h for the set;
  - coarse sampling, vectorised (step ≤ 10 s);
  - every crossing refined by bisection to ≤ 0.1 s;
  - the peak refined to ≤ 0.1 s / 0.001°;
  - no set within 12 h (GEO / HEO / long MEO) → `t_set = None` (continuous).
- **Fields** (Unix s, or `None` when not applicable):
  - `t_rise`, `t_in` (first upward Lim EL crossing in this pass), `t_peak`, `peak_alt`, `t_out` (downward Lim EL crossing), `t_set`;
  - `alt`, `az`, `rising` (alt increasing now), `sun_sep` (the object now vs the Sun).
- **Continuous objects:** for one that never sets, the peak is the highest altitude in the 12 h forward window, and `t_in` / `t_out` are the Lim EL crossings inside it (None if none).
- **Budget:** ≤ 30 ms per call for any object class on this PC.
- **Cache:** per (NORAD, TLE epoch, site, Lim EL), reused until `t_set` passes or 60 s go by. The 1 Hz checks only re-evaluate `alt` / `az` / `sun_sep` / the rules.

**3. Rules, `gateway/passes.py` `track_verdict(info, now) -> (reason, code, phase)`:** pure. The first failing row wins.

| # | Condition | Reason (G7) | code |
|---|---|---|---|
| 0 | the existing motion guards (`_gateway_refusal`: PWI4, mount connected, start-up, link stop, not homed / stalled, site mismatch, slewing / routing, jog) | as today | as today |
| 1 | TLE fetch failed | "Satellite not found or decayed" / "Satellite data unavailable" | `tle_missing` / `tle_unavailable` |
| 2 | `alt < 0` | "Below the horizon" | `below_horizon` |
| 3 | `sun_sep < 15` (the Sun shortcut applies as for gotos) | "Within 15° of the Sun" | `sun` |
| 4 | the pass never reaches Lim EL (`peak_alt < lim_el`) | "Pass peaks below the station limit elevation" | `peak_below_limit` |
| 5 | `alt < lim_el` and already past the peak | "Setting below the station limit elevation" | `setting` |
| 6 | time left above Lim EL `< 30 s`: `t_out − max(now, t_in)`, ∞ when `t_out` is None | "Pass ends in under 30 s" | `pass_too_short` |
| ✓ | `alt ≥ lim_el` → `phase = "track"`; else (rising toward Lim EL) → `phase = "pre_position"` | — | — |

Notes:
- **G5:** tracking another object is not a refusal (A1e: B replaces A). The caller decides in T4.2.
- **The Lim EL monitor margin** is not added to the rule; T4.2 decides where the track starts and stops.

**4. Check endpoint:**
- `track_object {norad:int}` added to the strict command schemas (`gateway/commands.py`) and to `CHECKABLE`.
- `POST /api/mount/check` answers `{ok, reason, code, target, detour: null, pass, tle_age_days}`:
  - `target` = the object's alt/az now;
  - `pass` = the fields above plus `phase`.
- **The real WS `track_object` command** in T4.1: validated, then refused with "Object tracking arrives in T4.2" (code `not_available`). Never sent to PWI4, not audited as motion.

**5. Web (A1a), `TrackCard.vue`:**
- While an object is selected, the card polls `/api/mount/check` (`track_object`) at 1 Hz, beside the state poll. Use a time-limited fetch; a gateway that does not answer → disabled "Gateway not responding".
- **TRACK OBJECT:**
  - enabled when `ok`;
  - otherwise disabled (the grey style as today), with `title` = the reason, plus a one-line muted reason under the button. The reason line must fit the 1280×600 @1.5 panel (`--u` sizing); drop to the tooltip only if it does not fit.
- **The click** is a no-op in T4.1; T4.2 sends `track_object`.
- **A pure helper `utils/track-verdict.ts`** maps the check answer to `{enabled, reason}` (vitest).
- No countdown yet (T4.2); no stale badge yet (T4.3).

**Must not touch:** `pwi4/`, ssa-toolkit, `docs/`, `analysis/` (its `/api/sats/{norad}` already returns `tle_line1/2`), PWI4 settings. No commits. Throwaway ports only; never the user's 3200 / 8400 / 8410.

**Briefs:**

| Brief | Kind | Scope | Timebox |
|---|---|---|---|
| **T4.1a** | gateway, unit tests only | `tle.py`, `passes.py`, the command schema, `CHECKABLE` + check fields, the WS refusal. Tests:<ul><li>`test_passes.py`: crossings vs a dense 0.1 s brute force ≤ 0.2 s for a LEO pass, a low pass peaking below Lim EL, a MEO and a GEO TLE (fixtures in the test file);</li><li>the rule table, one test per row, on synthetic `PassInfo` incl. edge cases (exactly 30 s left, peak exactly at Lim EL, `t_out` None);</li><li>`test_tle.py` with `MockTransport`: cache, 404 cache, timeout;</li><li>check-endpoint wiring with FakePwi4: the mount guards still win.</li></ul>Then the full non-sim pytest once + ruff | 90 min |
| **T4.1b** | web | `utils/track-verdict.ts` + vitest; `TrackCard.vue` polling + button state + reason line; typecheck + lint + touched vitest | 45 min |

**Gate (Tier 2):**
- Review both diffs against this spec.
- An independent spot check, with throwaway analysis + gateway and the local DB:
  - 5 real objects (2 LEO in or near a pass, 1 MEO, 1 GEO, 1 below the horizon): `t_in` / `t_out` / `peak_alt` vs a direct skyfield brute force, ≤ 1 s and ≤ 0.01°;
  - the verdict matches the rule table;
  - check latency ≤ 50 ms when cached.
- Real browser (UI changed), one pass at 1920 and at 1280×600 @1.5:
  - an enabled card;
  - a disabled one showing its reason (below the horizon, peak below the limit);
  - the analysis service stopped → "Satellite data unavailable".
- No sim test is needed (no PWI4 motion). The check endpoint's mount guards are covered by the FakePwi4 tests.

### T4.2 — Object track workflow: rendezvous, own-path following, stop at Lim EL, D-SUN for passes (spec, 2026-10-28)

**Goal:** TRACK OBJECT works (A1d). The gateway:
- re-checks the pass (T4.1 rules);
- moves the mount to a meeting point on the pass (the Lim EL crossing, or a point a little ahead when the object is already up);
- waits there;
- follows the satellite with its **own timed alt/az path** (Q17);
- detours around the Sun inside that path;
- stops at the Lim EL exit → STOPPED.

**User decisions (10-28):**
- **Q17 track engine = the gateway's own custom alt/az path** (not `follow_tle`): one mechanism for every pass, the Sun detour inside the path, the wrap under gateway control, the monitor knows the planned path, the path ends exactly at the Lim EL exit. Basis P22: 0.003° on the sim; ≥ 10 000 points; extend with add + apply.
- **Q18 near-zenith passes:** trackable. When `peak_alt > 87°` the card notes "Near-zenith pass: tracking lags near the peak" (P22: 0.6° lag at 88°).

**Sheet rules applied** (no new decisions):
- **G5 / A1e:** a new `track_object` while tracking replaces the track.
- **A5c-iv:** Go To is allowed during an object track and replaces it.
- **A5c-i:** HOME = stop, then home.
- **A5b:** TRACK (sidereal) ends the object track.
- **A5c-iii:** STOP ends it, holding the pose of the moment; jog / step are refused during an object track ("Object tracking; press STOP first").
- **G3 / A1d-4:** the track ends at Lim EL → STOPPED. **G4:** link loss stops it.

**1. Track planner, `gateway/trackplan.py`** (pure, no I/O; reuses `passes.py` and `dsun.py`):
- **Track points:**
  - the object's alt/az from sgp4 (skyfield, as in `passes.py`) every **1 s** (P22), from `t_start` to `t_out`;
  - **PWI4's refraction applied** (`transforms.refract_alt`): path coordinates are PWI4's refracted alt/az (P22);
  - the azimuth unwrapped to a continuous axis-0 sequence starting from the axis-0 value nearest the rendezvous pose.
- **Never-setting objects** (`t_out` None): the first chunk covers 30 min. The executor extends the path every 10 min (add + apply, P22) and stops the track if the object drops below Lim EL.
- **Rendezvous** (one rule for both phases): `t_r = max(t_in, now + slew_estimate + 3 s)`.
  - `slew_estimate` uses the existing slew-time model (vmax / accel + approach constant, P13) from the current pose to the object at `t_r`; iterate 3×.
  - The rendezvous point = the object at `t_r` (refracted). It must be ≥ Lim EL + 0.5 (the D-SUN `LIM_EL_PAD`) and not within 15° of the Sun.
  - `t_r` must leave ≥ 30 s before `t_out` (Q15). Otherwise refuse "Pass ends in under 30 s".
- **D-SUN for track segments** (§4 step 4):
  - Blocked intervals are where the track comes within **16°** of the Sun (R_DETOUR).
  - For each interval [t_a, t_b], the track points inside it are replaced by an arc on the 16° circle (Sun-centred tangent plane, `dsun` helpers), from the entry point at t_a to the exit point at t_b, timed uniformly over [t_a, t_b], with points every 0.25 s.
  - Pick the clockwise or counter-clockwise arc: the shorter one that stays ≥ Lim EL + 0.5 and ≤ the keyhole, with axis rates ≤ 0.8 vmax and arc speed ≤ `ARC_CRUISE_DEG_S`.
  - Neither side usable → **no detour** → refuse "Pass passes within 15° of the Sun; no safe detour" (code `sun_no_detour`).
  - The Sun position is evaluated at each interval's mid time (it moves 0.004°/s).
  - A blocked interval that includes `t_r` → the rendezvous moves to the end of that interval. If that leaves < 30 s, refuse.
- **Zenith note:** `peak_alt > 87°` → note text (Q18). It is not a refusal. A peak above the keyhole is still trackable; the planner does not reshape the track.
- **Output `TrackPlan`:**
  - `points` (t, az, alt, axis0);
  - `t_r`, `rendezvous` (alt, az, axis0), `t_end`;
  - `detours` [(t_a, t_b, side, length_deg)], `min_sun_sep_deg`;
  - `notes`, `corridor`.
- **Budget:** ≤ 100 ms for a 15 min LEO pass with one detour.
- **Unit tests:**
  - points vs skyfield + `refract_alt` ≤ 0.001°;
  - continuity of axis 0 across az 0/360 and through a near-zenith flip;
  - rendezvous for both phases;
  - detour clearance ≥ 16° − 0.05 along the arc, cw/ccw choice, no-detour cases (Lim EL, keyhole);
  - the 30 s rule;
  - the zenith note.

**2. Verdict additions (T4.1 rules, `passes.py` / `trackcheck.py`):**
- Row 7 = the plan itself: the T4.1 check now also runs the planner (cached with the pass). A no-detour or rendezvous failure refuses with the reasons above.
- `/api/mount/check` returns:
  - `detour`: `{seconds, count}` or null;
  - `notes`: a list;
  - `pass.t_r`.
- The card shows the notes (zenith) under an enabled button. The real command re-runs the same check (row 0 included) before doing anything.

**3. Executor, `ControlService` (`gateway/control.py`; the job pattern of `_route_job`):** `track_object {norad}`.
- **The sequence:**
  1. Refuse per the verdict.
  2. If tracking an object already: end that track (stop) first (G5).
  3. **Rendezvous goto:** the existing validated `goto_altaz` path to the rendezvous point, including D-SUN routing (`ROUTABLE`). State SLEWING. The goto ends STOPPED (`tracking_off`).
  4. If the goto arrives after `t_r − LEAD`, re-plan once from the current time. If that fails → STOPPED + reason.
  5. **Upload the track path** right after arrival:
     - `tracking_off` → wrap min set so the window holds the plan's axis-0 range (`route_wrap_plan`; a pass spans < 360°);
     - `custom_path/new altaz` → `add_point_list` (`jd, az, alt`, refracted) → `apply(update_wrap=0)`;
     - the first point = the current pose at `t_r` (the mount waits still until then, P21).
  6. The state override is TRACKING:
     - "Waiting for {name}" until `t_r`;
     - then "Tracking {name}";
     - "Avoiding the Sun" during a detour interval.
  7. **Long tracks:** extend every 10 min (add + apply, no `new`).
  8. **End:** at `t_end` + 1 s settle → `stop` + `tracking_off` (P21/P22: the path holds the last pose with tracking on) → STOPPED. State reason "Track ended at Lim EL" (cleared by the next command). Audit `track_done`.
- **Ack:** the command is acked when the rendezvous goto starts. A refusal acks `ok:false` + reason.
- **Ending a track:**
  - STOP (from any window): `track_stopped`.
  - Link loss: the existing G4 stop.
  - Go To / HOME / TRACK (sidereal): stop the track, then run the command (replaces it; audit `track_replaced`).
  - `track_object` again: replace.
  - jog / step / wrap_turn: refused "Object tracking; press STOP first".
  - A monitor limit stop: `track_stopped` with the limit reason (G3).
- **Snapshot `track`** (null when idle):
  - `{norad, name, phase: "slewing"|"waiting"|"tracking"|"detour", t_r, t_end, t_peak, peak_alt, detours, notes}`;
  - `ended`: `{norad, reason, at}` for 30 s after an end, so the card can show "Track ended at Lim EL".
- **Audit:** `track_start` (norad, name, TLE epoch, t_r, t_end, detours, min_sun_sep), `track_done`, `track_stopped`, `track_replaced`.
- **Monitor:**
  - while a track path runs: a validated plan with the track **corridor** (2°, as for routes) from the upload until `t_end` + settle;
  - the actual Lim EL / Sun limits stay on (G3);
  - the predictive look-ahead is OFF for the validated track (it would stop the mount early at the Lim EL exit);
  - during the wait at the rendezvous the plan's corridor holds the mount still.

**4. Web:**
- **Track Info card:**
  - TRACK OBJECT sends `track_object` (WS command with ack). A refusal shows its reason on the reason line.
  - While this card's NORAD is the snapshot `track`, the button reads **TRACKING** and is disabled (A1e). The reason line shows the phase:
    - "Slewing to the pass";
    - "Waiting for the pass — mm:ss" (countdown to `t_r`);
    - "Tracking — ends in mm:ss";
    - "Avoiding the Sun";
    - plus the zenith note when present.
  - After an end: "Track ended at Lim EL" / the stop reason.
- **Status line / panel:** TRACKING with the override text; TRACK / STOP behave as A5b.
- **Jog modal:** pad disabled during an object track with "Object tracking; press STOP first".
- **Not in T4.2:** the A1e pinning / stacking rules, the × hidden, the Track Target modal and the stale badge (all T4.3).

**5. Sim tests** (`gateway/tests/test_track_object_pwi4sim.py`, Sun pinned with `PW_TEST_SUN_ALTAZ`, a throwaway gateway with `PW_ANALYSIS_URL` → a **TLE stub** HTTP server on a throwaway port serving `/api/sats/{norad}` with synthetic TLEs; the generator from T4.2a `scratchpad/t42a/tlegen.py` moves to `gateway/tests/tlegen.py`):

| Test | Expectation |
|---|---|
| Normal pass (object already above Lim EL) | rendezvous ahead on the track; following error ≤ 0.05° after acquisition; ends STOPPED at `t_end` within 2 s, mount ≥ Lim EL throughout; audit start / done |
| Rising below the limit | SLEWING → TRACKING "Waiting" at the crossing point; the mount still until `t_r`; then follows; never below Lim EL − 0.1 (refracted) |
| Peak below the limit | refused, `peak_below_limit`, nothing sent to PWI4 |
| Through the Sun (Sun pinned on the pass) | "Avoiding the Sun" during the interval; measured min separation ≥ 15° (target ≥ 15.5°); back on the object ≤ 0.05° within 3 s after t_b |
| STOP mid-track | stopped within 0.3 s, STOPPED, `track_stopped` |
| Go To during a track | track ended, the goto runs |
| Wrap | the axis-0 wrap min unchanged by the track (no PWI4 self-change; the path has `update_wrap=0`) |
| Replace (G5) | a second `track_object` ends the first and starts the second |
| Near-zenith pass (peak ≈ 89°, `tlegen.ZENITH_89`-like) | the axis-0 lag near the peak does NOT trip the 2° corridor (it measures the distance to the path polyline in axis space, not to the time point); back on the object ≤ 0.05° within 5 s after the peak; the card note present |

- Keep each test to one short pass: rise 60–120 s ahead, 20–45° peaks.
- The user's 8400 gateway must be stopped for sim runs.

**Must not touch:** `pwi4/`, ssa-toolkit, `docs/`, `analysis/`, PWI4 settings. No commits. Throwaway ports only.

**Briefs:**

| Brief | Kind | Scope | Timebox |
|---|---|---|---|
| **T4.2b** | pure code + unit tests | `gateway/trackplan.py` + `test_trackplan.py`; verdict row 7 + check fields (`detour`, `notes`, `pass.t_r`) | 90 min |
| **T4.2c** | gateway wiring + FakePwi4 unit tests | executor, state override, snapshot `track`, ending rules, monitor plan, audit, the WS command; `tlegen.py` + TLE stub helper | 90 min |
| **T4.2d** | sim tests | `test_track_object_pwi4sim.py` (the table above), once; a second run only if a timing result is within 20 % of its limit | 60 min |
| **T4.2e** | web | card TRACK / TRACKING / phases / countdown / notes / ended; Jog disabled during an object track; vitest | 45 min |

**Gate (Tier 2):**
- Diff review.
- Re-run the new sim tests once.
- A spot check: one synthetic pass flown on the sim, its recorded pointing vs skyfield + refraction, min Sun separation.
- Real browser (T4.2e): a pass from click to "Track ended at Lim EL" at 1920 and 1280×600 @1.5.
- **Real-mount checklist additions:** satellite tracking via custom path on the L-350 (following error, the zenith lag at real axis speeds), the rendezvous wait.

### T4.3 — Card states and stacking (A1e), Track Target modal (A5c-ii), stale-TLE badge (spec, 2026-10-29)

**Goal:**
- The Track Info panel follows the A1e rules while tracking.
- MOUNT CONTROL › TRACK TARGET opens the Track Target modal (`concept/Track target Modal.png`): search, filter, pick, track.
- Objects with an old TLE carry a stale badge.

**User decisions (10-29):**
- **Q21 stacking:** while A is tracked and B is selected, B's full card sits on top and A collapses to a one-line pinned strip below it (name + phase line). Clicking the strip re-focuses A (B closes).
- **Q22 search set:** all live (non-decayed) objects: payloads, rocket bodies and debris. Stale TLEs are listed with the badge.
- **Q23 row selection:** lives in the modal only. The dashboard selection changes only when tracking starts.

**1. Analysis (`analysis/`):**
- `GET /api/sats/search`: add `country` (exact `country_code`, case-insensitive) and `orbit_group` (`leo` = LEO + SSO, `meo` = MEO, `geo` = GEO + HEO, the A2a mapping). The existing `orbit_class` is kept. Default (no filter) = the 500 latest NORAD ids, newest first, as today.
- `GET /api/sats/countries` → `[{code, count}]`: the distinct `country_code` of live objects, most common first, null codes left out. Cached with the catalogue (refreshed on its reload).
- Tests with the fixture source (filters, the mapping, countries). No DB writes.

**2. Track Info panel (A1e), `components/track/`:**
- **States**, from the selection store `selectedNorad` + the snapshot `track` (phase set = tracking):

| State | Panel shows |
|---|---|
| nothing selected, not tracking | "No object selected" (as today) |
| B selected, not tracking | B's card (as today) |
| tracking A, nothing selected or A selected | A's card **pinned**: button "TRACKING" (disabled), the × hidden, the phase line (T4.2e) |
| tracking A, B selected | B's full card + **A strip** below it (📌 name · phase line, e.g. "THAICOM 8 · Tracking — 03:21") |
| B's TRACK OBJECT while tracking A | B becomes the tracked object (G5 replace); A's strip goes |
| B closed / deselected while tracking A | A's pinned card returns |
| A strip clicked | the Sky Viewer selection re-focuses on A (B closes) |
| track ends (STOP, limit, Goto, Home, TRACK, Sun stop) | A's card stays as a normal selected card when A is the selection; if B is selected at that moment, A is dropped |

- **Every tracking command brings up the tracked object's card:** a successful `track_object` from the card, the modal or (later) Pass Analysis sets the selection to the tracked NORAD.
- **Layout:**
  - the strip is one line inside the panel, with ellipsis;
  - B's card + strip must fit the 1280×600 @1.5 panel; tighten via the existing `@container` block, and if it cannot fit, the strip replaces the card's reason line — report which;
  - a pure helper `utils/track-panel.ts` (state → what to render) with vitest for every row above.
- The strip shows the phase line from the snapshot (no extra polling).

**3. Stale-TLE badge:**
- `components/track/StaleBadge.vue`: a small amber chip "TLE {N} d" when `tle_age_days > 30` (constant `STALE_TLE_DAYS` = 30, shared with the analysis cutoff); `title` = "TLE is {N} days old: the position may be off".
- **Where:**
  - on the Track Info card, after the class chip (from `/api/sky/state` `tle_age_days`);
  - in the Track Target results (EPOCH cell);
  - later on Pass Analysis results (T5.1).
- vitest for the threshold.

**4. Track Target modal (A5c-ii), `components/track/TrackTargetDialog.vue`:**
- Opened by MOUNT CONTROL › TRACK TARGET (enable the button; it follows the same control-panel enabling as GOTO / JOG). The dialog shell and sizing are like the Go To modal.
- **Tabs:**
  - SATELLITE (active);
  - AIRCRAFT: present but disabled, `title` "No ADS-B source configured"; no aircraft content is built.
- **Satellite filters row:**
  - search box "Search name or NORAD ID" (Enter = FILTER);
  - Orbit select: any / LEO / MEO / GEO → `orbit_group`;
  - Origin select: "any" + the countries from `/api/sats/countries`, with flags (`FlagBadge`), default any;
  - FILTER and CLEAR buttons (CLEAR resets all and reloads the default list).
- **Results table** (render): NORAD ID ▼, NAME, ORBIT (`orbit_class`), ORIGIN (flag + code), EPOCH (`YYYY-MM-DD hh:mm:ss` in the dashboard clock's zone, UTC or Local) + the stale badge.
  - Sortable by any column (click the header; ▼ / ▲); default NORAD ID descending.
  - At most 500 rows; when 500 come back, a muted footer reads "Showing the first 500 matches — refine the search".
  - Scrolls inside the modal; row click selects (highlight); a double-click = TRACK SELECTED TARGET.
  - Loading, "No matches" and an error line ("Satellite data unavailable" when analysis is down; time-limited fetch).
- **TRACK SELECTED TARGET** (full width, like the render):
  - enabled from the gateway check (`POST /api/mount/check` `track_object` for the selected row, 1 Hz while a row is selected — `utils/track-verdict.ts`);
  - the status line below = the reason, the check note (zenith / Sun blocks) or "Ready";
  - nothing selected → disabled, "Select a target".
  - Click → `track_object` (WS ack). On ok: the dashboard selection = that NORAD (A1e) and the modal **closes**. On a refusal: the reason on the status line, the modal stays.
- **Keyboard:** ↑ / ↓ move the selection, Enter tracks, Esc closes.
- vitest for the pure parts (query building, sorting, the epoch format, the status-line text).

**Must not touch:** `pwi4/`, ssa-toolkit, `docs/`, the gateway (no change needed: `/api/sats/{norad}` serves any non-decayed object; the check / command exist), PWI4 settings. No commits. Throwaway ports only; never the user's 3200 / 8400 / 8410.

**Briefs:**

| Brief | Kind | Scope | Timebox |
|---|---|---|---|
| **T4.3a** | analysis | `country` + `orbit_group` search filters, `/api/sats/countries`, tests | 30 min |
| **T4.3b** ∥ a | web | A1e panel states + strip + `utils/track-panel.ts` + `StaleBadge` on the card; vitest | 60 min |
| **T4.3c** after a, b | web | Track Target modal + TRACK TARGET button + badge in results; vitest | 75 min |

**Gate (Tier 2):**
- Diff review.
- Analysis tests + a DB spot check through a throwaway analysis (counts only: default 500 newest; THAI country filter; the LEO group includes SSO).
- **Real browser**, throwaway servers + the PWI4 sim + the TLE stub for one synthetic pass, at 1920 and 1280×600 @1.5:
  - the modal: search / filter / sort, the stale badge, the status line, TRACK SELECTED TARGET closes the modal and brings up the tracked card;
  - A1e: select B while tracking → B + the A strip; strip click → A; deselect → A pinned; STOP with B selected → A dropped.
- No new sim test (no new motion).
- Phase 4 end (Tier 3) after T4.3: full sim suite ×2 (includes the owed `test_track_object_pwi4sim.py` re-run after T4.2f), harness, full browser pass, flaky list.

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
| T2.1 | ✅ | ✅ 10-21 | ✅ 10-21 | ✅ 10-21 | pending (incl. live sim goto follow) | 8 870 stars (V ≤ 6.5, indep. count ✓); 10 bright stars ≤ 0.37 px vs skyfield at 1920; stars 1 Hz / base flat; clock UTC↔Local persists, Asia/Bangkok ✓ |
| T2.1b | ✅ | ✅ 10-21 | ✅ 10-21 | ✅ 10-21 | pending | user review: sky colour by Sun alt + star fading, broken-ring marker, Settings save flow (disabled when unchanged, warn-then-confirm) |
| T2.2a | ✅ | ✅ 10-21 | ✅ 10-21 | ✅ 10-21 | with T2.2b | analysis: catalogue (active payloads), search (latest-500), binary sky chunks, pool + BELOW_NORMAL priority |
| T2.2b | ✅ | ✅ 10-21 | ✅ 10-21 | ✅ 10-21 | pending | web: chunk cache (head → 30 min, next at 10 min left), 3 class canvases |
| T2.3b | ✅ | ✅ 10-21 | ✅ 10-21 | ✅ 10-21 | pending | Labels toggle + FILTERS panel height scaling (user screenshot 1280×600 @1.5 overflow) |
| T2.3 | ✅ | ✅ 10-21 | ✅ 10-21 | ✅ 10-21 | pending (+ O-FAST recheck) | filters tree, nearest hover + tooltip (TLE age), select/focus, decluttered labels, read-only Track Info card, `/api/sky/state` |
| T3.1a | ✅ | ✅ 10-21 | ✅ 10-21 | ✅ 10-21 | pending (/dev/control) | command core, lock, link-loss + start-up + shutdown stop, audit, /dev/control |
| T3.1b | ✅ | ✅ 10-21 | ✅ 10-22 | ✅ 10-22 | pending (/dev/control) | safety v1: PWI4-site gate (O-SITE), target + path Sun checks, Sun shortcut, predictive monitor, recovery |
| T3.2 | ✅ | ✅ 10-22 | ✅ 10-22 | ✅ 10-22 | pending (incl. close PWI4 → CONNECT starts it) | connect/disconnect sequences (BUSY, failed step, PWI4 start 60 s), HOME (85/180 in settings, Sun-blocked → skip/confirm), MOUNT CONTROL panel |
| T3.3 | ✅ | ✅ 10-22 | ✅ 10-22 | ✅ 10-22 | pending (with T3.2; commit together) | TRACK = tracking_on (+ wrap centring, P19); H4 in gateway + `/api/transform` (0.47–0.53″ vs astropy); not-homed guard per connect session + dialog/chip; Pointing Mode STOP/TRACK |
| T3.3b | ✅ | ✅ 10-27 | ✅ 10-27 | ✅ 10-27 | ✅ 10-27 (with T3.4) | refraction parity (P18), keep PWI4 wrap turn (P19 revert), stall guard (P19a); spec ready (Q5 = PWI4's model) |
| T3.4 | ✅ | ✅ 10-27 | ✅ 10-27 | ✅ 10-27 | ✅ 10-27 (incl. T3.4d/e) | a: Go To modal + `/api/mount/check` dry run; b: PWI4 jog recording + rate probe (P20); c: Jog modal + jog/step (option A). First task under the new test tiers |
| T3.5 | ✅ | ✅ 10-27 | ✅ 10-28 | ✅ 10-28 | ✅ 10-28 (commit with the Phase 3 close) | D-SUN: a probe (P21), b planner, c wiring, d/f web, e sim tests (+ turn-watcher fix); browser pass 10-28 |
| T4.1 | ✅ | ✅ 10-28 | ✅ 10-28 | ✅ 10-28 | ✅ 10-28 (user visual check green) | a: TLE client + pass maths + rules + check endpoint (real TLEs: crossings 0.00 s, peak ≤ 0.005° vs skyfield); b: live TRACK OBJECT verdict + reason line on the card (browser pass 1920 + 1280×600 @1.5) |
| T4.2 | ✅ | ✅ 10-28 | ✅ 10-29 | ✅ 10-29 | ✅ 10-29 (+ T4.2f fixes, re-checked) | a probe (P22), b planner, c executor, d sim tests 10/10, e web; Q17 own path, Q18 zenith note, Q19 sky-distance corridor; browser pass click → "Track ended at Lim EL" at 1920 + 1280×600 @1.5 |
| T4.3 | ✅ | ✅ 10-29 | ✅ 10-29 | ✅ 10-29 | ✅ 10-29 (+ T4.3d / T4.3e adjustments, user-checked) | a search filters + countries; b A1e panel + strip + stale badge; c Track Target modal; browser pass 1920 + 1280×600 @1.5 (THAICOM 8 tracked from the modal) |
| T5.1 | ⬜ | | | | | O4 |
| T5.2 | ⬜ | | | | | |
| T5.3 | ⬜ | | | | | O6 |
| T6.1 | ⬜ | | | | | |
| T6.2 | ⬜ | | | | | |
| T6.3 | ⬜ | | | | | |

**Resume point (2026-10-29, Phase 4 COMPLETE, user-checked):** Phases 0–3 committed; T4.1 `1d76170`. **T4.2 (+ T4.2f) and T4.3 (+ T4.3d / T4.3e) are verified and user-checked; the user commits them as the Phase 4 close.** Next:
1. **Phase 5 (Analysis modals), T5.1 Pass Analysis lite (A2b-i) spec first**: optical visibility per O4, the live [Track] button (reuse `/api/mount/check` `track_object`, `afterTrackStarted`, `StaleBadge`), the pass list vs ssa-toolkit. Read the logic sheet A2b-i and `concept/Pass Analysis.png`.
2. **The real-mount session** (user) whenever available: the checklist below plus D-SUN custom-path following (10°/s arcs, 15°/s straights) and HOME routing vs the real homing path.
- **Real-mount session checklist** (the user's first session on the real L-350; homing + jog will be tested together):
  - PWI4's real jog behaviour (does a release keep tracking? speeds? P20; option A provisional);
  - the real azimuth mechanical range + wrap behaviour (T3.4e assumed the sim's ±300°);
  - stall-guard thresholds (P19a);
  - HOMING path (≈ 60° region sensor, P16) and the homing altitude floor;
  - what PWI4 reports before the first home, and whether `/status` has a homed flag;
  - whether `mount/stop` ends tracking;
  - real acceleration vs the jog look-ahead / limit margins;
  - refraction match (P18);
  - PWI4's wrap turn on `tracking_on` (P19);
  - `PWI4.cfg` behaviour;
  - satellite tracking via the custom path (following error, the zenith lag at real axis speeds, the rendezvous wait).

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
| 2026-10-27 | T3.4e | Azimuth jog across PWI4's wrap edge swung 360° silently. Option B: the gateway slides PWI4's wrap window along with an azimuth jog (no turn while the axis has mechanical room, sim ±300°); near the real mechanical limit the jog stops with a margin and prompts "turn 360° / stay"; Turn = a goto-like validated reposition (pad dimmed, STOP available; D-SUN routing in T3.5 like gotos); Stay = stop at the limit, prompt again on the next jog that way | user |
| 2026-10-29 | T4.3 Q21–Q23 | A1e stacking: B's full card + a one-line pinned strip for the tracked A (strip click re-focuses A); Track Target search lists all live objects (stale with the badge); a modal row selection stays in the modal (the dashboard selection changes when tracking starts) | user |
| 2026-10-29 | T4.2 Q20 | A slow object (GEO / MEO) that drifts into the Sun disc later (a blocked stretch > 10 min) is tracked until the 16° entry and ends STOPPED "Track ended: Sun approaching" (card note "Sun blocks the track in N min"); refused only with < 30 s left. LEO passes keep the D-SUN detour / no-detour refusal | user |
| 2026-10-29 | T4.2 Q19 | Object-track corridor measured on the sky (axis-0 offsets × cos alt), 2° kept; gotos / D-SUN slews keep the axis-space corridor. An 89° pass then tracks through the zenith (lag ≈ 2.1°, back in ≈ 6.4 s) instead of a corridor stop | user |
| 2026-10-28 | T4.2 Q17–Q18 | Satellites are followed with the gateway's own timed alt/az path (sgp4 + PWI4 refraction), not `follow_tle`; the Sun detour sits inside the path, the wrap stays under gateway control, the path ends at the Lim EL exit. Near-zenith passes (peak > 87°) are trackable with a card note (the azimuth axis lags near the peak) | user |
| 2026-10-28 | T4.1 Q13–Q16 | TLE fetched by the gateway from the analysis service by NORAD ID (`PW_ANALYSIS_URL`), the browser sends only the ID; TRACK OBJECT disabled while the object is within 15° of the Sun (later Sun crossings → D-SUN in T4.2); disabled with < 30 s left above Lim EL; stale TLEs (> 30 d) trackable with the badge | user |
| 2026-10-27 | T3.5 Q10–Q12 | D-SUN HOME routing only when the mount completed a home this session (pose trusted); otherwise skip/confirm as today. Go To: SLEW enabled with a detour note. State during a detour: SLEWING + "Avoiding the Sun" | user |
| 2026-10-27 | T3.4 user check | JOG mode always ends STOPPED (also from TRACK: jog = move to a place); STEP from TRACKING always ends TRACKING (step = offset a track; re-enable tracking if PWI4 dropped it). Jog STEP-running buttons dimmed solid, not translucent. Go To: a click anywhere in a group activates it; computed local El red for a failing celestial target; the inactive group live-updates after edits (bug: it did not). Go To + Sun guard: user-verified clean | user |
| 2026-10-27 | process | Test tiers: sub-tasks run only the touched units + new/changed sim tests once (non-sim full pytest once); the full sim suite ×2 + full browser pass only at the end of a main phase; timeboxes; progress notes; split mixed sub-tasks | user |
| 2026-10-26 | T3.4 | Q6 dry-run check; Q7 Go To modal stays open with a status line; Q8 jog while stopped = direct axis rates while held, no tracking_on, rate back to zero on release / disconnect; Q9 speeds = mount-axis rates | user |
| 2026-10-22 | T3.3 review | P18: local alt/az refracted like PWI4 (Go To must equal typing into PWI4). P19: PWI4's unwinding turn on tracking_on is intended (keeps turning room); revert the wrap centring. P19a: new stall guard (axis not moving 2–3 s while commanded → auto STOP + "home before further use"). PWI4 cfg + app settings were changed by the user to move the test Sun; kept for now | user |
| 2026-10-22 | T3.2 re-check | User re-checked the Details-modal controls: OK. PWI4 manual supplied: the Simulator has no Home commands; L-Series must home after every power cycle (P16) | user |
| 2026-10-22 | T3.2 | Home pose 85°/180° stored in the settings file (correctable); HOME blocked when its path crosses the Sun disc: on CONNECT skip home with a warning, on DISCONNECT ask "disconnect without homing?"; PWI4 start timeout 60 s (maybe 30 s later) |
| 2026-10-21 | T3.1 / O-SITE | User accepted the T3.1 review: PWI4 site for safety maths + refuse motion on site mismatch; single control lock (STOP from anyone); protocol ping/pong link-loss; predictive monitor + recovery; refuse Sun-crossing slews until D-SUN; **Sun shortcut**: skip Sun checks when `sun_alt + 15° < min(Lim EL, mount alt)`; HOME (≈85° el) subject to the Sun path check, refused when crossing until T3.5; nearest-wrap gotos; audit 90 days |
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
| P15 | **`goto_alt_az` does not end tracking**: a sim left TRACKING stays TRACKING through an alt-az goto (found at the T3.1b gate; made `test_goto_stop_state_sequence` flaky) | Local Az/El goto must end STOPPED (A5c-iv): `goto_altaz` sends `tracking_off` first (T3.1b fix); the flaky test now calls `tracking_off` before settling |
| P13 | Slews approach exponentially (slew time constant 0.5): 90° ≈ 9.5 s, 180°-ish ≈ 13.8 s | D-SUN slew-time model uses vmax/accel + the approach constant |
| P16 | **PWI4 User Manual (MAN7034 rev A, PWI4 4.1.5; user-supplied 10-22):** the Simulator mount has **no Home mount / Home axis** commands (its `find_home` is a no-op, P12). **L-Series and CDK700 mounts use incremental encoders and must be homed after every power cycle**. Homing drives to the **region sensor (≈ 60° altitude)** to find the direction, then on to the index mark | (a) HOME on the real L-350 is required, not optional. (b) The HOME Sun check predicts current pose → home pose (85/180); the real path goes via ≈ 60° alt, so the check is an approximation until measured. (c) Before the first home after power-up the reported alt/az may not be trustworthy, and every Lim EL / Sun check relies on it (decision Q3 in the T3.3 spec). (d) Real-mount checklist: record the homing path, what PWI4 reports before homing, and whether `/status` exposes a homed flag |
| P17 | Manual, other facts used by later tasks: Jog max **10°/s** (A5c-iii max matches); **mount offsets reset when slewing to a new target**; apparent RA/Dec = precession + nutation + annual aberration, **no refraction**; PWI4 **changes the Azm wrap region by itself when tracking a satellite** (cf. P8); slew time constant default 0.5 s (P13); extra PWI4 instances use ports 8220+N; PWI4 has no ASCOM MoveAxis; warranty excludes damage from software that does not implement mechanical motion limits (reinforces G1) | H4 convention = no refraction (T3.3); T3.4 jog speeds; T4.2 wrap handling (after `follow_tle` re-read the wrap range) |
| P18 | **PWI4's reported alt/az include atmospheric refraction** (T3.3 sim cross-check: PWI4 alt/az vs H4 of PWI4's own J2000 RA/Dec differ by 13–77″, matching Bennett refraction within 30″ in altitude; azimuth part < 20″). H4 and PWI4's apparent RA/Dec carry no refraction | The safety Lim EL check of an RA/Dec target (H4, unrefracted) is slightly stricter than PWI4's reported altitude (≈ 3.5′ at 15°): safe direction. T3.4 Go To modal: decide whether the computed local pair is shown refracted (as PWI4 reports) or not; real-mount check |
| P19 | **`tracking_on` after a goto that changed the axis-0 wrap window and was stopped mid-slew slews the axis back into the window (~20°/s, up to 360°)** (T3.3 sim, full suite only; reproduced with direct sim calls) | Fixed: before `tracking_on` the gateway centres the wrap window on the current azimuth (`_prepare_wrap`), so TRACK holds the current pointing (Q1 parity). Real-mount check: the same behaviour with cable wrap |
| P20 | **PWI4's own jog (Move Mount), recorded on the sim 10-27 while the user jogged:**<ul><li>the sim jog never moves the mount;</li><li>the offset-rate fields stay 0, so PWI4's jog uses an internal path that is not in the HTTP API (the public API has no jog / axis-velocity endpoint);</li><li>**a jog press on a stopped mount turns tracking on**: 5 tracking starts vs the user's 2 Track presses;</li><li>tracking ended on the user's STOPs, not visibly on release.</li></ul>**API probe:** `axis0_set_rate_arcsec_per_sec` does nothing on a stopped mount; while tracking it is exact (6.05° in 3 s at 2°/s incl. sidereal) | **T3.4 Q8 = option A (user 10-27):**<ul><li>jog from STOPPED = `tracking_on` + axis rate offset while held; on release (or a keep-alive timeout / disconnect) the rate goes to zero and the mount stops (`stop` + `tracking_off`), ending STOPPED;</li><li>before `tracking_on` the gateway checks axis 0 is inside its wrap window (P19), otherwise it refuses "Wrap out of range: re-home or Go To first";</li><li>while TRACKING: rate offsets only, tracking continues.</li></ul>**Real-mount session:** re-check PWI4's jog behaviour (does a release keep tracking? the speeds?) together with homing; the mechanics may change then |
| P21 | **Custom alt/az path (sim, T3.5a, 10-27):**<ul><li>`apply(update_wrap=0)` takes 0.06–0.11 s; the mount waits still until the first point's time, then follows the polyline;</li><li>straight lines: cross-track 0.000, mean error 0.02° at 5°/s, ≤ 0.06° at 10–15°/s;</li><li>curves add the chord sagitta (v·dt)²/(8R) + lag: r = 16° arc 0.05° / 0.21° at 5 / 10°/s with 0.5 s points; a 45° corner peaks at 0.10° / 0.23°, gone in ≈ 1 s;</li><li>a first point in the past: first-order catch-up (tau ≈ 0.5 s); a far first point: slewed to at ≤ 17°/s;</li><li>during the path `is_tracking` True, `is_slewing` False; **after the last point the mount holds the last alt/az with `is_tracking` still True** (not sidereal);</li><li>STOP freezes the sim at once (≤ 30 ms), tracking off;</li><li>**an axis-0 route crossing the wrap-window edge unwinds ≈ 346° mid-path even with `update_wrap=0`** (supervisor re-read the raw trace: +111 → −235); with the wrap min set so the window holds the whole route it follows continuously.</li></ul> | D-SUN executor:<ul><li>LEAD 2 s, first point = the current pose;</li><li>cruise 5°/s, points every 0.5 s (planner as is);</li><li>set the wrap min so the whole route's axis-0 range (+ margin) is inside the window, before apply; a route spanning ≥ 358° (`wrap_turn`) cannot fit: the path stops 2° short of the window edge and the final goto finishes;</li><li>wait route end + 2 s, then the final command (`tracking_off` is needed: the path hold reports tracking);</li><li>completion by the route clock + position, never by the flags;</li><li>real mount: STOP coasts v²/(2a).</li></ul> |
| P22 | **Satellite tracking on the sim (T4.2a, 10-28; synthetic ISS-like TLEs over the sim site; raw data `scratchpad/t42a/raw/`; supervisor re-checked the minimum altitudes and the wrap changes):**<ul><li>**`follow_tle` goes straight to the object's CURRENT position, with no altitude floor.** Pre-positioned at the 15° crossing point:<ul><li>call at t_in − 5 s: min 14.62°, acquired (< 0.05°) in 1.5 s;</li><li>call at t_in: no dip (15.06°), error 0.0006° at once;</li><li>call at t_in + 5 s: 1.5 s to acquire.</li></ul></li><li>From far away (180° in axis 0): acquired in 11.2 s; the altitude never went below the object's own.</li><li>Following error: 0.003° mean, 0.007° max against PWI4's refracted position (a constant ≈ 0.04° against geometric).</li><li>**`follow_tle` rewrites the axis-0 wrap min within 0.1 s, by tens of degrees, clamped at −300** (P17 confirmed). No unwind was seen, because the axis was already inside the new window.</li><li>**Custom altaz path along a pass** (`update_wrap=0`, wrap set beforehand): 0.003° mean, 0.015° max (0.006° after 3 s); 1 s points as good as 0.5 s.<ul><li>Path coordinates are PWI4's refracted alt/az.</li><li>Holds the last pose with `is_tracking` True after the last point.</li><li>Accepts ≥ 10 000 points in one `add_point_list` (110 ms).</li><li>**`add_point_list` + `apply(update_wrap=0)` without `new` extends a running path seamlessly**; `add_point_list` alone does nothing.</li></ul></li><li>**Near-zenith pass (peak 87.99°):** axis 0 demanded 28–29°/s and was saturated at 20.4°/s, with the same result for `follow_tle` and for a custom path. Peak error 0.62°, back < 0.05° in 3.8 s; no flip or unwind. A peak of about 89° would demand about 56°/s (not measured).</li><li>STOP during `follow_tle`: 28 ms, no coast (sim).</li></ul> | T4.2:<ul><li>a `follow_tle` hand-off must be called at t_in, not earlier, from the pre-positioned pose;</li><li>re-read the wrap after `follow_tle`;</li><li>a custom path can carry a whole pass (and its D-SUN detour) and can be extended for long tracks;</li><li>a custom path needs PWI4's refraction applied to the sgp4 alt (T3.3b model);</li><li>near-zenith passes lag near the peak;</li><li>real-mount checklist: tracking on the L-350, axis rates near the zenith.</li></ul> |

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

### T2.1 log (2026-10-21)
- **Worker delivered:**
  - `tools/build_star_data.py` producing `web/public/data/stars.json`: 8 870 stars at V ≤ 6.5, 1 625 at V ≤ 5. The file is committed; the catalogue is not.
  - `utils/sky-chart.ts`: projection, IAU 1976 precession and vectorised alt/az.
  - `utils/sky-clock.ts`.
  - `components/sky/{SkyChart,SkyClock}.vue` and `lib/sky/theme.ts`.
  - Gateway: `site.timezone` added to the snapshot.
  - Fixture generator `tools/gen_sky_fixture.py`, with the fixture at `web/tests/fixtures/sky-altaz.json` (max error 0.0064° alt / 0.0086° az).
  - `pyproject.toml` testpaths now include `tools/tests`.
- **Supervisor gate:**
  - Tests: pytest 87 passed / 13 skipped; ruff clean; web lint 0 errors; typecheck 0; vitest 258/258.
  - Star count independently recounted: 8 870.
  - **Star pixel positions vs skyfield**, 10 bright isolated stars at 1920 px, own measurement: max error **0.37 px**. E is left.
  - Layer frame rate: over 10 s, stars +10 and base +0.
  - Clock: the toggle persists across a reload. With the timezone null, Local Time = PC zone; this PC is set to UTC. With Asia/Bangkok, Local Time = UTC+7.
  - Injected pointing at 204.63/58.32 plots south-southwest, as in the render.
  - 3D Sun position cross-checked numerically: it looked "near N" only because of the camera's view from az 160°.
- **Not run:** the live sim goto follow, because PWI4 was not running. This moves to the user check.
- **Choices to show the user:**
  - No star labels.
  - Always-dark sky.
  - The Sun keep-out outline is drawn on the chart, and still drawn while the Sun is less than 15° below the horizon.
  - The Moon's lit limb is oriented toward the Sun's chart position.
  - The pointing marker is a ring plus a dot (the render's "separated circle" has a gapped ring).

### T2.1b log (2026-10-21)
- **User review of T2.1:** functionally clean. Asked for: sky colour by Sun altitude (OV:Hemisphere, bluer day) with stars fading; a broken-ring pointing marker; and the Settings save flow below.
- **Settings save flow:**
  - SAVE disabled when unchanged.
  - The warning never swaps the buttons.
  - Mismatch: the first SAVE warns (nothing saved); a location edit clears it; a second SAVE accepts.
  - Match or PWI4 unreachable: one click saves and closes. The user confirmed both paths.
  - The warning shows whenever the proposed location mismatches, whichever field changed (user-confirmed).
- **Delivered:**
  - New gateway `POST /api/mount/site-check`: read-only; checks a proposed location against the cached PWI4 site.
  - Sky colour and star-fade tables in `lib/sky/theme.ts`.
  - Test hook: `window.__skyTestSunAlt` plus the `sky-test-refresh` event.
- **Supervisor gate:**
  - Tests: pytest 88/13 skipped; ruff clean; lint 0 errors; typecheck 0; vitest 271/271.
  - Browser vs a throwaway gateway + a fake PWI4 (conftest status, site 14.3479/100.5656/12):
    - first run → 1 click saves;
    - reopen unchanged → SAVE disabled;
    - Lim EL 15→16 (match) → 1 click saves and closes;
    - lat 13.5 → warning, file unchanged;
    - edit → warning cleared;
    - lat 13.6 → warning → second click saves;
    - PWI4 stopped → 1 click saves.
    - Fake PWI4 log: 1 066 × `GET /status` only.
  - Sky colour strip at +30 / +2 / −4 / −10 / −20: blue day with no stars, peach, mauve with faint stars, violet, navy. Grid readable throughout.
  - Marker: broken ring, centre clear.
- **Noted for the user:** the marker's contrast on the daytime blue sky is low.
- **First run:** SAVE starts enabled because the PC zone is substituted for a null timezone. This is intended.

### T2.2a log (2026-10-21)
- **User decisions:**
  - DB via `POSTGRESQL_DSN` in the repo `.env`.
  - **`.env` was not git-ignored when the user added it.** The supervisor added `.env`, `.env.*` and `!.env.example` to `.gitignore` before any work started.
  - Live viewer = active payloads only.
  - This PC is the mount PC.
  - Requirements: fast first load; never stall mount operations.
- **Worker delivered:** `analysis/{db,catalog,chunks,service,priority}.py` plus endpoints `/api/sats/{status,refresh,search,{norad}}` and `/api/sky/{chunk,objects}`.
  - `db.py` is the only place the DSN is read. Postgres sessions run with `default_transaction_read_only=on`. Error text is never returned.
  - Enums are stored by name (= value).
  - Binary chunk header gains `n_seg`/`n_samples`, padded to 4-byte alignment.
  - `n_steps = ceil(d/step) + 1`, so a chunk boundary sample is shared with the next chunk.
- **Supervisor gate:**
  - **Tests and DB:** pytest 108 passed / 13 skipped; ruff clean. Live counts LEO+SSO 16 071, MEO 223, GEO+HEO 611. DB session confirmed `transaction_read_only = on`.
  - **Timing (HTTP, cold cache):**
    - LEO head 0.11 s / 86 KB; LEO full 1.82 s / 1.04 MB.
    - MEO/GEO head and full < 10 ms.
    - All three full chunks 1.86 s (target ≤ 5 s).
  - **Accuracy vs skyfield**, own float64 decoder, all samples of about 25 objects per layer:

    | Layer | Median | p99 | Max |
    |---|---|---|---|
    | LEO | 0.0008° | 0.0039° | 0.0045° |
    | MEO | 0.0004° | 0.0007° | 0.0007° |
    | GEO | 0.0004° | 0.0005° | 0.0005° |

    A first run showed 0.016° spikes. The cause was the supervisor's own float32 `arccos` quantisation in the check script, not the service.
  - **I8 / scope, against the DB:**
    - 0 of 3 151 not-live rows (non-payload / inactive / decayed) appear in any layer's objects list or chunk.
    - The default search (500, NORAD 100391–101108) contains 0 decayed.
    - GET of 5 decayed objects → 404.
    - Non-payloads remain searchable.
  - **I5:** with 33 full chunks computed back to back during a 20 s window, gateway WS 10.10 Hz (same as idle), `/status` p95 8.2 ms, analysis health max 9 ms. The analysis process and its pool child are both BelowNormal.

### T2.2b log (2026-10-21)
- **Worker delivered:** `utils/sky-chunks.ts`, `stores/sky.ts`, three layer canvases in SkyChart, and `SAT_COLORS` in `theme.ts`.
  - **Decoder:** typed-array views over the fetched buffer with no copies, per-object and per-segment lookup tables, and a unit-vector array for every sample. `positionsAt` is O(objects) with no allocation.
  - **Scheduler (`LayerScheduler`, pure):** fetches the 120 s head, then the full 1800 s chunk, and the next chunk when less than 600 s of coverage is left; evicts past chunks; flushes on a site change.
  - **Retries:** backoff from 0.5 s up to 30 s. Deviation: a 503 (catalogue loading) retries every 1 s.
  - **Web Worker:** not needed; a full LEO decode takes about 12–19 ms.
  - **Hooks:** `window.__skyTestClockOffsetMs`, `window.__skyStats`, `data-sats-*`, `data-frames-*`.
- **Supervisor gate:**
  - **Tests:** lint 0 errors, typecheck 0, vitest 286/286.
  - **Drawn pixel positions vs skyfield at 1920**, own measurement at the recorded draw times:
    - 8 LEO (including fast movers), max 0.33 px;
    - 5 MEO, max 0.17 px;
    - 6 GEO, max 0.10 px. One apparent 7 px was a neighbour inside the search box; the dot itself was within 0.3 px.
  - **First satellites after navigation (warm):** MEO/GEO 0.98 s, LEO 1.36 s (target ≤ 3 s). The worker measured a cold start (analysis just started) at about 3.5 s.
  - **Steady 30 s:** no long tasks; Mount Status 10 msg/s.
  - **Next-chunk fetch:** observed by the worker with the clock-offset hook; also covered by unit tests.
- **Marker change (user, option 2, done by supervisor):** 2 px dots became **5 px squares** (`SAT_SQUARE_PX`, scaled like the stars), filled with the class colour at 0.85 alpha with an opaque 1 px outline.
  - The outline is a lighter tint on the night disc and a darker shade on the day disc (switching at sky brightness 0.5).
  - Night, twilight (−5°) and day (+30°) screenshots: all three classes clearly visible, so **no colour nudge was needed** (the user's concern was that brighter colours would blend into the day sky).
  - Gates re-run: lint 0 errors, typecheck 0, vitest 286/286.
- **Smoother motion (user, 10-21):** positions were already interpolated between the 10 s samples; the jumps came from the 1 Hz LEO redraw (a fast mover near the zenith stepped about 2.4 px per redraw at 1920).
  - New cadences: **LEO 10 Hz** (about 0.25 px steps), MEO 5 s, **GEO 10 s** (user).
  - Measured: LEO draw 0.7 ms per frame at 1920 (0.5 ms at 1280 @1.5), about 515 objects drawn, so about 0.7 % of the main thread; no long tasks; Mount Status 10 msg/s.
  - New gate hook `data-ms-<layer>` (last draw cost).

### Open item O-FAST (user, 2026-10-21)
- Some LEO objects look unnaturally fast in the Sky Viewer. The user will check, via the T2.3 hover/card, whether the cause is bad data (e.g. stale or bad TLEs) or rendering/calculation/interpolation.
- Supervisor hypotheses: stale or bad TLEs (old epoch, SGP4 nonsense); interpolation across a sample gap (it must not); high-eccentricity objects near perigee.
- T2.3 adds TLE age to the tooltip and card data to help.
- **Supervisor diagnosis (10-21): bad data, not rendering.**
  - Method: max topocentric rate per LEO object over a real 30-min dense grid.
  - Normal objects: p99 0.90°/s; the fastest fresh-TLE objects (Starlink at about 300 km) reach 1.15°/s.
  - Outliers at 7–17°/s, which is physically impossible: HYDRA-W 63490, JILIN-1 GAOFEN 03D18 51844, STARLINK-36977 68224, INSPIRESAT 1 51657.
    - All have **TLEs 175–194 days old**, with perigee 227–289 km.
    - SGP4 far past its epoch on high-drag orbits gives nonsense; these have almost certainly re-entered but are not marked decayed in the DB.
  - **Live payloads by TLE age** (≤7 d / 7–30 / 30–90 / >90 d):
    - LEO 12211/12/1/60; SSO 3736/4/8/39; MEO 195/8/6/14; GEO 544/1/0/0; HEO 54/5/2/5.
    - 90 objects older than 30 days sit below 350 km perigee.
  - Proposal to the user: exclude TLE age > 30 d from the live viewer, keeping such objects searchable with a stale-TLE badge.
  - **User decision (10-21):** apply the 30-day cutoff to the live viewer (T2.3 item 7). Stale objects stay searchable with a **stale-TLE badge** (T4.3; also T5.1 results and the Track Info card).
- Side note: the newest `tle_epoch` in the DB is 2026-10-23, in the future relative to 10-21; some feeds publish predicted elements.

### T2.3 log (2026-10-21)
- **Worker delivered:**
  - `analysis/state.py` + `POST /api/sky/state`; the `STALE_TLE_DAYS = 30` live filter; `tle_age_days` on search/get/state.
  - Web: `utils/{filters,sky-pick,track-format,country-flag}.ts`, `stores/{filters,selection}.ts`, `components/filters/VisibilityFilters.vue`, `components/track/{TrackCard,FlagBadge}.vue`.
  - SkyChart: picking, labels canvas (greedy declutter), white selection ring, 0.22 focus dim, tooltip with TLE age.
  - Hooks: `__skyPick(x, y)` / `.positions(layer)`, `data-hover-norad`, `data-selected-norad`, `data-ms-labels`.
- **Supervisor gate:**
  - **Tests:** pytest 116 / 13 skipped; ruff clean; lint 0 errors; typecheck 0; vitest 297/297.
  - **Stale cutoff vs the DB:** live set 16 770 = an independent DB count; 0 of 135 stale payloads in the live set; the four O-FAST objects absent from live but searchable (ages 175–194 d). Max LEO topocentric rate now 1.52°/s (was 17°/s).
  - **Nearest picking:** synthetic pointer moves at 6 points in a dense GEO clump; the picker equals a brute-force nearest every time.
  - **Selection:** a click selects the clicked NORAD; the card (name, COSPAR/NORAD, chip, disabled TRACK OBJECT) updates at 1 Hz.
  - **Below Horizon:** TDRS 3 at −55.29°, live.
  - **Deselect:** a click on empty sky → "No object selected".
  - **Filters:** parent off → 0 drawn with the child checks kept; persists across a reload; parent on restores only the checked children.
- **BUG FOUND AND FIXED by the supervisor:** `/api/sky/state` was **27 s behind real time**.
  - Cause: `ts.utc(1970, 1, 1, 0, 0, t_unix)` counts the 27 leap seconds since 1972. Card az was off by up to about 7° for a LEO; the tooltip's range/illumination were affected the same way.
  - The worker's tests built their reference with the same call, so they could not catch it.
  - Fix: `ts.from_datetime(datetime.fromtimestamp(t_unix, timezone.utc))`.
  - The test reference uses an independent construction, plus a new regression test comparing state against the chunk pipeline's jday time grid along an ISS-fixture pass. It fails on the old code and passes now.
  - After the fix, browser card values lie between skyfield at t−1 s and t (poll latency) for a LEO (COSMOS 2416) and a GEO (FLTSATCOM 8).
- **Not live-checked:** the Umbra card state (no object above the horizon is in shadow near local noon); unit tests only.
- **Labels:** p95 about 3.3 ms in headless Chrome without a GPU, slightly over the 3 ms budget. Mount Status stays at 10 msg/s.
- **Open for the user:** flags load from **flagcdn.com** (as in ssa-toolkit), an external request from the mount PC and no flag offline. Proposal: bundle the flags locally.

### T2.3 follow-ups (2026-10-21, user review, done by supervisor)
- **Local assets:**
  - flags: `web/public/flags/*.svg` from flag-icons 7.2.3 (MIT, `LICENSE.txt` + README); 270 SVGs, 2.5 MB, scanned for scripts (none);
  - icons: `web/public/icons/{satellite,aircraft}.svg` (editable, README).
  - `FlagBadge` uses the local files.
  - Browser check: flags and icon load locally; **0 external hosts** in the page's resource list.
- **Track card:** data rows are indented by icon width + gap, so "Azimuth" starts exactly under the name (x 80.2 = 80.2). Label column = widest label + `column-gap: max(4ch, 2em)` (about 32 px at 1920).
- **Label flashing:** the supervisor offered sticky placement + fade + alternative positions; the user chose a simpler Labels toggle (T2.3b).

### T2.3b log (2026-10-21)
- **Worker delivered:**
  - `filters.labels` + the "Labels" checkbox (last top-level item);
  - SkyChart `showLabels` (off → only the hovered and selected labels);
  - FILTERS sizes as `clamp(min, --u·N, N)` with `--u = 100cqh/450` on the panel body (`container-type: size`), Tools `margin-top: auto`, and the Visibility fieldset `overflow-y: auto` as fallback;
  - `main` min-height lowered from 640 to 480 px (the 640 floor alone forced a page scroll at 600 tall).
- **Supervisor gate:**
  - **Tests:** lint 0 errors, typecheck 0, vitest 298/298.
  - **Page scroll == innerHeight and no panel overflow:**
    - 1920×1080: Tools 911–1047, identical to before;
    - 1280×720 @1.5: Tools 562–687 inside the panel (242–700);
    - 1280×600 @1.5: Tools 462–567 inside the panel (204–580); row font 11 px (the floor).
  - **Labels toggle:** 61 → 0 → 65 labels drawn.
  - The worker also checked hover/selected labels while off, persistence across a reload, the fallback scroll at 1280×500, and 1366×768 / 1536×864 / 1440×640 / 1280×560.

### T3.1a log (2026-10-21)
- **Worker delivered:**
  - `gateway/control.py`: `Pwi4Writer`, the only PWI4 writer; `ControlService` with a serialized worker, STOP preemption, the lock, link-loss/start-up/shutdown stops, and the `check_command` seam for T3.1b; `nearest_wrap_min`.
  - `gateway/commands.py`: strict schemas, 2 KiB message cap.
  - `gateway/audit.py`: JSONL per UTC day, `PW_AUDIT_DIR`, 90-day cleanup, secret keys dropped.
  - Link loss uses uvicorn protocol pings, `ws_ping_interval` 0.2 s and `ws_ping_timeout` 0.4 s.
  - `/dev/control` page; `sendCommand` in `mount-socket.ts` / `stores/mount.ts`.
  - **PWI4 finding:** `mount/stop` also ends tracking, but its HTTP response still shows `is_tracking=true` (stale); a fresh status read is correct. The gateway confirms stopped from position and re-sends tracking-off if needed. Needs a real-mount check.
  - `goto_radec_j2000` has no wrap and no Lim EL guard until T3.1b (P14 open until then).
- **Supervisor gate, independent client against a throwaway gateway + PWI4-sim (`sim_guard` checked):**
  - **Tests:** pytest 168 / 26 skipped; ruff clean; lint 0 errors; typecheck 0; vitest 303/303.
  - **I2 grep:** no PWI4 mount write endpoints outside `gateway/control.py` (excluding tests and the harness).
  - **STOP from a non-holder:** accepted (ack 8 ms), STOPPED 0.20 s later. A goto from a non-holder → "Another window has control".
  - **I3:**
    - holder TCP abort mid-slew → STOPPED in 0.22 s (seen by a viewer);
    - silent holder (paused reading, pings unanswered) mid-slew → 0.69 s;
    - abort mid-sidereal-track → 0.21 s;
    - still STOPPED 2 s later, lock "none", nothing resumed.
  - **Start-up stop:** a sim slew started directly in PWI4, then a fresh gateway launched → the mount stopped at az 89° (target 60°) by the time the gateway first answered health (2.5 s, Python start-up time); audit `startup_stop` confirmed in 236 ms.
  - **Dev page in a real browser:** take control → holder "you" → goto 55/120 SLEWING → STOP → STOPPED; acks listed.
  - **`PWI4.cfg` SHA-256** B7143218…1A42F0 unchanged before/after (wrap minimum is runtime-only). The sim was left disconnected.

### T3.1b log (2026-10-21 → 22, verified)
- **Worker delivered:**
  - `gateway/safety.py` (`SafetyEngine` + `SafetyMonitor`), `ephem.sun_altaz` / `radec_j2000_altaz`, radec nearest-wrap;
  - `PW_TEST_SUN_ALTAZ` test-only Sun override (inert unless set; audited);
  - path predictor validated on 12 recorded sim slews: rms 0.17° / max 0.66° without an acceleration limit. The sim ignores the reported 20°/s² (rms 5° with it), so both variants are checked.
  - Monitor stops in the worker's runs: outside-app slew with Lim EL 30 → stopped at 37.9° (0° overshoot); setting sidereal track → +0.0015° above Lim EL.
- **Supervisor gate (first pass):**
  - **Passed:**
    - pytest 193 / 31 skipped; ruff clean; web gates (vitest 303); I2 clean;
    - forged goto alt 14.9 and a below-horizon radec refused ("Below station limit elevation (Lim EL 15°)") and audited as `safety_refusal`;
    - site mismatch (Settings lat +1°) → goto refused "Station location differs from PWI4's site", STOP still accepted, goto accepted again after restore.
  - **BUG (blocks the gate):** the predictive monitor stops **legal** slews.
    - A valid goto 85 → 15.5 (Lim EL 15) was stopped at 22.97° (`limit_stop` at alt 24.5, `v` −19.5 → predicted 14.75).
    - Cause: 0.5 s look-ahead × 20°/s ≈ 10° margin, plus the lagging velocity window. The same flaw applies to the Sun-closing check.
    - Fix sent to the worker: validated gotos are monitored against actual limits plus a predicted-path corridor; the predictive look-ahead is kept for tracking and unvalidated motion; regression tests and sim runs at Lim EL + 0.5 / + 0.1.
  - **P15 found:** alt-az gotos keep tracking on. Fixed the flaky Phase-1 sim test (3/3 pass); `goto_altaz` must send `tracking_off` first (sent to the worker).
  - Supervisor test-client notes: blocking calls or a small `max_queue` in a test client stop pong replies, and the gateway (correctly) drops it as link loss.
- **PARTIAL at pause (worker report, 10-21 end of day): fix IMPLEMENTED; final sim suite not run.**
  - **Monitor fix:**
    - `SafetyEngine.check` builds a `ValidatedGoto` (both predicted paths + the start Sun separation).
    - `ControlService.goto_started` → `SafetyMonitor.begin`; `motion_cleared` (STOP / link loss / safety stop / shutdown / new command) → `SafetyMonitor.clear`.
    - While a plan is active: stop if alt < `min(Lim EL, start alt)` − 0.05°, or Sun distance ≤ 15° (or deeper than at start), or the mount is > 2° from the predicted path in axis space ("Mount left the validated slew path"). The recorded sim slews' max deviation is 0.041°.
    - The plan ends on arrival (0.2°), after 0.8 s with no motion, or at the predicted duration × 1.5 + 10 s; the predictive rules then resume.
  - **P15:** `goto_altaz` now sends `tracking_off`, then the wrap, then `goto_alt_az`.
  - **Tests:** default pytest **205 passed / 34 skipped**; ruff clean (re-run by the supervisor at pause: same).
  - **New sim tests passed when run alone (13477):**
    - 85 → Lim EL + 0.5 reached 15.5001° and → + 0.1 reached 15.1002°, with no `limit_stop`;
    - P15: ends STOPPED with tracking false.
  - **NOT YET DONE:** the full `PWI4_SIM=1` suite twice (control + safety + gateway sim tests), including re-running the earlier monitor sim tests (outside-app slew, setting track) after the fix. Then the supervisor re-gate: repeat 85 → 15.5 with an independent client, plus a slew passing about 17° from a pinned Sun via `PW_TEST_SUN_ALTAZ` on a throwaway gateway.
  - Sim left disconnected; `PWI4.cfg` hash unchanged (B7143218…A42F0, checked by the supervisor at pause).
- **Resumed and re-gated 10-22 (supervisor):**
  - **Full sim suite (control + safety + gateway):** pass 1 23/23, pass 2 15 failed. All 15 failures were the test helper `safe_target`, not the gateway:
    - it searched only 4 sweep lengths;
    - it required ≥ 35° from the real Sun from the *start* pose, but the Phase-1 test leaves the sim at az 147.7 / alt 45, 22.8° from the morning Sun (alt 38–40, az 117).
  - **Supervisor fix (`test_control_pwi4sim.safe_target`):** search sweeps 20–180° in both directions; accept a sweep that never gets closer to the Sun than it started and ends ≥ 35° clear (the same rule as gateway recovery).
  - **After the fix:** the suite passed **twice, 23/23 each** (4 min 36 s per pass).
  - **Independent re-gate** (own WS client, throwaway gateway, `PW_TEST_SUN_ALTAZ=40,180`):
    1. legal goto 85 → 15.5 (Lim EL 15) reached **15.4998°**, no `limit_stop` (the bug is fixed);
    2. a slew at alt 57 from az 150 to 210 (passing about 17° from the Sun) was accepted and completed;
    3. the same at alt 54 (about 14°) was **refused** "Slew path passes within 15° of the Sun", 0.000° motion, audited `safety_refusal`;
    4. PWI4 redirected directly mid-slew → `limit_stop` "Mount left the validated slew path" (deviation 2.59° > 2.0°) after 1.5 s.
  - **Gates:** pytest 205 / 34 skipped; ruff clean; lint 0 errors; typecheck 0; vitest 303/303.
  - `PWI4.cfg` hash unchanged; the sim was left disconnected; no throwaway listeners.



### T3.2 log (2026-10-22)
- **Worker delivered:**
  - `gateway/sequences.py`: `Sequencer` for connect/disconnect/home; detached PWI4 launcher (only when `/status` is unreachable and no PWI4.exe runs; never kills); home wait.
  - `Pwi4Writer`: connect/disconnect/enable/disable/find_home.
  - `safety.check_home` + `HomingWatch` (actual limits only while homing).
  - Snapshot `sequence`; BUSY / HOMING overrides; audit sequence events.
  - `settings.hardware.home_alt_deg` / `home_az_deg` (85/180, preserved across PUTs).
  - `components/mount/MountControlPanel.vue` + `utils/mount-control.ts`.
- **Deviations accepted by the supervisor (shown to the user):**
  - STOP in Pointing Mode is live (to cancel a home); TRACK is disabled until T3.3.
  - "CONNECTED" = mount connected AND both axes enabled; otherwise CONNECT is offered again ("Axes disabled: CONNECT enables them").
  - The skip/confirm home flows also apply to site mismatch / home pose below Lim EL.
  - The homing watch also has an actual-altitude floor `min(Lim EL, start alt)`. **To verify in the real-mount session:** it could false-stop a real home that dips low.
  - The worker's first browser runs proxied read-only satellite-chunk GETs to the user's analysis service on 8410 (Nuxt `/api` default). It switched to a closed port afterwards; nothing was started or stopped.
- **Supervisor gate:**
  - **Tests:** pytest 267 / 39 skipped; ruff clean; lint 0 errors; typecheck 0; vitest 318/318. The worker's full sim suite ran 28/28 twice.
  - **I2:** `/mount/` writes only in `control.py`; the launcher has no kill/terminate code. `PWI4.cfg` hash unchanged.
  - **Independent client on throwaway gateways + PWI4-sim:**
    - **A, real Sun:** connect → BUSY "Connecting: connect mount… / enable axes… / home mount…" → STOPPED (3.3 s); home → HOMING → STOPPED; disconnect → BUSY stop / home / disable / disconnect → **DISCONNECTED**, PWI4 reports disconnected with axes disabled (4.1 s).
    - **B, Sun pinned 75/180:** connect done with warning "Home skipped: path passes within 15° of the Sun"; home refused "Within 15° of the Sun"; disconnect → `home_blocked_confirm`; `skip_home` → stop / disable / disconnect → DISCONNECTED.
    - **C:** STOP 0.15 s after connect → sequence "Cancelled by STOP", STOPPED, no rollback.
    - Closing the holder connection mid-sequence → audited `sequence_cancelled` "Cancelled: control link lost" + `link_loss_stop`.
  - **Browser screenshots** (worker, reviewed): panel matches Main Dashboard 1 at 1920 and 1280×600 @1.5, no overflow.
  - The sim was left disconnected.

- **User check 10-22 + supervisor follow-ups:**
  - **"HOME never actuated" (user):** expected. The user's audit shows `find_home` sent and `home_finished how=no_motion` after 3 s. **The PWI4 simulator's `find_home` is a no-op (P12).** Real motion + HOMING detection wait for the real-mount session.
  - Sun-crossing slews were blocked and slews toward the disc were stopped (user confirmed).
  - **FIXED (supervisor):** the Detailed Mount Status modal's Connection / Pointing / Commands groups were still the disabled T1.4 placeholders.
    - Replaced with the live `MountControlPanel` in a fixed-height wrapper (`.md-control`, 380 px; 300 px when the viewport height is ≤ 900). One component, so the dashboard and the modal can never disagree.
    - Verified in a real browser: HOME → HOMING → STOPPED; DISCONNECT → confirm dialog stacked over the modal → DISCONNECTED; no overflow at 1920 and 1280×600 @1.5; lint / typecheck / vitest 318.
  - **`PWI4.cfg` hash changed** B7143218… → **4D752D34…** at 05:54:29 UTC, with all PWI4 config files rewritten at the same instant (`GUI.cfg`, `Simulator.Mount.cfg`, `Telemetry.cfg`, …).
    - Cause: PWI4 saving its own configuration; it was started at 05:34:55 by the user's CONNECT test.
    - The user's audit shows **no gateway command** at that moment (05:53:26 … 05:54:59), and `Pwi4Writer` only allows mount endpoints. Still `Devices.Mount = Simulator.Mount`.
    - **New baseline 4D752D34…**
    - **Rule update:** the hash only proves no-writes for unattended sim runs. When PWI4 is used by hand (GUI changes, restarts), PWI4 rewrites its own cfg. The guarantees are the `Pwi4Writer` endpoint whitelist test + the audit log.

### T3.3 log (2026-10-22)
- **Workers:** gateway (Sonnet) + web (Sonnet), in parallel against a fixed contract (snapshot `homed`, command `homed_override`, refusal strings).
- **Gateway delivered:**
  - `gateway/transforms.py` (H4; inverse iterates on the aberration offset) + `POST /api/transform` (token + Origin, strict validation, PWI4 site by default, 503 when unavailable);
  - `gateway/homed.py` (guard; session start causes: connect_sequence / already_connected / gateway_start / mount_connected_externally / pwi4_reconnect; ends on disconnect or PWI4 unreachable ≥ 3 s);
  - `homed_override`; refusals `Mount not homed` and `Mount is slewing; press STOP first`;
  - `track_start` audit; wrap centring before `tracking_on` (P19);
  - astropy added to `requirements-dev.txt` (dev only).
- **Web delivered:**
  - live Pointing Mode segments;
  - `trackDisabledReason` (priority: gateway → connected → other window → busy → axes → slewing → not homed → Lim EL → Sun with shortcut);
  - NOT HOMED chip + inline HOME / PROCEED;
  - the "Mount not homed" dialog (dashboard panel only; once per `homed.since` per window).
- **Worker gates:**
  - pytest 318 pass / 48 skip;
  - `PWI4_SIM=1` 366 pass ×2;
  - ruff clean;
  - vitest 329;
  - typecheck 0;
  - lint 0 errors.
  - Two pre-existing timing tests were flaky once under load (`test_monitor_lets_validated_goto_reach_lim_el_plus_margin[0.1]`, `test_goto_stop_state_sequence`); both passed on re-run.
- **Supervisor re-gate (independent scripts, own gateway on throwaway port 18733, Sun pinned):**
  - gates re-run green (pytest 318/48, ruff, typecheck, lint 0 errors, vitest 329);
  - H4 vs astropy spot check (60 random cases, 2010–2023) max 0.47″; round trip 0.0000″. Future dates showed 4.4″ = astropy's offline UT1 = UTC extrapolation, not H4.
  - Sim:
    - CONNECT → `homed` (sim home `no_motion`).
    - TRACK from STOPPED: J2000 drift 0.12″ over 30 s.
    - STOP: `is_tracking` false after 0.03 s, 0 drift over 3 s.
    - Parity: radec goto stopped mid-slew → TRACK held (0.20″ in 6 s; 58° from the old target).
    - `/api/transform` 200 / 422 / 401.
    - Gateway restart with the mount connected → `not_homed` (Q4); goto and TRACK refused `Mount not homed`; override → `overridden`; a second override refused.
    - Sun-blocked connect → warning + `not_homed`; HOME refused (Sun); override → TRACK refused `Within 15° of the Sun`; disconnect confirm flow.
  - Audit events present (`homed_session_start/end`, `homed`, `homed_override`, `track_start`). I2 grep clean.
  - `PWI4.cfg` unchanged across the gate.
  - **Real browser (1920 and 1280×600 @1.5):** found **one bug: the dashboard passed `home-dialog` instead of `homed-dialog`, so the not-homed dialog never opened** (unit tests covered the logic, not the page wiring). Fixed by the supervisor in `pages/index.vue`; re-run:
    - dialog after a Sun-blocked CONNECT (also at 1280×600);
    - Proceed anyway → guard cleared, TRACK enabled;
    - dialog again on the next session;
    - Escape → chip + inline HOME / PROCEED; inline override works;
    - TRACK → TRACKING → STOP on the dashboard and in the Details modal, in step;
    - no overflow (panel, page, modal wrapper 380 / 300);
    - no console errors.
  - The browser's Sun reason did not show with the test-pinned Sun because the snapshot carries the real Sun (the override is safety-only); in normal use they are the same Sun. The gateway refusal was verified on the sim.
  - Supervisor tweaks: web Sun comparison `<=` (as the gateway); `TIP_GOTO` → "Arrives in T3.4".
- **`PWI4.cfg`:** changed since the 10-22 morning baseline. PWI4's own files were rewritten 07:00–07:25 (incl. `Elmo.L350.Mount.cfg` at 07:00:58), consistent with the user looking at the L-350 device settings for the manual check; still `Devices.Mount = Simulator.Mount`. New baseline `40A4C3F4BF1168E7A7D5EFE6A8782FD2462A0B80F369A5A425C162A9D6A5578C`.
- **Known:** a window opening right after another may see a stale `not_homed` and get `Mount is not in the not-homed state` on override (harmless).

### T3.3b log (2026-10-26 → 27)
- **Gateway worker** (stopped at the usage limit before its final gates; report lost) delivered:
  - **Refraction:** Saemundsson × 0.9865 (PWI4's own scale, fitted on the sim) in `gateway/transforms.py`, tapered to 0 between −1 and −2° true alt, exact Newton inverse. H4, safety targets and the Sun (incl. the snapshot `sun` / `sun_distance`) are all in the observed frame.
  - **P19 revert:** no wrap centring before `tracking_on`.
  - **Stall guard:** `StallWatch` per axis = commanded (|dist_to_target| > 900″, or setpoint velocity > 0.05°/s) and position frozen ≤ 1″ for 2.5 s → `stall_stop` + `homed.reason = "stalled"`. Not evaluated during HOME or sequences. Stall text in `state_reason`. Refusals `Mount stalled; home it first`.
- **Web worker:** stalled chip / dialog (Home only) / reasons / status line.
- **Supervisor (Tier 2, new process):**
  - non-sim pytest 348 pass;
  - the 3 affected sim files once: 18/19 first, then 19/19;
  - web gates (typecheck 0, lint 0 errors, vitest 334).
  - **Measured:**
    - H4 (refracted) vs PWI4 0.31–0.46″ after a constant −0.70 s clock offset between PWI4's status time and ours (raw 3.6–10″);
    - P19: a TRACK after a stop outside the wrap window → PWI4 turned 359.93° in 22.9 s and landed within 0.01″; ordinary stopped gotos held (turn 0–0.02°, ≤ 0.01″);
    - stall axis 0 frozen at the +300° limit → guard 2.84 s; axis 1 frozen at the 91.1° limit → guard 2.64 s.
  - **Fixes by the supervisor:**
    - the web read the stall text from `snapshot.warning`; now `state_reason` (where the gateway puts it);
    - **`test_stall_pwi4sim[1]` depended on the real Sun** (base alt 20° or 72° picked by the real Sun, then a fixed +40°: from 20° it never reached the limit). The offset is now computed to go 9° past the axis-1 limit.
  - **Real browser:** the STALLED chip, status line with the axis, "Mount stalled" dialog with Home only, GO TO / TRACK refusals, HOME clears it.
  - Browser-script lessons:
    - closing the scripted browser while it holds control with the mount tracking triggers the (correct) link-loss stop;
    - a Sun-pinned sweep can be stopped by the (correct) Sun monitor before it stalls.

### T3.4 log (2026-10-27, first task under the test tiers)
- **T3.4a, Go To:**
  - **Gateway:** `POST /api/mount/check`. It shares `ControlService._motion_verdict` with the real command (the lock holder is not evaluated), takes no lock, writes no audit and sends no PWI4 write. Codes: `below_lim_el`, `sun_target`, `sun_path`, `not_homed`, `stalled`, … Later it also accepts `jog` / `step`.
  - **Web:** `GotoDialog.vue` + `utils/goto-modal.ts`. The red box uses the gateway codes (supervisor tweak: `below_lim_el` / `sun_target` red, `sun_path` not).
  - **Browser gate found 4 bugs, fixed by a 45-min worker:**
    1. the status lagged one edit behind (the check waited for the transform, `Promise.allSettled`);
    2. Celestial sometimes active on open;
    3. a slow first result;
    4. a format change activated the dimmed group (the widgets re-emit on sign flip / blur / format change; edits now need a real user gesture in the row).
    - The gateway's check was proven correct first with a direct API probe.
  - **Re-gate:** Local active on open; Ready at 1.2 s; az 195 → "Within 15° of the Sun" (red box); 230 → "Slew path passes…"; 120 → Ready. Each answer matches its input.
- **T3.4b, PWI4's jog:**
  - The user jogged in PWI4's Move Mount on the sim while a read-only recorder logged `/status` at 10 Hz (P20): the sim jog never moves; the offset fields stay 0; a jog press on a stopped mount turns tracking on.
  - API probe: a rate offset does nothing on a stopped mount and is exact while tracking.
  - **User decision: option A** (provisional until the real-mount session).
- **T3.4c, Jog:**
  - **Gateway:** `gateway/jog.py` `JogController`.
    - Commands: `jog` (keep-alive every 100 ms, 300 ms timeout), `jog_stop`, `step`; snapshot `jog` / `step`.
    - From STOPPED: wrap check → `tracking_on` + axis rate; release → stop_rate, stop, tracking_off, offset reset.
    - From TRACKING: rate only.
    - Steps: `goto_altaz` from STOPPED, `add_arcsec` while tracking.
    - New refusals: "Wrap out of range: re-home or Go To first", "Step in progress", "Slewing; press STOP first", "Jog in progress; release first", "Axis N at its mechanical limit", "Step goes past the axis N limit".
    - Monitor look-ahead is now speed-dependent, `max(0.5, 0.45 + v/2a)` (0.7 s at 10°/s); jogs that cannot stop before a limit are refused up front; the jogged axis counts as commanded for the stall guard.
  - **Web:** `JogDialog.vue` + `utils/jog-modal.ts`.
  - **Supervisor Tier 2:**
    - non-sim pytest 414 pass, ruff clean, web gates (typecheck 0, lint 0 errors, vitest 373);
    - `test_jog_pwi4sim.py` once, 6/6:
      - jog 2°/s → 1.998°/s (−0.1 %); stationary + not tracking 0.09 s after release;
      - keep-alive loss → stopped 0.41 s;
      - jog while tracking keeps tracking (1.1″ drift after release);
      - step 10′ stopped err ≤ 1.9″, tracking ≤ 1.8″;
      - 5°/s toward Lim EL 15° stopped at 16.88°;
      - wrap refusal;
    - real browser: hold ▶ jogs ("Jogging right at 5.0°/s") and release stops; window blur stops a held jog; the ArrowLeft key jogs and its keyup releases; a STEP 10′ moved el +0.17°; a second press during a step was ignored (pad disabled); the Go To checks above; no clipping at 1920 / 1280×600 @1.5 (the jog dialog's guide line touches the edge = scrollWidth flag only).
- **Process note:** T3.3b's remaining verification + all of T3.4 fitted in one session under the new tiers.

### T3.4d log (2026-10-27, user-check fixes)
- **Gateway:** JOG always ends STOPPED (also from TRACKING); STEP from TRACKING ends TRACKING (`_resume_tracking`, wrap check, `step_end.tracking_resumed`). Sim (changed tests once): jog from TRACKING → STOPPED 0.094 s after release; steps ≤ 2.1″, tracking step ends TRACKING.
- **Web:**
  - solid dimmed jog buttons (no opacity; the guide line no longer shows through);
  - Go To group activation on any pointerdown in the group;
  - a red computed Local El when the Celestial target fails (a `.gt-dim input` rule out-ranked `.is-invalid`).
- **Item 4 root cause (supervisor):** the Nuxt **dev proxy** sent `/api/transform` to the analysis service (only `/api/mount/*` went to the gateway), so the computed pair froze in dev. The unit tests use a fake API, so they could not see it. Fixed in `web/nuxt.config.ts` (`/api/transform` → gateway).
  - **Lesson:** any new gateway route outside `/api/mount` needs a dev-proxy entry. The browser gate is the only check that sees the proxy.
- **Real-browser re-gate:**
  - RA/Dec follows each az edit and the 1 Hz refresh;
  - clicking Celestial → active + Local El red for the Sun target;
  - jog from TRACKING → STOPPED; step from TRACKING → TRACKING;
  - dimmed pad screenshot OK.
- **Gates:** typecheck 0, lint 0 errors, vitest 381, ruff clean, test_jog 42.
- **Audit check of the user's session:** the user's "steps disable tracking" came from two explicit STOPs (05:23:01 / 05:23:07Z) before the steps; the tracking steps themselves kept tracking. The rule is now guaranteed anyway.

### T3.4e log (2026-10-27, azimuth wrap handling, option B)
- **Gateway** (`gateway/jog.py`, `control.py`, `safety.py`, `snapshot.py`, `commands.py`, `app.py`; new `tests/test_wrap.py`):
  - **Wrap slide:** during an axis-0 jog the window is re-centred (`pos − 180`) when the axis comes within 40° of its edge; at most every 0.5 s. If the write fails, the jog is released.
    - Sim check first: re-centring while tracking + rate offset does not jump. Without the slide PWI4 swung back at ~30°/s.
  - **Mechanical limit:** the jog ends at `limit − (stopping distance + 2°)` (9° at 10°/s) → snapshot `jog_limit {axis, dir, limit_deg, turn_deg, est_s}`.
    - A jog that way again → "Azimuth at its mechanical limit: turn 360° or jog the other way" (`az_limit`).
  - **Turn:** `wrap_turn {}` = goto-like (tracking_off, wrap window set to `target − 180`, `goto_alt_az` to the same alt/az); guarded like a goto incl. the Sun path (`TODO(T3.5 D-SUN)` in safety.py).
    - Snapshot `turn {from_deg, to_deg}`. "Turn in progress; press STOP first". "No room for a 360° turn".
  - **Cancel:** `wrap_turn_cancel {}` = stay.
  - **Supervisor change:** a jog from STOPPED (and the tracking-resume after a tracking step) with axis 0 outside the wrap window now **re-centres the window first** (audit `wrap_recentre`) instead of refusing "Wrap out of range". Found in the browser: after a reconnect PWI4's window reset left the axis at 255° outside [−180, 180], and every jog was refused. Tests updated.
- **Web:**
  - the "Azimuth limit reached" prompt (Turn 360° / Stay here), with the pad dimmed + "Turning 360° to …" while turning;
  - supervisor fix: drop the hold and clear refusals when `jog_limit` appears ("Another command is still running" showed from refused keep-alives).
- **Measured:**
  - **Worker sim:**
    - 10°/s across the old edge: max 11.2°/s, no swing;
    - limit stop at 292.25° (margin 9°);
    - turn 292.25 → −67.75° in 23.1 s, errors ≤ 1.3″;
    - STOP mid-turn cancels;
    - a pinned Sun on the sweep → refused.
  - **Supervisor:**
    - the re-centre sim test (moved 3.15°, max step 0.096°);
    - full non-sim pytest 447 pass; ruff clean; web gates (vitest 385);
    - real browser: hold → right at 10°/s from 255° → stopped 291.7° → prompt (text OK) → Turn → "Turning 360° to −68.3°…", pad dimmed → same pointing → jog works after.
  - `PWI4.cfg` unchanged.
- **For T3.5:** route `wrap_turn` through D-SUN like gotos. Known edge: a failed `wrap_turn` after the wrap write leaves the window moved (now harmless: the next jog re-centres).

### T3.5 log (2026-10-27)
- **T3.5b, planner** (`gateway/dsun.py` + `tests/test_dsun.py`, pure). The worker took ~11 min.
  - Sides are "cw" / "ccw" (by bearing from the Sun).
  - An endpoint inside 16° (but outside 15°) uses its radial projection onto the circle.
  - An arc is discarded for: Lim EL + 0.5, the keyhole (88° / axis-1 max), the axis-0 range, a different axis-0 winding (this catches the over-zenith arcs), or a rate limit (0.8 × vmax).
  - Timing: trapezoidal, with corners > 30° slowed to 1°/s; resampled every 0.5 s.
- **Supervisor Tier 2:**
  - 22 tests pass, ruff clean; the full non-sim pytest by the worker: 430 pass.
  - **Independent check** (3000 random slews, interpolating between route points):
    - min Sun separation 15.95° when the endpoints are ≥ 16.2° from the Sun; 15.13° when an endpoint itself is ~15.1° (by design);
    - max axis rate 5°/s; plan time ≤ 35 ms.
  - **Found:** about 20 % of the predictor-flagged slews have a clear straight line (the predictor's per-axis dog-leg crosses, the line does not), where the planner returns None.
  - **Fixed:** `plan_route(..., force=True)` returns the timed straight line (test added). T3.5c must use it and never send a plain goto for a flagged slew.
- **T3.5a, sim probe** (worker ~18 min; scripts and raw JSON in scratchpad `t35a/`): finding P21.
  - Supervisor check: `PWI4.cfg` unchanged (40a4c3f4…); PWI4 left stopped, tracking off, wrap min restored.
  - The wrap-edge unwind was confirmed in the raw trace.
- **T3.5d, web texts** (worker ~3 min):
  - `goto-modal.ts`: `parseCheck` detour, `detourLine`, `isRouting`, "Avoiding the Sun…" follow line; `sun_no_detour` without the red box;
  - `GotoDialog.vue` passes `routing`; `mount-control.ts` type `route`.
  - **Deviation:** the Jog modal's Turn 360° prompt sends `wrap_turn` without a dry check, so no "+N s" note there; a `sun_no_detour` refusal already shows.
  - The panel already shows `state_reason`.
  - Supervisor Tier 2: diff reviewed; typecheck 0, lint 0 errors (11 old warnings), vitest 105 in the 5 touched specs.
  - The real browser pass waits for T3.5c (it needs the gateway).
- **T3.5c, gateway wiring** (worker ~32 min):
  - **Files:** `safety.py` (planner call + `force`, `RoutedGoto` / `_routes`, `R_NO_DETOUR` → `sun_no_detour`, dry run `sun_detour` + `detour`, `final_check` of the short final leg, route plan flag + `begin_route`, HOME routing only when `homed`); `control.py` (custom-path writer methods, `route_wrap_plan`, `fly_route` / `_route_job` / `_route_end` / `fly_home_route`); `app.py` (hooks, `/api/mount/check` `detour`); `sequences.py` (`_home` flies the route, re-checks, then `find_home`); `snapshot.py` `RouteInfo`; `state.get_override`; `jog._watch_turn` does not time out during a route.
  - **Tests:** new `test_dsun_wiring.py` (17) and `test_dsun_pwi4sim.py` (4); 3 old tests now expect a detour instead of `sun_path`.
  - **Worker additions** (not in the spec, accepted):
    - the ack is sent when the route starts (the 5 s ack timeout);
    - while a route flies, other motion is refused with "Avoiding the Sun; press STOP first" (code `slewing`);
    - the route is refused with "Mount moved since the check; try again" when the pose moved more than 2° since the check;
    - wrap: a route spanning ≤ 357° uses `w = lo − 1`; a longer one (360° turn) is cut 2° short and the final goto finishes it;
    - celestial routes go to the alt/az at plan time, and the final `goto_ra_dec_j2000` closes the drift (0.1°).
- **Supervisor Tier 2:**
  - diff reviewed against the §6 contract;
  - ruff clean; full non-sim pytest 448 pass;
  - `test_dsun_pwi4sim.py` once: 3/4. **`test_no_detour_is_refused` failed:** a test bug, the target was exactly 180° away, so PWI4 may sweep the other way (away from the Sun) and a plain goto is correct (supervisor reproduced it: predictor min 21°). Fixed with a 170° target; it then passed.
  - **Sim numbers** (matching the worker's):
    - local goto through the pinned Sun: min Sun separation from `/status` **15.938°** (plan 16.0), arrival 0.0006°, motion 20.6 s vs plan 19.4 + 2 s settle, SLEWING mid-route, ends STOPPED with tracking off;
    - celestial: 15.939°, ends TRACKING, 0.109° pointing error;
    - STOP mid-route frozen in 0.063 s, no final goto sent.
  - `PWI4.cfg` unchanged; PWI4 left stopped, tracking off.
- **2026-10-28, closing T3.5:**
  - **T3.5e, sim tests** (worker): `test_wrap_turn_is_routed`, `test_home_is_routed_when_homed`, `test_home_not_routed_when_overridden` added to `test_dsun_pwi4sim.py`.
    - **Product bug found + fixed** (`jog._watch_turn`): during a route the turn watcher's clock under-counted, so a truncated turn was reported done before its final 2° goto had moved (end 0.17° off). Now `t0` follows the clock while a route flies, and the end error is 0.0002°.
    - Supervisor run of the 3 tests, once: pass.
      - Routed turn: min Sun separation 15.946°, plan 75 s / total 83.5 s, end error ≤ 0.0004°, axis 0 −68 → 292 with no reversal.
      - HOME routed only when homed: min separation 15.947°, final pose ≤ 0.008°.
  - **T3.5f, Jog prompt check** (worker): one dry check of `wrap_turn` per prompt; "Turn detours around the Sun (…)" or the refusal with Turn disabled.
  - **Real browser pass** (supervisor, throwaway Nuxt 13270 + gateway 18733, Sun pinned; 1920 and 1280×600):
    - Go To: "Path detours around the Sun", SLEW enabled, no red box → "Avoiding the Sun…" → "Arrived (STOPPED)";
    - no-detour: the reason shown, SLEW disabled, no red box;
    - Jog limit prompt with the detour note → the routed turn → STOPPED;
    - no page errors, no overflow.
  - **Supervisor fixes from the pass:**
    1. "+N s" read as extra time but N is the whole route → "(about N s)"; the turn prompt drops its straight-turn "about 20 s" when a detour applies;
    2. the panel status line said "TRACK: Slewing" during a route → "TRACK: Avoiding the Sun" (Q12).
    - Tests updated; web typecheck 0 / lint 0 errors / vitest 106; re-run in the browser: OK.


### Phase 3 end, Tier 3 (2026-10-28)
- **Setup notes:**
  - The user moved the PWI4 sim site and the app location (lat 0.12, lon 115.92) so the real Sun stays high.
  - The first full sim run (27 failures) was invalid: the user's own 8400 gateway was running against the same sim, and its (correct) monitor stopped the tests' slews. Rerun after the user stopped it.
  - `PWI4.cfg` was rewritten by PWI4 at the user's site change (03:53:59Z, before every Tier 3 run): **new baseline SHA-256 `4406df60…e2683a2`**.
- **Found and fixed:**
  1. **Monitor, false Lim EL stop near the zenith (product):** the sim lets axis 1 reach 91°. Passing 90° flips the REPORTED azimuth by 180°, the monitor's v_az spiked, and the speed-dependent look-ahead grew to ~16 s: a stop at alt 87 with v_alt −6 (predicted −12°). `SafetyMonitor.velocity` now uses the axis-0 position (continuous, physically bounded); unit test added.
  2. **Monitor, plan ended too early (product; the flaky unit test `test_monitor_lets_validated_goto_reach_lim_el_plus_margin[0.1]`):** a validated goto's watch ended within 0.2° of the target while still descending, handing a target at Lim EL + 0.1 to the predictive rule with a lagging velocity. It now ends on arrival only when settled (≤ 0.05°/s). Under parallel load: before 2/6 failed, after 12/12 passed.
  3. **Tests still depending on the real Sun** (now high at the new site): `test_stall_pwi4sim` (alt 20 base when the Sun is high → the gateway's correct predictive stop on the descent; now a fixed 45/150) and `test_monitor_stops_setting_sidereal_track` (a real-Sun precondition, although the gateway's Sun is pinned; removed).
  4. **Tests out of date since D-SUN:** `test_jog_pwi4sim::test_wrap_turn_refused_when_the_sun_is_on_its_path` → `..._is_routed` (accepted as a detour, STOP ends it); `test_safety_pwi4sim::test_sun_refusals_with_test_override` (path crossing → routed).
- **Results** (product code final before both runs):
  - **full sim suite: run A 56/56 (20.7 min); run B 55/56** (the failure was fix 3's real-Sun precondition; the fixed test then passed);
  - harness tests 17/17; PWI4 harness all scenarios ok (`tools/pwi4_harness/out/20261028T062219Z`);
  - non-sim pytest 458; ruff clean; web typecheck 0, lint 0 errors (11 old warnings), vitest 393/393.
- **Full browser pass** (throwaway Nuxt 13270 + gateway 18733, 1920 and 1280×600 @1.5):
  - not-homed chip/dialog → Proceed → Details → TRACK/STOP → Go To local (Arrived) and celestial (Tracking) → Jog hold (STOPPED) / Step → HOME (HOMING → homed) → DISCONNECT (confirm, BUSY steps) → CONNECT (BUSY, homes) → stall (STALLED — HOME REQUIRED, Home-only dialog) → HOME clears it;
  - D-SUN screens from the 10-28 T3.5 pass; no page errors;
  - at 1280×600 @1.5 the Details modal scrolls inside its frame (controls reachable); the jog dialog's guide line touches the edge (scrollWidth flag only, as in T3.4).
- **Browser-script lesson:** wait ≥ 3 s after closing a scripted browser that held control before driving the sim directly (its correct link-loss stop).
- **Flaky list:** `test_monitor_lets_validated_goto_reach_lim_el_plus_margin[0.1]` fixed (fix 2). `test_goto_stop_state_sequence` passed in all 3 full runs (A, B, the diagnostic 1b): closed.
- **Slowest sim tests** (trim later, not blocking): `test_wrap_turn_is_routed` 109 s, `test_track_from_stopped_is_sidereal_and_stop_holds` 102 s, `test_h4_vs_pwi4_reported_altaz` 84 s.

- **D-SUN speed-up (user 10-28, after the Tier 3 run):** the 5°/s route felt too slow.
  - **User choice:** arcs 10°/s, straight parts 15°/s (`dsun.ARC_CRUISE_DEG_S` / `CRUISE_DEG_S`; per segment, from `_assemble`'s arc flags), points every 0.25 s (`POINT_DT_S`), end wait 1 s (`control.ROUTE_SETTLE_S`).
  - The axis-rate cap (0.8 × vmax), the acceleration ramps, the corner slowdown and the monitor rules are unchanged.
  - **Checks:**
    - unit tests updated where they hard-coded the old speed (96 D-SUN / wrap / check tests pass); non-sim pytest 458; ruff clean;
    - supervisor random check (3000 slews, between-point interpolation): min Sun separation 15.949°, max rate 15.00°/s, plan ≤ 34 ms;
    - D-SUN sim tests + the two routed-path sim tests once: 9/9. Local detour **10.1 s planned / 11.4 s actual (was 19.4 / 20.5)**, min Sun separation 15.882° (target ≥ 15.5), arrival 0.0002°.
  - **Real-mount checklist:** custom-path following at 10 / 15°/s with the real acceleration.

### T4.1a log (2026-10-28)

**Worker (Sonnet), about 19 min:**
- New: `gateway/tle.py` (`TleSource`), `gateway/passes.py` (`compute_pass`, `refresh_now`, `track_verdict`), `gateway/trackcheck.py` (`TrackChecker`, the pass cache).
- Edited: `config.py` (`analysis_url` / `PW_ANALYSIS_URL`), `commands.py` (`track_object`, a motion command), `control.py` (homed-gated; the slewing refusal like `tracking on`; WS refusal "Object tracking arrives in T4.2" / `not_available`), `jog.py` (a jog / step / turn in progress refuses it), `safety.py` (site gate only), `ephem.py` (`timescale`), `app.py` (`CHECKABLE`, the check fields).
- 59 new unit tests (passes 21, tle 13, check endpoint 25). Full non-sim suite 546 passed / 67 skipped; ruff clean.
- **Deviations (accepted):**
  - The coarse 12 h scan uses sgp4 + a GMST rotation (≈ 0.005° from skyfield; skyfield costs 35 µs per sample). Every crossing and the peak is then re-evaluated with skyfield on a 0.5 s grid.
  - Below the horizon: no next pass is searched.
  - Row 5 also counts "falling now" as past the peak (never-setting objects).
  - `track_verdict(info, now, lim_el)`.
  - The Sun comes from `safety.sun_altaz`, so `PW_TEST_SUN_ALTAZ` works.
  - Leap-second bug caught: `_times` builds skyfield times from the array's own day.

**Supervisor Tier 2:**
- **Diff review vs the spec:**
  - row 0 wins, and no TLE fetch is made then;
  - other checkable commands keep their old body;
  - the WS refusal comes before the lock / queue;
  - thread-safe caches.
- Re-ran the new + touched tests (139 passed) and ruff (clean).
- **Spot check** (supervisor script, session scratchpad): throwaway analysis on 18710 with the local DB, PWI4 sim site (0.12, 115.92), Lim EL 15, no PWI4. Independent skyfield brute force (1 s + interpolation, peak at 0.1 s):

| Object | NORAD | Verdict | Crossings vs ref | Peak vs ref | First / cached ms |
|---|---|---|---|---|---|
| LEO above Lim EL | 25982 | ok, `track` | rise / in / out / set 0.00 s | 0.01 s, 0.000° | 45 (incl. TLE fetch) / 4.0 |
| LEO low, rising | 2872 | `peak_below_limit` (peak 12.30°) | rise / set 0.00 s | 0.01 s, 0.000° | 14.5 / 3.9 |
| LEO below horizon | 900 | `below_horizon` | — | — | 9.4 / 3.9 |
| MEO (never sets in 12 h) | 2866 | ok, `track` | — | peak at the 12 h window edge (ref grid 10 s), 0.005° | 19.6 / 3.9 |
| GEO class | 20253 | ok, `track` | — | 0.01 s, 0.000° | 20.0 / 3.9 |

- **Notes for T4.2:**
  - An object with several Lim EL dips inside one above-horizon pass (an HEO) can report a `t_out` before now. `track_verdict` would then say "Pass ends in under 30 s". This is rare; T4.2's track segments should take the next `t_out` after now.
  - The pre-position case (rising below Lim EL, peak above it) was not live during the spot check. It is covered by the unit tests and will be seen in the T4.2 sim passes.

### T4.1b log (2026-10-28)

**Worker (Sonnet), about 2 min:**
- New: `web/app/utils/track-verdict.ts` (`parseTrackVerdict`, `UNREACHABLE_VERDICT`) and `tests/unit/track-verdict.spec.ts` (13 cases).
- `TrackCard.vue`:
  - polls `POST /api/mount/check` (`track_object`) at 1 Hz through `useGatewayApi` (5 s request timeout; a tick is skipped while one is in flight; a generation counter drops answers of a stale selection);
  - button enabled (green gradient) or grey, with `data-enabled`;
  - the reason as the tooltip and a muted one-line `track-reason` (ellipsis);
  - the click is a no-op until T4.2.
- typecheck 0, lint 0 errors (11 old warnings), vitest 406.

**Supervisor Tier 2:**
- Diff review OK.
- **Browser pass** (supervisor script, session scratchpad `t41b_gate/`):
  - throwaway analysis 18710 (local DB) + gateway 18733 (`PW_ANALYSIS_URL` → 18710, Sun pinned −30°) + Nuxt 13270;
  - the PWI4 sim connected and stopped as test infrastructure, disconnected again afterwards;
  - objects picked by the pure rules on real TLEs; selection through the Pinia store.
- **Results:**
  - not homed → "Mount not homed";
  - after Proceed:
    - LEO above Lim EL enabled;
    - LEO rising below Lim EL (pre-position) enabled;
    - GEO-class enabled;
    - "Below the horizon";
    - "Pass peaks below the station limit elevation";
  - a quick switch never shows the previous object's verdict;
  - analysis stopped + an uncached NORAD → "Satellite data unavailable";
  - gateway stopped → "Gateway not responding" (≤ 12 s through the hanging dev proxy, 5 s request timeout);
  - no page overflow, no console errors.
- **Fix (supervisor):**
  - Problem: at 1280×600 @1.5 the card (`--u` at its 9 px floor) ran past the Track Info frame. It overran by 5 px without the reason line, which was already the case before T4.1, and by 17 px with it.
  - Fix: an `@container (max-height: 160px)` block tightens the card gap / padding.
  - Result: card bottom = frame bottom (179 / 179) with the reason line. 1920 is unchanged (screenshots checked).
- Re-ran typecheck 0, lint 0 errors, vitest 406.

### T4.2a / T4.2b log (2026-10-28)

**T4.2a probe (Sonnet, about 55 min):** finding P22. Supervisor re-check from the raw data:
- minimum altitude in the 15 s after the `follow_tle` call: 14.62° (t_in − 5 s), 15.06° (t_in), 15.65° (far);
- the wrap min changed at the call;
- the sim was left disconnected with wrap −180, and `PWI4.cfg` is unchanged.

Its scripts were moved out of the repo (session scratchpad) so `ruff check .` stays clean; the raw data stays in `scratchpad/t42a/raw/`.

**T4.2b planner (Sonnet, about 27 min):**
- New `gateway/trackplan.py`: `plan_track`, `extend_track`, `TrackPlan`, `TrackRefusal`, `TrackMount`, `TrackSetup`, `mount_from_status`, `slew_estimate_s`.
- New `gateway/tests/tlegen.py` (fixed `PEAK_70` / `ZENITH_89` TLEs) and `test_trackplan.py` (27 tests).
- Row 7 in `TrackChecker` (plan cached 10 s, replanned on > 0.5° pose change); `SafetyEngine.sun_altaz_at`.
- The check adds `detour {seconds, count}`, `notes`, `pass.t_r`.
- Non-sim 577 passed, 1 timing flake (jog keepalive, on the flaky list).
- **Accepted deviations:**
  - arcs follow the Sun's motion (exactly 16° all along);
  - the arc floor / ceiling relaxes to the arc's own end altitudes;
  - a pass ending within 15° of the Sun is refused;
  - long finite passes are chunked too (30 min);
  - chunks never end inside a Sun interval;
  - whole-second times.

**Supervisor Tier 2** (independent skyfield + `refract_alt` check, `PEAK_70` / `ZENITH_89` at the test site):

| Case | Plan ms | Track error vs skyfield + refraction | Min Sun separation | Notes |
|---|---|---|---|---|
| 70° pass, pre-position | 14 | 0.00000° | — | `t_r` = t_in + 4 s, by design (rendezvous ≥ Lim EL + 0.5); `t_end` = t_out exactly |
| 89° zenith pass | 18 | 0.00000° | — | zenith note; an axis-0 step of 55° per 0.5 s near the peak (demand ≫ 20°/s; the lag is accepted under Q18) |
| Sun on the track after the peak | 34 | ≤ 0.0011° | 16.000° | ccw 50° arc, rates ≤ 1.0°/s |
| Sun on the track before the peak | 30 | ≤ 0.0021° | 16.000° | ccw 50° arc |
| Sun 3° off the track | 42 | ≤ 0.0004° | 16.000° | ccw 48° arc |
| Already up (now = peak − 30 s) | 10 | 0.00000° | — | `t_r` = now + 7.3 s |

- The corridor metric is axis-space distance to the polyline (not to the time point), so a zenith lag should not trip it. The near-zenith sim test was added to the T4.2d table to prove it.
- Touched tests re-run: 142 passed; ruff clean.

### T4.2c pause (2026-10-28, end of day)

- **Paused at about 10 %.** The user asked to stop for the day; the worker paused at a safe point.
- **Changed so far:** `gateway/trackcheck.py` only. `TrackCheck` gained `plan` / `setup` / `tle` (executor use, not in the check body), and `evaluate(..., fresh=False)` can skip the plan cache to re-plan after the rendezvous slew.
- **Supervisor check:** `import gateway.app` OK; `test_check_track` / `test_trackplan` / `test_check` 85 passed; ruff + format clean; no processes left.
- **Settled design** (full text in `scratchpad/t42c/notes.txt`):
  - **Shared verdict:** `ControlService.track_verdict(cmd, conn_id, fresh)` = row 0 + a `track_eval` hook set by `app.py`. The check endpoint uses it with `conn_id` None; the real command runs it in a thread.
  - **Clock seams:** `track_clock` / `track_sleep`.
  - **Rendezvous goto:** an internal `goto_altaz` with `target0` (the plan's axis-0), through the normal job + D-SUN routing. The wrap min is set to `target0 − 180`, because the planner's axis-0 turn can differ by 360° from the nearest turn.
  - **Arrival and re-plan:** arrival = within 0.3° and still. Late → re-plan once with `fresh`; one short extra goto if the new rendezvous is > 0.5° away.
  - **Upload:** `tracking_off` → wrap from `route_wrap_plan` → monitor `begin_route(route=True)` (look-ahead off) → `new` / `add_point_list` (jd = unix / 86400 + 2440587.5) / `apply(0)`.
  - **Long tracks:** extend (add + apply) when < 20 min remain, checked at least every 10 min. A refused extension ends the track at the chunk end. `SafetyMonitor.extend_route`.
  - **Ending rules:**
    - STOP / link loss / limit → `track_stopped`;
    - Go To / `track_object` / sidereal TRACK / HOME / disconnect → `track_replaced`;
    - jog / step / wrap_turn → "Object tracking; press STOP first" (`object_tracking`);
    - "Track ended at Lim EL" override, kept until the next command; snapshot `ended` for 30 s.
  - **Snapshot `track`:** `{norad, name, phase, t_r, t_end, t_peak, peak_alt, detours[{t_a, t_b, side}], notes, ended}`.
  - **Audit:** start / done / stopped / replaced / replan.
  - **`tests/tle_stub.py`:** `http.server` thread.
- **Next steps (in order):**
  1. `target0` (`safety` / `control`).
  2. Verdict refactor + `app.py`.
  3. Snapshot + poller.
  4. `trackexec.py`.
  5. Ending rules.
  6. `tle_stub.py`.
  7. Tests.
  8. Full non-sim pytest + ruff.
- **Accepted deviation:** in steady state a long track is extended every ≈ 30 min (a new chunk when < 20 min remain), not every 10 min.

### T4.2c / T4.2d / T4.2e log (2026-10-29)

**T4.2c executor (fresh Sonnet worker, about 27 min, from the pause notes):**
- New `gateway/trackexec.py` (`TrackExecutor`), `tests/tle_stub.py`, `tests/test_tle_stub.py`, `tests/test_track_exec.py` (20).
- Edited: `control.py` (`track_verdict` shared by the check and the command, `_track_command`, `target0` goto + `_goto_wrap`, the ending rules), `safety.py` (`target0`, `extend_route`), `jog.py` (`object_tracking` refusal), `snapshot.py` / `poller.py` (`track`), `app.py`.
- Supervisor: diff + executor read against the design; the full non-sim suite 600 passed; ruff clean.

**T4.2e web (Sonnet, about 3 min, in parallel):**
- `utils/track-state.ts` (`trackCardState`, `phaseLine`, `fmtCountdown`); the card click → `track_object`;
- TRACKING label + phase line + countdown; ended reason; zenith note;
- Jog pad disabled "Object tracking; press STOP first";
- vitest 416.

**T4.2d sim tests (Sonnet, about 35 min):** `gateway/tests/test_track_object_pwi4sim.py`, 10 tests. Worker run 8/9, failing at 89°.
- **Supervisor fixes after the worker:**
  1. **Q19 (user):** the object-track corridor is measured on the sky (`corridor_distance(..., sky=True)`, `ValidatedGoto.sky`, `begin_route(sky=)`); unit test `test_track_corridor_is_measured_on_the_sky_near_the_zenith`.
  2. **Rendezvous:** the fixed-point iteration was replaced by a scan for the first reachable grid time (`trackplan.reachable`). Near the zenith the iteration oscillated onto unreachable points → "Missed the pass". The planner spot check is unchanged.
  3. **The `pwi` fixture** compares `PWI4.cfg` by content only (PWI4 re-saves the same bytes on a disconnect).
  4. **The Sun-row test** pins the Sun 20 s after the peak of a pass rising 90 s ahead. Pinned "75 s from now", it fell before t_r when the rig was slow, and the planner correctly moved the rendezvous past the interval.
  5. **The zenith recovery limit** is 5 s at 88° and 8 s at 89° (≈ 180° of axis 0 at 20°/s against a 3–4 s transit).
- **Supervisor full re-run** (Tier 2), 9/10 → the Sun row fixed (item 4) and re-run → **10/10**:

| Row | Result |
|---|---|
| normal | following error ≤ 0.0065°, min alt 15.061°, STOPPED ≈ 1.1 s after `t_end` |
| rising | waiting still at the crossing point; following error ≤ 0.0047°; min alt 15.61° |
| peak below | refused `peak_below_limit`, no motion |
| through the Sun | detour 61.8 s; separation 15.998° in the detour, 15.83° minimum over the run |
| STOP | 0.08 s |
| Go To | `track_replaced` |
| wrap | constant after the upload |
| replace | ok |
| zenith 88° | peak error 0.62°, back in 3.9 s |
| zenith 89° | peak error 2.11°, back in 6.4 s, no corridor stop |

**Supervisor browser pass** (throwaway gateway 18733 + TLE stub + Nuxt 13270, PWI4 sim; script `t42_gate/` in the session scratchpad):
- select → TRACK OBJECT enabled → click → TRACKING "Slewing to the pass" (SLEWING);
- → "Waiting for the pass — 01:03 … 00:06" (TRACKING);
- → "Tracking — ends in 04:12 … 00:02";
- Jog modal "Object tracking; press STOP first";
- → STOPPED, "Track ended at Lim EL";
- audit `track_start` / `track_done`;
- 1920 and 1280×600 @1.5: the card inside its frame (177 / 179), no overflow, no console errors;
- the sim was left disconnected, `PWI4.cfg` unchanged.

### T4.2f log (2026-10-29, the user's live check)

**User check:** all listed cases clean. Two findings:

1. **MEO high in the sky refused "Pass ends in under 30 s" (bug).**
   - Galileo 40890 at site 13.12 / 100.92, Lim EL 10, 05:46 UTC.
   - The above-horizon span was 16 h with two humps (a 14° hump, then a dip to 0.3° without setting, then the 80° hump). `compute_pass` took the first Lim EL in/out inside the span, so `t_out` was 7.8 h in the past.
   - The T4.2b note had flagged this as an HEO corner; MEO shows it is common.
2. **GEO with the Sun drifting closer refused "no safe detour" (too strict).**
   - THAICOM 8: the blocked stretch is about 2 h, which the 30 min chunking could not fit.

**Fix (Sonnet, about 14 min):**
- **`passes.py`:** Lim EL crossings are relative to now (the last up-crossing before now / the next down-crossing after now; below Lim EL: the next up-crossing before the set). Row 5 "setting" no longer fires while an up-crossing lies ahead.
- **`trackplan.py`:** a blocked stretch > `SUN_HOLD_S` 600 s ends the plan at the 16° entry (`end_reason` "Track ended: Sun approaching", note "Sun blocks the track in N min"). A rendezvous inside such a stretch is refused `sun_no_detour`.
- **`trackexec.py`:** the plan's end reason is used for the override / snapshot / audit, and `t_end` is the stop time.
- 613 non-sim tests passed.

**Supervisor re-check** (independent repro with the real TLEs, throwaway analysis 18710):
- **Galileo:** allowed 05:40–08:40, `t_out` ≈ 3.6 h ahead at 05:46 (the worker's brute force agrees ±1 s); `peak_below_limit` only at 09:40 (7.3°).
- **THAICOM:**
  - allowed 05:40–05:55 with "Sun blocks the track in 18 … 3 min" (16° entry within 0.07 s of a brute force);
  - refused at 05:58 (< 30 s) and 06:01 (inside 16°);
  - refused `sun` while within 15°;
  - allowed again from 07:40.
- Touched tests 110 passed; ruff clean.
- The THAICOM planning takes about 280 ms (a long Sun lattice); it is cached 10 s, so acceptable.
- **Sim re-run of `test_track_object_pwi4sim.py` still owed:** it needs the user's 8400 down; it is part of the Phase 4 end (Tier 3).
- **User re-check 10-29: cleared.** Open item, noted by the user, fix only if required: a GEO whose position the Sun disc is about to cover (the 15–16° band, the rendezvous inside the long Sun stretch) shows "Pass passes within 15° of the Sun; no safe detour" instead of "Sun blocks the track in …". A clearer reason, e.g. "Sun is about to cover the target", is a one-line change in `trackplan.py` (the `sun_no_detour` rendezvous refusal).

### T4.3 log (2026-10-29)

**T4.3a analysis (Sonnet, about 7 min):** `GET /api/sats/search` gains `country` (exact, case-insensitive) and `orbit_group` (leo = LEO + SSO, meo, geo = GEO + HEO; else 422); new `GET /api/sats/countries` (live objects, count desc, cached until the catalogue reload); 11 tests.

**DB spot check (counts only, throwaway 18710):**
- default 500, NORAD descending;
- THAI = the render's 8 objects;
- the LEO group = LEO + SSO;
- 98 countries (US 10 857, PRC 3 190, …);
- `_LIVE` = active and never decayed, with no object-type filter (Q22 holds; the newest 500 happen to be payloads).

**T4.3b panel (Sonnet, about 2 min):**
- New `utils/track-panel.ts` (the A1e table), `StaleBadge.vue` + `utils/tle-stale.ts`.
- The selection store's state poll follows the main card (`cardNorad`); `afterTrackStarted()`.
- The pinned card hides the ×; the strip button is `track-strip`.
- **The user asked for care with the vertical space. Supervisor browser measurement (injected track state, no gateway):**
  - at 1280×600 @1.5 the card + strip was **3 px over** the frame (the `overflow: hidden` wrapper hid it);
  - fixed with a tighter row gap / strip margin in the stripped short-panel block and a 0.12em row gap in the 160 px block;
  - after the fix: strip 176.7 / frame 179.1; pinned card 177.1 / 179.1; scrollHeight = clientHeight (nothing hidden); 1920: strip 281 / 330;
  - the strip now shrinks the name first, so the phase / countdown stays visible.

**T4.3c modal (Sonnet, about 5 min):**
- New `TrackTargetDialog.vue`, `utils/track-target.ts` (query, sort, epoch format, status, keyboard) + vitest.
- TRACK TARGET enabled with GOTO's rule.
- web tests 449.

**Supervisor browser pass** (throwaway analysis 18710 + gateway 18733, Sun pinned far, + Nuxt 13270, PWI4 sim; script `t43_gate/` in the session scratchpad), at 1920 and 1280×600 @1.5:
- **Modal:**
  - the default list: 500 + the footer, 2 stale badges;
  - "THAICOM" → 3; Origin THAI → 8 with 1 stale (TMSAT-1, "TLE 227 d");
  - sort by NAME;
  - the AIRCRAFT tab disabled;
  - row → "Ready";
  - TRACK SELECTED TARGET → the modal closes, card TRACKING THAICOM 8;
  - the modal fits the viewport (1280: 12–588 of 600).
- **A1e:**
  - B selected → B card + "📌 THAICOM 8 · Tracking";
  - strip click → A;
  - deselect → A pinned (× hidden);
  - STOP with B selected → A dropped;
  - audit `track_start` / `track_stopped`;
  - no console errors.
- **Note:** the dev-only Nuxt badge overlaps the modal status line at 1280 (not present in the built app).

### T4.3d log (2026-10-29, the user's visual adjustments before the Phase 4 close)

**User requests (reviewed as a change summary first):**
1. Track Target table rows showed above the sticky header (large screen, no scaling).
2. The Origin list: a blank flag slot for flag-less items; full owner names in the LIST only (user A: the closed box + the table keep the codes); searchable.
3. Track Info: the card must not shrink with the tracking strip; the strip (a minimized tracking card, one line, user C) is added under it; the space is reserved all the time (user B); small screens get the room from the left column.

**Worker (Sonnet, about 5 min), web only:**
- Table: `border-collapse: separate`, the scroll box without padding, an opaque sticky `th`.
- Origin: a custom type-to-filter combobox (`tt-origin`, `tt-origin-search`, `tt-origin-{CODE}`); `utils/owner-names.ts` (about 165 SATCAT owner names, the fallback = the code; vitest); `filterOwners`; the table cell keeps the code with the full name as a tooltip.
- Track Info: `--strip-reserve` 26 px subtracted from the card's `--u`; an always-rendered `.strip-slot`; the left column `minmax(var(--ti-min) = 196px, 0.87fr)`; the Filters floors lowered slightly.
- web tests 454.

**Supervisor browser measurement** (sim + throwaway servers, a real THAICOM 8 track; `t43d_gate/` in the session scratchpad):
- **Table:**
  - at 1920 @1.0 and 1280 @1.5, scrolled 157 px, probes 1 / 3 / 6 px under the box top all hit the header (no row above it).
- **Origin:**
  - 99 items, all names start at the same x (flag-less TBD / ITSO / ABS / ISS aligned);
  - "chi" → PRC / ROC / CHBZ / CHLE; "thai" → THAI, Enter → the box shows THAI → 8 rows.
  - **Fixed by the supervisor:** the 340 px list (anchored left) overflowed the dialog's right edge and scrolled the dialog sideways at 1920 (title cut). It is now right-aligned to the Origin box: list 875–1215 inside the dialog 530–1390, no scroll offset.
- **Track Info:**
  - the card height without / with the strip is identical (1920: 198.3 / 198.3; 1280 @1.5: 114.5 / 114.5, 9 px font);
  - strip bottom 277.3 / frame 329.9 (1920), 193.5 / 203.0 (1280).
- **Filters:** the content fits (Tools bottom = the panel bottom; tree 185 ≤ 198; no scroll) at both sizes.
- The pinned card 177.1 / 203 at 1280.

**Gates:** typecheck 0, lint 0 errors, vitest 454.

### T4.3e (2026-10-29, the user's last Phase 4 change)

- **Change:** with a track running and nothing selected, the pinned tracked card is clickable (pointer cursor, title "Show the tracked object in the Sky Viewer"); a click selects the tracked object (the Sky Viewer focus mode), the same as the strip under a selected card. A normal selected card's click does nothing. Supervisor change in `TrackCard.vue` (`refocusable`, `onCardClick`, `.card.refocus`).
- **Browser check** (track state injected, no gateway):
  - pinned + nothing selected → `data-refocus` true → click → selection 41552, refocus false;
  - B selected + strip → a card click changes nothing;
  - strip click → A;
  - deselect → pinned / clickable again.
- typecheck 0, lint 0 errors, vitest 454.

### Phase 4 end, Tier 3 (2026-10-29)

The user stopped 8400 / 3200 / 8410 (PWI4 kept running) and gave a 150 min budget (08:05–10:35 UTC). `PWI4.cfg` baseline is now `b714321830fb…` (the user's site change to 13.12° N / 100.92° E; Lim EL 10 in the app).

| Step | Result |
|---|---|
| T4.3 browser pass | ✅ (see the T4.3 log) |
| Full sim suite run A | 61 / 66 in 36.5 min: all 56 Phase 3 tests pass; 5 object-track tests failed on following error 0.43–0.61° (limit 0.05°); the rendezvous error was 0.0° |
| Investigation | <ul><li>run alone: the normal pass 0.004° max (twice), also right after the stall module;</li><li>PWI4 vs the PC clock: 6–17 ms;</li><li>no stray offset type in the tests;</li><li>conclusion: the test's yardstick. The sampler stamped each /status at its return, so a slow answer under load read as a pointing error for a 0.5–1°/s LEO</li></ul> |
| Fixes | <ul><li>(test) samples take the midpoint of the call, and calls slower than 0.15 s are skipped (counted in RESULTS);</li><li>**(product) `trackexec._upload` resets the axis offsets** after `tracking_off`. A STEP made while sidereal TRACKING keeps its offset (T3.4 design), and it would have shifted a later object track</li></ul> |
| Full sim suite run B | **66 / 66** in 35.7 min |
| PWI4 harness | 10 / 10 scenarios ok; harness tests 17 / 17; the sim left disconnected |
| Non-sim | 624 passed, 77 skipped; ruff + format clean |
| Web | typecheck 0, lint 0 errors (11 old warnings), vitest 449 |
| Flaky list | <ul><li>`test_jog` keepalive: t0 is now taken before the send (the timer starts when the gateway accepts the jog);</li><li>`test_track_exec` pass sequence: waits for the "Track ended" override instead of reading it at once;</li><li>8 / 8 green together → **list empty**</li></ul> |
| Full browser pass | the Phase 4 screens were covered by the T4.2 pass (click → "Track ended at Lim EL", Jog disabled) and the T4.3 pass (modal + A1e with a real THAICOM 8 track, after T4.2f) at 1920 and 1280×600 @1.5; Phase 3 screens unchanged since its Tier 3 |

**Open item (user, fix if required):** a GEO about to be covered by the Sun disc shows "no safe detour" instead of "Sun blocks the track in …" (T4.2f log).

## 9. Known limitations & risks

- **Gateway crash (G4 note):** nothing can send a stop if the gateway process dies; mitigated by a UI warning that hands the user off to PWI4.
- **Sky Viewer scale:** "no item limit" is the main performance risk; the O1 benchmark puts the propagation side at ~2 s per 30-min chunk for 20k objects, so the remaining risk is rendering (T2.2/T2.3 measure it).
- **D-SUN** is novel logic driving hardware; it ships only after planner unit tests + PWI4-sim runs + a supervised real-mount test.
- **Aircraft / ADS-B** is deferred; UI is present but disabled.
