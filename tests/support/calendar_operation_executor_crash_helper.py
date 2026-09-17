"""Owned MySQL Calendar executor crash process for recovery integration tests."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from app.application.calendar_operations import CalendarOperationService
from app.config import get_settings
from app.orchestration.mysql_runtime import build_mysql_cli_runtime


async def _crash_after_commit(
    _user: Any,
    _draft: Any,
    _item: Any,
    _binding: Any,
    _result: Any,
) -> None:
    os._exit(73)


async def _crash_after_first_item(
    original: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any
) -> Any:
    result = await original(*args, **kwargs)
    os._exit(74)
    return result


async def run(mode: str, worker_id: str) -> int:
    runtime, database = await build_mysql_cli_runtime(
        get_settings(), worker_id=worker_id
    )
    service = runtime.calendar_operation_service
    if not isinstance(service, CalendarOperationService):
        raise RuntimeError("Calendar executor service is unavailable")
    if mode == "post_commit_pre_binding":
        service._commit_binding = _crash_after_commit  # type: ignore[method-assign]
    elif mode == "post_first_item":
        original = service._run_item

        async def crash_once(*args: Any, **kwargs: Any) -> Any:
            return await _crash_after_first_item(original, *args, **kwargs)

        service._run_item = crash_once  # type: ignore[method-assign]
    else:
        raise ValueError("unknown crash mode")
    await runtime.worker.run_once()
    await runtime.calendar_operation_gateway.close()
    await database.dispose()
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(
            "usage: calendar_operation_executor_crash_helper.py MODE WORKER_ID"
        )
    raise SystemExit(asyncio.run(run(sys.argv[1], sys.argv[2])))
