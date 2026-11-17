import logging
import threading
from typing import Protocol

from pydantic import BaseModel

from launchplane.act import Stopped
from launchplane.docker_answers import ContainerHostError
from launchplane.instance import LaneBuild
from launchplane.judge import Work
from launchplane.product import ProductError
from launchplane.proxy import ProxyError
from launchplane.record import Interrupted, Record, RecordError, Request, Stands
from launchplane.secret_boundary import Secrets, cleaned, cleaned_value
from launchplane.steps import MaintenanceOutcome, MaintenanceResult, MoveResult, Outcome, RetireResult
from launchplane.telling import Tell

SECONDS_BETWEEN_LOOKS = 0.5
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


class Carries(Protocol):
    def move(self, target: LaneBuild, tell: Tell, /) -> MoveResult: ...
    def retire(self, product: str, lane: str, delete_data_set: str | None, tell: Tell, /) -> RetireResult: ...
    def begin_maintenance(self, product: str, lane: str, tell: Tell, /) -> MaintenanceResult: ...
    def end_maintenance(self, product: str, lane: str, tell: Tell, /) -> MaintenanceResult: ...


def carried_out(lanes: Carries, request: Request, tell: Tell) -> Answer:
    product, lane = request.product, request.lane
    match request.work:
        case Work.MOVE:
            if request.build is None:
                raise Stopped("The request names no build to move to. Nothing was changed.")
            return lanes.move(LaneBuild(product=product, lane=lane, build=request.build), tell)
        case Work.RETIRE:
            return lanes.retire(product, lane, request.delete_data_set, tell)
        case Work.BEGIN_MAINTENANCE:
            return lanes.begin_maintenance(product, lane, tell)
        case Work.END_MAINTENANCE:
            return lanes.end_maintenance(product, lane, tell)


class Worker:
    def __init__(self, record: Record, lanes: Carries, secrets: Secrets) -> None:
        self.record = record
        self.lanes = lanes
        self.secrets = secrets
        self.asked = threading.Event()
        self.stopping = threading.Event()

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.work_until_stopped, name="worker", daemon=True)
        thread.start()
        return thread

    def wake(self) -> None:
        self.asked.set()

    def stop(self) -> None:
        self.stopping.set()
        self.asked.set()

    def work_until_stopped(self) -> None:
        while not self.stopping.is_set():
            self.asked.clear()
            if not self.worked_one():
                self.asked.wait(SECONDS_BETWEEN_LOOKS)

    def worked_one(self) -> bool:
        try:
            return self.work_next()
        except RecordError as error:
            log.warning("No request was taken up: %s", error)
        except Interrupted as error:
            log.warning("A request was left to the Launchplane that took it up: %s", error)
        return False

    # One request at a time, the oldest first. A lane whose turn another Launchplane holds is left to it.
    def work_next(self) -> bool:
        request = self.record.next_up()
        if request is None:
            return False
        with self.record.turn(request.product, request.lane) as ours:
            if not ours:
                return False
            attempt = self.record.take_up(request.id)
            if attempt is not None:
                self.carry_through(request, attempt)
        return True

    def carry_through(self, request: Request, attempt: int) -> None:
        def tell(did: str, refusal: str | None) -> None:
            self.record.note(attempt, self.cleaned(did), None if refusal is None else self.cleaned(refusal))

        try:
            answer = carried_out(self.lanes, request, tell)
        except STOPPED as error:
            self.end(attempt, Stands.FAILED, None, f"{request.attempted()} stopped, and is safe to ask again: {error}")
        except Interrupted:
            raise
        except Exception as error:
            log.exception("%s failed in a way Launchplane does not know", request.attempted())
            why = f"{request.attempted()} failed in a way Launchplane does not know, and is safe to ask again: {error!r}"
            self.end(attempt, Stands.FAILED, None, why)
        else:
            self.end_with(attempt, answer)

    def end_with(self, attempt: int, answer: Answer) -> None:
        failed = answer.outcome in NOT_A_SUCCESS
        error = answer.error if isinstance(answer, MoveResult | MaintenanceResult) else None
        self.end(attempt, Stands.FAILED if failed else Stands.DONE, answer, error if failed else None)

    def end(self, attempt: int, stands: Stands, answer: BaseModel | None, error: str | None) -> None:
        seen = self.secrets.secrets_seen()
        given = {} if answer is None else answer.model_dump(mode="json")
        body = None if answer is None else {name: cleaned_value(value, seen) for name, value in given.items()}
        self.record.end(attempt, stands, body, None if error is None else cleaned(error, seen))

    def cleaned(self, text: str) -> str:
        return cleaned(text, self.secrets.secrets_seen())
