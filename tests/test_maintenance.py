from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from launchplane.instance import (
    BACK_SOON,
    DataSet,
    InstanceState,
    LaneRoute,
    LaneSeen,
    LoadedRoute,
    RouteStatus,
    RunningBackgroundService,
    RunningInstance,
)
from launchplane.judge import LaneVerdict, Standing, lane_verdict
from launchplane.maintenance import decide_begin, decide_end
from launchplane.plan import decide_move, decide_retire
from launchplane.steps import (
    AwaitBackgroundService,
    AwaitDatabase,
    AwaitRoute,
    BackgroundServiceRefused,
    Check,
    CheckRefused,
    Done,
    Event,
    FreshNames,
    LogRead,
    MaintenanceOutcome,
    MaintenanceResult,
    ReadLog,
    RemoveRoute,
    Retire,
    ShowBackSoon,
    StartDatabase,
    StartProxy,
    StartRefused,
    StartStopped,
    Stop,
    StopDatabase,
    StopPart,
    WriteRoute,
)
from tests.test_plan import QUIET, TARGET, WEB_ADDRESS, WITH_DATA, background_service_of, data_set, lane_instance, on_data

RUNNING, STOPPED = InstanceState.RUNNING, InstanceState.STOPPED
SINCE = datetime(2026, 11, 16, 14, 2, tzinfo=UTC)
WEB = "web"
BACKGROUND_SERVICE = "web-background-service"
DATABASE = "database-d1"
KILLED = 137


def web(state: InstanceState, container: str = WEB) -> RunningInstance:
    return on_data(lane_instance(container, "b", state), "d1")


def background_service(state: InstanceState, ran: float = 10.0) -> RunningBackgroundService:
    return background_service_of(web(RUNNING), running_for=ran if state == RUNNING else None).model_copy(update={"state": state})


def database(state: InstanceState, *, accepts: bool = True, exit_code: int = 0) -> DataSet:
    found = data_set("d1", state, accepts=accepts)
    stopped_with = exit_code if state == STOPPED else None
    return found.model_copy(update={"databases": (found.databases[0].model_copy(update={"exit_code": stopped_with}),)})


STOPPED_DATABASE = database(STOPPED)
STARTED_DATABASE = database(RUNNING)


@dataclass(frozen=True)
class Lane:
    route: tuple[str | None, str | None] | None
    web: InstanceState | None = STOPPED
    background_service: InstanceState | None = STOPPED
    database: DataSet | None = STOPPED_DATABASE
    background_service_ran: float = 10.0

    def seen(self) -> LaneSeen:
        if self.route is None:
            return LaneSeen((), LaneRoute(None, None, proxy_runs=False))
        loaded, written = self.route
        route = LaneRoute(
            None if loaded is None else LoadedRoute(loaded, RouteStatus.ENABLED),
            written,
            SINCE if written == BACK_SOON else None,
        )
        return LaneSeen(
            () if self.web is None else (web(self.web),),
            route,
            () if self.background_service is None else (background_service(self.background_service, self.background_service_ran),),
            () if self.database is None else (self.database,),
            QUIET,
        )


IN_MAINTENANCE = (BACK_SOON, BACK_SOON)
SERVING = (WEB, WEB)
UP = Lane(SERVING, RUNNING, RUNNING, database(RUNNING))


def answered(outcome: MaintenanceOutcome, *, in_maintenance: bool, error: str | None = None) -> Done[MaintenanceResult]:
    return Done(
        MaintenanceResult(
            product="site",
            lane="testing",
            web_address=WEB_ADDRESS,
            outcome=outcome,
            in_maintenance=in_maintenance,
            since=SINCE if in_maintenance else None,
            error=error,
        )
    )


KILLED_REPORT = f"The database {DATABASE} did not stop cleanly: it exited with code {KILLED}, so it was killed or it crashed"
BEGINS: dict[str, tuple[Lane, object]] = {
    "with no proxy running, it is started first": (Lane(None), StartProxy()),
    "the web address is switched to the back soon page before any part stops": (UP, ShowBackSoon(WEB_ADDRESS)),
    "killed after the route was written: no part stops until the proxy has loaded the back soon page": (
        Lane((WEB, BACK_SOON), RUNNING, RUNNING, database(RUNNING)),
        AwaitRoute(WEB_ADDRESS, BACK_SOON),
    ),
    "killed once the back soon page shows: the background service stops first": (
        Lane(IN_MAINTENANCE, RUNNING, RUNNING, database(RUNNING)),
        StopPart(BACKGROUND_SERVICE),
    ),
    "a background service that keeps restarting is stopped too": (
        Lane(IN_MAINTENANCE, RUNNING, InstanceState.RESTARTING, database(RUNNING)),
        StopPart(BACKGROUND_SERVICE),
    ),
    "killed after the background service stopped: the web part stops next": (
        Lane(IN_MAINTENANCE, RUNNING, STOPPED, database(RUNNING)),
        StopPart(WEB),
    ),
    "killed after the web part stopped: the database stops last, by its own deadline": (
        Lane(IN_MAINTENANCE, STOPPED, STOPPED, database(RUNNING)),
        StopDatabase(DATABASE),
    ),
    "a lane read with the back soon page loaded and every part stopped is in maintenance": (
        Lane(IN_MAINTENANCE),
        answered(MaintenanceOutcome.IN_MAINTENANCE, in_maintenance=True),
    ),
    "a database that had to be killed is answered as killed, not as a success": (
        Lane(IN_MAINTENANCE, database=database(STOPPED, exit_code=KILLED)),
        answered(MaintenanceOutcome.DATABASE_KILLED, in_maintenance=True, error=KILLED_REPORT),
    ),
    "a lane whose route goes to an instance that is gone still shows the back soon page": (
        Lane(("gone", "gone"), None, None, database(RUNNING)),
        ShowBackSoon(WEB_ADDRESS),
    ),
}


@pytest.mark.parametrize(("lane", "step"), BEGINS.values(), ids=BEGINS.keys())
def test_beginning_maintenance_takes_this_step_next_whatever_was_done_before(lane: Lane, step: object) -> None:
    assert decide_begin(lane.seen(), "site", "testing", WITH_DATA, ()) == step


def test_a_lane_with_nothing_on_the_host_is_not_given_a_back_soon_page() -> None:
    step = decide_begin(Lane((None, None), None, None, None).seen(), "site", "testing", WITH_DATA, ())
    assert isinstance(step, Stop)
    assert "nothing to put in maintenance" in step.reason


STARTED_WEB = StartStopped(WEB)
CHECKED = Check(WEB, "/health")
STARTED_BACKGROUND_SERVICE = StartStopped(BACKGROUND_SERVICE)
WATCHED = AwaitBackgroundService(BACKGROUND_SERVICE)
CHECK_REFUSED = CheckRefused(WEB, "Connection refused")
START_REFUSED = StartRefused(WEB, "Starting web failed: no such image")
WATCH_REFUSED = BackgroundServiceRefused(BACKGROUND_SERVICE, "It stopped.")
ALL_UP_BEHIND_BACK_SOON = Lane(IN_MAINTENANCE, RUNNING, RUNNING, STARTED_DATABASE)
ENDS: dict[str, tuple[Lane, tuple[Event, ...], object]] = {
    "with no proxy running, it is started first": (Lane(None), (), StartProxy()),
    "the database is started first": (Lane(IN_MAINTENANCE), (), StartDatabase(DATABASE)),
    "killed after the database started: it is waited on until it accepts connections": (
        Lane(IN_MAINTENANCE, database=database(RUNNING, accepts=False)),
        (),
        AwaitDatabase(DATABASE),
    ),
    "the web part is started once the database accepts connections": (
        Lane(IN_MAINTENANCE, database=STARTED_DATABASE),
        (),
        STARTED_WEB,
    ),
    "killed after the web part started: it is checked while the back soon page still shows": (
        Lane(IN_MAINTENANCE, RUNNING, STOPPED, STARTED_DATABASE),
        (),
        CHECKED,
    ),
    "a web part this call started that reads stopped again is checked, so its failure is answered, not started in a loop": (
        Lane(IN_MAINTENANCE, STOPPED, STOPPED, STARTED_DATABASE),
        (STARTED_WEB,),
        CHECKED,
    ),
    "the background service is started only once the web part passed its check in this call": (
        Lane(IN_MAINTENANCE, RUNNING, STOPPED, STARTED_DATABASE),
        (STARTED_WEB, CHECKED),
        STARTED_BACKGROUND_SERVICE,
    ),
    "killed after the background service started: it is watched as read, whoever started it, before the web address moves": (
        Lane(IN_MAINTENANCE, RUNNING, RUNNING, STARTED_DATABASE, background_service_ran=1.0),
        (CHECKED,),
        WATCHED,
    ),
    "a background service this call started that reads stopped again is watched, so its failure is answered": (
        Lane(IN_MAINTENANCE, RUNNING, STOPPED, STARTED_DATABASE),
        (CHECKED, STARTED_BACKGROUND_SERVICE),
        WATCHED,
    ),
    "the web address is switched back last, once the background service is read as having stayed up": (
        ALL_UP_BEHIND_BACK_SOON,
        (CHECKED,),
        WriteRoute(WEB_ADDRESS, WEB),
    ),
    "killed after the route was written back: the proxy is waited on": (
        Lane((BACK_SOON, WEB), RUNNING, RUNNING, STARTED_DATABASE),
        (),
        AwaitRoute(WEB_ADDRESS, WEB),
    ),
    "a call that switched the web address back answers ended, from the lane as read then": (
        UP,
        (CHECKED, WriteRoute(WEB_ADDRESS, WEB), AwaitRoute(WEB_ADDRESS, WEB)),
        answered(MaintenanceOutcome.ENDED, in_maintenance=False),
    ),
    "a web part that failed its check is stopped again": (
        Lane(IN_MAINTENANCE, RUNNING, STOPPED, STARTED_DATABASE),
        (STARTED_WEB, CHECK_REFUSED),
        StopPart(WEB),
    ),
    "after a failed check the database is stopped again too": (
        Lane(IN_MAINTENANCE, STOPPED, STOPPED, STARTED_DATABASE),
        (STARTED_WEB, CHECK_REFUSED, StopPart(WEB)),
        StopDatabase(DATABASE),
    ),
    "the log of a web part that failed its check is read only once everything has stopped": (
        Lane(IN_MAINTENANCE),
        (STARTED_WEB, CHECK_REFUSED, StopPart(WEB), StopDatabase(DATABASE)),
        ReadLog(WEB),
    ),
    "a failed check is answered with why and the log, and the lane is still in maintenance": (
        Lane(IN_MAINTENANCE),
        (CHECK_REFUSED, LogRead(WEB, "listening on :5678")),
        answered(
            MaintenanceOutcome.WEB_PART_FAILED,
            in_maintenance=True,
            error="Connection refused\nIts last log lines:\nlistening on :5678",
        ),
    ),
    "a failed check whose log could not be read is still answered, with everything stopped": (
        Lane(IN_MAINTENANCE),
        (CHECK_REFUSED, LogRead(WEB, "", unreadable="Docker refused")),
        answered(
            MaintenanceOutcome.WEB_PART_FAILED,
            in_maintenance=True,
            error="Connection refused Its log could not be read: Docker refused",
        ),
    ),
    "a web part that would not start has the database stopped again": (
        Lane(IN_MAINTENANCE, database=STARTED_DATABASE),
        (START_REFUSED,),
        StopDatabase(DATABASE),
    ),
    "a background service that did not stay up has itself stopped first, then the rest": (
        Lane(IN_MAINTENANCE, RUNNING, InstanceState.RESTARTING, STARTED_DATABASE),
        (CHECKED, STARTED_BACKGROUND_SERVICE, WATCH_REFUSED),
        StopPart(BACKGROUND_SERVICE),
    ),
    "a background service that did not stay up is answered as failed, and the lane is still in maintenance": (
        Lane(IN_MAINTENANCE),
        (WATCH_REFUSED, LogRead(BACKGROUND_SERVICE, "")),
        answered(MaintenanceOutcome.BACKGROUND_SERVICE_FAILED, in_maintenance=True, error="It stopped. It wrote no log."),
    ),
}


@pytest.mark.parametrize(("lane", "done", "step"), ENDS.values(), ids=ENDS.keys())
def test_ending_maintenance_takes_this_step_next(lane: Lane, done: tuple[Event, ...], step: object) -> None:
    assert decide_end(lane.seen(), "site", "testing", WITH_DATA, done) == step


END_STOPS = {
    "a lane with two instances, so which one received visitors cannot be told": (
        LaneSeen((web(STOPPED), web(STOPPED, "other")), Lane(IN_MAINTENANCE).seen().route, network_quiet=QUIET),
        (),
        "cannot tell which to start",
    ),
    "a lane with no instance to start": (Lane(IN_MAINTENANCE, None).seen(), (), "has 0 instances (none)"),
    "a web part that passed its check and can reach out": (
        LaneSeen(
            (on_data(lane_instance(WEB, "b"), "d1", "bridge"),), Lane(IN_MAINTENANCE).seen().route, (), (database(RUNNING),), QUIET
        ),
        (STARTED_WEB, CHECKED),
        "still shows the back soon page: The instance web is attached to bridge",
    ),
}


@pytest.mark.parametrize(("seen", "done", "reason"), END_STOPS.values(), ids=END_STOPS.keys())
def test_ending_maintenance_stops_with_why_and_leaves_the_back_soon_page_for(
    seen: LaneSeen, done: tuple[Event, ...], reason: str
) -> None:
    step = decide_end(seen, "site", "testing", WITH_DATA, done)
    assert isinstance(step, Stop)
    assert reason in step.reason


BACK_SOON_ROUTES = {
    "written and loaded": (BACK_SOON, BACK_SOON),
    "written and not yet loaded": (WEB, BACK_SOON),
}


@pytest.mark.parametrize("route", BACK_SOON_ROUTES.values(), ids=BACK_SOON_ROUTES.keys())
def test_a_move_refuses_a_lane_whose_route_points_at_the_back_soon_page_and_starts_nothing(route: tuple[str, str]) -> None:
    step = decide_move(Lane(route).seen(), TARGET, WITH_DATA, FreshNames("fresh", "fresh-data"), ())
    assert isinstance(step, Stop)
    assert "End maintenance first, with POST /lanes/site/testing/maintenance/end. Nothing was changed." in step.reason


def test_a_lane_in_maintenance_is_retired_as_any_other_route_first_then_its_stopped_parts() -> None:
    assert decide_retire(Lane(IN_MAINTENANCE).seen(), "site", "testing", None, ()) == RemoveRoute(WEB_ADDRESS, BACK_SOON)
    assert decide_retire(Lane((None, None)).seen(), "site", "testing", None, ()) == Retire(BACKGROUND_SERVICE)


def verdict_on(lane: Lane, *, at_work: bool = False) -> LaneVerdict:
    return lane_verdict(lane.seen(), WITH_DATA, "site", "testing", a_move_is_at_work=at_work)


def test_a_lane_in_maintenance_reads_as_in_maintenance_while_the_proxy_is_down_and_nothing_about_it_is_wrong() -> None:
    seen = Lane(IN_MAINTENANCE).seen()
    down = LaneSeen(
        seen.instances, LaneRoute(None, BACK_SOON, SINCE, proxy_runs=False), seen.background_services, seen.data_sets, QUIET
    )
    verdict = lane_verdict(down, WITH_DATA, "site", "testing", a_move_is_at_work=False)
    assert (verdict.in_maintenance, verdict.in_maintenance_since, verdict.problems) == (True, SINCE, [])


def test_a_back_soon_page_that_is_not_running_is_wrong_for_a_lane_in_maintenance_and_begin_does_not_answer_success() -> None:
    seen = Lane(IN_MAINTENANCE).seen()
    route = LaneRoute(seen.route.loaded, BACK_SOON, SINCE, back_soon_runs=False)
    without_page = LaneSeen(seen.instances, route, seen.background_services, seen.data_sets, QUIET)
    [problem] = lane_verdict(without_page, WITH_DATA, "site", "testing", a_move_is_at_work=True).problems
    step = decide_begin(without_page, "site", "testing", WITH_DATA, ())
    assert isinstance(step, Stop)
    assert "launchplane-back-soon is not running" in problem
    assert problem in step.reason


def test_a_lane_in_maintenance_with_every_part_stopped_is_as_intended_and_nothing_about_it_is_wrong() -> None:
    verdict = verdict_on(Lane(IN_MAINTENANCE))
    assert (verdict.in_maintenance, verdict.problems, verdict.release_under_way, verdict.left_unfinished) == (True, [], [], [])
    assert set(verdict.parts.values()) == {Standing.ON_PURPOSE}


def test_a_part_still_running_in_a_lane_in_maintenance_is_wrong() -> None:
    verdict = verdict_on(Lane(IN_MAINTENANCE, RUNNING, STOPPED, database(RUNNING)))
    assert verdict.problems == [
        "The instance web is running, in a lane in maintenance",
        f"The database {DATABASE} is running, in a lane in maintenance",
    ]
    assert verdict.parts == {WEB: Standing.WRONG, BACKGROUND_SERVICE: Standing.ON_PURPOSE, DATABASE: Standing.WRONG}


def test_while_a_call_is_at_work_a_part_still_running_in_a_lane_in_maintenance_is_under_way_not_wrong() -> None:
    verdict = verdict_on(Lane(IN_MAINTENANCE, RUNNING, STOPPED, database(RUNNING)), at_work=True)
    assert (verdict.problems, len(verdict.release_under_way)) == ([], 2)
    assert set(verdict.parts.values()) == {Standing.ON_PURPOSE}


def test_a_killed_database_in_a_lane_in_maintenance_is_wrong_even_while_a_call_is_at_work() -> None:
    verdict = verdict_on(Lane(IN_MAINTENANCE, database=database(STOPPED, exit_code=KILLED)), at_work=True)
    assert verdict.problems == [KILLED_REPORT]


def test_outside_maintenance_a_running_part_is_good_and_a_stopped_one_is_wrong() -> None:
    verdict = verdict_on(Lane(SERVING, RUNNING, STOPPED, database(RUNNING)))
    assert (verdict.in_maintenance, verdict.parts) == (
        False,
        {WEB: Standing.GOOD, BACKGROUND_SERVICE: Standing.WRONG, DATABASE: Standing.GOOD},
    )
