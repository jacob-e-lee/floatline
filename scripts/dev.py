#!/usr/bin/env python
"""Floatline - one-command local development / test runner.

WHY A PYTHON LAUNCHER: this machine's PowerShell execution policy is Undefined,
which blocks ``.ps1`` files (the bare ``npm`` shim is blocked for the same
reason). ``py -3.14`` runs regardless of policy, so a Python launcher is the
only form that reliably works here and on the VPS.

Usage (run from anywhere)::

    py -3.14 scripts\\dev.py                 # backend + Astro dev server
    py -3.14 scripts\\dev.py --no-loop       # same, but pause real trading
    py -3.14 scripts\\dev.py --prod          # build + serve the real dist
    py -3.14 scripts\\dev.py --smoke         # start stack, run the full test suite
    py -3.14 scripts\\dev.py --backend-only  # just the API

Press Ctrl+C once to shut everything down.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

BACKEND_PORT = 8000
DEV_PORT = 4321
PREVIEW_PORT = 4173

REQUIRED_MODULES = (
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "asyncpg",
    "apscheduler",
    "httpx",
    "pydantic_settings",
)


def log(msg: str) -> None:
    print(f"[dev] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"\n[dev] ERROR: {msg}\n", file=sys.stderr, flush=True)
    raise SystemExit(1)


def is_windows() -> bool:
    return os.name == "nt"


def find_npm() -> str:
    """Locate npm, preferring the .cmd shim that bypasses the PS1 policy block."""
    if is_windows():
        for candidate in ("npm.cmd", "npm.exe"):
            found = shutil.which(candidate)
            if found:
                return found
    found = shutil.which("npm")
    if found:
        return found
    die("npm was not found on PATH. Install Node.js 22+ and retry.")
    raise AssertionError("unreachable")


def run_checked(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    """Run a command to completion, streaming its output."""
    printable = " ".join(cmd)
    log(f"$ {printable}")
    result = subprocess.run(cmd, cwd=str(cwd), env=env)
    if result.returncode != 0:
        die(f"command failed ({result.returncode}): {printable}")


def npm_cmd(npm: str, *args: str) -> list[str]:
    """Wrap npm for CreateProcess: .cmd shims must go through cmd.exe."""
    if is_windows() and npm.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", npm, *args]
    return [npm, *args]


def spawn(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen:
    """Start a long-lived child in its own process group for clean teardown."""
    creationflags = 0
    if is_windows():
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    log(f"$ {' '.join(cmd)}   (cwd={cwd.name})")
    return subprocess.Popen(cmd, cwd=str(cwd), env=env, creationflags=creationflags)


def terminate(proc: subprocess.Popen | None) -> None:
    """Kill a child and, on Windows, its whole process tree (npm spawns node)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if is_windows():
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            proc.terminate()
    except Exception:  # noqa: BLE001 - teardown must never raise
        pass


def port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_for_health(port: int, timeout: float = 45.0) -> bool:
    """Poll /api/health until the API answers or the timeout expires."""
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(1.0)
    return False


def check_backend_deps() -> None:
    """Confirm the *current* interpreter has the backend requirements."""
    probe = ";".join(f"import {name}" for name in REQUIRED_MODULES)
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True
    )
    if result.returncode == 0:
        log(f"backend dependencies OK  ({sys.executable})")
        return

    missing = result.stderr.strip().splitlines()[-1] if result.stderr else "unknown"
    print(
        f"\n[dev] The interpreter running this script is missing backend packages:\n"
        f"      {sys.executable}\n      {missing}\n\n"
        f"      Note: bare `python` on this machine is MSYS2's interpreter and has\n"
        f"      NONE of the dependencies. Use the Windows launcher instead:\n\n"
        f"        py -3.14 scripts\\dev.py\n\n"
        f"      Or install into the current interpreter:\n\n"
        f"        {Path(sys.executable).name} -m pip install -r backend\\requirements.txt "
        f"-r backend\\requirements-dev.txt\n",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(1)


def ensure_frontend_deps(npm: str) -> None:
    if (FRONTEND_DIR / "node_modules").is_dir():
        log("frontend node_modules present")
        return
    log("installing frontend dependencies (first run, this takes a minute)...")
    run_checked(npm_cmd(npm, "install"), FRONTEND_DIR)


def print_banner(port: int, loop_enabled: bool, mode: str) -> None:
    api = f"http://127.0.0.1:{BACKEND_PORT}"
    dash = f"http://localhost:{port}"
    width = 66
    print("\n" + "=" * width)
    print("  FLOATLINE - local stack running")
    print("=" * width)
    print(f"  Dashboard   {dash}          ({mode})")
    print(f"  API docs    {api}/docs")
    print(f"  Health      {api}/api/health")
    print(f"  PID loop    {'ENABLED - real transactions every 60s' if loop_enabled else 'disabled'}")
    if loop_enabled:
        print("")
        print("  NOTE: the controller runs against Nessie + Alpaca paper and WILL")
        print("        place real transfers/orders. Use --no-loop to pause it.")
    print("-" * width)
    print("  Ctrl+C to stop everything")
    print("=" * width + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Floatline stack locally for testing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--prod", action="store_true",
                        help="build the Astro bundle and serve it (production shape)")
    parser.add_argument("--no-loop", dest="no_loop", action="store_true",
                        help="disable the APScheduler PID control loop (no real trades)")
    parser.add_argument("--loop", dest="force_loop", action="store_true",
                        help="force the control loop on even in --smoke mode")
    parser.add_argument("--smoke", action="store_true",
                        help="after startup, run the PID unit tests + the API smoke test")
    parser.add_argument("--backend-only", action="store_true",
                        help="start only the FastAPI backend")
    parser.add_argument("--open", dest="open_browser", action="store_true",
                        help="open the dashboard in your browser")
    args = parser.parse_args()

    # The smoke test drives the control cycle itself and needs deterministic
    # state, so the background loop is paused for it unless --loop is given.
    loop_enabled = args.force_loop or not (args.no_loop or args.smoke)
    frontend_port = PREVIEW_PORT if args.prod else DEV_PORT

    check_backend_deps()

    env = dict(os.environ)
    env["CONTROL_LOOP_ENABLED"] = "true" if loop_enabled else "false"
    env["PYTHONUNBUFFERED"] = "1"

    npm: str | None = None
    if not args.backend_only:
        npm = find_npm()
        ensure_frontend_deps(npm)

    for label, port in (("backend", BACKEND_PORT), ("frontend", frontend_port)):
        if port_in_use(port):
            die(
                f"port {port} is already in use ({label}). "
                f"Stop the existing process and retry.\n"
                f"      Tip: Get-NetTCPConnection -LocalPort {port} -State Listen"
            )

    backend: subprocess.Popen | None = None
    frontend: subprocess.Popen | None = None
    try:
        log(f"starting FastAPI on :{BACKEND_PORT} (control loop {'on' if loop_enabled else 'off'})")
        backend = spawn(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
            BACKEND_DIR,
            env,
        )

        log("waiting for /api/health ...")
        if not wait_for_health(BACKEND_PORT):
            terminate(backend)
            die(
                "the API never became healthy. Scroll up for the uvicorn traceback.\n"
                "      Common causes: bad DATABASE_URL in .env, or a missing dependency."
            )
        log("API healthy")

        if args.smoke:
            log("running PID unit tests")
            run_checked([sys.executable, "-m", "pytest", "tests", "-q"], BACKEND_DIR)
            log("running end-to-end smoke test")
            run_checked(
                [sys.executable, str(SCRIPTS_DIR / "smoke.py"),
                 "--base", f"http://127.0.0.1:{BACKEND_PORT}"],
                BACKEND_DIR,
                env,
            )

        if npm is not None:
            if args.prod:
                log("building the production bundle")
                run_checked(npm_cmd(npm, "run", "build"), FRONTEND_DIR)
                script = "preview"
            else:
                script = "dev"
            log(f"starting Astro ({script}) on :{frontend_port}")
            frontend = spawn(npm_cmd(npm, "run", script), FRONTEND_DIR, env)

        print_banner(frontend_port, loop_enabled, "astro preview" if args.prod else "astro dev")
        if args.open_browser and npm is not None:
            webbrowser.open(f"http://localhost:{frontend_port}")

        # Supervise: if either child dies, report it instead of hanging quietly.
        while True:
            time.sleep(1.0)
            if backend.poll() is not None:
                log(f"backend exited with code {backend.returncode}")
                return backend.returncode or 0
            if frontend is not None and frontend.poll() is not None:
                log(f"frontend exited with code {frontend.returncode}")
                return frontend.returncode or 0
    except KeyboardInterrupt:
        print()
        log("shutting down...")
        return 0
    finally:
        terminate(frontend)
        terminate(backend)
        log("stopped")


if __name__ == "__main__":
    raise SystemExit(main())
