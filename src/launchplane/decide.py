from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from launchplane.running_copy import CopyState, LaneCopy, RunningCopy

LANE_DOMAIN = "test"


class Outcome(StrEnum):
    SWITCHED = "switched"
    UNCHANGED = "unchanged"
    CLEANED_UP = "cleaned up"
    CHECK_FAILED = "check failed"


class MoveResult(BaseModel):
    product: str
    lane: str
    build: str
    name: str
    outcome: Outcome
    serving: str | None
    error: str | None = None


class RetireOutcome(StrEnum):
    RETIRED = "retired"
    UNCHANGED = "unchanged"


class RetireResult(BaseModel):
    product: str
    lane: str
    name: str
    outcome: RetireOutcome
    route_removed_to: str | None
    retired_copies: list[str]


@dataclass(frozen=True)
class ProxyNotRunning:
    pass


@dataclass(frozen=True)
class LaneRoute:
    loaded: str | None
    written: str | None


@dataclass(frozen=True)
class LaneSeen:
    copies: tuple[RunningCopy, ...]
    route: ProxyNotRunning | LaneRoute


@dataclass(frozen=True)
class StartProxy:
    pass


@dataclass(frozen=True)
class AwaitRoute:
    name: str
    copy: str | None


@dataclass(frozen=True)
class StartCopy:
    copy: LaneCopy
    name: str


@dataclass(frozen=True)
class Check:
    copy: str
    if_failed: MoveResult


@dataclass(frozen=True)
class WriteRoute:
    name: str
    copy: str | None


@dataclass(frozen=True)
class Retire:
    copy: str


type Action = StartProxy | AwaitRoute | StartCopy | Check | WriteRoute | Retire


@dataclass(frozen=True)
class ReadAgain:
    actions: tuple[Action, ...]


@dataclass(frozen=True)
class Plan[Answer]:
    actions: tuple[Action, ...]
    answer: Answer


START_PROXY_THEN_READ_AGAIN = ReadAgain((StartProxy(),))


def lane_name(product: str, lane: str) -> str:
    return f"{lane}.{product}.{LANE_DOMAIN}"


def await_written_route(route: LaneRoute, name: str) -> tuple[Action, ...]:
    return () if route.loaded == route.written else (AwaitRoute(name, route.written),)


def is_running_build(copy: RunningCopy | None, build: str) -> bool:
    return copy is not None and copy.state == CopyState.RUNNING and copy.build == build


def copy_left_waiting(copies: dict[str, RunningCopy], serving: str | None, build: str) -> str | None:
    return next((name for name in sorted(copies) if name != serving and is_running_build(copies[name], build)), None)


def retire_all_but(copies: dict[str, RunningCopy], keep: set[str | None]) -> tuple[Action, ...]:
    return tuple(Retire(name) for name in sorted(copies) if name not in keep)


def decide_move(seen: LaneSeen, target: LaneCopy, fresh_copy_name: str) -> ReadAgain | Plan[MoveResult]:
    if isinstance(seen.route, ProxyNotRunning):
        return START_PROXY_THEN_READ_AGAIN
    name = lane_name(target.product, target.lane)
    serving = seen.route.written
    copies = {copy.name: copy for copy in seen.copies}
    serving_copy = None if serving is None else copies.get(serving)
    settle = await_written_route(seen.route, name)
    unchanged = MoveResult(
        product=target.product, lane=target.lane, build=target.build, name=name, outcome=Outcome.UNCHANGED, serving=serving
    )

    if is_running_build(serving_copy, target.build):
        leftovers = retire_all_but(copies, keep={serving})
        outcome = Outcome.CLEANED_UP if leftovers else Outcome.UNCHANGED
        return Plan((*settle, *leftovers), unchanged.model_copy(update={"outcome": outcome}))

    left_waiting = copy_left_waiting(copies, serving, target.build)
    candidate = left_waiting or fresh_copy_name
    start = () if left_waiting else (StartCopy(target, fresh_copy_name),)
    retire_old = () if serving_copy is None else (Retire(serving_copy.name),)
    actions = (
        *settle,
        *retire_all_but(copies, keep={serving, left_waiting}),
        *start,
        Check(candidate, if_failed=unchanged.model_copy(update={"outcome": Outcome.CHECK_FAILED})),
        WriteRoute(name, candidate),
        AwaitRoute(name, candidate),
        *retire_old,
    )
    return Plan(actions, unchanged.model_copy(update={"outcome": Outcome.SWITCHED, "serving": candidate}))


def decide_retire(seen: LaneSeen, product: str, lane: str) -> ReadAgain | Plan[RetireResult]:
    if isinstance(seen.route, ProxyNotRunning):
        return START_PROXY_THEN_READ_AGAIN
    name = lane_name(product, lane)
    route_to = seen.route.written
    unroute = () if route_to is None else (WriteRoute(name, None), AwaitRoute(name, None))
    copies = sorted(copy.name for copy in seen.copies)
    changed = route_to is not None or bool(copies)
    answer = RetireResult(
        product=product,
        lane=lane,
        name=name,
        outcome=RetireOutcome.RETIRED if changed else RetireOutcome.UNCHANGED,
        route_removed_to=route_to,
        retired_copies=copies,
    )
    return Plan((*await_written_route(seen.route, name), *unroute, *(Retire(copy) for copy in copies)), answer)
