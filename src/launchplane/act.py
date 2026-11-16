import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from launchplane import decide
from launchplane.container_host import DatabaseLink, OnHost, PartStopped, ServerOfProduct
from launchplane.data_set import DatabaseServer, postgres_server
from launchplane.decide import (
    Action,
    DataSetAction,
    DataSetUse,
    Done,
    Event,
    FreshNames,
    InstanceAction,
    LaneRoute,
    LaneSeen,
    LoadedRoute,
    MoveResult,
    ProxyAction,
    ProxyNotRunning,
    RetireResult,
    RouteStatus,
    Stop,
)
from launchplane.instance import DataSet, LaneBuild, new_container_name, new_data_set_name
from launchplane.product import Database, Product, ProductError
from launchplane.proxy import CheckFailed

MOST_STEPS = 50


class InstanceHost(Protocol):
    def read(self, probing: ServerOfProduct | None = None) -> OnHost: ...
    def start_instance(self, target: LaneBuild, container: str, link: DatabaseLink | None) -> None: ...
    def start_job(self, target: LaneBuild, instance: str, link: DatabaseLink | None, command: list[str]) -> None: ...
    def await_job(self, container: str, steady_seconds: float) -> None: ...
    def create_data_set_network(self, product: str, lane: str, data_set: str) -> None: ...
    def create_data_set_volume(self, product: str, lane: str, data_set: str) -> None: ...
    def create_database(self, product: str, lane: str, data_set: str, network: str, volume: str, server: DatabaseServer) -> None: ...
    def start_database(self, container: str, server: DatabaseServer) -> None: ...
    def await_database(self, container: str, server: DatabaseServer) -> None: ...
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


class LaneVerdict(BaseModel):
    product: str
    lane: str
    problems: list[str]
    release_under_way: list[str] = []
    seconds_since_anything_was_made: float | None = None


@dataclass(frozen=True)
class Judged:
    on_host: OnHost
    lanes: list[LaneVerdict]


def failure_report(error: CheckFailed | PartStopped, log_tail: str) -> str:
    if not log_tail:
        return f"{error} It wrote no log."
    return f"{error}\nIts last log lines:\n{log_tail}"


def link_for(use: DataSetUse | None) -> DatabaseLink | None:
    if use is None:
        return None
    return DatabaseLink(use.data_set, use.network, use.database_container, postgres_server(use.database))


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
    return [*on_host.instances, *on_host.jobs, *on_host.data_sets]


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
        jobs=tuple(job for job in on_host.jobs if is_of_lane(product, lane, job)),
        data_sets=tuple(data_set for data_set in on_host.data_sets if is_of_lane(product, lane, data_set)),
        network_quiet={network.name: network.is_quiet for network in on_host.networks},
    )


def verdict_on(
    on_host: OnHost, product: str, lane: str, routes: Mapping[str, str], description: Product | ProductError
) -> LaneVerdict:
    if isinstance(description, ProductError):
        return LaneVerdict(product=product, lane=lane, problems=[str(description)])
    receiving = routes.get(decide.web_address_of(product, lane))
    route = LaneRoute(None if receiving is None else LoadedRoute(receiving, RouteStatus.ENABLED), receiving)
    verdict = decide.lane_verdict(lane_seen(on_host, product, lane, route), description)
    return LaneVerdict(
        product=product,
        lane=lane,
        problems=verdict.problems,
        release_under_way=verdict.release_under_way,
        seconds_since_anything_was_made=verdict.seconds_since_anything_was_made,
    )


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

    def read(self, product: str, lane: str, database: Database | None = None) -> LaneSeen:
        route = self.proxy.read_lane_route(decide.web_address_of(product, lane))
        server = None if database is None else postgres_server(database)
        return lane_seen(self.host.read(lambda asked: server if asked == product else None), product, lane, route)

    def read_judged(self, routes: Mapping[str, str]) -> Judged:
        descriptions: dict[str, Product | ProductError] = {}

        def description_of(product: str) -> Product | ProductError:
            if product not in descriptions:
                try:
                    descriptions[product] = self.read_product(product)
                except ProductError as error:
                    descriptions[product] = error
            return descriptions[product]

        def server_of(product: str) -> DatabaseServer | None:
            description = description_of(product)
            return (
                None
                if isinstance(description, ProductError) or description.database is None
                else postgres_server(description.database)
            )

        on_host = self.host.read(server_of)
        lanes = lanes_on(on_host)
        return Judged(on_host, [verdict_on(on_host, product, lane, routes, description_of(product)) for product, lane in lanes])

    def move(self, target: LaneBuild) -> MoveResult:
        with self.one_at_a_time:
            product = self.read_product(target.product)
            return self.settle(
                lambda: self.read(target.product, target.lane, product.database),
                lambda seen, done: decide.decide_move(seen, target, product, self.name_fresh(target), done),
                f"{target.lane} of {target.product}",
            )

    def retire(self, product: str, lane: str, delete_data_set: str | None = None) -> RetireResult:
        with self.one_at_a_time:
            return self.settle(
                lambda: self.read(product, lane),
                lambda seen, done: decide.decide_retire(seen, product, lane, delete_data_set, done),
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
        match action:
            case decide.Check(container=container, health_path=health_path):
                return self.check(container, health_path)
            case decide.AwaitJob(container=container):
                return self.await_job(container)
            case decide.StartProxy() | decide.AwaitRoute() | decide.WriteRoute() | decide.RemoveRoute():
                self.take_on_proxy(action)
            case (
                decide.CreateDataSetNetwork()
                | decide.CreateDataSetVolume()
                | decide.CreateDatabase()
                | decide.StartDatabase()
                | decide.AwaitDatabase()
                | decide.DeleteDataSet()
            ):
                self.take_on_data_set(action)
            case _:
                self.take_on_instance(action)
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

    def take_on_data_set(self, action: DataSetAction) -> None:
        match action:
            case decide.CreateDataSetNetwork(product=product, lane=lane, data_set=data_set):
                self.host.create_data_set_network(product, lane, data_set)
            case decide.CreateDataSetVolume(product=product, lane=lane, data_set=data_set):
                self.host.create_data_set_volume(product, lane, data_set)
            case decide.CreateDatabase() as create:
                server = postgres_server(create.database)
                self.host.create_database(create.product, create.lane, create.data_set, create.network, create.volume, server)
            case decide.StartDatabase(container=container, database=database):
                self.host.start_database(container, postgres_server(database))
            case decide.AwaitDatabase(container=container, database=database):
                self.host.await_database(container, postgres_server(database))
            case decide.DeleteDataSet(found=found):
                self.host.delete_data_set(found)

    def take_on_instance(self, action: InstanceAction) -> None:
        match action:
            case decide.StartInstance(target=target, container=container, use=use):
                self.host.start_instance(target, container, link_for(use))
            case decide.StartJob(target=target, instance=instance, use=use, command=command):
                self.host.start_job(target, instance, link_for(use), list(command))
            case decide.Retire(container=container):
                self.host.retire_instance(container)
            case decide.RemoveInstance(container=container):
                self.host.remove_instance(container)

    def check(self, container: str, health_path: str) -> Event:
        try:
            self.proxy.check_instance(container, health_path)
        except CheckFailed as error:
            return decide.CheckRefused(container, failure_report(error, self.host.read_log_tail(container)))
        return decide.Check(container, health_path)

    def await_job(self, container: str) -> Event:
        try:
            self.host.await_job(container, decide.JOB_STEADY_SECONDS)
        except PartStopped as error:
            return decide.JobRefused(container, failure_report(error, self.host.read_log_tail(container)))
        return decide.AwaitJob(container)
