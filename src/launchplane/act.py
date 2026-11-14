import threading
from collections.abc import Callable
from typing import Protocol

from launchplane import decide
from launchplane.decide import Action, Done, Event, LaneRoute, LaneSeen, MoveResult, ProxyNotRunning, RetireResult
from launchplane.proxy import CheckFailed
from launchplane.running_copy import LaneCopy, RunningCopy, new_copy_name

MOST_STEPS = 50


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
    def write_route(self, name: str, copy_name: str) -> None: ...
    def remove_route(self, name: str) -> None: ...
    def check_copy(self, copy_name: str) -> None: ...


class NotSettled(Exception):
    pass


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
        route = self.proxy.read_lane_route(decide.lane_name(product, lane))
        copies = tuple(copy for copy in self.host.read_copies() if (copy.product, copy.lane) == (product, lane))
        return LaneSeen(copies=copies, route=route)

    def move(self, target: LaneCopy) -> MoveResult:
        with self.one_at_a_time:
            return self.settle(
                target.product, target.lane, lambda seen, done: decide.decide_move(seen, target, self.name_copy(target), done)
            )

    def retire(self, product: str, lane: str) -> RetireResult:
        with self.one_at_a_time:
            return self.settle(product, lane, lambda seen, done: decide.decide_retire(seen, product, lane, done))

    def settle[Answer](
        self, product: str, lane: str, next_step: Callable[[LaneSeen, tuple[Event, ...]], Action | Done[Answer]]
    ) -> Answer:
        done: list[Event] = []
        for _ in range(MOST_STEPS):
            step = next_step(self.read(product, lane), tuple(done))
            if isinstance(step, Done):
                return step.answer
            done.append(self.take(step))
        raise NotSettled(
            f"{lane} of {product} did not settle within {MOST_STEPS} steps, so Launchplane stopped. "
            f"The last step was {done[-1]}. It is safe to ask again."
        )

    def take(self, action: Action) -> Event:
        match action:
            case decide.StartProxy():
                self.proxy.start()
            case decide.AwaitRoute(name=name, copy=copy_name):
                self.proxy.await_route(name, copy_name)
            case decide.StartCopy(copy=copy, name=copy_name):
                self.host.start_copy(copy, copy_name)
            case decide.Check(copy=copy_name):
                return self.check(copy_name)
            case decide.WriteRoute(name=name, copy=copy_name):
                self.proxy.write_route(name, copy_name)
            case decide.RemoveRoute(name=name):
                self.proxy.remove_route(name)
            case decide.Retire(copy=copy_name):
                self.host.retire_copy(copy_name)
            case decide.RemoveCopy(copy=copy_name):
                self.host.remove_copy(copy_name)
        return action

    def check(self, copy_name: str) -> Event:
        try:
            self.proxy.check_copy(copy_name)
        except CheckFailed as error:
            return decide.CheckRefused(copy_name, failure_report(error, self.host.read_log_tail(copy_name)))
        return decide.Check(copy_name)
