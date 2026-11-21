"""What a request did, put together once from the lines of all its attempts. It is history: nothing is decided from it.

A change counts as the request's only where the host said it changed something, or shows it with the request's own mark.
One the host already had as asked, one refused before any effect, and one found as asked with no mark are none of its.
"""

from launchplane.instance import BACK_SOON, LaneReading, LaneRoute, Unread
from launchplane.record import Event, Kept, Sent
from launchplane.steps import (
    MaintenanceOutcome,
    MaintenanceResult,
    MoveResult,
    Outcome,
    PutBack,
    Result,
    RetireOutcome,
    RetireResult,
)
from launchplane.telling import Seen

SETTLED = (Outcome.UNCHANGED, Outcome.REPAIRED)
ENDED = (MaintenanceOutcome.ENDED, MaintenanceOutcome.NOT_IN_MAINTENANCE)
RETIRES = ("route", "remove", "remove logins", "delete")


def changes(kept: Kept, *, back: bool = False) -> list[Event]:
    return [event for event in kept.events if event.action is not None and event.action.back is back and event.is_credited]


def unanswered(kept: Kept) -> list[str]:
    return [event.did for event in kept.events if event.is_unanswered]


def was_sent(kept: Kept, verb: str, subject: str) -> bool:
    return any(sent is not None and (sent.verb, sent.subject) == (verb, subject) for sent in (each.action for each in kept.events))


def of_a_move(answer: MoveResult, made: list[Event]) -> dict[str, object]:
    repaired = Outcome.REPAIRED if made else Outcome.UNCHANGED
    return {"did": [event.did for event in made], "outcome": repaired if answer.outcome in SETTLED else answer.outcome}


# A data set is deleted by this request when a deletion of its own removed something and the lane no longer has the set.
# Until then each part it deleted is said, beside a set the host still shows.
def of_a_retire(answer: RetireResult, made: list[Event], sent: list[Sent]) -> dict[str, object]:
    deleted = next((each.subject for each in sent if each.verb == "delete" and each.subject not in answer.kept_data_sets), None)
    retired = any(each.verb in RETIRES for each in sent)
    return {
        "deleted": [event.did for event in made if event.action is not None and event.action.verb == "delete"],
        "route_removed_to_container": next((each.was for each in sent if each.verb == "route"), None),
        "retired_containers": [each.subject for each in sent if each.verb == "remove"],
        "deleted_data_set": deleted,
        "outcome": RetireOutcome.DELETED if deleted else RetireOutcome.RETIRED if retired else RetireOutcome.UNCHANGED,
    }


# A lane found out of maintenance was ended by this request only if a route it published says so.
def of_maintenance(answer: MaintenanceResult, sent: list[Sent]) -> dict[str, object]:
    if answer.outcome not in ENDED:
        return {}
    published = any(each.verb == "route" and each.to not in (None, BACK_SOON) for each in sent)
    return {"outcome": MaintenanceOutcome.ENDED if published else MaintenanceOutcome.NOT_IN_MAINTENANCE}


# The answer says what the host shows now. What the request changed, put back and deleted to get there is said here.
def accounted[Answer: Result](answer: Answer, kept: Kept) -> Answer:
    made = changes(kept)
    sent = [event.action for event in made if event.action is not None]
    update: dict[str, object] = {"unanswered": unanswered(kept)}
    if isinstance(answer, MoveResult):
        update |= of_a_move(answer, made)
    elif isinstance(answer, RetireResult):
        update |= of_a_retire(answer, made, sent)
    elif isinstance(answer, MaintenanceResult):
        update |= of_maintenance(answer, sent)
    elif isinstance(answer, PutBack):
        update["put_back"] = [event.did for event in changes(kept, back=True)]
    return answer.model_copy(update=update)


# One sentence for a request that stopped: what it had changed by then, in every attempt, and what it never heard of.
def so_far(kept: Kept) -> str:
    made = [event.did for event in kept.events if event.is_credited]
    changed = f"Before that, this request did: {'; '.join(made)}. Those changes stay." if made else "Nothing was changed."
    waiting = unanswered(kept)
    return f"{changed} No answer was had to: {'; '.join(waiting)}. Each may have been sent, and may yet act." if waiting else changed


# What a reading shows of a change that had no answer. `stamp` is how the routes file names the attempt that sent it.
# None is a change the reading cannot show either way.
def seen_of(sent: Sent, generation: str, stamp: str, route: LaneRoute, lane: LaneReading | None) -> Seen | None:
    if sent.verb == "route":
        if isinstance(route.file, Unread):
            return None
        if route.written != sent.to:
            return Seen.NOT_AS_ASKED
        return Seen.OURS if sent.to is not None and route.installed_by == stamp else Seen.AS_ASKED
    if lane is None or sent.verb not in ("make", "remove", "delete"):
        return None
    made_by = {part.container: part.generation for part in (*lane.instances, *lane.background_services)}
    made_by |= {found.data_set: found.generation for found in lane.data_sets}
    there = sent.subject in made_by
    # A data set still there says nothing of the one part of it that was to go.
    if sent.verb == "delete":
        return None if there else Seen.AS_ASKED
    if sent.verb == "remove":
        return Seen.NOT_AS_ASKED if there else Seen.AS_ASKED
    return Seen.NOT_AS_ASKED if not there else Seen.OURS if made_by[sent.subject] == generation else Seen.AS_ASKED
