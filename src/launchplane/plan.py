from launchplane.instance import (
    DataSet,
    DataSetUse,
    InstanceState,
    LaneBuild,
    LaneReading,
    RunningInstance,
)
from launchplane.judge import LOGIN_KINDS, Finding, Judgement, Kind, subjects_of
from launchplane.product import Product
from launchplane.steps import (
    Action,
    AwaitBackgroundService,
    AwaitDatabase,
    AwaitRoute,
    BackgroundServiceRefused,
    Check,
    CheckRefused,
    CreateDatabase,
    CreateDataSetNetwork,
    CreateDataSetVolume,
    DeleteDataSet,
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
    TooLate,
    WriteRoute,
)

type Step[Answer] = Action | Done[Answer] | Stop
type StepBack = Step[PutBack] | TooLate
MADE_READY = {Kind.NO_NETWORK, Kind.NO_VOLUME, Kind.NO_DATABASE, Kind.STOPPED, Kind.NOT_ACCEPTING}
LOGINS_UNKNOWN = {Kind.LOGINS_UNREAD, Kind.NOT_ACCEPTING, Kind.NOT_ASKED}


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
        case WriteRoute(container=container):
            return f"wrote the route to {container}"
        case StartBackgroundService(instance=instance):
            return f"started the background service of {instance}"
        case Retire(container=container):
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


def route_step(judged: Judgement, done: tuple[Event, ...]) -> Action | Stop | None:
    if judged.of_kind(Kind.PROXY_STOPPED):
        return StartProxy()
    if judged.of_kind(Kind.ROUTE_NOT_LOADED):
        return AwaitRoute(judged.lane.web_address, judged.lane.route.written)
    disabled = judged.of_kind(Kind.ROUTE_DISABLED)
    if disabled:
        return Stop(f"{sentences(disabled)}. {changes_so_far(done)} The proxy's log says why; retiring the lane removes the route.")
    return None


# The instance this call started is taken before one left over: which it started, the host cannot show.
def candidate_for(
    judged: Judgement, receiving: str | None, target: LaneBuild, data_set: str | None, done: tuple[Event, ...]
) -> str | None:
    instances = {instance.container: instance for instance in judged.lane.instances}
    started_here = [event.container for event in done if isinstance(event, StartInstance)]
    own = next((name for name in reversed(started_here) if name in instances and name != receiving), None)
    left_over = (
        name
        for name, instance in sorted(instances.items())
        if name != receiving and instance.data_set == data_set and is_fit(judged, instance, target)
    )
    return own or next(left_over, None)


def is_fit(judged: Judgement, instance: RunningInstance, target: LaneBuild) -> bool:
    return instance.build == target.build and not judged.about_instance(instance)


def move_outcome(done: tuple[Event, ...]) -> Outcome:
    if any(isinstance(event, WriteRoute) for event in done):
        return Outcome.SWITCHED
    return Outcome.REPAIRED if changes_made(done) else Outcome.UNCHANGED


def after_refusal(refused: CheckRefused, lane: LaneReading, answer: MoveResult) -> Action | Done[MoveResult]:
    if any(instance.container == refused.container for instance in lane.instances):
        return RemoveInstance(refused.container)
    return Done(answer.model_copy(update={"outcome": Outcome.CHECK_FAILED, "error": refused.report}))


def data_set_change(lane: LaneReading, switching_to: RunningInstance) -> list[str]:
    receiving = lane.active
    if receiving is None or receiving.data_set in (None, switching_to.data_set):
        return []
    return [
        f"The instance {switching_to.container} runs against the data set {switching_to.data_set}, "
        f"not {receiving.data_set}, which the instance receiving visitors runs against"
    ]


# Whether a check passed in this call, the host cannot show, so it is asked of what the call has done.
def check_or_problems(judged: Judgement, candidate: RunningInstance, product: Product, done: tuple[Event, ...]) -> Check | list[str]:
    check = Check(candidate.container, product.health_path)
    if check not in done or candidate.state != InstanceState.RUNNING:
        return check
    in_the_way = [*judged.about_instance(candidate), *judged.about_data_set(judged.lane.data_set(candidate.data_set))]
    return [*[finding.sentence for finding in in_the_way], *data_set_change(judged.lane, candidate)]


def check_then_route(judged: Judgement, candidate: RunningInstance, product: Product, done: tuple[Event, ...]) -> Action | Stop:
    problems = check_or_problems(judged, candidate, product, done)
    if isinstance(problems, Check):
        return problems
    if problems:
        return Stop(
            f"The instance {candidate.container} passed its check and the lane is not as it must be to switch to it, so the "
            f"web address was not switched: {'; '.join(problems)}. {changes_so_far(done)} Asking again replaces the instance."
        )
    return WriteRoute(judged.lane.web_address, candidate.container)


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


def step_toward_ready(found: DataSet, kinds: set[Kind], lane: LaneReading, image: str) -> Action:
    if Kind.NO_NETWORK in kinds:
        return CreateDataSetNetwork(lane.product, lane.lane, found.data_set)
    if Kind.NO_VOLUME in kinds:
        return CreateDataSetVolume(lane.product, lane.lane, found.data_set)
    if Kind.NO_DATABASE in kinds:
        return CreateDatabase(lane.product, lane.lane, found.data_set, found.networks[0].name, found.volumes[0], image)
    server = found.databases[0]
    return StartDatabase(server.container) if Kind.STOPPED in kinds else AwaitDatabase(server.container)


def prepared(
    judged: Judgement, data_set: str | None, fresh: str | None, product: Product, done: tuple[Event, ...]
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
        return CreateDataSetNetwork(lane.product, lane.lane, data_set)
    findings = judged.about_data_set(found)
    beyond = [finding for finding in findings if finding.kind not in MADE_READY]
    # Nothing in a data set is made or started while it has a problem no step puts right.
    if beyond:
        return Stop(
            f"The data set {found.data_set} is not as it must be, and fixing it is not Launchplane's to choose: "
            f"{sentences(beyond)}. {changes_so_far(done)}"
        )
    if findings:
        return step_toward_ready(found, {finding.kind for finding in findings}, lane, product.database.image)
    return DataSetUse(found.data_set, found.networks[0].name, found.databases[0].container)


# Whether this call started the background service or a watch refused it, the host cannot show.
def background_service_step(
    judged: Judgement, serving: RunningInstance, target: LaneBuild, product: Product, use: DataSetUse | None, done: tuple[Event, ...]
) -> Action | None:
    findings = judged.about_background_services()
    if not findings or any(isinstance(event, BackgroundServiceRefused) for event in done):
        return None
    command = () if product.background_service is None else tuple(product.background_service.command)
    start = StartBackgroundService(target, serving.container, use, command)
    first = findings[0]
    if first.kind == Kind.NO_BACKGROUND_SERVICE:
        return start
    if first.kind == Kind.UNSTEADY or (first.kind in {Kind.STOPPED, Kind.NOT_RUNNING} and start in done):
        return AwaitBackgroundService(first.subject)
    return Retire(first.subject)


# A move answers for the instance it serves from and that instance's data set, not for another data set of the lane.
def left_for_a_move(judged: Judgement, serving: RunningInstance) -> list[Finding]:
    of_other_data_sets = {
        subject for found in judged.lane.data_sets if found.data_set != serving.data_set for subject in subjects_of(found)
    }
    return [finding for finding in judged.findings if finding.kind not in LOGIN_KINDS and finding.subject not in of_other_data_sets]


def settle_serving(
    judged: Judgement, serving: RunningInstance, target: LaneBuild, product: Product, answer: MoveResult, done: tuple[Event, ...]
) -> Step[MoveResult]:
    use = prepared(judged, serving.data_set, None, product, done)
    if not (use is None or isinstance(use, DataSetUse)):
        return use
    if (step := background_service_step(judged, serving, target, product, use, done)) is not None:
        return step
    old = sorted(instance.container for instance in judged.lane.instances if instance.container != serving.container)
    if old:
        return Retire(old[0])
    refused = next((event for event in done if isinstance(event, BackgroundServiceRefused)), None)
    if refused is not None:
        return Done(answer.model_copy(update={"outcome": Outcome.BACKGROUND_SERVICE_FAILED, "error": refused.report}))
    left = left_for_a_move(judged, serving)
    if left:
        return Stop(f"The lane is not as it must be, so the move does not answer success: {sentences(left)}. {changes_so_far(done)}")
    return Done(answer)


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


def decide_move(
    judged: Judgement, target: LaneBuild, product: Product, fresh: FreshNames, done: tuple[Event, ...]
) -> Step[MoveResult]:
    return with_logins_settled(move_step(judged, target, product, fresh, done), judged, done)


def move_step(
    judged: Judgement, target: LaneBuild, product: Product, fresh: FreshNames, done: tuple[Event, ...]
) -> Step[MoveResult]:
    lane = judged.lane
    route, web_address = lane.route, lane.web_address
    if route.in_maintenance:
        return Stop(
            f"{target.lane} of {target.product} is in maintenance: the routes file of {web_address} points at the back soon "
            f"page. End maintenance first, with POST /lanes/{target.product}/{target.lane}/maintenance/end. Nothing was changed."
        )
    if (step := route_step(judged, done)) is not None:
        return step
    receiving = next((instance for instance in lane.instances if instance.container == route.written), None)
    answer = MoveResult(
        product=target.product,
        lane=target.lane,
        build=target.build,
        web_address=web_address,
        outcome=move_outcome(done),
        container_receiving_visitors=route.written,
        data_set=None if receiving is None else receiving.data_set,
        did=changes_made(done),
    )
    refused = next((event for event in done if isinstance(event, CheckRefused)), None)
    if refused is not None:
        return after_refusal(refused, lane, answer)
    # Only one background service of a lane runs at a time, so one of any other instance stops before anything else.
    of_other_instances = sorted(running.container for running in lane.background_services if running.instance != route.written)
    if of_other_instances:
        return Retire(of_other_instances[0])
    if receiving is not None and is_fit(judged, receiving, target):
        return settle_serving(judged, receiving, target, product, answer, done)
    return replace_receiving(judged, receiving, target, product, fresh, done)


def replace_receiving(
    judged: Judgement,
    receiving: RunningInstance | None,
    target: LaneBuild,
    product: Product,
    fresh: FreshNames,
    done: tuple[Event, ...],
) -> Action | Stop:
    lane = judged.lane
    data_set = None if product.database is None else lane_data_set(lane, receiving, fresh.data_set)
    if isinstance(data_set, Stop):
        return data_set
    instances = {instance.container: instance for instance in lane.instances}
    receiving_container = None if receiving is None else receiving.container
    candidate = candidate_for(judged, receiving_container, target, data_set, done)
    strays = sorted(instances.keys() - {receiving_container, candidate})
    if strays:
        return Retire(strays[0])
    use = prepared(judged, data_set, fresh.data_set, product, done)
    if not (use is None or isinstance(use, DataSetUse)):
        return use
    if candidate is not None:
        return check_then_route(judged, instances[candidate], product, done)
    return StartInstance(target, fresh.container, use)


def retire_outcome(retired: bool, deleted: str | None) -> RetireOutcome:
    if deleted is not None:
        return RetireOutcome.DELETED
    return RetireOutcome.RETIRED if retired else RetireOutcome.UNCHANGED


def decide_retire(judged: Judgement, delete_data_set: str | None, done: tuple[Event, ...]) -> Step[RetireResult]:
    return with_logins_settled(retire_step(judged, delete_data_set, done), judged, done)


def retire_step(judged: Judgement, delete_data_set: str | None, done: tuple[Event, ...]) -> Step[RetireResult]:
    lane = judged.lane
    route, web_address = lane.route, lane.web_address
    if judged.of_kind(Kind.PROXY_STOPPED):
        return StartProxy()
    if judged.of_kind(Kind.ROUTE_NOT_LOADED):
        return AwaitRoute(web_address, route.written)
    if route.written is not None:
        return RemoveRoute(web_address, route.written)
    if lane.background_services:
        return Retire(min(running.container for running in lane.background_services))
    if lane.instances:
        return Retire(min(instance.container for instance in lane.instances))
    named = lane.data_set(delete_data_set)
    if named is not None:
        return DeleteDataSet(named)
    route_removed_to = next((event.container for event in done if isinstance(event, RemoveRoute)), None)
    retired = [event.container for event in done if isinstance(event, Retire)]
    cleaned = any(isinstance(event, RemoveLogins) for event in done)
    deleted = next((event.found.data_set for event in done if isinstance(event, DeleteDataSet)), None)
    return Done(
        RetireResult(
            product=lane.product,
            lane=lane.lane,
            web_address=web_address,
            outcome=retire_outcome(route_removed_to is not None or bool(retired) or cleaned, deleted),
            route_removed_to_container=route_removed_to,
            retired_containers=retired,
            deleted_data_set=deleted,
            kept_data_sets=sorted(found.data_set for found in lane.data_sets),
        )
    )


def put_back(done: tuple[Event, ...]) -> Done[PutBack]:
    return Done(PutBack(put_back=changes_made(done)))


# Whether visitors have been switched is read from the routes file, as the move reads which instance receives them.
# What the move started is found as the move finds the instance to switch to: beside the one receiving, of its build.
def move_step_back(judged: Judgement, target: LaneBuild, done: tuple[Event, ...]) -> StepBack:
    lane = judged.lane
    receiving = next((instance for instance in lane.instances if instance.container == lane.route.written), None)
    if receiving is not None and receiving.build == target.build:
        return TooLate(
            f"{lane.web_address} already sends its visitors to {receiving.container}, an instance of {target.build}, so the "
            "move could not be cancelled and is finished as asked. Moving the lane to its earlier build puts that build back."
        )
    # A move starts nothing in a lane in maintenance, and the instance stopped there is the lane's own.
    started = [] if lane.route.in_maintenance else [each.container for each in lane.instances if each.build == target.build]
    if started:
        return RemoveInstance(min(started))
    return with_logins_settled(put_back(done), judged, done)


# The first thing a retire changes is the route, and nothing it does after that can be put back.
def retire_step_back(judged: Judgement, done: tuple[Event, ...]) -> StepBack:
    lane = judged.lane
    if lane.route.written is not None:
        return put_back(done)
    return TooLate(
        f"{lane.web_address} has no route, so retiring {lane.lane} of {lane.product} could not be cancelled and is finished "
        "as asked: a retire removes the route first, and Launchplane does not make a removed instance again by itself. "
        "Moving the lane to a build brings it back on its data set."
    )
