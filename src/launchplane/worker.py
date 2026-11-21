import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel

from launchplane.account import accounted, seen_of, so_far, unseen, was_sent
from launchplane.act import Cancelled, CouldNotPutBack, Stopped, Waiting
from launchplane.docker_answers import ContainerHostError, ContainerHostRefused, LoginRemovalRefused
from launchplane.gate import Answer as GateAnswer
from launchplane.instance import LaneBuild, LaneReading, LaneRoute, Verdict
from launchplane.judge import Work
from launchplane.product import ProductError
from launchplane.proxy import ProxyError
from launchplane.record import Held, Interrupted, Record, RecordError, Reply, Request, Sent, Stands
from launchplane.secret_boundary import Secrets
from launchplane.steps import MaintenanceOutcome, MaintenanceResult, MoveResult, Outcome, Result, RetireResult
from launchplane.telling import NOBODY, Answered, Change, Said, Witness

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
                raise Stopped("The request names no build to move to.")
            return lanes.move(LaneBuild(product=product, lane=lane, build=request.build), request.generation, attempt, witness)
        case Work.RETIRE:
            return lanes.retire(product, lane, request.delete_data_set, witness)
        case Work.BEGIN_MAINTENANCE:
            return lanes.begin_maintenance(product, lane, witness)
        case Work.END_MAINTENANCE:
            return lanes.end_maintenance(product, lane, witness)


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

    # Each change an earlier attempt sent and had no answer to is looked for in what this attempt reads anyway, once,
    # and what is seen is written to its line. Nothing the attempt does is chosen from it.
    def reading_back(self, request: Request, attempt: int) -> Callable[[LaneRoute, LaneReading | None], None]:
        kept = self.record.read(request.id)
        lines = [] if kept is None else unseen(kept)
        stamps = {} if kept is None else {each.number: f"{request.generation} {each.id}" for each in kept.attempts}

        def look(route: LaneRoute, lane: LaneReading | None) -> None:
            for line, sent in list(lines):
                seen = seen_of(sent, request.generation, stamps[line.attempt], route, lane)
                if seen is not None:
                    self.record.complete(attempt, line.id, Reply(seen=seen, seen_at=datetime.now(UTC)))
                    lines.remove((line, sent))

        return look

    # What a call is given to say what it did, to ask before each action, and to ask between actions.
    def witness_of(self, request: Request, attempt: int, held: Held, *, again: bool) -> Witness:
        def tell(did: str, refusal: str | None) -> None:
            self.record.note(attempt, did, refusal)

        def about_to(did: str, change: Change) -> Answered:
            line = self.record.about_to(attempt, did, Sent.model_validate(change))

            def answered(said: Said | None, error: str | None, changed: Sequence[str]) -> None:
                heard = Reply() if said is None else Reply(said=said, at=datetime.now(UTC), changed=list(changed))
                self.record.complete(attempt, line, heard, error)

            return answered

        # Another Launchplane may hold the turn by now and have changed the lane, so what was decided is not acted on.
        def hold() -> None:
            if not held():
                raise Interrupted(f"The turn of {request.lane} of {request.product} is no longer held, so nothing more was done.")

        def refuse_cancel(why: str) -> None:
            self.record.refuse_cancel(request.id, why)

        def cancel_asked() -> bool:
            return self.record.cancel_asked(request.id)

        look = self.reading_back(request, attempt) if again else NOBODY.look
        return Witness(tell, hold, cancel_asked, refuse_cancel, request.generation, attempt, about_to, look)

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

    def accounted[Told: Result](self, request: Request, answer: Told) -> Told:
        kept = self.record.read(request.id)
        return answer if kept is None else accounted(answer, kept)

    def so_far(self, request: Request) -> str:
        kept = self.record.read(request.id)
        return "" if kept is None else so_far(kept)

    def ending_of(self, request: Request, attempt: int, witness: Witness, again: bool) -> Ending:
        try:
            # Every secret a container holds is known before the record is given anything about this request to clean.
            self.secrets.read_secrets()
            # A change of the route an earlier attempt sent may still arrive: it is fenced before this one reads the lane.
            if again and request.work is not Work.MOVE:
                self.lanes.fence(request.product, request.lane, request.generation, attempt - 1)
            answer = carried_out(self.lanes, request, attempt, witness)
        except Cancelled as cancelled:
            return ending(Stands.CANCELLED, self.accounted(request, cancelled.put_back), verdict=Verdict.ABANDONED)
        except CouldNotPutBack as error:
            return Ending(Stands.FAILED, error=str(error), verdict=Verdict.ABANDONED)
        except STOPPED as error:
            # A call that could not reach the host, was told to wait, or ran out of steps has learned nothing to end on.
            # A refusal the host answered with is an answer.
            unsure = isinstance(error, (Waiting, *HOST_UNREAD)) and not isinstance(error, ContainerHostRefused)
            why = (
                str(error) if unsure else f"{request.attempted()} stopped, and is safe to ask again: {error} {self.so_far(request)}"
            )
            return Ending(Stands.FAILED, error=why, left_open=unsure, write_refused=isinstance(error, LoginRemovalRefused))
        # Neither is the request's own failure: it stays working, and is taken up again as interrupted.
        except Interrupted, RecordError:
            raise
        except Exception as error:
            log.exception("%s failed in a way Launchplane does not know", request.attempted())
            why = f"{request.attempted()} failed in a way Launchplane does not know, and is safe to ask again: {error!r}"
            return Ending(Stands.FAILED, error=why)
        kept = self.record.read(request.id)
        answer = answer if kept is None else accounted(answer, kept)
        named = request.delete_data_set
        gone = isinstance(answer, RetireResult) and named not in (None, answer.deleted_data_set, *answer.kept_data_sets)
        # The lane has no such data set. Where no attempt sent its deletion, the request named one the lane never had.
        if gone and (kept is None or not was_sent(kept, "delete", named or "")):
            why = f"{request.attempted()} was done. It named the data set {named}, which the lane does not have, to delete."
            return Ending(Stands.FAILED, error=f"{why} Nothing was deleted.")
        failed = answer.outcome in NOT_A_SUCCESS
        error = answer.error if isinstance(answer, MoveResult | MaintenanceResult) else None
        switched = isinstance(answer, MoveResult) and answer.outcome is Outcome.SWITCHED
        verdict = Verdict.COMMITTED if switched else Verdict.NONE
        return ending(Stands.FAILED if failed else Stands.DONE, answer, error if failed else None, verdict)
