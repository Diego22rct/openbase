"""Native launch plans for Windows services.

The POSIX backends run each service through a generated ``bash`` wrapper, and
the wrapper bodies in :mod:`openbase_coder_cli.services.definitions` are real
shell programs: ``case`` statements, arrays, ``printf``-built YAML, and
``ipconfig``/``ip route`` interface discovery. Batch cannot express that
readably, so Windows skips the shell entirely and rebuilds the same decisions
here in Python, producing an argv plus an environment for each service.

Keep this in sync with the shell bodies: the two must agree on how
``LIVEKIT_NODE_IP``, ``LIVEKIT_URL``, and the LiveKit RTC config are derived.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

TAILSCALE_FALLBACKS = (
    Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    / "Tailscale"
    / "tailscale.exe",
    Path(os.environ.get("ProgramData", "C:/ProgramData"))
    / "chocolatey"
    / "bin"
    / "tailscale.exe",
)

DEFAULT_NETWORK_MODE = "tailscale"
DEFAULT_TCP_PORT = "7881"
DEFAULT_UDP_PORT = "7882"
DEFAULT_BIND_IP = "127.0.0.1"
# Go's net.Interfaces() reports the Windows adapter friendly name, which is
# what LiveKit matches against, and Windows' loopback adapter is this.
LOOPBACK_INTERFACE = "Loopback Pseudo-Interface 1"


class LaunchError(RuntimeError):
    """A service cannot be launched with the current configuration."""


@dataclass
class LaunchPlan:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)


def parse_env_file(path: Path) -> dict[str, str]:
    """Read a ``KEY=value`` env file the way the shell wrapper's ``set -a`` does."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def resolve_tailscale_cli() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in TAILSCALE_FALLBACKS:
        if candidate.is_file():
            return str(candidate)
    return None


def _tailscale_ip(cli: str, flag: str) -> str | None:
    try:
        result = subprocess.run(
            [cli, "ip", flag],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return None


def _valid_ipv4(value: str | None) -> str | None:
    if not value:
        return None
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return None
    return value


def _valid_ipv6(value: str | None) -> str | None:
    if not value or not re.fullmatch(r"[0-9A-Fa-f:]+", value):
        return None
    try:
        ipaddress.IPv6Address(value)
    except ValueError:
        return None
    return value


def interface_name_for_ip(ip: str) -> str | None:
    """Windows adapter alias owning ``ip``, or None when it is not assigned."""
    script = (
        "$a = Get-NetIPAddress -IPAddress '%s' -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 -ExpandProperty InterfaceAlias; "
        "if ($a) { Write-Output $a }" % ip
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
        return None
    alias = result.stdout.strip()
    return alias or None


def resolve_node_ips(env: dict[str, str]) -> tuple[str | None, str | None]:
    """Resolve the Tailscale IPv4/IPv6 the LiveKit services advertise."""
    node_ip = _valid_ipv4(env.get("LIVEKIT_NODE_IP"))
    node_ip_v6 = _valid_ipv6(env.get("LIVEKIT_NODE_IP_V6"))
    if node_ip and node_ip_v6:
        return node_ip, node_ip_v6

    cli = resolve_tailscale_cli()
    if cli is None:
        return node_ip, node_ip_v6
    if not node_ip:
        node_ip = _valid_ipv4(_tailscale_ip(cli, "-4"))
    if not node_ip_v6:
        node_ip_v6 = _valid_ipv6(_tailscale_ip(cli, "-6"))
    return node_ip, node_ip_v6


def _network_mode(env: dict[str, str]) -> str:
    return env.get("LIVEKIT_NETWORK_MODE") or DEFAULT_NETWORK_MODE


def livekit_client_url(env: dict[str, str]) -> str:
    """LIVEKIT_URL for services that connect to LiveKit (django, agent)."""
    mode = _network_mode(env)
    if mode == "tailscale":
        return env.get("LIVEKIT_AGENT_URL") or "ws://localhost:7880"
    if mode in ("local", "lan"):
        existing = env.get("LIVEKIT_URL")
        if existing:
            return existing
        node_ip, _ = resolve_node_ips(env)
        host = node_ip if mode == "lan" and node_ip else "localhost"
        return f"ws://{host}:7880"
    raise LaunchError(f"Unsupported LIVEKIT_NETWORK_MODE: {mode}")


def _rtc_config_body(
    tcp_port: str,
    udp_port: str,
    interfaces: list[str],
    ips: list[str],
) -> str:
    lines = [
        "rtc:",
        f"  tcp_port: {tcp_port}",
        f"  udp_port: {udp_port}",
        "  enable_loopback_candidate: true",
        "  interfaces:",
        "    includes:",
    ]
    lines.extend(f"      - {name}" for name in interfaces)
    lines.append("  ips:")
    lines.append("    includes:")
    lines.extend(f"      - {value}" for value in ips)
    return "\n".join(lines) + "\n"


def _livekit_keys(env: dict[str, str]) -> str:
    api_key = env.get("LIVEKIT_API_KEY", "")
    api_secret = env.get("LIVEKIT_API_SECRET", "")
    keys = f"{api_key}: {api_secret}"
    client_key = env.get("LIVEKIT_CLIENT_API_KEY")
    client_secret = env.get("LIVEKIT_CLIENT_API_SECRET")
    if (
        client_key
        and client_secret
        and client_key != api_key
        and client_secret != api_secret
    ):
        keys = f"{keys}\n{client_key}: {client_secret}"
    return keys


def livekit_server_argv(binary: str, env: dict[str, str]) -> list[str]:
    """Rebuild the livekit-server argv the shell wrapper would exec."""
    mode = _network_mode(env)
    tcp_port = env.get("LIVEKIT_TCP_PORT") or DEFAULT_TCP_PORT
    udp_port = env.get("LIVEKIT_UDP_PORT") or DEFAULT_UDP_PORT
    bind_ip = env.get("LIVEKIT_BIND_IP") or DEFAULT_BIND_IP

    if mode == "local":
        config_body = _rtc_config_body(
            tcp_port, udp_port, [LOOPBACK_INTERFACE], ["127.0.0.1/32"]
        )
        node_ip_args = ["--node-ip", bind_ip]
    elif mode == "tailscale":
        node_ip, node_ip_v6 = resolve_node_ips(env)
        if not node_ip:
            raise LaunchError(
                "LIVEKIT_NODE_IP is required for Tailscale LiveKit signaling "
                "and media. Connect Tailscale, then restart services."
            )
        interface = env.get("LIVEKIT_INTERFACE") or interface_name_for_ip(node_ip)
        if not interface:
            raise LaunchError(
                "LIVEKIT_INTERFACE is required for Tailscale LiveKit media; "
                f"no Windows adapter owns {node_ip}."
            )
        ips = ["127.0.0.1/32", f"{node_ip}/32"]
        if node_ip_v6:
            ips.append(f"{node_ip_v6}/128")
        config_body = _rtc_config_body(
            tcp_port, udp_port, [LOOPBACK_INTERFACE, interface], ips
        )
        node_ip_args = ["--node-ip", node_ip]
    else:
        raise LaunchError(f"Unsupported LIVEKIT_NETWORK_MODE: {mode}")

    return [
        binary,
        "--dev",
        "--bind",
        bind_ip,
        "--config-body",
        config_body,
        *node_ip_args,
        "--keys",
        _livekit_keys(env),
    ]
