"""Supervisor process for a single Windows service.

Windows has no per-user service manager equivalent to launchd agents or
systemd user units, so one of these runs per service. It owns exactly what
the unit files own elsewhere: environment loading, the working directory,
log redirection, the restart policy, and a pid file the CLI reads back.

Invoked as ``python -m openbase_coder_cli.services.windows_runner <unit.json>``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from openbase_coder_cli.services.windows_launch import (
    LaunchError,
    livekit_client_url,
    livekit_server_argv,
    parse_env_file,
)

# Matches RestartSec in the systemd units.
RESTART_DELAY_SECONDS = 5
# Stop restarting when a service dies immediately over and over, so a
# misconfigured install fails visibly instead of spinning forever.
CRASH_LOOP_THRESHOLD = 5
CRASH_LOOP_WINDOW_SECONDS = 30


def _log(handle, message: str) -> None:
    handle.write(f"[supervisor] {message}\n")
    handle.flush()


def build_environment(unit: dict) -> dict[str, str]:
    env = dict(os.environ)
    env.update(parse_env_file(Path(unit["env_file"])))
    env.update(unit.get("extra_env") or {})
    if unit.get("needs_livekit_url"):
        env["LIVEKIT_URL"] = livekit_client_url(env)
    return env


def _resolve_argv(unit: dict, env: dict[str, str]) -> list[str]:
    """Expand ``${VAR}`` argv slots, mirroring the shell's ``${VAR:-default}``."""
    if unit["kind"] == "livekit-server":
        return livekit_server_argv(unit["binary"], env)
    defaults = unit.get("argv_defaults") or {}
    resolved: list[str] = []
    for arg in unit["argv"]:
        if isinstance(arg, str) and arg.startswith("${") and arg.endswith("}"):
            name = arg[2:-1]
            resolved.append(env.get(name) or defaults.get(name, ""))
        else:
            resolved.append(arg)
    return resolved


def run(unit_path: Path) -> int:
    unit = json.loads(unit_path.read_text(encoding="utf-8"))
    log_path = Path(unit["log"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path = Path(unit["pid_file"])
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    child: subprocess.Popen | None = None
    stopping = False

    def _handle_stop(signum, _frame):  # noqa: ANN001 - signal handler shape
        nonlocal stopping
        stopping = True
        if child and child.poll() is None:
            child.terminate()

    for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            try:
                signal.signal(sig, _handle_stop)
            except (ValueError, OSError):
                pass

    restart_policy = unit.get("restart")
    recent_failures: list[float] = []
    exit_code = 0

    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as log_handle:
            while not stopping:
                try:
                    env = build_environment(unit)
                    argv = _resolve_argv(unit, env)
                except LaunchError as exc:
                    _log(log_handle, f"{exc}")
                    return 1

                _log(log_handle, f"starting {unit['name']}")
                child = subprocess.Popen(
                    argv,
                    cwd=unit["cwd"],
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )
                exit_code = child.wait()
                child = None

                if stopping:
                    break
                if restart_policy is None:
                    break
                if restart_policy == "on-failure" and exit_code == 0:
                    break

                now = time.monotonic()
                recent_failures = [
                    stamp
                    for stamp in recent_failures
                    if now - stamp < CRASH_LOOP_WINDOW_SECONDS
                ]
                recent_failures.append(now)
                if len(recent_failures) >= CRASH_LOOP_THRESHOLD:
                    _log(
                        log_handle,
                        f"{unit['name']} exited {len(recent_failures)} times in "
                        f"{CRASH_LOOP_WINDOW_SECONDS}s; giving up",
                    )
                    return exit_code or 1

                _log(
                    log_handle,
                    f"{unit['name']} exited with {exit_code}; "
                    f"restarting in {RESTART_DELAY_SECONDS}s",
                )
                time.sleep(RESTART_DELAY_SECONDS)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
        pid_path.unlink(missing_ok=True)

    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: windows_runner <unit.json>", file=sys.stderr)
        return 2
    return run(Path(args[0]))


if __name__ == "__main__":
    raise SystemExit(main())
