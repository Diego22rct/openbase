"""Windows service backend (launchd is used on macOS, systemd on Linux).

Windows has no per-user service manager, so a unit here is a JSON descriptor
plus a detached :mod:`openbase_coder_cli.services.windows_runner` supervisor
that keeps the service alive and records its pid under ``~/.openbase/run``.
The public functions mirror the systemd backend so the launchd facade can
dispatch to either without caring which host it is on.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import click

from openbase_coder_cli.paths import (
    LAUNCHD_DOMAIN,
    OPENBASE_BASE_DIR,
    WINDOWS_RUN_DIR,
    WINDOWS_UNIT_DIR,
)
from openbase_coder_cli.process_probe import pid_alive as probe_pid_alive
from openbase_coder_cli.services.console_settings import get_ignored_launchctl_labels
from openbase_coder_cli.services.definitions import ServiceDefinition
from openbase_coder_cli.services.installation import InstallationConfig
from openbase_coder_cli.services.voice_warning import warn_before_voice_interruption

OPENBASE_UNIT_PREFIX = f"{LAUNCHD_DOMAIN}."
VOICE_INTERRUPTING_SERVICE_LABELS = {
    f"{LAUNCHD_DOMAIN}.livekit-agent",
    f"{LAUNCHD_DOMAIN}.livekit-server",
}

# Detach the supervisor so it outlives the CLI invocation that started it.
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

START_TIMEOUT_SECONDS = 10
STOP_TIMEOUT_SECONDS = 10


def _service_label(svc: ServiceDefinition) -> str:
    return f"{LAUNCHD_DOMAIN}.{svc.name}"


def unit_path(svc: ServiceDefinition) -> Path:
    return WINDOWS_UNIT_DIR / f"{_service_label(svc)}.json"


def pid_path(svc: ServiceDefinition) -> Path:
    return WINDOWS_RUN_DIR / f"{svc.name}.pid"


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int | None) -> bool:
    return probe_pid_alive(pid)


def _service_argv(
    svc: ServiceDefinition, binaries: dict[str, str]
) -> tuple[str, list[str], dict[str, str], dict[str, str], bool, str | None]:
    """Return (kind, argv, argv_defaults, extra_env, needs_livekit_url, binary).

    The shell wrapper bodies in ``definitions`` are POSIX programs, so each
    service's effective command is rebuilt here instead of being parsed.
    """
    openbase_coder = binaries.get("openbase_coder", "openbase-coder")
    python = binaries.get("python", sys.executable)

    if svc.name == "livekit-server":
        return "livekit-server", [], {}, {}, False, binaries.get("livekit")
    if svc.name == "django-cli":
        return (
            "command",
            [
                openbase_coder,
                "server",
                "--host",
                "${OPENBASE_CODER_CLI_HOST}",
                "--port",
                "${OPENBASE_CODER_CLI_PORT}",
            ],
            {"OPENBASE_CODER_CLI_HOST": "127.0.0.1", "OPENBASE_CODER_CLI_PORT": "7999"},
            {},
            True,
            None,
        )
    if svc.name == "livekit-agent":
        return (
            "command",
            [python, "-m", "openbase_coder_cli.livekit_agent.livekit", "start"],
            {},
            {"LIVEKIT_AGENT_LOAD_THRESHOLD": "2.0"},
            True,
            None,
        )
    if svc.name == "sync-workers":
        return "command", [openbase_coder, "sync-workers", "run"], {}, {}, False, None
    if svc.name == "openbase-routines":
        return (
            "command",
            [
                openbase_coder,
                "routines",
                "run-loop",
                "--interval",
                "${OPENBASE_CODER_ROUTINES_INTERVAL}",
            ],
            {"OPENBASE_CODER_ROUTINES_INTERVAL": "60"},
            {},
            False,
            None,
        )
    if svc.name == "openbase-cloud-heartbeat":
        return (
            "command",
            [
                openbase_coder,
                "cloud",
                "heartbeat",
                "--interval",
                "${OPENBASE_CLOUD_HEARTBEAT_INTERVAL}",
            ],
            {"OPENBASE_CLOUD_HEARTBEAT_INTERVAL": "60"},
            {},
            False,
            None,
        )
    if svc.name == "openbase-cloud-auth-rehydrate":
        return (
            "command",
            [openbase_coder, "cloud", "rehydrate-auth"],
            {},
            {},
            False,
            None,
        )
    raise click.ClickException(
        f"Service '{svc.name}' has no Windows launch plan. Supported services "
        "are listed in services/windows.py."
    )


def generate_unit(svc: ServiceDefinition, config: InstallationConfig) -> Path:
    from openbase_coder_cli.services.launchd import (
        _log_path,
        _resolve_binaries,
        _runtime_workdir,
    )

    binaries = _resolve_binaries(config, [svc])
    kind, argv, argv_defaults, extra_env, needs_url, binary = _service_argv(
        svc, binaries
    )
    workdir = svc.workdir_template.format(
        workspace=config.workspace_path or _runtime_workdir(config),
        data_dir=str(OPENBASE_BASE_DIR),
        runtime_workdir=_runtime_workdir(config),
    )
    Path(workdir).mkdir(parents=True, exist_ok=True)

    unit = {
        "name": svc.name,
        "label": _service_label(svc),
        "description": svc.description,
        "kind": kind,
        "argv": argv,
        "argv_defaults": argv_defaults,
        "binary": binary,
        "extra_env": extra_env,
        "needs_livekit_url": needs_url,
        "cwd": workdir,
        "env_file": str(config.env_file),
        "log": str(_log_path(svc)),
        "pid_file": str(pid_path(svc)),
        "restart": svc.restart_policy,
        "keep_alive": svc.keep_alive,
    }

    path = unit_path(svc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(unit, indent=2) + "\n", encoding="utf-8")
    return path


def remove_unit(svc: ServiceDefinition) -> None:
    unit_path(svc).unlink(missing_ok=True)


def _supervisor_argv(svc: ServiceDefinition) -> list[str]:
    return [
        sys.executable,
        "-m",
        "openbase_coder_cli.services.windows_runner",
        str(unit_path(svc)),
    ]


def windows_bootstrap(svc: ServiceDefinition) -> None:
    from openbase_coder_cli.services.launchd import _prepare_service_start

    unit = unit_path(svc)
    if not unit.is_file():
        raise click.ClickException(
            f"Windows unit for {svc.name} is missing; run 'openbase-coder setup'."
        )
    windows_bootout(svc)
    _prepare_service_start(svc)
    WINDOWS_RUN_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        _supervisor_argv(svc),
        cwd=str(OPENBASE_BASE_DIR),
        creationflags=_DETACHED,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if pid_alive(_read_pid(pid_path(svc))):
            return
        time.sleep(0.2)
    raise click.ClickException(
        f"Failed to start {_service_label(svc)}: supervisor did not report a pid. "
        f"See {_log_hint(svc)}."
    )


def _log_hint(svc: ServiceDefinition) -> str:
    from openbase_coder_cli.services.launchd import _log_path

    return str(_log_path(svc))


def _running_supervisor_pids(svc: ServiceDefinition) -> set[int]:
    """Every supervisor process currently running this service's unit.

    The pid file alone is not enough: a supervisor that gave up after a
    crash loop removes its pid file on the way out, and one that was started
    twice leaves an untracked twin. Both would then race the new supervisor
    for the service's ports, so match on the unit path instead.
    """
    unit = str(unit_path(svc)).replace("'", "''")
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
        "-ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and "
        f"$_.CommandLine.Contains('{unit}') "
        "} | ForEach-Object { $_.ProcessId }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        value = line.strip()
        if value.isdigit() and int(value) != os.getpid():
            pids.add(int(value))
    return pids


def windows_bootout(svc: ServiceDefinition) -> bool:
    from openbase_coder_cli.services.launchd import _cleanup_lingering_processes

    path = pid_path(svc)
    pids = _running_supervisor_pids(svc)
    recorded = _read_pid(path)
    if pid_alive(recorded) and recorded is not None:
        pids.add(recorded)

    was_running = bool(pids)
    for pid in pids:
        _terminate_tree(pid)
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline and any(pid_alive(pid) for pid in pids):
        time.sleep(0.2)

    path.unlink(missing_ok=True)
    _cleanup_lingering_processes(svc)
    return was_running


def _terminate_tree(pid: int) -> None:
    # The supervisor spawns the real service, so kill the whole tree rather
    # than orphaning the child onto the port it holds.
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
        check=False,
    )


def windows_kickstart(svc: ServiceDefinition) -> bool:
    try:
        windows_bootstrap(svc)
    except click.ClickException:
        return False
    return True


def windows_kill(svc: ServiceDefinition) -> bool:
    return windows_bootout(svc)


def windows_status(svc: ServiceDefinition) -> dict:
    if not unit_path(svc).is_file():
        return {"installed": False}
    pid = _read_pid(pid_path(svc))
    alive = pid_alive(pid)
    return {
        "installed": True,
        "pid": str(pid) if alive and pid else None,
        "last_exit_code": None,
    }


@dataclass
class WindowsUserService:
    label: str
    unit_path: str
    loaded: bool
    running: bool
    pid: int | None
    command: str | None
    working_directory: str | None
    keep_alive: bool
    is_openbase_managed: bool

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "plist_path": self.unit_path,
            "loaded": self.loaded,
            "running": self.running,
            "pid": self.pid,
            "status": None,
            "command": self.command,
            "program": None,
            "program_arguments": [],
            "working_directory": self.working_directory,
            "run_at_load": True,
            "keep_alive": self.keep_alive,
            "disabled": None,
            "is_openbase_managed": self.is_openbase_managed,
            "plist_error": None,
        }


def _unit_paths() -> list[Path]:
    if not WINDOWS_UNIT_DIR.is_dir():
        return []
    return sorted(path for path in WINDOWS_UNIT_DIR.glob("*.json") if path.is_file())


def _read_unit(path: Path) -> WindowsUserService:
    label = path.stem
    try:
        unit = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        unit = {}
    pid = _read_pid(Path(unit.get("pid_file", ""))) if unit.get("pid_file") else None
    running = pid_alive(pid)
    argv = unit.get("argv") or []
    command = " ".join(argv) if argv else unit.get("binary")
    return WindowsUserService(
        label=unit.get("label", label),
        unit_path=str(path),
        loaded=bool(unit),
        running=running,
        pid=pid if running else None,
        command=command,
        working_directory=unit.get("cwd"),
        keep_alive=bool(unit.get("keep_alive", True)),
        is_openbase_managed=label.startswith(OPENBASE_UNIT_PREFIX),
    )


def list_windows_services_payload(include_ignored: bool = False) -> dict:
    services = [_read_unit(path) for path in _unit_paths()]
    ignored_labels = set(get_ignored_launchctl_labels())
    if not include_ignored:
        services = [
            service for service in services if service.label not in ignored_labels
        ]
    services.sort(
        key=lambda service: (not service.running, not service.loaded, service.label)
    )
    return {
        "services": [service.to_dict() for service in services],
        "error": None,
        "ignored_labels": sorted(ignored_labels),
    }


def run_windows_service_action(label: str, action: str) -> None:
    if action not in {"start", "stop", "restart"}:
        raise click.ClickException(f"Unsupported service action '{action}'.")

    from openbase_coder_cli.services.registry import find_service

    if not (WINDOWS_UNIT_DIR / f"{label}.json").is_file():
        raise click.ClickException(
            f"Windows unit '{label}' was not found in {WINDOWS_UNIT_DIR}."
        )

    if action in {"stop", "restart"} and label in VOICE_INTERRUPTING_SERVICE_LABELS:
        warn_before_voice_interruption(
            reason=f"service {action} {label}",
            emit_cli_warning=False,
        )

    svc = find_service(label.removeprefix(OPENBASE_UNIT_PREFIX))
    if action == "stop":
        windows_bootout(svc)
        return
    windows_bootstrap(svc)
