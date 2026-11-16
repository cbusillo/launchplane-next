from dataclasses import dataclass

import pytest

from launchplane.decide import (
    Action,
    AwaitDatabase,
    AwaitJob,
    AwaitRoute,
    Check,
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
    Verdict,
    WriteRoute,
    decide_move,
    decide_retire,
    lane_problems,
    lane_verdict,
)
from launchplane.instance import (
    LANE_NETWORK,
    DataSet,
    DataSetDatabase,
    HostNetwork,
    InstanceState,
    LaneBuild,
    Mount,
    RunningInstance,
    RunningJob,
)
from launchplane.product import Database, Job, Product

WEB_ADDRESS = "testing.site.test"
TARGET = LaneBuild(product="site", lane="testing", build="b")
FRESH = "site-testing-fresh"
QUIET = {LANE_NETWORK: True, "network-d1": True, "network-d2": True, "bridge": False}


def lane_instance(container: str, build: str, state: InstanceState = InstanceState.RUNNING) -> RunningInstance:
    return RunningInstance(product="site", lane="testing", build=build, container=container, state=state, networks=(LANE_NETWORK,))


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
    return LaneSeen(case.instances, LaneRoute(loaded, written), network_quiet=QUIET)


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
    accepts: bool | None = True,
) -> DataSet:
    servers = databases if databases is not None else (() if state is None else (f"database-{name}",))
    return DataSet(
        product="site",
        lane="testing",
        data_set=name,
        networks=() if quiet is None else (HostNetwork(name=f"network-{name}", is_quiet=quiet),),
        volumes=(f"volume-{name}",) if volume else (),
        databases=tuple(
            DataSetDatabase(
                container=server,
                state=state or InstanceState.RUNNING,
                networks=(f"network-{name}",),
                accepts_connections=accepts if state == InstanceState.RUNNING else None,
                mounts=(Mount(source=f"volume-{name}", path="/var/lib/postgresql"),),
            )
            for server in servers
        ),
    )


def on_data(instance: RunningInstance, name: str, *extra_networks: str) -> RunningInstance:
    return instance.model_copy(update={"data_set": name, "networks": (LANE_NETWORK, f"network-{name}", *extra_networks)})


def job_of(
    instance: RunningInstance,
    running_for: float | None = 10.0,
    container: str | None = None,
    networks: tuple[str, ...] | None = None,
) -> RunningJob:
    return RunningJob(
        product="site",
        lane="testing",
        build=instance.build,
        container=container or f"{instance.container}-job",
        state=InstanceState.RUNNING if running_for is not None else InstanceState.STOPPED,
        networks=networks or (f"network-{instance.data_set}",),
        running_for_seconds=running_for,
        instance=instance.container,
        data_set=instance.data_set,
    )


@dataclass(frozen=True)
class DataCase:
    instances: tuple[RunningInstance, ...]
    receiving: str | None
    data_sets: tuple[DataSet, ...]
    done: tuple[Event, ...]
    step: object
    jobs: tuple[RunningJob, ...] = ()


def with_database_on(found: DataSet, *networks: str) -> DataSet:
    return found.model_copy(update={"databases": (found.databases[0].model_copy(update={"networks": networks}),)})


def mounting(part: RunningInstance, source: str) -> RunningInstance:
    return part.model_copy(update={"mounts": (Mount(source=source, path="/data"),)})


def with_database_mounting(found: DataSet, *sources: str, at: tuple[str, ...] = ("/var/lib/postgresql",)) -> DataSet:
    mounts = tuple(Mount(source=source, path=path) for source in sources for path in at)
    return found.model_copy(update={"databases": (found.databases[0].model_copy(update={"mounts": mounts}),)})


OLD_ON_D1 = on_data(OLD, "d1")
NEW_ON_D1 = on_data(NEW, "d1")
NEW_ON_D2 = on_data(NEW, "d2")
D1 = data_set("d1")
RENAMED_STOPPED = data_set("d1", databases=("renamed-database",), state=InstanceState.STOPPED)
STOPPED_NEW_JOB = job_of(NEW_ON_D1, running_for=None)
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
        (RENAMED_STOPPED,),
        (),
        StartDatabase("renamed-database", DATABASE),
    ),
    "a database read as not accepting connections is waited for before an instance starts on it": DataCase(
        (), None, (data_set("d1", accepts=False),), (), AwaitDatabase("database-d1", DATABASE)
    ),
    "an instance starts on the data set once its database is read accepting connections": DataCase(
        (), None, (D1,), (), StartInstance(TARGET, FRESH, USE_D1)
    ),
    "a release uses the data set of the instance receiving visitors, even beside another": DataCase(
        (OLD_ON_D1,), "old", (D1, data_set("d2")), (), StartInstance(TARGET, FRESH, USE_D1)
    ),
    "the new instance is checked at the product's health path": DataCase(
        (OLD_ON_D1, NEW_ON_D1), "old", (D1,), (StartInstance(TARGET, "new", USE_D1),), Check("new", "/health")
    ),
    "an instance left by an interrupted release waits on its database as read before its check": DataCase(
        (OLD_ON_D1, NEW_ON_D1), "old", (data_set("d1", accepts=False),), (), AwaitDatabase("database-d1", DATABASE)
    ),
    "a checked instance attached only to its own quiet networks is switched to": DataCase(
        (OLD_ON_D1, NEW_ON_D1), "old", (D1,), (Check("new", "/health"),), WriteRoute(WEB_ADDRESS, "new")
    ),
    "a running instance of the build on another data set is retired, not switched to": DataCase(
        (OLD_ON_D1, NEW_ON_D2), "old", (D1, data_set("d2")), (), Retire("new")
    ),
    "a left-over instance that can reach out is retired, not switched to": DataCase(
        (OLD_ON_D1, on_data(NEW, "d1", "bridge")), "old", (D1,), (), Retire("new")
    ),
    "a serving instance attached to another data set's quiet network is replaced, not kept": DataCase(
        (on_data(NEW, "d1", "network-d2"),), "new", (D1, data_set("d2")), (), StartInstance(TARGET, FRESH, USE_D1), (NEW_JOB,)
    ),
    "the old job stops before the new one starts": DataCase(
        (OLD_ON_D1, NEW_ON_D1), "new", (D1,), (), Retire("old-job"), (job_of(OLD_ON_D1),)
    ),
    "the new job starts once no other job runs, before the old instance retires": DataCase(
        (OLD_ON_D1, NEW_ON_D1), "new", (D1,), (), STARTED_NEW_JOB
    ),
    "a job read as running less than its watch is waited on, whoever started it": DataCase(
        (NEW_ON_D1,), "new", (D1,), (), AwaitJob("new-job"), (job_of(NEW_ON_D1, running_for=1.0),)
    ),
    "a job that stopped is replaced": DataCase((NEW_ON_D1,), "new", (D1,), (), Retire("new-job"), (STOPPED_NEW_JOB,)),
    "a job this call started that is read restarting is awaited, so its failure is answered, not retried forever": DataCase(
        (NEW_ON_D1,),
        "new",
        (D1,),
        (STARTED_NEW_JOB,),
        AwaitJob("new-job"),
        (NEW_JOB.model_copy(update={"state": InstanceState.RESTARTING, "running_for_seconds": None}),),
    ),
    "a job read as restarting is replaced, however long Docker says it ran": DataCase(
        (NEW_ON_D1,), "new", (D1,), (), Retire("new-job"), (NEW_JOB.model_copy(update={"state": InstanceState.RESTARTING}),)
    ),
    "a job of another build than the instance it belongs to is replaced": DataCase(
        (NEW_ON_D1,), "new", (D1,), (), Retire("new-job"), (NEW_JOB.model_copy(update={"build": "a"}),)
    ),
    "a job with a volume mounted is replaced": DataCase(
        (NEW_ON_D1,),
        "new",
        (D1,),
        (),
        Retire("new-job"),
        (NEW_JOB.model_copy(update={"mounts": (Mount(source="volume-d1", path="/data"),)}),),
    ),
    "a serving instance with a volume mounted is replaced, not kept": DataCase(
        (mounting(NEW_ON_D1, "volume-d2"),), "new", (D1,), (), StartInstance(TARGET, FRESH, USE_D1), (NEW_JOB,)
    ),
    "a job labelled for another data set than its instance runs against is replaced": DataCase(
        (NEW_ON_D1,), "new", (D1,), (), Retire("new-job"), (NEW_JOB.model_copy(update={"data_set": "d2"}),)
    ),
    "a job that can reach out is replaced": DataCase(
        (NEW_ON_D1,), "new", (D1,), (), Retire("new-job"), (job_of(NEW_ON_D1, networks=("network-d1", "bridge")),)
    ),
    "two jobs of the serving instance are not answered unchanged: one is retired": DataCase(
        (NEW_ON_D1,), "new", (D1,), (), Retire("new-job"), (NEW_JOB, job_of(NEW_ON_D1, container="new-job-two"))
    ),
    "a job left without its instance is stopped first": DataCase(
        (OLD_ON_D1,), "old", (D1,), (), Retire("gone-job"), (job_of(on_data(lane_instance("gone", "a"), "d1")),)
    ),
    "a stopped database under a serving instance is started, not answered unchanged": DataCase(
        (NEW_ON_D1,), "new", (data_set("d1", InstanceState.STOPPED),), (), StartDatabase("database-d1", DATABASE), (NEW_JOB,)
    ),
    "a serving lane whose database does not accept connections is waited on, not answered": DataCase(
        (NEW_ON_D1,), "new", (data_set("d1", accepts=False),), (), AwaitDatabase("database-d1", DATABASE), (NEW_JOB,)
    ),
}


def lane_seen_of(case: DataCase) -> LaneSeen:
    loaded = None if case.receiving is None else LoadedRoute(case.receiving, RouteStatus.ENABLED)
    return LaneSeen(case.instances, LaneRoute(loaded, case.receiving), case.jobs, case.data_sets, QUIET)


def next_data_move_step(case: DataCase) -> Action | Done[MoveResult] | Stop:
    return decide_move(lane_seen_of(case), TARGET, WITH_DATA, FreshNames(FRESH, "fresh-data"), case.done)


@pytest.mark.parametrize("case", DATA_MOVES.values(), ids=DATA_MOVES.keys())
def test_a_move_with_data_takes_this_step_next(case: DataCase) -> None:
    assert next_data_move_step(case) == case.step


def test_a_lane_that_is_as_it_must_be_answers_unchanged() -> None:
    step = next_data_move_step(DataCase((NEW_ON_D1,), "new", (D1,), (), None, (NEW_JOB,)))
    assert isinstance(step, Done)
    assert step.answer.outcome == Outcome.UNCHANGED


def test_a_job_that_stops_during_its_watch_answers_job_failed_with_why_not_switched() -> None:
    refused = JobRefused("new-job", "The job new-job is not running.")
    done = (STARTED_NEW_JOB, refused, WriteRoute(WEB_ADDRESS, "new"))
    step = next_data_move_step(DataCase((NEW_ON_D1,), "new", (D1,), done, None, (STOPPED_NEW_JOB,)))
    assert isinstance(step, Done)
    assert (step.answer.outcome, step.answer.error) == (Outcome.JOB_FAILED, refused.report)


DATA_STOPS = {
    "two data sets and no instance receiving visitors": (DataCase((), None, (D1, data_set("d2")), (), None), "cannot tell which"),
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
    "a new instance given an outside network during its check": (
        DataCase((OLD_ON_D1, on_data(NEW, "d1", "bridge")), "old", (D1,), (STARTED_NEW, Check("new", "/health")), None),
        "can reach out through bridge",
    ),
    "a stopped database attached to an outside network, which is not started first": (
        DataCase((), None, (with_database_on(data_set("d1", InstanceState.STOPPED), "network-d1", "bridge"),), (), None),
        "can reach out through bridge",
    ),
    "a stopped database on a network that is not its data set's, which is not started first": (
        DataCase((), None, (with_database_on(data_set("d1", InstanceState.STOPPED), "network-d2"),), (), None),
        "network-d2, which it must not be",
    ),
    "a stopped database under a serving instance, attached to an outside network": (
        DataCase(
            (NEW_ON_D1,),
            "new",
            (with_database_on(data_set("d1", InstanceState.STOPPED), "network-d1", "bridge"),),
            (),
            None,
            (NEW_JOB,),
        ),
        "can reach out through bridge",
    ),
    "a running database with the right labels and network and another volume mounted": (
        DataCase((NEW_ON_D1,), "new", (with_database_mounting(D1, "another-volume"),), (), None, (NEW_JOB,)),
        "has another-volume mounted at /var/lib/postgresql, which it must not have; "
        "The database database-d1 does not have volume-d1 mounted at /var/lib/postgresql",
    ),
    "a database with its data set's volume mounted somewhere its data is not kept": (
        DataCase((), None, (with_database_mounting(D1, "volume-d1", at=("/somewhere-else",)),), (), None),
        "has volume-d1 mounted at /somewhere-else, which it must not have; "
        "The database database-d1 does not have volume-d1 mounted at /var/lib/postgresql",
    ),
    "a database with its data set's volume mounted twice": (
        DataCase((), None, (with_database_mounting(D1, "volume-d1", at=("/var/lib/postgresql", "/copy")),), (), None),
        "has volume-d1 mounted at /copy, which it must not have",
    ),
    "a database with a second volume mounted beside its own": (
        DataCase((), None, (with_database_mounting(D1, "volume-d1", "another-volume"),), (), None),
        "has another-volume mounted",
    ),
    "a stopped database with another volume mounted, which is not started first": (
        DataCase((), None, (with_database_mounting(data_set("d1", InstanceState.STOPPED), "another-volume"),), (), None),
        "has another-volume mounted",
    ),
    "a new instance that passed its check with a volume mounted": (
        DataCase((OLD_ON_D1, mounting(NEW_ON_D1, "volume-d1")), "old", (D1,), (STARTED_NEW, Check("new", "/health")), None),
        "has volume-d1 mounted",
    ),
    "an instance this call started and checked, on another data set than the one receiving visitors runs against": (
        DataCase(
            (OLD_ON_D1, NEW_ON_D2),
            "old",
            (D1, data_set("d2")),
            (StartInstance(TARGET, "new", DataSetUse("d2", "network-d2", "database-d2", DATABASE)), Check("new", "/health")),
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


LANE_PROBLEMS = {
    "an active instance that can reach out": (
        DataCase((on_data(NEW, "d1", "bridge"),), "new", (D1,), (), None, (NEW_JOB,)),
        "bridge",
    ),
    "an active instance on another data set's network": (
        DataCase((on_data(NEW, "d1", "network-d2"),), "new", (D1, data_set("d2")), (), None, (NEW_JOB,)),
        "network-d2, which it must not be",
    ),
    "two jobs": (DataCase((NEW_ON_D1,), "new", (D1,), (), None, (NEW_JOB, job_of(NEW_ON_D1, container="j2"))), "2 jobs"),
    "no job": (DataCase((NEW_ON_D1,), "new", (D1,), (), None), "0 jobs"),
    "a restarting job": (
        DataCase((NEW_ON_D1,), "new", (D1,), (), None, (NEW_JOB.model_copy(update={"state": InstanceState.RESTARTING}),)),
        "is restarting",
    ),
    "a job not yet past its watch": (DataCase((NEW_ON_D1,), "new", (D1,), (), None, (job_of(NEW_ON_D1, 1.0),)), "1.0 s of the 3 s"),
    "a database that does not accept connections": (
        DataCase((NEW_ON_D1,), "new", (data_set("d1", accepts=False),), (), None, (NEW_JOB,)),
        "does not accept connections",
    ),
    "a database attached to an outside network": (
        DataCase(
            (NEW_ON_D1,),
            "new",
            (with_database_on(D1, "network-d1", "bridge"),),
            (),
            None,
            (NEW_JOB,),
        ),
        "can reach out through bridge",
    ),
    "a database with nothing mounted": (
        DataCase((NEW_ON_D1,), "new", (with_database_mounting(D1),), (), None, (NEW_JOB,)),
        "does not have volume-d1 mounted",
    ),
    "a job with a volume mounted": (
        DataCase((NEW_ON_D1,), "new", (D1,), (), None, (NEW_JOB.model_copy(update={"mounts": (Mount(source="v", path="/data"),)}),)),
        "The job new-job has v mounted",
    ),
    "a job labelled for another data set": (
        DataCase((NEW_ON_D1,), "new", (D1,), (), None, (NEW_JOB.model_copy(update={"data_set": "d2"}),)),
        "says it runs against the data set d2, not d1",
    ),
    "a job of another build": (
        DataCase((NEW_ON_D1,), "new", (D1,), (), None, (NEW_JOB.model_copy(update={"build": "a"}),)),
        "runs a, not b",
    ),
    "a second instance beside the active one": (
        DataCase((NEW_ON_D1, OLD_ON_D1), "new", (D1,), (), None, (NEW_JOB,)),
        "runs beside the active one",
    ),
}


@pytest.mark.parametrize(("case", "problem"), LANE_PROBLEMS.values(), ids=LANE_PROBLEMS.keys())
def test_a_lane_is_judged_not_as_it_must_be_when_it_has(case: DataCase, problem: str) -> None:
    problems = lane_problems(lane_seen_of(case), TARGET, WITH_DATA)
    assert any(problem in each for each in problems), problems


SWITCH_PROBLEMS = {
    "its data set's database does not accept connections": (NEW_ON_D1, data_set("d1", accepts=False), "does not accept connections"),
    "its data set's network is not quiet": (NEW_ON_D1, data_set("d1", quiet=False), "not quiet"),
    "its data set's database can reach out": (
        NEW_ON_D1,
        with_database_on(D1, "network-d1", "bridge"),
        "can reach out through bridge",
    ),
    "it can reach out itself": (on_data(NEW, "d1", "bridge"), D1, "can reach out through bridge"),
}


@pytest.mark.parametrize(("candidate", "found", "problem"), SWITCH_PROBLEMS.values(), ids=SWITCH_PROBLEMS.keys())
def test_the_lane_is_judged_not_fit_to_switch_to_an_instance_when(candidate: RunningInstance, found: DataSet, problem: str) -> None:
    lane_seen = lane_seen_of(DataCase((OLD_ON_D1, candidate), "old", (found,), (), None, (job_of(OLD_ON_D1),)))
    problems = lane_problems(lane_seen, TARGET, WITH_DATA, switching_to=candidate)
    assert any(problem in each for each in problems), problems


def test_before_the_switch_the_old_instance_and_its_job_are_not_problems_because_the_steps_after_it_replace_them() -> None:
    lane_seen = lane_seen_of(DataCase((OLD_ON_D1, NEW_ON_D1), "old", (D1,), (), None, (job_of(OLD_ON_D1),)))
    assert lane_problems(lane_seen, TARGET, WITH_DATA, switching_to=NEW_ON_D1) == []


OLD_JOB = job_of(OLD_ON_D1)
STATES_OF_A_RELEASE = {
    "a new instance being prepared beside the active one": (
        DataCase((OLD_ON_D1, NEW_ON_D1), "old", (D1,), (), None, (OLD_JOB,)),
        ["The instance new runs beside the active one"],
    ),
    "the web address moved and the old instance and its job still there": (
        DataCase((OLD_ON_D1, NEW_ON_D1), "new", (D1,), (), None, (OLD_JOB,)),
        ["The job old-job belongs to old, not new", "The instance old runs beside the active one"],
    ),
    "the old job stopped and the new one not yet started": (
        DataCase((OLD_ON_D1, NEW_ON_D1), "new", (D1,), (), None),
        ["The lane has 0 jobs (none) where it must have one, belonging to new", "The instance old runs beside the active one"],
    ),
    "the new job not yet up as long as it must stay up": (
        DataCase((NEW_ON_D1,), "new", (D1,), (), None, (job_of(NEW_ON_D1, 1.0),)),
        ["The job new-job has run 1.0 s of the 3 s it must stay up"],
    ),
    "the new job made and not yet started, or the old one stopped and not yet removed": (
        DataCase((NEW_ON_D1,), "new", (D1,), (), None, (job_of(NEW_ON_D1, running_for=None),)),
        ["The job new-job is stopped"],
    ),
    "a lane being brought up, its database not yet accepting connections": (
        DataCase((NEW_ON_D1,), None, (data_set("d1", accepts=False),), (), None),
        ["No instance receives visitors", "The database database-d1 does not accept connections"],
    ),
}


def verdict_on(case: DataCase, *, moving: bool) -> Verdict:
    return lane_verdict(lane_seen_of(case), WITH_DATA, a_move_is_at_work=moving)


@pytest.mark.parametrize(("case", "states"), STATES_OF_A_RELEASE.values(), ids=STATES_OF_A_RELEASE.keys())
def test_while_a_move_is_at_work_what_is_read_is_a_release_under_way_and_none_of_it_is_wrong(
    case: DataCase, states: list[str]
) -> None:
    assert verdict_on(case, moving=True) == Verdict(problems=[], release_under_way=states, left_unfinished=[])


@pytest.mark.parametrize(("case", "states"), STATES_OF_A_RELEASE.values(), ids=STATES_OF_A_RELEASE.keys())
def test_with_no_move_at_work_the_same_state_is_a_release_left_unfinished_however_lately_it_was_made(
    case: DataCase, states: list[str]
) -> None:
    assert verdict_on(case, moving=False) == Verdict(problems=[], release_under_way=[], left_unfinished=states)


def test_a_lane_at_rest_and_as_it_must_be_has_nothing_wrong_nothing_under_way_and_nothing_unfinished() -> None:
    assert verdict_on(DataCase((OLD_ON_D1,), "old", (D1,), (), None, (OLD_JOB,)), moving=False) == Verdict([], [], [])


NEVER_ACCEPTABLE = {
    "the instance being prepared can reach out": (
        DataCase((OLD_ON_D1, on_data(NEW, "d1", "bridge")), "old", (D1,), (), None, (OLD_JOB,)),
        "The instance new can reach out through bridge",
    ),
    "the instance being prepared is attached to another data set": (
        DataCase((OLD_ON_D1, on_data(NEW, "d1", "network-d2")), "old", (D1, data_set("d2")), (), None, (OLD_JOB,)),
        "The instance new is attached to network-d2, which it must not be",
    ),
    "the old instance's job can reach out": (
        DataCase((OLD_ON_D1, NEW_ON_D1), "new", (D1,), (), None, (job_of(OLD_ON_D1, networks=("network-d1", "bridge")),)),
        "The job old-job can reach out through bridge",
    ),
    "the database is attached to an outside network": (
        DataCase((OLD_ON_D1, NEW_ON_D1), "old", (with_database_on(D1, "network-d1", "bridge"),), (), None, (OLD_JOB,)),
        "The database database-d1 can reach out through bridge",
    ),
    "the database has another data set's volume mounted": (
        DataCase((OLD_ON_D1, NEW_ON_D1), "old", (with_database_mounting(D1, "volume-d1", "volume-d2"),), (), None, (OLD_JOB,)),
        "The database database-d1 has volume-d2 mounted at /var/lib/postgresql, which it must not have",
    ),
}


@pytest.mark.parametrize(("case", "fault"), NEVER_ACCEPTABLE.values(), ids=NEVER_ACCEPTABLE.keys())
def test_what_is_never_acceptable_is_wrong_even_while_a_move_is_at_work_and_is_not_told_as_part_of_the_release(
    case: DataCase, fault: str
) -> None:
    verdict = verdict_on(case, moving=True)
    assert fault in verdict.problems
    assert fault not in verdict.release_under_way
    assert "The instance new runs beside the active one" in verdict.release_under_way or case.receiving == "new"


def test_a_job_that_keeps_restarting_with_no_move_at_work_is_wrong_not_something_left_unfinished() -> None:
    restarting = job_of(NEW_ON_D1).model_copy(update={"state": InstanceState.RESTARTING, "running_for_seconds": None})
    verdict = verdict_on(DataCase((NEW_ON_D1,), "new", (D1,), (), None, (restarting,)), moving=False)
    assert verdict == Verdict(["The job new-job is restarting"], [], [])


def test_a_lane_as_it_must_be_has_no_problems() -> None:
    assert lane_problems(lane_seen_of(DataCase((NEW_ON_D1,), "new", (D1,), (), None, (NEW_JOB,))), TARGET, WITH_DATA) == []


def test_a_retire_stops_jobs_before_instances_and_deletes_every_part_read_of_the_named_data_set() -> None:
    doubled = data_set("d2", databases=("database-a", "database-b"))

    def step(
        instances: tuple[RunningInstance, ...], jobs: tuple[RunningJob, ...], delete: str | None
    ) -> Action | Done[RetireResult] | Stop:
        lane_seen = LaneSeen(instances, LaneRoute(None, None), jobs, (D1, doubled))
        return decide_retire(lane_seen, "site", "testing", delete, ())

    assert step((OLD_ON_D1,), (job_of(OLD_ON_D1),), "d1") == Retire("old-job")
    assert step((), (), "d2") == DeleteDataSet(doubled)
    kept = step((), (), None)
    assert isinstance(kept, Done)
    assert kept.answer.kept_data_sets == ["d1", "d2"]
