from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, Protocol

from launchplane import maintenance, plan, steps
from launchplane.container_host import PartStopped
from launchplane.docker_answers import ContainerHostError, ContainerHostRefused, observing
from launchplane.gate import ALREADY, LOST
from launchplane.gate import Answer as GateAnswer
from launchplane.instance import (
    BACK_SOON,
    Conflicting,
    DataSet,
    DataSetUse,
    LaneBuild,
    LaneReading,
    OnHost,
    ProxySeen,
    Reading,
    Unread,
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
from launchplane.record import Kept, RecordError, Stands
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
from launchplane.telling import NOBODY, OF_A_PART, TO_PUT_BACK, Said, Witness, named, told

MOST_STEPS = 50
type Lane = tuple[str, str]


class InstanceHost(Protocol):
    def read(self, lane: Lane | None = None) -> OnHost: ...
    def start_instance(self, to: LaneBuild, name: str, use: DataSetUse | None, generation: str | None, by: Witness, /) -> None: ...
    def start_background_service(
        self, target: LaneBuild, instance: str, use: DataSetUse | None, command: list[str], generation: str | None, by: Witness
    ) -> None: ...
    def await_background_service(self, container: str, steady_seconds: float) -> None: ...
    def create_data_set_network(self, product: str, lane: str, data_set: str, generation: str | None = None) -> None: ...
    def create_data_set_volume(self, product: str, lane: str, data_set: str) -> None: ...
    def create_database(self, product: str, lane: str, data_set: str, network: str, volume: str, image: str) -> None: ...
    def start_database(self, container: str) -> None: ...
    def await_database(self, container: str) -> None: ...
    def start_part(self, container: str, by: Witness) -> None: ...
    def stop_part(self, container: str, by: Witness) -> None: ...
    def stop_database(self, container: str) -> None: ...
    def delete_data_set(self, found: DataSet) -> None: ...
    def retire_instance(self, container: str, by: Witness) -> None: ...
    def remove_unheld_logins(self, database: str, by: Witness) -> Unread | None: ...
    def read_log_tail(self, container: str) -> str: ...


# What is asked of the gate. Every call answers the verdict that then holds for the generation.
class AskedGate(Protocol):
    def verdict(self, web_address: str, generation: str) -> GateAnswer: ...
    def abandon(self, web_address: str, generation: str) -> GateAnswer: ...
    def discard(self, web_address: str, generation: str, attempt: int, candidate: str) -> GateAnswer: ...
    def fence(self, web_address: str, generation: str, attempt: int) -> GateAnswer: ...


class Proxy(Protocol):
    def is_running(self) -> bool: ...
    def start(self) -> None: ...
    def read(self) -> ProxySeen: ...
    def await_route(self, web_address: str, container: str | None, *, enabled: bool = False) -> None: ...
    def edit(self, web_address: str, generation: str, attempt: int, read: str, container: str | None) -> str: ...
    def switch(self, web_address: str, generation: str, attempt: int, candidate: str) -> GateAnswer: ...
    @property
    def gate(self) -> AskedGate: ...
    def check_instance(self, container: str, health_path: str) -> None: ...
    def why_web_address_fails(self, web_address: str) -> str | None: ...


type ProductReader = Callable[[str], Product]
type KeptReader = Callable[[], list[Kept]]


class Stopped(Exception):
    pass


# Nothing was done and nothing failed: the call is to be planned again from a later reading.
class Waiting(Stopped):
    pass


# A cancel won at the gate, and Docker refused to remove a part of its release, which is still there.
class CouldNotPutBack(Exception):
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
    cancels: list[Kept] = field(default_factory=list[Kept])
    # What the gate answers for each move being worked that a cancel was asked of. None is a gate that gave no answer.
    cancels_decided: dict[int, GateAnswer | None] = field(default_factory=dict[int, GateAnswer | None])


def fresh_names(target: LaneBuild) -> FreshNames:
    return FreshNames(container=new_container_name(target), data_set=new_data_set_name())


TAKE: dict[type, Callable[[InstanceHost, Proxy, Any, Witness], object]] = {
    steps.StartProxy: lambda _host, proxy, _step, _by: proxy.start(),
    steps.AwaitRoute: lambda _host, proxy, step, _by: proxy.await_route(step.web_address, step.container, enabled=step.enabled),
    steps.CreateDataSetNetwork: lambda host, _proxy, step, _by: host.create_data_set_network(
        step.product, step.lane, step.data_set, step.generation
    ),
    steps.CreateDataSetVolume: lambda host, _proxy, step, _by: host.create_data_set_volume(step.product, step.lane, step.data_set),
    steps.CreateDatabase: lambda host, _proxy, step, _by: host.create_database(
        step.product, step.lane, step.data_set, step.network, step.volume, step.image
    ),
    steps.StartDatabase: lambda host, _proxy, step, _by: host.start_database(step.container),
    steps.AwaitDatabase: observing()(lambda host, _proxy, step, _by: host.await_database(step.container)),
    steps.DeleteDataSet: lambda host, _proxy, step, _by: host.delete_data_set(step.found),
    steps.StartInstance: lambda host, _proxy, step, by: host.start_instance(
        step.target, step.container, step.use, step.generation, by
    ),
    steps.StartBackgroundService: lambda host, _proxy, step, by: host.start_background_service(
        step.target, step.instance, step.use, list(step.command), step.generation, by
    ),
    steps.Retire: lambda host, _proxy, step, by: host.retire_instance(step.container, by),
    steps.Abandon: lambda _host, proxy, step, _by: proxy.gate.abandon(step.web_address, step.generation),
    steps.StopPart: lambda host, _proxy, step, by: host.stop_part(step.container, by),
    steps.StopDatabase: lambda host, _proxy, step, _by: host.stop_database(step.container),
}


# Nothing is told as done before it is done: a change that put a lane back is marked so once the host has made it.
def taken_and_told(action: Action, take: Take, witness: Witness, *, back: bool) -> Event:
    witness.hold()
    if isinstance(action, steps.EditRoute | steps.Switch):
        return route_changed(action, take, witness)
    by = replace(witness, purpose=TO_PUT_BACK if back else "")
    try:
        event = take(action, by)
    except (ContainerHostError, ProxyError) as error:
        by.tell_of_step(named(action), str(error))
        raise
    did, refusal = told(action, event)
    if not isinstance(action, OF_A_PART) or refusal is not None:
        by.tell_of_step(f"{did}{TO_PUT_BACK}" if back and plan.change_made_by(event) is not None else did, refusal)
    return event


# A change of the route is written down before it is sent, and what the gate answered is written to the same line. An
# error is no answer: only a later reading of the routes file says whether the change was made.
def route_changed(action: steps.EditRoute | steps.Switch, take: Take, witness: Witness) -> Event:
    to = action.to if isinstance(action, steps.EditRoute) else action.container
    answered = witness.about_to(named(action), action.web_address, to)
    try:
        event = take(action, witness)
    except (ContainerHostError, ProxyError) as error:
        answered(None, str(error))
        raise
    refusal = told(action, event)[1]
    already = isinstance(event, steps.EditRoute) and event.already
    answered(Said.ALREADY if already else Said.CHANGED if refusal is None else Said.REFUSED, refusal)
    return event


type Take = Callable[[Action, Witness], Event]
type Decided[Answer] = Action | Done[Answer] | Done[PutBack] | Stop | TooLate | steps.CannotPutBack
type Decide[Answer] = Callable[[Judgement, tuple[Event, ...], Cancel | None], Decided[Answer]]


# The host is read first and the cancel after it, so no step is decided from a reading older than the cancel it missed.
# From a cancel on, the same planner takes the steps back, with the lane judged for that, and the call ends by saying so;
# unless the host shows it is too late: then the cancel is refused and the call goes on as asked.
def settle[Answer](
    read: Callable[[], LaneReading],
    judged_as: Callable[[LaneReading, bool], Judgement],
    next_step: Decide[Answer],
    take: Take,
    witness: Witness,
) -> Answer:
    done: list[Event] = []
    witness = replace(witness, credit=done.append)
    cancel: Cancel | None = None
    while True:
        lane = read()
        witness.look(lane.route, lane.made_by(witness.generation))
        # Two answers where a lane has room for one hold it: no winner is chosen, neither is touched, no call answers success.
        if isinstance(lane.route.loaded, Conflicting):
            raise Waiting(f"{lane.route.loaded}. The lane is held as it is until the proxy has one. Nothing more was done.")
        cancel = cancel or (Cancel() if witness.cancel_asked() else None)
        back = plan.is_turned_back(cancel)
        judged = judged_as(lane, back)
        step = next_step(judged, tuple(done), cancel)
        if isinstance(step, TooLate):
            witness.refuse_cancel(step.reason)
            cancel = Cancel(refused=True)
            continue
        unread = [each.report for each in done if isinstance(each, steps.LoginsUnread)]
        if isinstance(step, Done):
            if isinstance(step.answer, steps.Result):
                step.answer.logins_not_read = [*(finding.sentence for finding in judged.of_kind(*plan.LOGINS_UNKNOWN)), *unread]
            if isinstance(step.answer, PutBack):
                raise Cancelled(step.answer)
            return step.answer
        if isinstance(step, Stop):
            raise (Waiting if isinstance(step, steps.Wait) else Stopped)(" ".join([step.reason, *unread]))
        if isinstance(step, steps.CannotPutBack):
            raise CouldNotPutBack(step.reason)
        if sum(not isinstance(each, steps.Command) for each in done) == MOST_STEPS:
            raise Waiting(
                f"{lane.lane} of {lane.product} was not finished after {MOST_STEPS} steps, so Launchplane "
                f"stopped before {step}. What was done stays done, and asking again goes on from there."
            )
        done.append(taken_and_told(step, take, witness, back=back))


class Lanes:
    def __init__(
        self,
        host: InstanceHost,
        proxy: Proxy,
        read_product: ProductReader = lambda _product: Product(),
        name_fresh: Callable[[LaneBuild], FreshNames] = fresh_names,
        read_unfinished: KeptReader = lambda: [],
        read_ended: KeptReader = lambda: [],
    ) -> None:
        self.host = host
        self.proxy = proxy
        self.read_product = read_product
        self.name_fresh = name_fresh
        self.read_unfinished = read_unfinished
        self.read_ended = read_ended

    # The containers are read before the proxy, so what is secret on the host is known before anything can be said of it.
    @observing()
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

    def read_status(self, only: Lane | None = None) -> Status:
        # A request taken up or ended while the host is read is at work on what was read.
        before, unread_before = self.kept_or_why_not(self.read_unfinished)
        reading = self.read(EVERY_LANE if only is None else Scope(lane=only, web_addresses=True))
        requests, unread_after = self.kept_or_why_not(self.read_unfinished, unread_before)
        cancels, record_error = self.kept_or_why_not(self.read_ended, unread_after)
        # A request that ended between the two readings is shown once, as ended.
        ended = {each.id for each in cancels}
        requests = [] if record_error else [each for each in requests if each.id not in ended]
        at_work = {(each.product, each.lane): each.work for each in (*before, *requests) if each.stands is Stands.WORKING}
        # With either reading of the record missing, which lane a request is at work on is not known for any lane.
        unread = None if record_error is None else WorkUnread(record_error)
        descriptions = {product: self.description_of(product) for product in sorted({product for product, _ in reading.lanes()})}
        judgements = [
            judge(reading.lane(product, lane), descriptions[product], unread or at_work.get((product, lane)))
            for product, lane in reading.lanes()
        ]
        marked = [each for each in requests if (each.work, each.stands) == (Work.MOVE, Stands.WORKING) and each.cancel_asked_at]
        decided = {each.id: self.cancel_decided(each) for each in marked}
        return Status(reading, judgements, web_addresses_judged(reading), requests, record_error, cancels, decided)

    # Asked as the gate is, and nothing is started to ask it: a status call changes nothing.
    def cancel_decided(self, request: Kept) -> GateAnswer | None:
        try:
            return self.proxy.gate.verdict(web_address_of(request.product, request.lane), request.generation)
        except ContainerHostError, ProxyError:
            return None

    # The lane is read, and then the gate is asked, so the gate's answer is never older than the reading it is set beside.
    def move(self, target: LaneBuild, generation: str, attempt: int, witness: Witness = NOBODY) -> MoveResult:
        product = self.read_product(target.product)
        of = (target, product, generation, attempt, witness)
        return settle(
            *self.judging(target.product, target.lane, product, Work.MOVE),
            lambda judged, done, cancel: plan.decide_move(self.release(judged, done, *of), cancel),
            self.take,
            witness,
        )

    def release(
        self, judged: Judgement, done: tuple[Event, ...], to: LaneBuild, product: Product, generation: str, attempt: int, by: Witness
    ) -> plan.Move:
        verdict = self.gate(by).verdict(web_address_of(to.product, to.lane), generation)
        return plan.Move(judged, to, product, self.name_fresh(to), done, generation, attempt, verdict)

    # The gate is a command in the proxy's container, so a proxy that is absent or stopped is started before a move asks.
    def gate(self, by: Witness = NOBODY) -> AskedGate:
        if not self.proxy.is_running():
            by.hold()
            self.proxy.start()
        return self.proxy.gate

    # After a move failed with no verdict and its attempt was fenced. It reads nothing of the product's description:
    # what the generation made is known by its stamp.
    def clean_up(self, product: str, lane: str, generation: str, attempt: int, by: Witness = NOBODY, *, logins: bool) -> list[str]:
        of = (LaneBuild(product=product, lane=lane, build=""), Product(), generation, attempt, by)
        return settle(
            *self.judging(product, lane, self.description_of(product), Work.MOVE),
            lambda judged, done, _cancel: plan.decide_clean_up(self.release(judged, done, *of), logins=logins),
            self.take,
            by,
        ).logins_not_read

    # Before an ending that is no release is written: the gate confirms none, and no switch of this attempt or an earlier
    # one can rename after it. A host that refuses to run it has given no answer.
    @observing()
    def fence(self, product: str, lane: str, generation: str, attempt: int, by: Witness = NOBODY) -> GateAnswer:
        return self.gate(by).fence(web_address_of(product, lane), generation, attempt)

    # A cancel of a move, carried to the gate by whoever asks. Why it came too late, or None when it won there.
    def abandon(self, product: str, lane: str, generation: str) -> str | None:
        web_address = web_address_of(product, lane)
        answer = self.gate().abandon(web_address, generation)
        return plan.too_late_to_cancel(web_address) if answer.verdict is Verdict.COMMITTED else None

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
        # The mark is looked at first: on a lane that is not in maintenance nothing is changed and no part is read. A mark
        # that is no answer is not taken for no mark.
        web_address = web_address_of(product, lane)
        route = self.read(ROUTES_ONLY).proxy.route_of(web_address)
        witness.look(route, ())
        if not (route.in_maintenance or isinstance(route.file, Unread) or isinstance(route.loaded, Conflicting)):
            if route.proxy_runs and not is_loaded_as_written(route):
                self.proxy.await_route(web_address, route.written)
            return maintenance.answer_for(product, lane, route, MaintenanceOutcome.NOT_IN_MAINTENANCE)
        description = self.read_product(product)
        return settle(
            # Turned back, it is stopping the lane again, and the lane is judged as one going into maintenance.
            *self.judging(product, lane, description, Work.END_MAINTENANCE, Work.BEGIN_MAINTENANCE),
            lambda judged, done, cancel: maintenance.decide_end(judged, description, done, cancel),
            partial(self.take, behind_back_soon=True),
            witness,
        )

    # How a call reads its lane, and how it judges what it read. A move answers from what the status call sees, so it
    # asks its lane's web address too.
    def judging(
        self, product: str, lane: str, description: Product | ProductError, work: Work, turned_back: Work | None = None
    ) -> tuple[Callable[[], LaneReading], Callable[[LaneReading, bool], Judgement]]:
        scope = Scope(lane=(product, lane), web_addresses=work is Work.MOVE)
        return (
            lambda: self.read(scope).lane(product, lane),
            lambda read, back: judge(read, description, (turned_back or work) if back else work),
        )

    # A check of its own, whatever the step was decided from: read from the routes the proxy has loaded at the moment
    # of the action, and from no stamp or mark. A retire has removed the route by then, and the old instance of a
    # move is retired once the proxy has loaded the switch, so neither is stopped by it.
    def refuse_to_take_from_visitors(self, action: Action) -> None:
        if isinstance(action, steps.Retire | steps.StopPart | steps.StopDatabase):
            containers = {action.container}
        elif isinstance(action, steps.DeleteDataSet):
            containers = {database.container for database in action.found.databases}
        else:
            return
        for web_address, route in sorted(self.proxy.read().loaded.items()):
            if isinstance(route, Conflicting):
                why = f"{route}, so where those visitors go is not known"
            elif route.container in containers:
                why = f"The proxy sends the visitors of {web_address} to what it would take away, so that was left running"
            else:
                continue
            raise Waiting(f"Launchplane was about to {named(action)}. {why}. Nothing more was done.")

    # A part that fails behind the back soon page is stopped before its log is read, so its refusal carries no log yet.
    def take(self, action: Action, witness: Witness, *, behind_back_soon: bool = False) -> Event:
        self.refuse_to_take_from_visitors(action)
        if isinstance(action, steps.EditRoute):
            return self.edit_route(action, witness)
        if isinstance(action, steps.Check):
            return self.check(action.container, action.health_path, with_log=not behind_back_soon)
        if isinstance(action, steps.AwaitBackgroundService):
            return self.await_background_service(action.container, with_log=not behind_back_soon)
        if isinstance(action, steps.ReadLog):
            return self.read_log(action.container)
        if isinstance(action, steps.RemoveLogins):
            unread = self.host.remove_unheld_logins(action.database, witness)
            return action if unread is None else steps.LoginsUnread(action.database, str(unread))
        if isinstance(action, steps.StartStopped):
            try:
                self.host.start_part(action.container, witness)
            except ContainerHostRefused as error:
                return steps.StartRefused(action.container, str(error))
            return action
        if isinstance(action, steps.Switch):
            answer = self.proxy.switch(action.web_address, action.generation, action.attempt, action.container)
            return steps.Switched(action.container, answer == GateAnswer(Verdict.COMMITTED, action.container))
        if isinstance(action, steps.Discard):
            return self.discard(action, witness)
        TAKE[type(action)](self.host, self.proxy, action, witness)
        return action

    def edit_route(self, action: steps.EditRoute, witness: Witness) -> Event:
        edit = self.proxy.edit(action.web_address, witness.generation, witness.attempt, action.read, action.to)
        if edit == LOST:
            return steps.Refused(action.web_address, "The gate left the route: it was not the one read.")
        return replace(action, already=edit == ALREADY)

    # The one removal of an instance of a release. The gate is asked about the generation that made it, and obeyed: one
    # it says was renamed to stays. Otherwise, the gate has taken away every permission to switch to it, and it goes. One
    # no release made has no rename that could be on its way to it.
    def discard(self, action: steps.Discard, by: Witness) -> Event:
        made_by, container = action.generation, action.container
        if made_by is not None:
            answer = self.proxy.gate.discard(action.web_address, made_by, action.attempt, container)
            if answer == GateAnswer(Verdict.COMMITTED, container):
                return steps.Discarded(container, kept=True)
        self.refuse_to_take_from_visitors(steps.Retire(container))
        try:
            self.host.retire_instance(container, by)
        except ContainerHostRefused as error:
            return steps.RemovalRefused(container, str(error))
        return steps.Discarded(container, kept=False)

    def read_log(self, container: str) -> Event:
        try:
            return steps.LogRead(container, self.host.read_log_tail(container))
        except ContainerHostError as error:
            return steps.LogRead(container, "", unreadable=str(error))

    def refusal(self, error: CheckFailed | PartStopped, container: str, *, with_log: bool) -> str:
        return plan.failure_report(str(error), self.host.read_log_tail(container)) if with_log else str(error)

    @observing()
    def check(self, container: str, health_path: str, *, with_log: bool) -> Event:
        try:
            self.proxy.check_instance(container, health_path)
        except CheckFailed as error:
            return steps.CheckRefused(container, self.refusal(error, container, with_log=with_log))
        return steps.Check(container, health_path)

    @observing()
    def await_background_service(self, container: str, *, with_log: bool) -> Event:
        try:
            self.host.await_background_service(container, BACKGROUND_SERVICE_STEADY_SECONDS)
        except PartStopped as error:
            return steps.BackgroundServiceRefused(container, self.refusal(error, container, with_log=with_log))
        return steps.AwaitBackgroundService(container)
