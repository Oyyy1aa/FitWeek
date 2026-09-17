"""Independent MySQL orchestration Worker/Reaper command line."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from app.config import PersistenceBackend, get_settings
from app.orchestration.mysql_runtime import build_mysql_cli_runtime


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.orchestration.cli")
    commands = parser.add_subparsers(dest="role", required=True)
    worker = commands.add_parser("worker")
    mode = worker.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    worker.add_argument("--worker-id", required=True)
    reaper = commands.add_parser("reaper")
    reaper.add_argument("--once", action="store_true", required=True)
    return parser


async def _run(arguments: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.persistence_backend is not PersistenceBackend.MYSQL:
        raise RuntimeError("CLI requires the MySQL persistence backend.")
    if not settings.orchestrator_enabled:
        raise RuntimeError("CLI requires orchestration to be enabled.")
    worker_id = getattr(arguments, "worker_id", "reaper")
    runtime, database = await build_mysql_cli_runtime(settings, worker_id=worker_id)
    try:
        if arguments.role == "reaper":
            await runtime.reaper.run_once()
            return 0
        if arguments.once:
            result = await runtime.worker.run_once()
            print(result.outcome)
            return 0
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        graceful_signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGBREAK"):
            graceful_signals.append(signal.SIGBREAK)
        for signum in graceful_signals:
            try:
                loop.add_signal_handler(signum, stop.set)
            except NotImplementedError:
                signal.signal(signum, lambda *_args: stop.set())
        await runtime.worker.run_loop(stop)
        return 0
    finally:
        calendar_gateway = runtime.calendar_operation_gateway
        if calendar_gateway is not None:
            await calendar_gateway.close()
        schedule_calendar = runtime.schedule_calendar_gateway
        if schedule_calendar is not None:
            await schedule_calendar.close()
        schedule_gateway = runtime.schedule_model_gateway
        if schedule_gateway is not None:
            await schedule_gateway.close()
        session_design_gateway = runtime.session_design_model_gateway
        if session_design_gateway is not None:
            await session_design_gateway.close()
        profile_gateway = runtime.profile_agent_model_gateway
        if profile_gateway is not None:
            await profile_gateway.close()
        recovery_gateway = getattr(runtime, "recovery_draft_model_gateway", None)
        if recovery_gateway is not None:
            await recovery_gateway.close()
        await database.dispose()


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(arguments))
    except Exception as exc:
        print(
            f"orchestration CLI startup failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
