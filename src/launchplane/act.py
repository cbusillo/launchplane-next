import threading
from collections.abc import Callable
from typing import Protocol

from launchplane import decide
from launchplane.decide import Action, Done, Event, LaneRoute, LaneSeen, MoveResult, ProxyNotRunning, RetireResult, Stop
from launchplane.instance import LaneBuild, RunningInstance, new_container_name
from launchplane.proxy import CheckFailed

MOST_STEPS = 50


class InstanceHost(Protocol):
    def read_instances(self) -> list[RunningInstance]: ...
    def start_instance(self, target: LaneBuild, container: str) -> None: ...
    def remove_instance(self, container: str) -> None: ...
    def retire_instance(self, container: str) -> None: ...
    def read_log_tail(self, container: str) -> str: ...


class Proxy(Protocol):
    def start(self) -> None: ...
    def read_lane_route(self, web_address: str) -> ProxyNotRunning | LaneRoute: ...
    def await_route(self, web_address: str, container: str | None) -> None: ...
    def write_route(self, web_address: str, container: str) -> None: ...
    def remove_route(self, web_address: str) -> None: ...
    def check_instance(self, container: str) -> None: ...


class Stopped(Exception):
    pass


def failure_report(error: CheckFailed, log_tail: str) -> str:
    if not log_tail:
        return f"{error} The instance wrote no log."
    return f"{error}\nThe instance's last log lines:\n{log_tail}"


class Lanes:
    def __init__(self, host: InstanceHost, proxy: Proxy, name_container: Callable[[LaneBuild], str] = new_container_name) -> None:
        self.host = host
        self.proxy = proxy
        self.name_container = name_container
        self.one_at_a_time = threading.Lock()

    def read(self, product: str, lane: str) -> LaneSeen:
        route = self.proxy.read_lane_route(decide.web_address_of(product, lane))
        instances = tuple(
            instance for instance in self.host.read_instances() if (instance.product, instance.lane) == (product, lane)
        )
        return LaneSeen(instances=instances, route=route)

    def move(self, target: LaneBuild) -> MoveResult:
        with self.one_at_a_time:
            return self.settle(
                target.product,
                target.lane,
                lambda seen, done: decide.decide_move(seen, target, self.name_container(target), done),
            )

    def retire(self, product: str, lane: str) -> RetireResult:
        with self.one_at_a_time:
            return self.settle(product, lane, lambda seen, done: decide.decide_retire(seen, product, lane, done))

    def settle[Answer](
        self, product: str, lane: str, next_step: Callable[[LaneSeen, tuple[Event, ...]], Action | Done[Answer] | Stop]
    ) -> Answer:
        done: list[Event] = []
        while True:
            step = next_step(self.read(product, lane), tuple(done))
            if isinstance(step, Done):
                return step.answer
            if isinstance(step, Stop):
                raise Stopped(step.reason)
            if len(done) == MOST_STEPS:
                raise Stopped(
                    f"{lane} of {product} was not finished after {MOST_STEPS} steps, so Launchplane stopped before {step}. "
                    "What was done stays done, and asking again goes on from there."
                )
            done.append(self.take(step))

    def take(self, action: Action) -> Event:
        match action:
            case decide.StartProxy():
                self.proxy.start()
            case decide.AwaitRoute(web_address=web_address, container=container):
                self.proxy.await_route(web_address, container)
            case decide.StartInstance(target=target, container=container):
                self.host.start_instance(target, container)
            case decide.Check(container=container):
                return self.check(container)
            case decide.WriteRoute(web_address=web_address, container=container):
                self.proxy.write_route(web_address, container)
            case decide.RemoveRoute(web_address=web_address):
                self.proxy.remove_route(web_address)
            case decide.Retire(container=container):
                self.host.retire_instance(container)
            case decide.RemoveInstance(container=container):
                self.host.remove_instance(container)
        return action

    def check(self, container: str) -> Event:
        try:
            self.proxy.check_instance(container)
        except CheckFailed as error:
            return decide.CheckRefused(container, failure_report(error, self.host.read_log_tail(container)))
        return decide.Check(container)
