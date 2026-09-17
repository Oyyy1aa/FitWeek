"""Formal MySQL Recovery Draft API composition and lifecycle contracts."""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import closing
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import bindparam, func, select, text

import app.api.dependencies as dependencies_module
import app.main as main_module
from app.application.errors import UnsupportedPersistenceBackend
from app.application.recovery_drafts import RecoveryDraftService
from app.behavior.metrics import RecoveryMetrics
from app.config import PersistenceBackend, Settings
from app.domain.tools.enums import ToolCaller, ToolId
from app.model_gateway.gateway import ModelGateway
from app.persistence.database import Database
from app.persistence.mysql.models import (
    CalendarEventBindingModel,
    ContextSnapshotModel,
    MemoryCandidateModel,
    MemoryEvidenceModel,
    MemoryItemModel,
    RecoveryActionCandidateModel,
    RecoveryBehaviorSummaryModel,
    RecoveryCandidateSetModel,
    RecoveryChangeImpactModel,
    RecoveryDraftModel,
    RecoveryMemoryProposalModel,
    RecoveryTraceModel,
    SessionCheckinModel,
    SessionExerciseModel,
    WeeklyPlanModel,
    WorkoutSessionModel,
)
from app.persistence.mysql.recovery_draft_repository import (
    MySQLRecoveryDraftRepository,
)
from app.tool_gateway.gateway import ToolGateway
from tests.integration.test_mysql_session_design_draft_runtime import (
    _cleanup as _cleanup_business_data,
)
from tests.integration.test_mysql_session_design_draft_runtime import (
    _generation_payload,
)

RECOVERY_TABLES = (
    "recovery_trace",
    "recovery_memory_proposal",
    "recovery_draft",
    "recovery_action_candidate",
    "recovery_candidate_set",
    "recovery_change_impact",
    "recovery_behavior_summary",
)


def _free_port() -> int:
    with closing(socket.socket()) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _port_is_free(port: int) -> bool:
    with closing(socket.socket()) as candidate:
        candidate.settimeout(0.2)
        return candidate.connect_ex(("127.0.0.1", port)) != 0


def _stop_owned_process(process: subprocess.Popen[object], port: int) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=6)
    assert process.poll() is not None
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if _port_is_free(port):
            return
        time.sleep(0.03)
    pytest.fail("owned model stub port was not released")


@pytest.fixture
def recovery_model_stub_url() -> Iterator[str]:
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.stub_model_server.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("owned Recovery model stub exited during startup")
        try:
            if httpx.get(f"{base_url}/openapi.json", timeout=0.25).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.03)
    else:
        _stop_owned_process(process, port)
        pytest.fail("owned Recovery model stub did not start")
    try:
        yield base_url
    finally:
        _stop_owned_process(process, port)


def _settings(
    mysql_test_url: str,
    email: str,
    *,
    model_stub_url: str | None = None,
) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "persistence_backend": PersistenceBackend.MYSQL,
        "database_url": SecretStr(mysql_test_url),
        "redis_enabled": False,
        "orchestrator_enabled": False,
        "single_user_email": email,
        "_env_file": None,
    }
    if model_stub_url is None:
        values.update(
            {
                "model_gateway_enabled": False,
                "recovery_agent_enabled": False,
            }
        )
    else:
        values.update(
            {
                "model_gateway_enabled": True,
                "recovery_agent_enabled": True,
                "model_primary_provider": "http",
                "model_primary_base_url": SecretStr(model_stub_url),
                "model_primary_api_key": SecretStr("local-recovery-runtime-key"),
                "model_primary_model": "controlled-recovery-runtime",
                "model_backup_provider": "template-fallback",
                "model_max_attempts": 1,
            }
        )
    return Settings(**values)


def _profile_payload() -> dict[str, object]:
    return {
        "experience_level": "BEGINNER",
        "weekly_frequency": 2,
        "max_session_minutes": 45,
        "primary_goal": "GENERAL_FITNESS",
        "scope_confirmed": True,
    }


def _recovery_payload(
    token: str,
    plan: dict[str, Any],
    *,
    client_request_id: str | None = None,
    message: str = "Please reschedule this future training session.",
) -> dict[str, object]:
    return {
        "client_request_id": client_request_id or f"recovery-runtime-{token}",
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
        "request_type": "RESCHEDULE_REQUEST",
        "target_session_ids": [plan["sessions"][0]["id"]],
        "user_request": message,
    }


async def _create_confirmed_plan(client: AsyncClient, token: str) -> dict[str, Any]:
    profile = await client.put("/api/v1/profiles/me", json=_profile_payload())
    assert profile.status_code == 200, profile.text
    generated = await client.post(
        "/api/v1/plans/generate", json=_generation_payload(token)
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()["plan"]
    confirmed = await client.post(
        f"/api/v1/plans/{plan['id']}/confirm",
        json={"expected_version": plan["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


async def _cleanup_recovery(database: Database, user_ids: tuple[UUID, ...]) -> None:
    if not user_ids:
        return
    owners = [str(value) for value in user_ids]
    async with database.session_factory() as session:
        async with session.begin():
            for table in RECOVERY_TABLES:
                await session.execute(
                    text(f"DELETE FROM {table} WHERE user_id IN :owners").bindparams(
                        bindparam("owners", expanding=True)
                    ),
                    {"owners": owners},
                )
    for user_id in user_ids:
        await _cleanup_business_data(database, user_id)


async def _recovery_counts(database: Database, user_id: UUID) -> dict[str, int]:
    owner = str(user_id)
    models = {
        "summary": RecoveryBehaviorSummaryModel,
        "impact": RecoveryChangeImpactModel,
        "candidate_set": RecoveryCandidateSetModel,
        "candidates": RecoveryActionCandidateModel,
        "draft": RecoveryDraftModel,
        "proposals": RecoveryMemoryProposalModel,
        "trace": RecoveryTraceModel,
    }
    async with database.session_factory() as session:
        return {
            name: int(
                await session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.user_id == owner)
                )
                or 0
            )
            for name, model in models.items()
        }


def _row_values(row: object, model: type[object]) -> tuple[object, ...]:
    table = model.__table__  # type: ignore[attr-defined]
    return tuple(getattr(row, column.name) for column in table.columns)


async def _history_snapshot(
    database: Database, user_id: UUID
) -> dict[str, tuple[tuple[object, ...], ...]]:
    owner = str(user_id)
    async with database.session_factory() as session:
        plans = (
            await session.scalars(
                select(WeeklyPlanModel)
                .where(WeeklyPlanModel.user_id == owner)
                .order_by(WeeklyPlanModel.id)
            )
        ).all()
        sessions = (
            await session.scalars(
                select(WorkoutSessionModel)
                .join(
                    WeeklyPlanModel,
                    WorkoutSessionModel.plan_id == WeeklyPlanModel.id,
                )
                .where(WeeklyPlanModel.user_id == owner)
                .order_by(WorkoutSessionModel.id)
            )
        ).all()
        exercises = (
            await session.scalars(
                select(SessionExerciseModel)
                .join(
                    WorkoutSessionModel,
                    SessionExerciseModel.session_id == WorkoutSessionModel.id,
                )
                .join(
                    WeeklyPlanModel,
                    WorkoutSessionModel.plan_id == WeeklyPlanModel.id,
                )
                .where(WeeklyPlanModel.user_id == owner)
                .order_by(SessionExerciseModel.id)
            )
        ).all()

        async def owned_rows(model: type[Any]) -> tuple[object, ...]:
            return tuple(
                (
                    await session.scalars(
                        select(model).where(model.user_id == owner).order_by(model.id)
                    )
                ).all()
            )

        memories = await owned_rows(MemoryItemModel)
        memory_candidates = await owned_rows(MemoryCandidateModel)
        memory_ids = [item.id for item in memories]
        memory_evidence = (
            tuple(
                (
                    await session.scalars(
                        select(MemoryEvidenceModel)
                        .where(MemoryEvidenceModel.memory_id.in_(memory_ids))
                        .order_by(MemoryEvidenceModel.id)
                    )
                ).all()
            )
            if memory_ids
            else ()
        )
        checks = await owned_rows(SessionCheckinModel)
        contexts = await owned_rows(ContextSnapshotModel)
        bindings = await owned_rows(CalendarEventBindingModel)
        return {
            "plans": tuple(_row_values(item, WeeklyPlanModel) for item in plans),
            "sessions": tuple(
                _row_values(item, WorkoutSessionModel) for item in sessions
            ),
            "session_exercises": tuple(
                _row_values(item, SessionExerciseModel) for item in exercises
            ),
            "checkins": tuple(
                _row_values(item, SessionCheckinModel) for item in checks
            ),
            "memories": tuple(_row_values(item, MemoryItemModel) for item in memories),
            "memory_candidates": tuple(
                _row_values(item, MemoryCandidateModel) for item in memory_candidates
            ),
            "memory_evidence": tuple(
                _row_values(item, MemoryEvidenceModel) for item in memory_evidence
            ),
            "contexts": tuple(
                _row_values(item, ContextSnapshotModel) for item in contexts
            ),
            "calendar_bindings": tuple(
                _row_values(item, CalendarEventBindingModel) for item in bindings
            ),
        }


def _assert_unsupported(response: httpx.Response) -> None:
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "UNSUPPORTED_PERSISTENCE_BACKEND"
    assert "traceback" not in response.text.casefold()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_recovery_lifespan_composes_durable_services_without_memory_fallback(  # noqa: E501
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = getattr(dependencies_module, "build_mysql_recovery_draft_services", None)
    result_type = getattr(dependencies_module, "MySQLRecoveryDraftServices", None)
    assert callable(factory), "MySQL Recovery Draft composition factory is absent"
    assert result_type is not None, "typed MySQL Recovery Draft services are absent"

    token = uuid4().hex
    settings = _settings(mysql_test_url, f"recovery-lifespan-{token}@fitweek.test")
    close_count = 0
    detached_during_close = False
    captured: dict[str, Any] = {}
    application = main_module.create_application()
    real_factory = factory

    def counted_factory(*args: object, **kwargs: object) -> object:
        services = real_factory(*args, **kwargs)
        captured["services"] = services
        original_close = services.model_gateway.close

        async def counted_close() -> None:
            nonlocal close_count, detached_during_close
            close_count += 1
            detached_during_close = all(
                getattr(application.state, name, None) is None
                for name in (
                    "recovery_draft_repository",
                    "recovery_draft_service",
                    "recovery_draft_model_gateway",
                    "recovery_draft_tool_gateway",
                    "recovery_metrics",
                )
            )
            await original_close()

        services.model_gateway.close = counted_close
        return services

    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main_module, "build_mysql_recovery_draft_services", counted_factory
    )
    user_id: UUID | None = None
    try:
        async with application.router.lifespan_context(application):
            user_id = application.state.local_user.id
            services = captured["services"]
            assert isinstance(services, result_type)
            assert application.state.business_container is None
            assert isinstance(
                application.state.recovery_draft_repository,
                MySQLRecoveryDraftRepository,
            )
            assert isinstance(
                application.state.recovery_draft_service, RecoveryDraftService
            )
            assert isinstance(
                application.state.recovery_draft_model_gateway, ModelGateway
            )
            assert isinstance(
                application.state.recovery_draft_tool_gateway, ToolGateway
            )
            assert isinstance(application.state.recovery_metrics, RecoveryMetrics)
            assert application.state.recovery_draft_repository is services.repository
            assert application.state.recovery_draft_service is services.service
            assert (
                application.state.recovery_draft_model_gateway is services.model_gateway
            )
            assert (
                application.state.recovery_draft_tool_gateway is services.tool_gateway
            )
            assert application.state.recovery_metrics is services.metrics
            registration = services.tool_gateway.registry.get(
                ToolId.RECOVERY_SPACING_VALIDATOR.value, "phase-8a-v1"
            )
            assert registration.descriptor.allowed_callers == frozenset(
                {ToolCaller.RECOVERY_APPLICATION}
            )
            assert not hasattr(services.tool_gateway, "close")
            request = SimpleNamespace(app=application)
            assert dependencies_module.get_recovery_draft_service(request) is (
                services.service
            )
            application.state.recovery_draft_service = None
            with pytest.raises(UnsupportedPersistenceBackend):
                dependencies_module.get_recovery_draft_service(request)
            application.state.recovery_draft_service = services.service

            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://test"
            ) as client:
                missing_result = await client.get(
                    f"/api/v1/recovery-drafts/{uuid4()}/application-result"
                )
                assert missing_result.status_code == 404, missing_result.text
                missing_preview = await client.post(
                    f"/api/v1/recovery-drafts/{uuid4()}/memory-proposals/preview",
                    json={"selected_proposal_ids": [str(uuid4())]},
                )
                assert missing_preview.status_code == 404, missing_preview.text
                assert (
                    missing_preview.json()["error"]["code"]
                    == "RECOVERY_DRAFT_NOT_FOUND"
                )
                missing_run = await client.get(
                    f"/api/v1/recovery-application-runs/{uuid4()}"
                )
                assert missing_run.status_code == 404, missing_run.text
                assert missing_run.json()["error"]["code"] == "RUN_NOT_FOUND"
        assert close_count == 1
        assert detached_during_close is True
        assert all(
            getattr(application.state, name, None) is None
            for name in (
                "recovery_draft_repository",
                "recovery_draft_service",
                "recovery_draft_model_gateway",
                "recovery_draft_tool_gateway",
                "recovery_metrics",
            )
        )
    finally:
        if user_id is not None:
            await _cleanup_recovery(mysql_test_database, (user_id,))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_recovery_api_create_artifacts_idempotency_review_and_user_isolation(  # noqa: E501
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
    recovery_model_stub_url: str,
) -> None:
    assert callable(
        getattr(dependencies_module, "build_mysql_recovery_draft_services", None)
    ), "MySQL Recovery Draft runtime is absent"
    token = uuid4().hex
    first_settings = _settings(
        mysql_test_url,
        f"recovery-api-a-{token}@fitweek.test",
        model_stub_url=recovery_model_stub_url,
    )
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: first_settings)
    user_ids: list[UUID] = []
    winner_id = ""
    first_payload: dict[str, object] = {}
    try:
        first_app = main_module.create_application()
        async with first_app.router.lifespan_context(first_app):
            async with AsyncClient(
                transport=ASGITransport(app=first_app), base_url="http://test"
            ) as client:
                first_user = UUID((await client.get("/api/v1/users/me")).json()["id"])
                user_ids.append(first_user)
                plan = await _create_confirmed_plan(client, f"{token}-a")
                first_payload = _recovery_payload(token, plan)
                initial_counts = await _recovery_counts(mysql_test_database, first_user)
                assert set(initial_counts.values()) == {0}

                created = await client.post(
                    "/api/v1/recovery-drafts", json=first_payload
                )
                assert created.status_code == 201, created.text
                winner = created.json()
                winner_id = winner["id"]
                assert winner["source"] == "MODEL"
                assert winner["fallback_used"] is False
                assert winner["outcome"] == "COMPLETE"
                gateway = first_app.state.recovery_draft_model_gateway
                assert gateway.metrics().provider_attempts_total == 1
                replay = await client.post(
                    "/api/v1/recovery-drafts", json=first_payload
                )
                assert replay.status_code == 200, replay.text
                assert replay.json() == winner
                assert gateway.metrics().provider_attempts_total == 1

                artifacts = {}
                for suffix in (
                    "behavior-summary",
                    "change-impact",
                    "candidate-set",
                    "memory-proposals",
                    "trace",
                ):
                    response = await client.get(
                        f"/api/v1/recovery-drafts/{winner_id}/{suffix}"
                    )
                    assert response.status_code == 200, response.text
                    artifacts[suffix] = response.json()
                first_counts = await _recovery_counts(mysql_test_database, first_user)
                assert first_counts == {
                    "summary": 1,
                    "impact": 1,
                    "candidate_set": 1,
                    "candidates": len(artifacts["candidate-set"]["candidates"]),
                    "draft": 1,
                    "proposals": len(artifacts["memory-proposals"]),
                    "trace": 1,
                }

                before_conflict_counts = dict(first_counts)
                conflicting = {
                    **first_payload,
                    "user_request": "A changed request must not reuse this key.",
                }
                conflict = await client.post(
                    "/api/v1/recovery-drafts", json=conflicting
                )
                assert conflict.status_code == 409, conflict.text
                assert conflict.json()["error"]["code"] == (
                    "RECOVERY_DRAFT_IDEMPOTENCY_CONFLICT"
                )
                assert (
                    await _recovery_counts(mysql_test_database, first_user)
                    == before_conflict_counts
                )
                for suffix, body in artifacts.items():
                    response = await client.get(
                        f"/api/v1/recovery-drafts/{winner_id}/{suffix}"
                    )
                    assert response.json() == body

                history_before_review = await _history_snapshot(
                    mysql_test_database, first_user
                )
                reviewed = await asyncio.gather(
                    client.post(
                        f"/api/v1/recovery-drafts/{winner_id}/accept",
                        json={"expected_version": 1},
                    ),
                    client.post(
                        f"/api/v1/recovery-drafts/{winner_id}/accept",
                        json={"expected_version": 1},
                    ),
                )
                assert sorted(item.status_code for item in reviewed) == [200, 409]
                accepted = next(item for item in reviewed if item.status_code == 200)
                assert accepted.json()["status"] == "ACCEPTED"
                assert accepted.json()["version"] == 2
                stale = await client.post(
                    f"/api/v1/recovery-drafts/{winner_id}/reject",
                    json={"expected_version": 1},
                )
                assert stale.status_code == 409, stale.text
                assert (
                    await _history_snapshot(mysql_test_database, first_user)
                    == history_before_review
                )
                for suffix, body in artifacts.items():
                    response = await client.get(
                        f"/api/v1/recovery-drafts/{winner_id}/{suffix}"
                    )
                    assert response.json() == body

        second_settings = _settings(
            mysql_test_url,
            f"recovery-api-b-{token}@fitweek.test",
            model_stub_url=recovery_model_stub_url,
        )
        monkeypatch.setattr(main_module, "get_settings", lambda: second_settings)
        second_app = main_module.create_application()
        async with second_app.router.lifespan_context(second_app):
            async with AsyncClient(
                transport=ASGITransport(app=second_app), base_url="http://test"
            ) as client:
                second_user = UUID((await client.get("/api/v1/users/me")).json()["id"])
                user_ids.append(second_user)
                assert (
                    await client.get(f"/api/v1/recovery-drafts/{winner_id}")
                ).status_code == 404
                second_plan = await _create_confirmed_plan(client, f"{token}-b")
                second_payload = _recovery_payload(
                    f"{token}-b",
                    second_plan,
                    client_request_id=str(first_payload["client_request_id"]),
                )
                before_race = await _recovery_counts(mysql_test_database, second_user)
                assert set(before_race.values()) == {0}
                raced = await asyncio.gather(
                    client.post("/api/v1/recovery-drafts", json=second_payload),
                    client.post("/api/v1/recovery-drafts", json=second_payload),
                )
                successful = next(
                    item for item in raced if item.status_code in {200, 201}
                )
                race_draft = successful.json()
                assert race_draft["id"] != winner_id
                race_candidates = (
                    await client.get(
                        f"/api/v1/recovery-drafts/{race_draft['id']}/candidate-set"
                    )
                ).json()["candidates"]
                race_proposals = (
                    await client.get(
                        f"/api/v1/recovery-drafts/{race_draft['id']}/memory-proposals"
                    )
                ).json()
                after_race = await _recovery_counts(mysql_test_database, second_user)
                assert {
                    key: after_race[key] - before_race[key] for key in after_race
                } == {
                    "summary": 1,
                    "impact": 1,
                    "candidate_set": 1,
                    "candidates": len(race_candidates),
                    "draft": 1,
                    "proposals": len(race_proposals),
                    "trace": 1,
                }
                statuses = sorted(item.status_code for item in raced)
                error_codes = sorted(
                    item.json()["error"]["code"]
                    for item in raced
                    if item.status_code not in {200, 201}
                )
                assert statuses == [200, 201], {
                    "statuses": statuses,
                    "safe_error_codes": error_codes,
                }
                assert raced[0].json() == raced[1].json()
    finally:
        await _cleanup_recovery(mysql_test_database, tuple(user_ids))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_recovery_distinct_request_keys_reuse_deterministic_artifacts(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
    recovery_model_stub_url: str,
) -> None:
    token = uuid4().hex
    first_settings = _settings(
        mysql_test_url,
        f"recovery-reuse-a-{token}@fitweek.test",
        model_stub_url=recovery_model_stub_url,
    )
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: first_settings)
    user_ids: list[UUID] = []
    draft_ids: list[str] = []
    try:
        application = main_module.create_application()
        async with application.router.lifespan_context(application):
            async with AsyncClient(
                transport=ASGITransport(app=application),
                base_url="http://test",
            ) as client:
                user_id = UUID((await client.get("/api/v1/users/me")).json()["id"])
                user_ids.append(user_id)
                plan = await _create_confirmed_plan(client, f"{token}-reuse")
                context_count_before = len(
                    (
                        await _history_snapshot(
                            mysql_test_database,
                            user_id,
                        )
                    )["contexts"]
                )
                first_payload = _recovery_payload(
                    token,
                    plan,
                    client_request_id=f"recovery-reuse-a-{token}",
                )
                second_payload = {
                    **first_payload,
                    "client_request_id": f"recovery-reuse-b-{token}",
                }

                first_response = await client.post(
                    "/api/v1/recovery-drafts",
                    json=first_payload,
                )
                assert first_response.status_code == 201, first_response.text
                second_response = await client.post(
                    "/api/v1/recovery-drafts",
                    json=second_payload,
                )
                assert second_response.status_code == 201, second_response.text
                first_draft = first_response.json()
                second_draft = second_response.json()
                draft_ids.extend((first_draft["id"], second_draft["id"]))
                assert first_draft["id"] != second_draft["id"]
                assert (
                    first_draft["behavior_summary_id"]
                    == (second_draft["behavior_summary_id"])
                )
                assert (
                    first_draft["change_impact_snapshot_id"]
                    == (second_draft["change_impact_snapshot_id"])
                )
                assert (
                    first_draft["candidate_set_id"]
                    == (second_draft["candidate_set_id"])
                )
                assert (
                    first_draft["context_snapshot_reference_id"]
                    == (second_draft["context_snapshot_reference_id"])
                )

                async def artifacts(draft_id: str) -> dict[str, object]:
                    values: dict[str, object] = {}
                    for suffix in (
                        "behavior-summary",
                        "change-impact",
                        "candidate-set",
                        "memory-proposals",
                        "trace",
                    ):
                        response = await client.get(
                            f"/api/v1/recovery-drafts/{draft_id}/{suffix}"
                        )
                        assert response.status_code == 200, response.text
                        values[suffix] = response.json()
                    return values

                first_artifacts = await artifacts(first_draft["id"])
                second_artifacts = await artifacts(second_draft["id"])
                for canonical_name in (
                    "behavior-summary",
                    "change-impact",
                    "candidate-set",
                ):
                    assert (
                        first_artifacts[canonical_name]
                        == (second_artifacts[canonical_name])
                    )
                assert first_artifacts["trace"] != second_artifacts["trace"]
                assert first_artifacts["memory-proposals"] == []
                assert second_artifacts["memory-proposals"] == []
                candidate_count = len(
                    first_artifacts["candidate-set"]["candidates"]  # type: ignore[index]
                )
                assert await _recovery_counts(mysql_test_database, user_id) == {
                    "summary": 1,
                    "impact": 1,
                    "candidate_set": 1,
                    "candidates": candidate_count,
                    "draft": 2,
                    "proposals": 0,
                    "trace": 2,
                }
                history_after_two = await _history_snapshot(
                    mysql_test_database,
                    user_id,
                )
                assert len(history_after_two["contexts"]) == (context_count_before + 1)

                gateway = application.state.recovery_draft_model_gateway
                assert gateway.metrics().provider_attempts_total == 2
                first_replay = await client.post(
                    "/api/v1/recovery-drafts",
                    json=first_payload,
                )
                second_replay = await client.post(
                    "/api/v1/recovery-drafts",
                    json=second_payload,
                )
                assert first_replay.status_code == 200
                assert second_replay.status_code == 200
                assert first_replay.json() == first_draft
                assert second_replay.json() == second_draft
                assert gateway.metrics().provider_attempts_total == 2

                conflict = await client.post(
                    "/api/v1/recovery-drafts",
                    json={
                        **first_payload,
                        "user_request": "Changed evidence under the same key.",
                    },
                )
                assert conflict.status_code == 409, conflict.text
                assert conflict.json()["error"]["code"] == (
                    "RECOVERY_DRAFT_IDEMPOTENCY_CONFLICT"
                )
                assert (
                    await _history_snapshot(mysql_test_database, user_id)
                    == history_after_two
                )

                changed_evidence_payload = {
                    **first_payload,
                    "client_request_id": f"recovery-reuse-c-{token}",
                    "request_type": "REDUCE_FUTURE_LOAD",
                    "user_request": "Please reduce the future training load.",
                }
                changed_evidence = await client.post(
                    "/api/v1/recovery-drafts",
                    json=changed_evidence_payload,
                )
                assert changed_evidence.status_code == 201, changed_evidence.text
                changed_draft = changed_evidence.json()
                draft_ids.append(changed_draft["id"])
                assert changed_draft["context_snapshot_reference_id"] not in {
                    first_draft["context_snapshot_reference_id"],
                    second_draft["context_snapshot_reference_id"],
                }
                assert (
                    changed_draft["candidate_set_id"]
                    != (first_draft["candidate_set_id"])
                )
                assert gateway.metrics().provider_attempts_total == 3
                assert len(
                    (
                        await _history_snapshot(
                            mysql_test_database,
                            user_id,
                        )
                    )["contexts"]
                ) == (context_count_before + 2)

        other_settings = _settings(
            mysql_test_url,
            f"recovery-reuse-b-{token}@fitweek.test",
        )
        monkeypatch.setattr(main_module, "get_settings", lambda: other_settings)
        other_application = main_module.create_application()
        async with other_application.router.lifespan_context(other_application):
            async with AsyncClient(
                transport=ASGITransport(app=other_application),
                base_url="http://test",
            ) as other_client:
                other_user = UUID(
                    (await other_client.get("/api/v1/users/me")).json()["id"]
                )
                user_ids.append(other_user)
                for draft_id in draft_ids:
                    assert (
                        await other_client.get(f"/api/v1/recovery-drafts/{draft_id}")
                    ).status_code == 404
    finally:
        await _cleanup_recovery(mysql_test_database, tuple(user_ids))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_recovery_startup_failure_closes_owned_gateways_once(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = getattr(dependencies_module, "build_mysql_recovery_draft_services", None)
    assert callable(factory), "MySQL Recovery Draft composition factory is absent"
    assert hasattr(main_module, "build_mysql_recovery_draft_services")

    token = uuid4().hex
    settings = _settings(
        mysql_test_url, f"recovery-startup-failure-{token}@fitweek.test"
    )
    sentinel = RuntimeError("controlled post-Recovery startup failure")
    close_count = 0
    detached_during_close = False
    captured: dict[str, Any] = {}
    application = main_module.create_application()

    def counted_factory(*args: object, **kwargs: object) -> object:
        services = factory(*args, **kwargs)
        captured["services"] = services
        original_close = services.model_gateway.close

        async def counted_close() -> None:
            nonlocal close_count, detached_during_close
            close_count += 1
            detached_during_close = all(
                getattr(application.state, name, None) is None
                for name in (
                    "recovery_draft_repository",
                    "recovery_draft_service",
                    "recovery_draft_model_gateway",
                    "recovery_draft_tool_gateway",
                    "recovery_metrics",
                )
            )
            await original_close()

        services.model_gateway.close = counted_close
        return services

    user_id: UUID | None = None

    def fail_after_recovery(*_args: object, **kwargs: object) -> object:
        nonlocal user_id
        user_id = kwargs["user"].id
        raise sentinel

    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main_module, "build_mysql_recovery_draft_services", counted_factory
    )
    monkeypatch.setattr(
        main_module, "build_mysql_orchestration_runtime", fail_after_recovery
    )
    try:
        with pytest.raises(RuntimeError) as raised:
            async with application.router.lifespan_context(application):
                pytest.fail("controlled startup failure did not propagate")
        assert raised.value is sentinel
        assert close_count == 1
        assert detached_during_close is True
        assert not hasattr(captured["services"].tool_gateway, "close")
        assert all(
            getattr(application.state, name, None) is None
            for name in (
                "recovery_draft_repository",
                "recovery_draft_service",
                "recovery_draft_model_gateway",
                "recovery_draft_tool_gateway",
                "recovery_metrics",
            )
        )
    finally:
        for name in (
            "profile_agent_model_gateway",
            "session_design_model_gateway",
            "schedule_model_gateway",
            "schedule_calendar_gateway",
        ):
            gateway = getattr(application.state, name, None)
            close = getattr(gateway, "close", None)
            if close is not None:
                await close()
                setattr(application.state, name, None)
        if user_id is not None:
            await _cleanup_recovery(mysql_test_database, (user_id,))
