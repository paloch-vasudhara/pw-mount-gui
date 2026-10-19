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
| O2 | Slew rate / acceleration / keyhole limits | Read from PWI4 at connect; used by D-SUN timing. |
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

---

## 7. Progress tracker

Status: ⬜ not started · 🟦 design · 🟨 implementing · 🟪 verifying · ✅ done · ⏸ paused/partial

| Task | Status | Design | Impl | Verified | User check | Notes |
|---|---|---|---|---|---|---|
| T0.1 | ⬜ | | | | | record I1 baseline |
| T0.2 | ⬜ | | | | | |
| T0.3 | ⬜ | | | | | PWI4 sim mode |
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

**Resume point:** plan approved; waiting for the user's baseline commit, then start **T0.1** (design step).

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

### Benchmarks
**O1 — python-sgp4 vs orekit** (2026-10-19, this PC; synthetic TLE set 80 % LEO / 10 % MEO / 10 % GEO; one 30-min chunk at 10 s = 181 samples/satellite; az/el for site 13.786°N 101.464°E)

| Engine | Satellites | Time | Note |
|---|---|---|---|
| python-sgp4 2.25 (SatrecArray) + numpy az/el | 20 000 | **2.2 s** (parse 0.23 + propagate 1.36 + az/el 0.62) | measured |
| python-sgp4 | 2 000 | 0.21 s | measured |
| orekit (orekit310 env), TLEPropagator + TopocentricFrame | 500 | 2.3 s | measured → ~92 s for 20 000 |

Agreement (first 50 satellites × 7 epochs): TEME position diff max 1.4e-7 km; az/el separation median 0.35″, max 3.8″ (simplified GMST rotation, no polar motion) — negligible against the ≈ 3° FOV. Scripts kept outside the repo (session scratchpad); re-create in `tools/bench/` if needed.

### Deviation log
_Differences between the as-built app and the logic sheet / concept renders, with reasons._

| Date | Task | Deviation | Reason | Approved |
|---|---|---|---|---|

### Baselines
| Item | Value |
|---|---|
| ssa-toolkit HEAD at plan approval | `84f2ded` (re-record at T0.1 start) |
| pw-mount-control baseline commit | _user commit before T0.1 — record hash here_ |

### Session log
| Date | Session | Summary |
|---|---|---|
| 2026-10-19 | Planning | Concepts reviewed; logic sheet Rev 2 + HTML; plan drafted, reviewed, O1 benchmarked, approved |

---

## 9. Known limitations & risks

- **Gateway crash (G4 note):** nothing can send a stop if the gateway process dies; mitigated by a UI warning that hands the user off to PWI4.
- **Sky Viewer scale:** "no item limit" is the main performance risk; the O1 benchmark puts the propagation side at ~2 s per 30-min chunk for 20k objects, so the remaining risk is rendering (T2.2/T2.3 measure it).
- **D-SUN** is novel logic driving hardware; it ships only after planner unit tests + PWI4-sim runs + a supervised real-mount test.
- **Aircraft / ADS-B** is deferred; UI is present but disabled.
