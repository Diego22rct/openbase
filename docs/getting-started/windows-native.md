# Windows (Native, Experimental)

Openbase Coder is gaining a native Windows runtime — no Docker, no WSL. It is
not merged to `main` yet; the work lives on `develop` and a handful of
`feat/win-*` branches. Most users should still follow
[Run in Docker](../docker.md), the officially supported Windows path. Use this
guide if you are developing the native Windows port itself, or want to try it
before it ships.

## Prerequisites

In addition to the shared [prerequisites](index.md#prerequisites):

- Git
- [`uv`](https://docs.astral.sh/uv/) (installs its own pinned Python 3.12 —
  `uv python install 3.12` — no separate Python install needed)
- Node 20+ and pnpm for building the console from source
- [Tailscale for Windows](https://tailscale.com/download/windows) — `winget
  install --id Tailscale.Tailscale -e` also works
- **Windows Developer Mode**, enabled once per machine. Setup creates several
  symlinks (Claude/Codex config, thread-sync ignore files); without Developer
  Mode those calls fail with `OSError: [WinError 1314]`. Turn it on in
  **Settings → Privacy & security → For developers**, or from an elevated
  PowerShell:

  ```powershell
  New-Item -Path HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock -Force | Out-Null
  Set-ItemProperty -Path HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock -Name AllowDevelopmentWithoutDevLicense -Value 1 -Type DWord
  ```

## Clone and Run Setup

Same shape as [Developer Setup](developer-setup.md), but on `develop` (or a
`feat/win-*` branch) until native Windows support merges to `main`:

```powershell
uv tool install multi-workspace
git clone --branch develop --single-branch `
  https://github.com/openbase-community/openbase-coder-workspace
cd openbase-coder-workspace
.\scripts\setup --backend claude-code
```

`scripts/setup` is a bash script — run it from Git Bash, not PowerShell
directly (`bash scripts/setup --backend claude-code`). It fetches the sibling
repos with `multi`, syncs the `uv` workspace, builds the console with pnpm,
and runs `openbase-coder setup` against the checkout.

### If service install fails with "Access is denied"

`services install`/`start`/`up` create Windows Scheduled Tasks. On some
machines `schtasks /Create` needs an elevated shell even for a per-user task —
if setup's services step fails with `Error: Acceso denegado.` /
`Access is denied`, re-run just the services step from an elevated terminal:

```powershell
cd openbase-coder-workspace\cli
uv run openbase-coder services install
```

## Use the CLI Without `uv run`

Setup's CLI shim installer (`~/.local/bin/openbase-coder`) is POSIX-only today
and skips itself on Windows ("Workspace venv binary not found... skipping CLI
shim install"). Until that's fixed, add the workspace venv's `Scripts`
directory to your `PATH` so `openbase-coder` resolves directly:

```powershell
[Environment]::SetEnvironmentVariable(
  "PATH",
  "$env:USERPROFILE\path\to\openbase-coder-workspace\.venv\Scripts;" + [Environment]::GetEnvironmentVariable("PATH", "User"),
  "User"
)
```

(Open a new terminal afterward.) Until then, prefix every command with `cd
openbase-coder-workspace\cli && uv run` — that's what every command below
assumes if you skip this step.

## Authenticate With Openbase Cloud

```powershell
openbase-coder login
```

### If login hangs then fails with `WinError 10013`

The OAuth callback listens on a fixed local port (`127.0.0.1:52807` by
default — it must match the redirect URI registered with the backend, so it
can't just pick a free one). On some Windows machines that exact port is
already held by the `iphlpsvc` (IP Helper) service, and the CLI has no manual
fallback yet. Workaround: free the port for the login, then put it back —
from an elevated PowerShell:

```powershell
Stop-Service iphlpsvc -Force
# in another terminal: openbase-coder login
Start-Service iphlpsvc
Restart-Service Tailscale -Force   # iphlpsvc restart drops Tailscale's connection
```

Then reconfigure Serve if the console shows "Failed to fetch" afterward:

```powershell
openbase-coder services start
```

## Health Check

Same as [the shared health check](index.md#health-check):

```powershell
openbase-coder doctor
openbase-coder services status
curl http://127.0.0.1:7999/api/health/
```

## Known Gaps (as of this writing)

- `openbase-coder login`'s OAuth callback has no manual/paste-code fallback
  for when the local port can't bind (see above).
- The CLI shim installer doesn't produce a Windows-runnable shim yet (see
  "Use the CLI Without `uv run`" above).
- `openbase-coder self-update` is untested on Windows standalone installs.
- Some of the test suite still assumes a POSIX host (macOS `launchctl`,
  Linux `xdotool` computer-use, symlink-based thread-sync) and fails on
  Windows; this doesn't affect the runtime, only `pytest` runs from a
  workspace checkout.

If you hit something not listed here, check the branch's proposal/design
docs or ask in the team channel before re-diagnosing from scratch — several
of these were already root-caused once.
