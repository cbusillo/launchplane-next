from dataclasses import dataclass, replace

import pytest

from launchplane.instance import BACK_SOON, DataSet, InstanceState, RunningBackgroundService, RunningInstance
from launchplane.judge import Work
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
    RemoveLogins,
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
from tests.pretend_host import (
    SINCE,
    WEB_ADDRESS,
    WITH_DATA,
    Given,
    background_service_of,
    changed,
    data_set_named,
    lane_instance,
    on_data,
    with_database,
)
from tests.test_plan import TARGET

RUNNING, STOPPED = InstanceState.RUNNING, InstanceState.STOPPED
WEB = "web"
BACKGROUND_SERVICE = "web-background-service"
DATABASE = "database-d1"
KILLED = 137


def web(state: InstanceState, container: str = WEB) -> RunningInstance:
    return on_data(lane_instance(container, "b", state), "d1")


def background_service(state: InstanceState, ran: float = 10.0) -> RunningBackgroundService:
    return changed(background_service_of(web(RUNNING), running_for=ran if state == RUNNING else None), state=state)


def database(state: InstanceState, *, accepts: bool = True, exit_code: int = 0) -> DataSet:
    return data_set_named("d1", state, accepts=accepts, exit_code=exit_code)


STOPPED_DATABASE = database(STOPPED)
STARTED_DATABASE = database(RUNNING)


@dataclass(frozen=True)
class Lane:
    route: tuple[str | None, str | None] | None
    web: InstanceState | None = STOPPED
    background_service: InstanceState | None = STOPPED
    database: DataSet | None = STOPPED_DATABASE
    background_service_ran: float = 10.0

    def given(self) -> Given:
        return Given(
            () if self.web is None else (web(self.web),),
            self.route or (None, None),
            () if self.background_service is None else (background_service(self.background_service, self.background_service_ran),),
            () if self.database is None else (self.database,),
            proxy_runs=self.route is not None,
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
    assert decide_begin(lane.given().judged(Work.BEGIN_MAINTENANCE), ()) == step


def test_a_lane_with_nothing_on_the_host_is_not_given_a_back_soon_page() -> None:
    step = decide_begin(Lane((None, None), None, None, None).given().judged(Work.BEGIN_MAINTENANCE), ())
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
    "a login no container holds is removed while the back soon page still shows, before the web address is switched back": (
        Lane(IN_MAINTENANCE, RUNNING, RUNNING, data_set_named("d1", unheld=("i_left",))),
        (CHECKED,),
        RemoveLogins(DATABASE),
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
    assert decide_end(lane.given().judged(Work.END_MAINTENANCE), WITH_DATA, done) == step


END_STOPS = {
    "a lane with two instances, so which one received visitors cannot be told": (
        Given((web(STOPPED), web(STOPPED, "other")), IN_MAINTENANCE),
        (),
        "cannot tell which to start",
    ),
    "a lane with no instance to start": (Lane(IN_MAINTENANCE, None).given(), (), "has 0 instances (none)"),
    "a web part that passed its check and can reach out": (
        Given((on_data(lane_instance(WEB, "b"), "d1", "bridge"),), IN_MAINTENANCE, data_sets=(STARTED_DATABASE,)),
        (STARTED_WEB, CHECKED),
        "still shows the back soon page: The instance web is attached to bridge",
    ),
    "a lane with no background service where the product has one": (
        Lane(IN_MAINTENANCE, RUNNING, None, STARTED_DATABASE).given(),
        (CHECKED,),
        "not one ending maintenance can start: The lane has 0 background services",
    ),
    "logins that could not be read, said while the back soon page still shows and not after the lane is back": (
        Given(
            (web(RUNNING),), IN_MAINTENANCE, (background_service(RUNNING),), (with_database(STARTED_DATABASE, unheld_logins=None),)
        ),
        (CHECKED,),
        "so the web address still shows the back soon page: The logins of the database database-d1 could not be read",
    ),
}


@pytest.mark.parametrize(("given", "done", "reason"), END_STOPS.values(), ids=END_STOPS.keys())
def test_ending_maintenance_stops_with_why_and_leaves_the_back_soon_page_for(
    given: Given, done: tuple[Event, ...], reason: str
) -> None:
    step = decide_end(given.judged(Work.END_MAINTENANCE), WITH_DATA, done)
    assert isinstance(step, Stop)
    assert reason in step.reason


BACK_SOON_ROUTES = {
    "written and loaded": (BACK_SOON, BACK_SOON),
    "written and not yet loaded": (WEB, BACK_SOON),
}


@pytest.mark.parametrize("route", BACK_SOON_ROUTES.values(), ids=BACK_SOON_ROUTES.keys())
def test_a_move_refuses_a_lane_whose_route_points_at_the_back_soon_page_and_starts_nothing(route: tuple[str, str]) -> None:
    step = decide_move(Lane(route).given().judged(Work.MOVE), TARGET, WITH_DATA, FreshNames("fresh", "fresh-data"), ())
    assert isinstance(step, Stop)
    assert "End maintenance first, with POST /lanes/site/testing/maintenance/end. Nothing was changed." in step.reason


def test_a_move_refuses_a_lane_in_maintenance_before_it_starts_the_proxy() -> None:
    down = replace(Lane(IN_MAINTENANCE).given(), route=(None, BACK_SOON), proxy_runs=False)
    step = decide_move(down.judged(Work.MOVE), TARGET, WITH_DATA, FreshNames("fresh", "fresh-data"), ())
    assert isinstance(step, Stop)
    assert "Nothing was changed." in step.reason


def test_a_lane_in_maintenance_is_retired_as_any_other_route_first_then_its_stopped_parts() -> None:
    assert decide_retire(Lane(IN_MAINTENANCE).given().judged(Work.RETIRE), None, ()) == RemoveRoute(WEB_ADDRESS, BACK_SOON)
    assert decide_retire(Lane((None, None)).given().judged(Work.RETIRE), None, ()) == Retire(BACKGROUND_SERVICE)


def test_beginning_maintenance_does_not_answer_success_with_the_back_soon_page_not_running_and_says_so_as_the_status_call_does() -> (
    None
):
    without_page = replace(Lane(IN_MAINTENANCE).given(), back_soon_runs=False)
    [problem] = without_page.judged(None).findings
    step = decide_begin(without_page.judged(Work.BEGIN_MAINTENANCE), ())
    assert isinstance(step, Stop)
    assert "launchplane-back-soon is not running" in problem.sentence
    assert problem.sentence in step.reason


def test_ending_maintenance_switches_away_from_a_back_soon_page_that_is_not_running() -> None:
    without_page = replace(ALL_UP_BEHIND_BACK_SOON.given(), back_soon_runs=False)
    assert decide_end(without_page.judged(Work.END_MAINTENANCE), WITH_DATA, (CHECKED,)) == WriteRoute(WEB_ADDRESS, WEB)


def test_ending_maintenance_starts_the_stopped_database_of_another_data_set_of_the_lane_before_switching_back() -> None:
    spare = replace(ALL_UP_BEHIND_BACK_SOON.given(), data_sets=(STARTED_DATABASE, data_set_named("d2", STOPPED)))
    assert decide_end(spare.judged(Work.END_MAINTENANCE), WITH_DATA, (CHECKED,)) == StartDatabase("database-d2")
