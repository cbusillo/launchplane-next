from dataclasses import dataclass

import pytest

from launchplane.decide import (
    Action,
    AwaitDatabase,
    AwaitRoute,
    Check,
    CheckJob,
    CheckRefused,
    CreateDatabase,
    CreateDataSetNetwork,
    CreateDataSetVolume,
    DataSetUse,
    DeleteDataSet,
    Done,
    Event,
    FreshNames,
    JobRefused,
    LaneRoute,
    LaneSeen,
    LoadedRoute,
    MoveResult,
    Outcome,
    ProxyNotRunning,
    RemoveInstance,
    RemoveRoute,
    Retire,
    RetireOutcome,
    RetireResult,
    RouteStatus,
    StartDatabase,
    StartInstance,
    StartJob,
    StartProxy,
    Stop,
    WriteRoute,
    decide_move,
    decide_retire,
)
from launchplane.instance import DataSet, DataSetDatabase, DataSetNetwork, InstanceState, LaneBuild, RunningInstance, RunningJob
from launchplane.product import Database, Job, Product

WEB_ADDRESS = "testing.site.test"
TARGET = LaneBuild(product="site", lane="testing", build="b")
FRESH = "site-testing-fresh"


def lane_instance(container: str, build: str, state: InstanceState = InstanceState.RUNNING) -> RunningInstance:
    return RunningInstance(product="site", lane="testing", build=build, container=container, state=state, is_quiet=True)


OLD = lane_instance("old", "a")
NEW = lane_instance("new", "b")
STOPPED_NEW = lane_instance("new", "b", InstanceState.STOPPED)
STRAY = lane_instance("stray", "c")
STARTED_NEW = StartInstance(TARGET, "new")
REFUSED = CheckRefused("new", "Connection refused")


@dataclass(frozen=True)
class Case:
    instances: tuple[RunningInstance, ...]
    route: tuple[str | LoadedRoute | None, str | None]
    done: tuple[Event, ...]
    step: object


def answer(outcome: Outcome, receiving: str | None, error: str | None = None) -> Done[MoveResult]:
    return Done(
        MoveResult(
            product="site",
            lane="testing",
            build="b",
            web_address=WEB_ADDRESS,
            outcome=outcome,
            container_receiving_visitors=receiving,
            error=error,
        )
    )


MOVES = {
    "with no proxy running, it is started first": Case((), (None, None), (), StartProxy()),
    "a route written and not loaded is waited for before anything else": Case(
        (OLD, NEW), ("old", "new"), (), AwaitRoute(WEB_ADDRESS, "new")
    ),
    "a first move starts an instance": Case((), (None, None), (), StartInstance(TARGET, FRESH)),
    "an instance this call started is checked before any route names it": Case((NEW,), (None, None), (STARTED_NEW,), Check("new")),
    "an instance that passed its check is routed": Case(
        (OLD, NEW), ("old", "old"), (STARTED_NEW, Check("new")), WriteRoute(WEB_ADDRESS, "new")
    ),
    "an instance that stopped after passing is checked again, not routed": Case(
        (OLD, STOPPED_NEW), ("old", "old"), (STARTED_NEW, Check("new")), Check("new")
    ),
    "the old instance retires only once the route to the new one has loaded": Case(
        (OLD, NEW), ("new", "new"), (STARTED_NEW, Check("new"), WriteRoute(WEB_ADDRESS, "new")), Retire("old")
    ),
    "a finished switch answers switched": Case(
        (NEW,),
        ("new", "new"),
        (STARTED_NEW, Check("new"), WriteRoute(WEB_ADDRESS, "new"), Retire("old")),
        answer(Outcome.SWITCHED, "new"),
    ),
    "asking for the serving build again changes nothing": Case((NEW,), ("new", "new"), (), answer(Outcome.UNCHANGED, "new")),
    "an instance left beside the serving build is retired": Case((NEW, STRAY), ("new", "new"), (), Retire("stray")),
    "a finished cleanup answers cleaned up": Case((NEW,), ("new", "new"), (Retire("stray"),), answer(Outcome.CLEANED_UP, "new")),
    "a running instance left before the switch is checked, not started again": Case((OLD, NEW), ("old", "old"), (), Check("new")),
    "an instance neither serving nor about to be checked is retired first": Case(
        (OLD, NEW, STRAY), ("old", "old"), (), Retire("stray")
    ),
    "a stopped instance left before the switch is retired, then replaced": Case(
        (OLD, STOPPED_NEW), ("old", "old"), (), Retire("new")
    ),
    "a stopped serving instance is replaced, never checked": Case((STOPPED_NEW,), ("new", "new"), (), StartInstance(TARGET, FRESH)),
    "a serving instance that stopped while its route was awaited is replaced, not kept": Case(
        (OLD, STOPPED_NEW), ("new", "new"), (AwaitRoute(WEB_ADDRESS, "new"),), Retire("old")
    ),
    "an instance this call routed that then stopped is replaced": Case(
        (STOPPED_NEW,), ("new", "new"), (STARTED_NEW, Check("new"), WriteRoute(WEB_ADDRESS, "new")), StartInstance(TARGET, FRESH)
    ),
    "a route to an instance that is gone is not taken as serving": Case((), ("gone", "gone"), (), StartInstance(TARGET, FRESH)),
    "an instance that failed its check is removed": Case((OLD, NEW), ("old", "old"), (STARTED_NEW, REFUSED), RemoveInstance("new")),
    "a failed check answers check failed, never switched": Case(
        (OLD,),
        ("old", "old"),
        (STARTED_NEW, REFUSED, RemoveInstance("new")),
        answer(Outcome.CHECK_FAILED, "old", "Connection refused"),
    ),
}


def seen(case: Case) -> LaneSeen:
    loaded, written = case.route
    if case.step == StartProxy():
        return LaneSeen(case.instances, ProxyNotRunning())
    if isinstance(loaded, str):
        loaded = LoadedRoute(loaded, RouteStatus.ENABLED)
    return LaneSeen(case.instances, LaneRoute(loaded, written))


def next_move_step(case: Case) -> object:
    return decide_move(seen(case), TARGET, Product(), FreshNames(FRESH, "fresh-data"), case.done)


def next_retire_step(case: Case) -> object:
    return decide_retire(seen(case), "site", "testing", None, case.done)


@pytest.mark.parametrize("case", MOVES.values(), ids=MOVES.keys())
def test_a_move_takes_this_step_next(case: Case) -> None:
    assert next_move_step(case) == case.step


@pytest.mark.parametrize("status", [RouteStatus.DISABLED, RouteStatus.WARNING])
def test_a_move_stops_and_says_so_when_the_loaded_route_is_not_enabled(status: RouteStatus) -> None:
    step = next_move_step(Case((NEW,), (LoadedRoute("new", status), "new"), (), None))
    assert isinstance(step, Stop)
    assert f"reports it {status}" in step.reason
    assert "Nothing was changed." in step.reason


def test_a_move_stopped_by_a_disabled_route_it_wrote_says_what_it_changed_not_that_nothing_was() -> None:
    done = (STARTED_NEW, Check("new"), WriteRoute(WEB_ADDRESS, "new"), AwaitRoute(WEB_ADDRESS, "new"))
    step = next_move_step(Case((OLD, NEW), (LoadedRoute("new", RouteStatus.DISABLED), "new"), done, None))
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


def test_no_step_decided_in_any_case_routes_or_awaits_another_lanes_web_address() -> None:
    steps = [next_move_step(case) for case in MOVES.values()] + [next_retire_step(case) for case in RETIRES.values()]
    assert {step.web_address for step in steps if isinstance(step, WriteRoute | RemoveRoute | AwaitRoute)} == {WEB_ADDRESS}


WITH_DATA = Product(health_path="/health", database=Database(image="postgres:18"), job=Job(command=["job"]))
DATABASE = Database(image="postgres:18")


def data_set(
    name: str,
    state: InstanceState | None = InstanceState.RUNNING,
    *,
    quiet: bool | None = True,
    volume: bool = True,
    databases: tuple[str, ...] | None = None,
) -> DataSet:
    servers = databases if databases is not None else (() if state is None else (f"database-{name}",))
    return DataSet(
        product="site",
        lane="testing",
        data_set=name,
        networks=() if quiet is None else (DataSetNetwork(name=f"network-{name}", is_quiet=quiet),),
        volumes=(f"volume-{name}",) if volume else (),
        databases=tuple(DataSetDatabase(container=server, state=state or InstanceState.RUNNING) for server in servers),
    )


def on_data(instance: RunningInstance, name: str) -> RunningInstance:
    return instance.model_copy(update={"data_set": name})


def job_of(instance: RunningInstance, state: InstanceState = InstanceState.RUNNING) -> RunningJob:
    return RunningJob(
        product="site",
        lane="testing",
        build=instance.build,
        container=f"{instance.container}-job",
        state=state,
        is_quiet=True,
        instance=instance.container,
    )


@dataclass(frozen=True)
class DataCase:
    instances: tuple[RunningInstance, ...]
    receiving: str | None
    data_sets: tuple[DataSet, ...]
    done: tuple[Event, ...]
    step: object
    jobs: tuple[RunningJob, ...] = ()


OLD_ON_D1 = on_data(OLD, "d1")
NEW_ON_D1 = on_data(NEW, "d1")
NEW_ON_D2 = on_data(NEW, "d2")
AWAITED_D1 = AwaitDatabase("database-d1", DATABASE)
USE_D1 = DataSetUse("d1", "network-d1", "database-d1", DATABASE)
NEW_JOB = job_of(NEW_ON_D1)
STARTED_NEW_JOB = StartJob(TARGET, "new", USE_D1, ("job",))

DATA_MOVES = {
    "a lane with no data set gets a fresh network before anything else": DataCase(
        (), None, (), (), CreateDataSetNetwork("site", "testing", "fresh-data")
    ),
    "a data set with a network and no volume gets its volume": DataCase(
        (), None, (data_set("d1", None, volume=False),), (), CreateDataSetVolume("site", "testing", "d1")
    ),
    "a data set with no database gets one on the network and volume as read": DataCase(
        (), None, (data_set("d1", None),), (), CreateDatabase("site", "testing", "d1", "network-d1", "volume-d1", DATABASE)
    ),
    "a stopped database is started by the name it was read under, not made again": DataCase(
        (),
        None,
        (data_set("d1", databases=("renamed-database",), state=InstanceState.STOPPED),),
        (),
        StartDatabase("renamed-database"),
    ),
    "a database is waited for before an instance starts on it": DataCase((), None, (data_set("d1"),), (), AWAITED_D1),
    "an instance starts on the data set once its database answered, given the names as read": DataCase(
        (), None, (data_set("d1"),), (AWAITED_D1,), StartInstance(TARGET, FRESH, USE_D1)
    ),
    "a release uses the data set of the instance receiving visitors, even beside another": DataCase(
        (OLD_ON_D1,), "old", (data_set("d1"), data_set("d2")), (AWAITED_D1,), StartInstance(TARGET, FRESH, USE_D1)
    ),
    "the new instance is checked at the product's health path": DataCase(
        (OLD_ON_D1, NEW_ON_D1), "old", (data_set("d1"),), (StartInstance(TARGET, "new", USE_D1), AWAITED_D1), Check("new", "/health")
    ),
    "an instance left by an interrupted release waits for its database to be read again before its check": DataCase(
        (OLD_ON_D1, NEW_ON_D1), "old", (data_set("d1"),), (), AWAITED_D1
    ),
    "a running instance of the build on another data set is retired, not switched to": DataCase(
        (OLD_ON_D1, NEW_ON_D2), "old", (data_set("d1"), data_set("d2")), (), Retire("new")
    ),
    "an instance that can reach out is retired, not switched to": DataCase(
        (OLD_ON_D1, NEW_ON_D1.model_copy(update={"is_quiet": False})), "old", (data_set("d1"),), (), Retire("new")
    ),
    "the old job stops before the new one starts": DataCase(
        (OLD_ON_D1, NEW_ON_D1), "new", (data_set("d1"),), (AWAITED_D1,), Retire("old-job"), (job_of(OLD_ON_D1),)
    ),
    "the new job starts once no other job runs, before the old instance retires": DataCase(
        (OLD_ON_D1, NEW_ON_D1), "new", (data_set("d1"),), (AWAITED_D1,), STARTED_NEW_JOB
    ),
    "a job started in this call is checked before anything answers": DataCase(
        (NEW_ON_D1,), "new", (data_set("d1"),), (AWAITED_D1, STARTED_NEW_JOB), CheckJob("new-job"), (NEW_JOB,)
    ),
    "a job that stopped is replaced": DataCase(
        (NEW_ON_D1,), "new", (data_set("d1"),), (AWAITED_D1,), Retire("new-job"), (job_of(NEW_ON_D1, InstanceState.STOPPED),)
    ),
    "a job left without its instance is stopped first": DataCase(
        (OLD_ON_D1,), "old", (data_set("d1"),), (), Retire("gone-job"), (job_of(lane_instance("gone", "a")),)
    ),
    "a stopped database under a serving instance is started, not answered unchanged": DataCase(
        (NEW_ON_D1,), "new", (data_set("d1", InstanceState.STOPPED),), (), StartDatabase("database-d1"), (NEW_JOB,)
    ),
    "a serving lane answers only after its database answered just now": DataCase(
        (NEW_ON_D1,), "new", (data_set("d1"),), (), AWAITED_D1, (NEW_JOB,)
    ),
}


def lane_seen_of(case: DataCase) -> LaneSeen:
    loaded = None if case.receiving is None else LoadedRoute(case.receiving, RouteStatus.ENABLED)
    return LaneSeen(case.instances, LaneRoute(loaded, case.receiving), case.jobs, case.data_sets)


def next_data_move_step(case: DataCase) -> Action | Done[MoveResult] | Stop:
    return decide_move(lane_seen_of(case), TARGET, WITH_DATA, FreshNames(FRESH, "fresh-data"), case.done)


@pytest.mark.parametrize("case", DATA_MOVES.values(), ids=DATA_MOVES.keys())
def test_a_move_with_data_takes_this_step_next(case: DataCase) -> None:
    assert next_data_move_step(case) == case.step


def test_a_job_that_stops_during_its_check_answers_job_failed_with_why_not_switched() -> None:
    refused = JobRefused("new-job", "The job new-job stopped within 3 s of starting.")
    done = (STARTED_NEW_JOB, AWAITED_D1, refused, WriteRoute(WEB_ADDRESS, "new"))
    step = next_data_move_step(DataCase((NEW_ON_D1,), "new", (data_set("d1"),), done, None, (NEW_JOB,)))
    assert isinstance(step, Done)
    assert (step.answer.outcome, step.answer.error) == (Outcome.JOB_FAILED, refused.report)


DATA_STOPS = {
    "two data sets and no instance receiving visitors": (
        DataCase((), None, (data_set("d1"), data_set("d2")), (), None),
        "cannot tell which",
    ),
    "a data set whose network is not quiet": (DataCase((), None, (data_set("d1", quiet=False),), (), None), "not quiet"),
    "a left-over instance on a data set whose network is not quiet": (
        DataCase((OLD_ON_D1, NEW_ON_D1), "old", (data_set("d1", quiet=False),), (), None),
        "not quiet",
    ),
    "a database that keeps restarting": (DataCase((), None, (data_set("d1", InstanceState.RESTARTING),), (), None), "is restarting"),
    "a receiving instance whose data set is gone": (DataCase((OLD_ON_D1,), "old", (), (), None), "not on the host"),
    "two database servers carrying one data set's labels": (
        DataCase((), None, (data_set("d1", databases=("database-a", "database-b")),), (), None),
        "2 database servers (database-a, database-b)",
    ),
}


@pytest.mark.parametrize(("case", "reason"), DATA_STOPS.values(), ids=DATA_STOPS.keys())
def test_a_move_with_data_stops_with_why_instead_of_starting_an_instance(case: DataCase, reason: str) -> None:
    step = next_data_move_step(case)
    assert isinstance(step, Stop)
    assert reason in step.reason


def test_a_retire_stops_jobs_before_instances_and_deletes_every_part_read_of_the_named_data_set() -> None:
    doubled = data_set("d2", databases=("database-a", "database-b"))

    def step(
        instances: tuple[RunningInstance, ...], jobs: tuple[RunningJob, ...], delete: str | None
    ) -> Action | Done[RetireResult] | Stop:
        lane_seen = LaneSeen(instances, LaneRoute(None, None), jobs, (data_set("d1"), doubled))
        return decide_retire(lane_seen, "site", "testing", delete, ())

    assert step((OLD_ON_D1,), (job_of(OLD_ON_D1),), "d1") == Retire("old-job")
    assert step((), (), "d2") == DeleteDataSet(doubled)
    kept = step((), (), None)
    assert isinstance(kept, Done)
    assert kept.answer.kept_data_sets == ["d1", "d2"]
