"""Phase 9A.1 local HTTP server cleanup tests — Task 4.

Tests verify bounded eventual port release and process cleanup for the
shared http_server helper used by Phase 8A/8B fixtures and all other
real-HTTP tests that spawn subprocess servers.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from contextlib import closing

import pytest

from tests.support.http_server import (
    _is_port_free,
    shutdown_processes,
    wait_for_port_release,
)

pytestmark = pytest.mark.phase_9a1


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestIsPortFree:
    """Low-level port check must not raise or hang."""

    def test_free_port_returns_true(self) -> None:
        port = _free_port()
        assert _is_port_free(port)

    def test_port_check_is_fast(self) -> None:
        port = _free_port()
        started = time.monotonic()
        _is_port_free(port)
        assert time.monotonic() - started < 0.5

    def test_port_check_does_not_raise(self) -> None:
        for _ in range(10):
            _is_port_free(9999)  # typically no one listening here


class TestWaitForPortRelease:
    """Bounded polling must return promptly."""

    def test_release_free_port_returns_immediately(self) -> None:
        port = _free_port()
        started = time.monotonic()
        result = wait_for_port_release(port, deadline=time.monotonic() + 1.0)
        elapsed = time.monotonic() - started
        assert result is True
        assert elapsed < 0.5  # should return near-instantly

    def test_release_times_out_on_busy_port(self) -> None:

        port = _free_port()
        # Hold the port with a bound socket (listen makes it genuinely busy)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
            # Now the port should be detected as not-free
            assert not _is_port_free(port), "sanity: port should be detected as busy"

            started = time.monotonic()
            result = wait_for_port_release(
                port, deadline=time.monotonic() + 0.3, poll_interval=0.02
            )
            elapsed = time.monotonic() - started
            assert result is False
            assert elapsed >= 0.25  # waited near the full deadline
        finally:
            listener.close()

    def test_release_reports_true_after_port_closed(self) -> None:
        port = _free_port()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)

        # Close after a short delay (simulates process exit)
        import threading

        def close_after_delay() -> None:
            time.sleep(0.1)
            listener.close()

        t = threading.Thread(target=close_after_delay, daemon=True)
        t.start()

        result = wait_for_port_release(
            port, deadline=time.monotonic() + 2.0, poll_interval=0.02
        )
        assert result is True
        t.join(timeout=1.0)


class TestShutdownProcesses:
    """Graceful shutdown escalation: terminate → kill → port release."""

    def test_no_processes_is_noop(self) -> None:
        """Calling shutdown with no processes must not raise."""
        shutdown_processes([])

    def test_already_exited_processes_is_noop(self) -> None:
        """Calling shutdown on already-exited processes must not raise."""
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.01)"],
        )
        proc.wait(timeout=5)
        shutdown_processes([proc], ports=[port])
        assert proc.poll() is not None

    def test_long_running_process_is_killed(self) -> None:
        """A process that ignores SIGTERM is eventually killed."""
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                """
import signal, time as _t
signal.signal(signal.SIGTERM, signal.SIG_IGN)
_t.sleep(30)
""",
            ],
        )
        started = time.monotonic()
        shutdown_processes([proc], graceful_timeout=0.3, kill_timeout=2.0)
        elapsed = time.monotonic() - started
        assert proc.poll() is not None  # killed
        assert elapsed < 8.0

    def test_multiple_processes_cleaned_up(self) -> None:
        """All processes in a batch are cleaned up."""
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", f"import time; time.sleep({0.05 * (i + 1)})"],
            )
            for i in range(3)
        ]
        shutdown_processes(procs, graceful_timeout=3.0)
        for proc in procs:
            assert proc.poll() is not None

    def test_port_release_after_shutdown(self) -> None:
        """A port is released after the process holding it is shut down."""
        port = _free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"""
import socket, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", {port}))
s.listen(1)
time.sleep(30)
""",
            ],
        )
        time.sleep(0.15)  # let the process bind
        assert not _is_port_free(port), "port should be in use"
        shutdown_processes([proc], ports=[port], graceful_timeout=0.3, kill_timeout=2.0)
        assert wait_for_port_release(port, deadline=time.monotonic() + 3.0), (
            "port should be released"
        )


class TestModuleExports:
    """Verify the module's public API surface."""

    def test_functions_are_callable(self) -> None:
        assert callable(shutdown_processes)
        assert callable(wait_for_port_release)
        assert callable(_is_port_free)

    def test_default_signature_parameters_are_sensible(self) -> None:
        import inspect

        sig = inspect.signature(shutdown_processes)
        params = {
            name: param.default
            for name, param in sig.parameters.items()
            if param.default is not param.empty
        }
        assert params["graceful_timeout"] == 5.0
        assert params["kill_timeout"] == 5.0
        assert params["port_release_timeout"] == 5.0
