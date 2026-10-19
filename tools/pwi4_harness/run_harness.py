"""Run the PWI4 simulation-mode harness: ``python tools/pwi4_harness/run_harness.py [names...]``.

Refuses to run unless PWI4.cfg has Devices.Mount = Simulator.Mount (see sim_guard).
Writes out/<UTC timestamp>/report.json and report.md.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sim_guard  # noqa: E402
from common import PWI4, Ctx  # noqa: E402

OUT_ROOT = Path(__file__).resolve().parent / "out"


def run_all(only: list[str] | None = None, out_dir: Path | None = None) -> dict:
    cfg = sim_guard.assert_simulator()  # FIRST: before any command
    import scenarios

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = out_dir or (OUT_ROOT / ts)
    out.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(pwi=PWI4("127.0.0.1", sim_guard.PORT))
    report: dict = {"timestamp_utc": ts, "cfg": cfg, "scenarios": {}}
    st0 = ctx.pwi.status()
    report["pwi4_version"] = st0.pwi4.version
    report["site"] = [st0.site.latitude_degs, st0.site.longitude_degs, st0.site.height_meters]
    report["initial_connected"] = st0.mount.is_connected
    try:
        for name, fn in scenarios.SCENARIOS:
            if only and not any(o in name for o in only):
                continue
            t0 = time.perf_counter()
            entry: dict = {"status": "ok"}
            try:
                if name != "01_connect_enable_home":
                    ctx.ensure_ready()
                findings, raw = fn(ctx)
                st = ctx.pwi.status()
                if st.mount.is_connected:
                    entry["end_state"] = {
                        "axis0_wrap_range_min_degs": st.mount.axis0_wrap_range_min_degs,
                        "axis_pos_degs": [
                            st.mount.axis0.position_degs,
                            st.mount.axis1.position_degs,
                        ],
                        "tracking": st.mount.is_tracking,
                    }
                entry["findings"] = findings
                entry["raw"] = raw
            except Exception as exc:  # noqa: BLE001
                entry["status"] = "FAILED"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                entry["traceback"] = traceback.format_exc()
            finally:
                if name != "10_disconnect":
                    ctx.safe_state()
            entry["duration_s"] = round(time.perf_counter() - t0, 1)
            report["scenarios"][name] = entry
            print(f"{name}: {entry['status']} ({entry['duration_s']} s)", flush=True)
    finally:
        # always leave the mount safe and disconnected, as the user had it
        try:
            ctx.safe_state()
            if not report["initial_connected"]:
                for i in (0, 1):
                    try:
                        ctx.pwi.mount_disable(i)
                    except Exception:  # noqa: BLE001
                        pass
                ctx.pwi.mount_disconnect()
        except Exception as exc:  # noqa: BLE001
            ctx.note(f"final cleanup failed: {exc}")
        time.sleep(0.5)
        final = ctx.pwi.status()
        report["final_mount_is_connected"] = final.mount.is_connected
    report["notes"] = ctx.notes
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    (out / "report.md").write_text(render_md(report), encoding="utf-8")
    report["out_dir"] = str(out)
    return report


def _flat(d, prefix="") -> list[str]:
    rows = []
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            rows.extend(_flat(v, key + "."))
        else:
            rows.append(f"{key} = {v}")
    return rows


def render_md(rep: dict) -> str:
    lines = [
        f"# PWI4 simulation-mode harness report ({rep['timestamp_utc']})",
        "",
        f"- PWI4 {rep['pwi4_version']}, site lat/lon/h = {rep['site']}",
        f"- Guard OK: Devices.Mount = Simulator.Mount in `{rep['cfg']}`",
        f"- Mount connected at start: {rep['initial_connected']}; "
        f"**at end: {rep['final_mount_is_connected']}**",
        "",
        "| Scenario | Status | Duration (s) |",
        "|---|---|---|",
    ]
    for name, e in rep["scenarios"].items():
        lines.append(f"| {name} | {e['status']} | {e['duration_s']} |")
    for name, e in rep["scenarios"].items():
        lines += ["", f"## {name} — {e['status']}", ""]
        if e["status"] != "ok":
            lines += ["```", e.get("traceback", e.get("error", "")), "```"]
        if "end_state" in e:
            lines.append(f"- end_state = {e['end_state']}")
        for row in _flat(e.get("findings", {})):
            lines.append(f"- {row}")
    if rep.get("notes"):
        lines += ["", "## Notes", ""] + [f"- {n}" for n in rep["notes"]]
    return "\n".join(lines) + "\n"


def main() -> int:
    try:
        rep = run_all(sys.argv[1:] or None)
    except sim_guard.GuardError as exc:
        print(f"GUARD: {exc}", file=sys.stderr)
        return 2
    print(f"report: {rep['out_dir']}")
    print(f"final mount.is_connected = {rep['final_mount_is_connected']}")
    return 0 if all(e["status"] == "ok" for e in rep["scenarios"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
