"""Fitness profile and structured constraint endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.contracts import (
    ConstraintCreateRequest,
    ConstraintResponse,
    ProfileResponse,
    ProfileUpsertRequest,
)
from app.api.dependencies import get_current_user, get_profile_service
from app.application.profiles import (
    AddConstraintCommand,
    ProfileService,
    UpsertProfileCommand,
)
from app.domain.users.models import UserAccount

router = APIRouter(prefix="/profiles", tags=["profiles"])


@router.put("/me", response_model=ProfileResponse)
async def upsert_my_profile(
    payload: ProfileUpsertRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ProfileService, Depends(get_profile_service)],
) -> ProfileResponse:
    view = await service.upsert_profile(
        user,
        UpsertProfileCommand(
            experience_level=payload.experience_level,
            weekly_frequency=payload.weekly_frequency,
            max_session_minutes=payload.max_session_minutes,
            primary_goal=payload.primary_goal,
            scope_confirmed=payload.scope_confirmed,
            expected_version=payload.expected_version,
        ),
    )
    return ProfileResponse.from_view(view)


@router.get("/me", response_model=ProfileResponse)
async def get_my_profile(
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ProfileService, Depends(get_profile_service)],
) -> ProfileResponse:
    return ProfileResponse.from_view(await service.get_profile(user))


@router.post(
    "/me/constraints",
    response_model=ConstraintResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_my_constraint(
    payload: ConstraintCreateRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ProfileService, Depends(get_profile_service)],
) -> ConstraintResponse:
    constraint = await service.add_constraint(
        user,
        AddConstraintCommand(
            constraint_type=payload.constraint_type,
            constraint_value=payload.constraint_value,
            priority=payload.priority,
            is_hard=payload.is_hard,
            source=payload.source,
            valid_until=payload.valid_until,
        ),
    )
    return ConstraintResponse.from_domain(constraint)


@router.delete(
    "/me/constraints/{constraint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_my_constraint(
    constraint_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ProfileService, Depends(get_profile_service)],
) -> Response:
    await service.delete_constraint(user, constraint_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
