"""Reconciliation, idempotency, and partial recovery contracts."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest

from app.application.calendar_operations import (
    CalendarOperationService,
    CreateCalendarOperationCommand,
)
from app.calendar_operations.gateway import CalendarWriteGateway
from app.calendar_operations.http_provider import HttpCalendarWriteProvider
from app.calendar_operations.payload_policy import CalendarPayloadPolicy
from app.calendar_operations.reconciliation import CalendarReconciliationPolicy
from app.calendar_operations.scripted_provider import ScriptedCalendarWriteProvider
from app.domain.calendar_operations.enums import (
    CalendarBindingStatus,
    CalendarOperationDraftStatus,
    CalendarOperationItemStatus,
    CalendarOperationType,
)
from app.domain.calendar_operations.models import CalendarEventBinding
from app.domain.plans.models import WeeklyPlanStatus
from app.orchestration.clock import FakeClock
from app.persistence.memory import (
    InMemoryCalendarOperationRepository,
    InMemoryPlanRepository,
    InMemoryStore,
)
from app.tool_gateway.factory import build_tool_gateway
from tests.factories import TEST_NOW, make_plan, make_user
from tests.stub_calendar_server.app import app as calendar_stub

pytestmark = pytest.mark.phase_6b


def _binding(plan, session, fingerprint: str) -> CalendarEventBinding:
    return CalendarEventBinding(
        id=uuid4(),
        user_id=plan.user_id,
        provider="scripted",
        calendar_id="primary",
        root_plan_id=plan.series_id,
        session_id=session.id,
        external_event_id=f"event-{session.id}",
        stable_uid=f"uid-{session.id}",
        last_payload_fingerprint=fingerprint,
        status=CalendarBindingStatus.ACTIVE,
        created_at=TEST_NOW,
        updated_at=TEST_NOW,
        version=1,
    )


def test_reconciliation_emits_create_keep_update_delete_without_side_effects() -> None:
    plan = make_plan(status=WeeklyPlanStatus.CONFIRMED)
    payloads = CalendarPayloadPolicy()
    first, second = plan.sessions
    first_payload = payloads.build(
        user_id=plan.user_id, plan=plan, session=first, timezone="UTC"
    )
    keep = _binding(plan, first, first_payload.payload_fingerprint)
    stale = _binding(plan, second, "0" * 64)
    orphan = replace(_binding(plan, second, "1" * 64), session_id=uuid4())
    policy = CalendarReconciliationPolicy(payloads)
    items = policy.build_items(
        user_id=plan.user_id,
        plan=plan,
        provider="scripted",
        calendar_id="primary",
        timezone="UTC",
        bindings=(keep, stale, orphan),
        now=TEST_NOW,
        draft_seed="draft",
    )
    by_session = {item.session_id: item for item in items}
    assert by_session[first.id].operation_type is CalendarOperationType.KEEP
    assert by_session[first.id].status is CalendarOperationItemStatus.SKIPPED
    assert by_session[second.id].operation_type is CalendarOperationType.UPDATE
    assert by_session[orphan.session_id].operation_type is CalendarOperationType.DELETE
    assert tuple(item.session_id for item in items) == tuple(
        item.session_id
        for item in sorted(
            items,
            key=lambda value: (
                value.payload.start
                if value.payload is not None
                else plan.sessions[-1].scheduled_end,
                str(value.session_id),
            ),
        )
    )


@pytest.mark.phase_8a
async def test_scripted_provider_recovers_response_loss_without_duplicate_event() -> (
    None
):
    plan = make_plan(status=WeeklyPlanStatus.CONFIRMED)
    payload = CalendarPayloadPolicy().build(
        user_id=plan.user_id, plan=plan, session=plan.sessions[0], timezone="UTC"
    )
    provider = ScriptedCalendarWriteProvider()
    provider.queue_response_loss_after_create()
    gateway = CalendarWriteGateway(
        provider=provider, enabled=True, timeout_seconds=1, max_attempts=2
    )
    result = await gateway.create(
        calendar_id="primary", operation_key="stable-operation", payload=payload
    )
    assert result.succeeded
    assert len(result.attempts) == 2
    assert len(provider.events) == 1
    repeated = await gateway.create(
        calendar_id="primary", operation_key="stable-operation", payload=payload
    )
    assert repeated.succeeded
    assert len(provider.events) == 1
    deleted = await gateway.delete(
        calendar_id="primary",
        external_event_id="already-missing",
        operation_key="delete-missing",
    )
    assert deleted.succeeded


@pytest.mark.phase_8a
async def test_partial_execution_retries_only_failed_item() -> None:
    user = make_user()
    plan = make_plan(user_id=user.id, status=WeeklyPlanStatus.CONFIRMED)
    store = InMemoryStore()
    plans = InMemoryPlanRepository(store)
    operations = InMemoryCalendarOperationRepository(store)
    await plans.save(plan)
    provider = ScriptedCalendarWriteProvider()
    provider.queue_failure("CALENDAR_PROVIDER_HTTP_500", retryable=True)
    service = CalendarOperationService(
        plans=plans,
        operations=operations,
        gateway=CalendarWriteGateway(
            provider=provider, enabled=True, timeout_seconds=1, max_attempts=1
        ),
        clock=FakeClock(TEST_NOW - timedelta(days=1)),
        max_attempts=3,
    )
    draft, created = await service.create_draft(
        user,
        plan.series_id,
        plan.revision,
        CreateCalendarOperationCommand(
            client_request_id="partial",
            expected_plan_version=plan.version,
            provider="scripted",
            calendar_id="primary",
        ),
    )
    assert created
    draft = await service.approve(user, draft.id, draft.version)
    first = await service.execute(user, draft.id)
    assert first.status is CalendarOperationDraftStatus.PARTIALLY_SUCCEEDED
    succeeded = {
        item.id
        for item in first.items
        if item.status is CalendarOperationItemStatus.SUCCEEDED
    }
    failed = {
        item.id
        for item in first.items
        if item.status is CalendarOperationItemStatus.FAILED_RETRYABLE
    }
    assert len(succeeded) == len(failed) == 1
    calls_before = tuple(provider.calls)
    final = await service.execute(user, draft.id)
    assert final.status is CalendarOperationDraftStatus.SUCCEEDED
    assert len(provider.events) == 2
    assert len(provider.calls) == len(calls_before) + 1
    assert next(item for item in final.items if item.id in succeeded).attempt_count == 1


async def _executing_draft_resumes_after_post_commit_crash() -> None:
    class CrashOnceAfterCreateProvider(ScriptedCalendarWriteProvider):
        def __init__(self) -> None:
            super().__init__()
            self.crashed = False

        async def create_event(self, **kwargs):  # type: ignore[no-untyped-def]
            result = await super().create_event(**kwargs)
            if not self.crashed:
                self.crashed = True
                raise BaseException("test-only post-commit crash")
            return result

    user = make_user()
    plan = make_plan(user_id=user.id, status=WeeklyPlanStatus.CONFIRMED)
    store = InMemoryStore()
    plans = InMemoryPlanRepository(store)
    operations = InMemoryCalendarOperationRepository(store)
    await plans.save(plan)
    provider = CrashOnceAfterCreateProvider()
    service = CalendarOperationService(
        plans=plans,
        operations=operations,
        gateway=CalendarWriteGateway(
            provider=provider, enabled=True, timeout_seconds=1, max_attempts=1
        ),
        clock=FakeClock(TEST_NOW),
    )
    draft, _ = await service.create_draft(
        user,
        plan.series_id,
        plan.revision,
        CreateCalendarOperationCommand(
            client_request_id="post-commit-crash",
            expected_plan_version=plan.version,
            provider="scripted",
            calendar_id="primary",
        ),
    )
    approved = await service.approve(user, draft.id, draft.version)

    with pytest.raises(BaseException, match="post-commit crash"):
        await service.execute(user, approved.id)

    interrupted = await service.get_draft(user, approved.id)
    assert interrupted.status is CalendarOperationDraftStatus.EXECUTING
    assert interrupted.items[0].status is CalendarOperationItemStatus.RUNNING

    final = await service.execute(user, approved.id)

    assert final.status is CalendarOperationDraftStatus.SUCCEEDED
    assert len(provider.events) == len(plan.sessions)
    assert provider.calls[0] == provider.calls[1]
    assert await service.bindings(
        user,
        provider="scripted",
        calendar_id="primary",
        root_plan_id=plan.series_id,
    )


test_executing_draft_resumes_after_post_commit_crash_without_duplicate_event = (
    _executing_draft_resumes_after_post_commit_crash
)


async def test_circuit_open_admission_does_not_consume_calendar_business_attempt_budget(  # noqa: E501
) -> None:
    user = make_user()
    plan = make_plan(user_id=user.id, status=WeeklyPlanStatus.CONFIRMED)
    store = InMemoryStore()
    plans = InMemoryPlanRepository(store)
    operations = InMemoryCalendarOperationRepository(store)
    await plans.save(plan)
    clock = FakeClock(TEST_NOW)
    provider = ScriptedCalendarWriteProvider()
    provider.queue_failure("CALENDAR_PROVIDER_HTTP_500", retryable=True)
    gateway = CalendarWriteGateway(
        provider=provider,
        enabled=True,
        timeout_seconds=1,
        max_attempts=1,
        tool_gateway=build_tool_gateway(
            calendar_write_provider=provider,
            calendar_write_timeout_ms=1000,
            calendar_write_attempts=1,
            circuit_failure_threshold=1,
            circuit_open_duration_seconds=30,
            clock=clock,
        ),
        user_id=user.id,
    )
    service = CalendarOperationService(
        plans=plans,
        operations=operations,
        gateway=gateway,
        clock=clock,
        max_attempts=3,
    )
    draft, _ = await service.create_draft(
        user,
        plan.series_id,
        plan.revision,
        CreateCalendarOperationCommand(
            client_request_id="circuit-open-budget",
            expected_plan_version=plan.version,
            provider="scripted",
            calendar_id="primary",
        ),
    )
    approved = await service.approve(user, draft.id, draft.version)
    first = await service.execute(user, approved.id)
    assert first.status is CalendarOperationDraftStatus.PARTIALLY_SUCCEEDED
    assert sorted(item.attempt_count for item in first.items) == [0, 1]
    calls_after_failure = tuple(provider.calls)
    second = await service.execute(user, first.id)
    assert second.status is CalendarOperationDraftStatus.PARTIALLY_SUCCEEDED
    assert sorted(item.attempt_count for item in second.items) == [0, 1]
    assert tuple(provider.calls) == calls_after_failure
    assert await operations.list_attempts(user.id, second.id)
    assert (
        await operations.list_bindings(user.id, "scripted", "primary", plan.series_id)
        == ()
    )
    clock.advance(timedelta(seconds=30))
    final = await service.execute(user, second.id)
    assert final.status is CalendarOperationDraftStatus.SUCCEEDED
    assert len(provider.events) == len(plan.sessions)


async def test_http_write_provider_contract_and_delete_missing_are_idempotent() -> None:
    transport = httpx.ASGITransport(app=calendar_stub)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://calendar"
    ) as admin:
        await admin.post("/admin/reset")
        provider = HttpCalendarWriteProvider(
            base_url="http://calendar",
            api_key="local-calendar-write-test-key",
            timeout_seconds=1,
            max_response_bytes=4096,
            client=admin,
        )
        plan = make_plan(status=WeeklyPlanStatus.CONFIRMED)
        payload = CalendarPayloadPolicy().build(
            user_id=plan.user_id,
            plan=plan,
            session=plan.sessions[0],
            timezone="UTC",
        )
        created = await provider.create_event(
            calendar_id="primary", operation_key="http-key", payload=payload
        )
        reused = await provider.create_event(
            calendar_id="primary", operation_key="http-key", payload=payload
        )
        assert created.external_event_id == reused.external_event_id
        assert (await admin.get("/admin/events")).json()["event_count"] == 1
        assert created.external_event_id is not None
        await provider.delete_event(
            calendar_id="primary",
            external_event_id=created.external_event_id,
            operation_key="http-delete",
        )
        missing = await provider.delete_event(
            calendar_id="primary",
            external_event_id=created.external_event_id,
            operation_key="http-delete-again",
        )
        assert missing.external_event_id == created.external_event_id


@pytest.mark.parametrize(
    ("status_code", "expected_success", "expected_attempts", "expected_retryable"),
    (
        (429, True, 2, False),
        (500, True, 2, False),
        (401, False, 1, False),
        (403, False, 1, False),
    ),
)
@pytest.mark.phase_8a
async def test_http_gateway_classifies_retryable_and_permanent_statuses(
    status_code: int,
    expected_success: bool,
    expected_attempts: int,
    expected_retryable: bool,
) -> None:
    transport = httpx.ASGITransport(app=calendar_stub)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://calendar"
    ) as admin:
        await admin.post("/admin/reset")
        await admin.post(
            "/admin/fail-next",
            json={"status_code": status_code, "after_commit": False, "count": 1},
        )
        provider = HttpCalendarWriteProvider(
            base_url="http://calendar",
            api_key="local-calendar-write-test-key",
            timeout_seconds=1,
            max_response_bytes=4096,
            client=admin,
        )
        plan = make_plan(status=WeeklyPlanStatus.CONFIRMED)
        payload = CalendarPayloadPolicy().build(
            user_id=plan.user_id,
            plan=plan,
            session=plan.sessions[0],
            timezone="UTC",
        )
        result = await CalendarWriteGateway(
            provider=provider,
            enabled=True,
            timeout_seconds=1,
            max_attempts=2,
        ).create(
            calendar_id="primary",
            operation_key=f"status-{status_code}",
            payload=payload,
        )
        assert result.succeeded is expected_success
        assert len(result.attempts) == expected_attempts
        assert result.retryable is expected_retryable
        assert (await admin.get("/admin/events")).json()["event_count"] == (
            1 if expected_success else 0
        )


async def test_gateway_timeout_does_not_start_an_attempt_after_deadline() -> None:
    class SlowProvider(ScriptedCalendarWriteProvider):
        async def create_event(self, **kwargs):  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.05)
            return await super().create_event(**kwargs)

    plan = make_plan(status=WeeklyPlanStatus.CONFIRMED)
    payload = CalendarPayloadPolicy().build(
        user_id=plan.user_id,
        plan=plan,
        session=plan.sessions[0],
        timezone="UTC",
    )
    result = await CalendarWriteGateway(
        provider=SlowProvider(),
        enabled=True,
        timeout_seconds=0.001,
        max_attempts=2,
    ).create(
        calendar_id="primary",
        operation_key="bounded-timeout",
        payload=payload,
    )
    assert not result.succeeded
    # The total deadline can expire before the first attempt on a busy runner;
    # either outcome proves no follow-up attempt was started after expiry.
    assert len(result.attempts) <= 1
    if result.attempts:
        assert result.retryable
        assert {item.error_code for item in result.attempts} == {
            "TOOL_DEADLINE_EXCEEDED"
        }
