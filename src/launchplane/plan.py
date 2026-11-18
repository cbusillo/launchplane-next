from collections.abc import Callable, Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import overload

from launchplane.instance import DataSet, DataSetUse, InstanceState, LaneBuild, LaneReading, RunningInstance, Verdict
from launchplane.judge import LOGIN_KINDS, Finding, Judgement, Kind, is_loaded_as_written, subjects_of
from launchplane.product import Product
from launchplane.steps import (
    Abandon,
    Abandoned,
    Action,
    AwaitBackgroundService,
    AwaitDatabase,
    AwaitRoute,
    AwaitSwitch,
    BackgroundServiceRefused,
    Cancel,
    Check,
    CheckRefused,
    CreateDatabase,
    CreateDataSetNetwork,
    CreateDataSetVolume,
    DeleteDataSet,
    Discard,
    Discarded,
    Done,
    Event,
    FreshNames,
    MoveResult,
    Outcome,
    PutBack,
    RemoveInstance,
    RemoveLogins,
    RemoveRoute,
    Retire,
    RetireOutcome,
    RetireResult,
    ShowBackSoon,
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
    WriteRoute,
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


def maintenance_change_made_by(event: Event) -> str | None:
    match event:
        case ShowBackSoon(web_address=web_address):
            return f"switched {web_address} to the back soon page"
        case StopPart(container=container) | StopDatabase(container=container):
            return f"stopped {container}"
        case StartStopped(container=container):
            return f"started {container}"
        case _:
            return None


def change_made_by(event: Event) -> str | None:
    match event:
        case StartProxy():
            return "started the proxy"
        case CreateDataSetNetwork(data_set=data_set) | CreateDataSetVolume(data_set=data_set) | CreateDatabase(data_set=data_set):
            return f"made part of the data set {data_set}"
        case StartDatabase(container=container):
            return f"started the database {container}"
        case StartInstance(container=container):
            return f"started the instance {container}"
        case WriteRoute(container=container) | Switched(container=container, verdict=Verdict.COMMITTED):
            return f"wrote the route to {container}"
        case StartBackgroundService(instance=instance):
            return f"started the background service of {instance}"
        case Retire(container=container) | Discarded(container=container, verdict=Verdict.NONE):
            return f"retired {container}"
        case RemoveInstance(container=container):
            return f"removed {container}"
        case RemoveLogins(database=database):
            return f"removed the logins no container holds from {database}"
        case _:
            return maintenance_change_made_by(event)


def changes_made(done: tuple[Event, ...]) -> list[str]:
    return [change for change in map(change_made_by, done) if change is not None]


def changes_so_far(done: tuple[Event, ...]) -> str:
    changes = changes_made(done)
    if not changes:
        return "Nothing was changed."
    return f"Before that, this call {', '.join(changes)}, and those changes stay."


def sentences(findings: list[Finding]) -> str:
    return "; ".join(finding.sentence for finding in findings)


# A route that is already renamed for a release is waited for as that release's, which no deadline fails.
def loading_step(judged: Judgement, switched: bool = False) -> Action | None:
    if judged.of_kind(Kind.PROXY_STOPPED):
        return StartProxy()
    if judged.of_kind(Kind.ROUTE_NOT_LOADED):
        route = judged.lane.route
        if switched and route.written is not None:
            return AwaitSwitch(judged.lane.web_address, route.written)
        return AwaitRoute(judged.lane.web_address, route.written)
    return None


def route_step(judged: Judgement, done: tuple[Event, ...]) -> Action | Stop | None:
    if (step := loading_step(judged)) is not None:
        return step
    disabled = judged.of_kind(Kind.ROUTE_DISABLED)
    if disabled:
        return Stop(f"{sentences(disabled)}. {changes_so_far(done)} The proxy's log says why; retiring the lane removes the route.")
    return None


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


# A stopped server keeps its logins until it is next started. One that could not tell its logins is never taken to have none.
def login_step(judged: Judgement, done: tuple[Event, ...]) -> Action | Stop | None:
    for database in judged.lane.databases:
        about = judged.about({database.container})
        if any(finding.kind == Kind.LOGIN_UNHELD for finding in about):
            return RemoveLogins(database.container)
        unknown = next((finding for finding in about if finding.kind in LOGINS_UNKNOWN), None)
        if unknown is not None:
            why = unknown.sentence if unknown.kind == Kind.LOGINS_UNREAD else f"{unknown.sentence}, so its logins could not be read"
            return Stop(f"{why}. {changes_so_far(done)} Ask again once the server answers, or delete its data set by name.")
    return None


def with_logins_settled[Answer](step: Step[Answer], judged: Judgement, done: tuple[Event, ...]) -> Step[Answer]:
    return (login_step(judged, done) or step) if isinstance(step, Done) else step


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


class Phase(StrEnum):
    NOTHING_MADE = "nothing made"
    CANDIDATE_MADE = "candidate made"
    CANDIDATE_REFUSED = "candidate failed its check"
    CANDIDATE_CHECKED = "candidate checked"
    RENAMED = "renamed, not yet loaded"
    LOADED = "loaded"


# One release of a lane as one reading finds it.
@dataclass(frozen=True)
class Move:
    judged: Judgement
    target: LaneBuild
    product: Product
    fresh: FreshNames
    done: tuple[Event, ...]
    generation: str

    @property
    def lane(self) -> LaneReading:
        return self.judged.lane

    # What the gate told this attempt, where it asked. No reading made before the answer stands against it.
    @property
    def told(self) -> Verdict | None:
        return next((event.verdict for event in reversed(self.done) if isinstance(event, Switched | Abandoned)), None)

    # Read from the routes file itself, whoever asks and whichever attempt renamed it.
    @property
    def is_switched(self) -> bool:
        return self.lane.route.written_by == self.generation

    @property
    def is_abandoned(self) -> bool:
        return self.generation in self.lane.route.abandoned or self.told is Verdict.ABANDONED

    @property
    def named(self) -> RunningInstance | None:
        return next((instance for instance in self.lane.instances if instance.container == self.lane.route.written), None)

    # The instance this attempt started: a release makes its own, and one anything earlier left is never taken for it.
    @property
    def candidate(self) -> RunningInstance | None:
        started = {event.container for event in self.done if isinstance(event, StartInstance)}
        return next((instance for instance in self.lane.instances if instance.container in started), None)

    def answer(self, **update: object) -> MoveResult:
        named, done = self.named, self.done
        settled = Outcome.REPAIRED if changes_made(done) else Outcome.UNCHANGED
        answer = MoveResult(
            product=self.target.product,
            lane=self.target.lane,
            build=self.target.build,
            web_address=self.lane.web_address,
            outcome=Outcome.SWITCHED if self.is_switched else settled,
            container_receiving_visitors=self.lane.route.written,
            data_set=None if named is None else named.data_set,
            did=changes_made(done),
        )
        return answer.model_copy(update=update)


# The phase, and the instance its row acts on: the candidate before the rename, the instance the routes file names
# from it on.
def phase_of(move: Move) -> tuple[Phase, RunningInstance | None]:
    route, candidate = move.lane.route, move.candidate
    if move.is_switched:
        return (Phase.LOADED if route.proxy_runs and is_loaded_as_written(route) else Phase.RENAMED), move.named
    if any(isinstance(event, CheckRefused) for event in move.done):
        return Phase.CANDIDATE_REFUSED, candidate
    if candidate is None:
        return Phase.NOTHING_MADE, move.named
    return (Phase.CANDIDATE_CHECKED if is_checked(candidate, move.product, move.done) else Phase.CANDIDATE_MADE), candidate


# Every part of the lane but the instance kept and its background service, background services first. An instance goes
# through the gate, so a rename still on its way to it is refused before it is removed.
def old_part_step(lane: LaneReading, kept: str | None, *, background_services_only: bool = False) -> Action | None:
    background_services = sorted(running.container for running in lane.background_services if running.instance != kept)
    instances = sorted((each for each in lane.instances if each.container != kept), key=lambda each: each.container)
    if background_services:
        return Retire(background_services[0])
    if not instances or background_services_only:
        return None
    made_by = instances[0].generation
    return Discard(lane.web_address, instances[0].container, made_by if isinstance(made_by, str) else None)


def in_service(move: Move, _candidate: RunningInstance | None) -> Action | Stop | None:
    lane, target = move.lane, move.target
    if lane.route.in_maintenance:
        return Stop(
            f"{target.lane} of {target.product} is in maintenance: the routes file of {lane.web_address} points at the back "
            f"soon page. End maintenance first, with POST /lanes/{target.product}/{target.lane}/maintenance/end. Nothing was "
            "changed."
        )
    if lane.route.unread is not None:
        return Stop(f"{lane.route.unread}, so the lane is not moved. Retiring the lane removes the file. Nothing was changed.")
    return route_step(move.judged, move.done)


def fit_to_switch(move: Move, candidate: RunningInstance | None) -> Action | Stop | None:
    problems = [] if candidate is None else problems_switching_to(move.judged, candidate)
    if candidate is None or not problems:
        return in_service(move, candidate)
    return Stop(
        f"The instance {candidate.container} passed its check and the lane is not as it must be to switch to it, so the "
        f"web address was not switched: {'; '.join(problems)}. {changes_so_far(move.done)} Asking again replaces the instance."
    )


# The background service of the instance that serves: started where the product has one, watched while it comes up,
# and replaced when it is anything else that is wrong.
def background_service_step(move: Move, serving: RunningInstance, use: DataSetUse | None) -> Action | None:
    described = move.product.background_service
    if any(isinstance(event, BackgroundServiceRefused) for event in move.done):
        return None
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
# time, then its own, and only then no other instance. Then the answer.
def finish(move: Move, serving: RunningInstance) -> Step[MoveResult]:
    judged, done, lane = move.judged, move.done, move.lane
    if (step := old_part_step(lane, serving.container, background_services_only=True)) is not None:
        return step
    use = prepared(judged, serving.data_set, None, move.product, done)
    if not (use is None or isinstance(use, DataSetUse)):
        return use
    step = background_service_step(move, serving, use) or old_part_step(lane, serving.container)
    if step is not None:
        return step
    refused = next((event for event in done if isinstance(event, BackgroundServiceRefused)), None)
    if refused is not None:
        return Done(move.answer(outcome=Outcome.BACKGROUND_SERVICE_FAILED, error=refused.report))
    left = left_for_a_move(judged, serving)
    if left:
        return Stop(f"The lane is not as it must be, so the move does not answer success: {sentences(left)}. {changes_so_far(done)}")
    return Done(move.answer())


# An instance already receiving visitors on this build, with nothing wrong, is the lane as asked: no candidate is made
# beside it. Otherwise, what anything earlier left is cleaned up first.
def make_candidate(move: Move, receiving: RunningInstance | None) -> Step[MoveResult]:
    lane = move.lane
    if receiving is not None and is_fit(move.judged, receiving, move.target):
        return finish(move, receiving)
    if (step := old_part_step(lane, lane.route.written)) is not None:
        return step
    data_set = None if move.product.database is None else lane_data_set(lane, receiving, move.fresh.data_set)
    if isinstance(data_set, Stop):
        return data_set
    use = prepared(move.judged, data_set, move.fresh.data_set, move.product, move.done, move.generation)
    if not (use is None or isinstance(use, DataSetUse)):
        return use
    return StartInstance(move.target, move.fresh.container, use, move.generation)


def remove_refused(move: Move, candidate: RunningInstance | None) -> Step[MoveResult]:
    refused = next(event for event in move.done if isinstance(event, CheckRefused))
    if candidate is not None:
        return RemoveInstance(candidate.container)
    return Done(move.answer(outcome=Outcome.CHECK_FAILED, error=refused.report))


def await_load(move: Move, serving: RunningInstance) -> Step[MoveResult]:
    return loading_step(move.judged, switched=True) or finish(move, serving)


# A row acts on an instance the reading found. One that is gone since the phase was read stops the call and says so.
def on(act: Callable[[Move, RunningInstance], Step[MoveResult]]) -> Callable[[Move, RunningInstance | None], Step[MoveResult]]:
    def act_on(move: Move, instance: RunningInstance | None) -> Step[MoveResult]:
        if instance is not None:
            return act(move, instance)
        return Stop(
            f"The instance of this release that {move.lane.web_address} is to go to is not on the host. "
            f"{changes_so_far(move.done)} Asking for the build again starts a new one."
        )

    return act_on


def too_late_to_cancel(web_address: str) -> str:
    return (
        f"The routes file of {web_address} was already renamed for this release, and the rename is the point from which a "
        "move is not cancelled, so the move is finished as asked. Moving the lane to its earlier build puts that build back."
    )


# What the generation made is read from the stamp on each part: its background service, then its instance. Data stays.
def put_lane_back(move: Move) -> Step[PutBack]:
    lane = move.lane
    background_services = sorted(each.container for each in lane.background_services if each.generation == move.generation)
    instances = sorted(each.container for each in lane.instances if each.generation == move.generation)
    if background_services:
        return Retire(background_services[0])
    if instances:
        return RemoveInstance(instances[0])
    # A server the release had only just made is waited for, so its logins can be read and its data set is kept whole.
    starting = sorted(finding.subject for finding in move.judged.of_kind(Kind.NOT_ACCEPTING))
    if starting:
        return AwaitDatabase(starting[0])
    return with_logins_settled(put_back_or_what_is_left(move.judged, move.generation), move.judged, move.done)


# A cancel wins before the rename. With its mark at the gate, the lane is put back. Without it, a cancel that was asked
# is carried to the gate, where a rename already sent may still win. The gate is a command in the proxy's container.
def cancel_wins(move: Move, asked: bool) -> StepBack | None:
    route = move.lane.route
    if move.is_abandoned:
        return put_lane_back(move)
    if not asked:
        return None
    return Abandon(move.lane.web_address, move.generation) if route.proxy_runs else StartProxy()


def cancel_is_too_late(move: Move, asked: bool) -> StepBack | None:
    return TooLate(too_late_to_cancel(move.lane.web_address)) if asked else None


# What must be true answers None when it is, or the step that waits for it, or why it is not.
@dataclass(frozen=True)
class Row:
    must_be_true: Callable[[Move, RunningInstance | None], Action | Stop | None]
    act: Callable[[Move, RunningInstance | None], Step[MoveResult]]
    cancel: Callable[[Move, bool], StepBack | None]


# The move, one row for each state the host and this attempt can show. The phase picks the row and nothing else does.
MOVE: dict[Phase, Row] = {
    Phase.NOTHING_MADE: Row(in_service, make_candidate, cancel_wins),
    Phase.CANDIDATE_MADE: Row(in_service, on(lambda move, new: Check(new.container, move.product.health_path)), cancel_wins),
    Phase.CANDIDATE_REFUSED: Row(lambda _move, _new: None, remove_refused, cancel_wins),
    Phase.CANDIDATE_CHECKED: Row(
        fit_to_switch, on(lambda move, new: Switch(move.lane.web_address, new.container, move.generation)), cancel_wins
    ),
    Phase.RENAMED: Row(lambda _move, _new: None, on(await_load), cancel_is_too_late),
    Phase.LOADED: Row(lambda move, _new: route_step(move.judged, move.done), on(finish), cancel_is_too_late),
}


def decide_move(
    judged: Judgement,
    target: LaneBuild,
    product: Product,
    fresh: FreshNames,
    done: tuple[Event, ...],
    generation: str,
    cancel: Cancel | None = None,
) -> Step[MoveResult] | StepBack:
    move = Move(judged, target, product, fresh, done, generation)
    # The gate's answer stands. A reading that does not show what the gate said was done is not acted on.
    if move.told is Verdict.COMMITTED and not move.is_switched:
        return Stop(
            f"The gate answered that this release of {move.lane.web_address} is committed, and the routes file as read "
            f"names {move.lane.route.written}. Nothing was done against the gate's answer. {changes_so_far(done)}"
        )
    phase, instance = phase_of(move)
    row = MOVE[phase]
    back = row.cancel(move, is_turned_back(cancel))
    if back is not None:
        return back
    return with_logins_settled(row.must_be_true(move, instance) or row.act(move, instance), judged, done)


def retire_outcome(retired: bool, deleted: str | None) -> RetireOutcome:
    if deleted is not None:
        return RetireOutcome.DELETED
    return RetireOutcome.RETIRED if retired else RetireOutcome.UNCHANGED


# Once a cancel was asked, a data set named for deleting is kept, also where the rest is too late to put back.
@overload
def decide_retire(judged: Judgement, delete_data_set: str | None, done: tuple[Event, ...]) -> Step[RetireResult]: ...
@overload
def decide_retire(
    judged: Judgement, delete_data_set: str | None, done: tuple[Event, ...], cancel: Cancel | None
) -> Step[RetireResult] | StepBack: ...
def decide_retire(
    judged: Judgement, delete_data_set: str | None, done: tuple[Event, ...], cancel: Cancel | None = None
) -> Step[RetireResult] | StepBack:
    if cancel is not None and is_turned_back(cancel):
        return retire_step_back(judged, delete_data_set)
    return with_logins_settled(retire_step(judged, None if cancel else delete_data_set, done), judged, done)


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
    # A file that is no route of Launchplane's is never loaded as one, so it is removed before anything is waited for.
    if route.unread is not None:
        return RemoveRoute(web_address, None)
    if (step := loading_step(judged)) is not None:
        return step
    if route.written is not None:
        return RemoveRoute(web_address, route.written)
    if lane.background_services:
        return Retire(min(running.container for running in lane.background_services))
    if lane.instances:
        return Retire(min(instance.container for instance in lane.instances))
    named = lane.data_set(delete_data_set)
    if named is not None:
        return DeleteDataSet(named)
    route_removed = any(isinstance(event, RemoveRoute) for event in done)
    route_removed_to = next((event.container for event in done if isinstance(event, RemoveRoute)), None)
    retired = [event.container for event in done if isinstance(event, Retire)]
    cleaned = any(isinstance(event, RemoveLogins) for event in done)
    deleted = next((event.found.data_set for event in done if isinstance(event, DeleteDataSet)), None)
    return Done(
        RetireResult(
            product=lane.product,
            lane=lane.lane,
            web_address=web_address,
            outcome=retire_outcome(route_removed or bool(retired) or cleaned, deleted),
            route_removed_to_container=route_removed_to,
            retired_containers=retired,
            deleted_data_set=deleted,
            kept_data_sets=sorted(found.data_set for found in lane.data_sets),
        )
    )
