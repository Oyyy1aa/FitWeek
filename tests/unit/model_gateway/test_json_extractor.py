"""JSON extraction is bounded, non-evaluating, and single-object only."""

import pytest

from app.domain.model_gateway.errors import (
    ModelEmptyResponseError,
    ModelInvalidJsonError,
    ModelResponseTooLargeError,
)
from app.model_gateway.json_extractor import extract_json_object

pytestmark = pytest.mark.phase_3a


@pytest.mark.parametrize(
    "raw",
    [
        '{"ok":true}',
        '```json\n{"ok":true}\n```',
        'prefix text {"ok":true,"nested":{"value":"}"}} suffix',
    ],
)
def test_extracts_one_object_from_supported_wrappers(raw: str) -> None:
    assert extract_json_object(raw, max_bytes=1024)["ok"] is True


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"one":1} {"two":2}',
        '{"missing":true',
        "[1,2,3]",
        "{broken}",
    ],
)
def test_rejects_missing_multiple_or_malformed_objects(raw: str) -> None:
    with pytest.raises(ModelInvalidJsonError):
        extract_json_object(raw, max_bytes=1024)


def test_empty_and_oversized_responses_are_classified() -> None:
    with pytest.raises(ModelEmptyResponseError):
        extract_json_object("  ", max_bytes=1024)
    with pytest.raises(ModelResponseTooLargeError):
        extract_json_object('{"x":"' + "a" * 2000 + '"}', max_bytes=100)
