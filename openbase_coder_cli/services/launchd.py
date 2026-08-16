from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from string import Formatter

import click

from openbase_coder_cli.backend_binaries import backend_binary_candidates
from openbase_coder_cli.env_file import selected_backend_from_env_file
from openbase_coder_cli.livekit_install import installed_livekit_server_path
from openbase_coder_cli.paths import (
    DEFAULT_ENV_FILE_PATH,
    DEFAULT_LOG_DIR,
    LAUNCHD_DOMAIN,
    LAUNCHD_WRAPPER_DIR,
    OPENBASE_BASE_DIR,
    PLIST_DIR,
)
from openbase_coder_cli.platforms import (
    executable_suffixes,
    is_windows,
    venv_bin_dir,
)
from openbase_coder_cli.process_probe import pid_alive
from openbase_coder_cli.runtime import stable_runtime_package
from openbase_coder_cli.services.definitions import (
    RETIRED_SERVICE_NAMES,
    SERVICES,
    ServiceDefinition,
    default_services,
    retired_service_stub,
)
from openbase_coder_cli.services.installation import InstallationConfig


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _resolve_binary(name: str, homebrew_fallback: str | None = None) -> str:
    path = shutil.which(name)
    if path:
        return path
    fallbacks: list[Path] = []
    if homebrew_fallback:
        fallbacks.append(Path(homebrew_fallback))
    fallbacks.append(Path.home() / ".local" / "bin" / name)
    for fallback in fallbacks:
        if fallback.is_file():
            return str(fallback)
    raise click.ClickException(
        f"Could not find '{name}' on PATH. Please install it first."
    )


def _workspace_binary_candidates(config: InstallationConfig, name: str) -> list[Path]:
    if not config.workspace_path:
        return []
    workspace = Path(config.workspace_path)
    bin_dir = venv_bin_dir()
    # Windows venvs use Scripts/ and carry an executable suffix, so probe
    # every plausible spelling instead of the POSIX bin/<name> only.
    return [
        workspace / venv / bin_dir / f"{name}{suffix}"
        for venv in (".venv", "cli/.venv", "agent/.venv")
        for suffix in executable_suffixes()
    ]


def _resolve_binary_with_preferred_paths(
    name: str,
    preferred_paths: list[Path],
    homebrew_fallback: str | None = None,
) -> str:
    for path in preferred_paths:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return _resolve_binary(name, homebrew_fallback)


def _resolve_syncthing() -> str:
    from openbase_coder_cli.code_sync.syncthing import resolve_syncthing_binary

    return resolve_syncthing_binary()


def _runtime_workdir(config: InstallationConfig) -> str:
    runtime_package = stable_runtime_package()
    if runtime_package is not None:
        return str(runtime_package.root)
    return config.workspace_path or str(OPENBASE_BASE_DIR)


def _resolve_service_python(package) -> str:
    if package is not None and package.python_path.is_file():
        return str(package.python_path)
    return sys.executable


def _binary_resolvers(config: InstallationConfig) -> dict[str, Callable[[], str]]:
    # Standalone binaries are derived from the runtime package at generation
    # time (routed through the stable current/ alias) — never persisted, so a
    # self-update flip can't leave services pointing at a pruned release.
    package = stable_runtime_package()
    return {
        "uv": lambda: _resolve_binary_with_preferred_paths(
            "uv",
            _workspace_binary_candidates(config, "uv"),
            "/opt/homebrew/bin/uv",
        ),
        "codex": lambda: _resolve_binary_with_preferred_paths(
            "codex",
            backend_binary_candidates("codex"),
        ),
        "claude": lambda: _resolve_binary_with_preferred_paths(
            "claude",
            backend_binary_candidates("claude"),
        ),
        "livekit": lambda: _resolve_binary_with_preferred_paths(
            "livekit-server",
            [package.livekit_server_path]
            if package is not None
            else [installed_livekit_server_path()],
            "/opt/homebrew/bin/livekit-server",
        ),
        "python": lambda: _resolve_service_python(package),
        "syncthing": _resolve_syncthing,
        "openbase_coder": lambda: _resolve_binary_with_preferred_paths(
            "openbase-coder",
            [
                *([package.openbase_coder_path] if package is not None else []),
                *_workspace_binary_candidates(config, "openbase-coder"),
            ],
        ),
        "runtime_workdir": lambda: _runtime_workdir(config),
    }


def _service_template_keys(services: Iterable[ServiceDefinition]) -> set[str]:
    keys: set[str] = set()
    for svc in services:
        for template in (svc.command_template, svc.workdir_template):
            for _text, field, _spec, _conv in Formatter().parse(template):
                if field:
                    keys.add(field)
    return keys


def _resolve_binaries(
    config: InstallationConfig,
    services: Iterable[ServiceDefinition] | None = None,
) -> dict[str, str]:
    """Resolve only the binaries the given services actually reference.

    Resolution raises for binaries that cannot be found, so limiting it to the
    services being installed keeps optional backends (e.g. codex when Claude
    Code is selected) from failing installs on machines without them.
    """
    if services is None:
        services = SERVICES
    resolvers = _binary_resolvers(config)
    keys = _service_template_keys(services)
    return {key: resolvers[key]() for key in sorted(keys) if key in resolvers}


def _selected_backend(config: InstallationConfig) -> str:
    env_path = (
        Path(config.env_file).expanduser() if config.env_file else DEFAULT_ENV_FILE_PATH
    )
    return selected_backend_from_env_file(env_path)


def _uid() -> int:
    return os.getuid()


def _service_label(svc: ServiceDefinition) -> str:
    return f"{LAUNCHD_DOMAIN}.{svc.name}"


def _wrapper_path(svc: ServiceDefinition) -> Path:
    return LAUNCHD_WRAPPER_DIR / f"{svc.name}.sh"


def _plist_path(svc: ServiceDefinition) -> Path:
    return PLIST_DIR / f"{_service_label(svc)}.plist"


def _log_path(svc: ServiceDefinition) -> Path:
    return DEFAULT_LOG_DIR / f"{svc.name}.log"


def _truncate_log_file(path: Path, max_lines: int = 5000) -> None:
    if not path.exists():
        return

    lines = path.read_text(errors="replace").splitlines()
    trimmed = "\n".join(lines[-max_lines:])
    if trimmed:
        trimmed += "\n"

    with path.open("r+", encoding="utf-8", errors="replace") as handle:
        handle.seek(0)
        handle.write(trimmed)
        handle.truncate()


def _truncate_existing_logs(svc: ServiceDefinition) -> None:
    _truncate_log_file(_log_path(svc))


def _service_command(pid: int) -> str:
    if is_windows():
        return _service_command_windows(pid)
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ""
    return result.stdout.strip()


def _service_command_windows(pid: int) -> str:
    # CommandLine is not in tasklist output, so ask CIM for the full command.
    script = (
        "$p = Get-CimInstance Win32_Process -Filter 'ProcessId = %d' "
        "-ErrorAction SilentlyContinue; if ($p) { $p.CommandLine }" % pid
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _listening_pids(port: int) -> set[int]:
    if is_windows():
        return _listening_pids_netstat(port)
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return _listening_pids_ss(port)
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.add(int(line))
        except ValueError:
            continue
    return pids


def _listening_pids_ss(port: int) -> set[int]:
    try:
        result = subprocess.run(
            ["ss", "-ltnpH", f"sport = :{port}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return set()
    pids: set[int] = set()
    for match in re.finditer(r"pid=(\d+)", result.stdout):
        pids.add(int(match.group(1)))
    return pids


def _listening_pids_netstat(port: int) -> set[int]:
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[3].upper() != "LISTENING":
            continue
        local_address = fields[1]
        _, separator, local_port = local_address.rpartition(":")
        if not separator or local_port != str(port):
            continue
        if fields[4].isdigit():
            pids.add(int(fields[4]))
    return pids


def _signal_pid(pid: int, sig: signal.Signals) -> None:
    if is_windows():
        # Windows has no SIGTERM delivery; terminate the tree instead so the
        # supervisor does not leave the real service holding the port.
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return


def _matches_cleanup_signature(svc: ServiceDefinition, pid: int) -> bool:
    if not svc.cleanup_command_substrings:
        return True

    command = _service_command(pid)
    return all(token in command for token in svc.cleanup_command_substrings)


def _cleanup_candidate_pids(svc: ServiceDefinition) -> set[int]:
    candidates: set[int] = set()
    for port in svc.cleanup_ports:
        for pid in _listening_pids(port):
            if _matches_cleanup_signature(svc, pid):
                candidates.add(pid)
    return candidates


def _cleanup_lingering_processes(svc: ServiceDefinition) -> None:
    lingering_pids = _cleanup_candidate_pids(svc)

    if not lingering_pids:
        return

    for pid in lingering_pids:
        _signal_pid(pid, signal.SIGTERM)

    time.sleep(1)

    stubborn_pids = _cleanup_candidate_pids(svc)

    for pid in stubborn_pids:
        _signal_pid(pid, signal.SIGKILL)


def _ensure_launchd_paths() -> None:
    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    if _is_macos():
        LAUNCHD_WRAPPER_DIR.mkdir(parents=True, exist_ok=True)
        PLIST_DIR.mkdir(parents=True, exist_ok=True)
    elif is_windows():
        from openbase_coder_cli.paths import WINDOWS_RUN_DIR, WINDOWS_UNIT_DIR

        WINDOWS_UNIT_DIR.mkdir(parents=True, exist_ok=True)
        WINDOWS_RUN_DIR.mkdir(parents=True, exist_ok=True)
    else:
        from openbase_coder_cli.paths import SYSTEMD_UNIT_DIR

        LAUNCHD_WRAPPER_DIR.mkdir(parents=True, exist_ok=True)
        SYSTEMD_UNIT_DIR.mkdir(parents=True, exist_ok=True)


def _write_service_files(
    svc: ServiceDefinition,
    config: InstallationConfig,
    binaries: dict[str, str],
) -> None:
    if is_windows():
        # Windows units carry their own launch plan, so there is no shell
        # wrapper to generate.
        from openbase_coder_cli.services.windows import generate_unit as generate_win

        generate_win(svc, config)
        return
    generate_wrapper(svc, config, binaries)
    if _is_macos():
        generate_plist(svc, config)
    else:
        from openbase_coder_cli.services.systemd import generate_unit

        generate_unit(svc, config)


def _prepare_service_start(svc: ServiceDefinition) -> None:
    _truncate_existing_logs(svc)
    _cleanup_lingering_processes(svc)


def generate_wrapper(
    svc: ServiceDefinition,
    config: InstallationConfig,
    binaries: dict[str, str],
) -> Path:
    # In standalone mode there is no workspace checkout; fall back so
    # workdirs never render as an empty string.
    workspace = config.workspace_path or _runtime_workdir(config)
    env_file = config.env_file
    data_dir = str(OPENBASE_BASE_DIR)

    # Binary paths land in shell command position (e.g. the bundled CLI under
    # "/Applications/Openbase Coder.app" contains a space) — quote them.
    quoted_binaries = {name: shlex.quote(path) for name, path in binaries.items()}
    template_vars = {"workspace": workspace, "data_dir": data_dir, **quoted_binaries}
    cmd = svc.command_template.format(**template_vars)
    workdir = svc.workdir_template.format(
        workspace=workspace, data_dir=data_dir, **binaries
    )

    wrapper = _wrapper_path(svc)
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        textwrap.dedent(f"""\
        #!/bin/bash
        # Auto-generated wrapper for {svc.name}

        cd "{workdir}"

        if [ -f "{env_file}" ]; then
            set -a
            source "{env_file}"
            set +a
        fi

        export PATH="$HOME/.openbase/bin:$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

        {cmd}
    """)
    )
    wrapper.chmod(0o755)
    return wrapper


def generate_plist(svc: ServiceDefinition, config: InstallationConfig) -> Path:
    label = _service_label(svc)
    wrapper = _wrapper_path(svc)
    workdir = svc.workdir_template.format(
        workspace=config.workspace_path or _runtime_workdir(config),
        data_dir=str(OPENBASE_BASE_DIR),
        runtime_workdir=_runtime_workdir(config),
    )
    log_dir = DEFAULT_LOG_DIR

    plist = _plist_path(svc)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(
        textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{label}</string>
            <key>ProgramArguments</key>
            <array>
                <string>/bin/bash</string>
                <string>{wrapper}</string>
            </array>
            <key>WorkingDirectory</key>
            <string>{workdir}</string>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <{str(svc.keep_alive).lower()}/>
            <key>ThrottleInterval</key>
            <integer>5</integer>
            <key>StandardOutPath</key>
            <string>{log_dir}/{svc.name}.log</string>
            <key>StandardErrorPath</key>
            <string>{log_dir}/{svc.name}.log</string>
        </dict>
        </plist>
    """)
    )
    return plist


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def launchctl_bootstrap(svc: ServiceDefinition) -> None:
    if is_windows():
        from openbase_coder_cli.services.windows import windows_bootstrap

        windows_bootstrap(svc)
        return
    if not _is_macos():
        from openbase_coder_cli.services.systemd import systemd_bootstrap

        systemd_bootstrap(svc)
        return

    label = _service_label(svc)
    plist = _plist_path(svc)
    domain = f"gui/{_uid()}"
    _prepare_service_start(svc)
    _launchctl("enable", f"{domain}/{label}", check=False)
    for attempt in range(4):
        # Bootout on each attempt in case a prior bootstrap partially registered
        _launchctl("bootout", f"{domain}/{label}", check=False)
        time.sleep(0.5 * (attempt + 1))
        result = _launchctl("bootstrap", domain, str(plist), check=False)
        if result.returncode == 0:
            # An intermittent macOS state has been observed where bootstrap
            # succeeds but the job stays loaded without a PID, despite
            # RunAtLoad and KeepAlive (mechanism unconfirmed). Kickstart
            # without -k is a no-op for a running job and safely ensures the
            # newly registered job is asked to run.
            _launchctl("kickstart", f"{domain}/{label}", check=False)
            return
    raise click.ClickException(f"Failed to bootstrap {label}: {result.stderr.strip()}")


def launchctl_bootout(svc: ServiceDefinition) -> bool:
    if is_windows():
        from openbase_coder_cli.services.windows import windows_bootout

        return windows_bootout(svc)
    if not _is_macos():
        from openbase_coder_cli.services.systemd import systemd_bootout

        return systemd_bootout(svc)

    label = _service_label(svc)
    result = _launchctl("bootout", f"gui/{_uid()}/{label}", check=False)
    _cleanup_lingering_processes(svc)
    return result.returncode == 0


def launchctl_kickstart(svc: ServiceDefinition) -> bool:
    if is_windows():
        from openbase_coder_cli.services.windows import windows_kickstart

        return windows_kickstart(svc)
    if not _is_macos():
        from openbase_coder_cli.services.systemd import systemd_kickstart

        return systemd_kickstart(svc)

    label = _service_label(svc)
    _prepare_service_start(svc)
    result = _launchctl("kickstart", "-k", f"gui/{_uid()}/{label}", check=False)
    return result.returncode == 0


def launchctl_kill(svc: ServiceDefinition) -> bool:
    if is_windows():
        from openbase_coder_cli.services.windows import windows_kill

        return windows_kill(svc)
    if not _is_macos():
        from openbase_coder_cli.services.systemd import systemd_kill

        return systemd_kill(svc)

    label = _service_label(svc)
    result = _launchctl("kill", "SIGTERM", f"gui/{_uid()}/{label}", check=False)
    _cleanup_lingering_processes(svc)
    return result.returncode == 0


# Set to "external" when something other than launchd/systemd supervises the
# generated wrappers (e.g. the Docker entrypoint). The supervisor maintains
# <data_dir>/run/<name>.pid files: written on service start, removed on exit.
EXTERNAL_SUPERVISOR_ENV = "OPENBASE_CODER_SERVICE_SUPERVISOR"
EXTERNAL_SUPERVISOR_RUN_DIR = OPENBASE_BASE_DIR / "run"


def _external_supervisor() -> bool:
    return os.environ.get(EXTERNAL_SUPERVISOR_ENV, "").lower() == "external"


def _external_supervisor_status(svc: ServiceDefinition) -> dict:
    if not _wrapper_path(svc).is_file():
        return {"installed": False}
    pid: int | None = None
    try:
        pid = int((EXTERNAL_SUPERVISOR_RUN_DIR / f"{svc.name}.pid").read_text().strip())
    except (OSError, ValueError):
        pid = None
    if not pid_alive(pid):
        pid = None
    if not svc.install_by_default and pid is None:
        # Wrapper regeneration writes files for optional services (code-sync,
        # cloud heartbeat) regardless of whether their feature is on; under
        # an external supervisor "installed" means actually supervised, so a
        # disabled feature doesn't warn as an unexpectedly installed service.
        return {"installed": False}
    return {"installed": True, "pid": str(pid) if pid else None}


def launchctl_status(svc: ServiceDefinition) -> dict:
    if _external_supervisor():
        return _external_supervisor_status(svc)
    if is_windows():
        from openbase_coder_cli.services.windows import windows_status

        return windows_status(svc)
    if not _is_macos():
        from openbase_coder_cli.services.systemd import systemd_status

        return systemd_status(svc)

    label = _service_label(svc)
    result = _launchctl("print", f"gui/{_uid()}/{label}", check=False)
    if result.returncode != 0:
        return {"installed": False}

    info = result.stdout
    pid = None
    last_exit = None
    for line in info.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            pid = line.split("=")[1].strip()
        if "last exit code" in line:
            last_exit = line.split("=")[-1].strip()

    return {
        "installed": True,
        "pid": pid if pid and pid != "0" else None,
        "last_exit_code": last_exit,
    }


def install_all_services(config: InstallationConfig) -> None:
    _ensure_launchd_paths()
    coding_backend = _selected_backend(config)
    services = default_services(coding_backend)
    binaries = _resolve_binaries(config, services)

    for svc in default_services():
        if svc in services:
            continue
        if remove_service(svc):
            click.echo(
                f"  Removed {svc.name} (not used by the {coding_backend} backend)."
            )

    # Upgrades must not strand processes for services that no longer exist.
    for name in RETIRED_SERVICE_NAMES:
        if remove_service(retired_service_stub(name)):
            click.echo(f"  Removed retired service {name}.")

    for svc in services:
        click.echo(f"  Installing {svc.name}...")
        _write_service_files(svc, config, binaries)
        launchctl_bootstrap(svc)
        click.echo(f"    Loaded {_service_label(svc)}")

    click.echo()
    click.echo("All services installed and started.")
    click.echo(f"Logs: {DEFAULT_LOG_DIR}/")


def remove_service(svc: ServiceDefinition) -> bool:
    """Unload a service and delete its generated files. True if any existed."""
    existed = False
    plist = _plist_path(svc)
    wrapper = _wrapper_path(svc)
    if launchctl_status(svc).get("installed"):
        launchctl_bootout(svc)
        existed = True
    for path in (plist, wrapper):
        if path.exists():
            path.unlink()
            existed = True
    return existed


def install_service(config: InstallationConfig, svc: ServiceDefinition) -> None:
    _ensure_launchd_paths()
    binaries = _resolve_binaries(config, [svc])
    _write_service_files(svc, config, binaries)
    launchctl_bootstrap(svc)


def regenerate_service(config: InstallationConfig, svc: ServiceDefinition) -> None:
    _ensure_launchd_paths()
    binaries = _resolve_binaries(config, [svc])
    _write_service_files(svc, config, binaries)


def regenerate_all_services(config: InstallationConfig) -> None:
    _ensure_launchd_paths()
    coding_backend = _selected_backend(config)

    for svc in SERVICES:
        if not svc.supports_backend(coding_backend):
            click.echo(f"  Skipping {svc.name} (backend: {coding_backend}).")
            continue
        try:
            binaries = _resolve_binaries(config, [svc])
        except click.ClickException as exc:
            click.echo(click.style(f"  WARN  Skipping {svc.name}: {exc}", fg="yellow"))
            continue
        click.echo(f"  Regenerating {svc.name}...")
        _write_service_files(svc, config, binaries)

    click.echo("Regenerated all wrappers and plists.")
    click.echo("Run 'openbase-coder services install' to reload them.")
