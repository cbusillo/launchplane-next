from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from launchplane import steps
from launchplane.act import Lanes
from launchplane.api import create_app
from launchplane.instance import LaneBuild
from launchplane.record import Kept, Record, Stands
from launchplane.steps import Action, Event
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS
from tests.pretend_host import WEB_ADDRESS, LaunchplaneKilled, PretendHost, PretendProxy, numbered_names
from tests.test_requests import MOVE, TO_B
from tests.test_worker import kept_of

TO_C = {"build": "site:c"}
A, B, C = "site:a", "site:b", "site:c"
CANCEL, NEWER, KILL = "a cancel", "a newer request", "a kill"
KILL_THEN_CANCEL, CANCEL_THEN_KILL = "a kill, then a cancel", "a cancel, then a kill"
INTERRUPTIONS = (CANCEL, NEWER, KILL, KILL_THEN_CANCEL, CANCEL_THEN_KILL)
CANCELS = (CANCEL, KILL_THEN_CANCEL, CANCEL_THEN_KILL)
TOO_LATE = "already renamed for this release"


# Where a move is held: before an action is taken, or after its effect and before the worker has its reply.
@dataclass(frozen=True)
class Pause:
    when: str
    action: type[Action] | None = None
    # Whether the routes file is renamed for the release by then, which alone decides who wins.
    renamed: bool = False
    proxy_held: bool = False


PAUSES = {
    "M0, waiting for its lane's turn": Pause("waiting"),
    "M0, taken up, before its first action": Pause("before", steps.StartInstance),
    "M1, candidate started": Pause("after", steps.StartInstance),
    "M2, before its check": Pause("before", steps.Check),
    "M2, check passed": Pause("after", steps.Check),
    "M3, before the rename": Pause("before", steps.Switch),
    "M4, renamed and its reply not yet had, proxy still on A": Pause("after", steps.Switch, renamed=True, proxy_held=True),
    "M4, renamed, waiting for a proxy still on A": Pause("before", steps.AwaitSwitch, renamed=True, proxy_held=True),
    "M5, proxy on B, old instance still up": Pause("before", steps.Retire, renamed=True),
    "M6, old instance gone": Pause("after", steps.Retire, renamed=True),
    "Z, answered": Pause("ended", renamed=True),
}


@dataclass
class Held:
    proxy: PretendProxy
    client: TestClient
    worker: Worker
    record: Record
    pause: Pause
    interruption: str
    forward: Callable[[Action], Event]
    asked: int = 0
    interrupted: bool = False
    visitors_got: list[str] = field(default_factory=list[str])
    cancel_answers: list[tuple[int, dict[str, Any]]] = field(default_factory=list[tuple[int, dict[str, Any]]])
    newer: int | None = None

    # What a visitor gets is the instance the proxy has loaded, and that instance is on the host for as long as it is.
    def look(self) -> None:
        loaded = self.proxy.loaded[WEB_ADDRESS].container
        assert loaded in self.proxy.host.instances, f"the proxy sends visitors to {loaded}, which is gone"
        build = self.proxy.host.instances[loaded].build
        assert build is not None
        if self.visitors_got[-1:] != [build]:
            self.visitors_got.append(build)

    def cancel(self) -> None:
        answer = self.client.post(f"/requests/{self.asked}/cancel")
        self.cancel_answers.append((answer.status_code, answer.json()))

    def interrupt(self) -> None:
        self.interrupted = True
        if self.pause.proxy_held:
            route = self.proxy.read().route_of(WEB_ADDRESS)
            assert (route.written != "a", route.sends_visitors_to) == (True, "a"), "held after the rename with the proxy on A"
        if self.interruption in (CANCEL, CANCEL_THEN_KILL):
            self.cancel()
        if self.interruption == NEWER:
            self.newer = self.client.post(MOVE, json=TO_C).json()["id"]
        if self.interruption in (KILL, KILL_THEN_CANCEL, CANCEL_THEN_KILL):
            raise LaunchplaneKilled
        self.proxy.holds_delivery = False

    def take(self, action: Action) -> Event:
        here = not self.interrupted and self.pause.action is not None and isinstance(action, self.pause.action)
        self.look()
        if here and self.pause.when == "before":
            self.interrupt()
        event = self.forward(action)
        self.look()
        if here and self.pause.when == "after":
            self.interrupt()
        return event

    # Launchplane is down from a kill until work is next looked for, and a cancel after a kill comes in that time.
    def work_until_killed_or_nothing_is_left(self) -> None:
        try:
            while self.worker.work_next():
                pass
        except LaunchplaneKilled:
            self.cancel_while_down()

    def cancel_while_down(self) -> None:
        if self.interruption == KILL_THEN_CANCEL:
            self.cancel()

    def interrupt_between_requests(self) -> None:
        try:
            self.interrupt()
        except LaunchplaneKilled:
            self.cancel_while_down()

    def run(self) -> None:
        self.proxy.holds_delivery = self.pause.proxy_held
        self.look()
        self.asked = self.client.post(MOVE, json=TO_B).json()["id"]
        if self.pause.when == "ended":
            self.work_until_killed_or_nothing_is_left()
        if self.pause.when in ("waiting", "ended"):
            self.interrupt_between_requests()
        self.work_until_killed_or_nothing_is_left()
        assert self.interrupted, "the move never came to its pause"
        self.proxy.holds_delivery = False
        self.work_until_killed_or_nothing_is_left()
        self.look()

    def builds_on_the_host(self) -> list[str | None]:
        return [instance.build for instance in self.proxy.host.instances.values()]

    def switches(self) -> int:
        return sum(action.startswith("route ") for action in self.proxy.host.actions)


def launchplane_serving_a(record: Record) -> tuple[PretendProxy, Lanes, Worker, TestClient]:
    proxy = PretendProxy(PretendHost())
    proxy.host.place(LaneBuild(product="site", lane="testing", build=A), "a")
    proxy.route(WEB_ADDRESS, "a")
    lanes = Lanes(proxy.host, proxy, name_fresh=numbered_names(), read_unfinished=record.unfinished)
    worker = Worker(record, lanes, NO_SECRETS)
    return proxy, lanes, worker, TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes.abandon))


def held(record: Record, pause: Pause, interruption: str) -> Held:
    proxy, lanes, worker, client = launchplane_serving_a(record)
    run = Held(proxy, client, worker, record, pause, interruption, lanes.take)
    lanes.take = run.take
    run.run()
    return run


def outcome_of(kept: Kept) -> str | None:
    return None if kept.answer is None else kept.answer.get("outcome")


BEFORE_THE_RENAME = [(name, pause) for name, pause in PAUSES.items() if not pause.renamed]
FROM_THE_RENAME = [(name, pause) for name, pause in PAUSES.items() if pause.renamed and pause.when != "ended"]


def cells(pauses: list[tuple[str, Pause]], interruptions: tuple[str, ...]) -> list[Any]:
    return [pytest.param(pause, each, id=f"{name}: {each}") for name, pause in pauses for each in interruptions]


@pytest.mark.parametrize(("pause", "interruption"), cells(BEFORE_THE_RENAME, CANCELS))
def test_a_cancel_before_the_rename_wins_and_visitors_only_ever_get_the_old_build(
    record: Record, pause: Pause, interruption: str
) -> None:
    run = held(record, pause, interruption)
    kept = kept_of(record, run.asked)
    again = run.client.post(f"/requests/{run.asked}/cancel")
    assert (kept.stands, kept.cancel_refused, run.visitors_got, run.builds_on_the_host(), run.switches()) == (
        Stands.CANCELLED,
        None,
        [A],
        [A],
        0,
    )
    assert ({status for status, _ in run.cancel_answers} <= {200, 202}, again.status_code) == (True, 200)


@pytest.mark.parametrize(("pause", "interruption"), cells(BEFORE_THE_RENAME, (NEWER,)))
def test_a_newer_request_before_the_rename_takes_the_lane_from_the_old_build_to_the_newest_and_never_to_the_older(
    record: Record, pause: Pause, interruption: str
) -> None:
    run = held(record, pause, interruption)
    assert run.newer is not None
    older, newer = kept_of(record, run.asked), kept_of(record, run.newer)
    assert (older.stands, older.cancel_asked_by, older.cancel_refused) == (Stands.CANCELLED, f"request {run.newer}", None)
    assert (newer.stands, outcome_of(newer), run.visitors_got, run.builds_on_the_host(), run.switches()) == (
        Stands.DONE,
        "switched",
        [A, C],
        [C],
        1,
    )


@pytest.mark.parametrize(("pause", "interruption"), cells(list(PAUSES.items()), (KILL,)))
def test_a_kill_with_no_cancel_finishes_the_release_as_asked_and_switches_once(
    record: Record, pause: Pause, interruption: str
) -> None:
    run = held(record, pause, interruption)
    kept = kept_of(record, run.asked)
    assert (kept.stands, outcome_of(kept), run.visitors_got, run.builds_on_the_host(), run.switches()) == (
        Stands.DONE,
        "switched",
        [A, B],
        [B],
        1,
    )


@pytest.mark.parametrize(("pause", "interruption"), cells(FROM_THE_RENAME, CANCELS))
def test_a_cancel_from_the_rename_on_is_told_too_late_and_the_release_is_finished_as_asked_once(
    record: Record, pause: Pause, interruption: str
) -> None:
    run = held(record, pause, interruption)
    kept = kept_of(record, run.asked)
    again = run.client.post(f"/requests/{run.asked}/cancel")
    assert (kept.stands, outcome_of(kept), run.visitors_got, run.builds_on_the_host(), run.switches()) == (
        Stands.DONE,
        "switched",
        [A, B],
        [B],
        1,
    )
    assert [(status, TOO_LATE in body["error"]) for status, body in run.cancel_answers] == [(409, True)]
    assert (again.status_code, TOO_LATE in again.json()["error"]) == (409, True)


@pytest.mark.parametrize(("pause", "interruption"), cells(FROM_THE_RENAME, (NEWER,)))
def test_a_newer_request_from_the_rename_on_waits_for_the_older_to_finish_and_then_runs(
    record: Record, pause: Pause, interruption: str
) -> None:
    run = held(record, pause, interruption)
    assert run.newer is not None
    older, newer = kept_of(record, run.asked), kept_of(record, run.newer)
    assert older.cancel_refused is not None and TOO_LATE in older.cancel_refused
    assert (older.stands, outcome_of(older), older.cancel_asked_by) == (Stands.DONE, "switched", f"request {run.newer}")
    assert (newer.stands, outcome_of(newer), run.visitors_got, run.builds_on_the_host(), run.switches()) == (
        Stands.DONE,
        "switched",
        [A, B, C],
        [C],
        2,
    )


@pytest.mark.parametrize("interruption", INTERRUPTIONS)
def test_a_result_already_recorded_stands_whatever_comes_after_it(record: Record, interruption: str) -> None:
    run = held(record, PAUSES["Z, answered"], interruption)
    kept = kept_of(record, run.asked)
    assert (kept.stands, outcome_of(kept), kept.cancel_refused, [each.ended for each in kept.attempts]) == (
        Stands.DONE,
        "switched",
        None,
        ["done"],
    )
    assert [(status, "ended as done" in body["error"]) for status, body in run.cancel_answers] == (
        [(409, True)] if interruption in CANCELS else []
    )
    assert run.visitors_got == ([A, B, C] if interruption == NEWER else [A, B])


def test_a_move_to_the_build_already_serving_that_is_cancelled_before_its_result_is_saved_ends_as_cancelled(
    record: Record,
) -> None:
    proxy, _, worker, client = launchplane_serving_a(record)
    asked = client.post(MOVE, json={"build": A}).json()["id"]
    end, cancels = record.end, list[int]()

    def cancel_just_before_the_result_is_saved(
        attempt: int, stands: Stands, answer: dict[str, Any] | None, error: str | None
    ) -> bool:
        if not cancels:
            cancels.append(client.post(f"/requests/{asked}/cancel").status_code)
        return end(attempt, stands, answer, error)

    record.end = cancel_just_before_the_result_is_saved
    assert worker.work_next() is True
    kept = kept_of(record, asked)
    assert (kept.stands, cancels, client.post(f"/requests/{asked}/cancel").status_code) == (Stands.CANCELLED, [202], 200)
    assert (proxy.host.actions, list(proxy.host.instances)) == ([], ["a"])
