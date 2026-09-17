"""Deterministic merging and fingerprinting of privacy-minimal busy intervals."""

import hashlib
import json
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.calendar_read.models import CalendarBusyInterval
from app.domain.scheduling.enums import (
    BusyIntervalSource,
    CalendarReadMode,
    CalendarVerificationStatus,
)
from app.domain.scheduling.models import BusyInterval, BusySnapshot


def merge_busy(intervals: tuple[BusyInterval, ...]) -> tuple[BusyInterval, ...]:
    opaque = sorted(intervals, key=lambda item: (item.start, item.end, item.source))
    if not opaque:
        return ()
    merged: list[BusyInterval] = [opaque[0]]
    for item in opaque[1:]:
        previous = merged[-1]
        if item.start <= previous.end:
            merged[-1] = BusyInterval(
                start=previous.start,
                end=max(previous.end, item.end),
                source=(
                    BusyIntervalSource.MANUAL
                    if BusyIntervalSource.MANUAL in {previous.source, item.source}
                    else BusyIntervalSource.CALENDAR_PROVIDER
                ),
            )
        else:
            merged.append(item)
    return tuple(merged)


def build_busy_snapshot(
    *,
    user_id: UUID,
    timezone: str,
    mode: CalendarReadMode,
    provider_intervals: tuple[CalendarBusyInterval, ...],
    manual_intervals: tuple[BusyInterval, ...],
    provider_summary: str,
    created_at: datetime,
    range_start_utc: datetime | None = None,
    range_end_utc: datetime | None = None,
) -> BusySnapshot:
    intervals = merge_busy(
        tuple(
            BusyInterval(
                start=item.start,
                end=item.end,
                source=BusyIntervalSource.CALENDAR_PROVIDER,
            )
            for item in provider_intervals
            if item.transparency == "OPAQUE"
        )
        + manual_intervals
    )
    effective_start = range_start_utc or created_at
    effective_end = range_end_utc or created_at + timedelta(days=366)
    payload = {
        "timezone": timezone,
        "mode": mode.value,
        "provider_summary": provider_summary,
        "range_start_utc": effective_start.isoformat(),
        "range_end_utc": effective_end.isoformat(),
        "intervals": [
            (item.start.isoformat(), item.end.isoformat(), item.source.value)
            for item in intervals
        ],
        "policy": "busy-snapshot-v1",
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    provider_parts = provider_summary.split(":", maxsplit=1)
    provider_name = provider_parts[0]
    provider_version = provider_parts[1] if len(provider_parts) > 1 else "none"
    return BusySnapshot(
        id=uuid5(NAMESPACE_URL, f"fitweek:busy:{user_id}:{fingerprint}"),
        user_id=user_id,
        timezone=timezone,
        range_start_utc=effective_start,
        range_end_utc=effective_end,
        mode=mode,
        verification_status=(
            CalendarVerificationStatus.VERIFIED
            if mode is CalendarReadMode.PROVIDER
            else CalendarVerificationStatus.MANUAL_ONLY
        ),
        intervals=intervals,
        fingerprint=fingerprint,
        provider_summary=provider_summary,
        provider_name=provider_name,
        provider_version=provider_version,
        created_at=created_at,
    )
