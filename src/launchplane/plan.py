from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any

from launchplane.gate import Answer as GateAnswer
from launchplane.instance import (
    DataSet,
    DataSetUse,
    InstanceState,
    LaneBuild,
    LaneReading,
    RunningBackgroundService,
    RunningInstance,
    Unread,
    Verdict,
)
from launchplane.judge import LOGIN_KINDS, Finding, Judgement, Kind, subjects_of
from launchplane.product import Product
from launchplane.steps import (
    CANCEL_WON,
    Abandon,
    Action,
    AwaitBackgroundService,
    AwaitDatabase,
    AwaitRoute,
    BackgroundServiceRefused,
    Cancel,
    CannotPutBack,
    Check,
    CheckRefused,
    CreateDatabase,
    CreateDataSetNetwork,
    CreateDataSetVolume,
    DeleteDataSet,
    Discard,
    Discarded,
    Done,
    EditRoute,
    Event,
    FreshNames,
    LoginsUnread,
    MoveResult,
    Outcome,
    PutBack,
    RemovalRefused,
    RemoveLogins,
    Result,
    Retire,
    RetireOutcome,
    RetireResult,
    StartBackgroundService,
    StartDatabase,
    StartInstance,
    StartProxy,
    StartStopped,
    Stop,
    StopDatabase,
    StopPart,
    Switch,
    Switched,
    TooLate,
    Wait,
)

type Step[Answer] = Action | Done[Answer] | Stop
type StepBack = Step[PutBack] | TooLate
MADE_READY = {Kind.NO_NETWORK, Kind.NO_VOLUME, Kind.NO_DATABASE, Kind.STOPPED, Kind.NOT_ACCEPTING}
LOGINS_UNKNOWN = {Kind.LOGINS_UNREAD, Kind.NOT_ACCEPTING, Kind.NOT_ASKED}
WATCHED = {Kind.STOPPED, Kind.NOT_RUNNING, Kind.UNSTEADY}


def failure_report(why: str, log_tail: str) -> str:
    if not log_tail:
        return f"{why} It wrote no log."
    return f"{why}\nIts last log lines:\n{log_tail}"


CHANGES: dict[type, Callable[[Any], str | None]] = {
    StartProxy: lambda _step: "started the proxy",
    CreateDataSetNetwork: lambda step: f"made part of the data set {step.data_set}",
    CreateDataSetVolume: lambda step: f"made part of the data set {step.data_set}",
    CreateDatabase: lambda step: f"made part of the data set {step.data_set}",
    StartDatabase: lambda step: f"started the database {step.container}",
    StartInstance: lambda step: f"started the instance {step.container}",
    EditRoute: lambda step: f"wrote the route to {step.to}" if step.to else None,
    StartBackgroundService: lambda step: f"started the background service of {step.instance}",
    Retire: lambda step: f"retired {step.container}",
    RemoveLogins: lambda step: f"removed the logins no container holds from {step.database}",
    StopPart: lambda step: f"stopped {step.container}",
    StopDatabase: lambda step: f"stopped {step.container}",
    StartStopped: lambda step: f"started {step.container}",
}


# What a step that was taken changed. A switch the gate did not rename for, and an instance it kept, changed nothing.
def change_made_by(event: Event) -> str | None:
    if isinstance(event, Switched | Discarded):
        changed = event.won if isinstance(event, Switched) else not event.kept
        return f"{'wrote the route to' if isinstance(event, Switched) else 'retired'} {event.container}" if changed else None
    return CHANGES[type(event)](event) if type(event) in CHANGES else None


def changes_made(done: tuple[Event, ...]) -> list[str]:
    return [change for change in map(change_made_by, done) if change is not None]


def changes_so_far(done: tuple[Event, ...]) -> str:
    changes = changes_made(done)
    if not changes:
        return "Nothing was changed."
    return f"Before that, this call {', '.join(changes)}, and those changes stay."


def sentences(findings: list[Finding]) -> str:
    return "; ".join(finding.sentence for finding in findings)


def loading_step(judged: Judgement) -> Action | None:
    if judged.of_kind(Kind.PROXY_STOPPED):
        return StartProxy()
    if judged.of_kind(Kind.ROUTE_NOT_LOADED):
        return AwaitRoute(judged.lane.web_address, judged.lane.route.written)
    return None


def route_step(judged: Judgement, done: tuple[Event, ...]) -> Action | Stop | None:
    if (step := loading_step(judged)) is not None:
        return step
    disabled = judged.of_kind(Kind.ROUTE_DISABLED)
    if disabled:
        return Stop(f"{sentences(disabled)}. {changes_so_far(done)} The proxy's log says why; retiring the lane removes the route.")
    return None


# A routes file that is not the route Launchplane writes is never taken for no route: what it sends visitors to is not
# known, so nothing of its lane is changed and no call on the lane answers success.
def unread_route(lane: LaneReading, attempted: str, done: tuple[Event, ...]) -> Stop | None:
    file = lane.route.file
    return Stop(f"{file}, so the lane is not {attempted}. {changes_so_far(done)}") if isinstance(file, Unread) else None


def is_fit(judged: Judgement, instance: RunningInstance, target: LaneBuild) -> bool:
    return instance.build == target.build and not judged.about_instance(instance)


def data_set_change(lane: LaneReading, switching_to: RunningInstance) -> list[str]:
    receiving = lane.active
    if receiving is None or receiving.data_set in (None, switching_to.data_set):
        return []
    return [
        f"The instance {switching_to.container} runs against the data set {switching_to.data_set}, "
        f"not {receiving.data_set}, which the instance receiving visitors runs against"
    ]


# Whether a check passed in this call, the host cannot show, so it is asked of what the call has done.
def is_checked(instance: RunningInstance, product: Product, done: tuple[Event, ...]) -> bool:
    return Check(instance.container, product.health_path) in done and instance.state == InstanceState.RUNNING


def problems_switching_to(judged: Judgement, instance: RunningInstance) -> list[str]:
    in_the_way = [*judged.about_instance(instance), *judged.about_data_set(judged.lane.data_set(instance.data_set))]
    return [*[finding.sentence for finding in in_the_way], *data_set_change(judged.lane, instance)]


def lane_data_set(lane: LaneReading, receiving: RunningInstance | None, fresh: str) -> str | Stop:
    if receiving is not None and receiving.data_set is not None:
        return receiving.data_set
    names = sorted(data_set.data_set for data_set in lane.data_sets)
    if len(names) > 1:
        return Stop(
            f"The lane has the data sets {', '.join(names)} and no instance receiving visitors that uses one, "
            "so Launchplane cannot tell which one to start the instance on. Nothing was changed."
        )
    return names[0] if names else fresh


def step_toward_ready(found: DataSet, kinds: set[Kind], lane: LaneReading, image: str, generation: str | None) -> Action:
    if Kind.NO_NETWORK in kinds:
        return CreateDataSetNetwork(lane.product, lane.lane, found.data_set, generation)
    if Kind.NO_VOLUME in kinds:
        return CreateDataSetVolume(lane.product, lane.lane, found.data_set)
    if Kind.NO_DATABASE in kinds:
        return CreateDatabase(lane.product, lane.lane, found.data_set, found.networks[0].name, found.volumes[0], image)
    server = found.databases[0]
    return StartDatabase(server.container) if Kind.STOPPED in kinds else AwaitDatabase(server.container)


def prepared(
    judged: Judgement,
    data_set: str | None,
    fresh: str | None,
    product: Product,
    done: tuple[Event, ...],
    generation: str | None = None,
) -> Action | Stop | DataSetUse | None:
    if data_set is None or product.database is None:
        return None
    lane = judged.lane
    found = lane.data_set(data_set)
    if found is None and data_set != fresh:
        return Stop(
            f"The instance receiving visitors uses the data set {data_set}, which is not on the host. {changes_so_far(done)}"
        )
    if found is None:
        return CreateDataSetNetwork(lane.product, lane.lane, data_set, generation)
    findings = judged.about_data_set(found)
    beyond = [finding for finding in findings if finding.kind not in MADE_READY]
    # Nothing in a data set is made or started while it has a problem no step puts right.
    if beyond:
        return Stop(
            f"The data set {found.data_set} is not as it must be, and fixing it is not Launchplane's to choose: "
            f"{sentences(beyond)}. {changes_so_far(done)}"
        )
    if findings:
        return step_toward_ready(found, {finding.kind for finding in findings}, lane, product.database.image, generation)
    return DataSetUse(found.data_set, found.networks[0].name, found.databases[0].container)


# A move answers for the instance it serves from and that instance's data set, not for another data set of the lane.
def left_for_a_move(judged: Judgement, serving: RunningInstance) -> list[Finding]:
    of_other_data_sets = {
        subject for found in judged.lane.data_sets if found.data_set != serving.data_set for subject in subjects_of(found)
    }
    return [finding for finding in judged.findings if finding.kind not in LOGIN_KINDS and finding.subject not in of_other_data_sets]


# A login no container holds goes, from every server that told its logins. One that did not is never taken to have none.
# It holds up only a move that ends well and `needs` its data set. No other call waits or fails for a list: its answer names it.
def login_step(judged: Judgement, done: tuple[Event, ...], needs: DataSet | None = None) -> RemoveLogins | Wait | None:
    refused = {event.container: event.report for event in done if isinstance(event, LoginsUnread)}
    unheld = [finding.subject for finding in judged.of_kind(Kind.LOGIN_UNHELD) if finding.subject not in refused]
    if unheld:
        return RemoveLogins(unheld[0])
    needed = [finding.sentence for finding in judged.about(subjects_of(needs)) if finding.kind in LOGINS_UNKNOWN]
    untold = "; ".join([*needed, *(report for database, report in refused.items() if database in subjects_of(needs))])
    return Wait(f"{untold}. Everything else is done and stays as it is. The call ends once they are read.") if untold else None


# Whether the call is putting its lane back: a cancel was asked of it and the host has not shown it too late.
def is_turned_back(cancel: Cancel | None) -> bool:
    return cancel is not None and not cancel.refused


# The one end of every cancel, and its verdict is about this request alone, read from the host as it now is: nothing its
# generation made remains, and each part it put back is as it must be. What else is found on the lane is carried in the
# answer and is not the cancel's to answer for. A data set the generation made stays, and is named.
def put_back_or_what_is_left(judged: Judgement, generation: str | None, put_back: Collection[str] = ()) -> Done[PutBack] | Stop:
    lane = judged.lane
    left = judged.about(put_back)
    if left:
        return Stop(
            f"The cancel was carried out, and putting {lane.lane} of {lane.product} back left a part not as it must be, so "
            f"the request does not end as cancelled: {sentences(left)}."
        )
    kept = sorted(found.data_set for found in lane.data_sets if generation is not None and found.generation == generation)
    return Done(PutBack(data_sets_kept=kept, findings=[finding.sentence for finding in judged.findings]))


# One release of a lane: one reading of the host, and what the gate answers for the generation, asked after that
# reading. The gate's answer alone says whether the release is committed, abandoned or neither. The routes file and
# the proxy's loaded route say where visitors go, and never that.
@dataclass(frozen=True)
class Move:
    judged: Judgement
    target: LaneBuild
    product: Product
    fresh: FreshNames
    done: tuple[Event, ...]
    generation: str
    attempt: int
    gate: GateAnswer

    @property
    def lane(self) -> LaneReading:
        return self.judged.lane

    def instance(self, container: str | None) -> RunningInstance | None:
        return next((instance for instance in self.lane.instances if instance.container == container), None)

    # The instance this attempt started: a release makes its own, and one anything earlier left is never taken for it.
    @property
    def candidate(self) -> RunningInstance | None:
        started = {event.container for event in self.done if isinstance(event, StartInstance)}
        return next((instance for instance in self.lane.instances if instance.container in started), None)

    def answer(self, **update: object) -> MoveResult:
        receiving, done = self.instance(self.lane.route.written), self.done
        settled = Outcome.REPAIRED if changes_made(done) else Outcome.UNCHANGED
        answer = MoveResult(
            product=self.target.product,
            lane=self.target.lane,
            build=self.target.build,
            web_address=self.lane.web_address,
            outcome=Outcome.SWITCHED if self.gate.verdict is Verdict.COMMITTED else settled,
            container_receiving_visitors=self.lane.route.written,
            data_set=None if receiving is None else receiving.data_set,
            did=changes_made(done),
        )
        return answer.model_copy(update=update)


# Until the gate says a release is committed, nothing it made receives visitors. A routes file or a proxy that names
# its instance all the same contradicts the gate, and nothing is touched on a contradiction.
def own_named_by_route(move: Move) -> Wait | None:
    lane = move.lane
    made = {each.container for each in lane.instances if each.generation == move.generation}
    started = {event.container for event in move.done if isinstance(event, StartInstance)}
    named = sorted((made | started) & lane.route.names)
    if not named:
        return None
    return Wait(
        f"The gate does not say this release is committed, and the routes file or the proxy of {lane.web_address} names "
        f"its instance {named[0]}. Nothing was touched."
    )


def unread_owner(parts: Sequence[RunningInstance | RunningBackgroundService]) -> Wait | None:
    unread = next((part.generation for part in parts if isinstance(part.generation, Unread)), None)
    return None if unread is None else Wait(f"{unread}, so it was not touched.")


def removal_refused(done: tuple[Event, ...], container: str) -> str | None:
    for event in done:
        if isinstance(event, RemovalRefused) and event.container == container:
            return event.report
    return None


# The one way a part of a release goes. An instance goes through the gate, which either says its rename won, and then
# it stays, or takes away the permission to switch to it before it is removed. A part whose release cannot be read, or
# that the routes file or the proxy names, is waited on. A removal Docker refused is said, not tried again.
def discard(move: Move, instance: RunningInstance | RunningBackgroundService) -> Action | Stop | CannotPutBack:
    container = instance.container
    refusal = removal_refused(move.done, container)
    if refusal is not None and move.gate.verdict is Verdict.ABANDONED:
        return CannotPutBack(f"{CANCEL_WON}, and {container} could not be removed to put the lane back: {refusal}")
    if refusal is not None:
        return Stop(f"{container} could not be removed: {refusal} {changes_so_far(move.done)}")
    if (wait := unread_owner([instance])) is not None:
        return wait
    if container in move.lane.route.names:
        return Wait(f"The routes file or the proxy of {move.lane.web_address} names {container}, so it was not removed.")
    made_by = instance.generation if isinstance(instance, RunningInstance) else None
    return Discard(move.lane.web_address, container, made_by if isinstance(made_by, str) else None, move.attempt)


# An instance an earlier release was renamed to is that release's winner, and goes only once this release is committed
# to another instance and the proxy sends visitors there.
def has_later_target_enabled(move: Move, winner: str) -> bool:
    target = move.gate.candidate
    return move.gate.verdict is Verdict.COMMITTED and target != winner and move.lane.route.sends_visitors_to == target


# Every part of the lane but the instance kept and its background service, background services first.
def old_part_step(move: Move, kept: str | None, *, background_services_only: bool = False) -> Action | Stop | CannotPutBack | None:
    lane = move.lane
    background_services = sorted(running.container for running in lane.background_services if running.instance != kept)
    if background_services:
        return Retire(background_services[0])
    if background_services_only:
        return None
    for instance in sorted((each for each in lane.instances if each.container != kept), key=lambda each: each.container):
        if Discarded(instance.container, kept=True) not in move.done:
            return discard(move, instance)
        if has_later_target_enabled(move, instance.container):
            return Retire(instance.container)
    return None


def in_service(move: Move) -> Action | Stop | None:
    lane, target = move.lane, move.target
    if lane.route.in_maintenance:
        return Stop(
            f"{target.lane} of {target.product} is in maintenance: the routes file of {lane.web_address} points at the back "
            f"soon page. End maintenance first, with POST /lanes/{target.product}/{target.lane}/maintenance/end. Nothing was "
            "changed."
        )
    return unread_route(lane, "moved", move.done) or route_step(move.judged, move.done)


# The background service of the instance that serves: started where the product has one, watched while it comes up,
# and replaced when it is anything else that is wrong.
def background_service_step(move: Move, serving: RunningInstance, use: DataSetUse | None) -> Action | None:
    described = move.product.background_service
    start = StartBackgroundService(
        move.target, serving.container, use, () if described is None else tuple(described.command), move.generation
    )
    own = next((running for running in move.lane.background_services if running.instance == serving.container), None)
    if own is None:
        return None if described is None else start
    kinds = {finding.kind for finding in move.judged.about({own.container})}
    if not kinds:
        return None
    watched = kinds == {Kind.UNSTEADY} or (kinds <= WATCHED and start in move.done)
    return AwaitBackgroundService(own.container) if watched else Retire(own.container)


# Around the instance that serves: first no background service of another instance, since only one of a lane runs at a
# time, then its own, and only then no other instance. Then the answer. A committed release has one answer, so what
# would fail another move stops this one where it is, to be taken up again.
def finish(move: Move, serving: RunningInstance) -> Step[MoveResult] | CannotPutBack:
    judged, done = move.judged, move.done
    if (step := old_part_step(move, serving.container, background_services_only=True)) is not None:
        return step
    use = prepared(judged, serving.data_set, None, move.product, done)
    if not (use is None or isinstance(use, DataSetUse)):
        return use
    refused = next((event for event in done if isinstance(event, BackgroundServiceRefused)), None)
    if refused is not None and move.gate.verdict is Verdict.COMMITTED:
        return Stop(f"The background service did not stay up: {refused.report}")
    if refused is not None:
        return Done(move.answer(outcome=Outcome.BACKGROUND_SERVICE_FAILED, error=refused.report))
    step = background_service_step(move, serving, use) or old_part_step(move, serving.container)
    if step is not None:
        return step
    left = left_for_a_move(judged, serving)
    if left:
        return Stop(f"The lane is not as it must be, so the move does not answer success: {sentences(left)}. {changes_so_far(done)}")
    return login_step(judged, done, move.lane.data_set(serving.data_set)) or Done(move.answer())


# The gate says the routes file was renamed to this instance. Visitors are waited for, then the rest is finished.
def finish_committed(move: Move) -> Step[MoveResult] | CannotPutBack:
    lane, serving = move.lane, move.instance(move.gate.candidate)
    if serving is None:
        return Stop(
            f"{lane.web_address} was switched to {move.gate.candidate} for this release, and that instance is not on the "
            "host. Nothing else of the lane was touched."
        )
    if not lane.route.proxy_runs:
        return StartProxy()
    if lane.route.sends_visitors_to != serving.container:
        return AwaitRoute(lane.web_address, serving.container, enabled=True)
    return finish(move, serving)


# The gate has no verdict for the generation. A candidate of this attempt is checked and switched to. One that failed its
# check goes. With none, an instance already receiving visitors on this build with nothing wrong is the lane as asked;
# otherwise what anything earlier left is cleaned up, and a candidate is made.
def prepare(move: Move) -> Step[MoveResult] | CannotPutBack:
    lane, candidate, done = move.lane, move.candidate, move.done
    if (wait := own_named_by_route(move)) is not None:
        return wait
    refused = next((event for event in done if isinstance(event, CheckRefused)), None)
    if refused is not None:
        return (
            Done(move.answer(outcome=Outcome.CHECK_FAILED, error=refused.report)) if candidate is None else discard(move, candidate)
        )
    if (step := in_service(move)) is not None:
        return step
    if candidate is not None:
        return switch_or_check(move, candidate)
    receiving = move.instance(lane.route.written)
    if receiving is not None and is_fit(move.judged, receiving, move.target):
        return finish(move, receiving)
    if (step := old_part_step(move, lane.route.written)) is not None:
        return step
    data_set = None if move.product.database is None else lane_data_set(lane, receiving, move.fresh.data_set)
    if isinstance(data_set, Stop):
        return data_set
    use = prepared(move.judged, data_set, move.fresh.data_set, move.product, done, move.generation)
    if not (use is None or isinstance(use, DataSetUse)):
        return use
    return StartInstance(move.target, move.fresh.container, use, move.generation)


def switch_or_check(move: Move, candidate: RunningInstance) -> Action | Stop:
    if not is_checked(candidate, move.product, move.done):
        return Check(candidate.container, move.product.health_path)
    problems = problems_switching_to(move.judged, candidate)
    if problems:
        return Stop(
            f"The instance {candidate.container} passed its check and the lane is not as it must be to switch to it, so the "
            f"web address was not switched: {'; '.join(problems)}. {changes_so_far(move.done)} Asking again replaces the instance."
        )
    return Switch(move.lane.web_address, candidate.container, move.generation, move.attempt)


def too_late_to_cancel(web_address: str) -> str:
    return (
        f"The routes file of {web_address} was already renamed for this release, and the rename is the point from which a "
        "move is not cancelled, so the move is finished as asked. Moving the lane to its earlier build puts that build back."
    )


# What the generation made is read from the stamp on each part, and goes: its background service, then its instance.
# A background service started before the switch repairs the instance that serves: a cancel takes it back, a failure leaves it.
def own_part_step(move: Move, *, repairs: bool) -> Action | Stop | CannotPutBack | None:
    lane = move.lane
    wait = own_named_by_route(move) or unread_owner([*lane.background_services, *lane.instances])
    if wait is not None:
        return wait
    parts = (*lane.background_services, *lane.instances) if repairs else lane.instances
    own = [each for each in parts if each.generation == move.generation]
    return discard(move, min(own, key=lambda each: (isinstance(each, RunningInstance), each.container))) if own else None


# After a move failed with no verdict and its attempt is fenced: the instance it made goes, and each login read as unheld.
def decide_clean_up(move: Move, *, logins: bool) -> Step[Result] | CannotPutBack:
    if move.gate.verdict is not Verdict.NONE:
        return Wait(f"The gate answers {move.gate.verdict} for this release, so nothing of it was cleaned up.")
    return own_part_step(move, repairs=False) or (login_step(move.judged, move.done) if logins else None) or Done(Result())


# A cancel won. What the generation made goes. Data stays.
def put_lane_back(move: Move) -> Step[PutBack] | CannotPutBack:
    if (step := own_part_step(move, repairs=True)) is not None:
        return step
    if not any(isinstance(event, Abandon) for event in move.done):
        return Abandon(move.lane.web_address, move.generation)
    put_back = put_back_or_what_is_left(move.judged, move.generation)
    return (login_step(move.judged, move.done) or put_back) if isinstance(put_back, Done) else put_back


# The gate's answer picks what a move does, and nothing else does. A cancel that was asked and that the gate has not
# answered is carried there, where a rename already sent may still win; the gate is a command in the proxy's container.
def decide_move(move: Move, cancel: Cancel | None = None) -> Step[MoveResult] | StepBack | CannotPutBack:
    asked = is_turned_back(cancel)
    if move.gate.verdict is Verdict.ABANDONED:
        return put_lane_back(move)
    if move.gate.verdict is Verdict.COMMITTED:
        return TooLate(too_late_to_cancel(move.lane.web_address)) if asked else finish_committed(move)
    if asked:
        return Abandon(move.lane.web_address, move.generation) if move.lane.route.proxy_runs else StartProxy()
    return prepare(move)


# Once a cancel was asked, a data set named for deleting is kept, also where the rest is too late to put back.
def decide_retire(
    judged: Judgement, delete_data_set: str | None, done: tuple[Event, ...], cancel: Cancel | None = None
) -> Step[RetireResult] | StepBack:
    if (unread := unread_route(judged.lane, "retired", done)) is not None:
        return unread
    if cancel is not None and is_turned_back(cancel):
        return retire_step_back(judged, delete_data_set)
    return retire_step(judged, None if cancel else delete_data_set, done)


# The first thing a retire changes is the route, and nothing it removes after that can be put back.
def retire_step_back(judged: Judgement, delete_data_set: str | None) -> StepBack:
    lane = judged.lane
    if (step := loading_step(judged)) is not None:
        return step
    if lane.route.written is not None:
        return put_back_or_what_is_left(judged, None)
    kept = lane.data_set(delete_data_set)
    saved = (
        "" if kept is None else f" The data set {kept.data_set}, named for deleting, is kept: the cancel came before it was deleted."
    )
    return TooLate(
        f"{lane.web_address} has no route, so retiring {lane.lane} of {lane.product} could not be cancelled and is finished "
        "as asked: a retire removes the route first, and Launchplane does not make a removed instance again by itself. "
        f"Moving the lane to a build brings it back on its data set.{saved}"
    )


def retire_step(judged: Judgement, delete_data_set: str | None, done: tuple[Event, ...]) -> Step[RetireResult]:
    lane = judged.lane
    route, web_address = lane.route, lane.web_address
    if (step := loading_step(judged)) is not None:
        return step
    if (written := route.written) is not None:
        return EditRoute(web_address, None, route.installed, written)
    if lane.background_services:
        return Retire(min(running.container for running in lane.background_services))
    if lane.instances:
        return Retire(min(instance.container for instance in lane.instances))
    named = lane.data_set(delete_data_set)
    doubled = [finding for finding in judged.about(subjects_of(named)) if finding.kind == Kind.DOUBLED]
    if doubled:
        return Stop(
            f"{sentences(doubled)}, so nothing of the data set was deleted: which is its data is not known. {changes_so_far(done)}"
        )
    if named is not None:
        return DeleteDataSet(named)
    route_removed_to = next((event.was for event in done if isinstance(event, EditRoute)), None)
    retired = [event.container for event in done if isinstance(event, Retire)]
    deleted = next((event.found.data_set for event in done if isinstance(event, DeleteDataSet)), None)
    changed = any(isinstance(event, EditRoute | Retire | RemoveLogins) for event in done)
    return login_step(judged, done) or Done(
        RetireResult(
            product=lane.product,
            lane=lane.lane,
            web_address=web_address,
            outcome=RetireOutcome.DELETED if deleted else RetireOutcome.RETIRED if changed else RetireOutcome.UNCHANGED,
            route_removed_to_container=route_removed_to,
            retired_containers=retired,
            deleted_data_set=deleted,
            kept_data_sets=sorted(found.data_set for found in lane.data_sets),
        )
    )
