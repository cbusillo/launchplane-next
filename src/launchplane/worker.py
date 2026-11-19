import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from launchplane.act import Cancelled, CouldNotPutBack, Stopped
from launchplane.docker_answers import ContainerHostError
from launchplane.gate import Answer as GateAnswer
from launchplane.instance import LaneBuild, Verdict
from launchplane.judge import Work
from launchplane.product import ProductError
from launchplane.proxy import ProxyError
from launchplane.record import Held, Interrupted, Record, RecordError, Request, Stands, put_back_told
from launchplane.secret_boundary import Secrets
from launchplane.steps import MaintenanceOutcome, MaintenanceResult, MoveResult, Outcome, RetireResult
from launchplane.telling import Witness

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
    def verdict(self, product: str, lane: str, generation: str, /) -> GateAnswer: ...
    def fence(self, product: str, lane: str, generation: str, attempt: int, /) -> GateAnswer: ...
    def abandon(self, product: str, lane: str, generation: str, /) -> str | None: ...
    def retire(self, product: str, lane: str, delete_data_set: str | None, witness: Witness, /) -> RetireResult: ...
    def begin_maintenance(self, product: str, lane: str, witness: Witness, /) -> MaintenanceResult: ...
    def end_maintenance(self, product: str, lane: str, witness: Witness, /) -> MaintenanceResult: ...


# How a call came out, before it is written. `follows_the_gate` is whether the call itself ended on the gate's verdict:
# a switch finished, a cancel put back, or a cancel that won and could not be put back. Any other ending is no release,
# and is written only once the gate has confirmed that.
@dataclass(frozen=True)
class Ending:
    stands: Stands
    answer: dict[str, Any] | None = None
    error: str | None = None
    follows_the_gate: bool = False
    answered: bool = True


def ending(stands: Stands, answer: BaseModel | None, error: str | None = None, *, follows_the_gate: bool = False) -> Ending:
    return Ending(stands, None if answer is None else answer.model_dump(mode="json"), error, follows_the_gate)


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
                attempt = self.record.take_up(request.id, exempt=self.is_committed_or_unknown(request))
                if attempt is not None:
                    self.carry_through(request, attempt, held)
                return True
        finally:
            self.let_go(lane)

    # What a call is given to say what it did, to ask before each action, and to ask between actions. `seen` is told when
    # the call learns a cancel was asked of it.
    def witness_of(self, request: Request, attempt: int, held: Held, seen: threading.Event) -> Witness:
        def tell(did: str, refusal: str | None) -> None:
            self.record.note(attempt, did, refusal)

        # Another Launchplane may hold the turn by now and have changed the lane, so what was decided is not acted on.
        def hold() -> None:
            if not held():
                raise Interrupted(f"The turn of {request.lane} of {request.product} is no longer held, so nothing more was done.")

        def cancel_asked() -> bool:
            asked = self.record.cancel_asked(request.id)
            if asked:
                seen.set()
            return asked

        def refuse_cancel(why: str) -> None:
            self.record.refuse_cancel(request.id, why)

        return Witness(tell, hold, cancel_asked, refuse_cancel)

    # A move the gate says is committed is taken up however often it was interrupted, and so is one the gate cannot be
    # asked about: neither is a request that never ends.
    def is_committed_or_unknown(self, request: Request) -> bool:
        if request.work is not Work.MOVE or request.stands is not Stands.WORKING:
            return False
        try:
            return self.lanes.verdict(request.product, request.lane, request.generation).verdict is Verdict.COMMITTED
        except HOST_UNREAD:
            return True

    def carry_through(self, request: Request, attempt: int, held: Held) -> None:
        seen = threading.Event()
        witness = self.witness_of(request, attempt, held, seen)
        if request.work is not Work.MOVE:
            came_to = self.ending_of(request, attempt, witness)
            self.record.end(attempt, came_to.stands, came_to.answer, came_to.error)
            return
        while not self.move_ended_or_left_open(request, attempt, witness, seen):
            pass

    # One round of a move: the call, then the gate's word on its ending, then the record. False is another round: the
    # gate's verdict is not what the call ended on, or a cancel was recorded that the call never saw.
    def move_ended_or_left_open(self, request: Request, attempt: int, witness: Witness, seen: threading.Event) -> bool:
        came_to = self.ending_of(request, attempt, witness)
        if not came_to.follows_the_gate:
            try:
                verdict = self.lanes.fence(request.product, request.lane, request.generation, attempt).verdict
            except HOST_UNREAD as error:
                return self.left_open(attempt, f"The gate could not be asked, so nothing was ended. {came_to.error or ''} {error}")
            if verdict is not Verdict.NONE and came_to.answered:
                return False
            if verdict is not Verdict.NONE:
                said = "its routes file is renamed" if verdict is Verdict.COMMITTED else "its cancel won"
                return self.left_open(attempt, f"The gate says {said}, and the release is not finished yet: {came_to.error}")
        if self.record.end(attempt, came_to.stands, came_to.answer, came_to.error, cancel_seen=seen.is_set()):
            return True
        return self.cancel_carried_to_the_gate(request, attempt, seen)

    # The record holds a cancel this call never saw, so nothing was ended. The cancel is carried to the gate, and the
    # next round follows what the gate then answers.
    def cancel_carried_to_the_gate(self, request: Request, attempt: int, seen: threading.Event) -> bool:
        try:
            too_late = self.lanes.abandon(request.product, request.lane, request.generation)
        except HOST_UNREAD as error:
            return self.left_open(attempt, f"A cancel is on record and the gate could not be asked about it: {error}")
        if too_late is not None:
            self.record.refuse_cancel(request.id, too_late)
        seen.set()
        return False

    # No ending is written and the attempt is not counted. The request is taken up again by itself, after a while and
    # ahead of newer work on its lane, which stays this worker's until then.
    def left_open(self, attempt: int, why: str) -> bool:
        self.record.leave_open(attempt, why)
        self.sleep(SECONDS_BEFORE_TAKING_UP_AGAIN)
        return True

    def ending_of(self, request: Request, attempt: int, witness: Witness) -> Ending:
        try:
            # Every secret a container holds is known before the record is given anything about this request to clean.
            self.secrets.read_secrets()
            answer = carried_out(self.lanes, request, attempt, witness)
        except Cancelled as cancelled:
            kept = self.record.read(request.id)
            told = [] if kept is None else put_back_told(kept)
            return ending(Stands.CANCELLED, cancelled.put_back.model_copy(update={"put_back": told}), follows_the_gate=True)
        except CouldNotPutBack as error:
            return ending(Stands.FAILED, None, str(error), follows_the_gate=True)
        except STOPPED as error:
            why = f"{request.attempted()} stopped, and is safe to ask again: {error}"
            return Ending(Stands.FAILED, error=why, answered=False)
        # Neither is the request's own failure: it stays working, and is taken up again as interrupted.
        except Interrupted, RecordError:
            raise
        except Exception as error:
            log.exception("%s failed in a way Launchplane does not know", request.attempted())
            why = f"{request.attempted()} failed in a way Launchplane does not know, and is safe to ask again: {error!r}"
            return Ending(Stands.FAILED, error=why, answered=False)
        failed = answer.outcome in NOT_A_SUCCESS
        error = answer.error if isinstance(answer, MoveResult | MaintenanceResult) else None
        switched = isinstance(answer, MoveResult) and answer.outcome is Outcome.SWITCHED
        return ending(Stands.FAILED if failed else Stands.DONE, answer, error if failed else None, follows_the_gate=switched)
