"""Shared local HTTP server test helper for Phase 9A.1 portability.

Owns bounded server shutdown and port-release checks so that every fixture
that spawns a Uvicorn/Stub subprocess gets deterministic cleanup.

Architecture decisions (do not change without plan amendment):
- Graceful shutdown → terminate → kill fallback with monotonic polling
- Verified port release before returning (prevents cross-test port conflicts)
- No immediate connect_ex assertions — bounded release verification only
"""

from __future__ import annotations

import socket
import subprocess
import time
from collections.abc import Sequence
from contextlib import closing


def _is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Return True when no process is listening on *host*:*port*.

    Uses bind() (not connect_ex) so the check works regardless of whether
    the listener has called listen() yet — connect_ex can succeed against
    a socket that has only been bound, producing false negatives.
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(0.1)
        try:
            sock.bind((host, port))
            sock.listen(0)
            return True
        except OSError:
            return False


def wait_for_port_release(
    port: int,
    *,
    host: str = "127.0.0.1",
    deadline: float | None = None,
    poll_interval: float = 0.02,
) -> bool:
    """Poll until *port* is free or *deadline* is exceeded.

    Returns True when the port is confirmed free; False on timeout.
    """
    end = deadline if deadline is not None else time.monotonic() + 5.0
    while time.monotonic() < end:
        if _is_port_free(port, host=host):
            return True
        time.sleep(poll_interval)
    return _is_port_free(port, host=host)


def shutdown_processes(
    processes: Sequence[subprocess.Popen[bytes]],
    *,
    ports: Sequence[int] = (),
    graceful_timeout: float = 5.0,
    kill_timeout: float = 5.0,
    port_release_timeout: float = 5.0,
) -> None:
    """Shutdown *processes* gracefully, escalating to terminate then kill.

    After all processes exit, waits up to *port_release_timeout* for each
    *ports* value to be released (best-effort, does not raise).
    """
    # 1. Send SIGTERM to all processes
    for proc in processes:
        proc.terminate()

    # 2. Wait for graceful exit
    deadline = time.monotonic() + graceful_timeout
    for proc in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass

    # 3. Kill any stragglers
    deadline = time.monotonic() + kill_timeout
    for proc in processes:
        if proc.poll() is None:
            proc.kill()
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass

    # 4. Wait for port release (best-effort)
    for port in ports:
        wait_for_port_release(port, deadline=time.monotonic() + port_release_timeout)
