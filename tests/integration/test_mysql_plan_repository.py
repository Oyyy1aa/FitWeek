"""Real MySQL contracts for plans, sessions, revisions, and check-ins."""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.domain.checkins.models import CheckInStatus, WorkoutCheckIn
from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.replanning.models import PlanChangeMetadata, PlanChangeType
from app.persistence.database import Database
from app.persistence.mysql.checkin_repository import MySQLCheckInRepository
from app.persistence.mysql.models import (
    ExerciseCatalogModel,
    IdempotencyRecordModel,
    SessionCheckinModel,
    SessionExerciseModel,
    UserAccountModel,
    WeeklyPlanModel,
    WorkoutSessionModel,
)
from app.persistence.mysql.plan_repository import MySQLPlanRepository
from app.persistence.mysql.user_repository import MySQLUserAccountRepository
from tests.factories import TEST_NOW, make_plan, make_user


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_plan_revision_sessions_and_checkins_survive_new_repositories(
    mysql_test_database: Database,
) -> None:
    """Data is scoped, versioned, idempotent, and readable after adapter recreation."""

    user = replace(make_user(), email=f"plan-{uuid4().hex}@fitweek.test")
    plan = make_plan(user_id=user.id)
    plans = MySQLPlanRepository(mysql_test_database.session_factory)
    checkins = MySQLCheckInRepository(mysql_test_database.session_factory)
    users = MySQLUserAccountRepository(mysql_test_database.session_factory)
    session = plan.sessions[0]
    revision_sessions = ()
    revision_id = None
    checkin = WorkoutCheckIn(
        id=uuid4(),
        client_event_id=f"event-{uuid4().hex}",
        user_id=user.id,
        plan_id=plan.series_id,
        plan_revision=plan.revision,
        session_id=session.id,
        status=CheckInStatus.COMPLETED,
        actual_minutes=session.estimated_minutes,
        perceived_effort=5,
        note=None,
        occurred_at=TEST_NOW,
        created_at=TEST_NOW,
        updated_at=TEST_NOW,
        version=1,
    )
    try:
        await users.save(user)
        async with mysql_test_database.session_factory() as db_session:
            async with db_session.begin():
                catalog_row = await db_session.get(
                    ExerciseCatalogModel, "bodyweight_squat"
                )
                if catalog_row is None:
                    db_session.add(
                        ExerciseCatalogModel(
                            id="bodyweight_squat",
                            name="Bodyweight squat",
                            category="strength",
                            equipment="none",
                            payload={},
                            version=1,
                        )
                    )

        assert await plans.save(plan) == plan
        restored = await MySQLPlanRepository(mysql_test_database.session_factory).get(
            plan.id
        )
        assert restored == plan
        assert await plans.list_sessions_for_user(plan.id, user.id) == list(
            plan.sessions
        )
        assert await plans.get_session_for_user(session.id, user.id) == session

        updated = replace(
            plan,
            status=WeeklyPlanStatus.VALIDATED,
            version=2,
        )
        assert await plans.save(updated) == updated
        with pytest.raises(RepositoryConflictError):
            await plans.save(replace(plan, version=2))

        revision_sessions = tuple(
            replace(item, id=uuid4()) for item in updated.sessions
        )
        revision = replace(
            updated,
            id=uuid4(),
            status=WeeklyPlanStatus.VALIDATED,
            revision=2,
            root_plan_id=plan.id,
            parent_revision=1,
            revision_reason=PlanChangeType.SESSION_DURATION_CHANGED.value,
            change_metadata=PlanChangeMetadata(
                change_type=PlanChangeType.SESSION_DURATION_CHANGED,
                source_revision=1,
                changed_session_ids=(plan.sessions[0].id,),
                preserved_session_ids=(plan.sessions[1].id,),
                immutable_session_ids=(),
                change_fingerprint="b" * 64,
                replanning_policy_version="test-v1",
                client_request_id="replan-1",
            ),
            sessions=revision_sessions,
            estimated_total_minutes=sum(
                item.estimated_minutes for item in revision_sessions
            ),
            confirmed_at=None,
            version=1,
        )
        revision_id = revision.id
        assert await plans.save(revision) == revision
        assert (
            await MySQLPlanRepository(
                mysql_test_database.session_factory
            ).find_by_replan_request(user.id, "replan-1")
            == revision
        )

        await plans.bind_generation_request(user.id, "generation-1", "a" * 64, plan.id)
        assert await plans.get_generation_request(user.id, "generation-1") == (
            "a" * 64,
            updated,
        )
        with pytest.raises(RepositoryUniqueError):
            await plans.bind_generation_request(
                user.id, "generation-1", "b" * 64, plan.id
            )

        assert await checkins.save(checkin) == checkin
        assert await checkins.save(replace(checkin, id=uuid4())) == checkin
        assert await checkins.list_by_user(user.id) == [checkin]
    finally:
        async with mysql_test_database.session_factory() as db_session:
            async with db_session.begin():
                await db_session.execute(
                    delete(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.user_id == str(user.id)
                    )
                )
                await db_session.execute(
                    delete(SessionCheckinModel).where(
                        SessionCheckinModel.user_id == str(user.id)
                    )
                )
                await db_session.execute(
                    delete(SessionExerciseModel).where(
                        SessionExerciseModel.session_id.in_(
                            tuple(
                                str(item.id)
                                for item in (*plan.sessions, *revision_sessions)
                            )
                        )
                    )
                )
                await db_session.execute(
                    delete(WorkoutSessionModel).where(
                        WorkoutSessionModel.plan_id.in_(
                            tuple(
                                str(item)
                                for item in (plan.id, revision_id)
                                if item is not None
                            )
                        )
                    )
                )
                await db_session.execute(
                    delete(WeeklyPlanModel).where(
                        WeeklyPlanModel.id.in_(
                            tuple(
                                str(item)
                                for item in (plan.id, revision_id)
                                if item is not None
                            )
                        )
                    )
                )
                await db_session.execute(
                    delete(UserAccountModel).where(UserAccountModel.id == str(user.id))
                )
