"""Business validation for controlled Schedule Agent selections."""

from app.domain.scheduling.models import ScheduleAgentOutput, TimeSlotCandidateSet
from app.scheduling.time_policy import overlaps


class ScheduleAgentBusinessValidator:
    def validate(
        self, output: ScheduleAgentOutput, candidate_set: TimeSlotCandidateSet
    ) -> ScheduleAgentOutput:
        allowed = candidate_set.allowed_by_session
        slot_map = candidate_set.slot_map
        assignments = output.assignments
        assigned_ids = tuple(item.session_id for item in assignments)
        unresolved = output.unresolved_session_ids
        if len(assigned_ids) != len(set(assigned_ids)):
            raise ValueError("duplicate Session assignment")
        if len(unresolved) != len(set(unresolved)):
            raise ValueError("duplicate unresolved Session")
        if set(assigned_ids) & set(unresolved):
            raise ValueError("Session is both assigned and unresolved")
        if set(assigned_ids) | set(unresolved) != set(allowed):
            raise ValueError("output must account for every requested Session")
        selected_slots = []
        for assignment in assignments:
            if assignment.session_id not in allowed:
                raise ValueError("unknown Session ID")
            if assignment.slot_id not in allowed[assignment.session_id]:
                raise ValueError("unknown or cross-Session Slot ID")
            selected_slots.append(slot_map[assignment.slot_id])
        for index, left in enumerate(selected_slots):
            if any(
                overlaps(left.start, left.end, right.start, right.end)
                for right in selected_slots[index + 1 :]
            ):
                raise ValueError("selected Slot IDs overlap")
        return output
