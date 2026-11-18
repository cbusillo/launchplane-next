import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
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
    LaneReading,
    OnHost,
    ProxySeen,
    Reading,
    Verdict,
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
from launchplane.telling import NOBODY, TO_PUT_BACK, Witness, named, tell_nobody, told

MOST_STEPS = 50
# A host or proxy that cannot be read is asked again this often, this far apart, before the call stops over it.
MOST_READS_MISSED = 5
SECONDS_BETWEEN_READS = 2.0
type Lane = tuple[str, str]


class InstanceHost(Protocol):
    def read(self, lane: Lane | None = None) -> OnHost: ...
    def start_instance(self, target: LaneBuild, container: str, use: DataSetUse | None, generation: str | None = None) -> None: ...
    def start_background_service(
        self, target: LaneBuild, instance: str, use: DataSetUse | None, command: list[str], generation: str | None = None
    ) -> None: ...
    def await_background_service(self, container: str, steady_seconds: float) -> None: ...
    def create_data_set_network(self, product: str, lane: str, data_set: str, generation: str | None = None) -> None: ...
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
    def switch(self, web_address: str, container: str, generation: str) -> Verdict: ...
    def abandon(self, web_address: str, generation: str) -> Verdict: ...
    def discard(self, web_address: str, container: str, made_by: str) -> Verdict: ...
    def remove_route(self, web_address: str) -> None: ...
    def check_instance(self, container: str, health_path: str) -> None: ...
    def show_back_soon(self, web_address: str) -> None: ...
    def why_web_address_fails(self, web_address: str) -> str | None: ...


type ProductReader = Callable[[str], Product]
type KeptReader = Callable[[], list[Kept]]
type CancelsReader = Callable[[], list[Request]]


class Stopped(Exception):
    pass


# A cancel was carried out and the lane is put back, so the call has no answer of its own to give.
class Cancelled(Exception):
    def __init__(self, put_back: PutBack) -> None:
        super().__init__("The call was cancelled and its lane put back.")
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
    cancels: list[Request] = field(default_factory=list[Request])


def fresh_names(target: LaneBuild) -> FreshNames:
    return FreshNames(container=new_container_name(target), data_set=new_data_set_name())


type Take = Callable[[InstanceHost, Proxy, Any], object]

TAKE: dict[type, Take] = {
    steps.StartProxy: lambda _host, proxy, _step: proxy.start(),
    steps.AwaitRoute: lambda _host, proxy, step: proxy.await_route(step.web_address, step.container),
    steps.WriteRoute: lambda _host, proxy, step: proxy.write_route(step.web_address, step.container),
    steps.RemoveRoute: lambda _host, proxy, step: proxy.remove_route(step.web_address),
    steps.CreateDataSetNetwork: lambda host, _proxy, step: host.create_data_set_network(
        step.product, step.lane, step.data_set, step.generation
    ),
    steps.CreateDataSetVolume: lambda host, _proxy, step: host.create_data_set_volume(step.product, step.lane, step.data_set),
    steps.CreateDatabase: lambda host, _proxy, step: host.create_database(
        step.product, step.lane, step.data_set, step.network, step.volume, step.image
    ),
    steps.StartDatabase: lambda host, _proxy, step: host.start_database(step.container),
    steps.AwaitDatabase: lambda host, _proxy, step: host.await_database(step.container),
    steps.DeleteDataSet: lambda host, _proxy, step: host.delete_data_set(step.found),
    steps.StartInstance: lambda host, _proxy, step: host.start_instance(step.target, step.container, step.use, step.generation),
    steps.StartBackgroundService: lambda host, _proxy, step: host.start_background_service(
        step.target, step.instance, step.use, list(step.command), step.generation
    ),
    steps.Retire: lambda host, _proxy, step: host.retire_instance(step.container),
    steps.RemoveInstance: lambda host, _proxy, step: host.remove_instance(step.container),
    steps.RemoveLogins: lambda host, _proxy, step: host.remove_unheld_logins(step.database),
    steps.ShowBackSoon: lambda _host, proxy, step: proxy.show_back_soon(step.web_address),
    steps.StopPart: lambda host, _proxy, step: host.stop_part(step.container),
    steps.StopDatabase: lambda host, _proxy, step: host.stop_database(step.container),
}


# A change that puts a lane back is told before it is made, so it is in the record even if the record is lost just after.
def taken_and_told(action: Action, take: Callable[[Action], Event], witness: Witness, *, back: bool) -> Event:
    witness.hold()
    tell = witness.tell
    told_before = back and plan.change_made_by(action) is not None
    if told_before:
        tell(f"{named(action)}{TO_PUT_BACK}", None)
    try:
        event = take(action)
    except (ContainerHostError, ProxyError) as error:
        tell(named(action), str(error))
        raise
    if not told_before:
        tell(*told(action, event))
    return event


type Decide[Answer] = Callable[[Judgement, tuple[Event, ...], Cancel | None], Action | Done[Answer] | Done[PutBack] | Stop | TooLate]


# The host is read first and the cancel after it, so no step is decided from a reading older than the cancel it missed.
# From a cancel on, the same planner takes the steps back, with the lane judged for that, and the call ends by saying so;
# unless the host shows it is too late: then the cancel is refused and the call goes on as asked.
def settle[Answer](
    read: Callable[[], LaneReading],
    judged_as: Callable[[LaneReading, bool], Judgement],
    next_step: Decide[Answer],
    take: Callable[[Action], Event],
    witness: Witness,
) -> Answer:
    done: list[Event] = []
    cancel: Cancel | None = None
    while True:
        lane = read()
        if cancel is None and witness.cancel_asked():
            cancel = Cancel()
        back = plan.is_turned_back(cancel)
        step = next_step(judged_as(lane, back), tuple(done), cancel)
        if isinstance(step, TooLate):
            witness.refuse_cancel(step.reason)
            cancel = Cancel(refused=True)
            continue
        if isinstance(step, Done):
            if isinstance(step.answer, PutBack):
                raise Cancelled(step.answer)
            return step.answer
        if isinstance(step, Stop):
            raise Stopped(step.reason)
        if len(done) == MOST_STEPS:
            raise Stopped(
                f"{lane.lane} of {lane.product} was not finished after {MOST_STEPS} steps, so Launchplane "
                f"stopped before {step}. What was done stays done, and asking again goes on from there."
            )
        # A release that is renamed and not yet loaded is waited for as long as it takes: the wait is no step of the
        # call's count and is told once, however often it is taken.
        waiting = isinstance(step, steps.AwaitSwitch) and isinstance(done[-1] if done else None, steps.NotLoadedYet)
        event = taken_and_told(step, take, replace(witness, tell=tell_nobody) if waiting else witness, back=back)
        if waiting and isinstance(event, steps.NotLoadedYet):
            continue
        done.append(event)


class Lanes:
    def __init__(
        self,
        host: InstanceHost,
        proxy: Proxy,
        read_product: ProductReader = lambda _product: Product(),
        name_fresh: Callable[[LaneBuild], FreshNames] = fresh_names,
        read_unfinished: KeptReader = lambda: [],
        read_cancels: CancelsReader = lambda: [],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.sleep = sleep
        self.host = host
        self.proxy = proxy
        self.read_product = read_product
        self.name_fresh = name_fresh
        self.read_unfinished = read_unfinished
        self.read_cancels = read_cancels

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
        cancels, record_error = self.kept_or_why_not(self.read_cancels, unread_after)
        requests = [] if record_error else requests
        at_work = {(each.product, each.lane): each.work for each in (*before, *requests) if each.stands is Stands.WORKING}
        # With either reading of the record missing, which lane a request is at work on is not known for any lane.
        unread = None if record_error is None else WorkUnread(record_error)
        descriptions = {product: self.description_of(product) for product in sorted({product for product, _ in reading.lanes()})}
        judgements = [
            judge(reading.lane(product, lane), descriptions[product], unread or at_work.get((product, lane)))
            for product, lane in reading.lanes()
        ]
        return Status(reading, judgements, web_addresses_judged(reading), requests, record_error, cancels)

    def move(self, target: LaneBuild, generation: str, witness: Witness = NOBODY) -> MoveResult:
        product = self.read_product(target.product)
        return settle(
            *self.judging(target.product, target.lane, product, Work.MOVE),
            lambda judged, done, cancel: plan.decide_move(
                judged, target, product, self.name_fresh(target), done, generation, cancel
            ),
            self.take,
            witness,
        )

    # A cancel of a move, carried to the gate by whoever asks. Why it came too late, or None when it won there.
    def abandon(self, product: str, lane: str, generation: str) -> str | None:
        web_address = web_address_of(product, lane)
        verdict = self.proxy.abandon(web_address, generation)
        return plan.too_late_to_cancel(web_address) if verdict is Verdict.COMMITTED else None

    def retire(self, product: str, lane: str, delete_data_set: str | None = None, witness: Witness = NOBODY) -> RetireResult:
        return settle(
            *self.judging(product, lane, self.description_of(product), Work.RETIRE),
            lambda judged, done, cancel: plan.decide_retire(judged, delete_data_set, done, cancel),
            self.take,
            witness,
        )

    def begin_maintenance(self, product: str, lane: str, witness: Witness = NOBODY) -> MaintenanceResult:
        judging = self.judging(product, lane, self.read_product(product), Work.BEGIN_MAINTENANCE)
        return settle(*judging, maintenance.decide_begin, self.take, witness)

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
            *self.judging(product, lane, description, Work.END_MAINTENANCE, Work.BEGIN_MAINTENANCE),
            lambda judged, done, cancel: maintenance.decide_end(judged, description, done, cancel),
            self.take_behind_back_soon,
            witness,
        )

    # How a call reads its lane, and how it judges what it read. A move answers from what the status call sees, so it
    # asks its lane's web address too.
    def judging(
        self, product: str, lane: str, description: Product | ProductError, work: Work, turned_back: Work | None = None
    ) -> tuple[Callable[[], LaneReading], Callable[[LaneReading, bool], Judgement]]:
        scope = Scope(lane=(product, lane), web_addresses=work is Work.MOVE)
        return (
            self.patient(lambda: self.read(scope).lane(product, lane)),
            lambda read, back: judge(read, description, (turned_back or work) if back else work),
        )

    # A reading that fails is waited on and asked again, so no call spins on a host it cannot read, and after a few in a
    # row the call stops with the last failure.
    def patient(self, read: Callable[[], LaneReading]) -> Callable[[], LaneReading]:
        def read_or_wait_and_read_again() -> LaneReading:
            for _ in range(MOST_READS_MISSED - 1):
                try:
                    return read()
                except ContainerHostError, ProxyError:
                    self.sleep(SECONDS_BETWEEN_READS)
            return read()

        return read_or_wait_and_read_again

    # A check of its own, whatever the step was decided from: read from the routes the proxy has loaded at the moment
    # of the action, and from no stamp or mark. A retire has removed the route by then, and the old instance of a
    # move is retired once the proxy has loaded the switch, so neither is stopped by it.
    def refuse_to_take_from_visitors(self, action: Action) -> None:
        if not isinstance(action, steps.Retire | steps.RemoveInstance | steps.StopPart):
            return
        loaded = self.proxy.read().loaded
        sent_here = sorted(web_address for web_address, route in loaded.items() if route.container == action.container)
        if sent_here:
            raise Stopped(
                f"Launchplane was about to {named(action)}, and the proxy sends the visitors of {', '.join(sent_here)} to it, "
                "so it was left running. Nothing more was done."
            )

    def take(self, action: Action) -> Event:
        self.refuse_to_take_from_visitors(action)
        if isinstance(action, steps.Check):
            return self.check(action.container, action.health_path)
        if isinstance(action, steps.AwaitBackgroundService):
            return self.await_background_service(action.container)
        if isinstance(action, steps.StartStopped):
            return self.start_stopped(action)
        if isinstance(action, steps.Switch):
            return steps.Switched(action.container, self.proxy.switch(action.web_address, action.container, action.generation))
        if isinstance(action, steps.AwaitSwitch):
            return self.await_switch(action)
        if isinstance(action, steps.Discard):
            return self.discard(action)
        if isinstance(action, steps.Abandon):
            return steps.Abandoned(self.proxy.abandon(action.web_address, action.generation))
        TAKE[type(action)](self.host, self.proxy, action)
        return action

    # The gate is asked first, and its answer is what is acted on: an instance whose release it says is committed stays,
    # and only one it has refused every rename to is looked at for visitors and retired.
    def discard(self, action: steps.Discard) -> Event:
        # An instance no release made has no rename that could be on its way to it.
        made_by = action.generation
        verdict = Verdict.NONE if made_by is None else self.proxy.discard(action.web_address, action.container, made_by)
        if verdict is not Verdict.COMMITTED:
            self.refuse_to_take_from_visitors(steps.Retire(action.container))
            self.host.retire_instance(action.container)
        return steps.Discarded(action.container, verdict)

    def await_switch(self, action: steps.AwaitSwitch) -> Event:
        try:
            self.proxy.await_route(action.web_address, action.container)
        except ProxyError as error:
            return steps.NotLoadedYet(action.container, str(error))
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
