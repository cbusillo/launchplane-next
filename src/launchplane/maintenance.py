from launchplane.instance import (
    DataSetUse,
    InstanceState,
    LaneBuild,
    LaneRoute,
    LaneSeen,
    RouteStatus,
    RunningInstance,
    web_address_of,
)
from launchplane.judge import Fix, StillUp, background_service_problems, findings_as_read, killed_databases, still_up
from launchplane.plan import (
    changes_so_far,
    check_or_problems,
    disabled_route_stop,
    failure_report,
    is_loaded_as_written,
    prepared,
)
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
    LogRead,
    MaintenanceOutcome,
    MaintenanceResult,
    ReadLog,
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


def answer_for(product: str, lane: str, route: LaneRoute, outcome: MaintenanceOutcome) -> MaintenanceResult:
    return MaintenanceResult(
        product=product,
        lane=lane,
        web_address=web_address_of(product, lane),
        outcome=outcome,
        in_maintenance=route.in_maintenance,
        since=route.back_soon_since,
    )


def not_as_it_must_be(seen: LaneSeen, description: Product, done: tuple[Event, ...]) -> Stop | None:
    problems = [finding.text for finding in findings_as_read(seen, description)]
    if not problems:
        return None
    state = "is in maintenance" if seen.route.in_maintenance else "is out of maintenance"
    return Stop(
        f"The lane {state} and is not as it must be, so the call does not answer success: {'; '.join(problems)}. "
        f"{changes_so_far(done)}"
    )


def decide_begin(seen: LaneSeen, product: str, lane: str, description: Product, done: tuple[Event, ...]) -> Step:
    route = seen.route
    if not route.proxy_runs:
        return StartProxy()
    web_address = web_address_of(product, lane)
    step = route_step(route, web_address, done)
    if step is not None:
        return step
    if not route.in_maintenance:
        if route.written is None and has_nothing(seen):
            return Stop(
                f"{lane} of {product} has no route, no instance, no background service and no data set on the host, "
                "so there is nothing to put in maintenance. Nothing was changed."
            )
        return ShowBackSoon(web_address)
    up = still_up(seen)
    if up:
        return stop_of(up[0])
    killed = killed_databases(seen)
    if killed:
        answer = answer_for(product, lane, route, MaintenanceOutcome.DATABASE_KILLED)
        return Done(answer.model_copy(update={"error": "; ".join(killed)}))
    return not_as_it_must_be(seen, description, done) or Done(answer_for(product, lane, route, MaintenanceOutcome.IN_MAINTENANCE))


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


type Refusal = CheckRefused | StartRefused | BackgroundServiceRefused


def refusal_report(refused: Refusal, log: LogRead) -> str:
    if log.unreadable is not None:
        return f"{refused.report} Its log could not be read: {log.unreadable}"
    return failure_report(refused.report, log.tail)


# Everything stops before the log is read, so a log that cannot be read leaves nothing running.
def stop_again(seen: LaneSeen, refused: Refusal, answer: MaintenanceResult, done: tuple[Event, ...]) -> Step:
    up = still_up(seen)
    if up:
        return stop_of(up[0])
    logs = [event for event in done if isinstance(event, LogRead)]
    log = next((each for each in logs if each.container == refused.container), None)
    if log is None:
        return ReadLog(refused.container)
    of_the_web_part = any(instance.container == refused.container for instance in seen.instances)
    outcome = MaintenanceOutcome.WEB_PART_FAILED if of_the_web_part else MaintenanceOutcome.BACKGROUND_SERVICE_FAILED
    return Done(answer.model_copy(update={"outcome": outcome, "error": refusal_report(refused, log)}))


def background_service_step(
    seen: LaneSeen, instance: RunningInstance, description: Product, done: tuple[Event, ...]
) -> Action | Stop | None:
    problems = background_service_problems(seen, instance, description)
    if not problems:
        return None
    first = problems[0]
    if first.container is None or first.fix == Fix.REPLACE:
        return Stop(
            f"The background service is not one ending maintenance can start: {first.text}. {changes_so_far(done)} "
            "The web address still shows the back soon page. Retiring the lane and moving it to a build again brings it back."
        )
    start = StartStopped(first.container)
    stopped = any(
        running.container == first.container and running.state == InstanceState.STOPPED for running in seen.background_services
    )
    if stopped and start not in done:
        return start
    return AwaitBackgroundService(first.container)


def bring_back(
    seen: LaneSeen, product: str, lane: str, description: Product, answer: MaintenanceResult, done: tuple[Event, ...]
) -> Step:
    refused = next((event for event in done if isinstance(event, CheckRefused | StartRefused | BackgroundServiceRefused)), None)
    if refused is not None:
        return stop_again(seen, refused, answer, done)
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
    return background_service_step(seen, instance, description, done) or WriteRoute(answer.web_address, instance.container)


def decide_end(seen: LaneSeen, product: str, lane: str, description: Product, done: tuple[Event, ...]) -> Step:
    route = seen.route
    if not route.proxy_runs:
        return StartProxy()
    step = route_step(route, web_address_of(product, lane), done)
    if step is not None:
        return step
    answer = answer_for(product, lane, route, MaintenanceOutcome.ENDED)
    if route.in_maintenance:
        return bring_back(seen, product, lane, description, answer, done)
    return not_as_it_must_be(seen, description, done) or Done(answer)
