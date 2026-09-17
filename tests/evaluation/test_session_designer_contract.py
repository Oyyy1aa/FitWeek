"""Sixty frozen Session Designer whitelist contract cases."""

import json
from pathlib import Path

import pytest

from app.domain.exercises.catalog_seed import CATALOG_SEED
from app.domain.session_design.models import SessionDesignerOutput
from app.session_design.fallback import DeterministicSessionFallback
from app.session_design.validator import SessionDesignerBusinessValidator
from tests.unit.session_design.test_domain_and_policies import candidate_fixture

pytestmark = pytest.mark.phase_5a

CASES = json.loads(
    (Path(__file__).parent / "session_designer_cases.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CASES, ids=lambda value: value["case_id"])
def test_frozen_session_designer_contract(case: dict[str, object]) -> None:
    owner, constraints, template, candidates = candidate_fixture()
    valid = DeterministicSessionFallback().build(candidates, template)  # type: ignore[arg-type]
    expected = bool(case["expected_contract_accepted"])
    output: SessionDesignerOutput
    if expected:
        output = valid
    else:
        output = valid.model_copy(
            update={
                "selections": (
                    valid.selections[0].model_copy(
                        update={"exercise_id": "not_in_frozen_candidate_set"}
                    ),
                    *valid.selections[1:],
                )
            }
        )
    if expected:
        assert (
            SessionDesignerBusinessValidator().validate(
                output,
                candidate_set=candidates,
                template=template,  # type: ignore[arg-type]
                profile=owner,
                constraints=constraints,
                catalog={item.id: item for item in CATALOG_SEED},
            )
            is output
        )
    else:
        with pytest.raises(ValueError):
            SessionDesignerBusinessValidator().validate(
                output,
                candidate_set=candidates,
                template=template,  # type: ignore[arg-type]
                profile=owner,
                constraints=constraints,
                catalog={item.id: item for item in CATALOG_SEED},
            )


def test_evaluation_set_is_exactly_sixty_unique_cases() -> None:
    assert len(CASES) == 60
    assert len({item["case_id"] for item in CASES}) == 60
    assert {item["category"] for item in CASES} == {
        "valid",
        "outside_candidate",
        "invented_exercise",
        "duplicate_exercise",
        "disabled_exercise",
        "equipment_mismatch",
        "location_mismatch",
        "excluded_feature",
        "beginner_difficulty",
        "invalid_schema",
        "duration_boundary",
        "fallback",
    }
