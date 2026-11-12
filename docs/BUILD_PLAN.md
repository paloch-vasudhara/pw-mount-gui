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

### Phase 5b — Aircraft (ADS-B), added 2026-11-07
| Task | Scope | Gate |
|---|---|---|
| T5.4 | ADS-B feed (WSS, gateway), validation, Settings ADS-B, CPR + aircraft state + predictor | Fake-platform replay; CPR vectors; no token leak |
| T5.5 | Web: Aircrafts filter, Sky Viewer layer + hover, A1b card, AIRCRAFT tab | Browser pass on the fake feed |
| T5.6 | Aircraft cueing: P23 probe, `track_aircraft` planner / executor, rules | Sim tracks from a replayed capture; user overflight |

**User check:** a live feed, then a real overflight cued on the L-350.

### Phase 6 — Hardening & packaging
| Task | Scope | Gate |
|---|---|---|
| T6.1 | Security pass (rate limits, message size caps, schema validation, token handling), gateway-crash warning → PWI4 handoff (G4 note); **ephemeris fail-safe (user 11-12, before packaging):** with no ephemeris the Sun checks are skipped today (`sun is None` → `use_sun` False: Az/El Go To, jog, track, tracking on and the 10 Hz monitor run without the Sun keep-out / D-SUN; only RA/Dec Go To refuses) → refuse motion with "Sun position unavailable (ephemeris missing)" (STOP / Disconnect stay allowed; Home: user to decide), a red chip on the Sky Viewer + Mount Status, and the gateway retries loading `de421.bsp` every 30 s (today it loads once at start) | I6, I7 re-run; fuzzed WS messages |
| T6.2 | Deployment modes: web-app (remote analysis URL + server token) and standalone (local analysis + pywebview window); one config switch; **one install package per planned item (user 11-12):** pinned Python deps (a lock file from the tested `.venv`), `de421.bsp` shipped or fetched, `adsb_core` from a pinned adsb_interpreter git tag (dev: the `.pth` link), README steps incl. secrets re-entry | Both modes start from a clean checkout following the README |
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
| 2 | `alt < 0` and no Lim EL crossing within `LEAD_S` = 5 min (T5.1b, Q27b: inside the lead the object is trackable, phase `pre_position`) | "Below the horizon" | `below_horizon` |
| 3 | `sun_sep < 15` (the Sun shortcut applies as for gotos); below 0° inside the lead: the separation at the Lim EL crossing (`sun_sep_in`) | "Within 15° of the Sun" | `sun` |
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

### T5.1 — Satellite Pass Analysis lite (A2b-i), optical visibility (O4), live [Track] button (spec, 2026-10-30)

**Goal:** FILTERS › Tools › SATELLITE PATH ANALYSIS opens the Satellite Pass Analysis modal (`concept/Pass Analysis.png`): pick a satellite, OPTICAL / LINE OF SIGHT, a start–end window, LIST PASSES, a sortable result table, and a live [Track] button on rows that are in progress or about to start.

**User decisions (10-30):**
- **Q24 darkness:** keep O4. Optical "Visible Duration" = above Lim EL **and** sunlit **and** Sun below 0° at the station. (ssa-toolkit uses civil twilight, −6°. The comparison gate runs our engine at −6° for the optical part.)
- **Q25 times:** UTC everywhere in the modal (Start / End fields, Rises / Sets columns), as in the render and ssa-toolkit.
- **Q26 search:** SEARCH opens the Track Target search + table in **picker mode**. Picking a row fills the field "NAME (NORAD)". One search UI in the app.
- **Q27 / Q27b [Track] timing:** a row gets [Track] when its pass is above Lim EL now **or reaches Lim EL within 5 min**. The 5-minute lead applies **everywhere** (card, Track Target modal, Pass Analysis), so an object still below 0° can be tracked when its Lim EL crossing is ≤ 5 min ahead. The mount slews to the crossing point and waits (the T4.2 workflow).

**1. Analysis: pass engine + endpoint (`analysis/`)**
- `analysis/passes.py` (pure, no I/O):
  - geometric alt/az, no refraction (the chunks / state convention);
  - skyfield + sgp4 with the existing DE421 load (`state.py`);
  - a coarse 10 s scan, then crossings refined to ≤ 0.5 s and the peak by a parabola vertex. Copy the ideas from `gateway/passes.py`; the analysis service must not import `gateway`.
- **Passes** = the segments with alt ≥ Lim EL that overlap [start, end].
  - The scan is padded by 2 h on each side, so a pass in progress at Start shows its true rise and set.
  - An object above Lim EL through the whole padded window (GEO) is one row with rise / set null ("—"), and its duration is clipped to the window.
- **Fields per pass:**
  - `t_rise`, `t_set` (Lim EL crossings, Unix s) and `rise_az`, `set_az`;
  - `t_peak`, `max_el`;
  - `duration_s` (the part of the segment inside the window);
  - OPTICAL: `visible_s` and `visible` = [[t0, t1], …], the sub-intervals where the object is sunlit (`is_sunlit`, DE421) and the Sun's geometric alt < 0°, sampled every 10 s and refined to ≤ 1 s.
- **OPTICAL drops passes with `visible_s` = 0** (ssa parity).
- `POST /api/passes`:
  - body: `{norad, site{lat, lon, alt_m}, lim_el, start_unix, end_unix, mode: "optical"|"los", sun_alt_max: 0}`;
  - `sun_alt_max` is test-only; the web never sends it;
  - returns `{name, norad, tle_epoch, tle_age_days, passes[]}`, sorted by rise.
- **Rules:**
  - end > start; window ≤ 7 days (422 "Window longer than 7 days");
  - a 404 when the object is not found or decayed;
  - stale TLEs are allowed, and `tle_age_days` is returned for the badge;
  - the computation runs in a worker thread with a 20 s limit (503 "Pass computation timed out").
- **Tests** with synthetic TLEs (reuse `gateway/tests/tlegen.py` ideas; copy, don't import):
  - a LEO pass: crossings / peak vs a brute-force 0.1 s skyfield scan, ≤ 0.5 s / ≤ 0.01°;
  - a two-hump MEO gives two rows;
  - a GEO row with nulls;
  - the window clip;
  - optical: a pass in Earth shadow → dropped, a twilight pass → partial `visible_s`;
  - validation errors.

**2. Gateway: the 5-minute lead (Q27b) (`gateway/passes.py`, `trackplan.py`, `control.py`)**
- `LEAD_S = 300`. In `compute_pass`, when alt < 0 now: search forward ≤ `LEAD_S` + the time to climb to Lim EL for the next rise.
  - If the Lim EL crossing `t_in` is ≤ now + `LEAD_S`, return that pass (rise, in, peak, out, set, as for a pass in progress below Lim EL).
  - Otherwise return as today ("Below the horizon").
- `track_verdict`, row 2:
  - "Below the horizon" applies only when `t_in` is missing or more than `LEAD_S` ahead;
  - a below-horizon object with `t_in` ≤ `LEAD_S` ahead goes on to the remaining rows, with phase `pre_position`.
  - The **Sun row for a below-horizon object** uses the separation at the meeting point (`target0`), not the object's position now.
- The planner / executor already handle the meeting point + wait. Confirm that `plan_track` accepts `alt < 0` now (the meeting point is the Lim EL crossing) and the arrival re-check (`track_eval`) passes.
- The card phase line stays "Waiting for the pass — mm:ss". The card's TRACK OBJECT becomes enabled up to 5 min before the crossing.
- **Tests:**
  - `test_passes.py` rows: below 0°, crossing 4 min ahead → allowed `pre_position`; 6 min ahead → "Below the horizon"; Sun at the meeting point → "Within 15° of the Sun";
  - one new row in `test_track_object_pwi4sim.py`: a TleStub pass rising 3 min ahead → slew, wait, track, "Track ended at Lim EL".
- Logic sheet A1d step 1 and the T4.1 rule table get the lead (supervisor doc update).

**3. Web: the modal (`components/analysis/PassAnalysisDialog.vue`, `utils/pass-analysis.ts`)**
- ToolsGroup: enable SATELLITE PATH ANALYSIS, which opens the modal. The dialog shell is like the Go To / Track Target modals; layout per the render.
- **Satellite:** a read-only field ("no satellite selected" placeholder; "NAME (NORAD)" + `StaleBadge` once picked) + SEARCH.
  - SEARCH opens `TrackTargetDialog` with `mode="pick"`:
    - title "SELECT SATELLITE";
    - the bottom button "SELECT TARGET", with no gateway check and no status line;
    - a double-click picks;
    - it emits `picked(row)` and closes.
  - The track mode stays unchanged.
  - Default prefill: the dashboard's selected or tracked object, if any.
- **Mode:** a SegToggle OPTICAL | LINE OF SIGHT, default OPTICAL.
- **Start / End:** H1 date-time fields in UTC.
  - Default Start = now (UTC, to the minute), End = Start + 24 h.
  - End ≤ Start or a window over 7 days → the field is marked invalid and LIST PASSES is disabled, with the reason as the button's title.
- **LIST PASSES:** full width; grey when disabled, green when ready (render).
  - Loading state on the button.
  - Errors on a muted line in the table area: "Satellite data unavailable", the 422 text, "Pass computation timed out".
- **Table** (render columns):
  - Rises (UTC) `YYYY-MM-DD hh:mm:ss`, Rise Az `.1f°`, Max El `.1f°`, Sets (UTC) `hh:mm:ss` (with the date when it differs from the rise), Set Az, Duration / Visible Duration as `mmᴹ ssˢ`, or `hhᴴ mmᴹ` from 1 h;
  - a GEO row shows "—" for rise / set;
  - sortable by every column (▼ / ▲), default Rises ascending;
  - an empty result reads "No passes above Lim EL in this window" (optical: "No visible passes …").
- **[Track]:**
  - a column right of the duration column (space always reserved, per the logic sheet);
  - the button shows on the row whose pass is in progress or whose `t_rise` is ≤ 5 min ahead, re-evaluated at 1 Hz from the client clock;
  - enabled by the gateway check (`utils/track-verdict.ts`, `/api/mount/check` `track_object` for that NORAD at 1 Hz while the button shows); `title` = the reason or note;
  - a click sends `track_object` (WS ack), then `afterTrackStarted()` (the dashboard selection = that object, A1e). The modal **stays open** and the row's button turns into a disabled "TRACKING" while that object is tracked.
- **Limits:** the result is a snapshot. Nothing is recomputed while the modal is open, except the [Track] eligibility and verdict. A pass that has ended loses its button.
- **vitest:**
  - window validation;
  - the duration format;
  - sorting;
  - the [Track] eligibility (in progress / ≤ 5 min / ended / GEO);
  - the picker-mode emit;
  - request building.

**Must not touch:** `pwi4/`, ssa-toolkit (read/copy only; I1), `docs/`, PWI4 settings. No commits. Throwaway ports only; never the user's 3200 / 8400 / 8410.

**Briefs:**

| Brief | Kind | Scope | Timebox |
|---|---|---|---|
| **T5.1a** | analysis | `analysis/passes.py` + `POST /api/passes` + tests | 60 min |
| **T5.1b** ∥ a | gateway | the 5-min lead (passes / verdict / Sun at the meeting point) + unit rows + 1 sim row | 45 min |
| **T5.1c** after a | web | Pass Analysis modal, Tools button, Track Target picker mode, [Track] column; vitest | 90 min |

**Gate (Tier 2):**
- Diff review; analysis + gateway unit tests; the new sim row (×1; the user's 8400 stopped).
- **ssa-toolkit comparison** (I1 kept): ssa-toolkit's pass engine run read-only in its own environment (`PYTHONDONTWRITEBYTECODE=1`, no files written inside ssa-toolkit; `git status` unchanged afterwards).
  - Inputs: 3 real TLEs (a LEO, TERRA 25994, a MEO), the user's site, Lim EL 10, 24 h.
  - LOS: same pass count; rise / set ≤ 2 s; max el ≤ 0.05°; az ≤ 0.1°.
  - OPTICAL: our engine with `sun_alt_max = −6` vs ssa's observable intervals, ≤ 20 s (cylindrical vs skyfield shadow, ssa's 10 s sampling).
  - If ssa-toolkit's engine cannot run here: report it; the comparison falls back to the user's side-by-side check.
- **Real browser**, throwaway servers + the PWI4 sim + the TLE stub, at 1920 and 1280×600 @1.5:
  - picker → field filled; LIST PASSES in both modes; sorting;
  - [Track] on a stub pass ≤ 5 min ahead → the mount waits, then tracks; the tracked card comes up and the row shows TRACKING.

---

### T5.2 — Astrometry from Image lite (A2b-iv), wired into IOD rows (spec, 2026-10-30)

**Goal:** IOD › (…) › Add Astrometry opens ASTROMETRY FROM IMAGE (`concept/Astrometry.png`):
- load a frame;
- epoch / exposure (autofilled from metadata);
- DETECT STREAKS (the mount tracked the object: stars are streaks, the target is a point);
- review / click-edit the detections;
- RUN ASTROMETRY on astrometry.net (nova).

The solved target RA/Dec at mid-exposure fills the IOD row. The job runs in the background with a pill.

**Decisions:**
- **Q29:** image work + the nova job in the **analysis** service; RUN goes through the **gateway**, which adds the API key server-to-server (the key never reaches the browser).
- **Q30:** the scale hint from FITS `FOCALLEN` / `XPIXSZ`, else Settings (RASA11 620 mm + a new "Camera pixel size (µm)").
- **Q31:** server-side jobs survive a refresh; one at a time; job pill; in memory (lost on an analysis restart).
- **Q34:** the IOD state is recalled until a refresh; after a refresh a finished job is applied to a row from the pill.
- **User 10-30:** the gateway is always up to use the tools.
- **Reference (read / copy only, I1):**
  - ssa-toolkit `photometry/` (engine, io, background, detect, streaks, select, solve, platesolve, pixel_coords, types);
  - `data_api/routes/astrometry.py` + `astrometry_jobs.py` (`/measure`, `/run` nova path, the job registry, `_run_solve` object mode, the midpoint export);
  - the legacy UI `frontend/src/tdm-tool.js` (detections edit table, focus-a-coordinate-then-click-the-image "specify", add / delete detection, the target lock, `ZoomPane` wheel-to-cursor zoom + pan, the bottom-corner job pill that persists and reattaches after a reload).
  - Port only what this flow needs: no ASTAP, no sidereal mode, no TDM, no WCS cache.

**1. Analysis (`analysis/astrometry/`, a package)**
- **Port** from ssa-toolkit `photometry/`, object mode only:
  - image loading (FITS / TIFF / JPG / PNG; the same accepted types: `.fits .fit .tif .tiff .jpg .jpeg .png`) with metadata: `DATE-OBS` epoch (exposure start), `EXPTIME` / `EXPOSURE`, `FOCALLEN`, `XPIXSZ`;
  - the background + detection + streak classification + role assignment (`DetectionEngine`, `measure_at` click-snap);
  - the nova client + the object-mode solve (the star-streak midpoints as a synthetic point field → WCS) + pixel → RA/Dec mapping of the target.
  - Keep module boundaries close to the source so later fixes port easily; attribute the origin in each module docstring.
- **New deps** (`analysis/requirements.txt` + the venv): `photutils`, `scipy`, `scikit-image`, `tifffile`, `pillow`, `requests` (astropy is already present). Pin to the versions ssa-toolkit uses where they install on Python 3.12.
- **Image sessions:** an uploaded frame is kept in memory under an `image_id` (max 3 frames, 30 min idle TTL), so click-snap and RUN don't re-upload. Temp files are deleted.
- **Endpoints (analysis):**
  - `POST /api/astrometry/image` (multipart `file`) → `{image_id, preview{png_b64, width, height, scale}, meta{format, bit_depth, dims, epoch_iso|null, exposure_s|null, focal_mm|null, pixel_um|null, warnings[]}}`. No detection yet: the frame shows first.
  - `POST /api/astrometry/detect {image_id}` → `{detections[], n_streaks, target|null, warnings[]}`. A detection = `{id, role: "star"|"target", shape: "streak"|"point", x, y, xs, ys, xe, ye, brt 1–5}`.
  - `POST /api/astrometry/snap {image_id, x, y, role}` → one refined detection (`measure_at`).
  - `POST /api/astrometry/run` — **called only by the gateway** (rejects a request without `apikey`):
    - body `{image_id, detections[] (edited), epoch_iso, exposure_s, focal_mm, pixel_um, apikey}` → 202 `{job_id, state}`;
    - 409 when a job is active.
  - `GET /api/astrometry/job/{id}` and `GET /api/astrometry/job` (the active or latest job, for reattach) → `{job_id, state: uploading|queued|solving|mapping|done|failed|cancelled|timeout, stage_text, started, result?: {ra_deg, dec_deg, epoch_mid_iso, epoch_mid_unix, residual_arcsec, scale_arcsec_px, n_stars_used}, error?: readable}`. Never the key.
  - `POST /api/astrometry/job/{id}/cancel`.
- **Rules:**
  - RUN needs ≥ 10 streaks + a target (422 otherwise);
  - the epoch is required;
  - mid-exposure = epoch + exposure / 2 (exposure missing → the epoch itself, with a warning in the result);
  - the scale check vs the hint, as ssa does (a wrong-scale solve → failed, "Solved scale does not match the camera").
  - The nova solve timeout is 300 s.
  - Readable errors only: "astrometry.net could not solve this frame", "astrometry.net unreachable", "Invalid astrometry.net API key", "Solve timed out".
- **Tests:**
  - port the relevant ssa-toolkit `tests/photometry` cases;
  - detection on `examples/data/astrometry/OW0021_20250404130130.tif`, read in place from ssa-toolkit (read-only; skip if absent): ≥ 10 streaks + a target;
  - the metadata parse;
  - the session TTL;
  - the job state machine with a fake nova client (no network in tests);
  - the run validation.

**2. Gateway**
- **Settings:** `camera_pixel_um: float | null` (Hardware group; 0.5–30 µm; default null) in the settings model + file.
- **The relay:**
  - `POST /api/mount/astrometry/run` (auth as the other mount routes) adds the key from the secret store and forwards the body to analysis `/api/astrometry/run` (`PW_ANALYSIS_URL`);
  - returns the analysis answer;
  - no key stored → 409 "Set the astrometry.net API key in Station Settings";
  - analysis down → 503 "Analysis service unavailable";
  - the key never appears in logs, the audit or responses.
  - A focal / pixel hint missing from the body is filled from Settings (620 mm OTA + `camera_pixel_um`); still missing → a blind solve (no hint).
- **Tests:** key injection (memory secret store + a fake analysis), the no-key 409, no key in any response or log line.

**3. Web**
- **IOD wiring:**
  - Add Astrometry enabled (the T5.3 row states pending / failed / valid already exist);
  - it opens ASTROMETRY FROM IMAGE for that row (the overwrite rules of T5.3 apply first);
  - RUN → the row becomes "Astrometry pending" (job id kept in the row + the IOD memo);
  - done → RA / Dec bright, Az / El by H4 at the mid-exposure epoch (dimmed), Source "Astrometry", epoch = mid-exposure;
  - failed → "Astrometry failed" (replace / remove without a prompt).
- **Astrometry modal (render):** left: Epoch (UTC, H1) + Exposure (s) above the Data Entries table; right: Image Preview with the toolbar.
  - **Epoch / Exposure:** autofilled from the metadata, editable; empty → RUN disabled ("Enter the exposure start time").
  - **Table:** T (target X / Y) then S1…Sn with S / E rows (X / Y) + BRT; scrolls.
    - Focusing a coordinate arms "specify": the next click on the image sets that point (snapped by `/snap` for the target).
    - "+ add streak" / ✕ remove a streak; the target row cannot be deleted, only re-specified.
    - A selected row highlights its overlay.
  - **Preview:** "select image" placeholder; a click opens the file prompt (the accepted types).
    - Toolbar: the streak-overlay visibility toggle, − / zoom % / +, wheel-to-cursor zoom, drag-pan when zoomed (port `ZoomPane`).
    - Overlay: streaks as lines with S / E, the target as a ring.
  - **Buttons:**
    - DETECT STREAKS: needs an image; replaces the table, with a confirm when it has edits;
    - CLEAR: clears the image + table;
    - RUN ASTROMETRY: green only with ≥ 10 streaks + a target + an epoch; the `title` gives the reason; → the gateway relay → a 202 closes the modal and the job pill appears; 409 / 503 show on a status line.
- **Job pill:** a small bottom-corner pill on the dashboard (not inside a modal).
  - Content: "Astrometry · <stage>" with a spinner; done → "Astrometry · solved — row N" (or "Apply to a row…" when the row is gone after a refresh: picks an IOD row and opens IOD); failed → the readable reason; × dismisses a finished job; "Cancel" while running.
  - It polls `GET /api/astrometry/job/{id}` every 2.5 s while active.
  - On page load: `GET /api/astrometry/job` reattaches an active / unclaimed finished job.
- **Settings:** Hardware gains "Camera pixel size [ ] µm" (follow the T1.2 / T1.5 layout rules, A5 widths).
- **vitest:**
  - table / overlay model, specify, add / remove;
  - RUN readiness;
  - the pill states, reattach;
  - the IOD row transitions;
  - the settings field.

**Must not touch:** `pwi4/`, ssa-toolkit (read / copy only; test images are read in place), `docs/`, PWI4 settings. No commits. Throwaway ports only. Tests never call the real astrometry.net (fakes); the supervisor's gate may run ONE real nova solve with the user's key only after asking the user.

**Briefs:**

| Brief | Kind | Scope | Timebox |
|---|---|---|---|
| **T5.2a** | analysis | the photometry port (io, detection, snap) + deps + image sessions + `/image` `/detect` `/snap` + tests | 90 min |
| **T5.2b** after a | analysis + gateway | the nova client + object solve + mapping + the job registry + `/run` `/job` + the gateway relay + Settings `camera_pixel_um` (gateway) + tests | 90 min |
| **T5.2c** ∥ b (contract above) | web | the Astrometry modal (preview / zoom / overlay / table / specify / detect) | 90 min |
| **T5.2d** after b, c | web | RUN + the job pill + reattach + IOD wiring + the Settings field | 75 min |

**Gate (Tier 2):**
- Diff reviews; unit tests; detection on the OW0021 frames vs ssa-toolkit's own engine output for the same frames (`orekit310` env not needed: the photometry stack only; run ssa's engine in-process read-only, I1 checked) — the streak count and the target position within 1 px.
- **Browser, 1920 + 1280×600 @1.5:** load → detect → edit → RUN (fake nova behind a test switch) → pill → the IOD row filled → refresh → reattach.
- **One real end-to-end run (with the user's go-ahead, their key):** the three OW0021 frames → three rows → IOD → compare the orbit with OneWeb's real TLE (the user's planned pipeline test).

---

### T5.3 — Initial Orbit Determination lite (A2b-ii), built BEFORE T5.2 (spec, 2026-10-30)

**Goal:** FILTERS › Tools › INITIAL ORBIT DETERMINATION opens the IOD modal (`concept/IOD.png`):
- 3 angles-only observation rows (manual now; astrometry in T5.2);
- INITIAL ORBIT DETERMINATION;
- the P2 state vector and a coarse TLE.

**User decisions (10-30):**
- **Q28 order:** IOD (T5.3) is built first, then Astrometry (T5.2) plugs into a real IOD row.
- **Q32 TLE:** SGP4-fitted. A few Newton iterations adjust the mean elements so SGP4 reproduces the P2 state at its epoch (start: the osculating elements).
- **Q33 result use:** display + copy only (tracking an IOD orbit can come later as its own task).
- **Q34 state:** recall until refresh, like Pass Analysis: close / reopen keeps the rows + results, a refresh clears them. (T5.2: a pending astrometry row keeps its job; after a refresh a finished job is applied from the job pill.)
- **For T5.2, recorded now:**
  - **Q29:** image work + the astrometry.net job in the analysis service; RUN goes through the gateway, which adds the API key server-to-server.
  - **Q30:** the scale hint from FITS `FOCALLEN` / `XPIXSZ`, else a new Settings › Hardware "Camera pixel size (µm)" + RASA11 620 mm.
  - **Q31:** server-side jobs that survive a refresh, one at a time, with a job pill.

**1. Analysis: IOD engine + endpoint (`analysis/iod.py`, pure; `POST /api/iod`)**
- **Input:**
  - `{site{lat, lon, alt_m}, obs: [{t_unix, ra_deg, dec_deg} × 3]}`;
  - RA/Dec = topocentric J2000 directions (astrometric, as a plate solve or H4 gives them; the ~20″ aberration difference is negligible for a coarse IOD; note it in the docstring).
- **Site inertial position** at each epoch from skyfield (`wgs84` site `.at(t)` in GCRS, km).
- **Gauss** (Curtis Alg. 5.5) + the iterative improvement (Alg. 5.6, f / g from universal variables), until the slant ranges change < 1e-8 relative or 50 iterations.
  - Root selection: the positive real roots of the 8th-order polynomial; several → choose the one whose r2 gives an orbit above the atmosphere (|r| > 6478 km) and, if still ambiguous, the smallest |r2| (report the choice).
- **Velocity at P2 (O6):** Herrick-Gibbs when the angle between consecutive position vectors < 3°, else Gibbs. Report the method used.
- **Output:**
  - `{epoch_unix (= t2), r_km[3], v_kms[3] (GCRS ≈ J2000), method: "gauss+gibbs" | "gauss+herrick-gibbs", iterations, coe{a_km, e, i_deg, raan_deg, argp_deg, nu_deg, period_min}, tle{line1, line2}, tle_fit{iterations, pos_err_km, vel_err_ms}}`.
- **Coarse TLE (Q32):**
  - convert r2 / v2 to TEME at t2 (skyfield frames);
  - starting from the osculating COE, run Newton / least-squares on (n, e, i, Ω, ω, M) with `sgp4` so the TEME state at the epoch matches (≤ 10 iterations; stop at ≤ 10 m / ≤ 1 cm/s);
  - B* = 0;
  - catalogue "A9999" (alpha-5, as the render), class U, blank international designator;
  - epoch = t2;
  - valid checksums.
  - **If the fit does not converge,** return the osculating TLE and say so (`tle_fit.converged = false`).
- **Readable failures** (422 `{detail}`; never raw exceptions):
  - fewer than 3 observations or duplicate epochs → "Three observations with different epochs are needed";
  - obs not in time order → the engine sorts; P2 = the middle epoch;
  - no positive root → "No orbit fits these observations (check the epochs and angles)";
  - diverged → "Orbit solution did not converge";
  - hyperbolic / sub-surface → "The fitted orbit is not a bound orbit above the Earth";
  - coplanar or degenerate geometry → "Observation directions are too close together".
- **Tests** (synthetic, `analysis/tests/test_iod.py`): observations generated from known TLEs with skyfield topocentric J2000 RA/Dec at the user site:
  - LEO 500 km, 60 s / 120 s spacing (Herrick-Gibbs);
  - LEO with 5 min spacing (Gibbs);
  - MEO;
  - GEO (2 h spacing).
  - **Criteria (noise-free):** |Δr2| ≤ 1 km, |Δv2| ≤ 1 m/s vs the truth state; the TLE reproduces the state ≤ 10 m at its epoch; the TLE-predicted RA/Dec at t1 / t3 within 0.05° of the observations (LEO).
  - **A 2″-noise case:** report the errors (no hard bound) for the log.
  - Every failure branch.
  - Runtime < 0.5 s per solve.

**2. Web: the IOD modal (`components/analysis/IodDialog.vue`, `utils/iod.ts`, `utils/iod-memo.ts`)**
- ToolsGroup: enable INITIAL ORBIT DETERMINATION, which opens the modal. Layout per the render: Data Entries table, the full-width INITIAL ORBIT DETERMINATION button + status line, the P2 State Vector box and the Coarse TLE Estimation box (monospace).
- **Table columns:**
  - row slot (1 › 2 › 3 ›), Epoch (UTC) ▲/▼ (sortable; the slot numbers stay with their rows), Az., El., R.A., Dec., Source, (…);
  - angles in decimal degrees, 3 decimals shown (the render), up to 4 typed;
  - R.A. in degrees 0–360;
  - Dec / El signed.
- **(…) menu:**
  - Add Manual;
  - Add Astrometry — present but disabled until T5.2, `title` "Available when Astrometry is added";
  - Remove Entry — red; inactive on an empty row.
- **Add Manual (logic sheet A2b-ii):**
  - the row turns into inputs; Epoch pre-filled with now (UTC) via the H1 suite (digits only, fixed separators);
  - Az / El / R.A. / Dec number inputs; typing in one pair clears the other;
  - Source "Manual";
  - Enter or a click outside confirms → the other pair is computed with H4 at the ROW EPOCH (`POST /api/transform`, the gateway) and shown dimmed;
  - the typed pair is bright (the render: manual az / el bright, RA / Dec dimmed);
  - Esc cancels the edit;
  - ranges: Az 0–360, El 0–90 (an observation below the horizon is refused with the field marked), RA 0–360, Dec −90…90.
  - When the gateway is unreachable, the other pair stays empty with a muted "—" and the row is still valid if it has RA / Dec; an Az / El-only row is not valid until the conversion succeeds (IOD needs RA / Dec).
- **Overwrite rules:** adding to or removing an occupied VALID row asks for confirmation (a small inline confirm: "Replace row N?" / "Remove row N?"); an empty or failed row is replaced without a prompt.
- **INITIAL ORBIT DETERMINATION:**
  - grey / disabled until 3 valid rows with distinct epochs (the `title` gives the reason), green when ready;
  - click → `POST /api/iod` (via `/api`, analysis) with the rows' RA / Dec / epoch + the site from the gateway snapshot;
  - the status line under it: "Done" on success (with the method, e.g. "Done · Gauss + Herrick-Gibbs"), or the readable 422 / "Analysis service unavailable".
- **Outputs:**
  - P2 State Vector: `R_2 : [ x, y, z ]   V_2 : [ vx, vy, vz ]` (km, km/s, 3 decimals; the render);
  - Coarse TLE Estimation: the two lines;
  - a small copy button on each box (copies the text; a brief "Copied");
  - when `tle_fit.converged` is false, a muted note "Osculating elements (fit did not converge)".
  - Editing a row after a result greys the outputs as stale ("Inputs changed — run again") until the next run.
- **Recall (Q34):** module-scope memo (rows, results, status, sort), saved on an open → closed transition only (the T5.1d lesson); a refresh clears it.
- **Layout:** fit 1280×600 @1.5 and 1920; follow the render proportions; measure before proposing changes (the T5.1e method).
- **vitest:** row validation / ranges, pair clearing, readiness rules, overwrite-confirm rules, the output formats, the request building, the memo.

**Must not touch:** `pwi4/`, ssa-toolkit (read/copy only; I1), `docs/`, PWI4 settings, the gateway (H4 exists). No commits. Throwaway ports only; never the user's 3200 / 8400 / 8410.

**Briefs:**

| Brief | Kind | Scope | Timebox |
|---|---|---|---|
| **T5.3a** | analysis | `analysis/iod.py` (Gauss + improvement, Gibbs / HG, COE, SGP4-fitted TLE) + `POST /api/iod` + tests | 75 min |
| **T5.3b** ∥ a (API contract above) | web | IOD modal + Tools button + manual rows + H4 + outputs + memo; vitest | 90 min |

**Gate (Tier 2):**
- Diff review; analysis + web unit tests.
- **Reference check (I1 kept):** the same synthetic observations through orekit's `IodGauss` (+ Gibbs / Herrick-Gibbs) in the `orekit310` env (in-process, `PYTHONDONTWRITEBYTECODE=1`, ssa-toolkit `git status` unchanged): r2 / v2 vs ours, and both vs the truth.
- **Real browser** (throwaway analysis + gateway for H4 without PWI4 motion — or the snapshot injected + H4 stubbed if the user's 8400 is up; 1920 + 1280×600 @1.5):
  - 3 manual rows from a real satellite's computed positions → IOD → TLE;
  - the TLE's NORAD-free orbit vs the real one (period / inclination);
  - the overwrite confirms; recall.
- No sim test (no motion).

---

### T5.2f — Astrometry: images from other setups, scale-mismatch review (spec, 2026-11-09, from the user's real cases)

**Problem (user 11-09):** an image taken with a different OTA / camera solves on astrometry.net, but no target RA/Dec is produced. Two station constraints are applied to images they do not describe:
- the gateway relay fills a missing focal length / pixel size with the station's (RASA11 620 mm + Settings `camera_pixel_um`), and astrometry.net is told ±25 % around that scale (it may then fail to solve);
- after a solve, `scale_ok()` fails the job ("Solved scale does not match the camera") when the solved scale is > 25 % off the hint, so the solution is thrown away.

**Decisions (user 11-09):**
- **Q51 image source:** an "Image source: Station | Other setup" toggle in the Astrometry dialog, default Station. When the file's own optics (FITS `FOCALLEN` / `XPIXSZ`) differ from the station's by > 25 % in plate scale, the toggle switches to Other with a note; the user can switch back. Station = the station hints as now; **Other = no station hints** (the file's own hints if present, else a blind solve).
- **Q52 mismatch:** a solve is never failed for its scale. When the solved scale does not match the station (> 25 % off), the job finishes with a **review**: solved scale + field extent vs the station's, and the user chooses **Use** or **Discard**. Same for both sources.
- **Q53 where:** the pill (Use / Discard) and the waiting IOD row (Use / Discard in the row, the in-row confirm style). The row stays pending until a choice. (The Astrometry dialog closes on RUN, so "the dialog" = the IOD row.)
- **Q54 site:** other-setup images are taken from the station's location; IOD keeps the Settings location (the user moves the Settings location to work another site). The row's source shows the image was an other-setup image.

**1. Analysis (`analysis/astrometry/`)**
- `RunIn`: new `source: "station" | "other"` (default `"station"`), `station_focal_mm: float | None`, `station_pixel_um: float | None` (the station's optics, for the comparison only; never used as a hint).
- Hint (unchanged rule): `hint_scale_arcsec(focal_mm, pixel_um)` from the body's `focal_mm` / `pixel_um` (blind when missing).
- **Remove the scale failure:** `scale_ok` no longer raises `ERR_SCALE` (keep the constant out of use, or delete it with its tests updated).
- **Result `scale_check`** (always present in a DONE result):
  `{source, solved_arcsec_px, field_deg: [w, h], station_arcsec_px | null, station_field_deg: [w, h] | null, match: true | false | null}`;
  field = image width / height × scale; `match` = solved within 0.8–1.25 × the station scale; `null` when the station scale is unknown (no `camera_pixel_um`).
- The fingerprint keeps including the hint scale (a Station and an Other run of the same image are different submissions; that is intended).
- `FakeNova`: an env `PW_ASTROMETRY_FAKE_SCALE` (arcsec / px) overrides the returned scale, so a mismatch can be produced in tests and the browser gate.

**2. Gateway relay (`POST /api/mount/astrometry/run`)**
- `AstrometryRun` gains `source` (default `"station"`).
- `station`: fill missing `focal_mm` / `pixel_um` with the station's (as now).
- `other`: **never fill** from the station; pass only what the client sent (the file's own header values or nothing).
- Always add `station_focal_mm = 620.0` and `station_pixel_um = Settings camera_pixel_um` (may be null).
- The key handling / scrubbing is unchanged (tests stay green).
- The resume relay is unchanged (the resumed job reuses the stored spec).

**3. Web**
- **Astrometry dialog:** the Image source toggle (`SegToggle`, `data-testid="as-source"`), default Station.
  - Auto-suggest: when the loaded file's `focal_mm` and / or `pixel_um` give a plate scale > 25 % off the station's (station pixel from Settings; if the station pixel is unknown compare focal length only), switch to Other and show "File optics (N mm, M µm) differ from the station: treated as Other setup" on the status line.
  - A user change of the toggle wins until the next image load.
  - RUN sends `source`; with Other it sends only the file's own `focal_mm` / `pixel_um`.
- **Job result → IOD row:**
  - `scale_check.match === false` → the row becomes **review** (still not usable by IOD, like pending), text "Scale differs from the station: solved S″/px, F°×G° (station s″/px, f°×g°)", in-row **Use** / **Discard** (`iod-review-use` / `iod-review-discard`).
  - Use → the row is filled as an Astrometry row (RA / Dec / epoch as a normal result, then H4 as now).
  - Discard → the row becomes empty; the pill is dismissed.
  - `match` true / null → applied directly as now.
  - **Source label:** an other-setup row shows its source as "Astrometry (other)" when that fits the column at 1280×600 @1.5; otherwise a shorter form chosen by measurement, with the full text in its `title`. Station rows keep "Astrometry".
- **Pill:** a done job awaiting review shows the title "Astrometry solved — scale differs" and **Use** / **Discard** buttons (`as-pill-use` / `as-pill-discard`), the same actions as the row. When its row is not in IOD (after a refresh), Use takes the "apply to a row…" path (the first free row) and applies it directly (the user already chose Use).
- The review survives closing / reopening IOD (the memo) and a refresh (the pill reattaches to the done job; the row is restored as review from the job).

**4. Tests**
- analysis: no scale failure; `scale_check` fields (match true / false / null; the field extent); Other runs carry no station hint; the fake scale override.
- gateway: `other` never filled from the station; `station` filled as before; the station values always added; the key-echo tests stay green.
- web (vitest): the auto-suggest rule; the RUN body per source; the row review → Use / Discard; the pill review buttons; the source label.
- Gates: touched tests + the non-sim suite once; web typecheck / lint / vitest. The supervisor runs the browser check (fake nova with `PW_ASTROMETRY_FAKE_SCALE`) at 1920 + 1280×600 @1.5. Real astrometry.net only by the user.

---

### T5.7 — Mount CCTV view, prepared for a live feed (spec, 2026-11-09)

**Goal:**
- The MOUNT STATUS panel shows either the 3D model or a live CCTV feed of the mount (one camera, watched from a distance).
- The expanded view (Detailed Mount Status) adds **Side by side**.
- With no CCTV configured, nothing changes: no CCTV options appear, the panel is exactly as today.
- Built now with a fake camera, so the real camera only needs its URL and credentials entered in Settings.

**Decisions (user 11-09):**
- **Q56:** the stream goes **through the gateway**. The camera's address and credentials stay server-side (credentials in Windows Credential Manager); the browser only talks to our app (one origin, works remotely).
- **Q57:** prepare for every common format; the camera will most likely be an IP camera.
  - Built and tested now: **MJPEG** (multipart over HTTP, also the snapshot fallback) and **HLS** (hls.js in the browser).
  - **RTSP** (the usual IP-camera stream) cannot play in a browser. It is handled by a relay sidecar (MediaMTX or go2rtc) that republishes RTSP as HLS; the gateway then proxies that HLS. The relay is set up when the camera is known (deployment, with T6.2); a documented config and a check script ship now.
- **Q58:** one feed.

**1. Settings (gateway store + Station Settings dialog)**
- A new **CCTV** group: Enable, Type (MJPEG / HLS), Stream URL, Username, Password (write-only, Credential Manager key `cctv_password`, shown masked, never sent back), and a status line (Not configured / Connecting / Live / Unreachable: reason).
- "Configured" = enabled and a URL is set. The web only learns `{configured, type, status}`, never the URL or credentials.
- URL rules: `http`/`https` only (RTSP goes through the relay's HLS URL); no credentials inside the URL (use the fields); validated on save.
- **Scrolling (user 11-09):** with the new group the dialog body must scroll where it does not fit (1280×600 @1.5 certainly). The body scrolls inside the dialog; the title bar and the CANCEL / SAVE SETTINGS bar stay fixed. The scrollbar is styled like the app's existing one in `VisibilityFilters.vue` (thin, the theme's track / thumb colours, Firefox `scrollbar-width` / `scrollbar-color` included), moved into one shared utility class so later scroll areas reuse it; no default white OS scrollbar.

**2. Gateway (`gateway/cctv.py`)**
- `GET /api/mount/cctv/status` → `{configured, type, state, error|null, viewers}` (auth as the other status routes).
- **Stream ticket:** `<img>` / hls.js requests cannot carry our `X-Mount-Token` header.
  - `POST /api/mount/cctv/ticket` (header auth) returns a ticket valid 60 s, bound to the session.
  - The stream URLs take `?t=<ticket>`; a used ticket stays valid for that stream's lifetime.
  - Tickets are never logged or audited.
- **MJPEG:** `GET /api/mount/cctv/mjpeg?t=…` streams the camera's multipart response through (async, chunked, the camera's Content-Type kept).
  - At most 2 concurrent viewers; a viewer that stops reading is dropped.
  - The upstream connection closes when the last viewer leaves.
- **Snapshot:** `GET /api/mount/cctv/snapshot?t=…` returns one JPEG (from the MJPEG stream, or a snapshot URL if the camera has one). This is the fallback when streaming fails.
- **HLS:** `GET /api/mount/cctv/hls/{path}?t=…` proxies the playlist and segments.
  - Playlist URIs are rewritten to our proxy path.
  - Only paths under the configured playlist's base are fetched (**no open proxy**).
  - Size and time limits per request.
- Upstream auth: Basic or Digest with the stored username / password, added server-side only.
- Errors map to readable states ("Camera unreachable", "Camera rejected the login", "Stream format not recognised"). Camera URLs and credentials never appear in responses, logs or the audit (the astrometry-key rules).
- Read-only: the gateway never sends anything to the camera except GETs for the stream (no PTZ, no camera settings).

**3. Web**
- **Panel (compact):** when configured, a small two-option toggle **3D | CCTV** at the slot's top-right corner, inside `mount-3d-slot`.
  - CCTV shows the stream fitted to the slot (`object-fit: contain`, letterboxed).
  - While CCTV is shown, the 3D renderer stops rendering (no frames), as the idle rule.
- **Expanded (Detailed Mount Status):** **3D | CCTV | Side by side**; side by side splits the 3D area into two halves (3D left, CCTV right).
- **Per-viewer memory:** the choice is kept in localStorage (try / catch), default 3D. A remembered CCTV choice falls back to 3D when CCTV is no longer configured.
- **Streams only while visible:** opened when shown, closed when hidden, when the dialog closes, or when the tab is hidden (`visibilitychange`), to save bandwidth.
- **States over the video area:**
  - "Connecting…";
  - "CCTV feed unavailable — retrying" (backoff 2 → 30 s);
  - after 3 failed stream attempts, a snapshot refreshed every 5 s, labelled "Snapshot".
  - A small LIVE / SNAPSHOT chip in a corner.
- **Player:** MJPEG = `<img src>`; HLS = `<video muted playsinline>` + hls.js (pinned exact version), or native HLS where the browser supports it.
- **Sizing:** the toggle's size and position are measured and listed for the user before they are applied (the user tunes them), at 1920 and 1280×600 @1.5.

**4. Fake camera + relay prep (tools)**
- `tools/fake_cctv.py`: a local camera on a throwaway port.
  - MJPEG: Pillow-rendered frames with a moving marker + clock, ~10 fps, optional Basic auth.
  - Snapshot JPEG.
  - HLS: via ffmpeg's test pattern when ffmpeg is on PATH; otherwise HLS is skipped with a notice.
  - Used by the gateway tests (MJPEG / snapshot; HLS through recorded playlist + segment fixtures) and the browser check.
- `tools/cctv_relay/`: a MediaMTX config template (RTSP in → HLS out) + a README (install, point it at the camera's RTSP URL, put the HLS URL in Settings) + `check_stream.py` (fetches the configured stream once and reports format / resolution / fps, without printing credentials).

**5. Tests**
- **gateway:**
  - the ticket (expiry, session binding, never logged);
  - the MJPEG pass-through and the 2-viewer cap;
  - snapshot;
  - HLS rewriting and the no-open-proxy rule;
  - credentials never echoed (responses, logs, audit);
  - the settings fields (password write-only) and the URL rules.
- **web (vitest):** the toggle only when configured; the options per view (2 compact, 3 expanded); the memory fallback; streams closing when hidden; the state texts.
- **Browser check (supervisor):** the fake camera (MJPEG and HLS) at 1920 + 1280×600 @1.5:
  - the panel toggle;
  - the expanded side by side;
  - not configured = unchanged;
  - feed loss → retry → snapshot.
- Gates: touched tests + the non-sim suite once; web typecheck / lint / vitest.

**Order:** T5.7 does not depend on the ADS-B work and can be built while the ADS-B capture is awaited.

---

### Phase 5b — implementation plan (revised 2026-11-10, after adsb_interpreter P1b–P3; for user review)

Supersedes the build parts of the 11-07 spec where they differ (the 11-07 decisions Q35–Q50 stay, with Q45 changed by Q60).

**What adsb_interpreter delivered (read only for us; its BUILD_PLAN 11-10):**
- **`adsb_core` 0.3.0** (pure library, its J3 contract):
  - `platform.PlatformClient(url, token, on_event, kinds=("decoded",), ssl_context=…, on_status=…)` (pinned SIP AERO CA, states connecting / connected / stale / unreachable / auth_failed / tls_error / error);
  - `messages` (reads the real `cpr_*` fields);
  - `timebase` (Q40, `DelayStats`);
  - `height` (Q39);
  - `state.AircraftTable(site, range_limit_km)` (`ingest(envelope, now)`, `expire`, `snapshot(now)` → views with az / el / range);
  - `predict` (`position_at`, `path(ac, t0, t1, step)`; velocity + turn-rate projection, Kalman fallback, residual stats).
- **The capture questions are settled on 24 h of real data (our T5.4a is done there):**
  - U1: CPR fields `cpr_format` / `cpr_latitude_code` / `cpr_longitude_code`; `raw` is always null.
  - U2: decoded publishing is on.
  - U3: frame → decoded event p50 0.26 s / p95 0.80 s → stay on `decoded`.
  - U5: 78 % of messages carry `timestamp_status` 0 (source 6) → `received_at` time base.
  - No GNSS height (baro + the TC19 difference).
  - About 0.73 messages/s.
  - **Per-aircraft position every 1.0 s at p50, 8 s at p95.**
  - **Predictor residual p50 4.8 m / p95 21.5 m** (≤ 10 s ahead).
  - Receiver range p50 26 km, max 61 km (only the Bangkok area is heard).
- **The feed service (8500)** has a **simulated mode** (`python -m feed --simulate`): an 18-flight, 30-minute looping Bangkok scenario (arrivals / departures VTBS / VTBD, a holding turn, no-callsign, no-velocity, silent, beyond-range and a spoofed jump), served on its WS hub `/api/feed/ws` as 1 Hz `aircraft` snapshots (session token from `/api/feed/session`, first-message auth).
- The PC clock was resynced 11-10. A **second read token for pw-mount-control** is ready with the user (its D8).
- **Contract note:** adsb_interpreter reads the `location` block of our settings file (IMPORT FROM MOUNT CONTROL, read only). The `location` format must stay as it is.

**Decisions (user 11-10):**
- **Q59 simulated data:** validation only. In simulated mode our gateway reads adsb_interpreter's feed on 8500, started with `--simulate`. Live mode uses our own `PlatformClient` connection (Q55).
- **Q60 coast (replaces Q45's 5 s):** while tracking, the predicted path is re-sent every 1 s from the last fix. STOP when the last position is > 10 s old, or the predictor has been on the Kalman fallback for > 5 s.
- **Q61:** TRACK AIRCRAFT is allowed on simulated aircraft too (all safety rules apply).
- **Q62 mode + enable rule:**
  - simulated mode = the gateway launched with `--simulated-adsb` (or `PW_ADSB_SIMULATE=1`), used for the build and validation;
  - **the aircraft feature is enabled only on a green ADS-B connection**: `connected` or `stale` (connected, no data) are green; `connecting` / `unreachable` / `auth_failed` / `tls_error` / `error` / not configured / module missing = not green → aircraft display and TRACK AIRCRAFT disabled with the reason;
  - in simulated mode, green = the 8500 feed's WS is open and the feed reports `simulated`.

**Not prepared yet (found in the review):**
1. `adsb_core` is not installed in our `.venv` → an editable install of `../adsb_interpreter/adsb_core` (dev; a pinned git tag for releases, its D7), plus a version guard (accept 0.3.x).
2. Nothing on our side is built: the Settings ADS-B group is a disabled placeholder; Filters › Aircrafts, the Track Target AIRCRAFT tab and the A1b card are disabled.
3. The simulated scenario is served as processed views, not raw events → an adapter to `adsb_core.state.Aircraft`, so the same `predict.path` cues simulated and live aircraft.
4. Site height: adsb_core works in ellipsoidal height (HAE); our Settings altitude is MSL. Use adsb_interpreter's convention (HAE = MSL − 30 m geoid near Bangkok) for the aircraft geometry only.
5. **Few aircraft rise above Lim EL from our site** (approaches at 1 km at 20 km are ≈ 3° elevation; FL350 at 30 km ≈ 19°). The simulated Bangkok scenario may have few or no trackable aircraft → cueing is validated with our own synthetic overflights (adsb_core's DO-260B CPR encoder → envelopes → `AircraftTable`) in tests and on PWI4-sim; the 8500 scenario validates the live path, display and rules.
6. The platform was down for updates on 11-10 → the live connection is unit-tested (fake client); its first real check is the user's.

**T5.4b — gateway integration (`gateway/adsb.py`)**
- Optional import: `adsb_core` missing / failing / not 0.3.x → the gateway starts normally, every satellite / mount function works, and the ADS-B state is `module_missing` ("ADS-B module not installed").
- **Two sources behind one interface:**
  - **Live:** `PlatformClient` (URL + token from Settings) → `AircraftTable.ingest` → expire every 1 s.
  - **Simulated** (`--simulated-adsb`): a client of `http://127.0.0.1:8500` (session token via `/api/feed/session` with `Sec-Fetch-Site: none`; WS `/api/feed/ws`, auth first; reconnect with backoff) → per-ICAO views → the `Aircraft` adapter.
  - The feed's URL / port are configurable for tests (a throwaway feed).
- **Isolation:** the runner is its own task with bounded queues; a source error / slow source never blocks the poller, the 10 Hz hub or a satellite track (a test measures the 10 Hz rate with a misbehaving source).
- **Snapshot to the web:** a separate 1 Hz `aircraft` WS message containing:
  - the visible aircraft within the range limit and above 0°: ICAO, callsign, az, el, range, track, alt ft, gs, vertical rate, position age, stale, height source, predictor;
  - `lat` / `lon` for the selected or tracked aircraft;
  - `adsb: {state, green, simulated, detail, delay p50 / p95, counts}` (also in the 10 Hz status, for the chip).
- **Settings › ADS-B group** (enabled):
  - Enable; WSS URL (default `wss://10.22.123.68/api/v1/ws`); read token (write-only, Credential Manager `adsb_read_token`, never echoed);
  - default range limit (20 / 50 / 100 / 200 / 350 km, default 100);
  - the status line (state, last message age, delay p50).
  - In simulated mode the group shows SIMULATED (source: adsb_interpreter feed 8500) and the live fields are inactive.
  - The CA is adsb_core's bundled, pinned CA (no setting).
- **Tests:** the optional import (missing / wrong version), both sources (a fake `PlatformClient`; a fake 8500 feed server), the adapter vs `predict.path`, the green rule per state, token never in responses / logs / audit, isolation (hub rate).

**T5.5 — web**
- **Filters › Aircrafts:** enabled when green (child: Airborne).
- **Sky Viewer:**
  - aircraft icon rotated to the track, callsign label (Labels toggle), dimmed below Lim EL, faded when stale;
  - nearest-pointer hover tooltip; click selects;
  - interpolated between the 1 Hz updates;
  - a SIMULATED DATA badge in simulated mode;
  - an "ADS-B feed down" chip when enabled but not green.
- **A1b Aircraft card (Q43):** callsign / ICAO, Lat, Lon, Altitude ft, Range km, Az / El, GS, track, V/S, data age, height-source flag, [TRACK AIRCRAFT] + the reason line; the A1e pinning / stacking shared with satellite cards.
- **Track Target › AIRCRAFT tab:** the aircraft within the preset, search by callsign / ICAO, sort by range; TRACK SELECTED TARGET.
- **Browser check** on the simulated feed at 1920 + 1280×600 @1.5.

**T5.6b — aircraft cueing (`gateway/airtrack.py`, P23)**
- `track_aircraft {icao}` + the `/api/mount/check` dry run.
- **Q44 rules:** green connection, a fresh fix (≤ 3 s), above Lim EL (or crossing within the rendezvous), > 15° from the Sun, within the range limit, the near-zenith note / refusal (Q46).
- **Rendezvous** as T4.2, with `predict.path` instead of sgp4.
- **Every 1 s:**
  - the path from now − 0.5 s to now + 10 s at 0.5 s → az / el at the site (HAE);
  - new + add + apply(update_wrap = 0);
  - the wrap window set once before the rendezvous goto, centred on the start azimuth;
  - each path checked against Lim EL and the Sun's 16° circle, and cut before a violation.
- **End:** STOP, Q60 (fix > 10 s, Kalman > 5 s), connection not green, below Lim EL, out of range, Sun, the wrap window, or another command (G5). End = STOPPED with the reason on the card; audit `track_aircraft` / `track_done` (ICAO + callsign only).
- **Tests:**
  - unit tests on synthetic overflights (straight, turning, gaps up to 12 s → the Q60 stop, Kalman fallback);
  - sim tests on PWI4-sim driven by synthetic overflights (Sun pinned);
  - one run against the 8500 simulated feed if any scenario aircraft is above Lim EL from our site.

**T5.6c — the user's live check:** the live feed with the second read token (once the platform is back), then a real overflight cued on the L-350.

**Order:** T5.4b → T5.5 → T5.6b; one worker per task; the supervisor verifies each (browser checks need the user's 3200 / 8400 / 8410 stopped; the 8500 simulated feed is started by the supervisor on a throwaway port when possible, else by the user).

---

### Phase 5b — Aircraft display + cueing from ADS-B (spec, 2026-11-07; approved by the user 11-07)

> **Update 11-09 (user, Q55): the ADS-B pipeline comes from `adsb_core`**, the library of the sibling repo `C:\Users\soar\Desktop\DELV\adsb_interpreter` (read only for us; its D1). That repo is the reference for how ADS-B data is read and interpreted. Consequences for this spec:
> - **sections 1–2 are implemented by importing `adsb_core`**, not by our own modules: `adsb_core.platform` (the WSS client), `adsb_core.cpr`, `adsb_core.geometry` (done there in its P1a, 11-09), and `messages` / `timebase` / `height` / `state` / `predict` (its P1b, pending on the capture). Our gateway keeps: the runner task, Settings (URL, token in Credential Manager), the snapshot to the web, and everything in sections 3–4 (cueing, UI). The Q35–Q50 rules live in `adsb_core` and its tests are the contract (its J3).
> - install: an editable path install in dev, pinned to a git tag of adsb_interpreter for releases (its D7).
> - **isolation (user 11-09): the satellite side must work when the ADS-B side does not.** The gateway imports `adsb_core` optionally: missing / broken / a different major version → the gateway starts normally, satellites and every mount function work, and the aircraft UI stays disabled with the reason ("ADS-B module not installed" / "ADS-B feed down"). A feed failure never blocks or slows the poller, the WS hub or a satellite track; the ADS-B runner is its own task with bounded queues.
> - our gateway connects to the platform itself with its own read token; adsb_interpreter's servers (8500 / 8510) do not need to be running.
> - T5.4b / T5.4c shrink to the integration (runner, settings, snapshot, isolation tests); the tracker rows keep their numbers.
> - **clock:** adsb_interpreter measured this PC 0.41 s fast (Windows Time service not running, 11-09). That is ≈ 0.6° at 250 m/s and 10 km: the NTP sync is required before cueing (Q40).

**Goal:**
- Show live aircraft from the SIP AERO platform on the Sky Viewer (hover + info card), list them in Track Target › AIRCRAFT.
- Cue the mount onto a chosen aircraft (TRACK AIRCRAFT) with a predicted custom alt/az path that is refreshed on every update.
- Background and full reasoning: [docs/adsb_findings.html](adsb_findings.html) (the source review, error budget, Q35–Q50).

**Decisions (user 11-07: all recommendations accepted):**
- **Q35 transport:** WSS `wss://10.22.123.68/api/v1/ws`, read token in the first message, strict TLS with the bundled CA.
- **Q36 kind:** `decoded`; `live` only if the capture shows `decoded` lagging > ~1 s.
- **Q37:** the client runs in the **gateway** (the token never reaches the browser; the data sits next to the executor).
- **Q38:** own CPR decoder (no pyModeS: GPL-3.0).
- **Q39 height:** GNSS (TC 20–22) → baro + the TC19 GNSS−baro difference (≤ 60 s old) → baro alone (flagged). No refraction for aircraft.
- **Q40 time:** the payload `timestamp` when `timestamp_status ≠ 0` and `timestamp_source` ∈ {1, 3, 4}; else `received_at` (flagged). The feed latency is measured and shown. NTP on the mount PC is required.
- **Q41 prediction (user amendment):** primary = **the aircraft's reported velocity and heading** (TC19 east/north + vertical rate, ground track; turn rate from the recent track values). **Fallback = a Kalman filter (H3)** on the decoded positions when the velocity data is incomplete (no TC19 within 3 s, airspeed-only subtype without a usable vector, or a velocity that disagrees with the position history).
- **Q42 display:** airborne, above 0°, within the range limit; faded after 10 s without a position, removed after 60 s; no trails.
- **Q43 card:** the A1b spec (callsign / ICAO, Lat, Lon .5f°, Altitude .0f ft, Range .3f km) + Az / El, ground speed, track, vertical rate, data age, altitude-source flag.
- **Q44 TRACK AIRCRAFT active when ALL:** a position ≤ 3 s old from a completed CPR fix; airborne; above Lim EL now (or predicted to cross it within the rendezvous); > 15° from the Sun; within the range limit. Near-zenith note as Q18.
- **Q45:** no fresh fix for 5 s → STOP ("ADS-B data lost"); the predicted path entering the Sun's 16° circle → STOP before it ("Sun approaching"), no detour.
- **Q46:** near-zenith overflights allowed with the lag note; refused above a peak elevation set from P23.
- **Q47:** an ADS-B status line in Settings + a "feed down" chip on the Sky Viewer.
- **Q48:** the public CA bundled in the repo (`gateway/certs/sip-aero-ca.crt`) with its SHA-256 fingerprint pinned in code + a test.
- **Q49:** Phase 5b runs after the Phase 5 end (Tier 3) and before T6.
- **Q50:** automated tests use only a fake platform replaying recorded captures; never the live platform.
- **User 11-07 facts:**
  - the receiver sits on the same building as the mount (a slightly different position): the **Settings location serves as the receiver position** (the range origin and the CPR sanity check; the offset of a few tens of metres is negligible); no separate receiver-location setting;
  - recorded history will be available soon (the T5.4a input);
  - the mount PC reaches `10.22.123.68:443` through ZeroTier (to be confirmed on that PC).

**Reference (read only, like I1):** `C:\Users\soar\Desktop\DELV\sip-aero-python` (the vendor's client kit). Port the connection logic of `receive_ws.py` (`tls_context`, auth-first handshake + `hello` check, the packet loop, 1008 → fatal, 429 / 5xx → retry, backoff 1 → 30 s, TLS verify-reason text). Never modify that folder.

**1. Gateway — feed (`gateway/adsb_feed.py`)**
- An asyncio task started with the app when ADS-B is enabled in Settings; restarted on a Settings change.
- `connect(url + "?kinds=decoded", ssl=strict ctx, proxy=None, open_timeout 10, ping 20/20, max_size 1 MB, max_queue None)`; first message `{"type":"auth","token":…}`; expect `hello` with `authentication: "read"` within 10 s.
- Live-forward only: no `after_id`, no REST catch-up loop. On `gap`: log it, continue (old positions are useless). Optional at start: one `GET /api/v1/records?kind=decoded&limit=500` to warm callsigns / CPR pairs (only rows ≤ 60 s old are used).
- States for the status line: `disabled`, `connecting`, `connected`, `auth_failed` (1008 / bad hello; no retry until the token or Settings change), `tls_error` (verify code + reason), `unreachable` (retry with backoff), `stale` (connected but no decoded message for 60 s; heartbeat seen).
- Counters: messages, rejected (validation), last message age, latency p50 / p95 (`now − fix time`), decode fixes.
- **Validation** (pydantic, from `decoded_payload.schema.json`): schema const, ICAO `^[0-9A-F]{6}$`, numeric ranges (altitude −1 500…60 000 ft, speed ≤ 1 000 kt, angles 0–360, vertical rate ≤ ±10 000 fpm), packets > 64 KB dropped, unknown `decoded.type` ignored. Rejections are counted, not logged per message.
- **Secrets:** the token from the secret store (Credential Manager, key `adsb_read_token`), write-only in Settings like the astrometry key; never logged, audited, echoed or sent to the browser; the auth message is not logged.

**2. Gateway — aircraft state (`gateway/aircraft.py`)**
- Table by ICAO: callsign, category, CPR even / odd (codes, time), position (lat, lon, height, source, fix time, quality), velocity (east / north / up m/s, ground speed, track, time), GNSS−baro difference (time), signal, last seen.
- **CPR** (`gateway/cpr.py`): global decode from an even + odd airborne pair ≤ 10 s apart with the same NL; afterwards local decode against the aircraft's last position. The first fix always comes from a global pair (even / odd frames alternate at ~2 Hz, so it costs ≈ 1 s); the Settings site is not used as a first-fix reference, because the receiver can hear aircraft beyond 180 NM, where a site-referenced local decode is ambiguous. Reject > 350 km from the site or > 1 000 kt implied since the last fix; on rejection drop back to global.
- **Height (Q39)**, **time (Q40)** as decided.
- **Geometry:** WGS84 geodetic → ECEF → ENU at the Settings site → az, el, slant range (pure numpy; tested vs skyfield / astropy to < 1″).
- **Predictor (Q41):** `predict(icao, t) → (lat, lon, h)`.
  - Primary: from the last position fix, dead-reckon with the latest velocity (≤ 3 s old) on the local tangent plane; a turn rate from the last ≥ 3 ground-track values over ≤ 10 s (coordinated-turn arc when |ω| > 0.3°/s, capped at 6°/s).
  - Fallback: a constant-velocity Kalman filter (state E, N, U, vE, vN, vU) fed by the position fixes; used when the velocity is missing / stale or disagrees with the filter by > 30 m/s; the snapshot says which predictor is active.
  - Residual monitor: each new fix is compared with the prediction made for its time; p50 / p95 per aircraft kept for the card / log (this is how the H3 gains get tuned later).
- **Snapshot to the web** (WS hub, 1 Hz, a separate `aircraft` message, not inside the 10 Hz mount snapshot): per aircraft within the range limit and above 0°: ICAO, callsign, az, el, range, heading (track), alt ft, ground speed, vertical rate, age, flags (stale, alt source, predictor). The selected / tracked aircraft also gets lat / lon.
- Expiry: no position for 60 s → removed; no message for 5 min → table row dropped.

**3. Gateway — aircraft track (`gateway/airtrack.py`, using the P23 findings)**
- New command `track_aircraft {icao}`; validated like `track_object` (control lock, I3, audit `track_aircraft` with ICAO + callsign only).
- **Check** (`/api/mount/check` dry run + the card's live verdict): the Q44 rules; reasons in G7 style ("ADS-B position too old", "Below Lim EL", "Within 15° of the Sun", "Out of range", "Overflight too close to the zenith").
- **Rendezvous:** like T4.2: slew estimate → aim at the predicted position at now + slew + margin (iterate); first path point = the current pose; if the aircraft is below Lim EL but crossing soon, wait at the predicted crossing (≤ 60 s ahead, else refused).
- **Following loop** (each update, max 1 Hz):
  - predicted path from **now − 0.5 s** to **now + H** (H = 10 s), 0.5 s points, geometric alt / az; the first point is never in the future (P23: the mount would hold still until it);
  - **new + add_point_list + apply(update_wrap = 0)**;
  - the wrap window is set once, BEFORE the rendezvous goto (P9), centred on the start azimuth and clamped to the mechanical range (±180° of room either way); an update whose path would leave the window ends the track ("Track left the wrap window"), like T4.2 (P23);
  - every path is checked against Lim EL and the Sun's 16° circle; the path is cut before a violation and the track ends when the mount reaches the cut.
- **End:** STOP, a Q45 condition, the aircraft below Lim EL / out of range, the zenith rule, or a newer command (Go To / Home / satellite track replaces it, G5). End = `stop` + `tracking_off` → STOPPED with the reason on the card; audit `track_done`.
- The safety monitor (Sun / Lim EL predictive stop, link-loss stop G4) applies unchanged.

**4. Web**
- Settings › ADS-B group (enabled): Enable, WSS URL, read token (write-only), CA fingerprint (read-only), event kind (decoded / live), default range limit (20 / 50 / 100 / 200 / 350 km, default 100), status line (state, last message age, latency p50).
- Filters › Aircrafts tree enabled when configured (child: Airborne).
- Sky Viewer: aircraft icon (`/icons/aircraft.svg`) rotated to the ground track, callsign label (Labels toggle), dimmed below Lim EL, faded when stale; nearest-pointer hover tooltip (callsign / ICAO, alt, range, age); click selects; positions interpolated between the 1 Hz updates.
- A1b Aircraft card (Q43) with [TRACK AIRCRAFT] + reason line; A1e pinning / stacking rules shared with satellite cards.
- Track Target › AIRCRAFT tab: current aircraft within the preset, search callsign / ICAO, sort by range; TRACK SELECTED TARGET.
- A "ADS-B feed down" chip on the Sky Viewer when enabled and not `connected`.

**5. Tests**
- Unit: CPR (the 1090 MHz Riddle vector ICAO 40621D → 52.2572°, 3.9194°; zone boundaries; rejections), height selection, time-quality rules, ENU vs skyfield, predictor (straight, turning, missing velocity → Kalman), validation, token never in logs / responses.
- Fake platform (`PW_ADSB_FAKE=…`): a local WSS/WS server doing the auth handshake and replaying a recorded JSONL capture time-shifted to now; used by gateway tests and the browser gates.
- Sim (Tier 2 at the task end): aircraft tracks on PWI4-sim driven by the replay (Sun pinned with `PW_TEST_SUN_ALTAZ`).
- Real: the user only (a live feed check, then a real overflight on the L-350).

**6. Tasks**

| Task | Scope | Gate |
|---|---|---|
| T5.4a | Capture analysis (the user's recorded history): field names (CPR), kinds present, rates, latency, timestamp mix → spec update | Report + fixtures |
| T5.4b | `adsb_feed` + validation + Settings ADS-B (token in the secret store, CA pinned) + the fake platform | Unit + fake-feed tests; no token leak |
| T5.4c | `cpr` + `aircraft` state + geometry + predictor (velocity primary, Kalman fallback) + the 1 Hz snapshot | Vectors; replay → positions; residual stats |
| T5.5 | Web: filters, Sky Viewer layer + hover, A1b card, AIRCRAFT tab, feed chip, Settings group | Browser pass 1920 + 1280×600 @1.5 on the fake feed |
| T5.6a | ✅ Sim probe P23 (path replacement), done 11-07: replacement works (0.023° mean at 1 s updates) | Finding P23 |
| T5.6b | `airtrack` planner / executor + rules + monitor | Sim tracks from the replay |
| T5.6c | User live check: the real feed, then a real overflight | User |


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
| T5.1 | ✅ | ✅ 10-30 | ✅ 10-30 | ✅ 10-30 | ✅ 10-30 (+ T5.1d / T5.1e adjustments, user-checked) | O4; Q24–Q27 (5-min lead everywhere); a analysis engine (ssa comparison passed), b gateway lead (sim row passed), c web modal (browser pass 1920 + 1280×600 @1.5, real LEO [Track] → tracking) |
| T5.2 | ✅ | ✅ 10-30 | ✅ 11-02 | ✅ 11-02 | ✅ 11-02 (+ T5.2e resilience; 3 real astrometry.net solves by the user) | Q29–Q31; a port (OW0021 identical to ssa), b nova + jobs + relay (key-echo hardening), c modal, d run + pill + IOD wiring + 2 layout fixes |
| T5.3 | ✅ | ✅ 10-30 | ✅ 11-02 | ✅ 11-02 | ✅ 11-02 (+ T5.3d fixes; Vallado Ex. 7-2 verified by the user and as a test) | O6; Q28 built before T5.2; Q32–Q34; a engine (orekit reference: ours closer to truth), b modal (browser 1920 + 1280×600 @1.5) |
| T5.2f | ✅ | ✅ 11-09 | ✅ 11-09 | ✅ 11-09 | pending (user's real cases) | Q51–Q54; other-setup images + scale review; browser 1920 + 1280×600 @1.5 |
| T5.4 | ✅ spec approved 11-07, plan revised 11-10 | ✅ 11-10 (T5.4b) | ✅ 11-10 | ✅ 11-10 (simulated feed) | pending (live: the user's token, platform back) | a: done by adsb_interpreter (U1–U5, 11-10); b: gateway integration |
| T5.5 | ✅ spec approved 11-07, plan revised 11-10 | ✅ 11-10 | ✅ 11-10 | ✅ 11-10 (browser, simulated feed) | ✅ 11-10 user-checked (icon 20 px + sky-contrast outline; tooltip flip: user confirms live) | aircraft layer, card, AIRCRAFT tab, filters, badges |
| T5.6 | ✅ spec approved 11-07; T5.6b spec 11-10 | ✅ 11-11 (T5.6b) | ✅ 11-11 (unit / API, fakes) | ✅ 11-11 (PWI4-sim 5 / 5; browser on the 8500 simulated feed) | pending (T5.6c: live feed + real overflight) | a: P23 sim probe done 11-07; b: `airtrack.py` (D1–D4) |
| T5.7 | ✅ spec approved 11-09 (+ Settings scrolling) | ✅ 11-09 | ✅ 11-09 (unit / API) | **browser check owed** | pending | | | | | CCTV view prep (Q56–Q58); independent of the ADS-B work |
| T6.1 | ⬜ | | | | | |
| T6.2 | ⬜ | | | | | |
| T6.3 | ⬜ | | | | | |

**Resume point (2026-11-11, T5.6b done):** T5.6b built, PWI4-sim 5 / 5 and the browser check passed (see the T5.6b log), uncommitted. Next: T5.6c (the user: the read token in Settings › ADS-B once the platform is up, the live feed, then a real overflight); owed: the T5.7 CCTV browser check. Earlier (2026-11-10, T5.4b + T5.5 done): Phase 5 complete; T5.2f + T5.7 committed (`373fc39`); Phase 5b plan revised 11-10 (Q59–Q62); T5.4b gateway ADS-B + T5.5 web aircraft display done, uncommitted. Next:
1. **T5.6b aircraft cueing** (`gateway/airtrack.py`, P23 rules, Q60 coast), then T5.6c (the user's live check: token + platform). Owed: the T5.7 CCTV browser check; the tooltip-flip browser look.
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

### T5.6b — aircraft cueing (spec, 2026-11-10)

Builds on: the 11-07 Phase 5b spec §3 (aircraft track), P23, the revised plan (11-10), Q44 / Q46 / Q60–Q62.
Where they differ, this spec wins.

**Supervisor decisions made here (flagged to the user):**
- **D1 pointing site = PWI4's site** (`poller.pwi4_site()`, the mount's own location, as for satellites), height HAE = MSL + `SITE_HAE_OFFSET_M`. The aircraft display (T5.4b rows) keeps the Settings location. When the two differ by > 30 m horizontally the check adds a note "Settings location differs from PWI4's site by N m" (an aircraft at 3 km: 100 m ≈ 2°).
- **D2 refraction: the path is sent refracted** (`transforms.refract_alt` on the geometric elevation), for P18 parity (PWI4 and the monitor work in observed alt/az). This supersedes Q39's "no refraction for aircraft" for the pointing frame only. One constant `AIRCRAFT_REFRACT = True` (a stellar refraction slightly overestimates a target inside the atmosphere: ≤ 0.015° at 20° el).
- **D3 zenith rule = rates, not a fixed elevation (Q46):** the check predicts the overflight on a straight line (velocity projection, 180 s ahead, 1 s steps) and refuses when the required axis-0 rate on any point while above Lim EL exceeds `RATE_FRACTION` × the mount's axis-0 vmax (from `TrackMount`), reason "Overflight too close to the zenith". A predicted peak ≥ 87° (and allowed) gives the note "Near-zenith overflight: tracking lags near the peak". During the track, a path segment needing more than that rate is cut there (end reason "Overflight too close to the zenith"). The real L-350's vmax comes from the same mount parameters the satellite planner uses.
- **D4 snapshot:** a separate `airtrack` field (not the satellite `track`), so the satellite code is untouched. Satellite track and aircraft track are mutually exclusive: either one starting ends the other (G5, "Replaced by a new track").

**1. Command + check**
- `track_aircraft {icao}`: `icao` = 6 hex chars, upper-cased. In `commands.py` (ARG_MODELS, MOTION_COMMANDS, CmdName), `control.py` / `app.py` motion sets, CHECKABLE, the web `mount-socket.ts` command union.
- **One verdict function** shared by `/api/mount/check` (dry, `conn_id` None) and the real command (like `ControlService.track_verdict`): row 0 = `_motion_verdict` (lock, ready, mount, homed, sequence, D-SUN route, slewing...), then the aircraft rules in this order (first failing reason wins):
  1. ADS-B not green → the runner's `detail()` (e.g. "ADS-B feed down"); no runner / module missing → "ADS-B not available".
  2. ICAO unknown / no position → "Aircraft not in view".
  3. position older than 3 s → "ADS-B position too old" (Q44).
  4. beyond the range limit → "Out of range".
  5. below Lim EL now and not crossing it within 60 s (straight-line prediction) → "Below Lim EL".
  6. within 15° of the Sun now (`SUN_KEEPOUT_DEG`) → "Within 15° of the Sun".
  7. D3 zenith rate → "Overflight too close to the zenith".
  8. the rendezvous plan (§2) fails → its reason ("Cannot reach the aircraft in time", "Track does not fit the wrap window").
- Check body: `ok, reason, code, target {alt, az}` (now, observed), `notes` [], `aircraft {icao, callsign, peak_el, t_peak}` or null. Codes: `adsb_down`, `not_in_view`, `stale_fix`, `out_of_range`, `below_limit`, `sun`, `zenith`, `no_rendezvous`.
- Audit: `track_aircraft` (ICAO + callsign only, never positions / tokens), `airtrack_done` / `airtrack_stopped` with the reason and elapsed.

**2. Rendezvous**
- Like T4.2 (`slew_estimate_s` from `trackplan`): t_r = now + slew estimate + 3 s, iterated 3×, aiming at the PREDICTED position at t_r (`predict.position_at`). If the aircraft is below Lim EL now but crosses within 60 s, t_r = max(that, crossing + 0.5 s at Lim EL + 0.5°).
- t_r − now > 60 s → refused "Cannot reach the aircraft in time".
- The rendezvous goto = an ordinary validated `goto_altaz` job with `target0` (as `_track_command`), D-SUN routed if needed.
- **Wrap window set ONCE before the goto (P9 / P23):** centred on the rendezvous axis-0 (wrap_min = target0 − 180), clamped so the window [wrap_min, wrap_min + 360] stays inside the mechanical axis-0 range when that range is ≥ 360° wide (`TrackMount.axis0_range`). Today `_goto_wrap` sets `target0 − 180` unclamped: add an internal, gateway-only way for the rendezvous job to carry its clamped wrap_min (NOT a new public command argument; the public `goto_altaz` schema is unchanged). The executor never moves the window afterwards; a path point outside it is a cut (§3).

**3. Executor (`gateway/airtrack.py`, `AirTrackExecutor`, one per gateway, `ControlService.airx`)**
- Phases: `slewing` (rendezvous goto) → `waiting` (path uploaded, mount holds until t_r) → `tracking`.
- Arrival like `TrackExecutor._arrive` (within 0.3°, still). Late arrival → one re-plan; late again → stop "Missed the aircraft".
- **Update loop every 1.0 s** (`air_clock` / `air_sleep` test seams like `track_clock` / `track_sleep`):
  - a = the runner's `aircraft_for(icao)`; `predict.path(a, t0, now + 10, 0.5)` with t0 = now − 0.5 (tracking) or t_r (waiting: the mount must hold, P21);
  - each point → look angles from the D1 site → D2 refraction → observed (alt, az) + axis0 unwrapped inside the wrap window;
  - **cut** the path before the first point that is below Lim EL (+0 pad), within 16° of the Sun (`R_DETOUR_DEG`), outside the wrap window (`ROUTE_WRAP_MARGIN_DEG`), beyond the range limit, or over the D3 rate; the cut path's end reason is remembered (the track ends when the clock reaches the cut point + 1 s);
  - fewer than 2 points left → end now with that reason;
  - `custom_path_new("altaz")` + `custom_path_add_points` (JD from the absolute point times: this IS the latency compensation, the fix time drives the prediction) + `custom_path_apply(0)`;
  - the safety monitor's sky-corridor plan is replaced with the new path each update (`route_begin(..., sky=True)` or a new `route_replace` in safety.py if `route_begin` resets state that must survive); a normal 1 s replacement must never trip the corridor (test).
- **End conditions (STOP + tracking_off → state STOPPED with the reason, `airtrack.ended` for 30 s):**
  - Q60: last fix > 10 s old → "ADS-B data lost"; predictor `kalman` for > 5 s continuously → "ADS-B prediction unreliable";
  - ADS-B not green → its detail;
  - the cut reasons: "Aircraft below Lim EL", "Sun approaching", "Track left the wrap window", "Out of range", "Overflight too close to the zenith";
  - the aircraft disappears from the table → "Aircraft lost";
  - STOP / link loss / safety stop → the same `on_cancel` texts as the satellite track; another motion command, sequence or a satellite track → ended "Replaced by …" (G5), and a new aircraft track ends the old one.
- Never relies on the path hold (P23): the loop stops the mount itself; a crashed loop stops the mount (`_abort` as TrackExecutor).
- State override text: "Tracking CALLSIGN" (callsign, else ICAO), "Waiting for CALLSIGN".
- Snapshot `airtrack`: `{icao, callsign, phase, t_r, notes, ended: {icao, reason, at} | null}`; null when idle and nothing ended in the last 30 s.
- Simulated aircraft (Q61) are trackable: no special case.

**4. Web**
- `utils/aircraft.ts` `aircraftTrackVerdict`: the last branch uses the polled gateway check (`/api/mount/check {cmd:'track_aircraft', args:{icao}}`, 1 Hz, like TrackCard's satellite verdict: in-flight skip, generation counter, unreachable verdict). Keep the earlier local branches (no ADS-B / not green / not in view) so no request is sent then. Remove `AIRCRAFT_TRACK_PENDING`.
- TRACK AIRCRAFT button on the A1b card and TRACK SELECTED TARGET on the Track Target AIRCRAFT tab send `track_aircraft`; refusal text shown 3 s like satellites.
- The A1b card while this aircraft is tracked: the reason line shows the phase ("Slewing to rendezvous", "Waiting for the aircraft", "Tracking"), then the ended reason for 30 s; the button is disabled while it is the tracked aircraft (STOP is the existing global STOP).
- The tracked aircraft stays selected after the ack (selection store, like `afterTrackStarted`). The Sky Viewer marks the tracked aircraft (the same tracked-target ring the satellites use, if one exists; else no new visual — list it).
- Mount status shows the override text (already generic).

**5. Tests**
- Unit (`gateway/tests/test_airtrack.py`, fakes only: `FakePwi4`, `fake_adsb`, a fake runner exposing `aircraft_for` / `green` / `detail`; synthetic `adsb_core.state.Aircraft` built with `adapt_feed_aircraft` from `offset_row` rows):
  - each check rule (reason + code), the check endpoint body, `track_aircraft` refused / accepted;
  - straight overflight: rendezvous, wrap set once before the goto, updates every 1 s with first point = now − 0.5 (tracking) / t_r (waiting), JD times, point spacing 0.5 s, ≤ 10 s ahead;
  - turning flight (turn_dps) follows `predict.path`;
  - gaps: fix 9 s old → continues; 11 s → "ADS-B data lost"; kalman 6 s → "ADS-B prediction unreliable";
  - cuts: Lim EL exit, Sun 16°, wrap window, range, zenith rate; ends at the cut + 1 s;
  - feed goes not green mid-track → stop;
  - G5: satellite track ↔ aircraft track ↔ Go To replace each other; STOP / link loss;
  - D1 note, D2 refraction applied, D3 refusal + note;
  - the corridor monitor not tripped by 30 s of 1 s replacements on a straight overflight;
  - audit has ICAO + callsign only.
- PWI4-sim (`gateway/tests/test_airtrack_pwi4sim.py`, `PWI4_SIM=1` only, `sim_guard` via the shared `pwi` fixture, Sun pinned with `PW_TEST_SUN_ALTAZ`, a throwaway gateway with `--simulated-adsb` against `fake_adsb.FakeFeed` on 18750–18759, the feed fed by a synthetic overflight generator at 1 Hz with 0.3 s latency, positioned relative to PWI4's site):
  - GA fly-by (3 km, 130 m/s, 2 km aside); cruise (10.7 km, 250 m/s, peak ~60°); a 3°/s turn: mean error vs truth ≤ 0.05°, max ≤ 0.3° while tracking (after 3 s settle);
  - feed stop mid-track → STOP within 10 s + 1.5 s, mount still afterwards;
  - Lim EL exit → ended at the cut, mount stopped;
  - the wrap window restored by the fixture.
  The worker writes these but does NOT run them (the supervisor runs them when the user has PWI4 in Simulator mode and their servers stopped).
- Web vitest: the verdict branches (incl. the polled check), the card's phase lines, the button send + refusal.
- Gates: touched gateway tests + the non-sim gateway suite once; ruff on touched files; web typecheck / lint / vitest.

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
| 2026-11-10 | Q59–Q62 | Simulated ADS-B (validation only) = adsb_interpreter's feed on 8500 started with --simulate; coast 10 s (Q45 → Q60: stop when the last fix > 10 s or Kalman > 5 s); TRACK AIRCRAFT allowed on simulated aircraft; simulated mode = gateway launched with --simulated-adsb; the aircraft feature is enabled only on a green ADS-B connection (connected / stale) | user |
| 2026-11-09 | Q55–Q58 | ADS-B: pw-mount-control imports adsb_interpreter's `adsb_core` (optional import; the satellite side works without it; own platform connection). CCTV (T5.7): one camera, streamed through the gateway (credentials server-side), prepared for MJPEG / HLS now and RTSP via a relay sidecar (likely an IP camera); panel 3D | CCTV, expanded adds Side by side; nothing shown when not configured | user |
| 2026-11-09 | T5.2f Q51–Q54 | Astrometry: an Image source toggle (Station / Other setup, auto-suggested from the file's FITS optics); Other = no station hints; a solve is never failed for its scale: a scale that differs from the station is reviewed (Use / Discard) in the pill and the waiting IOD row; other-setup images are from the station's location (the Settings location is moved for another site) | user |
| 2026-11-07 | Phase 5b Q35–Q50 | ADS-B from the SIP AERO platform: WSS + read token in the gateway, `decoded` kind, own CPR decoder, height GNSS → baro + TC19 difference → baro, payload time when GNSS/NTP-qualified, prediction from the reported velocity + heading with a Kalman fallback when the data is incomplete, display / card / rules / stop rules as recommended, CA bundled + pinned, fake-platform tests only, Phase 5b before T6. The receiver is on the mount's building (Settings location = receiver position) | user |
| 2026-10-30 | T5.3 user check | IOD checked live (accuracy tested end-to-end after T5.2). Sort ▲ as Pass Analysis; width 660 kept; the gateway must be up to use the tools; the in-row confirm kept; O6 3° rule kept as a standing rule until real-data tests; T5.3 is committed together with T5.2 | user |
| 2026-10-30 | T5.2 / T5.3 Q28–Q34 | Build IOD (T5.3) before Astrometry (T5.2). Astrometry: image work + the nova job in analysis, the gateway relays the API key server-to-server; scale hint from FITS FOCALLEN / XPIXSZ else a new Settings camera pixel size + RASA11 620 mm; server-side jobs survive a refresh (one at a time, job pill). IOD: SGP4-fitted coarse TLE; display + copy only; the modal state is recalled until a refresh | user |
| 2026-10-30 | T5.1 user confirmations | Sort arrows ▲ asc / ▼ desc (not the render); a satellite change clears the result, mode / window keep it; reopening recalls the last modal state until a page refresh; rises / sets outside the listed window show "—" (duration clipped to the window); exact search matches first (Track Target + picker) | user |
| 2026-10-30 | T5.1 Q24–Q27 | Pass Analysis: optical darkness stays O4 (Sun < 0°; the ssa comparison runs ours at −6°); all modal times UTC; SEARCH reuses the Track Target table in picker mode; [Track] on passes in progress or reaching Lim EL within 5 min, and the 5-min lead applies to every TRACK OBJECT (card, modal, Pass Analysis): an object below 0° is trackable when its Lim EL crossing is ≤ 5 min ahead (meeting point + wait) | user |
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
| P23 | **Aircraft-style path replacement (sim, T5.6a, 11-07; synthetic fly-bys, status at 10 Hz; script + raw JSON in the session scratchpad `p23/`):**<ul><li>**`custom_path/new` + `add_point_list` + `apply(update_wrap=0)` on a RUNNING path replaces it smoothly**: apply 0.05–0.09 s; no stop, no jump. A small bump after each replacement (0.035–0.04° in the first 0.3 s, 0.015° later).</li><li>GA fly-by (3 km, 130 m/s, 2 km aside, peak 56°, az ≤ 3.7°/s), error vs the true position: one upload 0.015° mean / 0.066° max; replaced every **1 s 0.023° / 0.17°**, every **2 s 0.019° / 0.15°**, every 0.5 s 0.030° / 0.19° (more bumps).</li><li>**The first point must be in the past** (now − 0.5 s): with the first point 0.5 s ahead the mount holds still until it (P21) at every replacement → 0.14° mean / 0.37° max.</li><li>Prediction with 1 s feed latency: straight flight, velocity-based extrapolation 0.025° / 0.17° (= exact); **3°/s turn with a straight-line prediction 0.18° / 0.27°**; the same **without latency compensation ≈ 2.0°** (the along-track lag v·L/R).</li><li>Cruise overflights (10.7 km, 250 m/s): peak 82.5° (az 10.2°/s) 0.015° / 0.11°; peak 86.3° (az 20.3°/s) 0.015° / 0.10°; the sim axis 0 reached 36°/s. **The sim does not show the real azimuth limit**: the zenith refusal (Q46) must come from the real mount's rates.</li><li>Feed stop: the path ends at its last point (+10 s) and the mount **holds that pose with `is_tracking` True** (P21 confirmed) → the executor must STOP on feed loss (Q45).</li><li>Probe mistake, re-run: the first C / D runs placed the route beyond the sim's +300° axis-0 limit (the axis stopped at 300.01°). With the wrap window set before the goto and the route inside ±290° they were clean.</li></ul> | T5.6b:<ul><li>replace the path at **1 s** (2 s is as good; not faster than 1 s) with points from **now − 0.5 s** to now + 10 s;</li><li>latency compensation is mandatory (fix time → now);</li><li>set the wrap window once, before the rendezvous goto, centred on the start azimuth and clamped to the mechanical range (±180° of room); a later path outside it ends the track;</li><li>STOP on feed loss (5 s), never rely on the path hold;</li><li>Q46 zenith limit from the real L-350 (real-mount checklist: azimuth rate on an overflight).</li></ul> |

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

~~Open item~~ **fixed 11-06 (user request):** a GEO about to be covered by the Sun disc now reads "Target is too close to the Sun; try again later" (code still `sun_no_detour`).

### T5.1a / T5.1b log (2026-10-30)

- **T5.1a (worker, verified):**
  - `analysis/passes.py` + `POST /api/passes` + `analysis/tests/test_passes.py` (11 tests);
  - vs a 0.1 s brute force: crossings ≤ 0.035 s, peak ≤ 0.00002°;
  - 3 days ≈ 1 s.
- **Supervisor ssa-toolkit comparison:**
  - Run in the `orekit310` conda env, in-process, `PYTHONDONTWRITEBYTECODE`; ssa-toolkit `git status` unchanged; HEAD `84f2ded`.
  - Inputs: ISS 25544, TERRA 25994, NAVSTAR 86 68791; site 13.12 / 100.92 / 0 m; Lim EL 10; 2026-10-30 00:00Z + 3 d.
  - **LOS:** pass counts equal (8 / 8 / 7); rise / set ≤ 0.13 s; max el ≤ 0.006°; az ≤ 0.009°.
  - **OPTICAL at −6°:** the same observable passes (0 / 0 / 4); visible edges ≤ 2.2 s.
  - **Edge differences (by design):** a pass in progress at Start shows its true rise (ssa clips to Start); a MEO pass setting > 2 h after End has `t_set` null ("—"), where ssa clips to End.
  - Script: the session scratchpad `t51_cmp/`.
- **T5.1b (worker, verified):**
  - `LEAD_S` = 300; `_lead_pass` in `compute_pass`; row 2 / row 3 (`sun_sep_in` = the separation at `t_in`, with the Sun at check time: ≤ ~1.25° drift over 5 min; the planner checks the real Sun path).
  - `trackcheck` no longer caches pass-less below-horizon entries (the lead window would hide for up to 60 s).
  - Units for passes / check / plan / exec.
  - Supervisor re-run of the new sim row `test_below_horizon_lead_waits_and_tracks`:
    - passed in 455 s;
    - object at −2.13° at the command; t_r − t_cross 5.3 s; waiting 243 s;
    - following error ≤ 0.0095°; min alt 15.06 (Lim EL 15);
    - "Track ended at Lim EL"; the `PWI4.cfg` hash unchanged.
- **Gates:** non-sim 642 passed; ruff clean.
- **Docs:** the logic sheet A1d (txt + html) and the T4.1 rule table rows 2 / 3 carry the lead.

### T5.1c log (2026-10-30)

- **Worker deliverables:**
  - `components/analysis/PassAnalysisDialog.vue`, `utils/pass-analysis.ts`;
  - the `TrackTargetDialog` `mode="pick"` prop + `picked` emit;
  - ToolsGroup SATELLITE PATH ANALYSIS enabled;
  - a `~` alias in `vitest.config.ts`;
  - 3 new spec files.
- **Gates (supervisor re-run):** typecheck 0; lint 0 errors (11 old warnings in `components/ui`); vitest 480 (was 454).
- **Choices the spec left open:**
  - site + Lim EL from the gateway snapshot (= the Settings location, the same as the Sky Viewer);
  - a satellite change clears the result; a mode / window change keeps it, and the header follows the listed mode;
  - reopening resets the modal;
  - the prefill name comes from `/api/sats/search`;
  - 404 → "Satellite not found";
  - one gateway check per listed NORAD, 1 Hz while a [Track] button shows.
- **Real browser** (throwaway analysis 18710 + gateway 18733, Sun pinned, Nuxt 13270, PWI4 sim):
  - **1280×600 @1.5:**
    - dialog 12–588 px of 600; the table area scrolls (336 / 336); no page overflow;
    - picker: title SELECT SATELLITE, SELECT TARGET disabled until a row is picked, no TRACK / status; it closes and fills "TERRA NOVA (100909)";
    - LOS listing: 2 rows; the optical listing: "No visible passes above Lim EL in this window";
    - a mode toggle keeps the listed result;
    - sort Max El ▲ / ▼ reorders;
    - the ᴹ ˢ glyphs render in Barlow.
  - **1920:**
    - a real LEO (HULIANWANG DIGUI-214, 100973) rising in 222 s: prefilled from the dashboard selection;
    - [Track] enabled ("Track this object") → click → snapshot slewing → waiting → tracking;
    - the row reads TRACKING (disabled); the dashboard selection = 100973.
  - Ports free afterwards; the sim was restored.
- **For the user to confirm:** the render shows "Rises (UTC) ▼" on the default ascending sort; the app uses ▲ = ascending (the Track Target convention).

### T5.1d (2026-10-30, the user's confirmations before the live check; supervisor changes)

- **A:** keep ▲ = ascending / ▼ = descending (not the render's ▼). **B:** the result rules as built.
- **C, recall:** reopening the modal recalls the last state (satellite, mode, window, result, sort); a page refresh forgets it.
  - `utils/pass-analysis-memo.ts` (module scope, not storage);
  - saved on an open → closed transition only (an immediate watch at mount would otherwise store an empty state);
  - the dashboard prefill applies only when nothing is recalled.
- **D, window edges:** a rise before Start or a set after End shows "—" (time and azimuth: `clipToWindow`). Sorting uses the shown values; [Track] keeps the real times. Durations were already clipped by the analysis.
- **E, exact match first:**
  - analysis `search` orders exact name / COSPAR / NORAD matches first, so the 500 limit never cuts them (new test with its own bulk DB);
  - the Track Target table keeps exact matches of the query on top (`exactFirst`).
- **Gates:** vitest 484; typecheck 0; lint 0 errors; analysis 52; ruff clean.
- **Browser** (1280×600 @1.5):
  - "TERRA" picks TERRA (25994);
  - close → dashboard selection ISS → reopen: TERRA, LOS, 3 rows, "Duration" recalled;
  - ports free.

### T5.1e (2026-10-30, the user's live check: sizing; supervisor changes)

- **Live check result:** functionally clean (items 2–9). Sizing nudges agreed from a measured proposal.
- **Applied:**
  - modal 960 → **820**;
  - form grid `64 | 272 (field 200 + SEARCH 72; Mode 272) | free | 40 | date suite flush right`, 10 px gaps; the Start / End labels left-aligned, so both suites start at the same x;
  - table columns = widest content / header + a 20 px gap: Rises 150 (+14 inset), Rise Az 80, Max El 75, Sets 150, Set Az 80, Duration ≥ 125, [Track] 84 (+14); table 774;
  - Sets always `YYYY-MM-DD hh:mm:ss` (`fmtSet` removed);
  - a click (or Enter) on the Satellite value field opens the picker.
- **Measured** (analysis + Nuxt only, the snapshot injected; the user's 8400 was running, so no gateway / PWI4):
  - 1920 and 1280×600 @1.5: dialog 820, form 774; Start / End labels both at the same x; suites flush with the table edge; columns 164 / 80 / 75 / 150 / 80 / 127 / 98;
  - no page overflow; the field click opens SELECT SATELLITE.
- **Gates:** vitest 484; typecheck 0; lint 0 errors.
- **Follow-up (user):**
  - Duration column −40 (≥ 85) → modal **780**, table 734;
  - the "Visible Duration" header overflows into the empty [Track] header (z-index 3), fully readable;
  - measured at 1920 + 1280×600 @1.5 (both modes).
- **T5.1 user-checked; the user commits it.**

### T5.3a / T5.3b log (2026-10-30, IOD built before T5.2 per Q28)

- **T5.3a (worker, verified):**
  - `analysis/iod.py` + `POST /api/iod` + `analysis/tests/test_iod.py` (28); analysis 80 passed; ruff clean.
  - **Two-body truth:** state recovered exactly (≤ 1e-5 km / m/s).
  - **SGP4 truth:** LEO ≈ 1–1.7 km / 7–10 m/s, MEO 0.9 km / 0.18 m/s, GEO 0.3 km / 0.05 m/s. This is the J2 / SGP4 physics a two-body fit cannot absorb; the spec's ≤ 1 km / 1 m/s vs SGP4 was too strict for LEO, so the tests assert it vs two-body truth and ≤ 3 km / 25 m/s vs SGP4 for LEO.
  - **TLE fit:**
    - 2–6 m (LEO), 12–22 m (MEO / GEO: the TLE text resolution);
    - converged in every case;
    - TLE-predicted directions within 0.033° at t1 / t3.
  - **Additions:** a Levenberg-Marquardt refinement takes over when Alg. 5.6 drifts on long MEO / GEO arcs; candidate roots are tried in increasing |r2|; the response gains `roots`.
  - O6 applied literally: LEO 60 s (3.8°) uses Gibbs; Herrick-Gibbs at 30 s.
- **T5.3b (worker, verified):**
  - `IodDialog.vue`, `utils/iod.ts`, `utils/iod-memo.ts`, ToolsGroup INITIAL ORBIT DETERMINATION enabled; vitest 514 (+30); typecheck 0; lint 0 errors.
  - **Choices:**
    - row states empty / pending / failed / partial / valid ("partial" = a typed Az / El not yet converted);
    - only a valid row asks before replace / remove (an inline confirm in the row);
    - ▲ = ascending (the app convention);
    - 360 wraps to 0;
    - El 0–90;
    - plain text inputs (≤ 4 decimals);
    - Esc closes menu → confirm → edit → dialog;
    - an in-dialog menu;
    - modal 660 px.
- **Supervisor reference check (orekit `IodGauss`, `orekit310`, in-process, I1 unchanged):** real TLEs from the T5.1 passes.

| case | ours vs truth | orekit vs truth |
|---|---|---|
| ISS 60 s | 0.82 km / 9.9 m/s | 1.80 km / 21.7 m/s |
| ISS 150 s | 0.78 km / 9.9 m/s | 6.94 km / 82.5 m/s |
| TERRA 120 s | 1.23 km / 14.3 m/s | 4.88 km / 53.9 m/s |
| NAVSTAR 86 1200 s | 1.29 km / 0.21 m/s | 88.0 km / 14.7 m/s |

  orekit's plain Gauss has no iterative improvement; ours is closer to the truth in every case.
- **Browser** (throwaway analysis + gateway for H4 + Nuxt, no mount commands):
  - **1920:**
    - Add Manual RA / Dec → H4 filled Az / El dimmed (opacity) at the row epoch;
    - 3 ISS rows → "Done · Gauss + Gibbs";
    - R_2 / V_2 within 0.07 km / 1 m/s of the direct engine run (the web sends 4-decimal angles);
    - TLE i 51.63°, n 15.555 rev/day;
    - the replace confirm "Replace row 1? Yes / No";
    - recall after close / reopen.
  - **1280×600 @1.5:** the dialog 660 × 473 inside 600; outputs visible; no page overflow.
  - Ports free.

### T5.2a / T5.2b / T5.2c log (2026-10-30)

- **T5.2a (worker, verified):**
  - `analysis/astrometry/` = the photometry port (types, io, background, detect, morphology, streaks, trails, select, engine) + `sessions.py` (3 frames, 30 min TTL) + `service.py` + `api.py` (`/image`, `/detect`, `/snap`).
  - Sidereal branches kept unreachable, so the modules stay port-identical; `engine.raw` is float32.
  - **New deps:** photutils 3.1.1 (`<4`), scipy 1.18.1, scikit-image 0.26.0, tifffile 2026.10.15, pillow 13.0.0, requests 2.34.2, python-multipart 0.0.32.
  - **OW0021 detection vs ssa-toolkit's own engine** (in-process, read-only, I1 kept): identical, 40 / 10 / 23 streaks, the target to 0.01 px; 61 MP frame: load 0.3 s, detect 5–7 s.
  - In object mode, edge-clipped streaks are already dropped by `select` before emission, so no flag is needed downstream (supervisor check).
  - The OW0021 TIFFs carry no epoch / exposure; they are typed manually.
- **T5.2b (worker, verified + one supervisor hardening):**
  - `nova.py` (the real client: a synthetic-FITS upload, the WCS download; the fake), `solve.py` (the object solve, the mapping, the scale check 0.8–1.25 vs the hint, mid-exposure), `jobs.py` (single active, the last 20, cooperative cancel), `/run` `/job` `/job/{id}` `/cancel`;
  - `PW_ASTROMETRY_FAKE=1` = a deterministic fake (RA 150 / Dec 20, ~3 s);
  - the gateway relay `POST /api/mount/astrometry/run` (key from the secret store, 620 mm + `camera_pixel_um` hints, 409 no key, 503 analysis down; audit `astrometry_run` with the status only);
  - Settings `hardware.camera_pixel_um`.
  - **Supervisor:** a FastAPI 422 can echo its whole input; the relay now replaces any analysis reply that contains the key, or any non-string 422 detail, with "The astrometry request was rejected (…)" (new test).
  - The real nova HTTP paths are untested (no network in tests); the first real run is the user-approved end-to-end test.
- **T5.2c (worker, verified):** `AstrometryDialog.vue`, `useZoomPane.ts`, `utils/astrometry.ts`, `pages/dev/astrometry.vue`; vitest 545.
- **Supervisor browser check** (`/dev/astrometry`, throwaway analysis 18711 + Nuxt 13272; the `files` step added to `tools/browser_gate/cdp.mjs`):
  - real OW0021_130130 loaded → DETECT: 40 streaks, the target at 5152.2 / 3151.8; RUN title "Enter the exposure start time";
  - zoom 156 %; the overlay toggle;
  - no page overflow at 1920 / 1280×600 @1.5.
  - **Found:**
    - the Epoch field overflows the 244 px left column over the "Image Preview" heading;
    - the status line shows the 3-line engine warning (the dialog grows ~45 px).
    - Both go to T5.2d.
- **Gates:** analysis + gateway non-sim 802 + the relay test; ruff clean.

### T5.2d log (2026-10-30)

- **Worker deliverables:**
  - `utils/astrometry-job.ts`, `stores/astrometry-job.ts` (polling 2.5 s, reattach, cancel, dismiss in module scope + sessionStorage), `AstrometryJobPill.vue` (bottom-left, mounted in `pages/index.vue`);
  - IOD Add Astrometry wired (stacked `AstrometryDialog`, the relay, pending / solved / failed rows, resolved even with IOD closed via the memo);
  - Settings "Pixel [ ] µm" (90 px, title "Camera pixel size"; the 100 px label column);
  - layout fixes: (a) Epoch / Exposure on one full-width row; (b) a one-line status + the full warnings in the `title`.
  - "Apply to a row…" after a refresh = the first empty row (else the first non-usable one; all valid → a message).
- **Supervisor fix:** a refused RUN (`runStatus` error) was hidden behind the detect summary on the status line; error now takes precedence (+ test). vitest 567; typecheck 0; lint 0 errors.
- **End-to-end browser gate** (analysis 18711 `PW_ASTROMETRY_FAKE=1` + gateway 18734 memory secrets with a dummy key + Nuxt 13272; 1920 and 1280×600 @1.5):
  - IOD › Add Astrometry → OW0021_130130 → DETECT (40 + the target; "40 streaks detected, target found · 8 edge streaks excluded") → epoch 13:01:30 / 2 s → RUN;
  - the dialog closes; row 1 "Astrometry pending" at 13:01:31 (mid-exposure); pill "Astrometry · Uploading to astrometry.net · Cancel";
  - → row 1 RA 149.461° / Dec +19.941° bright, Az / El 68.376° / 69.335° by H4 dimmed, Source Astrometry; pill "solved — row 1";
  - refresh → pill reattached "solved — apply to a row…";
  - the Epoch row does not overlap the preview; the dialog height is steady (614 → 617 / 527 → 530 of 600); no page overflow;
  - ports free.

### T5.3d IOD fix pass (2026-11-02, from the user's IOD test)

- **User test results (11-02):**
  - IOD verified against Vallado (4th ed.) Example 7-2 (pp. 447–449): very close to the known orbit (closer than the book's Gauss);
  - three issues found: H4 needed PWI4; IOD used PWI4's location; Add Manual on rows 2 / 3 did not work.
  - **Confirmations:** A (apply to the first empty row) and B (the "Pixel" label) kept; C the pill moves to the Sky Viewer bottom-left; D whole-second epochs kept, angle inputs up to 6 decimals (display 3, the full value restored on edit); D* Edit Entry; E1 an edited Astrometry row becomes Manual; E2 failed rows are replaced, not edited.
- **Causes:**
  - **(1 + 2)** `/api/transform` defaulted to PWI4's site (503 without PWI4) and IOD sent no site. The snapshot / `POST /api/iod` already used the Settings location.
  - **(3)** On a filled row, Replace → Yes removed the focused button; the row's focus-out saw no focused element and cancelled the blank edit; `startEdit` also focused the first row's Az cell (reproduced in a browser).
- **Fixes:**
  - **gateway (supervisor):** H4 defaults to the Settings location (logic sheet H4), never needs PWI4 (Go To motion keeps PWI4's site behind the mismatch gate); a test with PWI4 down.
  - **web (worker):**
    - the edit focuses its own row's input; a focus loss to nothing no longer commits / cancels; a capture-phase outside-pointer commit;
    - Edit Entry (valid / partial rows; full-precision prefill; Esc restores; Astrometry → Manual);
    - 6-decimal inputs stored as typed;
    - every H4 request carries the Settings `site`;
    - the pill inside the Sky Viewer `SectionFrame` (absolute, 8 px left / bottom).
  - vitest 579; typecheck 0; lint 0 errors.
- **Regression test:** Vallado Example 7-2 added to `analysis/tests/test_iod.py`. Ours vs the known orbit: |Δr| 0.52 km, |Δv| 0.58 m/s, a 12 242.5 km (truth 12 246.0), e 0.19985, i 39.9996°, Ω 330.0005° (the book's EOP corrections are not applied).
- **Found + fixed (supervisor):**
  - `analysis/app.py` and `analysis/requirements.txt` were rewritten at 10-30 10:16:33 UTC, after the last handoff, most likely by the T5.2a worker's lingering background task (its notice said background work was still running).
  - The rewrite registered the astrometry router twice with two different `ImageSessions` (`/image` + `/detect` on one store, `/run` on another → a real RUN would 404 "Image expired") and duplicated the deps list.
  - Removed both; added an assembled-app test (`/image` then `/run` through `create_app()`).
  - Only those two files were touched.
  - **Process lesson:** briefs now require workers to leave no background process running, and the supervisor checks file mtimes vs the handoff at the start of a session.
- **Browser check** (throwaway Nuxt 13270, the snapshot injected, the user's servers untouched; 1280×600 @1.5):
  - Replace → Yes on a filled row 2 opens the edit focused in that row; 33.123456 / 44.654321 commit (shown 33.123° / 44.654°);
  - Edit prefills the full values; Esc restores the row and keeps the dialog open;
  - an edit to 33.987654 shows 33.988°, and reopening shows 33.987654;
  - the pill at the Sky Viewer's bottom-left, clear of the clock, but its width cap truncates "Astrometry · Solving" to "Astrometry · Solv…" at 1280 (option offered to the user).
- **Gates:**
  - analysis + gateway non-sim 804 (6 timing-budget tests `test_compute_pass_budget_30ms` failed once while web builds loaded the machine, then passed on re-run → the flaky list);
  - analysis 209 incl. Vallado + the assembled-app test;
  - ruff clean.
- **Follow-up (user 11-02): a two-line pill.**
  - The title on top ("Astrometry" / "Astrometry solved" / "Astrometry failed", muted / green / red), the status below at full width ("Solving", "row N", "apply to a row…", the reason).
  - Browser, 1280×600 @1.5 + 1920: all running / solved texts fit; only the longest failure reason truncates at 1280 (the full text in the hover title); clear of the clock.
  - vitest 579; typecheck 0; lint 0 errors.

### T5.2e astrometry.net resilience (2026-11-02, from the user's real test)

- **User real test (11-02):**
  - the solve works; valid RA / Dec fill the row, and Az / El are dimmed and correct;
  - first run: a 409 from astrometry.net mid-job → "Astrometry failed / unreachable"; the re-run solved but created a 2nd submission on astrometry.net.
  - The gateway audit showed both relays 202, so the 409 was astrometry.net's.
- **Cause:** `NovaHttpClient` (as in ssa-toolkit) had no retries: one bad status anywhere killed the job, every error mapped to "unreachable", nothing was logged.
- **Fix (worker, user-approved + amendment):**
  - per-request retry (connection / timeout / 409 / 429 / 5xx; 2, 5, 10 s); upload retried only on no response or 429 / 503;
  - poll tolerance (5 consecutive failures to give up);
  - results-not-ready retry (30 s);
  - a 30 min solve limit (slow ≠ failed); 60 s per request;
  - resume (`POST /api/astrometry/job/{id}/resume` + the gateway relay, the same key-scrub hardening) re-polls the same subid with no upload;
  - re-RUN fingerprint (`image_id` + the star midpoints + the hint scale) re-attaches to the remembered subid or reuses a stored solution — never a second upload unless astrometry.net said "could not solve" or the detections changed;
  - timeout / cancel stop all polling (tested: no HTTP calls after the end) and keep the subid (resumable);
  - readable reasons ("busy (HTTP n) — retried 3×", "unreachable (network)", …);
  - one log line per step attempt (no key / session; tested);
  - web: a Retry button on a resumable failed pill; the IOD row re-pointed to the new job.
  - astrometry.net has no cancel API: nothing is sent on cancel.
- **Gates (supervisor):** analysis + gateway non-sim 845; ruff clean; web 585; typecheck 0; lint 0 errors. The latest write 05:28 UTC, before the report (no lingering task).

### T5 close (2026-11-02, user-checked)

- **User real test after T5.2e:** three astrometry.net uploads, all solved; no 409 / failure; no double submission.
- **The user keeps exercising the astrometry engine** and reports any uncaught error cases.
- **The user commits T5.2 + T5.3 (+ T5.3d, T5.2e)** as one commit.
- **Owed (Phase 5 end, Tier 3) next session:** full sim suite ×2 (the user's 8400 stopped), the harness, the full browser pass (1920 + 1280×600 @1.5: Pass Analysis, IOD, Astrometry with a fake nova, the pill, Settings), the flaky list (`test_compute_pass_budget_30ms` under CPU load).

### Real-mount test (user, 2026-11-06, PlaneWave L-350)

- **HOME:** works on the real mount; HOME with the Sun in the way is blocked correctly. **Home is cleared** (no longer an open status item).
- **D-SUN:** works for every tracking and motion action (Go To, Jog, HOME, track segments).
- **Mechanical limits:** with the mount homed at start, Jog and slews never reached the real mechanical limit; the 360° unwind message comes up first (the hang handler was not needed).
- **Astrometry:** occasional hiccups remain; the user is investigating whether they come from astrometry.net (slow / cut off under load; current lean) or our pipeline. The analysis log's `nova step=` lines trace each astrometry.net step.
- **ADS-B / aircraft (user decision):** a separate parallel app supplies the aircraft tracks (and their analytics). This app displays them on the Sky Viewer (hover + info) and tracks one on command. Its interface is still to be given.

### GEO Sun wording (2026-11-06, user request; the T4.2f open item)

- `gateway/trackplan.py`: a rendezvous inside a long Sun interval (> `SUN_HOLD_S`; a slow object such as a GEO drifting into the Sun) now refuses with `R_SUN_TOO_CLOSE` = "Target is too close to the Sun; try again later" instead of "Pass passes within 15° of the Sun; no safe detour". The code stays `sun_no_detour` (the web shows the gateway's reason; nothing keys on the text). The real no-detour cases (track ending inside the disc, no cw / ccw way, no clear chunk end) keep `R_NO_DETOUR`.
- Tests: the two GEO refusal tests in `gateway/tests/test_trackplan.py` assert the new text. trackplan + check_track + dsun_wiring 82 passed; ruff clean.

### ADS-B source review (2026-11-07, user request)

- **Reviewed (read-only):** `C:/Users/soar/Desktop/DELV/sip-aero-python`, the SIP AERO platform client kit (`receive_ws.py`, CA, API / decoded / telemetry contracts). No connection made, nothing run.
- **Report:** [docs/adsb_findings.html](adsb_findings.html) — findings, error budget, cueing design, decisions **Q35–Q50**, proposed **Phase 5b** (T5.4a–T5.6c, before T6).
- **Key facts:**
  - WSS `wss://10.22.123.68/api/v1/ws` + read token in the first message, strict TLS with the bundled CA.
  - The `decoded` kind gives callsign, altitude, velocity, but **lat/lon are always null (CPR codes only)**: we decode CPR ourselves (own code; pyModeS is GPL-3.0).
  - The pointing budget is set by latency (1 s at 250 m/s = 1.4° at 10 km vs FOV ≈ 3°) → timestamped dead reckoning, NTP on the mount PC.
  - Cueing = custom alt/az path replaced each update; needs sim probe P23 first.
- **Waiting on the user:** a 10–15 min capture (`--kinds decoded,live,telemetry`), the receiver location, the mount-PC route to `10.22.123.68:443`, answers Q35–Q50.

### T5.6a sim probe P23 (2026-11-07, supervisor, user request)

- **Question (user):** can the aircraft pipeline (a trajectory updated continuously) run on PWI4?
- **Answer: yes.** A running custom alt/az path can be replaced every 1–2 s (new + add + apply, `update_wrap=0`) without stops or jumps; 0.02° mean following error on the sim (finding P23 in section 8, PWI4 behaviour).
- **Run:** PWI4 in Simulator mode (`sim_guard` passed; the user's 8400 / 8410 down; the sim mount was disconnected and was left disconnected); 11 scenarios + 3 re-runs, ≈ 25 min; Sun ≥ 30° from every path.
- **Design consequences** (folded into the T5.6 spec): 1 s updates, first point at now − 0.5 s, latency compensation mandatory, wrap set before the goto, STOP on feed loss, the zenith limit from the real mount.

### Phase 5 end, Tier 3 (2026-11-07)

The user stopped 3200 (8400 / 8410 already down; PWI4 kept running in Simulator mode) and gave a 3 h window (08:47–11:47 UTC); done 11:07.
**New `PWI4.cfg` baseline `54bd3e19…`** (written by PWI4 at 08:03 after the user's real-mount session; site 13.8451° N / 100.5696° E, 100 m; `Simulator.Mount` limits unchanged ±300 / −40…91). PWI4 4.1.6.99, unchanged.

| Step | Result |
|---|---|
| Full sim suite run A | 60 / 67 in 44.2 min. All 7 failures from one fragile test: `test_stall_pwi4sim[0]` added +180° to axis 0 (at 150°) expecting a target of 330° past the +300° limit, but 330° and −30° are equally far and PWI4 picked −30° today (reproduced with the sim client), so nothing stalled; the failed test left the mount tracking with the offset, and the 6 object-track tests after it failed (`test_normal_pass` alone: passed) |
| Fix (test only) | the stall offset is now +170° (target 320°, unambiguously the nearer side): reproduced the stall at 300.21°; both stall tests pass |
| Full sim suite run B | **66 / 67** in 43.5 min. The one failure, `test_near_zenith_pass[88.0]`, was the executor's designed "Missed the pass: could not reach the rendezvous in time" after one replan: the test's pass is already up and the mount starts wherever the previous test left it. Alone 4 / 4 (2 runs × 2 params); passed in Phase 4 run B → flaky list |
| PWI4 harness | 10 / 10 scenarios; harness tests 17 / 17; the sim left disconnected |
| Non-sim | 854 passed; ruff check clean. `ruff format --check`: 34 files would be reformatted (Phase 5 analysis code: the astrometry port, `iod.py`, `chunks.py`, `app.py`, their tests; 1 gateway test, 1 harness file); lint-clean, style only, not changed (no bulk format; user decision) |
| Web | typecheck 0, lint 0 errors (11 old warnings), vitest 585 / 585 |
| Full browser pass | 1920 + 1280×600 @1.5 on throwaway ports (Nuxt 13272, gateway 18734 with memory secrets, analysis 18711 with the fake nova). All checks passed, no page overflow:<ul><li>**Pass Analysis:** ISS picked through the Track Target picker (the AIRCRAFT tab disabled "No ADS-B source configured"), 2 passes listed (11:12 and 22:43 UTC, durations shown), recalled on reopen, no [Track] on a pass > 5 min ahead (Q27b)</li><li>**IOD:** a manual row at 6 decimals shows 3 (123.457° / 45.123°); Replace on the filled row 1 → Yes → focus in row 1's Az; Edit prefills the full precision (33.123456 / 44.654321); Esc keeps the row</li><li>**Astrometry (fake nova):** OW0021 → 40 streaks + target (8 edge excluded) → RUN → pill "Uploading…" inside the Sky Viewer → row 2 solved (70.535° / 68.891°), pill "Astrometry solved · row 2"</li><li>**Pill Retry:** an injected failed resumable job shows the title, the reason (truncated at 1280, full text on hover), Retry and ✕; dismiss clears it</li><li>**Settings:** the Pixel field shows 3.76, no error</li><li>**Phase 4 spot:** CONNECT the sim mount → TRACK TARGET enabled → ISS "Below the horizon" → DISCONNECT</li></ul>A dev-only resource 404 on the first page load (not in the gateway / Nuxt logs; seen before) |
| Flaky list | `test_compute_pass_budget_30ms` 9 / 9 on an idle machine (fails only under CPU load) → keep listed; `test_near_zenith_pass[88.0]` (start-pose dependent) → new entry; fix both in T6.3 (e.g. a fixed start pose before the near-zenith test, a looser budget under load) |

**Phase 5 (T5.1–T5.3) is complete.** Next: the user's review of the Phase 5b spec (section 6), then T5.4a when the recorded ADS-B history is available.

### T5.2f log (2026-11-09, from the user's real astrometry cases)

- **Cause:** station hints applied to other setups' images. The relay filled a missing focal / pixel with RASA11 620 mm + Settings pixel (astrometry.net ±25 % around it), and `scale_ok` then failed a solve > 25 % off ("Solved scale does not match the camera"), dropping the RA / Dec.
- **Worker (Sonnet):** implemented the spec:
  - analysis `source` / station optics / `scale_check`, no scale failure, `PW_ASTROMETRY_FAKE_SCALE`;
  - the relay's Other path never filled from the station;
  - web: the toggle + auto-suggest, review rows / pill Use / Discard, "Astro (oth)" label (title "Astrometry (other)").
  - Deviation (accepted): after a refresh the review is on the pill only (the IOD memo clears on refresh by design); the pill's Use applies to the first free row.
- **Supervisor:**
  - reviewed the relay / solve diffs (key handling untouched);
  - gates: touched tests 108, ruff clean, typecheck 0, lint 0 errors, vitest 601;
  - **change:** the review row now shows the short "S″/px vs station s″/px" (the full text clipped to "Scale differs from the station: sol…" in its ~190 px cell at both sizes); the full text is in `title` and on the pill; vitest updated.
- **Browser check** (throwaway Nuxt 13272 / gateway 18734 / analysis 18711 with the fake nova at 4.1″/px vs the station 1.25″/px; the user stopped 3200 / 8400 / 8410; PWI4 not running, so the cfg-only simulator check; no mount commands), 1920 + 1280×600 @1.5:
  - a scratch FITS copy with FOCALLEN 200 mm / 9 µm → Other + the note (the original sample, 1600 mm / 9 µm = 1.16″/px, is correctly not suggested);
  - a TIFF → Station; the toggle resets per image;
  - Station and Other runs → a review row "4.1″/px vs station 1.25″/px" (not clipped) + Use / Discard; the pill "Astrometry solved — scale differs" + Use / Discard;
  - row Use → the values + "Astrometry"; pill Use (IOD closed) → "Astro (oth)" (fits 70 px), kept on reopen;
  - row Discard and pill Discard (IOD closed) → the row empty, the pill gone;
  - no page overflow.
  - Note: with IOD open, a pill click only closes the modal (outside click); the review stays.
- **User facts to know:** the auto-suggest needs FITS optics; a JPG / TIFF from another setup needs the toggle set to Other by hand (else the station hint may keep astrometry.net from solving).

### T5.7 log (2026-11-09)

- **Worker (Sonnet), per the spec:**
  - `gateway/cctv.py`: status, a 60 s session-bound ticket, an MJPEG pass-through (2 viewers, shared upstream), snapshot, and an HLS proxy (playlist rewriting, no open proxy, traversal-free paths, Basic / Digest upstream auth, no redirects followed, the ticket redacted from the access log, httpx request logging silenced).
  - Settings: a CCTV group (On / Off, MJPEG / HLS, URL, user, password in the secret store `cctv_password`, status line); the body scrolls with a shared `.app-scroll` class (VisibilityFilters reuses it, unchanged look).
  - Web: `CctvPlayer` (MJPEG `<img>` / HLS `<video>` + hls.js 1.5.17 on demand or native, overlays, LIVE / SNAPSHOT chip, closes when hidden), `CctvViewToggle` (panel 3D | CCTV, only when configured; expanded 3D | CCTV | Side by side); the 3D renderer stops while hidden; per-viewer memory.
  - `tools/fake_cctv.py` (MJPEG / snapshot / HLS via ffmpeg / Basic auth / a `/__mode/` feed-loss hook); `tools/cctv_relay/` (MediaMTX RTSP → HLS template, README, `check_stream.py` without credentials).
  - **Deviation (kept, the user did not ask to change it):** the URL and the username are write-only like the password ("•••• set" + clear); the browser never learns them. Showing them for editing is a one-line change in `public()`.
- **Supervisor:**
  - code review of the proxy guards; **added `trust_env=False`** on the camera client (never a system proxy);
  - **test fix:** `test_stall.py::test_lim_el_and_sun_use_the_refracted_frame` failed tonight. It read `now` twice ms apart, and a setting Sun looked like negative refraction (−1.5e-5°; refraction is exactly 0 that far below the horizon). Both reads now use one instant; 25 / 25.
  - **user 11-09 spacing:** the Settings scroll area bottom padding 4 → 20 px, the CCTV group bottom padding 8 → 14 px, the button bar top padding 12 → 16 px.
  - Gates: CCTV + settings + security + relay 125; non-sim 917 + the fixed test (worker run); ruff clean; typecheck 0; lint 0 errors; vitest 631.
- **Owed:** the real-browser check, which needs the user's 3200 / 8400 / 8410 stopped (the fake camera, MJPEG and HLS, 1920 + 1280×600 @1.5: the toggle, side by side, not-configured = unchanged, feed loss → retry → snapshot, the toggle sizes measured for the user's tuning; CSS values now ≈ 84 × 23 px at 1920, ≈ 70 × 19 px at 1280).

### T5.4b log (2026-11-10)

- **Setup (supervisor):** `adsb_core` 0.3.0 linked into our `.venv` by a `.pth` file (`adsb_core_dev.pth` → `adsb_interpreter/adsb_core/src`), so nothing is written into adsb_interpreter (its `git status` checked before / after; run Python with `PYTHONDONTWRITEBYTECODE=1`). For releases: a pinned git tag (its D7).
- **Worker (Sonnet):**
  - `gateway/adsb.py`: optional `load_core()` (missing / broken / not 0.3.x → `module_missing`); `LiveSource` (PlatformClient → bounded queue → `AircraftTable`); `SimulatedSource` (8500 feed: session token, auth-first WS, backoff); `adapt_feed_aircraft` (feed view → `adsb_core.state.Aircraft`, Kalman for no-velocity rows); `AdsbRunner` (own tasks, 1 Hz tick, restart on a Settings save, `aircraft_for`); the green rule; site HAE = alt − 30 m.
  - `hub.py` aux slot (`publish_aux`), so the aircraft frame never displaces a status frame.
  - The settings `adsb` block (token write-only under `adsb_read_token`); the `adsb` block in the 10 Hz status; `--simulated-adsb`.
  - Web: the Settings › ADS-B group (enable, URL, write-only token, range limit, status line, SIMULATED); `utils/adsb.ts` types.
  - Tests: `fake_adsb.py` + `test_adsb.py`.
  - The worker left the full gates unrun (reported honestly); the supervisor ran them.
- **Supervisor:**
  - The non-sim suite found `test_ws_good_hello_then_status` failing: the gateway sent an empty `aircraft` frame every second to every client even with ADS-B off. **Fix:** frames go out only while there are aircraft, plus one empty frame when the last goes (the 10 Hz status carries the `adsb` block anyway). Two worker tests updated to the rule; a new test of the rule.
  - Gates: non-sim 969 + the fixed test; `test_adsb` + `test_security` 63; ruff clean; typecheck 0; lint 0 errors; vitest 640.
  - **Real check against adsb_interpreter's running simulated feed (8500, read only):** a throwaway gateway (18733, memory secrets, temp settings) → `adsb` state `simulated`, green; 26 aircraft frames in 25 s; 13 aircraft, 7 visible; rows with az / el / range / speed / track / predictor (one `kalman`, one stale) / height source `baro+diff`. With the throwaway's default site the highest elevation was 10.5° (below Lim EL, as the plan expected: cueing is validated with synthetic overflights in T5.6b).
- **Owed:** the live connection (the user stores the second read token in Settings › ADS-B when the platform is up); the browser look of the ADS-B group (with T5.5's browser check).

### T5.5 log (2026-11-10, worker; browser check owed to the supervisor)

- **Web:** `stores/aircraft.ts` (the 1 Hz `aircraft` message, cleared on the empty frame / disconnect / not green); `utils/aircraft.ts` + `utils/aircraft-target.ts` (pure: frames + dead-reckoning capped at 2 s, icon rotation -track, labels, A1b formats, one TRACK AIRCRAFT verdict function for T5.6b, filter / chip rules, tab list); Sky Viewer `air` canvas + labels + hover/click on the shared selection; Filters › Aircrafts › Airborne; A1b card in TrackCard (two-column rows to fit the short panel; shared A1e strip); Track Target › AIRCRAFT tab (`AircraftTargetPane.vue`); chips "SIMULATED DATA" (top right) / "ADS-B feed down" (top left) in the top strip of the chart box.
- **Deviation:** the gateway aircraft rows now carry `lat` / `lon` (5 dp) for every row (instead of a selection command); `test_adsb` updated.
- **Other changes:** `DEFAULT_FILTERS.aircrafts` is now true and persisted (gating is by the ADS-B state, not the stored value); the Filters panel `--u` denominator 450 → 480 for the added Airborne row.
- **Provisional sizes (user tunes):** aircraft icon 14 px × chart scale k (k = clamp(R/340, 0.8, 1.4)); pick radius 12 px × k; stale alpha 0.4; chips 11 px bold, 16 px line, 1 px / 8 px padding, 2 px from the box edges; card rows 0.92 em.
- **Owed:** the browser look at 1920 + 1280×600 @1.5 on the 8500 simulated feed (chart / chips / card fit).

### T5.5 supervisor check (2026-11-10)

- Gates re-run: `test_adsb` + `test_security` 63, ruff clean, typecheck 0, lint 0 errors, vitest 678 (the worker's non-sim run: 971).
- **Browser check** (the user's 3200 / 8400 / 8410 down; throwaway Nuxt 13272 / gateway 18734 with `PW_ADSB_SIMULATE=1` / analysis 18711; the user's running 8500 simulated feed read only; site 13.8451 / 100.5696), 1920 + 1280×600 @1.5:
  - 10 aircraft in the store and drawn, state `simulated` green, the SIMULATED DATA badge top-right of the chart box, no feed-down chip, the Airborne filter enabled;
  - hover → the tooltip "QTR841 (06A0E7) · Alt 36 000 ft | Range … | Age 1.7 s"; click → selected (ring) and the A1b card with every value, no clipped value, no card overflow at either size;
  - Track Target › AIRCRAFT enabled, 10 rows sorted by range, sortable headers, TRACK SELECTED TARGET disabled with "Aircraft tracking: coming in T5.6b";
  - no page overflow.
  - **Seen:** scenario aircraft above Lim EL from our site exist (AIQ3141 at 0.8 km +31°, N688CP +47°): candidates for T5.6b.
  - **For the user (sizes):** the aircraft icons are small and low-contrast on the daytime (orange) sky disc, especially at 1280 (≈ 11 px); at 1280 the hover tooltip near the west edge runs past the Sky Viewer frame into the MOUNT CONTROL panel.
- **User 11-10 (sizes):** the icon larger + an outline that stands out from the sky; everything else as is (the tooltip spill accepted).
  - `AIRCRAFT_ICON_PX` 14 → **20** (≈ 25 px at 1920, ≈ 16 px at 1280 @1.5); pick radius 12 → 14.
  - The icon is drawn as a vector (the `aircraft.svg` path as `Path2D`) with a fill and a 1.5 px outline chosen from the sky brightness (`aircraftIconColors`): a light sky (`skyBrightness` ≥ 0.5) → light fill `#f4f6f8` + dark outline `#161c24`; a dark sky → dark fill `#262c35` + light outline `#eef1f4`. The old `brightness(0.45)` filter and the SVG image load are gone.
  - Checks: typecheck 0, lint 0 errors, vitest 678 (+1 colour test in `aircraft.spec.ts`). Browser at 1920 + 1280 @1.5: the outlined icons stand out on the dusk sky (the dark-sky variant); the light-sky variant is covered by the unit test (the sky was past daylight at the check).

- **User 11-10: the tooltip flip.** The flip existed but measured the tooltip before its new text rendered (width 0 / stale on first show and on a satellite ↔ aircraft switch), so it spilled into the next panel. Now `placeTip()` runs on the pointer move, again after the render (`nextTick`), and whenever the text changes (`watch([airTip, tipLines])`): right / below the pointer, flipped left / above to stay inside the chart box. Checks: typecheck 0, lint 0 errors, vitest 679. **Browser check owed** (the user's servers were back up): the user confirms on their 3200 (hover an aircraft near the right edge).
### T5.6b log (2026-11-10 / 11-11)

- Spec: "T5.6b — aircraft cueing (spec, 2026-11-10)" above; supervisor decisions D1 (pointing from PWI4's site, a note when Settings differs > 30 m), D2 (the path sent refracted, P18 parity; supersedes Q39's "no refraction" for pointing only), D3 (zenith rule from the axis-0 rate vs 0.8 × vmax, note at peak ≥ 87°), D4 (a separate snapshot `airtrack`; satellite and aircraft tracks replace each other).
- Worker (Sonnet; stopped once by a usage limit mid-task, resumed 11-11): `gateway/airtrack.py` (`AirChecker`, `AirTrackExecutor`, `build_path`), `track_aircraft` in commands / control / app / safety (`replace_route`) / jog / snapshot / poller, `adsb.py` (`core`, `range_limit_m`); web `aircraft-track.ts`, `useAircraftVerdict.ts`, card / AIRCRAFT tab buttons, selection, jog gating. Additions: the rendezvous axis-0 representation that keeps the straight-line track inside its window longest (else the nearest).
- Supervisor check: code read (path cuts, Q60, rendezvous, check rules); gateway non-sim 754 passed / 72 skipped / 1 failed (`test_homed.py::test_connect_on_an_already_connected_mount_ends_homed`, a `wait_for` timing test in an untouched area, 3 / 3 files green alone → flaky list); typecheck 0, lint 0 errors, vitest 696; no pyc in adsb_interpreter.
- Note: the slew estimate assumes the nearest axis-0 representation; when the rendezvous picks the other one the mount may arrive late and use its one re-plan.
- **PWI4-sim (11-11, the user's servers stopped, PWI4 Simulator):** the first run failed 5 / 5 on test-harness faults, fixed by the supervisor: the ICAOs "ABG001" / "ABT001" / "ABL001" are not hex (the gateway rightly refused them → `CASE_ICAO`, "ABE001"); `Rig.close` awaited the fake feed on a closed event loop, which masked every result (now suppressed). Then **5 / 5**: cruise 0.008° mean / 0.047° max, GA 0.033 / 0.074, turn 0.054 / 0.118 (re-run 0.021 / 0.062; the turn case got its own 0.08° mean limit, the max limit 0.3° unchanged), feed freeze → "ADS-B data lost" 10.52 s after the last fix, Lim EL exit → ended 0.89 s after the exit, lowest 15.02°; PWI4.cfg unchanged.
- **Browser (11-11)** on the user's 8500 simulated feed (read only; throwaway Nuxt 13272 / gateway 18734 / analysis 18711; the scenario's high aircraft come and go, and the feed went silent / was restarted by the user once): N688CP at +10° crossing Lim EL → TRACK AIRCRAFT accepted with the rendezvous at the crossing; card lines "Slewing to rendezvous" → "Waiting for the aircraft" → "Tracking", button TRACKING (disabled), state TRACKING "Tracking N688CP"; closing the browser → "Control link lost", mount stopped (disconnect = stop); 1920 + 1280×600 @1.5: no clipped value, no overflow. Audit: `track_aircraft` / `airtrack_stopped` with ICAO + callsign only. The card's az / el is the feed row (geometric, up to ~1.5 s old), so it differs from the mount readout by a few tenths while the aircraft climbs; expected.
- Pre-existing: one 404 console error on page load (also in the T5.5 runs); not investigated.

### T5.7 first real camera (2026-11-12)

- Camera: TP-Link Tapo C200 (RTSP only: `rtsp://USER:PASS@IP:554/stream1` with the camera account from the Tapo app) → MediaMTX **v1.21.3** relay → HLS 1920×1080 H.264 15 fps ≈ 1.5 Mbit/s.
- **MediaMTX ≥ v1.2x session handling:** the first `index.m3u8` request answers 302 → `?cookieCheck=1` (+ a cookie); later requests need the `hlsSession` cookie (else 401 "session not found"), OR, when the cookie test fails, the playlist carries `?session=<id>` in each URI. The gateway refuses redirects, so the 302 stopped it. **Working setting:** Stream URL `http://127.0.0.1:8888/mount/index.m3u8?cookieCheck=1` (our proxy keeps each URI's query, so `?session=` flows through). Checker: 1 variant 1920×1080, live, 6 segments of 3.7 s (the C200's keyframe interval, not our 2 s) → about 10 s player delay.
- **Bug fixed:** `GET /api/mount/cctv/status` was a sync route; on a fresh camera (state connecting, no viewers) it starts the probe with `asyncio.create_task` off the event loop → RuntimeError → the route failed, the web never learned `configured`, no 3D | CCTV toggle, the state stayed Connecting. Now `async`; regression test `test_status_of_a_fresh_camera_probes_it[hls|mjpeg]` (the T5.7 tests always had a result or a viewer first). test_cctv 58 passed; reproduced and fixed against the user's MediaMTX in-process (status live, entry playlist proxied with the session).
- **Follow-ups (small):** accept MediaMTX's one same-origin `?cookieCheck=1` redirect so the plain URL works; relay README + template: the URL above, the real delay, the Tapo camera account; optional low-latency HLS if 10 s matters (user to decide).

## 9. Known limitations & risks

- **Gateway crash (G4 note):** nothing can send a stop if the gateway process dies; mitigated by a UI warning that hands the user off to PWI4.
- **Sky Viewer scale:** "no item limit" is the main performance risk; the O1 benchmark puts the propagation side at ~2 s per 30-min chunk for 20k objects, so the remaining risk is rendering (T2.2/T2.3 measure it).
- **D-SUN** is novel logic driving hardware; it ships only after planner unit tests + PWI4-sim runs + a supervised real-mount test. **Real-mount test passed (user, 11-06).**
- **Aircraft / ADS-B** is deferred; UI is present but disabled. Source (user, 11-06): a separate parallel app; this app displays + tracks on command (interface to be given).
