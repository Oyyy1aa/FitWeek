"""Stable bounded assignment maximizing the number of scheduled Sessions."""

from app.domain.scheduling.models import (
    ScheduleAgentOutput,
    ScheduleAssignmentOutput,
    TimeSlotCandidateSet,
)
from app.scheduling.time_policy import overlaps


class DeterministicScheduleFallback:
    policy_version = "deterministic-schedule-v1"

    def __init__(self, *, max_nodes: int = 20000) -> None:
        self._max_nodes = max_nodes

    def build(self, candidate_set: TimeSlotCandidateSet) -> ScheduleAgentOutput:
        allowed = candidate_set.allowed_by_session
        slot_map = candidate_set.slot_map
        session_ids = tuple(sorted(allowed, key=str))
        best: tuple[tuple[str, str], ...] = ()
        nodes = 0

        def search(index: int, selected: tuple[tuple[str, str], ...]) -> None:
            nonlocal best, nodes
            nodes += 1
            if nodes > self._max_nodes:
                return
            if len(selected) + (len(session_ids) - index) < len(best):
                return
            if index == len(session_ids):
                if len(selected) > len(best) or (
                    len(selected) == len(best) and selected < best
                ):
                    best = selected
                return
            session_id = session_ids[index]
            for slot_id in allowed[session_id]:
                slot = slot_map[slot_id]
                if any(
                    overlaps(
                        slot.start,
                        slot.end,
                        slot_map[current_slot_id].start,
                        slot_map[current_slot_id].end,
                    )
                    for _, current_slot_id in selected
                ):
                    continue
                search(index + 1, selected + ((str(session_id), slot_id),))
            search(index + 1, selected)

        search(0, ())
        assigned = {session for session, _ in best}
        return ScheduleAgentOutput(
            assignments=tuple(
                ScheduleAssignmentOutput(session_id=session_id, slot_id=slot_id)
                for session_id, slot_id in best
            ),
            unresolved_session_ids=tuple(
                session_id
                for session_id in session_ids
                if str(session_id) not in assigned
            ),
            explanation_summary=(
                "A deterministic bounded scheduler selected only frozen candidate IDs."
            ),
        )
