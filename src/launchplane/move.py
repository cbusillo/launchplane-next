import threading
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from launchplane.name_switch import CheckFailed
from launchplane.running_copy import CopyState, LaneCopy, RunningCopy, new_copy_name

LANE_DOMAIN = "test"


class Outcome(StrEnum):
    SWITCHED = "switched"
    UNCHANGED = "unchanged"
    CLEANED_UP = "cleaned up"
    CHECK_FAILED = "check failed"


class MoveResult(BaseModel):
    product: str
    lane: str
    build: str
    name: str
    outcome: Outcome
    serving: str | None
    error: str | None = None


class CopyHost(Protocol):
    def read_copies(self) -> list[RunningCopy]: ...
    def start_copy(self, copy: LaneCopy, name: str) -> None: ...
    def remove_copy(self, name: str) -> None: ...
    def retire_copy(self, name: str) -> None: ...
    def read_log_tail(self, name: str) -> str: ...


class Switch(Protocol):
    def ensure_ready_for(self, name: str) -> None: ...
    def read_serving(self) -> dict[str, str]: ...
    def check_copy(self, copy_name: str) -> None: ...
    def point(self, name: str, copy_name: str) -> None: ...


def lane_name(product: str, lane: str) -> str:
    return f"{lane}.{product}.{LANE_DOMAIN}"


def failure_report(error: CheckFailed, log_tail: str) -> str:
    if not log_tail:
        return f"{error} The copy wrote no log."
    return f"{error}\nThe copy's last log lines:\n{log_tail}"


def is_running_build(copy: RunningCopy | None, build: str) -> bool:
    return copy is not None and copy.state == CopyState.RUNNING and copy.build == build


def copy_left_waiting(copies: dict[str, RunningCopy], serving: str | None, build: str) -> str | None:
    return next((name for name, copy in copies.items() if name != serving and is_running_build(copy, build)), None)


class Mover:
    def __init__(self, host: CopyHost, switch: Switch) -> None:
        self.host = host
        self.switch = switch
        self.one_at_a_time = threading.Lock()

    def lane_copies(self, product: str, lane: str) -> dict[str, RunningCopy]:
        return {copy.name: copy for copy in self.host.read_copies() if (copy.product, copy.lane) == (product, lane)}

    def retire_all_but(self, copies: dict[str, RunningCopy], keep: set[str | None]) -> bool:
        leftovers = copies.keys() - keep
        for name in leftovers:
            self.host.retire_copy(name)
        return bool(leftovers)

    def start_new_copy(self, target: LaneCopy) -> str:
        name = new_copy_name(target)
        self.host.start_copy(target, name)
        return name

    def move(self, target: LaneCopy) -> MoveResult:
        with self.one_at_a_time:
            return self.move_now(target)

    def move_now(self, target: LaneCopy) -> MoveResult:
        name = lane_name(target.product, target.lane)
        self.switch.ensure_ready_for(name)
        copies = self.lane_copies(target.product, target.lane)
        serving = self.switch.read_serving().get(name)
        result = MoveResult(
            product=target.product, lane=target.lane, build=target.build, name=name, outcome=Outcome.UNCHANGED, serving=serving
        )

        serving_copy = None if serving is None else copies.get(serving)
        if is_running_build(serving_copy, target.build):
            if self.retire_all_but(copies, keep={serving}):
                return result.model_copy(update={"outcome": Outcome.CLEANED_UP})
            return result

        left_waiting = copy_left_waiting(copies, serving, target.build)
        self.retire_all_but(copies, keep={serving, left_waiting})
        candidate = left_waiting or self.start_new_copy(target)
        try:
            self.switch.check_copy(candidate)
        except CheckFailed as error:
            report = failure_report(error, self.host.read_log_tail(candidate))
            self.host.remove_copy(candidate)
            return result.model_copy(update={"outcome": Outcome.CHECK_FAILED, "error": report})

        self.switch.point(name, candidate)
        if serving_copy is not None:
            self.host.retire_copy(serving_copy.name)
        return result.model_copy(update={"outcome": Outcome.SWITCHED, "serving": candidate})
