from launchplane.instance import (
    DataSet,
    DataSetUse,
    InstanceState,
    LaneBuild,
    LaneRoute,
    LaneSeen,
    LoadedRoute,
    RouteStatus,
    RunningInstance,
    web_address_of,
)
from launchplane.judge import (
    Fix,
    background_service_problems,
    data_set_of,
    data_set_problems,
    instance_problems,
    lane_problems,
)
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
    RemoveInstance,
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
    WriteRoute,
)


def failure_report(why: str, log_tail: str) -> str:
    if not log_tail:
        return f"{why} It wrote no log."
    return f"{why}\nIts last log lines:\n{log_tail}"


def is_loaded_as_written(route: LaneRoute) -> bool:
    return (None if route.loaded is None else route.loaded.container) == route.written


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
        case _:
            return maintenance_change_made_by(event)


def changes_so_far(done: tuple[Event, ...]) -> str:
    changes = [change for change in map(change_made_by, done) if change is not None]
    if not changes:
        return "Nothing was changed."
    return f"Before that, this call {', '.join(changes)}, and those changes stay."


def disabled_route_stop(web_address: str, loaded: LoadedRoute, done: tuple[Event, ...]) -> Stop:
    return Stop(
        f"The proxy has loaded the route of {web_address} to {loaded.container} and reports it {loaded.status}, "
        f"so the web address reaches no instance. {changes_so_far(done)} "
        "The proxy's log says why; retiring the lane removes the route."
    )


def candidate_for(
    seen: LaneSeen, receiving: str | None, target: LaneBuild, product: Product, data_set: str | None, done: tuple[Event, ...]
) -> str | None:
    instances = {instance.container: instance for instance in seen.instances}
    started_here = [event.container for event in done if isinstance(event, StartInstance)]
    own = next((name for name in reversed(started_here) if name in instances and name != receiving), None)
    left_over = (
        name
        for name, instance in sorted(instances.items())
        if name != receiving and instance.data_set == data_set and not instance_problems(instance, target, product, seen)
    )
    return own or next(left_over, None)


def move_outcome(done: tuple[Event, ...]) -> Outcome:
    if any(isinstance(event, WriteRoute) for event in done):
        return Outcome.SWITCHED
    if any(isinstance(event, Retire | StartBackgroundService | StartDatabase) for event in done):
        return Outcome.CLEANED_UP
    return Outcome.UNCHANGED


def after_refusal(refused: CheckRefused, instances: dict[str, RunningInstance], answer: MoveResult) -> Action | Done[MoveResult]:
    if refused.container in instances:
        return RemoveInstance(refused.container)
    return Done(answer.model_copy(update={"outcome": Outcome.CHECK_FAILED, "error": refused.report}))


def check_or_problems(
    seen: LaneSeen, candidate: RunningInstance, target: LaneBuild, product: Product, done: tuple[Event, ...]
) -> Check | list[str]:
    check = Check(candidate.container, product.health_path)
    if check not in done or candidate.state != InstanceState.RUNNING:
        return check
    return lane_problems(seen, target, product, switching_to=candidate)


def check_then_route(
    seen: LaneSeen, candidate: RunningInstance, target: LaneBuild, product: Product, done: tuple[Event, ...]
) -> Action | Stop:
    problems = check_or_problems(seen, candidate, target, product, done)
    if isinstance(problems, Check):
        return problems
    if problems:
        return Stop(
            f"The instance {candidate.container} passed its check and the lane is not as it must be to switch to it, so the "
            f"web address was not switched: {'; '.join(problems)}. {changes_so_far(done)} Asking again replaces the instance."
        )
    return WriteRoute(web_address_of(target.product, target.lane), candidate.container)


def lane_data_set(seen: LaneSeen, receiving: RunningInstance | None, fresh: str) -> str | Stop:
    if receiving is not None and receiving.data_set is not None:
        return receiving.data_set
    names = sorted(data_set.data_set for data_set in seen.data_sets)
    if len(names) > 1:
        return Stop(
            f"The lane has the data sets {', '.join(names)} and no instance receiving visitors that uses one, "
            "so Launchplane cannot tell which one to start the instance on. Nothing was changed."
        )
    return names[0] if names else fresh


def step_toward_ready(found: DataSet, target: LaneBuild, image: str) -> Action:
    product, lane = target.product, target.lane
    if not found.networks:
        return CreateDataSetNetwork(product, lane, found.data_set)
    if not found.volumes:
        return CreateDataSetVolume(product, lane, found.data_set)
    if not found.databases:
        return CreateDatabase(product, lane, found.data_set, found.networks[0].name, found.volumes[0], image)
    server = found.databases[0]
    return StartDatabase(server.container) if server.state == InstanceState.STOPPED else AwaitDatabase(server.container)


def prepared(
    seen: LaneSeen, data_set: str | None, fresh: str | None, target: LaneBuild, product: Product, done: tuple[Event, ...]
) -> Action | Stop | DataSetUse | None:
    if data_set is None:
        return None
    if product.database is None:
        return None
    found = data_set_of(seen, data_set)
    if found is None and data_set != fresh:
        return Stop(
            f"The instance receiving visitors uses the data set {data_set}, which is not on the host. {changes_so_far(done)}"
        )
    if found is None:
        return CreateDataSetNetwork(target.product, target.lane, data_set)
    problems = data_set_problems(found, seen, has_database=True)
    unfixable = [problem.text for problem in problems if not problem.fixable]
    # Nothing in a data set is made or started while it has a problem no step puts right.
    if unfixable:
        return Stop(
            f"The data set {found.data_set} is not as it must be, and fixing it is not Launchplane's to choose: "
            f"{'; '.join(unfixable)}. {changes_so_far(done)}"
        )
    if problems:
        return step_toward_ready(found, target, product.database.image)
    return DataSetUse(found.data_set, found.networks[0].name, found.databases[0].container)


def background_service_step(
    seen: LaneSeen, serving: RunningInstance, target: LaneBuild, product: Product, use: DataSetUse | None, done: tuple[Event, ...]
) -> Action | None:
    problems = background_service_problems(seen, serving, product)
    if not problems or any(isinstance(event, BackgroundServiceRefused) for event in done):
        return None
    command = () if product.background_service is None else tuple(product.background_service.command)
    start = StartBackgroundService(target, serving.container, use, command)
    first = problems[0]
    if first.container is None:
        return start
    if first.fix == Fix.WATCH or (first.fix == Fix.REPLACE_OR_WATCH and start in done):
        return AwaitBackgroundService(first.container)
    return Retire(first.container)


def settle_serving(
    seen: LaneSeen, serving: RunningInstance, target: LaneBuild, product: Product, answer: MoveResult, done: tuple[Event, ...]
) -> Action | Done[MoveResult] | Stop:
    use = prepared(seen, serving.data_set, None, target, product, done)
    if not (use is None or isinstance(use, DataSetUse)):
        return use
    if (step := background_service_step(seen, serving, target, product, use, done)) is not None:
        return step
    old = sorted(instance.container for instance in seen.instances if instance.container != serving.container)
    if old:
        return Retire(old[0])
    refused = next((event for event in done if isinstance(event, BackgroundServiceRefused)), None)
    if refused is not None:
        return Done(answer.model_copy(update={"outcome": Outcome.BACKGROUND_SERVICE_FAILED, "error": refused.report}))
    problems = lane_problems(seen, target, product)
    if problems:
        return Stop(
            f"The lane is not as it must be, so the move does not answer success: {'; '.join(problems)}. {changes_so_far(done)}"
        )
    return Done(answer)


def decide_move(
    seen: LaneSeen, target: LaneBuild, product: Product, fresh: FreshNames, done: tuple[Event, ...]
) -> Action | Done[MoveResult] | Stop:
    if not seen.route.proxy_runs:
        return StartProxy()
    web_address = web_address_of(target.product, target.lane)
    if seen.route.in_maintenance:
        return Stop(
            f"{target.lane} of {target.product} is in maintenance: {web_address} shows the back soon page. End maintenance "
            f"first, with POST /lanes/{target.product}/{target.lane}/maintenance/end. Nothing was changed."
        )
    if not is_loaded_as_written(seen.route):
        return AwaitRoute(web_address, seen.route.written)
    if seen.route.loaded is not None and seen.route.loaded.status != RouteStatus.ENABLED:
        return disabled_route_stop(web_address, seen.route.loaded, done)
    instances = {instance.container: instance for instance in seen.instances}
    receiving = None if seen.route.written is None else instances.get(seen.route.written)
    answer = MoveResult(
        product=target.product,
        lane=target.lane,
        build=target.build,
        web_address=web_address,
        outcome=move_outcome(done),
        container_receiving_visitors=seen.route.written,
        data_set=None if receiving is None else receiving.data_set,
    )
    refused = next((event for event in done if isinstance(event, CheckRefused)), None)
    if refused is not None:
        return after_refusal(refused, instances, answer)
    # Only one background service of a lane runs at a time, so one of any other instance stops before anything else.
    of_other_instances = sorted(running.container for running in seen.background_services if running.instance != seen.route.written)
    if of_other_instances:
        return Retire(of_other_instances[0])
    if receiving is not None and not instance_problems(receiving, target, product, seen):
        return settle_serving(seen, receiving, target, product, answer, done)
    return replace_receiving(seen, receiving, target, product, fresh, done)


def replace_receiving(
    seen: LaneSeen,
    receiving: RunningInstance | None,
    target: LaneBuild,
    product: Product,
    fresh: FreshNames,
    done: tuple[Event, ...],
) -> Action | Stop:
    data_set = None if product.database is None else lane_data_set(seen, receiving, fresh.data_set)
    if isinstance(data_set, Stop):
        return data_set
    instances = {instance.container: instance for instance in seen.instances}
    receiving_container = None if receiving is None else receiving.container
    candidate = candidate_for(seen, receiving_container, target, product, data_set, done)
    strays = sorted(instances.keys() - {receiving_container, candidate})
    if strays:
        return Retire(strays[0])
    use = prepared(seen, data_set, fresh.data_set, target, product, done)
    if not (use is None or isinstance(use, DataSetUse)):
        return use
    if candidate is not None:
        return check_then_route(seen, instances[candidate], target, product, done)
    return StartInstance(target, fresh.container, use)


def retire_outcome(retired: bool, deleted: str | None) -> RetireOutcome:
    if deleted is not None:
        return RetireOutcome.DELETED
    return RetireOutcome.RETIRED if retired else RetireOutcome.UNCHANGED


def decide_retire(
    seen: LaneSeen, product: str, lane: str, delete_data_set: str | None, done: tuple[Event, ...]
) -> Action | Done[RetireResult] | Stop:
    if not seen.route.proxy_runs:
        return StartProxy()
    web_address = web_address_of(product, lane)
    if not is_loaded_as_written(seen.route):
        return AwaitRoute(web_address, seen.route.written)
    if seen.route.written is not None:
        return RemoveRoute(web_address, seen.route.written)
    if seen.background_services:
        return Retire(min(running.container for running in seen.background_services))
    if seen.instances:
        return Retire(min(instance.container for instance in seen.instances))
    named = next((found for found in seen.data_sets if found.data_set == delete_data_set), None)
    if named is not None:
        return DeleteDataSet(named)
    route_removed_to = next((event.container for event in done if isinstance(event, RemoveRoute)), None)
    retired = [event.container for event in done if isinstance(event, Retire)]
    deleted = next((event.found.data_set for event in done if isinstance(event, DeleteDataSet)), None)
    return Done(
        RetireResult(
            product=product,
            lane=lane,
            web_address=web_address,
            outcome=retire_outcome(route_removed_to is not None or bool(retired), deleted),
            route_removed_to_container=route_removed_to,
            retired_containers=retired,
            deleted_data_set=deleted,
            kept_data_sets=sorted(found.data_set for found in seen.data_sets),
        )
    )
