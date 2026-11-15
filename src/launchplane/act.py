import threading
from collections.abc import Callable
from typing import Protocol

from launchplane import decide
from launchplane.container_host import OnHost
from launchplane.data_set import DatabaseServer, postgres_server
from launchplane.decide import (
    Action,
    Done,
    Event,
    FreshNames,
    HostAction,
    LaneRoute,
    LaneSeen,
    MoveResult,
    ProxyAction,
    ProxyNotRunning,
    RetireResult,
    Stop,
)
from launchplane.instance import LaneBuild, new_container_name, new_data_set_name
from launchplane.product import Product
from launchplane.proxy import CheckFailed

MOST_STEPS = 50


class InstanceHost(Protocol):
    def read(self) -> OnHost: ...
    def start_instance(self, target: LaneBuild, container: str, data_set: str | None) -> None: ...
    def start_job(self, target: LaneBuild, instance: str, data_set: str | None, command: list[str]) -> None: ...
    def ensure_data_set(self, product: str, lane: str, data_set: str, server: DatabaseServer) -> None: ...
    def await_database(self, container: str, server: DatabaseServer) -> None: ...
    def delete_data_set(self, data_set: str) -> None: ...
    def remove_instance(self, container: str) -> None: ...
    def retire_instance(self, container: str) -> None: ...
    def read_log_tail(self, container: str) -> str: ...


class Proxy(Protocol):
    def start(self) -> None: ...
    def read_lane_route(self, web_address: str) -> ProxyNotRunning | LaneRoute: ...
    def await_route(self, web_address: str, container: str | None) -> None: ...
    def write_route(self, web_address: str, container: str) -> None: ...
    def remove_route(self, web_address: str) -> None: ...
    def check_instance(self, container: str, health_path: str) -> None: ...


type ProductReader = Callable[[str], Product]


class Stopped(Exception):
    pass


def failure_report(error: CheckFailed, log_tail: str) -> str:
    if not log_tail:
        return f"{error} The instance wrote no log."
    return f"{error}\nThe instance's last log lines:\n{log_tail}"


def fresh_names(target: LaneBuild) -> FreshNames:
    return FreshNames(container=new_container_name(target), data_set=new_data_set_name())


class PartOfLane(Protocol):
    @property
    def product(self) -> str: ...
    @property
    def lane(self) -> str | None: ...


def is_of_lane(product: str, lane: str, part: PartOfLane) -> bool:
    return (part.product, part.lane) == (product, lane)


class Lanes:
    def __init__(
        self,
        host: InstanceHost,
        proxy: Proxy,
        read_product: ProductReader = lambda _product: Product(),
        name_fresh: Callable[[LaneBuild], FreshNames] = fresh_names,
    ) -> None:
        self.host = host
        self.proxy = proxy
        self.read_product = read_product
        self.name_fresh = name_fresh
        self.one_at_a_time = threading.Lock()

    def read(self, product: str, lane: str) -> LaneSeen:
        route = self.proxy.read_lane_route(decide.web_address_of(product, lane))
        on_host = self.host.read()
        return LaneSeen(
            instances=tuple(instance for instance in on_host.instances if is_of_lane(product, lane, instance)),
            route=route,
            jobs=tuple(job for job in on_host.jobs if is_of_lane(product, lane, job)),
            data_sets=tuple(data_set for data_set in on_host.data_sets if is_of_lane(product, lane, data_set)),
        )

    def move(self, target: LaneBuild) -> MoveResult:
        with self.one_at_a_time:
            product = self.read_product(target.product)
            return self.settle(
                target.product,
                target.lane,
                lambda seen, done: decide.decide_move(seen, target, product, self.name_fresh(target), done),
            )

    def retire(self, product: str, lane: str, delete_data_set: str | None = None) -> RetireResult:
        with self.one_at_a_time:
            return self.settle(product, lane, lambda seen, done: decide.decide_retire(seen, product, lane, delete_data_set, done))

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
            case decide.Check(container=container, health_path=health_path):
                return self.check(container, health_path)
            case decide.StartProxy() | decide.AwaitRoute() | decide.WriteRoute() | decide.RemoveRoute():
                self.take_on_proxy(action)
            case _:
                self.take_on_host(action)
        return action

    def take_on_proxy(self, action: ProxyAction) -> None:
        match action:
            case decide.StartProxy():
                self.proxy.start()
            case decide.AwaitRoute(web_address=web_address, container=container):
                self.proxy.await_route(web_address, container)
            case decide.WriteRoute(web_address=web_address, container=container):
                self.proxy.write_route(web_address, container)
            case decide.RemoveRoute(web_address=web_address):
                self.proxy.remove_route(web_address)

    def take_on_host(self, action: HostAction) -> None:
        match action:
            case decide.EnsureDataSet(product=product, lane=lane, data_set=data_set, database=database):
                self.host.ensure_data_set(product, lane, data_set, postgres_server(database))
            case decide.AwaitDatabase(container=container, database=database):
                self.host.await_database(container, postgres_server(database))
            case decide.StartInstance(target=target, container=container, data_set=data_set):
                self.host.start_instance(target, container, data_set)
            case decide.StartJob(target=target, instance=instance, data_set=data_set, command=command):
                self.host.start_job(target, instance, data_set, list(command))
            case decide.Retire(container=container):
                self.host.retire_instance(container)
            case decide.RemoveInstance(container=container):
                self.host.remove_instance(container)
            case decide.DeleteDataSet(data_set=data_set):
                self.host.delete_data_set(data_set)

    def check(self, container: str, health_path: str) -> Event:
        try:
            self.proxy.check_instance(container, health_path)
        except CheckFailed as error:
            return decide.CheckRefused(container, failure_report(error, self.host.read_log_tail(container)))
        return decide.Check(container, health_path)
