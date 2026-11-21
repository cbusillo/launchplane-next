from launchplane.instance import BACK_SOON, DataSetUse, InstanceState, LaneReading, LaneRoute, RunningInstance, web_address_of
from launchplane.judge import Judgement, Kind
from launchplane.plan import (
    Step,
    StepBack,
    failure_report,
    is_checked,
    is_turned_back,
    left_for_a_move,
    login_step,
    prepared,
    problems_switching_to,
    put_back_or_what_is_left,
    route_step,
    sentences,
    unread_route,
)
from launchplane.product import Product
from launchplane.steps import (
    Action,
    AwaitBackgroundService,
    BackgroundServiceRefused,
    Cancel,
    Check,
    CheckRefused,
    Done,
    EditRoute,
    Event,
    LoginsUnread,
    LogRead,
    MaintenanceOutcome,
    MaintenanceResult,
    ReadLog,
    StartRefused,
    StartStopped,
    Stop,
    StopDatabase,
    StopPart,
    TooLate,
)

STARTED_AGAIN = {Kind.STOPPED, Kind.NOT_RUNNING, Kind.UNSTEADY}


# What writes stops before the web part, and the database stops last.
def next_to_stop(lane: LaneReading) -> Action | None:
    parts = [
        *sorted(lane.background_services, key=lambda each: each.container),
        *sorted(lane.instances, key=lambda each: each.container),
    ]
    for part in parts:
        if part.state != InstanceState.STOPPED:
            return StopPart(part.container)
    for database in sorted(lane.databases, key=lambda each: each.container):
        if database.state != InstanceState.STOPPED:
            return StopDatabase(database.container)
    return None


def has_nothing(lane: LaneReading) -> bool:
    return not (lane.instances or lane.background_services or lane.data_sets)


def answer_for(product: str, lane: str, route: LaneRoute, outcome: MaintenanceOutcome) -> MaintenanceResult:
    return MaintenanceResult(
        product=product,
        lane=lane,
        web_address=web_address_of(product, lane),
        outcome=outcome,
        in_maintenance=route.in_maintenance,
        since=route.back_soon_since,
    )


def not_as_it_must_be(judged: Judgement, done: tuple[Event, ...]) -> Stop | None:
    holders_unread = {event.container for event in done if isinstance(event, LoginsUnread)}
    left = (finding for finding in judged.findings if finding.kind != Kind.LOGINS_UNREAD)
    found = [finding for finding in left if not (finding.kind == Kind.LOGIN_UNHELD and finding.subject in holders_unread)]
    if not found:
        return None
    state = "is in maintenance" if judged.lane.route.in_maintenance else "is out of maintenance"
    return Stop(f"The lane {state} and is not as it must be, so the call does not answer success: {sentences(found)}.")


def decide_begin(judged: Judgement, done: tuple[Event, ...], cancel: Cancel | None = None) -> Step[MaintenanceResult] | StepBack:
    lane = judged.lane
    route = lane.route
    step = unread_route(lane, "put in maintenance") or route_step(judged)
    if step is not None:
        return step
    if cancel is not None and is_turned_back(cancel):
        return begin_step_back(judged)
    if not route.in_maintenance:
        if route.written is None and has_nothing(lane):
            return Stop(
                f"{lane.lane} of {lane.product} has no route, no instance, no background service and no data set on the host, "
                "so there is nothing to put in maintenance."
            )
        return EditRoute(lane.web_address, BACK_SOON, route.installed)
    if (stop := next_to_stop(lane)) is not None:
        return stop
    killed = judged.of_kind(Kind.KILLED)
    if killed:
        answer = answer_for(lane.product, lane.lane, route, MaintenanceOutcome.DATABASE_KILLED)
        return Done(answer.model_copy(update={"error": sentences(killed)}))
    return not_as_it_must_be(judged, done) or Done(answer_for(lane.product, lane.lane, route, MaintenanceOutcome.IN_MAINTENANCE))


def the_instance(lane: LaneReading) -> RunningInstance | Stop:
    if len(lane.instances) == 1:
        return lane.instances[0]
    names = ", ".join(sorted(instance.container for instance in lane.instances)) or "none"
    return Stop(
        f"{lane.lane} of {lane.product} has {len(lane.instances)} instances ({names}) where ending maintenance needs one, "
        "so Launchplane cannot tell which to start. Retiring the lane and moving it to a build again brings it back on its "
        "data set. The web address still shows the back soon page."
    )


type Refusal = CheckRefused | StartRefused | BackgroundServiceRefused


def refusal_report(refused: Refusal, log: LogRead) -> str:
    if log.unreadable is not None:
        return f"{refused.report} Its log could not be read: {log.unreadable}"
    return failure_report(refused.report, log.tail)


# Everything stops before the log is read, so a log that cannot be read leaves nothing running.
def stop_again(lane: LaneReading, refused: Refusal, answer: MaintenanceResult, done: tuple[Event, ...]) -> Step[MaintenanceResult]:
    if (stop := next_to_stop(lane)) is not None:
        return stop
    logs = [event for event in done if isinstance(event, LogRead)]
    log = next((each for each in logs if each.container == refused.container), None)
    if log is None:
        return ReadLog(refused.container)
    of_the_web_part = any(instance.container == refused.container for instance in lane.instances)
    outcome = MaintenanceOutcome.WEB_PART_FAILED if of_the_web_part else MaintenanceOutcome.BACKGROUND_SERVICE_FAILED
    return Done(answer.model_copy(update={"outcome": outcome, "error": refusal_report(refused, log)}))


STILL_BACK_SOON = "The web address still shows the back soon page."


# Whether this call started a part that reads stopped again, the host cannot show, so it is asked of what the call has done.
def background_service_step(judged: Judgement, done: tuple[Event, ...]) -> Action | Stop | None:
    findings = judged.about_background_services()
    if not findings:
        return None
    first = findings[0]
    if first.kind not in STARTED_AGAIN:
        return Stop(
            f"The background service is not one ending maintenance can start: {first.sentence}. "
            f"{STILL_BACK_SOON} Retiring the lane and moving it to a build again brings it back."
        )
    start = StartStopped(first.subject)
    if first.kind == Kind.STOPPED and start not in done:
        return start
    return AwaitBackgroundService(first.subject)


# A login nobody holds goes, and what else is found is said, while the back soon page still shows, so the lane never comes
# back with it unsaid. Like a move, ending maintenance answers for the instance and its data set, not for another data set.
def rest_step(judged: Judgement, instance: RunningInstance, done: tuple[Event, ...]) -> Action | Stop | None:
    if (step := login_step(judged, done)) is not None:
        return step
    # Switching back is what takes visitors off a back soon page that is not running.
    left = [finding for finding in left_for_a_move(judged, instance) if finding.kind != Kind.BACK_SOON_STOPPED]
    if not left:
        return None
    return Stop(
        f"The lane is not as it must be to switch back, so the web address still shows the back soon page: "
        f"{sentences(left)}. Beginning maintenance again stops what was started."
    )


def bring_back(judged: Judgement, product: Product, answer: MaintenanceResult, done: tuple[Event, ...]) -> Step[MaintenanceResult]:
    lane = judged.lane
    refused = next((event for event in done if isinstance(event, CheckRefused | StartRefused | BackgroundServiceRefused)), None)
    if refused is not None:
        return stop_again(lane, refused, answer, done)
    instance = the_instance(lane)
    if isinstance(instance, Stop):
        return instance
    use = prepared(judged, instance.data_set, None, product)
    if not (use is None or isinstance(use, DataSetUse)):
        return use
    start = StartStopped(instance.container)
    if instance.state == InstanceState.STOPPED and start not in done:
        return start
    if not is_checked(instance, product, done):
        return Check(instance.container, product.health_path)
    problems = problems_switching_to(judged, instance)
    if problems:
        return Stop(
            f"The instance {instance.container} passed its check and the lane is not as it must be to switch back to it, so "
            f"the web address still shows the back soon page: {'; '.join(problems)}. "
            "Beginning maintenance again stops what was started."
        )
    step = background_service_step(judged, done) or rest_step(judged, instance, done)
    return step or EditRoute(answer.web_address, instance.container, lane.route.installed)


def decide_end(
    judged: Judgement, product: Product, done: tuple[Event, ...], cancel: Cancel | None = None
) -> Step[MaintenanceResult] | StepBack:
    lane = judged.lane
    step = unread_route(lane, "brought out of maintenance") or route_step(judged)
    if step is not None:
        return step
    if cancel is not None and is_turned_back(cancel):
        return end_step_back(judged)
    answer = answer_for(lane.product, lane.lane, lane.route, MaintenanceOutcome.ENDED)
    if lane.route.in_maintenance:
        return bring_back(judged, product, answer, done)
    return not_as_it_must_be(judged, done) or Done(answer)


# The first thing beginning maintenance changes is what visitors get, and bringing a lane back is ending maintenance.
def begin_step_back(judged: Judgement) -> StepBack:
    lane = judged.lane
    if not lane.route.in_maintenance:
        return put_back_or_what_is_left(judged, None)
    return TooLate(
        f"{lane.web_address} already shows the back soon page, so beginning maintenance could not be cancelled and is "
        f"finished as asked. POST /lanes/{lane.product}/{lane.lane}/maintenance/end brings the lane back."
    )


# Until the web address is switched back, what ending maintenance started is stopped again, as after a failed check.
# The lane is then judged as one in maintenance is, so a part that did not stop cleanly is found.
def end_step_back(judged: Judgement) -> StepBack:
    lane = judged.lane
    if lane.route.in_maintenance:
        # Every part of the lane is stopped again, so the cancel answers for each of them as the host shows it.
        parts = [*lane.instances, *lane.background_services, *lane.databases]
        return next_to_stop(lane) or put_back_or_what_is_left(judged, None, {part.container for part in parts})
    return TooLate(
        f"{lane.web_address} already sends its visitors to {lane.route.written}, so ending maintenance could not be cancelled "
        f"and is finished as asked. POST /lanes/{lane.product}/{lane.lane}/maintenance/begin puts the lane in maintenance again."
    )
