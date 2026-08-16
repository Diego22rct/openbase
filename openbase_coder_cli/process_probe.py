"""Cross-platform liveness check for a supervised process id.

``os.kill(pid, 0)`` is the POSIX idiom, but it is not a reliable probe on
Windows: CPython routes it through OpenProcess/TerminateProcess, and for a
detached process it can raise ERROR_INVALID_PARAMETER while the process is
very much alive. Windows therefore asks the kernel directly instead.
"""

from __future__ import annotations

import os

from openbase_coder_cli.platforms import is_windows

if is_windows():
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _WAIT_TIMEOUT = 0x00000102

    def _windows_pid_alive(pid: int) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid
        )
        if not handle:
            return False
        try:
            # A live process never signals, so the wait times out; an exited
            # one signals immediately. This avoids the STILL_ACTIVE(259)
            # ambiguity of GetExitCodeProcess.
            return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)


def pid_alive(pid: int | None) -> bool:
    """Whether ``pid`` refers to a running process."""
    if pid is None or pid <= 0:
        return False
    if is_windows():
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
