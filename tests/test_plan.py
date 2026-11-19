from dataclasses import dataclass, replace
from typing import cast

import pytest

from launchplane.gate import Answer as GateAnswer
from launchplane.instance import (
    DataSet,
    DataSetUse,
    InstanceState,
    LaneBuild,
    LoadedRoute,
    Mount,
    Reading,
    RouteStatus,
    RunningBackgroundService,
    RunningInstance,
    Unread,
    Verdict,
)
from launchplane.judge import Judgement, Kind, Work, judge
from launchplane.plan import Move, decide_move, decide_retire, too_late_to_cancel
from launchplane.product import Product
from launchplane.steps import (
    Abandon,
    Action,
    AwaitBackgroundService,
    AwaitDatabase,
    AwaitRoute,
    BackgroundServiceRefused,
    Cancel,
    CannotPutBack,
    Check,
    CheckRefused,
    CreateDatabase,
    CreateDataSetNetwork,
    CreateDataSetVolume,
    DeleteDataSet,
    Discard,
    Discarded,
    Done,
    Event,
    FreshNames,
    MoveResult,
    Outcome,
    PutBack,
    RemovalRefused,
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
    TooLate,
    Wait,
)
from tests.pretend_host import (
    D1,
    EARLIER,
    GENERATION,
    NEW,
    NEW_BACKGROUND_SERVICE,
    NEW_ON_D1,
    OLD,
    OLD_ON_D1,
    SINCE,
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
ATTEMPT = 7
NO_VERDICT, ABANDONED = GateAnswer(Verdict.NONE), GateAnswer(Verdict.ABANDONED)
COMMITTED_TO_NEW = GateAnswer(Verdict.COMMITTED, "new")
STARTED_NEW = StartInstance(TARGET, "new", None, GENERATION)
SWITCH_TO_NEW = Switch(WEB_ADDRESS, "new", GENERATION, ATTEMPT)


# The step that takes an instance through the gate and removes it. `made_by` is the generation its label names.
def gone(container: str, made_by: str | None = None) -> Discard:
    return Discard(WEB_ADDRESS, container, made_by, ATTEMPT)


SWITCHED_TO_NEW = Switched("new", won=True)
REFUSED = CheckRefused("new", "Connection refused")


@dataclass(frozen=True)
class Case:
    instances: tuple[RunningInstance, ...]
    route: tuple[str | LoadedRoute | None, str | None]
    done: tuple[Event, ...]
    step: object
    # Whether the routes file says it was renamed for this release.
    gate: GateAnswer = NO_VERDICT


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
    "an instance that passed its check is routed": Case((OLD, NEW), ("old", "old"), (STARTED_NEW, Check("new")), SWITCH_TO_NEW),
    "an instance that stopped after passing is checked again, not routed": Case(
        (OLD, STOPPED_NEW), ("old", "old"), (STARTED_NEW, Check("new")), Check("new")
    ),
    "the old instance retires only once the route to the new one has loaded": Case(
        (OLD, NEW),
        ("new", "new"),
        (STARTED_NEW, Check("new"), SWITCHED_TO_NEW),
        gone("old"),
        gate=COMMITTED_TO_NEW,
    ),
    "a finished switch answers switched": Case(
        (NEW,),
        ("new", "new"),
        (STARTED_NEW, Check("new"), SWITCHED_TO_NEW, Discarded("old", kept=False)),
        answer(Outcome.SWITCHED, "new", did=("started the instance new", "wrote the route to new", "retired old")),
        gate=COMMITTED_TO_NEW,
    ),
    "a switch found in the routes file answers switched, though this attempt did none of it": Case(
        (NEW,), ("new", "new"), (), answer(Outcome.SWITCHED, "new"), gate=COMMITTED_TO_NEW
    ),
    "a route renamed for this release and not loaded is waited for as long as it takes": Case(
        (OLD, NEW), ("old", "new"), (), AwaitRoute(WEB_ADDRESS, "new", enabled=True), gate=COMMITTED_TO_NEW
    ),
    "asking for the serving build again changes nothing": Case((NEW,), ("new", "new"), (), answer(Outcome.UNCHANGED, "new")),
    "an instance left beside the serving build is retired": Case((NEW, STRAY), ("new", "new"), (), gone("stray")),
    "a finished cleanup answers repaired, with what it did": Case(
        (NEW,), ("new", "new"), (Discarded("stray", kept=False),), answer(Outcome.REPAIRED, "new", did=("retired stray",))
    ),
    "a running instance of the build that anything earlier left is retired, never taken for the candidate": Case(
        (OLD, NEW), ("old", "old"), (), gone("new")
    ),
    "what anything earlier left is retired before a candidate is made": Case((OLD, STRAY), ("old", "old"), (), gone("stray")),
    "a stopped instance left before the switch is retired, then replaced": Case((OLD, STOPPED_NEW), ("old", "old"), (), gone("new")),
    "a stopped serving instance is replaced, never checked": Case(
        (STOPPED_NEW,), ("new", "new"), (), StartInstance(TARGET, FRESH, None, GENERATION)
    ),
    "a serving instance that stopped while its route was awaited is replaced, not kept": Case(
        (OLD, STOPPED_NEW), ("new", "new"), (AwaitRoute(WEB_ADDRESS, "new"),), gone("old")
    ),
    "a route to an instance that is gone is not taken as serving": Case(
        (), ("gone", "gone"), (), StartInstance(TARGET, FRESH, None, GENERATION)
    ),
    "an instance that failed its check is removed": Case((OLD, NEW), ("old", "old"), (STARTED_NEW, REFUSED), gone("new")),
    "a failed check answers check failed, never switched": Case(
        (OLD,),
        ("old", "old"),
        (STARTED_NEW, REFUSED, Discarded("new", kept=False)),
        answer(Outcome.CHECK_FAILED, "old", "Connection refused", did=("started the instance new", "retired new")),
    ),
}


def given(case: Case) -> Given:
    return Given(case.instances, case.route, proxy_runs=case.step != StartProxy())


def a_move(judged: Judgement, done: tuple[Event, ...], gate: GateAnswer, product: Product) -> Move:
    return Move(judged, TARGET, product, FRESH_NAMES, done, GENERATION, ATTEMPT, gate)


def next_move_step(case: Case, cancel: Cancel | None = None) -> object:
    return decide_move(a_move(given(case).judged(Work.MOVE, Product()), case.done, case.gate, Product()), cancel)


def next_retire_step(case: Case) -> object:
    return decide_retire(given(case).judged(Work.RETIRE, Product()), None, case.done)


@pytest.mark.parametrize("case", MOVES.values(), ids=MOVES.keys())
def test_a_move_takes_this_step_next(case: Case) -> None:
    assert next_move_step(case) == case.step


MADE_NEW = changed(NEW, generation=GENERATION)
TOO_LATE = TooLate(too_late_to_cancel(WEB_ADDRESS))
CARRIED_TO_THE_GATE = Abandon(WEB_ADDRESS, GENERATION)
DISCARD_NEW = gone("new", GENERATION)
SERVING_OLD, SERVING_NEW = ("old", "old"), ("new", "new")
# What the gate answers, the lane as read, and what a move does with a cancel asked of it: the gate's answer alone picks.
CANCELS = {
    "no verdict, nothing made: the cancel is carried to the gate": (NO_VERDICT, Case((OLD,), SERVING_OLD, (), CARRIED_TO_THE_GATE)),
    "no verdict, a candidate checked: the cancel is carried to the gate": (
        NO_VERDICT,
        Case((OLD, MADE_NEW), SERVING_OLD, (STARTED_NEW, Check("new")), CARRIED_TO_THE_GATE),
    ),
    "no verdict, though the routes file names the candidate: the file says nothing of the verdict": (
        NO_VERDICT,
        Case((OLD, MADE_NEW), SERVING_NEW, (), CARRIED_TO_THE_GATE),
    ),
    "committed, the proxy still on the old instance: too late": (COMMITTED_TO_NEW, Case((OLD, MADE_NEW), SERVING_OLD, (), TOO_LATE)),
    "committed and loaded: too late": (COMMITTED_TO_NEW, Case((OLD, MADE_NEW), SERVING_NEW, (), TOO_LATE)),
    "abandoned: its candidate goes through the gate and is removed": (
        ABANDONED,
        Case((OLD, MADE_NEW), SERVING_OLD, (), DISCARD_NEW),
    ),
    "abandoned, sweep finished": (ABANDONED, Case((OLD,), SERVING_OLD, (CARRIED_TO_THE_GATE,), Done(PutBack(findings=[])))),
}


@pytest.mark.parametrize(("gate", "case"), CANCELS.values(), ids=CANCELS.keys())
def test_with_a_cancel_asked_a_move_does_what_the_gate_answers_for_its_generation(gate: GateAnswer, case: Case) -> None:
    assert next_move_step(replace(case, gate=gate), Cancel()) == case.step


FOLLOWS_THE_GATE = {
    "abandoned with no cancel on record is put back all the same": (ABANDONED, Case((OLD, MADE_NEW), SERVING_OLD, (), DISCARD_NEW)),
    "abandoned leaves an instance of the build another generation made": (
        ABANDONED,
        Case(
            (OLD, changed(NEW, generation=EARLIER)),
            SERVING_OLD,
            (CARRIED_TO_THE_GATE,),
            Done(PutBack(findings=["The instance new runs beside the active one"])),
        ),
    ),
    "committed while the routes file still names the old instance waits for the candidate the gate names": (
        COMMITTED_TO_NEW,
        Case((OLD, MADE_NEW), ("old", "old"), (), AwaitRoute(WEB_ADDRESS, "new", enabled=True)),
    ),
    "committed to an instance that is not on the host stops, and the old instance is not touched": (
        COMMITTED_TO_NEW,
        Case((OLD,), SERVING_OLD, (), None),
    ),
    "no verdict with the routes file and the proxy naming an instance of this generation is waited on": (
        NO_VERDICT,
        Case((OLD, MADE_NEW), SERVING_NEW, (), None),
    ),
    "abandoned with the proxy on an instance of this generation is waited on, and its background service is not touched": (
        ABANDONED,
        Case((OLD, MADE_NEW), SERVING_NEW, (), None),
    ),
    "abandoned with a part whose release cannot be read is waited on": (
        ABANDONED,
        Case((OLD, changed(NEW, generation=Unread(why="the label is no generation"))), SERVING_OLD, (), None),
    ),
    "no verdict with a leftover whose release cannot be read is waited on, and the gate is not skipped": (
        NO_VERDICT,
        Case((OLD, changed(NEW, generation=Unread(why="the label is no generation"))), SERVING_OLD, (), None),
    ),
}


@pytest.mark.parametrize(("gate", "case"), FOLLOWS_THE_GATE.values(), ids=FOLLOWS_THE_GATE.keys())
def test_a_move_follows_the_gates_answer_and_never_the_routes_file_or_the_loaded_route(gate: GateAnswer, case: Case) -> None:
    step = next_move_step(replace(case, gate=gate))
    assert isinstance(step, Stop) if case.step is None else step == case.step
    assert isinstance(step, Wait) == (case.step is None and gate is not COMMITTED_TO_NEW)


LEFT_BY_EARLIER = changed(NEW, generation=EARLIER)


def test_an_earlier_winner_is_retired_only_once_this_release_is_committed_elsewhere_and_visitors_are_sent_there() -> None:
    winner: RunningInstance = changed(OLD, generation=EARLIER)
    kept = (Discarded("old", kept=True),)
    asked = Case((winner, MADE_NEW), SERVING_NEW, (), gone("old", EARLIER), COMMITTED_TO_NEW)
    before_this_release_is_committed = Case((winner,), (None, None), kept, None)
    assert [next_move_step(asked), next_move_step(replace(asked, done=kept))] == [asked.step, Retire("old")]
    assert isinstance(next_move_step(before_this_release_is_committed), StartInstance)


def test_a_removal_docker_refused_stops_a_move_and_ends_a_cancel_that_won_as_could_not_put_back() -> None:
    refused = Case((OLD, MADE_NEW), SERVING_OLD, (RemovalRefused("new", "409 Conflict: removal in progress"),), None)
    stopped, could_not = next_move_step(refused), next_move_step(replace(refused, gate=ABANDONED))
    assert isinstance(stopped, Stop) and "could not be removed: 409 Conflict" in stopped.reason
    assert isinstance(could_not, CannotPutBack) and could_not.reason.startswith("The cancel won, and new could not be removed")


def test_a_cancel_asked_while_the_proxy_is_down_starts_it_first_since_the_gate_is_a_command_in_its_container() -> None:
    judged = Given((OLD,), (None, "old"), proxy_runs=False).judged(Work.MOVE, Product())
    assert decide_move(a_move(judged, (), NO_VERDICT, Product()), Cancel()) == StartProxy()


def test_a_background_service_that_does_not_stay_up_under_a_committed_release_stops_it_and_never_answers_failed() -> None:
    refused = BackgroundServiceRefused("new-background-service", "The background service new-background-service is not running.")
    judged = Given((NEW_ON_D1,), SERVING_NEW, (STOPPED_NEW_BACKGROUND_SERVICE,), (D1,)).judged(Work.MOVE)
    step = decide_move(a_move(judged, (STARTED_NEW_BACKGROUND_SERVICE, refused), COMMITTED_TO_NEW, WITH_DATA))
    assert isinstance(step, Stop) and "did not stay up" in step.reason


def test_the_old_web_instance_stays_while_the_new_background_service_has_not_stayed_up() -> None:
    refused = BackgroundServiceRefused("new-background-service", "The background service new-background-service is not running.")
    judged = Given((OLD_ON_D1, NEW_ON_D1), SERVING_NEW, (STOPPED_NEW_BACKGROUND_SERVICE,), (D1,)).judged(Work.MOVE)
    step = decide_move(a_move(judged, (STARTED_NEW_BACKGROUND_SERVICE, refused), COMMITTED_TO_NEW, WITH_DATA))
    assert isinstance(step, Stop) and "did not stay up" in step.reason


def test_a_background_service_of_a_cancelled_release_that_docker_refuses_to_remove_ends_as_could_not_put_back() -> None:
    made = (changed(NEW_ON_D1, generation=GENERATION),)
    service = STOPPED_NEW_BACKGROUND_SERVICE.model_copy(update={"generation": GENERATION})
    judged = Given((OLD_ON_D1, *made), ("old", "old"), (service,), (D1,)).judged(Work.MOVE)
    asked = decide_move(a_move(judged, (), ABANDONED, WITH_DATA))
    refused = decide_move(a_move(judged, (RemovalRefused(service.container, "409 Conflict"),), ABANDONED, WITH_DATA))
    assert asked == Discard(WEB_ADDRESS, service.container, None, ATTEMPT)
    assert isinstance(refused, CannotPutBack) and f"{service.container} could not be removed" in refused.reason


@pytest.mark.parametrize("status", [RouteStatus.DISABLED, RouteStatus.WARNING])
def test_a_move_stops_and_says_so_when_the_loaded_route_is_not_enabled(status: RouteStatus) -> None:
    step = next_move_step(Case((NEW,), (LoadedRoute("new", status), "new"), (), None))
    assert isinstance(step, Stop)
    assert f"reports it {status}" in step.reason
    assert "Nothing was changed." in step.reason


def test_a_committed_release_whose_route_the_proxy_reports_not_enabled_is_waited_for_and_nothing_old_is_retired() -> None:
    waited = AwaitRoute(WEB_ADDRESS, "new", enabled=True)
    disabled = Case((OLD, NEW), (LoadedRoute("new", RouteStatus.DISABLED), "new"), (), waited)
    assert next_move_step(replace(disabled, gate=COMMITTED_TO_NEW)) == disabled.step


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
        (OLD_ON_D1, NEW_ON_D1), "old", (data_set_named("d1", accepts=False),), (), gone("new")
    ),
    "a checked instance attached only to its own quiet networks is switched to": DataCase(
        (OLD_ON_D1, NEW_ON_D1),
        "old",
        (D1,),
        (STARTED_NEW_ON_D1, Check("new", "/health")),
        SWITCH_TO_NEW,
    ),
    "a running instance of the build on another data set is retired, not switched to": DataCase(
        (OLD_ON_D1, NEW_ON_D2), "old", (D1, data_set_named("d2")), (), gone("new")
    ),
    "a left-over instance that can reach out is retired, not switched to": DataCase(
        (OLD_ON_D1, on_data(NEW, "d1", "bridge")), "old", (D1,), (), gone("new")
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
    "the new background service starts once no other background service runs, before the old instance retires": DataCase(
        (OLD_ON_D1, NEW_ON_D1), "new", (D1,), (), STARTED_NEW_BACKGROUND_SERVICE
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
    step = decide_move(a_move(given_data(case).judged(Work.MOVE), case.done, NO_VERDICT, WITH_DATA))
    return cast("Action | Done[MoveResult] | Stop", step)


@pytest.mark.parametrize("case", DATA_MOVES.values(), ids=DATA_MOVES.keys())
def test_a_move_with_data_takes_this_step_next(case: DataCase) -> None:
    assert next_data_move_step(case) == case.step


LEFT_ON_D1 = data_set_named("d1", unheld=("i_left",))
AT_REST_WITH_A_LOGIN_LEFT = DataCase((NEW_ON_D1,), "new", (LEFT_ON_D1,), (), None, (NEW_BACKGROUND_SERVICE,))


def test_a_move_does_not_answer_while_a_running_server_has_a_login_no_container_holds() -> None:
    assert next_data_move_step(AT_REST_WITH_A_LOGIN_LEFT) == RemoveLogins("database-d1")


def test_a_failed_check_is_not_answered_until_the_login_of_the_instance_it_removed_is_gone() -> None:
    refused = (
        StartInstance(TARGET, "new", USE_D1),
        CheckRefused("new", "Connection refused"),
        gone("new"),
    )
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
    done = (STARTED_NEW_BACKGROUND_SERVICE, refused)
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


def test_a_routes_file_launchplane_did_not_write_is_that_lanes_fault_and_stops_its_move() -> None:
    proxy = Given((OLD,), ("old", None)).proxy()
    proxy.unread[WEB_ADDRESS] = "The routes file is not the route Launchplane writes"
    lane = Reading(SINCE, proxy.read(), proxy.host.read(), {WEB_ADDRESS: None}).lane("site", "testing")
    judged = judge(lane, Product(), Work.MOVE)
    step = decide_move(a_move(judged, (), NO_VERDICT, Product()))
    assert [finding.shown for finding in judged.of_kind(Kind.ROUTE_UNREAD)] == ["wrong"]
    assert isinstance(step, Stop) and "so the lane is not moved" in step.reason
