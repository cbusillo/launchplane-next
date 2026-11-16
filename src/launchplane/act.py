import threading
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from launchplane import plan, steps
from launchplane.container_host import HasDatabase, PartStopped
from launchplane.docker_answers import OnHost
from launchplane.instance import (
    DataSet,
    DataSetUse,
    LaneBuild,
    LaneRoute,
    LaneSeen,
    LoadedRoute,
    ProxyNotRunning,
    RouteStatus,
    new_container_name,
    new_data_set_name,
    web_address_of,
)
from launchplane.judge import BACKGROUND_SERVICE_STEADY_SECONDS, LaneVerdict, lane_verdict
from launchplane.product import Product, ProductError
from launchplane.proxy import CheckFailed
from launchplane.steps import Action, Done, Event, FreshNames, MoveResult, RetireResult, Stop

MOST_STEPS = 50


class InstanceHost(Protocol):
    def read(self, has_database: HasDatabase | None = None) -> OnHost: ...
    def start_instance(self, target: LaneBuild, container: str, use: DataSetUse | None) -> None: ...
    def start_background_service(self, target: LaneBuild, instance: str, use: DataSetUse | None, command: list[str]) -> None: ...
    def await_background_service(self, container: str, steady_seconds: float) -> None: ...
    def create_data_set_network(self, product: str, lane: str, data_set: str) -> None: ...
    def create_data_set_volume(self, product: str, lane: str, data_set: str) -> None: ...
    def create_database(self, product: str, lane: str, data_set: str, network: str, volume: str, image: str) -> None: ...
    def start_database(self, container: str) -> None: ...
    def await_database(self, container: str) -> None: ...
    def delete_data_set(self, found: DataSet) -> None: ...
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


@dataclass(frozen=True)
class Judged:
    on_host: OnHost
    lanes: list[LaneVerdict]


def failure_report(error: CheckFailed | PartStopped, log_tail: str) -> str:
    if not log_tail:
        return f"{error} It wrote no log."
    return f"{error}\nIts last log lines:\n{log_tail}"


def fresh_names(target: LaneBuild) -> FreshNames:
    return FreshNames(container=new_container_name(target), data_set=new_data_set_name())


class PartOfLane(Protocol):
    @property
    def product(self) -> str: ...
    @property
    def lane(self) -> str | None: ...


def is_of_lane(product: str, lane: str, part: PartOfLane) -> bool:
    return (part.product, part.lane) == (product, lane)


def parts_of(on_host: OnHost) -> list[PartOfLane]:
    return [*on_host.instances, *on_host.background_services, *on_host.data_sets]


def lanes_on(on_host: OnHost) -> list[tuple[str, str]]:
    lanes: set[tuple[str, str]] = set()
    for part in parts_of(on_host):
        lane = part.lane
        if lane is not None:
            lanes.add((part.product, lane))
    return sorted(lanes)


def lane_seen(on_host: OnHost, product: str, lane: str, route: ProxyNotRunning | LaneRoute) -> LaneSeen:
    return LaneSeen(
        instances=tuple(instance for instance in on_host.instances if is_of_lane(product, lane, instance)),
        route=route,
        background_services=tuple(running for running in on_host.background_services if is_of_lane(product, lane, running)),
        data_sets=tuple(data_set for data_set in on_host.data_sets if is_of_lane(product, lane, data_set)),
        network_quiet={network.name: network.is_quiet for network in on_host.networks},
    )


def verdict_on(
    on_host: OnHost, product: str, lane: str, routes: Mapping[str, str], description: Product | ProductError, a_move_is_at_work: bool
) -> LaneVerdict:
    if isinstance(description, ProductError):
        return LaneVerdict(product=product, lane=lane, a_move_is_at_work=a_move_is_at_work, problems=[str(description)])
    receiving = routes.get(web_address_of(product, lane))
    route = LaneRoute(None if receiving is None else LoadedRoute(receiving, RouteStatus.ENABLED), receiving)
    seen = lane_seen(on_host, product, lane, route)
    return lane_verdict(seen, description, product, lane, a_move_is_at_work=a_move_is_at_work)


type Take = Callable[[InstanceHost, Proxy, Any], None]

TAKE: dict[type, Take] = {
    steps.StartProxy: lambda _host, proxy, _step: proxy.start(),
    steps.AwaitRoute: lambda _host, proxy, step: proxy.await_route(step.web_address, step.container),
    steps.WriteRoute: lambda _host, proxy, step: proxy.write_route(step.web_address, step.container),
    steps.RemoveRoute: lambda _host, proxy, step: proxy.remove_route(step.web_address),
    steps.CreateDataSetNetwork: lambda host, _proxy, step: host.create_data_set_network(step.product, step.lane, step.data_set),
    steps.CreateDataSetVolume: lambda host, _proxy, step: host.create_data_set_volume(step.product, step.lane, step.data_set),
    steps.CreateDatabase: lambda host, _proxy, step: host.create_database(
        step.product, step.lane, step.data_set, step.network, step.volume, step.image
    ),
    steps.StartDatabase: lambda host, _proxy, step: host.start_database(step.container),
    steps.AwaitDatabase: lambda host, _proxy, step: host.await_database(step.container),
    steps.DeleteDataSet: lambda host, _proxy, step: host.delete_data_set(step.found),
    steps.StartInstance: lambda host, _proxy, step: host.start_instance(step.target, step.container, step.use),
    steps.StartBackgroundService: lambda host, _proxy, step: host.start_background_service(
        step.target, step.instance, step.use, list(step.command)
    ),
    steps.Retire: lambda host, _proxy, step: host.retire_instance(step.container),
    steps.RemoveInstance: lambda host, _proxy, step: host.remove_instance(step.container),
}


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
        self.lane_being_moved: tuple[str, str] | None = None

    @contextmanager
    def at_work_on(self, product: str, lane: str) -> Generator[None]:
        with self.one_at_a_time:
            self.lane_being_moved = (product, lane)
            try:
                yield
            finally:
                self.lane_being_moved = None

    def read(self, product: str, lane: str, *, has_database: bool = False) -> LaneSeen:
        route = self.proxy.read_lane_route(web_address_of(product, lane))
        return lane_seen(self.host.read(lambda asked: has_database and asked == product), product, lane, route)

    def read_judged(self, routes: Mapping[str, str]) -> Judged:
        descriptions: dict[str, Product | ProductError] = {}

        def description_of(product: str) -> Product | ProductError:
            if product not in descriptions:
                try:
                    descriptions[product] = self.read_product(product)
                except ProductError as error:
                    descriptions[product] = error
            return descriptions[product]

        def has_database(product: str) -> bool:
            description = description_of(product)
            return not isinstance(description, ProductError) and description.database is not None

        # A move that starts or ends while the host is read is at work on what was read.
        moving = {self.lane_being_moved}
        on_host = self.host.read(has_database)
        moving.add(self.lane_being_moved)
        return Judged(
            on_host,
            [
                verdict_on(on_host, product, lane, routes, description_of(product), (product, lane) in moving)
                for product, lane in lanes_on(on_host)
            ],
        )

    def move(self, target: LaneBuild) -> MoveResult:
        with self.at_work_on(target.product, target.lane):
            product = self.read_product(target.product)
            return self.settle(
                lambda: self.read(target.product, target.lane, has_database=product.database is not None),
                lambda seen, done: plan.decide_move(seen, target, product, self.name_fresh(target), done),
                f"{target.lane} of {target.product}",
            )

    def retire(self, product: str, lane: str, delete_data_set: str | None = None) -> RetireResult:
        with self.at_work_on(product, lane):
            return self.settle(
                lambda: self.read(product, lane),
                lambda seen, done: plan.decide_retire(seen, product, lane, delete_data_set, done),
                f"{lane} of {product}",
            )

    def settle[Answer](
        self,
        read: Callable[[], LaneSeen],
        next_step: Callable[[LaneSeen, tuple[Event, ...]], Action | Done[Answer] | Stop],
        lane_name: str,
    ) -> Answer:
        done: list[Event] = []
        while True:
            step = next_step(read(), tuple(done))
            if isinstance(step, Done):
                return step.answer
            if isinstance(step, Stop):
                raise Stopped(step.reason)
            if len(done) == MOST_STEPS:
                raise Stopped(
                    f"{lane_name} was not finished after {MOST_STEPS} steps, so Launchplane stopped before {step}. "
                    "What was done stays done, and asking again goes on from there."
                )
            done.append(self.take(step))

    def take(self, action: Action) -> Event:
        if isinstance(action, steps.Check):
            return self.check(action.container, action.health_path)
        if isinstance(action, steps.AwaitBackgroundService):
            return self.await_background_service(action.container)
        TAKE[type(action)](self.host, self.proxy, action)
        return action

    def check(self, container: str, health_path: str) -> Event:
        try:
            self.proxy.check_instance(container, health_path)
        except CheckFailed as error:
            return steps.CheckRefused(container, failure_report(error, self.host.read_log_tail(container)))
        return steps.Check(container, health_path)

    def await_background_service(self, container: str) -> Event:
        try:
            self.host.await_background_service(container, BACKGROUND_SERVICE_STEADY_SECONDS)
        except PartStopped as error:
            return steps.BackgroundServiceRefused(container, failure_report(error, self.host.read_log_tail(container)))
        return steps.AwaitBackgroundService(container)
