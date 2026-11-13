import threading
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from launchplane.name_switch import CheckFailed
from launchplane.running_copy import CopyState, LaneCopy, RunningCopy

LANE_DOMAIN = "test"


class Outcome(StrEnum):
    SWITCHED = "switched"
    UNCHANGED = "unchanged"
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
    def start_copy(self, copy: LaneCopy) -> None: ...
    def remove_copy(self, name: str) -> None: ...
    def retire_copy(self, name: str) -> None: ...
    def read_log_tail(self, name: str) -> str: ...


class Switch(Protocol):
    def ensure_ready(self) -> None: ...
    def read_serving(self) -> dict[str, str]: ...
    def check_copy(self, copy_name: str) -> None: ...
    def point(self, name: str, copy_name: str) -> None: ...


def lane_name(product: str, lane: str) -> str:
    return f"{lane}.{product}.{LANE_DOMAIN}"


def failure_report(error: CheckFailed, log_tail: str) -> str:
    if not log_tail:
        return f"{error} The copy wrote no log."
    return f"{error}\nThe copy's last log lines:\n{log_tail}"


class Mover:
    def __init__(self, host: CopyHost, switch: Switch) -> None:
        self.host = host
        self.switch = switch
        self.one_at_a_time = threading.Lock()

    def lane_copies(self, product: str, lane: str) -> dict[str, RunningCopy]:
        return {copy.name: copy for copy in self.host.read_copies() if (copy.product, copy.lane) == (product, lane)}

    def remove_all_but(self, copies: dict[str, RunningCopy], keep: set[str | None]) -> None:
        for name in copies.keys() - keep:
            self.host.remove_copy(name)

    def start_fresh_unless_running(self, target: LaneCopy, existing: RunningCopy | None) -> None:
        if existing is not None and existing.state == CopyState.RUNNING:
            return
        self.host.remove_copy(target.name)
        self.host.start_copy(target)

    def move(self, target: LaneCopy) -> MoveResult:
        with self.one_at_a_time:
            return self.move_now(target)

    def move_now(self, target: LaneCopy) -> MoveResult:
        name = lane_name(target.product, target.lane)
        self.switch.ensure_ready()
        copies = self.lane_copies(target.product, target.lane)
        serving = self.switch.read_serving().get(name)
        result = MoveResult(
            product=target.product, lane=target.lane, build=target.build, name=name, outcome=Outcome.UNCHANGED, serving=serving
        )

        if serving == target.name and target.name in copies:
            self.remove_all_but(copies, keep={serving})
            return result

        self.remove_all_but(copies, keep={serving, target.name})
        self.start_fresh_unless_running(target, copies.get(target.name))
        try:
            self.switch.check_copy(target.name)
        except CheckFailed as error:
            report = failure_report(error, self.host.read_log_tail(target.name))
            self.host.remove_copy(target.name)
            return result.model_copy(update={"outcome": Outcome.CHECK_FAILED, "error": report})

        self.switch.point(name, target.name)
        if serving is not None and serving in copies:
            self.host.retire_copy(serving)
        return result.model_copy(update={"outcome": Outcome.SWITCHED, "serving": target.name})
