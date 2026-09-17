"""Subprocess helpers for MySQL orchestration recovery integration tests."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import timedelta

from httpx import AsyncClient, HTTPError

from app.config import get_settings
from app.orchestration.clock import SystemClock
from app.persistence.database import get_database
from app.persistence.mysql.orchestration_repository import MySQLOrchestrationRepository
from app.persistence.mysql.user_repository import MySQLUserAccountRepository


@dataclass(frozen=True, slots=True)
class ApiProcess:
    process: asyncio.subprocess.Process
    port: int

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def reserve_unreachable_loopback_port() -> int:
    """Reserve then release one port for a guaranteed-unreachable child endpoint."""

    return _reserve_loopback_port()


async def _communicate_bounded(
    process: asyncio.subprocess.Process, *, timeout_seconds: float
) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        process.terminate()
        try:
            return await asyncio.wait_for(process.communicate(), timeout=5)
        except TimeoutError:
            process.kill()
            return await process.communicate()


async def start_api_process(environment: dict[str, str]) -> ApiProcess:
    """Start a real Uvicorn API process on one fresh loopback port."""

    port = _reserve_loopback_port()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    api = ApiProcess(process=process, port=port)
    try:
        async with AsyncClient(base_url=api.base_url) as client:
            for _ in range(100):
                if process.returncode is not None:
                    raise RuntimeError("owned API process exited before readiness")
                try:
                    response = await client.get("/health/ready", timeout=0.25)
                    if response.status_code == 200:
                        return api
                except HTTPError:
                    pass
                await asyncio.sleep(0.05)
    except BaseException:
        await stop_api_process(api)
        raise
    await stop_api_process(api)
    raise RuntimeError("owned API process did not become ready within five seconds")


async def start_profile_model_stub_process() -> ApiProcess:
    """Start the local test-only Profile model Stub on a bounded loopback port."""

    port = _reserve_loopback_port()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "uvicorn",
        "tests.stub_model_server.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stub = ApiProcess(process=process, port=port)
    try:
        async with AsyncClient(base_url=stub.base_url) as client:
            for _ in range(100):
                if process.returncode is not None:
                    raise RuntimeError(
                        "owned Profile model Stub exited before readiness"
                    )
                try:
                    response = await client.get("/openapi.json", timeout=0.25)
                    if response.status_code == 200:
                        return stub
                except HTTPError:
                    pass
                await asyncio.sleep(0.05)
    except BaseException:
        await stop_api_process(stub)
        raise
    await stop_api_process(stub)
    raise RuntimeError(
        "owned Profile model Stub did not become ready within five seconds"
    )


async def stop_api_process(api: ApiProcess) -> None:
    """Stop only the API process created by this harness and release its port."""

    if api.process.returncode is None:
        api.process.terminate()
    await _communicate_bounded(api.process, timeout_seconds=10)
    for _ in range(20):
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", api.port)
        except OSError:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            # Windows can reset the probe while the terminated listener releases
            # its socket; that reset itself proves no owned server accepted it.
            return
        await asyncio.sleep(0.05)
    raise RuntimeError("owned API process port remained open after shutdown")


async def run_worker_once(
    environment: dict[str, str], worker_id: str
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.orchestration.cli",
        "worker",
        "--once",
        "--worker-id",
        worker_id,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate_bounded(process, timeout_seconds=30)
    return process.returncode or 0, stdout.decode(), stderr.decode()


async def start_worker_once_process(
    environment: dict[str, str], worker_id: str
) -> asyncio.subprocess.Process:
    """Start one production Worker CLI process without awaiting its result."""

    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.orchestration.cli",
        "worker",
        "--once",
        "--worker-id",
        worker_id,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def wait_for_worker_process(
    process: asyncio.subprocess.Process,
) -> tuple[int, str, str]:
    """Collect a bounded Worker CLI result created by this harness."""

    stdout, stderr = await _communicate_bounded(process, timeout_seconds=30)
    return process.returncode or 0, stdout.decode(), stderr.decode()


async def start_worker_loop_process(
    environment: dict[str, str], worker_id: str
) -> asyncio.subprocess.Process:
    """Start a production loop Worker in a signal-addressable process group."""

    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.orchestration.cli",
        "worker",
        "--loop",
        "--worker-id",
        worker_id,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **options,
    )


async def wait_for_worker_loop_start(
    process: asyncio.subprocess.Process, *, timeout_seconds: float = 5
) -> None:
    """Boundedly ensure the owned loop process remains alive after startup."""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None:
            raise RuntimeError("owned loop Worker exited during runtime startup")
        await asyncio.sleep(0.05)


async def signal_worker_loop_gracefully(
    process: asyncio.subprocess.Process,
) -> tuple[int, str, str]:
    """Send the platform's graceful signal and fail rather than force-kill."""

    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.send_signal(signal.SIGTERM)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except TimeoutError as exc:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        raise RuntimeError(
            "owned loop Worker did not exit after graceful signal"
        ) from exc
    return process.returncode or 0, stdout.decode(), stderr.decode()


async def stop_worker_process_for_cleanup(
    process: asyncio.subprocess.Process,
) -> None:
    """Boundedly terminate only an owned process after an assertion failure."""

    if process.returncode is None:
        process.terminate()
    await _communicate_bounded(process, timeout_seconds=10)


async def run_reaper_once(environment: dict[str, str]) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.orchestration.cli",
        "reaper",
        "--once",
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate_bounded(process, timeout_seconds=30)
    return process.returncode or 0, stdout.decode(), stderr.decode()


async def run_claim_then_exit(
    environment: dict[str, str], worker_id: str
) -> tuple[int, str, str]:
    """Commit one real claim in a child process, then exit before execution."""

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "tests/support/mysql_orchestrator_process.py",
        "claim-then-exit",
        worker_id,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate_bounded(process, timeout_seconds=30)
    return process.returncode or 0, stdout.decode(), stderr.decode()


async def run_profile_apply_then_exit(
    environment: dict[str, str], worker_id: str
) -> tuple[int, str, str]:
    """Commit the real Profile apply business transaction, then exit pre-checkpoint."""

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "tests/support/profile_draft_apply_crash_helper.py",
        worker_id,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate_bounded(process, timeout_seconds=30)
    return process.returncode or 0, stdout.decode(), stderr.decode()


async def run_calendar_executor_crash(
    environment: dict[str, str], mode: str, worker_id: str
) -> tuple[int, str, str]:
    """Run one owned Calendar executor process that exits at a real crash boundary."""

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "tests/support/calendar_operation_executor_crash_helper.py",
        mode,
        worker_id,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate_bounded(process, timeout_seconds=30)
    return process.returncode or 0, stdout.decode(), stderr.decode()


def cli_environment(*, database_url: str, email: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PERSISTENCE_BACKEND": "mysql",
            "DATABASE_URL": database_url,
            "ORCHESTRATOR_ENABLED": "true",
            "REDIS_ENABLED": "false",
            "SINGLE_USER_EMAIL": email,
        }
    )
    return environment


async def claim_then_exit(worker_id: str) -> int:
    database = get_database()
    try:
        user = await MySQLUserAccountRepository(database.session_factory).get_by_email(
            get_settings().single_user_email
        )
        if user is None:
            return 1
        repository = MySQLOrchestrationRepository(
            database.session_factory,
            execution_user_id=user.id,
        )
        claim = await repository.claim_next_step(
            worker_id=worker_id,
            lease_duration=timedelta(seconds=10),
            now=SystemClock().now(),
        )
        if claim is None:
            return 1
        print(
            json.dumps(
                {
                    "run_id": str(claim.run.id),
                    "step_id": str(claim.step.id),
                    "fencing_token": claim.fencing_token,
                }
            )
        )
        return 0
    finally:
        await database.dispose()


if __name__ == "__main__":
    if sys.argv[1:] and sys.argv[1] == "claim-then-exit":
        raise SystemExit(
            asyncio.run(
                claim_then_exit(sys.argv[2] if len(sys.argv) > 2 else "crash-worker")
            )
        )
    raise SystemExit("usage: mysql_orchestrator_process.py claim-then-exit [worker-id]")
