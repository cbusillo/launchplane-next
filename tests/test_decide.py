from dataclasses import dataclass

import pytest

from launchplane.decide import (
    Action,
    AwaitRoute,
    Check,
    LaneRoute,
    LaneSeen,
    Outcome,
    ProxyNotRunning,
    ReadAgain,
    Retire,
    RetireOutcome,
    StartCopy,
    StartProxy,
    WriteRoute,
    decide_move,
    decide_retire,
)
from launchplane.running_copy import CopyState, LaneCopy, RunningCopy

NAME = "testing.site.test"
TARGET = LaneCopy(product="site", lane="testing", build="b")
FRESH = "site-testing-new"


def copy(name: str, build: str, state: CopyState = CopyState.RUNNING) -> RunningCopy:
    return RunningCopy(product="site", lane="testing", build=build, name=name, state=state)


def step(action: Action) -> str:
    match action:
        case StartProxy():
            return "start proxy"
        case AwaitRoute(copy=copy_name):
            return f"await {copy_name}"
        case StartCopy(name=copy_name):
            return f"start {copy_name}"
        case Check(copy=copy_name):
            return f"check {copy_name}"
        case WriteRoute(copy=copy_name):
            return f"route {copy_name}"
        case Retire(copy=copy_name):
            return f"retire {copy_name}"


def steps(actions: tuple[Action, ...]) -> list[str]:
    return [step(action) for action in actions]


@dataclass(frozen=True)
class Case:
    copies: tuple[RunningCopy, ...]
    loaded: str | None
    written: str | None
    steps: list[str]
    outcome: Outcome
    serving: str | None


OLD = copy("old", "a")
SAME = copy("same", "b")
WAITING = copy("waiting", "b")
STOPPED_SAME = copy("same", "b", CopyState.STOPPED)
STRAY = copy("stray", "c")
SWITCH_FROM_OLD = [f"start {FRESH}", f"check {FRESH}", f"route {FRESH}", f"await {FRESH}", "retire old"]

MOVES = {
    "a first move starts, checks, then routes": Case(
        (), None, None, [f"start {FRESH}", f"check {FRESH}", f"route {FRESH}", f"await {FRESH}"], Outcome.SWITCHED, FRESH
    ),
    "the old copy retires only after the route to the new one has loaded": Case(
        (OLD,), "old", "old", SWITCH_FROM_OLD, Outcome.SWITCHED, FRESH
    ),
    "asking for the serving build again changes nothing": Case((SAME,), "same", "same", [], Outcome.UNCHANGED, "same"),
    "a copy left by an interrupted move is retired when the serving build is asked for": Case(
        (SAME, STRAY), "same", "same", ["retire stray"], Outcome.CLEANED_UP, "same"
    ),
    "a running copy left before the switch is checked and routed, not started again": Case(
        (OLD, WAITING), "old", "old", ["check waiting", "route waiting", "await waiting", "retire old"], Outcome.SWITCHED, "waiting"
    ),
    "a stopped copy left before the switch is retired and replaced": Case(
        (OLD, copy("left", "b", CopyState.STOPPED)), "old", "old", ["retire left", *SWITCH_FROM_OLD], Outcome.SWITCHED, FRESH
    ),
    "a stopped serving copy is replaced by a new copy, checked before any request": Case(
        (STOPPED_SAME,),
        "same",
        "same",
        [f"start {FRESH}", f"check {FRESH}", f"route {FRESH}", f"await {FRESH}", "retire same"],
        Outcome.SWITCHED,
        FRESH,
    ),
    "a route to a copy that is gone is not taken as serving": Case(
        (), "gone", "gone", [f"start {FRESH}", f"check {FRESH}", f"route {FRESH}", f"await {FRESH}"], Outcome.SWITCHED, FRESH
    ),
    "a route written but not loaded is waited for before anything else": Case(
        (OLD, SAME), "old", "same", ["await same", "retire old"], Outcome.CLEANED_UP, "same"
    ),
    "a route written to the old copy but not loaded is waited for, then the move goes on": Case(
        (OLD, WAITING),
        "waiting",
        "old",
        ["await old", "check waiting", "route waiting", "await waiting", "retire old"],
        Outcome.SWITCHED,
        "waiting",
    ),
}


@pytest.mark.parametrize("case", MOVES.values(), ids=MOVES.keys())
def test_a_move_decides_these_actions_in_this_order(case: Case) -> None:
    plan = decide_move(LaneSeen(case.copies, LaneRoute(loaded=case.loaded, written=case.written)), TARGET, FRESH)
    assert not isinstance(plan, ReadAgain)
    assert steps(plan.actions) == case.steps
    assert (plan.answer.outcome, plan.answer.serving) == (case.outcome, case.serving)


def test_a_failed_check_answers_check_failed_with_the_old_copy_still_serving() -> None:
    plan = decide_move(LaneSeen((OLD,), LaneRoute(loaded="old", written="old")), TARGET, FRESH)
    assert not isinstance(plan, ReadAgain)
    (check,) = (action for action in plan.actions if isinstance(action, Check))
    assert (check.if_failed.outcome, check.if_failed.serving) == (Outcome.CHECK_FAILED, "old")


def test_a_check_is_never_made_of_the_copy_the_written_route_names() -> None:
    for case in MOVES.values():
        plan = decide_move(LaneSeen(case.copies, LaneRoute(loaded=case.loaded, written=case.written)), TARGET, FRESH)
        assert not isinstance(plan, ReadAgain)
        checked = {action.copy for action in plan.actions if isinstance(action, Check)}
        assert case.written not in checked


@dataclass(frozen=True)
class RetireCase:
    copies: tuple[RunningCopy, ...]
    loaded: str | None
    written: str | None
    steps: list[str]
    outcome: RetireOutcome
    route_removed_to: str | None


RETIRES = {
    "the route is removed and loaded gone before any copy stops": RetireCase(
        (OLD, STRAY), "old", "old", ["route None", "await None", "retire old", "retire stray"], RetireOutcome.RETIRED, "old"
    ),
    "a retired lane is unchanged": RetireCase((), None, None, [], RetireOutcome.UNCHANGED, None),
    "a lane whose route is gone retires its copies": RetireCase((OLD,), None, None, ["retire old"], RetireOutcome.RETIRED, None),
    "a route removed but not loaded is waited for before any copy stops": RetireCase(
        (OLD,), "old", None, ["await None", "retire old"], RetireOutcome.RETIRED, None
    ),
    "a route to a copy that is gone is removed": RetireCase(
        (), "gone", "gone", ["route None", "await None"], RetireOutcome.RETIRED, "gone"
    ),
}


@pytest.mark.parametrize("case", RETIRES.values(), ids=RETIRES.keys())
def test_a_retire_decides_these_actions_in_this_order(case: RetireCase) -> None:
    plan = decide_retire(LaneSeen(case.copies, LaneRoute(loaded=case.loaded, written=case.written)), "site", "testing")
    assert not isinstance(plan, ReadAgain)
    assert steps(plan.actions) == case.steps
    assert (plan.answer.outcome, plan.answer.route_removed_to) == (case.outcome, case.route_removed_to)
    assert plan.answer.retired_copies == sorted(copy.name for copy in case.copies)


def test_with_no_proxy_running_the_first_move_starts_it_and_reads_again() -> None:
    assert decide_move(LaneSeen((), ProxyNotRunning()), TARGET, FRESH) == ReadAgain((StartProxy(),))


def test_with_no_proxy_running_a_retire_starts_it_and_reads_again() -> None:
    assert decide_retire(LaneSeen((OLD,), ProxyNotRunning()), "site", "testing") == ReadAgain((StartProxy(),))


def test_a_lane_only_ever_routes_its_own_name() -> None:
    plans = [decide_move(LaneSeen(case.copies, LaneRoute(case.loaded, case.written)), TARGET, FRESH) for case in MOVES.values()]
    plans += [
        decide_retire(LaneSeen(case.copies, LaneRoute(case.loaded, case.written)), "site", "testing") for case in RETIRES.values()
    ]
    names = {action.name for plan in plans for action in plan.actions if isinstance(action, WriteRoute | AwaitRoute)}
    assert names == {NAME}
