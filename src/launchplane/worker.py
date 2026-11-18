import logging
import threading
from typing import Any, Protocol

from pydantic import BaseModel

from launchplane.act import Cancelled, Stopped
from launchplane.docker_answers import ContainerHostError
from launchplane.instance import LaneBuild
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
STOPPED = (ContainerHostError, ProxyError, ProductError, Stopped)
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
    def move(self, target: LaneBuild, generation: str, witness: Witness, /) -> MoveResult: ...
    def retire(self, product: str, lane: str, delete_data_set: str | None, witness: Witness, /) -> RetireResult: ...
    def begin_maintenance(self, product: str, lane: str, witness: Witness, /) -> MaintenanceResult: ...
    def end_maintenance(self, product: str, lane: str, witness: Witness, /) -> MaintenanceResult: ...


type Ending = tuple[Stands, dict[str, Any] | None, str | None]


def ending(stands: Stands, answer: BaseModel | None, error: str | None = None) -> Ending:
    return stands, None if answer is None else answer.model_dump(mode="json"), error


def carried_out(lanes: Carries, request: Request, witness: Witness) -> Answer:
    product, lane = request.product, request.lane
    match request.work:
        case Work.MOVE:
            if request.build is None:
                raise Stopped("The request names no build to move to. Nothing was changed.")
            return lanes.move(LaneBuild(product=product, lane=lane, build=request.build), request.generation, witness)
        case Work.RETIRE:
            return lanes.retire(product, lane, request.delete_data_set, witness)
        case Work.BEGIN_MAINTENANCE:
            return lanes.begin_maintenance(product, lane, witness)
        case Work.END_MAINTENANCE:
            return lanes.end_maintenance(product, lane, witness)


# Several threads each take the oldest request of a lane none of them is on, so a lane has one worker and its requests
# run in the order they came, while different lanes are worked at once.
class Worker:
    def __init__(self, record: Record, lanes: Carries, secrets: Secrets) -> None:
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
                attempt = self.record.take_up(request.id)
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

    # A move does not end, however it would, over a cancel its call never saw: the record refuses that ending, and the
    # call goes through once more, sees the cancel at its first reading and answers it from the host. Once more and no
    # oftener: a cancel that still cannot be answered, since the host cannot be read, ends the request with that reason.
    def carry_through(self, request: Request, attempt: int, held: Held) -> None:
        seen = threading.Event()
        witness = self.witness_of(request, attempt, held, seen)
        if not self.record.end(attempt, *self.ending_of(request, witness), cancel_seen=seen.is_set()):
            self.record.end(attempt, *self.ending_of(request, witness))

    def ending_of(self, request: Request, witness: Witness) -> Ending:
        try:
            # Every secret a container holds is known before the record is given anything about this request to clean.
            self.secrets.read_secrets()
            answer = carried_out(self.lanes, request, witness)
        except Cancelled as cancelled:
            kept = self.record.read(request.id)
            told = [] if kept is None else put_back_told(kept)
            return ending(Stands.CANCELLED, cancelled.put_back.model_copy(update={"put_back": told}))
        except STOPPED as error:
            return ending(Stands.FAILED, None, f"{request.attempted()} stopped, and is safe to ask again: {error}")
        # Neither is the request's own failure: it stays working, and is taken up again as interrupted.
        except Interrupted, RecordError:
            raise
        except Exception as error:
            log.exception("%s failed in a way Launchplane does not know", request.attempted())
            why = f"{request.attempted()} failed in a way Launchplane does not know, and is safe to ask again: {error!r}"
            return ending(Stands.FAILED, None, why)
        failed = answer.outcome in NOT_A_SUCCESS
        error = answer.error if isinstance(answer, MoveResult | MaintenanceResult) else None
        return ending(Stands.FAILED if failed else Stands.DONE, answer, error if failed else None)
