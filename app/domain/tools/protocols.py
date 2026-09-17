"""Ports used by applications through the Tool Gateway."""

from typing import Protocol, TypeVar

from pydantic import BaseModel

from app.domain.tools.models import ToolInvocationContext

RequestT = TypeVar("RequestT", bound=BaseModel, contravariant=True)
ResponseT = TypeVar("ResponseT", bound=BaseModel, covariant=True)


class ToolAdapter(Protocol[RequestT, ResponseT]):
    """A single-attempt typed adapter; retries are exclusively Gateway-owned."""

    async def invoke_once(
        self, context: ToolInvocationContext, request: RequestT
    ) -> ResponseT: ...

    async def close(self) -> None: ...
