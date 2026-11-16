"""T6.2c: build the standalone portable folder (and zip) of PW Mount Control.

    .venv/Scripts/python tools/package/build.py           # build dist/pw-mount-control-<version>/
    .venv/Scripts/python tools/package/build.py --installer   # the folder, then the Setup.exe
    .venv/Scripts/python tools/package/build.py --lock    # regenerate requirements-lock.txt
    .venv/Scripts/python tools/package/build.py --clean   # remove dist/ and tools/package/.cache/

The result runs on Windows 10/11 x64 with no Python, Node, repo or venv: double-click
`PW Mount Control.cmd`. It holds the python.org embeddable Python, the pinned wheels
(downloaded for win_amd64 / cp312), adsb_core (copied from the sibling adsb_interpreter checkout),
the app folders, the generated web app and the relay template. Never included: `.env*`, tests,
docs, settings, audit logs, the repo's git history, the user's camera login.

`--installer` then compiles tools/package/installer.iss with Inno Setup 7 (ISCC) into
dist/PW-Mount-Control-Setup-<version>.exe (unsigned).

Network: python.org (the embeddable zip) and PyPI (pip download) only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as md
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "tools" / "package"
CACHE = PKG / ".cache"
DIST = ROOT / "dist"
LOCK = ROOT / "requirements-lock.txt"
REQ_FILES = (
    ROOT / "gateway" / "requirements.txt",
    ROOT / "analysis" / "requirements.txt",
    ROOT / "launcher" / "requirements.txt",
)
APP_NAME = "pw-mount-control"

PY_VERSION = "3.12.10"  # the last 3.12 release with Windows binaries; the venv's cp312 ABI
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
# SHA-256 as published in python.org's own SBOM for this file (<PY_URL>.spdx.json); every build
# verifies the cached / downloaded zip against it.
PY_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"

ADSB_SRC = ROOT.parent / "adsb_interpreter" / "adsb_core" / "src" / "adsb_core"
ADSB_REPO = ROOT.parent / "adsb_interpreter"
ADSB_MAJOR_MINOR = "0.3."

# Left out of the lock: copied separately (adsb_core) or only there to build the venv.
LOCK_SKIP = {"adsb-core", "pip", "setuptools", "wheel"}
SDIST_ONLY = {"proxy-tools"}  # no wheel on PyPI (pywebview dependency, pure Python)
APP_DIRS = ("gateway", "analysis", "launcher", "pwi4")
APP_IGNORE = shutil.ignore_patterns("__pycache__", "tests", "*.pyc", "*.pyo")
RELAY_PLACEHOLDER = "rtsp://USER:PASSWORD@CAMERA-IP:554/stream1"

PTH_LINES = ("python312.zip", ".", r"Lib\site-packages", "..", "import site")


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[build] FAILED: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=False, **kw)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rmtree(path: Path) -> None:
    def onerr(func, p, _exc):
        os.chmod(p, 0o700)
        func(p)

    if path.exists():
        shutil.rmtree(path, onexc=onerr)


def folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"


# ------------------------------------------------------------------ version


def git(*args: str, cwd: Path = ROOT) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def app_version() -> str:
    m = re.search(
        r'^VERSION\s*=\s*"([^"]+)"', (ROOT / "gateway" / "app.py").read_text("utf-8"), re.M
    )
    if not m:
        die("VERSION not found in gateway/app.py")
    return m.group(1)


def git_info() -> tuple[str, bool]:
    commit = git("rev-parse", "--short", "HEAD") or "nogit"
    dirty = bool(git("status", "--porcelain"))
    return commit, dirty


def full_version() -> str:
    commit, dirty = git_info()
    return f"{app_version()}+{commit}" + ("-dirty" if dirty else "")


# ------------------------------------------------------------------ lock file


def _roots() -> list[Requirement]:
    reqs = []
    for f in REQ_FILES:
        for line in f.read_text("utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                reqs.append(Requirement(line))
    return reqs


def runtime_closure() -> dict[str, str]:
    """{canonical name: installed version} of the runtime closure of the three requirements
    files, walked over the metadata installed in the running interpreter (the dev venv)."""
    env = default_environment()
    todo = [(canonicalize_name(r.name), frozenset(r.extras)) for r in _roots()]
    seen: dict[str, set[str]] = {}
    versions: dict[str, str] = {}
    while todo:
        name, extras = todo.pop()
        if name in seen and extras <= seen[name]:
            continue
        seen.setdefault(name, set()).update(extras)
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            die(f"{name} is required but not installed in the .venv")
        versions[name] = dist.version
        for line in dist.requires or []:
            req = Requirement(line)
            if req.marker and not any(
                req.marker.evaluate({**env, "extra": e}) for e in (extras | {""})
            ):
                continue
            todo.append((canonicalize_name(req.name), frozenset(req.extras)))
    return {n: v for n, v in sorted(versions.items()) if n not in LOCK_SKIP}


def write_lock() -> None:
    pins = runtime_closure()
    lines = [
        "# Runtime closure of the gateway/analysis/launcher requirements, as in the dev .venv.",
        "# Generated by: .venv\\Scripts\\python tools\\package\\build.py --lock   (do not edit)",
        "# adsb_core is not here: the build copies it from adsb_interpreter.",
        *(f"{n}=={v}" for n, v in pins.items()),
    ]
    LOCK.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"{LOCK.name}: {len(pins)} packages")


def read_lock() -> dict[str, str]:
    if not LOCK.is_file():
        die("requirements-lock.txt is missing: run build.py --lock")
    pins: dict[str, str] = {}
    for line in LOCK.read_text("utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            r = Requirement(line)
            pins[canonicalize_name(r.name)] = next(iter(r.specifier)).version
    for name, ver in pins.items():
        try:
            have = md.version(name)
        except md.PackageNotFoundError:
            die(f"lock pin {name}=={ver} is not installed in the .venv (run --lock?)")
        if have != ver:
            die(f"lock pin {name}=={ver} differs from the .venv's {have} (run --lock?)")
    return pins


# ------------------------------------------------------------------ pieces


def fetch_python() -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / Path(PY_URL).name
    if not target.is_file():
        log(f"downloading {PY_URL}")
        tmp = target.with_suffix(".part")
        with urllib.request.urlopen(PY_URL, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.replace(target)
    got = sha256_of(target)
    if got != PY_SHA256:
        die(
            f"{target.name}: SHA-256 {got} does not match the pinned {PY_SHA256} "
            "(delete the cache file and retry, or update PY_SHA256 after checking python.org)"
        )
    return target


def install_python(zip_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)
    pth = dest / "python312._pth"
    if not pth.is_file():
        die("python312._pth missing in the embeddable package")
    pth.write_text("\n".join(PTH_LINES) + "\n", encoding="ascii")
    site = dest / "Lib" / "site-packages"
    site.mkdir(parents=True)
    # The _pth file puts the interpreter in isolated mode: PYTHONDONTWRITEBYTECODE is ignored,
    # so the no-write rule is set here (the app folder may be read-only; .pyc ship precompiled).
    (site / "sitecustomize.py").write_text(
        "import sys\nsys.dont_write_bytecode = True\n", encoding="utf-8"
    )


def wheels_for_lock(pins: dict[str, str]) -> dict[str, Path]:
    wheels = CACHE / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    # pure-Python packages that PyPI ships as an sdist only: built into a py3-none-any wheel here
    for name in sorted(SDIST_ONLY & set(pins)):
        stem = name.replace("-", "_") + "-" + pins[name] + "-"
        if not list(wheels.glob(stem + "*.whl")):
            cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(wheels),
                   f"{name}=={pins[name]}", "--disable-pip-version-check"]  # fmt: skip
            if run(cmd, cwd=ROOT).returncode != 0:
                die(f"building a wheel from the {name} sdist failed")
        if not list(wheels.glob(stem + "py3-none-any.whl")):
            die(f"{name}: the wheel built from the sdist is not pure Python (py3-none-any)")
    rest = CACHE / "download-lock.txt"
    rows = [f"{n}=={v}" for n, v in pins.items() if n not in SDIST_ONLY]
    rest.write_text(chr(10).join(rows) + chr(10), encoding="utf-8")
    cmd = [
        sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--no-deps",
        "--platform", "win_amd64", "--python-version", "3.12", "--implementation", "cp",
        "--abi", "cp312", "-r", str(rest), "-d", str(wheels), "--disable-pip-version-check",
    ]  # fmt: skip
    r = run(cmd, cwd=ROOT)
    if r.returncode != 0:
        die(
            "pip download failed (a pinned package has no cp312 / win_amd64 / py3 wheel? "
            "see the pip output above for its name)"
        )
    found: dict[str, Path] = {}
    for w in wheels.glob("*.whl"):
        try:
            name, ver, _, _ = parse_wheel_filename(w.name)
        except Exception:
            continue
        n = canonicalize_name(name)
        if pins.get(n) == str(ver):
            found[n] = w
    missing = sorted(set(pins) - set(found))
    if missing:
        die("no wheel for: " + ", ".join(missing))
    return found


def install_wheels(site: Path, wheels_dir: Path) -> None:
    cmd = [
        sys.executable, "-m", "pip", "install", "--no-index", "--find-links", str(wheels_dir),
        "--target", str(site), "--no-deps", "--no-compile", "--no-warn-script-location",
        "--disable-pip-version-check", "-r", str(LOCK),
    ]  # fmt: skip
    if run(cmd, cwd=ROOT).returncode != 0:
        die("pip install into the runtime failed")
    rmtree(site / "bin")  # console-script launchers of the build machine: not used


def copy_adsb_core(site: Path) -> tuple[str, str]:
    """(version, adsb_interpreter commit) - or ('missing', '') with a warning."""
    if not (ADSB_SRC / "__init__.py").is_file():
        log(
            f"WARNING: {ADSB_SRC} not found: the package runs without adsb_core "
            "(ADS-B shows 'module not installed')"
        )
        return "missing", ""
    text = (ADSB_SRC / "__init__.py").read_text("utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    version = m.group(1) if m else "unknown"
    if not version.startswith(ADSB_MAJOR_MINOR):
        die(f"adsb_core {version} is not {ADSB_MAJOR_MINOR}x")
    shutil.copytree(
        ADSB_SRC, site / "adsb_core", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    for need in ("certs", "schemas"):
        if not (site / "adsb_core" / need).is_dir():
            die(f"adsb_core/{need} was not copied")
    return version, git("rev-parse", "--short", "HEAD", cwd=ADSB_REPO) or "unknown"


def copy_app(dest: Path) -> None:
    for d in APP_DIRS:
        shutil.copytree(ROOT / d, dest / d, ignore=APP_IGNORE)
    eph = dest / "data" / "ephemeris"
    eph.mkdir(parents=True)
    shutil.copy2(ROOT / "data" / "ephemeris" / "de421.bsp", eph / "de421.bsp")
    shutil.copy2(PKG / "icon" / "app.ico", dest / "app.ico")


def build_web(dest: Path) -> int:
    pnpm = shutil.which("pnpm")
    if not pnpm:
        die("pnpm not found on PATH (needed for the web build)")
    if run([pnpm, "generate"], cwd=ROOT / "web").returncode != 0:
        die("pnpm generate failed")
    out = ROOT / "web" / ".output" / "public"
    if not (out / "index.html").is_file():
        die("web/.output/public/index.html missing after pnpm generate")
    shutil.copytree(out, dest, ignore=shutil.ignore_patterns("dev"))
    if (dest / "dev").exists():
        die("dev pages leaked into the web copy")
    return sum(1 for f in dest.rglob("*") if f.is_file())


def clean_mediamtx(text: str) -> str:
    """The relay config with every RTSP URL replaced by the placeholder (never the user's login)."""
    text = re.sub(r"rtsp://[^\s'\"]+", RELAY_PLACEHOLDER, text)
    if re.search(r"://[^/\s'\"]*@", text.replace(RELAY_PLACEHOLDER, "")):
        die("mediamtx.yml still holds a URL with a login after cleaning")
    stray = [ip for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)
             if ip not in ("127.0.0.1", "0.0.0.0")]  # fmt: skip
    if stray:
        die("mediamtx.yml still holds a non-loopback address after cleaning (edit the template)")
    return text


def copy_relay(dest: Path) -> None:
    dest.mkdir()
    src = ROOT / "tools" / "cctv_relay"
    note = (
        "> In this package: `python\\python.exe relay\\check_stream.py` replaces the\n"
        "> `.venv\\Scripts\\python tools\\cctv_relay\\check_stream.py` below, and `mediamtx.exe`\n"
        "> goes into this `relay` folder (the launcher then starts it when CCTV type HLS is on).\n"
        "\n"
    )
    (dest / "README.md").write_text(
        note + (src / "README.md").read_text("utf-8"), encoding="utf-8", newline="\n"
    )
    (dest / "mediamtx.yml").write_text(
        clean_mediamtx((src / "mediamtx.yml").read_text("utf-8")), encoding="utf-8", newline="\n"
    )
    shutil.copy2(src / "check_stream.py", dest / "check_stream.py")


README_FIRST = """\
PW Mount Control {version}
=======================================

Telescope-mount control (PWI4) with a sky view, satellite / aircraft tracking and astrometry.
This folder is the whole program: it needs no Python, Node or installer.
(PW-Mount-Control-Setup-*.exe, built with "build.py --installer", installs the same files to
Program Files with Start menu entries.)

START
  Double-click "PW Mount Control.cmd". The app window opens (it needs the Microsoft Edge
  WebView2 runtime, present on Windows 10/11; without it the app opens in your browser instead).
  Closing the window stops everything. PWI4 must be running on the same PC.

FIRST RUN (once)
  1. Station Settings: set the observatory location (latitude / longitude / height).
  2. Astrometry.net API key (for plate solving), the CCTV stream and the ADS-B token: enter them
     in Station Settings. Keys are kept in Windows Credential Manager, not in files here.
  3. Satellite database login (for the satellite catalogue): open a Command Prompt in this folder
     and run
         python\\python.exe -m analysis.set_dsn
     Paste the PostgreSQL connection string when asked (typing is hidden). It is stored in
     Windows Credential Manager for your Windows user; "python\\python.exe -m analysis.set_dsn
     --clear" removes it. Without it the app works, but the satellite catalogue is unavailable.
  4. Optional CCTV relay (RTSP camera): see relay\\README.md. Put mediamtx.exe into the relay
     folder and enter your camera's address in relay\\mediamtx.yml (the file here is a template).

WHERE THINGS ARE
  Settings, window position and logs: %LOCALAPPDATA%\\pw-mount-control
  Logs (launcher.log, gateway.out.log, analysis.out.log): %LOCALAPPDATA%\\pw-mount-control\\logs
  This folder is never written to while the app runs, so it can live in Program Files.

UNINSTALL
  Installed with Setup: use Windows "Apps & features" (it asks about settings and stored logins).
  Portable folder: delete this folder and %LOCALAPPDATA%\\pw-mount-control. To remove
  stored logins as well, run "python\\python.exe -m analysis.set_dsn --clear" first and delete
  the "pw-mount-control" entries in Windows Credential Manager.
"""

CMD_FILE = '@start "" "%~dp0python\\pythonw.exe" -m launcher\r\n'


def write_root_files(dest: Path, version: str) -> None:
    # The launcher resolves everything from the app root; start in it (explorer sets cwd = folder,
    # but a shortcut may not).
    (dest / "PW Mount Control.cmd").write_bytes(
        ('@echo off\r\ncd /d "%~dp0"\r\n' + CMD_FILE).encode("ascii")
    )
    (dest / "README-FIRST.txt").write_text(
        README_FIRST.format(version=version), encoding="utf-8", newline="\r\n"
    )


def compile_pyc(dist: Path) -> None:
    py = dist / "python" / "python.exe"
    targets = [str(dist / d) for d in (*APP_DIRS, "relay")] + [
        str(dist / "python" / "Lib" / "site-packages")
    ]
    cmd = [
        str(py),
        "-m",
        "compileall",
        "-q",
        "-f",  # the interpreter start itself writes a timestamp .pyc of sitecustomize first
        "-j",
        "0",
        "--invalidation-mode",
        "checked-hash",
        *targets,
    ]
    r = run(cmd, cwd=dist, env=_clean_env(Path(tempfile.gettempdir())))
    if r.returncode != 0:
        log("WARNING: some files did not compile (compileall exit code %d)" % r.returncode)


# ------------------------------------------------------------------ checks


def safety_check(dist: Path) -> int:
    """No .env*, no 'POSTGRESQL_DSN=' text, no URL login outside site-packages (names only)."""
    bad_names, bad_text, bad_login = [], [], []
    n = 0
    site = dist / "python" / "Lib" / "site-packages"
    for f in dist.rglob("*"):
        if not f.is_file():
            continue
        n += 1
        if f.name.lower().startswith(".env"):
            bad_names.append(f.relative_to(dist))
        data = f.read_bytes()
        if b"POSTGRESQL_DSN=" in data:
            bad_text.append(f.relative_to(dist))
        if site not in f.parents and f.suffix in (".yml", ".md", ".txt", ".cmd", ".json"):
            for m in re.finditer(rb"://([^/\s'\"@]*)@", data):
                if m.group(1) != b"USER:PASSWORD":
                    bad_login.append(f.relative_to(dist))
                    break
    for what, items in (
        (".env* file names", bad_names),
        ("files containing POSTGRESQL_DSN=", bad_text),
        ("files holding a URL login", bad_login),
    ):
        if items:
            die(f"{what} in the package: " + ", ".join(map(str, items)))
    for banned in (
        ".git",
        ".venv",
        "node_modules",
        "scratchpad",
        "docs",
        "concept",
        "scope-pointer",
    ):
        if (dist / banned).exists():
            die(f"{banned} must not be in the package")
    return n


def _clean_env(local: Path) -> dict[str, str]:
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    return {
        "SystemRoot": sysroot,
        "PATH": rf"{sysroot}\System32",
        "TEMP": str(local),
        "TMP": str(local),
        "LOCALAPPDATA": str(local),
        "USERPROFILE": str(local),  # Path.home() (astropy config dir); always set on Windows
    }


IMPORTS = (
    "gateway.app, analysis.app, launcher.__main__, webview, adsb_core, skyfield, astropy, "
    "photutils, skimage, scipy, numpy, keyring, sqlalchemy, psycopg2, sgp4, PIL, tifffile, "
    "requests, httpx, uvicorn, dotenv, tzdata, websockets, multipart"
)
EPHEM_CODE = (
    "from pathlib import Path\n"
    "from gateway.ephem import Ephem\n"
    "import adsb_core, launcher.ui as ui, analysis.db as db\n"
    "e = Ephem(Path('data/ephemeris/de421.bsp'))\n"
    "assert e.available, e.error\n"
    "from gateway.config import REPO_ROOT\n"
    "from launcher import paths\n"
    "print('ephemeris', 'ok', '| adsb_core', adsb_core.__version__, '| tkinter', ui.have_tk(),\n"
    "      '| REPO_ROOT == APP_ROOT', REPO_ROOT == paths.APP_ROOT,\n"
    "      '| web', paths.web_dir() is not None, '| icon', paths.icon_path() is not None)\n"
)


def verify(dist: Path) -> list[str]:
    """§7: the built runtime in an environment without repo / venv / PATH. No service, no window."""
    out: list[str] = []
    py = str(dist / "python" / "python.exe")
    before = {p for p in dist.rglob("*")}
    with tempfile.TemporaryDirectory(prefix="pwmc-verify-") as tmp:
        env = _clean_env(Path(tmp))
        steps = [
            ("imports", [py, "-c", f"import {IMPORTS}; print('imports ok')"]),
            ("ephemeris + paths", [py, "-c", EPHEM_CODE]),
            ("gateway --help", [py, "-m", "gateway", "--help"]),
            ("analysis --help", [py, "-m", "analysis", "--help"]),
            ("launcher --help", [py, "-m", "launcher", "--help"]),
            ("set_dsn --help", [py, "-m", "analysis.set_dsn", "--help"]),
        ]
        for label, cmd in steps:
            r = subprocess.run(cmd, cwd=dist, env=env, capture_output=True, text=True, timeout=300)
            first = (r.stdout.strip().splitlines() or [""])[0][:200]
            out.append(f"{label}: exit {r.returncode} | {first}")
            if r.returncode != 0:
                die(f"verification '{label}' failed:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
    created = [p for p in dist.rglob("*") if p not in before]
    out.append(f"files created in the package by the verification runs: {len(created)}")
    if created:
        die(
            "the runtime wrote into the package folder: "
            + ", ".join(str(p.relative_to(dist)) for p in created[:10])
        )
    return out


def largest_packages(site: Path, n: int = 10) -> list[tuple[str, int]]:
    sizes = []
    for d in site.iterdir():
        if d.is_dir() and not d.name.endswith((".dist-info", "__pycache__")):
            sizes.append((d.name, folder_size(d)))
        elif d.is_file() and d.suffix == ".pyd":
            sizes.append((d.name, d.stat().st_size))
    return sorted(sizes, key=lambda x: -x[1])[:n]


def zip_dist(dist: Path) -> Path:
    zpath = dist.parent / (dist.name + ".zip")
    if zpath.exists():
        zpath.unlink()
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(dist.rglob("*")):
            if f.is_file():
                z.write(f, Path(dist.name) / f.relative_to(dist))
    return zpath


# ------------------------------------------------------------------ main


ISCC_NAMES = (
    r"%LOCALAPPDATA%\Programs\Inno Setup 7",
    r"%ProgramFiles(x86)%\Inno Setup 7",
    r"%ProgramFiles(x86)%\Inno Setup 6",
    r"%ProgramFiles%\Inno Setup 7",
    r"%ProgramFiles%\Inno Setup 6",
)


def find_iscc() -> Path:
    env = os.environ.get("PW_ISCC")
    if env:
        if not Path(env).is_file():
            die(f"PW_ISCC={env} is not a file")
        return Path(env)
    for d in ISCC_NAMES:
        p = Path(os.path.expandvars(d)) / "ISCC.exe"
        if "%" not in str(p) and p.is_file():
            return p
    die("ISCC.exe (Inno Setup 7) not found: install it or set PW_ISCC")
    raise AssertionError  # unreachable


def build_installer(dist: Path, version: str) -> Path:
    iscc = find_iscc()
    numeric = ".".join((re.findall(r"\d+", app_version().split("+")[0]) + ["0"] * 4)[:4])
    cmd = [
        str(iscc),
        f"/DAppVersion={version}",
        f"/DAppVersionNumeric={numeric}",
        f"/DSourceDir={dist}",
        f"/DOutputDir={DIST}",
        str(PKG / "installer.iss"),
    ]
    r = run(cmd, cwd=ROOT, capture_output=True, text=True)
    for ln in (r.stdout + r.stderr).splitlines():
        print(f"[iscc] {ln}", flush=True)
    if r.returncode != 0:
        die(f"ISCC failed (exit {r.returncode})")
    setup = DIST / f"PW-Mount-Control-Setup-{version}.exe"
    if not setup.is_file():
        die(f"{setup.name} was not produced")
    log(f"installer: {setup}  {mb(setup.stat().st_size)}")
    return setup


def build() -> tuple[Path, str]:
    version = full_version()
    commit, dirty = git_info()
    pins = read_lock()
    dist = DIST / f"{APP_NAME}-{version}"
    rmtree(dist)
    dist.mkdir(parents=True)
    log(f"building {dist.name}")

    py_zip = fetch_python()
    install_python(py_zip, dist / "python")
    site = dist / "python" / "Lib" / "site-packages"

    wheels = wheels_for_lock(pins)
    install_wheels(site, CACHE / "wheels")
    adsb_version, adsb_commit = copy_adsb_core(site)

    copy_app(dist)
    web_files = build_web(dist / "web")
    copy_relay(dist / "relay")
    write_root_files(dist, version)
    compile_pyc(dist)

    n_files = safety_check(dist)
    log(f"safety check ok ({n_files} files: no .env*, no POSTGRESQL_DSN=, no URL login)")
    lines = verify(dist)
    for ln in lines:
        log(ln)

    total = folder_size(dist)
    info = [
        f"PW Mount Control {version}",
        f"git commit: {commit}  dirty: {'yes' if dirty else 'no'}",
        f"build time (UTC): {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')}",
        f"embeddable Python: {PY_VERSION} win-amd64  sha256 {PY_SHA256}",
        f"  source: {PY_URL}",
        f"adsb_core: {adsb_version}  (adsb_interpreter commit {adsb_commit or 'n/a'})",
        f"web build: {web_files} files (dev pages left out)",
        f"total size: {total} bytes ({mb(total)})",
        "",
        f"wheels ({len(wheels)}), from requirements-lock.txt:",
        *(
            f"  {w.name}  sha256 {sha256_of(w)}"
            + ("  (built from the PyPI sdist)" if n in SDIST_ONLY else "")
            for n, w in sorted(wheels.items())
        ),
        "",
        "verification:",
        *(f"  {ln}" for ln in lines),
    ]
    (dist / "BUILD-INFO.txt").write_text("\n".join(info) + "\n", encoding="utf-8", newline="\r\n")

    zpath = zip_dist(dist)
    log(f"folder: {dist}  {mb(folder_size(dist))}")
    log(f"zip:    {zpath}  {mb(zpath.stat().st_size)}")
    log("largest packages:")
    for name, size in largest_packages(site):
        log(f"  {mb(size):>10}  {name}")
    return dist, version


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--lock", action="store_true", help="regenerate requirements-lock.txt from .venv"
    )
    p.add_argument("--clean", action="store_true", help="remove dist/ and tools/package/.cache/")
    p.add_argument(
        "--installer", action="store_true", help="also compile the Inno Setup installer (ISCC)"
    )
    args = p.parse_args()
    if args.clean:
        rmtree(DIST)
        rmtree(CACHE)
        log("removed dist/ and the cache")
        return
    if args.lock:
        write_lock()
        return
    if sys.version_info[:2] != (3, 12) or not (ROOT / ".venv").is_dir():
        die("run this with the repo's .venv (Python 3.12)")
    dist, version = build()
    if args.installer:
        build_installer(dist, version)


if __name__ == "__main__":
    main()
