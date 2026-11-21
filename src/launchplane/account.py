"""What is said of a request's past, from its lines. It is history: nothing is decided from it.

Only a change of the route has a line written before it is sent, so only that is known across attempts. What an attempt
did to the lane's parts, its data set and its logins is known to that attempt alone, and is said to be not known once a
request was taken up again.
"""

from launchplane.instance import BACK_SOON, LaneRoute, Unread
from launchplane.record import Event, Kept
from launchplane.steps import (
    NOT_KNOWN,
    MaintenanceOutcome,
    MaintenanceResult,
    MoveResult,
    Outcome,
    Result,
    RetireOutcome,
    RetireResult,
)
from launchplane.telling import Seen

ENDED = (MaintenanceOutcome.ENDED, MaintenanceOutcome.NOT_IN_MAINTENANCE)


# What a reading shows of a change of the route that had no answer. `stamp` is how the routes file names the request
# and the attempt that sent it. None is a routes file that could not be read.
def seen_of(line: Event, stamp: str, route: LaneRoute) -> Seen | None:
    sent = line.action
    if sent is None or isinstance(route.file, Unread):
        return None
    if route.written != sent.to:
        return Seen.NOT_AS_ASKED
    return Seen.OURS if sent.to is not None and route.installed_by == stamp else Seen.AS_ASKED


# The answer says what the host shows and what the attempt that ended the request did. Said here: what the request's
# changes of the route show over every attempt, and that the rest is not known where an earlier attempt's is lost.
def accounted[Answer: Result](answer: Answer, kept: Kept | None) -> Answer:
    if kept is None:
        return answer
    routes = [event.route for event in kept.events if event.is_credited]
    again = len(kept.attempts) > 1
    update: dict[str, object] = {"not_known": NOT_KNOWN} if again else {}
    if isinstance(answer, MaintenanceResult) and answer.outcome in ENDED:
        # A lane found out of maintenance was ended by this request only if a route it published says so.
        published = any(to not in (None, BACK_SOON) for to, _ in routes)
        update["outcome"] = MaintenanceOutcome.ENDED if published else MaintenanceOutcome.NOT_IN_MAINTENANCE
    if isinstance(answer, RetireResult):
        removed = next((was for to, was in routes if to is None), None)
        update["route_removed_to_container"] = answer.route_removed_to_container or removed
        update["outcome"] = RetireOutcome.NOT_KNOWN if again and answer.outcome is RetireOutcome.UNCHANGED else answer.outcome
    if isinstance(answer, MoveResult):
        # A switch an earlier attempt sent, and that is the request's own, is among what the request did.
        earlier = [event.did for event in kept.events if event.is_credited and event.attempt < len(kept.attempts)]
        update["did"] = [*earlier, *answer.did]
        update["outcome"] = Outcome.NOT_KNOWN if again and answer.outcome is Outcome.UNCHANGED else answer.outcome
    return answer.model_copy(update=update)
