from dataclasses import dataclass
from typing import cast

import pytest

from launchplane.instance import (
    DataSet,
    DataSetUse,
    InstanceState,
    LaneBuild,
    LoadedRoute,
    Mount,
    RouteStatus,
    RunningBackgroundService,
    RunningInstance,
    Unread,
)
from launchplane.judge import Work
from launchplane.plan import decide_move, decide_retire
from launchplane.product import Product
from launchplane.steps import (
    Action,
    AwaitBackgroundService,
    AwaitDatabase,
    AwaitRoute,
    AwaitSwitch,
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
    RemoveLogins,
    RemoveRoute,
    Retire,
    RetireOutcome,
    RetireResult,
    StartBackgroundService,
    StartDatabase,
    StartInstance,
    StartProxy,
    Stop,
    Switch,
    Switched,
)
from tests.pretend_host import (
    D1,
    GENERATION,
    NEW,
    NEW_BACKGROUND_SERVICE,
    NEW_ON_D1,
    OLD,
    OLD_ON_D1,
    STRAY,
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

TARGET = LaneBuild(product="site", lane="testing", build="b")
FRESH = "site-testing-fresh"
FRESH_NAMES = FreshNames(FRESH, "fresh-data")

STOPPED_NEW = lane_instance("new", "b", InstanceState.STOPPED)
STARTED_NEW = StartInstance(TARGET, "new", None, GENERATION)
SWITCHED_TO_NEW = Switched("new", won=True)
REFUSED = CheckRefused("new", "Connection refused")


@dataclass(frozen=True)
class Case:
    instances: tuple[RunningInstance, ...]
    route: tuple[str | LoadedRoute | None, str | None]
    done: tuple[Event, ...]
    step: object
    # Whether the routes file says it was renamed for this release.
    switched: bool = False


def answer(outcome: Outcome, receiving: str | None, error: str | None = None, did: tuple[str, ...] = ()) -> Done[MoveResult]:
    return Done(
        MoveResult(
            product="site",
            lane="testing",
            build="b",
            web_address=WEB_ADDRESS,
            outcome=outcome,
            container_receiving_visitors=receiving,
            did=list(did),
            error=error,
        )
    )


MOVES = {
    "with no proxy running, it is started first": Case((), (None, None), (), StartProxy()),
    "a route written and not loaded is waited for before anything else": Case(
        (OLD, NEW), ("old", "new"), (), AwaitRoute(WEB_ADDRESS, "new")
    ),
    "a first move starts an instance": Case((), (None, None), (), StartInstance(TARGET, FRESH, None, GENERATION)),
    "an instance this call started is checked before any route names it": Case((NEW,), (None, None), (STARTED_NEW,), Check("new")),
    "an instance that passed its check is routed": Case(
        (OLD, NEW), ("old", "old"), (STARTED_NEW, Check("new")), Switch(WEB_ADDRESS, "new", GENERATION)
    ),
    "an instance that stopped after passing is checked again, not routed": Case(
        (OLD, STOPPED_NEW), ("old", "old"), (STARTED_NEW, Check("new")), Check("new")
    ),
    "the old instance retires only once the route to the new one has loaded": Case(
        (OLD, NEW), ("new", "new"), (STARTED_NEW, Check("new"), SWITCHED_TO_NEW), Retire("old"), switched=True
    ),
    "a finished switch answers switched": Case(
        (NEW,),
        ("new", "new"),
        (STARTED_NEW, Check("new"), SWITCHED_TO_NEW, Retire("old")),
        answer(Outcome.SWITCHED, "new", did=("started the instance new", "wrote the route to new", "retired old")),
        switched=True,
    ),
    "a switch found in the routes file answers switched, though this attempt did none of it": Case(
        (NEW,), ("new", "new"), (), answer(Outcome.SWITCHED, "new"), switched=True
    ),
    "a route renamed for this release and not loaded is waited for as long as it takes": Case(
        (OLD, NEW), ("old", "new"), (), AwaitSwitch(WEB_ADDRESS, "new"), switched=True
    ),
    "a switch whose candidate lost at the gate is made again by nobody: the candidate is still only checked": Case(
        (OLD, NEW), ("old", "old"), (STARTED_NEW, Check("new"), Switched("new", won=False)), Switch(WEB_ADDRESS, "new", GENERATION)
    ),
    "asking for the serving build again changes nothing": Case((NEW,), ("new", "new"), (), answer(Outcome.UNCHANGED, "new")),
    "an instance left beside the serving build is retired": Case((NEW, STRAY), ("new", "new"), (), Retire("stray")),
    "a finished cleanup answers repaired, with what it did": Case(
        (NEW,), ("new", "new"), (Retire("stray"),), answer(Outcome.REPAIRED, "new", did=("retired stray",))
    ),
    "a running instance of the build that anything earlier left is retired, never taken for the candidate": Case(
        (OLD, NEW), ("old", "old"), (), Retire("new")
    ),
    "what anything earlier left is retired before a candidate is made": Case((OLD, STRAY), ("old", "old"), (), Retire("stray")),
    "a stopped instance left before the switch is retired, then replaced": Case(
        (OLD, STOPPED_NEW), ("old", "old"), (), Retire("new")
    ),
    "a stopped serving instance is replaced, never checked": Case(
        (STOPPED_NEW,), ("new", "new"), (), StartInstance(TARGET, FRESH, None, GENERATION)
    ),
    "a serving instance that stopped while its route was awaited is replaced, not kept": Case(
        (OLD, STOPPED_NEW), ("new", "new"), (AwaitRoute(WEB_ADDRESS, "new"),), Retire("old")
    ),
    "a route to an instance that is gone is not taken as serving": Case(
        (), ("gone", "gone"), (), StartInstance(TARGET, FRESH, None, GENERATION)
    ),
    "an instance that failed its check is removed": Case((OLD, NEW), ("old", "old"), (STARTED_NEW, REFUSED), RemoveInstance("new")),
    "a failed check answers check failed, never switched": Case(
        (OLD,),
        ("old", "old"),
        (STARTED_NEW, REFUSED, RemoveInstance("new")),
        answer(Outcome.CHECK_FAILED, "old", "Connection refused", did=("started the instance new", "removed new")),
    ),
}


def given(case: Case) -> Given:
    return Given(case.instances, case.route, proxy_runs=case.step != StartProxy(), written_by=GENERATION if case.switched else None)


def next_move_step(case: Case) -> object:
    return decide_move(given(case).judged(Work.MOVE, Product()), TARGET, Product(), FRESH_NAMES, case.done, GENERATION)


def next_retire_step(case: Case) -> object:
    return decide_retire(given(case).judged(Work.RETIRE, Product()), None, case.done)


@pytest.mark.parametrize("case", MOVES.values(), ids=MOVES.keys())
def test_a_move_takes_this_step_next(case: Case) -> None:
    assert next_move_step(case) == case.step


@pytest.mark.parametrize("status", [RouteStatus.DISABLED, RouteStatus.WARNING])
def test_a_move_stops_and_says_so_when_the_loaded_route_is_not_enabled(status: RouteStatus) -> None:
    step = next_move_step(Case((NEW,), (LoadedRoute("new", status), "new"), (), None))
    assert isinstance(step, Stop)
    assert f"reports it {status}" in step.reason
    assert "Nothing was changed." in step.reason


def test_a_move_stopped_by_a_disabled_route_it_wrote_says_what_it_changed() -> None:
    done = (STARTED_NEW, Check("new"), SWITCHED_TO_NEW, AwaitSwitch(WEB_ADDRESS, "new"))
    step = next_move_step(Case((OLD, NEW), (LoadedRoute("new", RouteStatus.DISABLED), "new"), done, None, switched=True))
    assert isinstance(step, Stop)
    assert "started the instance new, wrote the route to new" in step.reason
    assert "Nothing was changed" not in step.reason


def retired(route_removed_to: str | None, containers: list[str]) -> Done[RetireResult]:
    outcome = RetireOutcome.RETIRED if route_removed_to or containers else RetireOutcome.UNCHANGED
    return Done(
        RetireResult(
            product="site",
            lane="testing",
            web_address=WEB_ADDRESS,
            outcome=outcome,
            route_removed_to_container=route_removed_to,
            retired_containers=containers,
        )
    )


RETIRES = {
    "with no proxy running, it is started first": Case((OLD,), (None, None), (), StartProxy()),
    "a route written and not loaded is waited for first": Case((OLD,), ("old", "new"), (), AwaitRoute(WEB_ADDRESS, "new")),
    "the route goes first": Case((OLD,), ("old", "old"), (), RemoveRoute(WEB_ADDRESS, "old")),
    "no instance stops until the proxy has loaded the route gone": Case(
        (OLD,), ("old", None), (RemoveRoute(WEB_ADDRESS, "old"),), AwaitRoute(WEB_ADDRESS, None)
    ),
    "instances retire once the route is gone": Case((OLD, STRAY), (None, None), (RemoveRoute(WEB_ADDRESS, "old"),), Retire("old")),
    "a finished retirement answers what it removed": Case(
        (), (None, None), (RemoveRoute(WEB_ADDRESS, "old"), Retire("old")), retired("old", ["old"])
    ),
    "a lane whose route was already gone answers no route removed": Case((), (None, None), (Retire("old"),), retired(None, ["old"])),
    "a retired lane is unchanged": Case((), (None, None), (), retired(None, [])),
    "a lane whose loaded route is disabled has it removed": Case(
        (OLD,), (LoadedRoute("old", RouteStatus.DISABLED), "old"), (), RemoveRoute(WEB_ADDRESS, "old")
    ),
    "a disabled route still loaded is waited for gone before any instance stops": Case(
        (OLD,), (LoadedRoute("old", RouteStatus.DISABLED), None), (RemoveRoute(WEB_ADDRESS, "old"),), AwaitRoute(WEB_ADDRESS, None)
    ),
}


@pytest.mark.parametrize("case", RETIRES.values(), ids=RETIRES.keys())
def test_a_retire_takes_this_step_next(case: Case) -> None:
    assert next_retire_step(case) == case.step


IMAGE = "postgres:18"


@dataclass(frozen=True)
class DataCase:
    instances: tuple[RunningInstance, ...]
    receiving: str | None
    data_sets: tuple[DataSet, ...]
    done: tuple[Event, ...]
    step: object
    background_services: tuple[RunningBackgroundService, ...] = ()


NEW_ON_D2 = on_data(NEW, "d2")
OUTSIDE_AND_STOPPED = with_database(data_set_named("d1", InstanceState.STOPPED), networks=("network-d1", "bridge"))
RENAMED_STOPPED = data_set_named("d1", databases=("renamed-database",), state=InstanceState.STOPPED)
STOPPED_NEW_BACKGROUND_SERVICE = background_service_of(NEW_ON_D1, running_for=None)
USE_D1 = DataSetUse("d1", "network-d1", "database-d1")
STARTED_NEW_BACKGROUND_SERVICE = StartBackgroundService(TARGET, "new", USE_D1, ("work",), GENERATION)
STARTED_NEW_ON_D1 = StartInstance(TARGET, "new", USE_D1, GENERATION)

DATA_MOVES = {
    "a lane with no data set gets a fresh network before anything else": DataCase(
        (), None, (), (), CreateDataSetNetwork("site", "testing", "fresh-data", GENERATION)
    ),
    "a data set with a network and no volume gets its volume": DataCase(
        (), None, (data_set_named("d1", None, volume=False),), (), CreateDataSetVolume("site", "testing", "d1")
    ),
    "a data set with no database gets one on the network and volume as read": DataCase(
        (), None, (data_set_named("d1", None),), (), CreateDatabase("site", "testing", "d1", "network-d1", "volume-d1", IMAGE)
    ),
    "a stopped database is started by the name it was read under, not made again": DataCase(
        (),
        None,
        (RENAMED_STOPPED,),
        (),
        StartDatabase("renamed-database"),
    ),
    "a database read as not accepting connections is waited for before an instance starts on it": DataCase(
        (), None, (data_set_named("d1", accepts=False),), (), AwaitDatabase("database-d1")
    ),
    "an instance starts on the data set once its database is read accepting connections": DataCase(
        (), None, (D1,), (), StartInstance(TARGET, FRESH, USE_D1, GENERATION)
    ),
    "a release uses the data set of the instance receiving visitors, even beside another": DataCase(
        (OLD_ON_D1,), "old", (D1, data_set_named("d2")), (), StartInstance(TARGET, FRESH, USE_D1, GENERATION)
    ),
    "the new instance is checked at the product's health path": DataCase(
        (OLD_ON_D1, NEW_ON_D1), "old", (D1,), (STARTED_NEW_ON_D1,), Check("new", "/health")
    ),
    "an instance left by an interrupted attempt is retired before its data set is waited on": DataCase(
        (OLD_ON_D1, NEW_ON_D1), "old", (data_set_named("d1", accepts=False),), (), Retire("new")
    ),
    "a checked instance attached only to its own quiet networks is switched to": DataCase(
        (OLD_ON_D1, NEW_ON_D1), "old", (D1,), (STARTED_NEW_ON_D1, Check("new", "/health")), Switch(WEB_ADDRESS, "new", GENERATION)
    ),
    "a running instance of the build on another data set is retired, not switched to": DataCase(
        (OLD_ON_D1, NEW_ON_D2), "old", (D1, data_set_named("d2")), (), Retire("new")
    ),
    "a left-over instance that can reach out is retired, not switched to": DataCase(
        (OLD_ON_D1, on_data(NEW, "d1", "bridge")), "old", (D1,), (), Retire("new")
    ),
    "a serving instance attached to another data set's quiet network is replaced, not kept": DataCase(
        (on_data(NEW, "d1", "network-d2"),),
        "new",
        (D1, data_set_named("d2")),
        (),
        StartInstance(TARGET, FRESH, USE_D1, GENERATION),
        (NEW_BACKGROUND_SERVICE,),
    ),
    "the old background service stops before the new one starts": DataCase(
        (OLD_ON_D1, NEW_ON_D1), "new", (D1,), (), Retire("old-background-service"), (background_service_of(OLD_ON_D1),)
    ),
    "the old instance retires before the new background service starts": DataCase(
        (OLD_ON_D1, NEW_ON_D1), "new", (D1,), (), Retire("old")
    ),
    "the new background service starts once nothing of the old instance is left": DataCase(
        (NEW_ON_D1,), "new", (D1,), (), STARTED_NEW_BACKGROUND_SERVICE
    ),
    "a background service read as running less than its watch is waited on, whoever started it": DataCase(
        (NEW_ON_D1,),
        "new",
        (D1,),
        (),
        AwaitBackgroundService("new-background-service"),
        (background_service_of(NEW_ON_D1, running_for=1.0),),
    ),
    "a background service that stopped is replaced": DataCase(
        (NEW_ON_D1,), "new", (D1,), (), Retire("new-background-service"), (STOPPED_NEW_BACKGROUND_SERVICE,)
    ),
    "a background service this call started and reads as restarting is awaited, so its failure is answered, not retried": DataCase(
        (NEW_ON_D1,),
        "new",
        (D1,),
        (STARTED_NEW_BACKGROUND_SERVICE,),
        AwaitBackgroundService("new-background-service"),
        (changed(NEW_BACKGROUND_SERVICE, state=InstanceState.RESTARTING, running_for_seconds=None),),
    ),
    "a background service read as restarting is replaced, however long Docker says it ran": DataCase(
        (NEW_ON_D1,),
        "new",
        (D1,),
        (),
        Retire("new-background-service"),
        (changed(NEW_BACKGROUND_SERVICE, state=InstanceState.RESTARTING),),
    ),
    "a background service of another build than the instance it belongs to is replaced": DataCase(
        (NEW_ON_D1,), "new", (D1,), (), Retire("new-background-service"), (changed(NEW_BACKGROUND_SERVICE, build="a"),)
    ),
    "two background services of the serving instance are not answered unchanged: one is retired": DataCase(
        (NEW_ON_D1,),
        "new",
        (D1,),
        (),
        Retire("new-background-service"),
        (NEW_BACKGROUND_SERVICE, background_service_of(NEW_ON_D1, container="new-background-service-two")),
    ),
    "a background service left without its instance is stopped first": DataCase(
        (OLD_ON_D1,),
        "old",
        (D1,),
        (),
        Retire("gone-background-service"),
        (background_service_of(on_data(lane_instance("gone", "a"), "d1")),),
    ),
    "a stopped database under a serving instance is started, not answered unchanged": DataCase(
        (NEW_ON_D1,),
        "new",
        (data_set_named("d1", InstanceState.STOPPED),),
        (),
        StartDatabase("database-d1"),
        (NEW_BACKGROUND_SERVICE,),
    ),
    "a serving lane whose database does not accept connections is waited on, not answered": DataCase(
        (NEW_ON_D1,), "new", (data_set_named("d1", accepts=False),), (), AwaitDatabase("database-d1"), (NEW_BACKGROUND_SERVICE,)
    ),
}


def given_data(case: DataCase) -> Given:
    return Given(case.instances, (case.receiving, case.receiving), case.background_services, case.data_sets)


def next_data_move_step(case: DataCase) -> Action | Done[MoveResult] | Stop:
    step = decide_move(given_data(case).judged(Work.MOVE), TARGET, WITH_DATA, FRESH_NAMES, case.done, GENERATION)
    return cast("Action | Done[MoveResult] | Stop", step)


@pytest.mark.parametrize("case", DATA_MOVES.values(), ids=DATA_MOVES.keys())
def test_a_move_with_data_takes_this_step_next(case: DataCase) -> None:
    assert next_data_move_step(case) == case.step


LEFT_ON_D1 = data_set_named("d1", unheld=("i_left",))
AT_REST_WITH_A_LOGIN_LEFT = DataCase((NEW_ON_D1,), "new", (LEFT_ON_D1,), (), None, (NEW_BACKGROUND_SERVICE,))


def test_a_move_does_not_answer_while_a_running_server_has_a_login_no_container_holds() -> None:
    assert next_data_move_step(AT_REST_WITH_A_LOGIN_LEFT) == RemoveLogins("database-d1")


def test_a_failed_check_is_not_answered_until_the_login_of_the_instance_it_removed_is_gone() -> None:
    refused = (StartInstance(TARGET, "new", USE_D1), CheckRefused("new", "Connection refused"), RemoveInstance("new"))
    case = DataCase((on_data(OLD, "d1"),), "old", (LEFT_ON_D1,), refused, None, (background_service_of(on_data(OLD, "d1")),))
    assert next_data_move_step(case) == RemoveLogins("database-d1")


def retire_step_with(found: DataSet, delete: str | None = None) -> object:
    return decide_retire(Given(data_sets=(found,)).judged(Work.RETIRE), delete, ())


def test_a_retire_does_not_answer_while_a_running_server_has_a_login_no_container_holds() -> None:
    assert retire_step_with(LEFT_ON_D1) == RemoveLogins("database-d1")


def test_a_retire_answers_with_a_stopped_server_whose_logins_wait_for_its_next_start() -> None:
    assert isinstance(retire_step_with(data_set_named("d1", InstanceState.STOPPED)), Done)


UNKNOWN_LOGINS = {
    "does not accept connections": data_set_named("d1", accepts=False),
    "was not asked whether it accepts connections": with_database(D1, accepts_connections=None, unheld_logins=None),
    "could not tell its logins": with_database(D1, unheld_logins=Unread(why="psql: no such database")),
}


@pytest.mark.parametrize("found", UNKNOWN_LOGINS.values(), ids=UNKNOWN_LOGINS.keys())
def test_a_retire_does_not_answer_success_over_a_running_server_that(found: DataSet) -> None:
    step = retire_step_with(found)
    assert isinstance(step, Stop)
    assert "could not be read" in step.reason


def test_a_move_stops_over_logins_that_could_not_be_read_in_the_words_the_status_call_has_for_them() -> None:
    case = DataCase((NEW_ON_D1,), "new", (UNKNOWN_LOGINS["could not tell its logins"],), (), None, (NEW_BACKGROUND_SERVICE,))
    step = next_data_move_step(case)
    [unread] = given_data(case).judged(None).findings
    assert isinstance(step, Stop)
    assert "unknown: psql: no such database" in unread.sentence
    assert unread.sentence in step.reason


def test_a_data_set_named_for_deleting_is_deleted_without_its_logins_being_removed_first() -> None:
    assert retire_step_with(LEFT_ON_D1, delete="d1") == DeleteDataSet(LEFT_ON_D1)


def test_a_lane_that_is_as_it_must_be_answers_unchanged() -> None:
    step = next_data_move_step(DataCase((NEW_ON_D1,), "new", (D1,), (), None, (NEW_BACKGROUND_SERVICE,)))
    assert isinstance(step, Done)
    assert step.answer.outcome == Outcome.UNCHANGED


def test_a_background_service_that_stops_during_its_watch_answers_background_service_failed_with_why_not_switched() -> None:
    refused = BackgroundServiceRefused("new-background-service", "The background service new-background-service is not running.")
    done = (STARTED_NEW_BACKGROUND_SERVICE, refused, SWITCHED_TO_NEW)
    step = next_data_move_step(DataCase((NEW_ON_D1,), "new", (D1,), done, None, (STOPPED_NEW_BACKGROUND_SERVICE,)))
    assert isinstance(step, Done)
    assert (step.answer.outcome, step.answer.error) == (Outcome.BACKGROUND_SERVICE_FAILED, refused.report)


DATA_STOPS = {
    "two data sets and no instance receiving visitors": (
        DataCase((), None, (D1, data_set_named("d2")), (), None),
        "cannot tell which",
    ),
    "a data set whose network is not quiet": (DataCase((), None, (data_set_named("d1", quiet=False),), (), None), "not quiet"),
    "a release on a data set whose network is not quiet": (
        DataCase((OLD_ON_D1,), "old", (data_set_named("d1", quiet=False),), (), None),
        "not quiet",
    ),
    "a database that keeps restarting": (
        DataCase((), None, (data_set_named("d1", InstanceState.RESTARTING),), (), None),
        "is restarting",
    ),
    "a receiving instance whose data set is gone": (DataCase((OLD_ON_D1,), "old", (), (), None), "not on the host"),
    "two database servers carrying one data set's labels": (
        DataCase((), None, (data_set_named("d1", databases=("database-a", "database-b")),), (), None),
        "2 database servers (database-a, database-b)",
    ),
    "a new instance given an outside network during its check": (
        DataCase((OLD_ON_D1, on_data(NEW, "d1", "bridge")), "old", (D1,), (STARTED_NEW, Check("new", "/health")), None),
        "can reach out through bridge",
    ),
    "a stopped database attached to an outside network, which is not started first": (
        DataCase((), None, (OUTSIDE_AND_STOPPED,), (), None),
        "can reach out through bridge",
    ),
    "a stopped database under a serving instance, attached to an outside network": (
        DataCase(
            (NEW_ON_D1,),
            "new",
            (OUTSIDE_AND_STOPPED,),
            (),
            None,
            (NEW_BACKGROUND_SERVICE,),
        ),
        "can reach out through bridge",
    ),
    "a running database with the right labels and network and another volume mounted": (
        DataCase(
            (NEW_ON_D1,),
            "new",
            (with_database(D1, mounts=(Mount(source="another-volume", path="/var/lib/postgresql"),)),),
            (),
            None,
            (NEW_BACKGROUND_SERVICE,),
        ),
        "has another-volume mounted at /var/lib/postgresql, which it must not have; "
        "The database database-d1 does not have volume-d1 mounted at /var/lib/postgresql",
    ),
    "a new instance that passed its check with a volume mounted": (
        DataCase(
            (OLD_ON_D1, changed(NEW_ON_D1, mounts=(Mount(source="volume-d1", path="/data"),))),
            "old",
            (D1,),
            (STARTED_NEW, Check("new", "/health")),
            None,
        ),
        "has volume-d1 mounted",
    ),
    "an instance this call started and checked, on another data set than the one receiving visitors runs against": (
        DataCase(
            (OLD_ON_D1, NEW_ON_D2),
            "old",
            (D1, data_set_named("d2")),
            (StartInstance(TARGET, "new", DataSetUse("d2", "network-d2", "database-d2")), Check("new", "/health")),
            None,
        ),
        "runs against the data set d2, not d1",
    ),
}


@pytest.mark.parametrize(("case", "reason"), DATA_STOPS.values(), ids=DATA_STOPS.keys())
def test_a_move_with_data_stops_with_why_instead_of_switching_or_answering_success(case: DataCase, reason: str) -> None:
    step = next_data_move_step(case)
    assert isinstance(step, Stop)
    assert reason in step.reason


def test_a_retire_stops_background_services_first_and_deletes_every_part_of_the_named_data_set() -> None:
    doubled = data_set_named("d2", databases=("database-a", "database-b"))

    def step(
        instances: tuple[RunningInstance, ...], background_services: tuple[RunningBackgroundService, ...], delete: str | None
    ) -> Action | Done[RetireResult] | Stop:
        judged = Given(instances, background_services=background_services, data_sets=(D1, doubled)).judged(Work.RETIRE)
        return decide_retire(judged, delete, ())

    assert step((OLD_ON_D1,), (background_service_of(OLD_ON_D1),), "d1") == Retire("old-background-service")
    assert step((), (), "d2") == DeleteDataSet(doubled)
    kept = step((), (), None)
    assert isinstance(kept, Done)
    assert kept.answer.kept_data_sets == ["d1", "d2"]
