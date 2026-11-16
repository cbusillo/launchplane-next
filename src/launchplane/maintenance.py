from launchplane.instance import (
    BACK_SOON,
    DataSetUse,
    InstanceState,
    LaneBuild,
    LaneRoute,
    LaneSeen,
    ProxyNotRunning,
    RouteStatus,
    RunningBackgroundService,
    RunningInstance,
    web_address_of,
)
from launchplane.judge import StillUp, active_instance, findings_at_rest, killed_databases, still_up
from launchplane.plan import changes_so_far, check_or_problems, disabled_route_stop, is_loaded_as_written, prepared
from launchplane.product import Product
from launchplane.steps import (
    Action,
    AwaitBackgroundService,
    AwaitRoute,
    BackgroundServiceRefused,
    Check,
    CheckRefused,
    Done,
    Event,
    MaintenanceOutcome,
    MaintenanceResult,
    ShowBackSoon,
    StartProxy,
    StartRefused,
    StartStopped,
    Stop,
    StopDatabase,
    StopPart,
    WriteRoute,
)

type Step = Action | Done[MaintenanceResult] | Stop


def route_step(route: LaneRoute, web_address: str, done: tuple[Event, ...]) -> Action | Stop | None:
    if not is_loaded_as_written(route):
        return AwaitRoute(web_address, route.written)
    if route.loaded is not None and route.loaded.status != RouteStatus.ENABLED:
        return disabled_route_stop(web_address, route.loaded, done)
    return None


def stop_of(up: StillUp) -> Action:
    return StopDatabase(up.container) if up.is_database else StopPart(up.container)


def has_nothing(seen: LaneSeen) -> bool:
    return not (seen.instances or seen.background_services or seen.data_sets)


def decide_begin(seen: LaneSeen, product: str, lane: str, done: tuple[Event, ...]) -> Step:
    if isinstance(seen.route, ProxyNotRunning):
        return StartProxy()
    web_address = web_address_of(product, lane)
    step = route_step(seen.route, web_address, done)
    if step is not None:
        return step
    if seen.route.written != BACK_SOON:
        if seen.route.written is None and has_nothing(seen):
            return Stop(
                f"{lane} of {product} has no route, no instance, no background service and no data set on the host, "
                "so there is nothing to put in maintenance. Nothing was changed."
            )
        return ShowBackSoon(web_address)
    up = still_up(seen)
    if up:
        return stop_of(up[0])
    answer = MaintenanceResult(
        product=product,
        lane=lane,
        web_address=web_address,
        outcome=MaintenanceOutcome.IN_MAINTENANCE,
        in_maintenance=True,
        since=seen.route.back_soon_since,
    )
    killed = killed_databases(seen)
    if killed:
        return Done(answer.model_copy(update={"outcome": MaintenanceOutcome.DATABASE_KILLED, "error": "; ".join(killed)}))
    return Done(answer)


def the_instance(seen: LaneSeen, product: str, lane: str, done: tuple[Event, ...]) -> tuple[RunningInstance, LaneBuild] | Stop:
    way_out = (
        f"Retiring the lane and moving it to a build again brings it back on its data set. {changes_so_far(done)} "
        "The web address still shows the back soon page."
    )
    if len(seen.instances) != 1:
        names = ", ".join(sorted(instance.container for instance in seen.instances)) or "none"
        return Stop(
            f"{lane} of {product} has {len(seen.instances)} instances ({names}) where ending maintenance needs one, "
            f"so Launchplane cannot tell which to start. {way_out}"
        )
    [instance] = seen.instances
    if instance.build is None:
        return Stop(f"The instance {instance.container} does not say which build it is, so it cannot be judged. {way_out}")
    return instance, LaneBuild(product=product, lane=lane, build=instance.build)


def stop_again(seen: LaneSeen, refused: CheckRefused | StartRefused, answer: MaintenanceResult) -> Step:
    up = still_up(seen)
    if up:
        return stop_of(up[0])
    return Done(answer.model_copy(update={"outcome": MaintenanceOutcome.WEB_PART_FAILED, "error": refused.report}))


def bring_back(
    seen: LaneSeen, product: str, lane: str, description: Product, answer: MaintenanceResult, done: tuple[Event, ...]
) -> Step:
    refused = next((event for event in done if isinstance(event, CheckRefused | StartRefused)), None)
    if refused is not None:
        return stop_again(seen, refused, answer)
    found = the_instance(seen, product, lane, done)
    if isinstance(found, Stop):
        return found
    instance, target = found
    use = prepared(seen, instance.data_set, None, target, description, done)
    if not (use is None or isinstance(use, DataSetUse)):
        return use
    start = StartStopped(instance.container)
    if instance.state == InstanceState.STOPPED and start not in done:
        return start
    problems = check_or_problems(seen, instance, target, description, done)
    if isinstance(problems, Check):
        return problems
    if problems:
        return Stop(
            f"The instance {instance.container} passed its check and the lane is not as it must be to switch back to it, so "
            f"the web address still shows the back soon page: {'; '.join(problems)}. {changes_so_far(done)} "
            "Beginning maintenance again stops what was started."
        )
    return WriteRoute(answer.web_address, instance.container)


def own_background_service(seen: LaneSeen, description: Product) -> RunningBackgroundService | None:
    active = active_instance(seen)
    if active is None or description.background_service is None:
        return None
    own = [running for running in seen.background_services if running.instance == active.container]
    return own[0] if len(own) == 1 else None


def background_service_step(seen: LaneSeen, description: Product, done: tuple[Event, ...]) -> Action | None:
    background_service = own_background_service(seen, description)
    if background_service is None:
        return None
    start = StartStopped(background_service.container)
    if background_service.state == InstanceState.STOPPED and start not in done:
        return start
    watch = AwaitBackgroundService(background_service.container)
    return watch if start in done and watch not in done else None


def finish(seen: LaneSeen, description: Product, answer: MaintenanceResult, done: tuple[Event, ...]) -> Step:
    answer = answer.model_copy(update={"in_maintenance": False, "since": None})
    refused = next((event for event in done if isinstance(event, BackgroundServiceRefused | StartRefused)), None)
    if refused is not None:
        return Done(answer.model_copy(update={"outcome": MaintenanceOutcome.BACKGROUND_SERVICE_FAILED, "error": refused.report}))
    step = background_service_step(seen, description, done)
    if step is not None:
        return step
    if not any(isinstance(event, WriteRoute | StartStopped) for event in done):
        return Done(answer.model_copy(update={"outcome": MaintenanceOutcome.NOT_IN_MAINTENANCE}))
    problems = [finding.text for finding in findings_at_rest(seen, description)]
    if problems:
        return Stop(
            f"Maintenance ended: {answer.web_address} no longer shows the back soon page. The lane is not as it must be, "
            f"so the call does not answer success: {'; '.join(problems)}. {changes_so_far(done)} "
            "Moving the lane to its build puts it right."
        )
    return Done(answer)


def decide_end(seen: LaneSeen, product: str, lane: str, description: Product, done: tuple[Event, ...]) -> Step:
    if isinstance(seen.route, ProxyNotRunning):
        return StartProxy()
    web_address = web_address_of(product, lane)
    step = route_step(seen.route, web_address, done)
    if step is not None:
        return step
    answer = MaintenanceResult(
        product=product,
        lane=lane,
        web_address=web_address,
        outcome=MaintenanceOutcome.ENDED,
        in_maintenance=True,
        since=seen.route.back_soon_since,
    )
    if seen.route.written == BACK_SOON:
        return bring_back(seen, product, lane, description, answer, done)
    return finish(seen, description, answer, done)
