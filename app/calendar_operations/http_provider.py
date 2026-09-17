"""Minimal HTTP Calendar provider for the local Phase 6B stub only."""

import hashlib
from typing import Any

import httpx

from app.calendar_operations.gateway import CalendarProviderError
from app.domain.calendar_operations.models import (
    CalendarEventPayload,
    CalendarProviderResult,
)


class HttpCalendarWriteProvider:
    provider_name = "http"
    provider_version = "phase-6b-v1"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        max_response_bytes: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._max_bytes = max_response_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def create_event(
        self, *, calendar_id: str, operation_key: str, payload: CalendarEventPayload
    ) -> CalendarProviderResult:
        return await self._request(
            "POST",
            f"/calendars/{calendar_id}/events",
            operation_key=operation_key,
            payload=payload,
        )

    async def update_event(
        self,
        *,
        calendar_id: str,
        external_event_id: str,
        operation_key: str,
        payload: CalendarEventPayload,
    ) -> CalendarProviderResult:
        return await self._request(
            "PUT",
            f"/calendars/{calendar_id}/events/{external_event_id}",
            operation_key=operation_key,
            payload=payload,
        )

    async def delete_event(
        self,
        *,
        calendar_id: str,
        external_event_id: str,
        operation_key: str,
    ) -> CalendarProviderResult:
        return await self._request(
            "DELETE",
            f"/calendars/{calendar_id}/events/{external_event_id}",
            operation_key=operation_key,
            payload=None,
            fallback_event_id=external_event_id,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation_key: str,
        payload: CalendarEventPayload | None,
        fallback_event_id: str | None = None,
    ) -> CalendarProviderResult:
        document = None if payload is None else self._document(payload)
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Idempotency-Key": operation_key,
                },
                json=document,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise CalendarProviderError(
                "CALENDAR_PROVIDER_UNAVAILABLE", retryable=True
            ) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise CalendarProviderError(
                f"CALENDAR_PROVIDER_HTTP_{response.status_code}", retryable=True
            )
        if response.status_code == 404 and method == "DELETE":
            return CalendarProviderResult(
                external_event_id=fallback_event_id,
                response_reference_hash=hashlib.sha256(response.content).hexdigest(),
            )
        if response.status_code >= 400:
            raise CalendarProviderError(
                f"CALENDAR_PROVIDER_HTTP_{response.status_code}", retryable=False
            )
        if len(response.content) > self._max_bytes:
            raise CalendarProviderError(
                "CALENDAR_PROVIDER_RESPONSE_TOO_LARGE", retryable=False
            )
        if method == "DELETE" and response.status_code == 204:
            return CalendarProviderResult(
                external_event_id=fallback_event_id,
                response_reference_hash=hashlib.sha256(response.content).hexdigest(),
            )
        try:
            raw: Any = response.json()
            external_id = str(raw["event_id"])
            if not external_id:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise CalendarProviderError(
                "CALENDAR_PROVIDER_INVALID_RESPONSE", retryable=False
            ) from exc
        return CalendarProviderResult(
            external_event_id=external_id,
            response_reference_hash=hashlib.sha256(response.content).hexdigest(),
        )

    @staticmethod
    def _document(payload: CalendarEventPayload) -> dict[str, str]:
        return {
            "stable_uid": payload.stable_uid,
            "summary": payload.summary,
            "description": payload.description,
            "start": payload.start.isoformat(),
            "end": payload.end.isoformat(),
            "timezone": payload.timezone,
            "transparency": payload.transparency,
        }

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
