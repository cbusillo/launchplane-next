import http.client
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import pytest

from launchplane.proxy import GATE, PROXY_NAME, abandoned_mark_of, route_file_of, stamp_line
from tests.bed import (
    SCRATCH_ADDRESS,
    RequestLoop,
    UnexpectedAnswer,
    ask,
    hold,
    holds,
    kill_held_launchplane,
    launchplane_held,
    let_go,
    move_lane,
    needs_test_bed,
    on_scratch,
    request_ended,
    request_loop,
    retire,
    wait_for,
    wait_until_stopped_at,
)
from tests.test_bed_data_set import DESCRIPTION, build_pretend_product, place_description

pytestmark = needs_test_bed

CANCEL, NEWER, KILL = "a cancel", "a newer request", "a kill"
KILL_THEN_CANCEL, CANCEL_THEN_KILL = "a kill, then a cancel", "a cancel, then a kill"
INTERRUPTIONS = (CANCEL, NEWER, KILL, KILL_THEN_CANCEL, CANCEL_THEN_KILL)
CANCELS = (CANCEL, KILL_THEN_CANCEL, CANCEL_THEN_KILL)
TOO_LATE = "already renamed for this release"
THE_RENAME = "proxy.pass_gate?only=mv%20"
TURN, TAKE_UP, GATE_ANSWER = "before.record.turn", "before.record.take_up", "after.proxy.abandon"
UNTOUCHED = "untouched"
NOTHING = "no lane"


# One lane under test: its three builds, and what a visitor is answered by each.
@dataclass(frozen=True)
class Lane:
    product: str
    builds: tuple[str, str, str]
    answers: tuple[str, str, str]
    lane: str = "held"
    has_data: bool = False

    @property
    def web_address(self) -> str:
        return f"{self.lane}.{self.product}.test"

    def answer_of(self, build: str) -> str:
        return self.answers[self.builds.index(build)]

    def beside(self, lane: str) -> Lane:
        return Lane(self.product, self.builds, self.answers, lane, self.has_data)


SITE = Lane(
    "test-bed-gate",
    ("nginx:1.30.6-alpine", "nginx:1.31.8-alpine", "caddy:2.11.9-alpine"),
    ("nginx/1.30.6", "nginx/1.31.8", "Caddy"),
)
LEDGER = Lane(
    "test-bed-gate-ledger",
    ("launchplane-test-ledger:a", "launchplane-test-ledger:b", "launchplane-test-ledger:c"),
    ("a", "b", "c"),
    has_data=True,
)


# Where the real Launchplane is held: at a call it makes on the host, the proxy or its record.
@dataclass(frozen=True)
class Pause:
    hold: str
    # Whether the routes file is renamed for the release by then, which alone decides who wins.
    renamed: bool = False
    proxy_held: bool = False
    # The proxy is let load the rename while its reply is held, so visitors are on B before the worker knows.
    proxy_on_b: bool = False
    saved: bool = False
    # The lane has nothing on the host when the move is asked.
    from_nothing: bool = False
    # The one interruption this pause is proven with on the bed. A pause with none stands for its row and has all five.
    # The whole table runs against the pretend host in tests/test_interruptions.py.
    once: str | None = None


SITE_PAUSES = {
    "M0, waiting for its lane's turn": Pause(TURN),
    "M0, taken up, before its first action": Pause("before.host.start_instance", once=CANCEL),
    "M1, candidate created and not started": Pause("after.host.create", once=KILL),
    "M1, candidate started and not yet checked": Pause("after.host.start_instance"),
    "M2, its check under way": Pause("before.proxy.request_instance", once=NEWER),
    "M2, check passed": Pause("after.proxy.check_instance"),
    "M3, before the gate": Pause("before.proxy.switch", once=KILL_THEN_CANCEL),
    "M3, staged, the rename not yet sent": Pause(f"before.{THE_RENAME}"),
    "M4, renamed, its reply not yet had, proxy still on A": Pause(f"after.{THE_RENAME}", renamed=True, proxy_held=True),
    "M4, renamed, waiting for a proxy still on A": Pause(
        "before.proxy.await_route", renamed=True, proxy_held=True, once=CANCEL_THEN_KILL
    ),
    "M5, proxy on B, the reply to the rename not yet had": Pause(f"after.{THE_RENAME}", renamed=True, proxy_on_b=True, once=CANCEL),
    "M5, proxy on B, old instance still up": Pause("before.proxy.discard", renamed=True),
    "M6, old instance gone": Pause("after.host.retire_instance", renamed=True),
    "Z, before its result is saved": Pause("before.record.end", renamed=True),
    "Z, result saved": Pause("after.record.end", renamed=True, saved=True),
}
LEDGER_PAUSES = {
    "M1, the network of a new data set made": Pause("after.host.create_data_set_network", from_nothing=True, once=CANCEL),
    "M1, the volume of a new data set made": Pause("after.host.create_data_set_volume", from_nothing=True, once=KILL),
    "M1, the database server of a new data set made": Pause("after.host.create_database", from_nothing=True, once=NEWER),
    "M5, before the old background service is retired": Pause("before.host.retire_instance", renamed=True, once=KILL_THEN_CANCEL),
    "M5, the new background service started": Pause("after.host.start_background_service", renamed=True, once=CANCEL_THEN_KILL),
    "M5, the new background service being watched": Pause("before.host.await_background_service", renamed=True, once=KILL),
    "M6, before the logins no container holds are removed": Pause("before.host.remove_unheld_logins", renamed=True, once=CANCEL),
}


# Looked up once: a lookup that fails on the machine that probes is not an answer of the lane.
@cache
def scratch_ip() -> str:
    return socket.gethostbyname(SCRATCH_ADDRESS)


def answering(lane: Lane) -> str:
    connection = http.client.HTTPConnection(scratch_ip(), 80, timeout=5)
    try:
        connection.request("GET", "/", headers={"Host": lane.web_address})
        response = connection.getresponse()
        body = response.read().decode()
        if response.status == 404 and "page not found" in body:
            return NOTHING
        if response.status != 200:
            raise UnexpectedAnswer(f"status {response.status}")
        return body.split()[-1] if lane.has_data else response.getheader("Server", "")
    finally:
        connection.close()


def running() -> dict[str, Any]:
    status, body = ask("/running")
    assert status == 200, body
    return body


def instances_of(lane: Lane) -> list[dict[str, Any]]:
    return [each for each in running()["instances"] if (each["product"], each["lane"]) == (lane.product, lane.lane)]


def builds_of(lane: Lane) -> list[str]:
    return [each["build"] for each in instances_of(lane)]


def data_sets_of(lane: Lane) -> list[str]:
    return [each["data_set"] for each in running()["data_sets"] if (each["product"], each["lane"]) == (lane.product, lane.lane)]


def route_file_stamp(lane: Lane) -> str:
    return on_scratch(f"docker exec {PROXY_NAME} head -n 1 {route_file_of(lane.web_address)}").strip()


def retire_with_its_data(lane: Lane) -> None:
    for data_set in data_sets_of(lane):
        assert retire(lane.product, lane.lane, data_set)[0] == 200
    assert retire(lane.product, lane.lane)[0] == 200


def write_row(lane: Lane, text: str) -> None:
    connection = http.client.HTTPConnection(scratch_ip(), 80, timeout=5)
    connection.request("POST", "/rows", body=text, headers={"Host": lane.web_address})
    assert connection.getresponse().status == 201
    connection.close()


def rows_of(lane: Lane) -> str:
    connection = http.client.HTTPConnection(scratch_ip(), 80, timeout=5)
    connection.request("GET", "/rows", headers={"Host": lane.web_address})
    rows = connection.getresponse().read().decode()
    connection.close()
    return rows


@dataclass
class Held:
    lane: Lane
    pause: Pause
    interruption: str
    asked: dict[str, Any] = field(default_factory=dict[str, Any])
    newer: int | None = None
    cancel_answers: list[tuple[int, dict[str, Any]]] = field(default_factory=list[tuple[int, dict[str, Any]]])
    cancel_cut_short: list[OSError] = field(default_factory=list[OSError])
    ended: dict[str, Any] = field(default_factory=dict[str, Any])
    newer_ended: dict[str, Any] = field(default_factory=dict[str, Any])
    visitors_got: list[str] = field(default_factory=list[str])
    visitor_failures: list[str] = field(default_factory=list[str])
    untouched_got: list[str] = field(default_factory=list[str])
    untouched_failures: list[str] = field(default_factory=list[str])

    def cancel(self, wait: int = 0) -> None:
        try:
            self.cancel_answers.append(ask(f"/requests/{self.asked['id']}/cancel?wait={wait}", {}, timeout=wait + 30))
        except OSError as error:
            self.cancel_cut_short.append(error)

    # A cancel has no winner until it reaches the gate, so the release is held until the gate's mark is on the host.
    def cancel_is_answered_or_has_won_at_the_gate(self) -> bool:
        mark = abandoned_mark_of(self.lane.web_address, self.asked["generation"])
        return bool(self.cancel_answers) or bool(on_scratch(f"docker exec {PROXY_NAME} sh -c 'test -e {mark} && echo won || true'"))

    # What the host shows at the pause: the routes file names this release, and the proxy still sends visitors to A.
    def look_while_the_proxy_is_held(self, loop: RequestLoop) -> None:
        assert route_file_stamp(self.lane) == stamp_line(self.asked["generation"])
        [receiving] = [each for each in instances_of(self.lane) if each["receives_visitors_for"]]
        assert (receiving["build"], loop.answers[-1]) == (self.lane.builds[0], self.lane.answers[0])

    def a_cancel_waiting_for_the_lane_to_be_put_back(self) -> None:
        caller = threading.Thread(target=self.cancel, args=(90,))
        caller.start()
        if self.pause.renamed:
            caller.join(timeout=30)
            assert not caller.is_alive(), "a cancel that is too late is told so while the release is still held"
        else:
            wait_for(self.cancel_is_answered_or_has_won_at_the_gate, seconds=30)
        let_go()
        caller.join(timeout=120)

    def a_kill_then_a_cancel_before_any_worker_recovers(self) -> None:
        recovery = TURN if self.pause.hold == TURN else TAKE_UP
        hold(recovery)
        kill_held_launchplane()
        if self.pause.hold != recovery:
            let_go(self.pause.hold.split("?")[0])
        if not self.pause.saved:
            wait_until_stopped_at(recovery)
        self.cancel()
        let_go()

    def a_cancel_then_a_kill_before_the_cancel_is_answered(self) -> None:
        hold(GATE_ANSWER)
        caller = threading.Thread(target=self.cancel, args=(90,))
        caller.start()

        def the_cancel_is_answered_or_held_after_the_gate() -> bool:
            return not caller.is_alive() or GATE_ANSWER in holds()["stopped_at"]

        wait_for(the_cancel_is_answered_or_held_after_the_gate, seconds=30)
        kill_held_launchplane()
        let_go()
        caller.join(timeout=120)

    def interrupt(self) -> None:
        if self.interruption == CANCEL:
            self.a_cancel_waiting_for_the_lane_to_be_put_back()
        elif self.interruption == NEWER:
            status, newer = ask(f"/lanes/{self.lane.product}/{self.lane.lane}/move", {"build": self.lane.builds[2]})
            assert status == 202, newer
            self.newer = newer["id"]
            let_go()
        elif self.interruption == KILL:
            kill_held_launchplane()
            let_go()
        elif self.interruption == KILL_THEN_CANCEL:
            self.a_kill_then_a_cancel_before_any_worker_recovers()
        else:
            self.a_cancel_then_a_kill_before_the_cancel_is_answered()

    def run(self) -> None:
        untouched = self.lane.beside(UNTOUCHED)
        if self.pause.proxy_held:
            hold("proxy-delivery")
        hold(self.pause.hold)
        with request_loop(lambda: answering(self.lane)) as loop, request_loop(lambda: answering(untouched)) as beside:
            status, self.asked = ask(f"/lanes/{self.lane.product}/{self.lane.lane}/move", {"build": self.lane.builds[1]})
            assert status == 202, self.asked
            wait_until_stopped_at(self.pause.hold, seconds=120)
            if self.pause.proxy_held:
                self.look_while_the_proxy_is_held(loop)
            if self.pause.proxy_on_b:
                wait_for(lambda: loop.answers[-1] == self.lane.answers[1], seconds=30)
            self.interrupt()
            self.ended = request_ended(self.asked["id"])
            if self.newer is not None:
                self.newer_ended = request_ended(self.newer)
        self.visitors_got, self.visitor_failures = loop.answers_in_order(), loop.failures
        self.untouched_got, self.untouched_failures = beside.answers_in_order(), beside.failures

    def cancelled_again(self) -> tuple[int, dict[str, Any]]:
        return ask(f"/requests/{self.asked['id']}/cancel", {})

    def outcome(self) -> tuple[str, str | None]:
        answer: dict[str, str] = self.ended["answer"] or {}
        return self.ended["stands"], answer.get("outcome")

    def got(self, *builds: str) -> list[str]:
        return [self.lane.answer_of(build) for build in builds]


@pytest.fixture(scope="module", autouse=True)
def held_bed() -> Iterator[None]:
    with launchplane_held():
        for lane in (SITE, LEDGER):
            retire_with_its_data(lane)
            retire_with_its_data(lane.beside(UNTOUCHED))
        build_pretend_product(LEDGER.builds[2], "c")
        place_description(LEDGER.product, DESCRIPTION)
        yield
        let_go()
        for lane in (SITE, LEDGER):
            retire_with_its_data(lane)
            retire_with_its_data(lane.beside(UNTOUCHED))


def held(lane: Lane, pause: Pause, interruption: str) -> Held:
    let_go()
    untouched = lane.beside(UNTOUCHED)
    if pause.from_nothing:
        retire_with_its_data(lane)
    else:
        assert move_lane(lane.product, lane.lane, lane.builds[0])[0] == 200
    assert move_lane(untouched.product, untouched.lane, untouched.builds[0])[0] == 200
    [beside_before] = instances_of(untouched)
    if lane.has_data and not pause.from_nothing:
        write_row(lane, "written before the release")
    run = Held(lane, pause, interruption)
    try:
        run.run()
    finally:
        let_go()
    assert (run.visitor_failures, run.untouched_failures, run.untouched_got) == ([], [], run.got(lane.builds[0]))
    assert instances_of(untouched) == [beside_before], "the lane beside was touched"
    return run


def cells(pauses: dict[str, Pause], interruptions: tuple[str, ...], lane: Lane, *, renamed: bool, saved: bool = False) -> list[Any]:
    return [
        pytest.param(lane, pause, each, id=f"{'ledger: ' if lane.has_data else ''}{name}: {each}")
        for name, pause in pauses.items()
        for each in interruptions
        if (pause.renamed, pause.saved) == (renamed, saved) and pause.once in (None, each)
    ]


def every(interruptions: tuple[str, ...], *, renamed: bool, saved: bool = False) -> list[Any]:
    return [
        *cells(SITE_PAUSES, interruptions, SITE, renamed=renamed, saved=saved),
        *cells(LEDGER_PAUSES, interruptions, LEDGER, renamed=renamed, saved=saved),
    ]


def as_it_began(run: Held) -> tuple[list[str], list[str]]:
    lane = run.lane
    return ([], [NOTHING]) if run.pause.from_nothing else ([lane.builds[0]], run.got(lane.builds[0]))


@pytest.mark.parametrize(("lane", "pause", "interruption"), every(CANCELS, renamed=False))
def test_a_cancel_before_the_rename_wins_visitors_get_the_old_build_throughout_and_data_is_kept(
    lane: Lane, pause: Pause, interruption: str
) -> None:
    run = held(lane, pause, interruption)
    builds, visitors_got = as_it_began(run)
    assert (run.outcome(), run.ended["cancel_refused"], builds_of(lane), run.visitors_got) == (
        ("cancelled", None),
        None,
        builds,
        visitors_got,
    )
    assert ({status for status, _ in run.cancel_answers} <= {200, 202}, run.cancelled_again()[0]) == (True, 200)
    if interruption == CANCEL:
        assert [status for status, _ in run.cancel_answers] == [200], "a cancel that waits is answered once the lane is put back"
    if lane.has_data:
        assert sorted(run.ended["answer"]["data_sets_kept"]) == sorted(data_sets_of(lane)), "the data set it made is kept and named"


@pytest.mark.parametrize(("lane", "pause", "interruption"), every((NEWER,), renamed=False))
def test_a_newer_request_before_the_rename_takes_the_lane_to_the_newest_build_and_never_to_the_older(
    lane: Lane, pause: Pause, interruption: str
) -> None:
    run = held(lane, pause, interruption)
    began = [] if pause.from_nothing else [lane.builds[0]]
    assert (run.outcome(), run.ended["cancel_asked_by"]) == (("cancelled", None), f"request {run.newer}")
    assert (run.newer_ended["stands"], run.newer_ended["answer"]["outcome"]) == ("done", "switched")
    assert (builds_of(lane), run.visitors_got) == ([lane.builds[2]], [*as_it_began(run)[1], *run.got(lane.builds[2])]), began


@pytest.mark.parametrize(("lane", "pause", "interruption"), every((KILL,), renamed=False) + every((KILL,), renamed=True))
def test_a_kill_with_no_cancel_finishes_the_release_and_the_routes_file_names_it(
    lane: Lane, pause: Pause, interruption: str
) -> None:
    run = held(lane, pause, interruption)
    assert (run.outcome(), builds_of(lane), run.visitors_got) == (
        ("done", "switched"),
        [lane.builds[1]],
        [*as_it_began(run)[1], *run.got(lane.builds[1])],
    )
    assert route_file_stamp(lane) == stamp_line(run.asked["generation"])


@pytest.mark.parametrize(("lane", "pause", "interruption"), every(CANCELS, renamed=True))
def test_a_cancel_from_the_rename_on_is_told_too_late_names_the_rename_and_the_release_is_finished_as_asked(
    lane: Lane, pause: Pause, interruption: str
) -> None:
    run = held(lane, pause, interruption)
    again_status, again = run.cancelled_again()
    assert (run.outcome(), builds_of(lane), run.visitors_got) == (
        ("done", "switched"),
        [lane.builds[1]],
        run.got(lane.builds[0], lane.builds[1]),
    )
    assert [(status, TOO_LATE in body["error"]) for status, body in run.cancel_answers] == [(409, True)] * len(run.cancel_answers)
    assert (again_status, TOO_LATE in again["error"], route_file_stamp(lane)) == (409, True, stamp_line(run.asked["generation"]))
    if lane.has_data:
        assert "written before the release" in rows_of(lane)


@pytest.mark.parametrize(("lane", "pause", "interruption"), every((NEWER,), renamed=True))
def test_a_newer_request_from_the_rename_on_runs_after_the_older_has_finished_and_the_older_says_why_it_was_not_cancelled(
    lane: Lane, pause: Pause, interruption: str
) -> None:
    run = held(lane, pause, interruption)
    assert (run.outcome(), run.ended["cancel_asked_by"], TOO_LATE in (run.ended["cancel_refused"] or "")) == (
        ("done", "switched"),
        f"request {run.newer}",
        True,
    )
    assert (run.newer_ended["stands"], run.newer_ended["answer"]["outcome"]) == ("done", "switched")
    assert (builds_of(lane), run.visitors_got) == ([lane.builds[2]], run.got(*lane.builds))


@pytest.mark.parametrize(("lane", "pause", "interruption"), every(INTERRUPTIONS, renamed=True, saved=True))
def test_a_result_already_saved_stands_whatever_comes_after_it(lane: Lane, pause: Pause, interruption: str) -> None:
    run = held(lane, pause, interruption)
    again_status, again = run.cancelled_again()
    assert (run.outcome(), run.ended["cancel_refused"]) == (("done", "switched"), None)
    assert [status for status, _ in run.cancel_answers] == [409] * len(run.cancel_answers)
    assert (again_status, "ended as done" in again["error"]) == (409, True)
    assert run.visitors_got == run.got(*lane.builds[: 3 if interruption == NEWER else 2])


def test_a_move_to_the_build_already_active_cancelled_before_its_result_is_saved_ends_as_cancelled() -> None:
    let_go()
    assert move_lane(SITE.product, SITE.lane, SITE.builds[0])[0] == 200
    before = instances_of(SITE)
    hold("before.record.end")
    try:
        with request_loop(lambda: answering(SITE)) as loop:
            status, asked = ask(f"/lanes/{SITE.product}/{SITE.lane}/move", {"build": SITE.builds[0]})
            assert status == 202, asked
            wait_until_stopped_at("before.record.end")
            marked = ask(f"/requests/{asked['id']}/cancel", {})
            let_go()
            ended = request_ended(asked["id"])
    finally:
        let_go()
    assert (marked[0], ended["stands"], ask(f"/requests/{asked['id']}/cancel", {})[0]) == (202, "cancelled", 200)
    assert (instances_of(SITE), loop.failures, loop.answers_in_order()) == (before, [], [SITE.answers[0]])


GATE_HELD = "/tmp/launchplane-gate-held"  # noqa: S108 a file inside the proxy's container
HOLD_THE_GATE = f"touch {GATE_HELD}; flock {GATE} sh -c 'while [ -e {GATE_HELD} ]; do sleep 0.1; done'"


def in_proxy(command: str, flags: str = "") -> str:
    return on_scratch(f'docker exec {flags} {PROXY_NAME} sh -c "{command}"')


def processes_whose(file: str, text: str) -> set[str]:
    found = in_proxy(f"grep -l {text} /proc/[0-9]*/{file} 2>/dev/null || true").split()
    return {path.split("/")[2] for path in found}


# The process of a rename that Docker has accepted and that waits for the gate's lock, inside the gate: the one flock
# whose command names a mark, which the test's own hold of the gate does not.
def rename_waiting_at_the_gate() -> str:
    return next(iter(processes_whose("comm", "flock") & processes_whose("cmdline", "abandoned")), "")


@dataclass
class RenameInFlight:
    asked: dict[str, Any]
    process: str
    loop: RequestLoop


# A release whose rename Docker has accepted and that is held inside the gate, and Launchplane killed there. No worker
# recovers until the test lets go of the request's take-up.
@pytest.fixture
def rename_in_flight_and_launchplane_killed() -> Iterator[RenameInFlight]:
    let_go()
    assert move_lane(SITE.product, SITE.lane, SITE.builds[0])[0] == 200
    in_proxy(HOLD_THE_GATE, flags="--detach")
    process = ""
    try:
        with request_loop(lambda: answering(SITE)) as loop:
            status, asked = ask(f"/lanes/{SITE.product}/{SITE.lane}/move", {"build": SITE.builds[1]})
            assert status == 202, asked
            hold(TAKE_UP)
            wait_for(lambda: bool(rename_waiting_at_the_gate()), seconds=60)
            process = rename_waiting_at_the_gate()
            kill_held_launchplane()
            wait_until_stopped_at(TAKE_UP)
            assert route_file_stamp(SITE) != stamp_line(asked["generation"]), "the rename is still inside the gate"
            yield RenameInFlight(asked, process, loop)
    finally:
        in_proxy(f"rm -f {GATE_HELD}; kill -CONT {process} 2>/dev/null || true")
        let_go()


def test_a_rename_in_flight_when_launchplane_is_killed_that_reaches_the_gate_first_is_honoured_and_finished(
    rename_in_flight_and_launchplane_killed: RenameInFlight,
) -> None:
    held = rename_in_flight_and_launchplane_killed
    in_proxy(f"rm -f {GATE_HELD}")
    wait_for(lambda: route_file_stamp(SITE) == stamp_line(held.asked["generation"]), seconds=30)
    let_go()
    ended = request_ended(held.asked["id"])
    assert (ended["stands"], ended["answer"]["outcome"], builds_of(SITE)) == ("done", "switched", [SITE.builds[1]])
    assert [attempt["ended"] for attempt in ended["attempts"]] == ["interrupted", "done"]


def test_a_rename_in_flight_that_reaches_the_gate_after_its_candidate_was_cleaned_up_renames_nothing(
    rename_in_flight_and_launchplane_killed: RenameInFlight,
) -> None:
    held = rename_in_flight_and_launchplane_killed
    [left] = [each["container"] for each in instances_of(SITE) if each["build"] == SITE.builds[1]]
    in_proxy(f"kill -STOP {held.process}; rm -f {GATE_HELD}")
    let_go()
    ended = request_ended(held.asked["id"])
    [serving] = instances_of(SITE)
    in_proxy(f"kill -CONT {held.process}")
    wait_for(lambda: not rename_waiting_at_the_gate(), seconds=30)
    assert (ended["stands"], ended["answer"]["outcome"], serving["build"], serving["container"] != left) == (
        "done",
        "switched",
        SITE.builds[1],
        True,
    )
    assert (instances_of(SITE), route_file_stamp(SITE)) == ([serving], stamp_line(held.asked["generation"]))
    assert ask("/running")[1]["web_addresses"] and answering(SITE) == SITE.answers[1]
