import threading
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from launchplane import maintenance, plan, steps
from launchplane.container_host import PartStopped
from launchplane.docker_answers import ContainerHostError
from launchplane.instance import (
    BACK_SOON,
    DataSet,
    DataSetUse,
    LaneBuild,
    OnHost,
    ProxySeen,
    Reading,
    new_container_name,
    new_data_set_name,
    web_address_of,
)
from launchplane.judge import (
    BACKGROUND_SERVICE_STEADY_SECONDS,
    Judgement,
    WebAddressJudged,
    Work,
    is_loaded_as_written,
    judge,
    web_addresses_judged,
)
from launchplane.product import Product, ProductError
from launchplane.proxy import CheckFailed
from launchplane.steps import Action, Done, Event, FreshNames, MaintenanceOutcome, MaintenanceResult, MoveResult, RetireResult, Stop

MOST_STEPS = 50
type Lane = tuple[str, str]


class InstanceHost(Protocol):
    def read(self, lane: Lane | None = None) -> OnHost: ...
    def start_instance(self, target: LaneBuild, container: str, use: DataSetUse | None) -> None: ...
    def start_background_service(self, target: LaneBuild, instance: str, use: DataSetUse | None, command: list[str]) -> None: ...
    def await_background_service(self, container: str, steady_seconds: float) -> None: ...
    def create_data_set_network(self, product: str, lane: str, data_set: str) -> None: ...
    def create_data_set_volume(self, product: str, lane: str, data_set: str) -> None: ...
    def create_database(self, product: str, lane: str, data_set: str, network: str, volume: str, image: str) -> None: ...
    def start_database(self, container: str) -> None: ...
    def await_database(self, container: str) -> None: ...
    def start_part(self, container: str) -> None: ...
    def stop_part(self, container: str) -> None: ...
    def stop_database(self, container: str) -> None: ...
    def delete_data_set(self, found: DataSet) -> None: ...
    def remove_instance(self, container: str) -> None: ...
    def retire_instance(self, container: str) -> None: ...
    def remove_unheld_logins(self, database: str) -> None: ...
    def read_log_tail(self, container: str) -> str: ...


class Proxy(Protocol):
    def start(self) -> None: ...
    def read(self) -> ProxySeen: ...
    def await_route(self, web_address: str, container: str | None) -> None: ...
    def write_route(self, web_address: str, container: str) -> None: ...
    def remove_route(self, web_address: str) -> None: ...
    def check_instance(self, container: str, health_path: str) -> None: ...
    def show_back_soon(self, web_address: str) -> None: ...
    def why_web_address_fails(self, web_address: str) -> str | None: ...


type ProductReader = Callable[[str], Product]
type AtWork = tuple[Lane, Work] | None


class Stopped(Exception):
    pass


# What a caller needs read. A reading never holds more than was asked for, and never stands for what was not.
@dataclass(frozen=True)
class Scope:
    parts: bool = True
    # The one lane whose containers, volumes and database servers are read, or every lane's.
    lane: Lane | None = None
    web_addresses: bool = False


ROUTES_ONLY = Scope(parts=False)
EVERY_LANE = Scope(web_addresses=True)


@dataclass(frozen=True)
class Status:
    reading: Reading
    lanes: list[Judgement]
    web_addresses: list[WebAddressJudged]


def fresh_names(target: LaneBuild) -> FreshNames:
    return FreshNames(container=new_container_name(target), data_set=new_data_set_name())


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
    steps.RemoveLogins: lambda host, _proxy, step: host.remove_unheld_logins(step.database),
    steps.ShowBackSoon: lambda _host, proxy, step: proxy.show_back_soon(step.web_address),
    steps.StopPart: lambda host, _proxy, step: host.stop_part(step.container),
    steps.StopDatabase: lambda host, _proxy, step: host.stop_database(step.container),
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
        self.at_work: AtWork = None

    @contextmanager
    def at_work_on(self, product: str, lane: str, work: Work) -> Generator[None]:
        with self.one_at_a_time:
            self.at_work = ((product, lane), work)
            try:
                yield
            finally:
                self.at_work = None

    # The containers are read before the proxy, so what is secret on the host is known before anything can be said of it.
    def read(self, scope: Scope) -> Reading:
        read_at = datetime.now(UTC)
        on_host = self.host.read(scope.lane) if scope.parts else None
        proxy = self.proxy.read()
        return Reading(read_at, proxy, on_host, self.web_address_failures(proxy) if scope.web_addresses else {})

    # Asked at once, so ones that hang cost one limit and not one each. One the proxy sends to no product is not asked.
    def web_address_failures(self, proxy: ProxySeen) -> dict[str, str | None]:
        to_a_product = sorted(web_address for web_address, container in proxy.sends_visitors_to().items() if container != BACK_SOON)
        if not (proxy.runs and to_a_product):
            return {}
        with ThreadPoolExecutor(max_workers=len(to_a_product)) as pool:
            return dict(zip(to_a_product, pool.map(self.proxy.why_web_address_fails, to_a_product), strict=True))

    def description_of(self, product: str) -> Product | ProductError:
        try:
            return self.read_product(product)
        except ProductError as error:
            return error

    def read_status(self) -> Status:
        # A call that starts or ends while the host is read is at work on what was read.
        before = self.at_work
        reading = self.read(EVERY_LANE)
        at_work = dict(each for each in (before, self.at_work) if each is not None)
        descriptions = {product: self.description_of(product) for product in sorted({product for product, _ in reading.lanes()})}
        judgements = [
            judge(reading.lane(product, lane), descriptions[product], at_work.get((product, lane)))
            for product, lane in reading.lanes()
        ]
        return Status(reading, judgements, web_addresses_judged(reading))

    def move(self, target: LaneBuild) -> MoveResult:
        with self.at_work_on(target.product, target.lane, Work.MOVE):
            product = self.read_product(target.product)
            return self.settle(
                self.judging(target.product, target.lane, product, Work.MOVE),
                lambda judged, done: plan.decide_move(judged, target, product, self.name_fresh(target), done),
            )

    def retire(self, product: str, lane: str, delete_data_set: str | None = None) -> RetireResult:
        with self.at_work_on(product, lane, Work.RETIRE):
            return self.settle(
                self.judging(product, lane, self.description_of(product), Work.RETIRE),
                lambda judged, done: plan.decide_retire(judged, delete_data_set, done),
            )

    def begin_maintenance(self, product: str, lane: str) -> MaintenanceResult:
        with self.at_work_on(product, lane, Work.BEGIN_MAINTENANCE):
            return self.settle(
                self.judging(product, lane, self.read_product(product), Work.BEGIN_MAINTENANCE), maintenance.decide_begin
            )

    def end_maintenance(self, product: str, lane: str) -> MaintenanceResult:
        with self.at_work_on(product, lane, Work.END_MAINTENANCE):
            # The mark is looked at first: on a lane that is not in maintenance nothing is changed and no part is read.
            web_address = web_address_of(product, lane)
            route = self.read(ROUTES_ONLY).proxy.route_of(web_address)
            if not route.in_maintenance:
                if route.proxy_runs and not is_loaded_as_written(route):
                    self.proxy.await_route(web_address, route.written)
                return maintenance.answer_for(product, lane, route, MaintenanceOutcome.NOT_IN_MAINTENANCE)
            description = self.read_product(product)
            return self.settle(
                self.judging(product, lane, description, Work.END_MAINTENANCE),
                lambda judged, done: maintenance.decide_end(judged, description, done),
                self.take_behind_back_soon,
            )

    def judging(self, product: str, lane: str, description: Product | ProductError, work: Work) -> Callable[[], Judgement]:
        return lambda: judge(self.read(Scope(lane=(product, lane))).lane(product, lane), description, work)

    def settle[Answer](
        self,
        read_judged: Callable[[], Judgement],
        next_step: Callable[[Judgement, tuple[Event, ...]], Action | Done[Answer] | Stop],
        take: Callable[[Action], Event] | None = None,
    ) -> Answer:
        done: list[Event] = []
        while True:
            judged = read_judged()
            step = next_step(judged, tuple(done))
            if isinstance(step, Done):
                return step.answer
            if isinstance(step, Stop):
                raise Stopped(step.reason)
            if len(done) == MOST_STEPS:
                raise Stopped(
                    f"{judged.lane.lane} of {judged.lane.product} was not finished after {MOST_STEPS} steps, so Launchplane "
                    f"stopped before {step}. What was done stays done, and asking again goes on from there."
                )
            done.append((take or self.take)(step))

    def take(self, action: Action) -> Event:
        if isinstance(action, steps.Check):
            return self.check(action.container, action.health_path)
        if isinstance(action, steps.AwaitBackgroundService):
            return self.await_background_service(action.container)
        if isinstance(action, steps.StartStopped):
            return self.start_stopped(action)
        TAKE[type(action)](self.host, self.proxy, action)
        return action

    def start_stopped(self, action: steps.StartStopped) -> Event:
        try:
            self.host.start_part(action.container)
        except ContainerHostError as error:
            return steps.StartRefused(action.container, str(error))
        return action

    # A part that fails behind the back soon page is stopped before its log is read, so its refusal carries no log yet.
    def take_behind_back_soon(self, action: Action) -> Event:
        if isinstance(action, steps.Check):
            return self.check(action.container, action.health_path, with_log=False)
        if isinstance(action, steps.AwaitBackgroundService):
            return self.await_background_service(action.container, with_log=False)
        if isinstance(action, steps.ReadLog):
            return self.read_log(action.container)
        return self.take(action)

    def read_log(self, container: str) -> Event:
        try:
            return steps.LogRead(container, self.host.read_log_tail(container))
        except ContainerHostError as error:
            return steps.LogRead(container, "", unreadable=str(error))

    def refusal(self, error: CheckFailed | PartStopped, container: str, *, with_log: bool) -> str:
        return plan.failure_report(str(error), self.host.read_log_tail(container)) if with_log else str(error)

    def check(self, container: str, health_path: str, *, with_log: bool = True) -> Event:
        try:
            self.proxy.check_instance(container, health_path)
        except CheckFailed as error:
            return steps.CheckRefused(container, self.refusal(error, container, with_log=with_log))
        return steps.Check(container, health_path)

    def await_background_service(self, container: str, *, with_log: bool = True) -> Event:
        try:
            self.host.await_background_service(container, BACKGROUND_SERVICE_STEADY_SECONDS)
        except PartStopped as error:
            return steps.BackgroundServiceRefused(container, self.refusal(error, container, with_log=with_log))
        return steps.AwaitBackgroundService(container)
