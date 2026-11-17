from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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
    WorkUnread,
    is_loaded_as_written,
    judge,
    web_addresses_judged,
)
from launchplane.product import Product, ProductError
from launchplane.proxy import CheckFailed, ProxyError
from launchplane.record import Kept, RecordError, Request, Stands
from launchplane.steps import (
    Action,
    Cancel,
    Done,
    Event,
    FreshNames,
    MaintenanceOutcome,
    MaintenanceResult,
    MoveResult,
    PutBack,
    RetireResult,
    Stop,
    TooLate,
)
from launchplane.telling import NOBODY, Witness, named, told

MOST_STEPS = 50
type Lane = tuple[str, str]


class InstanceHost(Protocol):
    def read(self, lane: Lane | None = None) -> OnHost: ...
    def start_instance(self, target: LaneBuild, container: str, use: DataSetUse | None, request: int | None = None) -> None: ...
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
type KeptReader = Callable[[], list[Kept]]
type CancelledReader = Callable[[], list[Request]]


class Stopped(Exception):
    pass


# A cancel was carried out and the lane is put back, so the call has no answer of its own to give.
class Cancelled(Exception):
    def __init__(self, put_back: PutBack) -> None:
        super().__init__(f"The call was cancelled: {put_back.put_back or 'nothing had to be put back'}.")
        self.put_back = put_back


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
    requests: list[Kept] = field(default_factory=list[Kept])
    record_error: str | None = None
    cancelled: list[Request] = field(default_factory=list[Request])


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
    steps.StartInstance: lambda host, _proxy, step: host.start_instance(step.target, step.container, step.use, step.request),
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


def taken_and_told(action: Action, take: Callable[[Action], Event], witness: Witness) -> Event:
    witness.hold()
    tell = witness.tell
    try:
        event = take(action)
    except (ContainerHostError, ProxyError) as error:
        tell(named(action), str(error))
        raise
    tell(*told(action, event))
    return event


type Decide[Answer] = Callable[[Judgement, tuple[Event, ...], Cancel | None], Action | Done[Answer] | Done[PutBack] | Stop | TooLate]


# A cancel is read between actions. From then on the same planner takes the steps back, with the lane read and judged
# for that, and the call ends by saying so; unless the host shows it is too late: then the cancel is refused and the
# call goes on as asked.
def settle[Answer](
    read_judged: Callable[[bool], Judgement],
    next_step: Decide[Answer],
    take: Callable[[Action], Event],
    witness: Witness,
) -> Answer:
    done: list[Event] = []
    turned_at: int | None = None
    refused = False
    while True:
        if turned_at is None and witness.cancel_asked():
            turned_at = len(done)
        cancel = None if turned_at is None else Cancel(tuple(done[turned_at:]), refused)
        judged = read_judged(plan.is_turned_back(cancel))
        step = next_step(judged, tuple(done), cancel)
        if isinstance(step, TooLate):
            witness.refuse_cancel(step.reason)
            refused = True
            continue
        if isinstance(step, Done):
            if isinstance(step.answer, PutBack):
                raise Cancelled(step.answer)
            return step.answer
        if isinstance(step, Stop):
            raise Stopped(step.reason)
        if len(done) == MOST_STEPS:
            raise Stopped(
                f"{judged.lane.lane} of {judged.lane.product} was not finished after {MOST_STEPS} steps, so Launchplane "
                f"stopped before {step}. What was done stays done, and asking again goes on from there."
            )
        done.append(taken_and_told(step, take, witness))


class Lanes:
    def __init__(
        self,
        host: InstanceHost,
        proxy: Proxy,
        read_product: ProductReader = lambda _product: Product(),
        name_fresh: Callable[[LaneBuild], FreshNames] = fresh_names,
        read_unfinished: KeptReader = lambda: [],
        read_cancelled: CancelledReader = lambda: [],
    ) -> None:
        self.host = host
        self.proxy = proxy
        self.read_product = read_product
        self.name_fresh = name_fresh
        self.read_unfinished = read_unfinished
        self.read_cancelled = read_cancelled

    # The containers are read before the proxy, so what is secret on the host is known before anything can be said of it.
    def read(self, scope: Scope) -> Reading:
        read_at = datetime.now(UTC)
        on_host = self.host.read(scope.lane) if scope.parts else None
        proxy = self.proxy.read()
        return Reading(read_at, proxy, on_host, self.web_address_failures(proxy, scope) if scope.web_addresses else {})

    # Asked at once, so ones that hang cost one limit and not one each. One the proxy sends to no product is not asked.
    def web_address_failures(self, proxy: ProxySeen, scope: Scope) -> dict[str, str | None]:
        only = None if scope.lane is None else web_address_of(*scope.lane)
        to_a_product = sorted(
            web_address
            for web_address, container in proxy.sends_visitors_to().items()
            if container != BACK_SOON and only in (None, web_address)
        )
        if not (proxy.runs and to_a_product):
            return {}
        with ThreadPoolExecutor(max_workers=len(to_a_product)) as pool:
            return dict(zip(to_a_product, pool.map(self.proxy.why_web_address_fails, to_a_product), strict=True))

    def description_of(self, product: str) -> Product | ProductError:
        try:
            return self.read_product(product)
        except ProductError as error:
            return error

    @staticmethod
    def kept_or_why_not[Read](read: Callable[[], list[Read]], unread: str | None = None) -> tuple[list[Read], str | None]:
        if unread is not None:
            return [], unread
        try:
            return read(), None
        except RecordError as error:
            return [], str(error)

    def read_status(self) -> Status:
        # A request taken up or ended while the host is read is at work on what was read.
        before, unread_before = self.kept_or_why_not(self.read_unfinished)
        reading = self.read(EVERY_LANE)
        requests, unread_after = self.kept_or_why_not(self.read_unfinished, unread_before)
        cancelled, record_error = self.kept_or_why_not(self.read_cancelled, unread_after)
        requests = [] if record_error else requests
        at_work = {(each.product, each.lane): each.work for each in (*before, *requests) if each.stands is Stands.WORKING}
        # With either reading of the record missing, which lane a request is at work on is not known for any lane.
        unread = None if record_error is None else WorkUnread(record_error)
        descriptions = {product: self.description_of(product) for product in sorted({product for product, _ in reading.lanes()})}
        judgements = [
            judge(reading.lane(product, lane), descriptions[product], unread or at_work.get((product, lane)))
            for product, lane in reading.lanes()
        ]
        return Status(reading, judgements, web_addresses_judged(reading), requests, record_error, cancelled)

    def move(self, target: LaneBuild, witness: Witness = NOBODY) -> MoveResult:
        product = self.read_product(target.product)
        return settle(
            self.judging(target.product, target.lane, product, Work.MOVE),
            lambda judged, done, cancel: plan.decide_move(
                judged, target, product, self.name_fresh(target), done, cancel, witness.request
            ),
            self.take,
            witness,
        )

    def retire(self, product: str, lane: str, delete_data_set: str | None = None, witness: Witness = NOBODY) -> RetireResult:
        return settle(
            self.judging(product, lane, self.description_of(product), Work.RETIRE),
            lambda judged, done, cancel: plan.decide_retire(judged, delete_data_set, done, cancel),
            self.take,
            witness,
        )

    def begin_maintenance(self, product: str, lane: str, witness: Witness = NOBODY) -> MaintenanceResult:
        judging = self.judging(product, lane, self.read_product(product), Work.BEGIN_MAINTENANCE)
        return settle(judging, maintenance.decide_begin, self.take, witness)

    def end_maintenance(self, product: str, lane: str, witness: Witness = NOBODY) -> MaintenanceResult:
        # The mark is looked at first: on a lane that is not in maintenance nothing is changed and no part is read.
        web_address = web_address_of(product, lane)
        route = self.read(ROUTES_ONLY).proxy.route_of(web_address)
        if not route.in_maintenance:
            if route.proxy_runs and not is_loaded_as_written(route):
                self.proxy.await_route(web_address, route.written)
            return maintenance.answer_for(product, lane, route, MaintenanceOutcome.NOT_IN_MAINTENANCE)
        description = self.read_product(product)
        return settle(
            # Turned back, it is stopping the lane again, and the lane is judged as one going into maintenance.
            self.judging(product, lane, description, Work.END_MAINTENANCE, Work.BEGIN_MAINTENANCE),
            lambda judged, done, cancel: maintenance.decide_end(judged, description, done, cancel),
            self.take_behind_back_soon,
            witness,
        )

    # A move answers from what the status call sees, so it asks its lane's web address too.
    def judging(
        self, product: str, lane: str, description: Product | ProductError, work: Work, turned_back: Work | None = None
    ) -> Callable[[bool], Judgement]:
        scope = Scope(lane=(product, lane), web_addresses=work is Work.MOVE)
        return lambda back: judge(self.read(scope).lane(product, lane), description, (turned_back or work) if back else work)

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
