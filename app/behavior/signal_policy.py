"""Transparent repeated-behavior signal creation."""

import hashlib
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from app.domain.behavior.enums import BehaviorPatternType
from app.domain.behavior.models import BehaviorEvidenceReference, BehaviorPattern
from app.domain.checkins.models import CheckInStatus


def _ratio(count: int, total: int) -> Decimal:
    return (Decimal(count) / Decimal(total)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )


def _pattern_id(kind: BehaviorPatternType, key: str) -> str:
    raw = f"{kind.value}:{key}"
    return "pattern-" + hashlib.sha256(raw.encode()).hexdigest()[:20]


def build_patterns(
    evidence: tuple[BehaviorEvidenceReference, ...],
    *,
    min_occurrences: int,
    ratio_threshold: Decimal,
    scheduled_dimensions: tuple[tuple[str, str, str], ...] | None = None,
) -> tuple[BehaviorPattern, ...]:
    dimensions = scheduled_dimensions or tuple(
        (
            item.scheduled_weekday,
            item.scheduled_time_bucket,
            item.location,
        )
        for item in evidence
    )
    opportunities: dict[str, Counter[str]] = {
        "weekday": Counter(item[0] for item in dimensions),
        "time": Counter(item[1] for item in dimensions),
        "location": Counter(item[2] for item in dimensions),
    }
    definitions = (
        (BehaviorPatternType.REPEATED_SKIP_WEEKDAY, "weekday", CheckInStatus.SKIPPED),
        (BehaviorPatternType.REPEATED_SKIP_TIME_OF_DAY, "time", CheckInStatus.SKIPPED),
        (
            BehaviorPatternType.REPEATED_COMPLETION_TIME_OF_DAY,
            "time",
            CheckInStatus.COMPLETED,
        ),
        (
            BehaviorPatternType.REPEATED_COMPLETION_LOCATION,
            "location",
            CheckInStatus.COMPLETED,
        ),
        (BehaviorPatternType.REPEATED_SKIP_LOCATION, "location", CheckInStatus.SKIPPED),
    )
    patterns: list[BehaviorPattern] = []
    for kind, dimension, status in definitions:
        grouped: dict[str, list[UUID]] = defaultdict(list)
        for item in evidence:
            if item.status is status:
                key = {
                    "weekday": item.scheduled_weekday,
                    "time": item.scheduled_time_bucket,
                    "location": item.location,
                }[dimension]
                grouped[key].append(item.checkin_id)
        for key, ids in grouped.items():
            count = len(ids)
            total = opportunities[dimension][key]
            ratio = _ratio(count, total)
            if count >= min_occurrences and ratio >= ratio_threshold:
                patterns.append(
                    BehaviorPattern(
                        pattern_id=_pattern_id(kind, key),
                        pattern_type=kind,
                        key=key,
                        occurrence_count=count,
                        opportunity_count=total,
                        ratio=ratio,
                        evidence_ids=tuple(sorted(ids, key=str)),
                    )
                )
    for kind, predicate in (
        (
            BehaviorPatternType.REPEATED_PARTIAL_COMPLETION,
            lambda item: item.status is CheckInStatus.PARTIALLY_COMPLETED,
        ),
        (
            BehaviorPatternType.REPEATED_HIGH_REPORTED_RPE,
            lambda item: item.reported_rpe is not None and item.reported_rpe >= 8,
        ),
    ):
        ids = [item.checkin_id for item in evidence if predicate(item)]
        count = len(ids)
        total = len(evidence)
        ratio = _ratio(count, total) if total else Decimal("0")
        if count >= min_occurrences and ratio >= ratio_threshold:
            patterns.append(
                BehaviorPattern(
                    pattern_id=_pattern_id(kind, "ALL"),
                    pattern_type=kind,
                    key="ALL",
                    occurrence_count=count,
                    opportunity_count=total,
                    ratio=ratio,
                    evidence_ids=tuple(sorted(ids, key=str)),
                )
            )
    return tuple(sorted(patterns, key=lambda item: (item.pattern_type.value, item.key)))
