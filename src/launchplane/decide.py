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


@dataclass(frozen=True)
class WriteRoute:
    name: str
    copy: str


@dataclass(frozen=True)
class RemoveRoute:
    name: str
    copy: str


@dataclass(frozen=True)
class Retire:
    copy: str


@dataclass(frozen=True)
class RemoveCopy:
    copy: str


@dataclass(frozen=True)
class CheckRefused:
    copy: str
    report: str


@dataclass(frozen=True)
class Done[Answer]:
    answer: Answer


type Action = StartProxy | AwaitRoute | StartCopy | Check | WriteRoute | RemoveRoute | Retire | RemoveCopy
type Event = Action | CheckRefused


def lane_name(product: str, lane: str) -> str:
    return f"{lane}.{product}.{LANE_DOMAIN}"


def is_running_build(copy: RunningCopy | None, build: str) -> bool:
    return copy is not None and copy.state == CopyState.RUNNING and copy.build == build


def candidate_for(copies: dict[str, RunningCopy], serving: str | None, build: str, done: tuple[Event, ...]) -> str | None:
    started_here = [event.name for event in done if isinstance(event, StartCopy)]
    own = next((name for name in reversed(started_here) if name in copies and name != serving), None)
    return own or next((name for name in sorted(copies) if name != serving and is_running_build(copies[name], build)), None)


def move_outcome(done: tuple[Event, ...]) -> Outcome:
    if any(isinstance(event, WriteRoute) for event in done):
        return Outcome.SWITCHED
    if any(isinstance(event, Retire) for event in done):
        return Outcome.CLEANED_UP
    return Outcome.UNCHANGED


def after_refusal(refused: CheckRefused, copies: dict[str, RunningCopy], answer: MoveResult) -> Action | Done[MoveResult]:
    if refused.copy in copies:
        return RemoveCopy(refused.copy)
    return Done(answer.model_copy(update={"outcome": Outcome.CHECK_FAILED, "error": refused.report}))


def check_then_route(name: str, candidate: RunningCopy, done: tuple[Event, ...]) -> Action:
    if Check(candidate.name) in done and candidate.state == CopyState.RUNNING:
        return WriteRoute(name, candidate.name)
    return Check(candidate.name)


def decide_move(seen: LaneSeen, target: LaneCopy, fresh_copy_name: str, done: tuple[Event, ...]) -> Action | Done[MoveResult]:
    if isinstance(seen.route, ProxyNotRunning):
        return StartProxy()
    name = lane_name(target.product, target.lane)
    if seen.route.loaded != seen.route.written:
        return AwaitRoute(name, seen.route.written)
    serving = seen.route.written
    copies = {copy.name: copy for copy in seen.copies}
    answer = MoveResult(
        product=target.product, lane=target.lane, build=target.build, name=name, outcome=move_outcome(done), serving=serving
    )
    refused = next((event for event in done if isinstance(event, CheckRefused)), None)
    if refused is not None:
        return after_refusal(refused, copies, answer)
    if is_running_build(None if serving is None else copies.get(serving), target.build):
        others = sorted(copies.keys() - {serving})
        return Retire(others[0]) if others else Done(answer)
    candidate = candidate_for(copies, serving, target.build, done)
    strays = sorted(copies.keys() - {serving, candidate})
    if strays:
        return Retire(strays[0])
    if candidate is None:
        return StartCopy(target, fresh_copy_name)
    return check_then_route(name, copies[candidate], done)


def decide_retire(seen: LaneSeen, product: str, lane: str, done: tuple[Event, ...]) -> Action | Done[RetireResult]:
    if isinstance(seen.route, ProxyNotRunning):
        return StartProxy()
    name = lane_name(product, lane)
    if seen.route.loaded != seen.route.written:
        return AwaitRoute(name, seen.route.written)
    if seen.route.written is not None:
        return RemoveRoute(name, seen.route.written)
    if seen.copies:
        return Retire(min(copy.name for copy in seen.copies))
    route_removed_to = next((event.copy for event in done if isinstance(event, RemoveRoute)), None)
    retired_copies = [event.copy for event in done if isinstance(event, Retire)]
    changed = route_removed_to is not None or bool(retired_copies)
    return Done(
        RetireResult(
            product=product,
            lane=lane,
            name=name,
            outcome=RetireOutcome.RETIRED if changed else RetireOutcome.UNCHANGED,
            route_removed_to=route_removed_to,
            retired_copies=retired_copies,
        )
    )
