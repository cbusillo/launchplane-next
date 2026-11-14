from dataclasses import dataclass

import pytest

from launchplane.decide import (
    AwaitRoute,
    Check,
    CheckRefused,
    Done,
    Event,
    LaneRoute,
    LaneSeen,
    MoveResult,
    Outcome,
    ProxyNotRunning,
    RemoveCopy,
    RemoveRoute,
    Retire,
    RetireOutcome,
    RetireResult,
    StartCopy,
    StartProxy,
    WriteRoute,
    decide_move,
    decide_retire,
)
from launchplane.running_copy import CopyState, LaneCopy, RunningCopy

NAME = "testing.site.test"
TARGET = LaneCopy(product="site", lane="testing", build="b")
FRESH = "site-testing-fresh"


def lane_copy(name: str, build: str, state: CopyState = CopyState.RUNNING) -> RunningCopy:
    return RunningCopy(product="site", lane="testing", build=build, name=name, state=state)


OLD = lane_copy("old", "a")
NEW = lane_copy("new", "b")
STOPPED_NEW = lane_copy("new", "b", CopyState.STOPPED)
STRAY = lane_copy("stray", "c")
STARTED_NEW = StartCopy(TARGET, "new")
REFUSED = CheckRefused("new", "Connection refused")


@dataclass(frozen=True)
class Case:
    copies: tuple[RunningCopy, ...]
    route: tuple[str | None, str | None]
    done: tuple[Event, ...]
    step: object


def answer(outcome: Outcome, serving: str | None, error: str | None = None) -> Done[MoveResult]:
    return Done(MoveResult(product="site", lane="testing", build="b", name=NAME, outcome=outcome, serving=serving, error=error))


MOVES = {
    "with no proxy running, it is started first": Case((), (None, None), (), StartProxy()),
    "a route written and not loaded is waited for before anything else": Case(
        (OLD, NEW), ("old", "new"), (), AwaitRoute(NAME, "new")
    ),
    "a first move starts a copy": Case((), (None, None), (), StartCopy(TARGET, FRESH)),
    "a copy this call started is checked before any route names it": Case((NEW,), (None, None), (STARTED_NEW,), Check("new")),
    "a copy that passed its check is routed": Case((OLD, NEW), ("old", "old"), (STARTED_NEW, Check("new")), WriteRoute(NAME, "new")),
    "a copy that stopped after passing is checked again, not routed": Case(
        (OLD, STOPPED_NEW), ("old", "old"), (STARTED_NEW, Check("new")), Check("new")
    ),
    "the old copy retires only once the route to the new one has loaded": Case(
        (OLD, NEW), ("new", "new"), (STARTED_NEW, Check("new"), WriteRoute(NAME, "new")), Retire("old")
    ),
    "a finished switch answers switched": Case(
        (NEW,), ("new", "new"), (STARTED_NEW, Check("new"), WriteRoute(NAME, "new"), Retire("old")), answer(Outcome.SWITCHED, "new")
    ),
    "asking for the serving build again changes nothing": Case((NEW,), ("new", "new"), (), answer(Outcome.UNCHANGED, "new")),
    "a copy left beside the serving build is retired": Case((NEW, STRAY), ("new", "new"), (), Retire("stray")),
    "a finished cleanup answers cleaned up": Case((NEW,), ("new", "new"), (Retire("stray"),), answer(Outcome.CLEANED_UP, "new")),
    "a running copy left before the switch is checked, not started again": Case((OLD, NEW), ("old", "old"), (), Check("new")),
    "a copy neither serving nor about to be checked is retired first": Case((OLD, NEW, STRAY), ("old", "old"), (), Retire("stray")),
    "a stopped copy left before the switch is retired, then replaced": Case((OLD, STOPPED_NEW), ("old", "old"), (), Retire("new")),
    "a stopped serving copy is replaced, never checked": Case((STOPPED_NEW,), ("new", "new"), (), StartCopy(TARGET, FRESH)),
    "a serving copy that stopped while its route was awaited is replaced, not kept": Case(
        (OLD, STOPPED_NEW), ("new", "new"), (AwaitRoute(NAME, "new"),), Retire("old")
    ),
    "a copy this call routed that then stopped is replaced": Case(
        (STOPPED_NEW,), ("new", "new"), (STARTED_NEW, Check("new"), WriteRoute(NAME, "new")), StartCopy(TARGET, FRESH)
    ),
    "a route to a copy that is gone is not taken as serving": Case((), ("gone", "gone"), (), StartCopy(TARGET, FRESH)),
    "a copy that failed its check is removed": Case((OLD, NEW), ("old", "old"), (STARTED_NEW, REFUSED), RemoveCopy("new")),
    "a failed check answers check failed, never switched": Case(
        (OLD,), ("old", "old"), (STARTED_NEW, REFUSED, RemoveCopy("new")), answer(Outcome.CHECK_FAILED, "old", "Connection refused")
    ),
}


@pytest.mark.parametrize("case", MOVES.values(), ids=MOVES.keys())
def test_a_move_takes_this_step_next(case: Case) -> None:
    route = ProxyNotRunning() if case.step == StartProxy() else LaneRoute(*case.route)
    assert decide_move(LaneSeen(case.copies, route), TARGET, FRESH, case.done) == case.step


def retired(route_removed_to: str | None, copies: list[str]) -> Done[RetireResult]:
    outcome = RetireOutcome.RETIRED if route_removed_to or copies else RetireOutcome.UNCHANGED
    return Done(
        RetireResult(
            product="site", lane="testing", name=NAME, outcome=outcome, route_removed_to=route_removed_to, retired_copies=copies
        )
    )


RETIRES = {
    "with no proxy running, it is started first": Case((OLD,), (None, None), (), StartProxy()),
    "a route written and not loaded is waited for first": Case((OLD,), ("old", "new"), (), AwaitRoute(NAME, "new")),
    "the route goes first": Case((OLD,), ("old", "old"), (), RemoveRoute(NAME, "old")),
    "no copy stops until the proxy has loaded the route gone": Case(
        (OLD,), ("old", None), (RemoveRoute(NAME, "old"),), AwaitRoute(NAME, None)
    ),
    "copies retire once the route is gone": Case((OLD, STRAY), (None, None), (RemoveRoute(NAME, "old"),), Retire("old")),
    "a finished retirement answers what it removed": Case(
        (), (None, None), (RemoveRoute(NAME, "old"), Retire("old")), retired("old", ["old"])
    ),
    "a lane whose route was already gone answers no route removed": Case((), (None, None), (Retire("old"),), retired(None, ["old"])),
    "a retired lane is unchanged": Case((), (None, None), (), retired(None, [])),
}


@pytest.mark.parametrize("case", RETIRES.values(), ids=RETIRES.keys())
def test_a_retire_takes_this_step_next(case: Case) -> None:
    route = ProxyNotRunning() if case.step == StartProxy() else LaneRoute(*case.route)
    assert decide_retire(LaneSeen(case.copies, route), "site", "testing", case.done) == case.step


def test_no_step_of_any_case_routes_or_awaits_another_lanes_name() -> None:
    steps = [case.step for case in [*MOVES.values(), *RETIRES.values()]]
    assert {step.name for step in steps if isinstance(step, WriteRoute | RemoveRoute | AwaitRoute)} == {NAME}
