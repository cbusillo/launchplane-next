import logging
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, Protocol

from pydantic import BaseModel

from launchplane.act import Cancelled, CouldNotPutBack, Stopped, Waiting
from launchplane.docker_answers import ContainerHostError, ContainerHostRefused, LoginRemovalRefused
from launchplane.gate import Answer as GateAnswer
from launchplane.instance import BACK_SOON, LaneBuild, LaneRoute, Verdict
from launchplane.judge import Work
from launchplane.product import ProductError
from launchplane.proxy import ProxyError
from launchplane.record import Event, Held, Interrupted, Kept, Record, RecordError, Reply, Request, Stands, put_back_told
from launchplane.secret_boundary import Secrets
from launchplane.steps import Command, MaintenanceOutcome, MaintenanceResult, MoveResult, Outcome, RetireOutcome, RetireResult, Sent
from launchplane.telling import LOOKS, NOBODY, Answered, Said, Witness

SECONDS_BETWEEN_LOOKS = 0.5
# Lanes worked at once. Each holds one connection for its turn, and all share the one container host.
MOST_LANES_AT_ONCE = 4
# How long a request that could not be ended is left before it is taken up again.
SECONDS_BEFORE_TAKING_UP_AGAIN = 5.0
HOST_UNREAD = (ContainerHostError, ProxyError)
STOPPED = (*HOST_UNREAD, ProductError, Stopped)
NOT_A_SUCCESS = {
    Outcome.CHECK_FAILED,
    Outcome.BACKGROUND_SERVICE_FAILED,
    MaintenanceOutcome.DATABASE_KILLED,
    MaintenanceOutcome.WEB_PART_FAILED,
    MaintenanceOutcome.BACKGROUND_SERVICE_FAILED,
}
log = logging.getLogger(__name__)

type Answer = MoveResult | RetireResult | MaintenanceResult
type Lane = tuple[str, str]


class Carries(Protocol):
    def move(self, target: LaneBuild, generation: str, attempt: int, witness: Witness, /) -> MoveResult: ...
    def clean_up(self, product: str, lane: str, generation: str, attempt: int, by: Witness, /, *, logins: bool) -> list[str]: ...
    def fence(self, product: str, lane: str, generation: str, attempt: int, by: Witness = NOBODY, /) -> GateAnswer: ...
    def abandon(self, product: str, lane: str, generation: str, /) -> str | None: ...
    def retire(self, product: str, lane: str, delete_data_set: str | None, witness: Witness, /) -> RetireResult: ...
    def begin_maintenance(self, product: str, lane: str, witness: Witness, /) -> MaintenanceResult: ...
    def end_maintenance(self, product: str, lane: str, witness: Witness, /) -> MaintenanceResult: ...


# How a call came out, before it is written. `verdict` is the gate's verdict the call itself ended on: committed for a
# switch finished, abandoned for a cancel put back or one that could not be. With none the ending is no release, and is
# written only once the gate has confirmed that. `left_open` is a call that learned nothing to end on.
@dataclass(frozen=True)
class Ending:
    stands: Stands
    answer: dict[str, Any] | None = None
    error: str | None = None
    verdict: Verdict = Verdict.NONE
    left_open: bool = False
    # A database server refused to remove a login. The request ends on that, and the removal is not sent again.
    write_refused: bool = False


def ending(stands: Stands, answer: BaseModel | None, error: str | None = None, verdict: Verdict = Verdict.NONE) -> Ending:
    return Ending(stands, None if answer is None else answer.model_dump(mode="json"), error, verdict)


def carried_out(lanes: Carries, request: Request, attempt: int, witness: Witness) -> Answer:
    product, lane = request.product, request.lane
    match request.work:
        case Work.MOVE:
            if request.build is None:
                raise Stopped("The request names no build to move to. Nothing was changed.")
            return lanes.move(LaneBuild(product=product, lane=lane, build=request.build), request.generation, attempt, witness)
        case Work.RETIRE:
            return lanes.retire(product, lane, request.delete_data_set, witness)
        case Work.BEGIN_MAINTENANCE:
            return lanes.begin_maintenance(product, lane, witness)
        case Work.END_MAINTENANCE:
            return lanes.end_maintenance(product, lane, witness)


# Whether a line says the request changed something. A change of the route or a command for a part says so by its own line.
def is_change(event: Event) -> bool:
    sent = (event.action or event.command) is not None
    return event.is_own if sent else event.error is None and not event.did.startswith(LOOKS)


# A request's answer says what every attempt of it did, as each told it. It is history: nothing is decided from it.
def of_every_attempt(answer: MoveResult, kept: Kept | None) -> MoveResult:
    if kept is None or len(kept.attempts) < 2:
        return answer
    did = [event.did for event in kept.events if is_change(event)]
    repaired = Outcome.REPAIRED if did and answer.outcome is Outcome.UNCHANGED else answer.outcome
    return answer.model_copy(update={"did": did, "outcome": repaired, "not_answered": kept.commands_not_answered()})


# A lane found out of maintenance was brought out by this request where a change of the route it made sends visitors to
# an instance. It is history, for the answer: what was done on the host was decided without it.
def ended_by(answer: MaintenanceResult, kept: Kept | None) -> MaintenanceResult:
    lines = [] if kept is None else kept.events
    published = any(line.is_credited and line.action is not None and line.action.to not in (None, BACK_SOON) for line in lines)
    return answer.model_copy(update={"outcome": MaintenanceOutcome.ENDED}) if published else answer


def accounted(answer: Answer, read: Callable[[], Kept | None]) -> Answer:
    if isinstance(answer, MoveResult):
        return of_every_attempt(answer, read())
    if isinstance(answer, MaintenanceResult) and answer.outcome is MaintenanceOutcome.NOT_IN_MAINTENANCE:
        return ended_by(answer, read())
    return answer


# Several threads each take the oldest request of a lane none of them is on, so a lane has one worker and its requests
# run in the order they came, while different lanes are worked at once.
class Worker:
    def __init__(self, record: Record, lanes: Carries, secrets: Secrets, sleep: Callable[[float], None] = time.sleep) -> None:
        self.sleep = sleep
        self.record = record
        self.lanes = lanes
        self.secrets = secrets
        self.at_work: set[Lane] = set()
        self.choosing = threading.Lock()
        self.asked = threading.Condition()
        self.times_asked = 0
        self.stopping = False

    def start(self) -> list[threading.Thread]:
        threads = [
            threading.Thread(target=self.work_until_stopped, name=f"worker-{number}", daemon=True)
            for number in range(1, MOST_LANES_AT_ONCE + 1)
        ]
        for thread in threads:
            thread.start()
        return threads

    def wake(self) -> None:
        with self.asked:
            self.times_asked += 1
            self.asked.notify_all()

    def stop(self) -> None:
        self.stopping = True
        self.wake()

    def work_until_stopped(self) -> None:
        while not self.stopping:
            asked_before = self.times_asked
            if not self.worked_one():
                self.wait_to_be_asked(asked_before)

    # A request asked for while this thread was looking is not slept through: it was asked since the thread last saw.
    def wait_to_be_asked(self, asked_before: int) -> None:
        with self.asked:
            self.asked.wait_for(lambda: self.times_asked != asked_before, SECONDS_BETWEEN_LOOKS)

    def worked_one(self) -> bool:
        try:
            return self.work_next()
        except RecordError as error:
            log.warning("No request was taken up: %s", error)
        except Interrupted as error:
            log.warning("A request was left to the Launchplane that took it up: %s", error)
        return False

    def work_next(self) -> bool:
        return any(self.worked(request) for request in self.record.next_up())

    def claimed(self, lane: Lane) -> bool:
        with self.choosing:
            if lane in self.at_work:
                return False
            self.at_work.add(lane)
            return True

    def let_go(self, lane: Lane) -> None:
        with self.choosing:
            self.at_work.discard(lane)

    # A lane another thread is on, or whose turn another Launchplane holds, is left to it.
    def worked(self, request: Request) -> bool:
        lane = (request.product, request.lane)
        if not self.claimed(lane):
            return False
        try:
            with self.record.turn(*lane) as held:
                if held is None:
                    return False
                taken = self.record.take_up(request.id)
                if taken is not None:
                    witness = self.witness_of(request, taken.attempt, held, again=taken.again)
                    while not self.ended_or_left_open(request, taken.attempt, witness, taken.again):
                        pass
                return True
        finally:
            self.let_go(lane)

    # Each change of the route an earlier attempt sent with no answer is looked for in every reading this attempt makes
    # anyway: it may land late. A make is looked for in the first only: a container this attempt then makes is its own.
    def reading_back(self, request: Request, attempt: int) -> Callable[[LaneRoute, Collection[str]], None]:
        kept = self.record.read(request.id)
        lines = [] if kept is None else kept.lines_to_read_back()
        stamps = {} if kept is None else {each.number: f"{request.generation} {each.id}" for each in kept.attempts}

        def look(route: LaneRoute, made: Collection[str]) -> None:
            for line in list(lines):
                if line.is_seen_in(route, stamps[line.attempt], made):
                    self.record.complete(attempt, line.id, Reply(read_back_at=datetime.now(UTC)))
                    lines.remove(line)
            lines[:] = [each for each in lines if each.command is None]

        return look

    # What a call is given to say what it did, to ask before each action, and to ask between actions.
    def witness_of(self, request: Request, attempt: int, held: Held, *, again: bool) -> Witness:
        def tell(did: str, refusal: str | None) -> None:
            self.record.note(attempt, did, refusal)

        def written(did: str, sent: Sent | Command) -> Answered:
            line = self.record.about_to(attempt, did, sent)

            def answered(said: Said | None, error: str | None) -> None:
                heard = Reply(made=said is Said.CHANGED, already=said is Said.ALREADY, at=datetime.now(UTC))
                self.record.complete(attempt, line, Reply() if said is None else heard, error)

            return answered

        # Another Launchplane may hold the turn by now and have changed the lane, so what was decided is not acted on.
        def hold() -> None:
            if not held():
                raise Interrupted(f"The turn of {request.lane} of {request.product} is no longer held, so nothing more was done.")

        def refuse_cancel(why: str) -> None:
            self.record.refuse_cancel(request.id, why)

        look = self.reading_back(request, attempt) if again else NOBODY.look
        asked = partial(self.record.cancel_asked, request.id)
        return Witness(tell, hold, asked, refuse_cancel, request.generation, attempt, look=look, write=written)

    # One round of any request: the call, then for a move the gate's word on its ending, then the record. A move's ending
    # that is no release is written once the attempt is fenced with no verdict and, where it failed, what the release made
    # is cleaned up and read to be gone. False is another round: the record holds a cancel the gate had not decided, and now has.
    def ended_or_left_open(self, request: Request, attempt: int, witness: Witness, again: bool) -> bool:
        came_to = self.ending_of(request, attempt, witness, again)
        if came_to.left_open:
            return self.left_open(attempt, came_to.error or "")
        if came_to.verdict is Verdict.NONE and request.work is Work.MOVE:
            lane = (request.product, request.lane, request.generation, attempt)
            try:
                fenced = self.lanes.fence(*lane).verdict
                if fenced is Verdict.NONE and came_to.stands is Stands.FAILED:
                    came_to = self.cleaned_up(came_to, lane, witness)
            except STOPPED as error:
                return self.left_open(attempt, f"{came_to.error or 'It was done.'} It is not ended yet: {error}")
            came_to = replace(came_to, verdict=fenced)
            if fenced is not Verdict.NONE and not came_to.write_refused:
                why = f"The gate answers {fenced} for this release, so it is not ended on: {came_to.error or 'done'}"
                return self.left_open(attempt, why)
        if self.record.end(attempt, came_to.stands, came_to.answer, came_to.error, decided=came_to.verdict):
            return True
        return self.cancel_carried_to_the_gate(request, attempt)

    # The failure stands whatever cleaning up after it could not read or was refused, and its error says which.
    def cleaned_up(self, came_to: Ending, lane: tuple[str, str, str, int], witness: Witness) -> Ending:
        try:
            left = "; ".join(self.lanes.clean_up(*lane, witness, logins=not came_to.write_refused))
        except LoginRemovalRefused as error:
            left = str(error)
        return replace(came_to, error=f"{came_to.error} The clean-up after it is not known to be whole: {left}") if left else came_to

    # The record holds a cancel the gate has not decided, so nothing was ended. The cancel is carried to the gate, and
    # the next round follows what the gate then answers.
    def cancel_carried_to_the_gate(self, request: Request, attempt: int) -> bool:
        try:
            too_late = self.lanes.abandon(request.product, request.lane, request.generation)
        except HOST_UNREAD as error:
            return self.left_open(attempt, f"A cancel is on record and the gate could not be asked about it: {error}")
        if too_late is not None:
            self.record.refuse_cancel(request.id, too_late)
        return False

    # No ending is written. The request is taken up again by itself, after a while and
    # ahead of newer work on its lane, which stays this worker's until then.
    def left_open(self, attempt: int, why: str) -> bool:
        self.record.leave_open(attempt, why)
        self.sleep(SECONDS_BEFORE_TAKING_UP_AGAIN)
        return True

    def ending_of(self, request: Request, attempt: int, witness: Witness, again: bool) -> Ending:
        try:
            # Every secret a container holds is known before the record is given anything about this request to clean.
            self.secrets.read_secrets()
            # A change of the route an earlier attempt sent may still arrive: it is fenced before this one reads the lane.
            if again and request.work is not Work.MOVE:
                self.lanes.fence(request.product, request.lane, request.generation, attempt - 1)
            answer = carried_out(self.lanes, request, attempt, witness)
        except Cancelled as cancelled:
            kept = self.record.read(request.id)
            told = [] if kept is None else put_back_told(kept)
            return ending(Stands.CANCELLED, cancelled.put_back.model_copy(update={"put_back": told}), verdict=Verdict.ABANDONED)
        except CouldNotPutBack as error:
            return Ending(Stands.FAILED, error=str(error), verdict=Verdict.ABANDONED)
        except STOPPED as error:
            # A call that could not reach the host, was told to wait, or ran out of steps has learned nothing to end on.
            # A refusal the host answered with is an answer.
            unsure = isinstance(error, (Waiting, *HOST_UNREAD)) and not isinstance(error, ContainerHostRefused)
            why = str(error) if unsure else f"{request.attempted()} stopped, and is safe to ask again: {error}"
            return Ending(Stands.FAILED, error=why, left_open=unsure, write_refused=isinstance(error, LoginRemovalRefused))
        # Neither is the request's own failure: it stays working, and is taken up again as interrupted.
        except Interrupted, RecordError:
            raise
        except Exception as error:
            log.exception("%s failed in a way Launchplane does not know", request.attempted())
            why = f"{request.attempted()} failed in a way Launchplane does not know, and is safe to ask again: {error!r}"
            return Ending(Stands.FAILED, error=why)
        answer = accounted(answer, partial(self.record.read, request.id))
        named = request.delete_data_set
        if isinstance(answer, RetireResult) and named not in (None, answer.deleted_data_set, *answer.kept_data_sets):
            # The lane has no such data set. This request deleted it only if an attempt of it sent that: history, for the answer.
            kept = self.record.read(request.id)
            if not any(event.did == f"delete the data set {named}" for event in ([] if kept is None else kept.events)):
                why = f"{request.attempted()} was done. It named the data set {named}, which the lane does not have, to delete."
                return Ending(Stands.FAILED, error=f"{why} Nothing was deleted.")
            answer = answer.model_copy(update={"outcome": RetireOutcome.DELETED, "deleted_data_set": named})
        failed = answer.outcome in NOT_A_SUCCESS
        error = answer.error if isinstance(answer, MoveResult | MaintenanceResult) else None
        switched = isinstance(answer, MoveResult) and answer.outcome is Outcome.SWITCHED
        verdict = Verdict.COMMITTED if switched else Verdict.NONE
        return ending(Stands.FAILED if failed else Stands.DONE, answer, error if failed else None, verdict)
