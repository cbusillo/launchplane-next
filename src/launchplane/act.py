import threading
from collections.abc import Callable
from typing import Protocol

from launchplane.decide import (
    Action,
    AwaitRoute,
    Check,
    LaneRoute,
    LaneSeen,
    MoveResult,
    ProxyNotRunning,
    ReadAgain,
    Retire,
    RetireResult,
    StartCopy,
    StartProxy,
    WriteRoute,
    decide_move,
    decide_retire,
    lane_name,
)
from launchplane.proxy import CheckFailed, ProxyError
from launchplane.running_copy import LaneCopy, RunningCopy, new_copy_name

PASSES_TO_START_THE_PROXY = 2


class CopyHost(Protocol):
    def read_copies(self) -> list[RunningCopy]: ...
    def start_copy(self, copy: LaneCopy, name: str) -> None: ...
    def remove_copy(self, name: str) -> None: ...
    def retire_copy(self, name: str) -> None: ...
    def read_log_tail(self, name: str) -> str: ...


class Proxy(Protocol):
    def start(self) -> None: ...
    def read_lane_route(self, name: str) -> ProxyNotRunning | LaneRoute: ...
    def await_route(self, name: str, copy_name: str | None) -> None: ...
    def write_route(self, name: str, copy_name: str | None) -> None: ...
    def check_copy(self, copy_name: str) -> None: ...


class Refused(Exception):
    def __init__(self, answer: MoveResult) -> None:
        super().__init__(answer.error)
        self.answer = answer


def failure_report(error: CheckFailed, log_tail: str) -> str:
    if not log_tail:
        return f"{error} The copy wrote no log."
    return f"{error}\nThe copy's last log lines:\n{log_tail}"


class Lanes:
    def __init__(self, host: CopyHost, proxy: Proxy, name_copy: Callable[[LaneCopy], str] = new_copy_name) -> None:
        self.host = host
        self.proxy = proxy
        self.name_copy = name_copy
        self.one_at_a_time = threading.Lock()

    def read(self, product: str, lane: str) -> LaneSeen:
        route = self.proxy.read_lane_route(lane_name(product, lane))
        copies = tuple(copy for copy in self.host.read_copies() if (copy.product, copy.lane) == (product, lane))
        return LaneSeen(copies=copies, route=route)

    def plan[P](self, product: str, lane: str, decide: Callable[[LaneSeen], ReadAgain | P]) -> P:
        for _ in range(PASSES_TO_START_THE_PROXY):
            plan = decide(self.read(product, lane))
            if not isinstance(plan, ReadAgain):
                return plan
            self.carry_out(plan.actions)
        raise ProxyError(f"The proxy was started and still does not run, so {lane} of {product} was not changed.")

    def move(self, target: LaneCopy) -> MoveResult:
        fresh_copy_name = self.name_copy(target)
        with self.one_at_a_time:
            plan = self.plan(target.product, target.lane, lambda seen: decide_move(seen, target, fresh_copy_name))
            try:
                self.carry_out(plan.actions)
            except Refused as refusal:
                return refusal.answer
            return plan.answer

    def retire(self, product: str, lane: str) -> RetireResult:
        with self.one_at_a_time:
            plan = self.plan(product, lane, lambda seen: decide_retire(seen, product, lane))
            self.carry_out(plan.actions)
            return plan.answer

    def carry_out(self, actions: tuple[Action, ...]) -> None:
        for action in actions:
            self.take(action)

    def take(self, action: Action) -> None:
        match action:
            case StartProxy():
                self.proxy.start()
            case AwaitRoute(name=name, copy=copy_name):
                self.proxy.await_route(name, copy_name)
            case StartCopy(copy=copy, name=copy_name):
                self.host.start_copy(copy, copy_name)
            case Check(copy=copy_name, if_failed=if_failed):
                self.check_or_remove(copy_name, if_failed)
            case WriteRoute(name=name, copy=copy_name):
                self.proxy.write_route(name, copy_name)
            case Retire(copy=copy_name):
                self.host.retire_copy(copy_name)

    def check_or_remove(self, copy_name: str, if_failed: MoveResult) -> None:
        try:
            self.proxy.check_copy(copy_name)
        except CheckFailed as error:
            report = failure_report(error, self.host.read_log_tail(copy_name))
            self.host.remove_copy(copy_name)
            raise Refused(if_failed.model_copy(update={"error": report})) from error
