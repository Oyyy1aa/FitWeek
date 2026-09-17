"""Local single-user identity endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.dependencies import get_current_user
from app.domain.users.models import UserAccount

router = APIRouter(prefix="/users", tags=["users"])


class CurrentUserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    timezone: str
    status: str

    @classmethod
    def from_domain(cls, user: UserAccount) -> "CurrentUserResponse":
        return cls(
            id=str(user.id),
            email=user.email,
            display_name=user.display_name,
            timezone=user.timezone,
            status=user.status.value,
        )


@router.get("/me", response_model=CurrentUserResponse)
async def get_my_user(
    user: Annotated[UserAccount, Depends(get_current_user)],
) -> CurrentUserResponse:
    """Return the local identity without exposing an authentication surface."""

    return CurrentUserResponse.from_domain(user)
