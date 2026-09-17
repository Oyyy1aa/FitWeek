"""Canonical serialization used only for deterministic character accounting."""

import json

from app.domain.context.models import ContextSection


def serialize_sections(sections: tuple[ContextSection, ...]) -> str:
    payload = [
        {
            "name": section.name.value,
            "source": section.source,
            "version": section.version,
            "items": [
                {
                    "key": item.key,
                    "value": item.value,
                    "source": item.source,
                    "source_reference": item.source_reference,
                }
                for item in section.items
            ],
        }
        for section in sections
    ]
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def character_count(sections: tuple[ContextSection, ...]) -> int:
    return len(serialize_sections(sections))
