import http.client
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from typing import Any
from urllib.parse import quote

import pytest

from launchplane.gate import GATE_FOLDER_NAME, Answer, Gate
from launchplane.instance import Verdict
from launchplane.proxy import PROXY_NAME, VOLUME_FOLDER, route_file_of
from tests.bed import (
    SCRATCH_ADDRESS,
    RequestLoop,
    UnexpectedAnswer,
    ask,
    ask_and_wait,
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
from tests.held_launchplane import M3_BIN
from tests.test_bed_data_set import DESCRIPTION, build_pretend_product, place_description
from tests.test_bed_gate import ScratchHost

pytestmark = needs_test_bed

CANCEL, KILL = "a cancel", "a kill"
KILL_THEN_CANCEL, CANCEL_THEN_KILL = "a kill, then a cancel", "a cancel, then a kill"
INTERRUPTIONS = (CANCEL, KILL, KILL_THEN_CANCEL, CANCEL_THEN_KILL)
CANCELS = (CANCEL, KILL_THEN_CANCEL, CANCEL_THEN_KILL)
TOO_LATE = "already renamed for this release"
THE_RENAME = "proxy.switch"
TURN, TAKE_UP, GATE_ANSWER = "before.record.turn", "before.record.take_up", "after.lanes.abandon"
UNTOUCHED = "untouched"
NOTHING = "no lane"
# Traefik loads a changed routes file within 2 s.
SECONDS_THE_PROXY_TAKES_TO_LOAD = 4


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
    # A hold reached first, at which the lane's background service is stopped by hand, so that it is watched after.
    service_stopped_at: str | None = None
    # At the hold, the candidate's routes file is staged and armed as the gate does just before it renames.
    staged: bool = False
    # The one interruption this pause is proven with on the bed. A pause with none stands for its row and has all four.
    # The whole table runs against the pretend host in tests/test_interruptions.py.
    once: str | None = None


SITE_PAUSES = {
    "M0, waiting for its lane's turn": Pause(TURN),
    "M0, taken up, before its first action": Pause("before.host.start_instance", once=CANCEL),
    "M1, candidate created and not started": Pause("after.host.create", once=KILL),
    "M1, candidate started and not yet checked": Pause("after.host.start_instance"),
    "M2, its check under way": Pause("before.proxy.request_instance", once=KILL_THEN_CANCEL),
    "M2, check passed": Pause("after.proxy.check_instance"),
    "M3, before the gate": Pause(f"before.{THE_RENAME}"),
    "M3, staged and armed, not yet renamed": Pause(f"before.{THE_RENAME}", staged=True, once=KILL),
    "M4, renamed, its reply not yet had, proxy still on A": Pause(f"after.{THE_RENAME}", renamed=True, proxy_held=True),
    "M4, renamed, waiting for a proxy still on A": Pause(
        "before.proxy.await_route", renamed=True, proxy_held=True, once=CANCEL_THEN_KILL
    ),
    "M5, proxy on B, the reply to the rename not yet had": Pause(f"after.{THE_RENAME}", renamed=True, proxy_on_b=True, once=CANCEL),
    "M5, proxy on B, old instance still up": Pause("before.host.retire_instance", renamed=True),
    "M6, old instance gone": Pause("after.host.retire_instance", renamed=True),
    "Z, before its result is saved": Pause("before.record.end", renamed=True),
    "Z, result saved": Pause("after.record.end", renamed=True, saved=True),
}
LEDGER_PAUSES = {
    "M1, the network of a new data set made": Pause("after.host.create_data_set_network", from_nothing=True, once=CANCEL),
    "M1, the volume of a new data set made": Pause("after.host.create_data_set_volume", from_nothing=True, once=KILL),
    "M1, the database server of a new data set made": Pause("after.host.create_database", from_nothing=True, once=CANCEL_THEN_KILL),
    "M5, before the old background service is retired": Pause("before.host.retire_instance", renamed=True, once=KILL_THEN_CANCEL),
    "M5, the new background service started": Pause("after.host.start_background_service", renamed=True, once=CANCEL_THEN_KILL),
    "M5, the new background service being watched": Pause(
        "before.host.await_background_service", renamed=True, service_stopped_at="after.host.start_background_service", once=KILL
    ),
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


# The gate itself, asked from the test as Launchplane asks it. Asking changes nothing.
GATE = Gate(ScratchHost(), PROXY_NAME, VOLUME_FOLDER)
ABANDONED = Answer(Verdict.ABANDONED)


def verdict_of(lane: Lane, generation: str) -> Answer:
    return GATE.verdict(lane.web_address, generation)


def committed_to_the_one_instance_of(lane: Lane, generation: str, build: str) -> bool:
    [instance] = instances_of(lane)
    return (verdict_of(lane, generation), instance["build"]) == (Answer(Verdict.COMMITTED, instance["container"]), build)


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


def stop_background_services_of(lane: Lane) -> None:
    labels = f"--filter label=launchplane.product={lane.product} --filter label=launchplane.lane={lane.lane}"
    on_scratch(f"docker stop $(docker ps --quiet {labels} --filter name=background-service)")


def interrupt_armed_switch(lane: Lane, asked: dict[str, Any]) -> None:
    generation = asked["generation"]
    [candidate] = [each["container"] for each in instances_of(lane) if each["build"] == lane.builds[1]]
    base = VOLUME_FOLDER / GATE_FOLDER_NAME / f"{lane.web_address}.{generation}"
    shim = '#!/bin/sh\ncase "$1" in *.next) kill -KILL "$PPID"; exit 1 ;; esac\nexec /bin/busybox mv "$@"\n'
    place = f"mkdir -p {M3_BIN} && cat > {M3_BIN}/mv && chmod +x {M3_BIN}/mv"
    on_scratch(f"docker exec -i {PROXY_NAME} sh -c '{place}'", given=shim)
    only = "?only=" + quote(f"'{generation}', 'switch'")
    try:
        hold("interrupt-switch" + only)
        hold("after.host.run_in" + only)
        let_go(f"before.{THE_RENAME}")
        wait_until_stopped_at("after.host.run_in")
        assert in_proxy(f"test -f {base}.{candidate}.next && cat {base}.armed").split()[1] == candidate
        assert verdict_of(lane, generation) == Answer(Verdict.NONE)
    finally:
        in_proxy(f"rm -f {M3_BIN}/mv && rmdir {M3_BIN}")


@dataclass
class Held:
    lane: Lane
    pause: Pause
    interruption: str
    asked: dict[str, Any] = field(default_factory=dict[str, Any])
    cancel_answers: list[tuple[int, dict[str, Any]]] = field(default_factory=list[tuple[int, dict[str, Any]]])
    cancel_cut_short: list[OSError] = field(default_factory=list[OSError])
    ended: dict[str, Any] = field(default_factory=dict[str, Any])
    visitors_got: list[str] = field(default_factory=list[str])
    visitor_failures: list[str] = field(default_factory=list[str])
    untouched_got: list[str] = field(default_factory=list[str])
    untouched_failures: list[str] = field(default_factory=list[str])

    def cancel(self, wait: int = 0) -> None:
        try:
            self.cancel_answers.append(ask(f"/requests/{self.asked['id']}/cancel?wait={wait}", {}, timeout=wait + 30))
        except OSError as error:
            self.cancel_cut_short.append(error)

    # A cancel has no winner until it reaches the gate, so the release is held until the gate answers that it won.
    def cancel_is_answered_or_has_won_at_the_gate(self) -> bool:
        return bool(self.cancel_answers) or verdict_of(self.lane, self.asked["generation"]) == ABANDONED

    # What the host shows at the pause: the gate says committed, and the proxy still sends visitors to A.
    def look_while_the_proxy_is_held(self, loop: RequestLoop) -> None:
        assert verdict_of(self.lane, self.asked["generation"]).verdict is Verdict.COMMITTED
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
        if self.pause.service_stopped_at:
            hold(self.pause.service_stopped_at)
        with request_loop(lambda: answering(self.lane)) as loop, request_loop(lambda: answering(untouched)) as beside:
            status, self.asked = ask(f"/lanes/{self.lane.product}/{self.lane.lane}/move", {"build": self.lane.builds[1]})
            assert status == 202, self.asked
            if self.pause.service_stopped_at:
                wait_until_stopped_at(self.pause.service_stopped_at, seconds=120)
                stop_background_services_of(self.lane)
                let_go(self.pause.service_stopped_at)
            wait_until_stopped_at(self.pause.hold, seconds=120)
            if self.pause.staged:
                interrupt_armed_switch(self.lane, self.asked)
            if self.pause.proxy_held:
                self.look_while_the_proxy_is_held(loop)
            if self.pause.proxy_on_b:
                wait_for(lambda: loop.answers[-1] == self.lane.answers[1], seconds=30)
            self.interrupt()
            self.ended = request_ended(self.asked["id"])
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
        for build, version in zip(LEDGER.builds, LEDGER.answers, strict=True):
            build_pretend_product(build, version)
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


@pytest.mark.parametrize(("lane", "pause", "interruption"), every((KILL,), renamed=False) + every((KILL,), renamed=True))
def test_a_kill_with_no_cancel_finishes_the_release_and_the_gate_says_it_is_committed_to_the_one_instance_left(
    lane: Lane, pause: Pause, interruption: str
) -> None:
    run = held(lane, pause, interruption)
    assert (run.outcome(), builds_of(lane), run.visitors_got) == (
        ("done", "switched"),
        [lane.builds[1]],
        [*as_it_began(run)[1], *run.got(lane.builds[1])],
    )
    assert committed_to_the_one_instance_of(lane, run.asked["generation"], lane.builds[1])


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
    assert (again_status, TOO_LATE in again["error"]) == (409, True)
    assert committed_to_the_one_instance_of(lane, run.asked["generation"], lane.builds[1])
    if lane.has_data:
        assert "written before the release" in rows_of(lane)


@pytest.mark.parametrize(("lane", "pause", "interruption"), every(INTERRUPTIONS, renamed=True, saved=True))
def test_a_result_already_saved_stands_whatever_comes_after_it(lane: Lane, pause: Pause, interruption: str) -> None:
    run = held(lane, pause, interruption)
    again_status, again = run.cancelled_again()
    assert (run.outcome(), run.ended["cancel_refused"]) == (("done", "switched"), None)
    assert [status for status, _ in run.cancel_answers] == [409] * len(run.cancel_answers)
    assert (again_status, "ended as done" in again["error"]) == (409, True)
    assert run.visitors_got == run.got(*lane.builds[:2])


# Answers as Gunicorn would, so a visitor it ever reached would show among the answers.
NEVER_STARTED = "kennethreitz/httpbin@sha256:599fe5e5073102dbb0ee3dbb65f049dab44fa9fc251f6835c9990f8fb196a72b"


def request_row_of(lane: Lane, request: int) -> tuple[list[str], str]:
    card = next(card for card in running()["cards"] if card["title"] == f"{lane.product} · {lane.lane}")
    row = next(row for row in card["rows"] if row["name"] == f"Request {request}")
    return [pill["text"] for pill in row["pills"]], row["note"]


def test_a_move_held_before_its_switch_finishes_as_asked_then_the_newest_waiting_runs_and_one_that_waited_between_never_starts() -> (
    None
):
    let_go()
    assert move_lane(SITE.product, SITE.lane, SITE.builds[0])[0] == 200
    hold(f"before.{THE_RENAME}")
    try:
        with request_loop(lambda: answering(SITE)) as loop:
            first = asked_to_move(SITE.builds[1])
            wait_until_stopped_at(f"before.{THE_RENAME}")
            second, third = asked_to_move(NEVER_STARTED), asked_to_move(SITE.builds[2])
            replaced, at_work = request_ended(second["id"]), ask(f"/requests/{first['id']}")[1]
            let_go()
            finished, ran_next = request_ended(first["id"]), request_ended(third["id"])
    finally:
        let_go()
    assert (at_work["stands"], at_work["cancel_asked_by"], finished["stands"], finished["answer"]["outcome"]) == (
        "working",
        None,
        "done",
        "switched",
    ), finished
    assert (replaced["stands"], replaced["replaced_by"], replaced["attempts"], replaced["events"]) == (
        "replaced",
        third["id"],
        [],
        [],
    ), replaced
    assert (ran_next["stands"], ran_next["answer"]["outcome"], builds_of(SITE)) == ("done", "switched", [SITE.builds[2]]), ran_next
    assert (loop.failures, loop.answers_in_order()) == ([], list(SITE.answers))
    pills, note = request_row_of(SITE, second["id"])
    assert (pills, f"was replaced by request {third['id']} while it waited" in note) == (["replaced"], True)


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
HOLD_THE_GATE = f"touch {GATE_HELD}; flock {VOLUME_FOLDER} sh -c 'while [ -e {GATE_HELD} ]; do sleep 0.1; done'"
A, B, C = SITE.builds
FAILS_ITS_CHECK = "hashicorp/http-echo:1.0"


def in_proxy(command: str, flags: str = "") -> str:
    return on_scratch(f'docker exec {flags} {PROXY_NAME} sh -c "{command}"')


def processes_whose(file: str, text: str) -> set[str]:
    found = in_proxy(f"grep -l {text} /proc/[0-9]*/{file} 2>/dev/null || true").split()
    return {path.split("/")[2] for path in found}


def asked_to_move(build: str) -> dict[str, Any]:
    status, asked = ask(f"/lanes/{SITE.product}/{SITE.lane}/move", {"build": build})
    assert status == 202, asked
    return asked


def cancel(request: int, wait: int = 0) -> tuple[int, dict[str, Any]]:
    return ask(f"/requests/{request}/cancel?wait={wait}", {}, timeout=wait + 30)


def how_it_stands(request: int) -> str:
    return ask(f"/requests/{request}")[1]["stands"]


def ended_as(request: int) -> tuple[str, str | None]:
    ended = request_ended(request)
    answer: dict[str, str] = ended["answer"] or {}
    return ended["stands"], answer.get("outcome")


# One of the rule's held cases: the lane on A and the lane beside it, both probed from outside for the whole of the case.
# Leaving it, every probe answered, the lane's visitors got exactly the builds expected and in that order, and the
# lane beside kept its instance and its build.
RENAMES = "/tmp/launchplane-renames"  # noqa: S108 a file inside the proxy's container
# Every file moved out of the gate's folder, as the kernel reports it: a staged routes file leaves it only by the rename.
WATCH_RENAMES = f"inotifyd - {VOLUME_FOLDER / GATE_FOLDER_NAME}:m > {RENAMES}"


@dataclass
class Case:
    beside_before: list[dict[str, Any]]
    loop: RequestLoop
    beside: RequestLoop
    expected: tuple[str, ...] = (A,)

    def visitors_get(self, *builds: str) -> None:
        self.expected = builds

    # How often a staged routes file of the generation was renamed into place since the case began.
    @staticmethod
    def renames_of(generation: str) -> int:
        moved = in_proxy(f"cat {RENAMES}").splitlines()
        return sum(generation in line and line.endswith(".next") for line in moved)

    def now_serves(self, build: str) -> bool:
        return self.loop.answers[-1] == SITE.answer_of(build)


@pytest.fixture
def case() -> Iterator[Case]:
    let_go()
    untouched = SITE.beside(UNTOUCHED)
    assert (move_lane(SITE.product, SITE.lane, A)[0], move_lane(untouched.product, untouched.lane, A)[0]) == (200, 200)
    in_proxy(WATCH_RENAMES, flags="--detach")
    try:
        with request_loop(lambda: answering(SITE)) as loop, request_loop(lambda: answering(untouched)) as beside:
            held_case = Case(instances_of(untouched), loop, beside)
            yield held_case
    finally:
        in_proxy(f"pkill inotifyd; rm -f {GATE_HELD}")
        let_go()
    assert (loop.failures, beside.failures) == ([], [])
    assert loop.answers_in_order() == [SITE.answer_of(build) for build in held_case.expected]
    assert (beside.answers_in_order(), instances_of(untouched)) == ([SITE.answers[0]], held_case.beside_before)


@dataclass
class InFlight:
    asked: dict[str, Any]
    process: str
    left: str


# A switch names its candidate, which no other question to the gate does.
def switch_waiting_for_the_lock(candidate: str) -> set[str]:
    return processes_whose("comm", "flock") & processes_whose("cmdline", candidate)


# The rule's cases 1 and 4 begin here: B's switch is accepted by Docker and held before it has the gate's lock, and
# Launchplane is killed. The held command is frozen, the lock let go, and no worker recovers until the test lets it.
@pytest.fixture
def switch_in_flight(case: Case) -> Iterator[InFlight]:
    hold("before.proxy.switch")
    asked, process = asked_to_move(B), ""
    try:
        wait_until_stopped_at("before.proxy.switch", seconds=120)
        [left] = [each["container"] for each in instances_of(SITE) if each["build"] == B]
        in_proxy(HOLD_THE_GATE, flags="--detach")
        hold(TAKE_UP)
        let_go("before.proxy.switch")
        wait_for(lambda: bool(switch_waiting_for_the_lock(left)), seconds=60)
        [process] = switch_waiting_for_the_lock(left)
        in_proxy(f"kill -STOP {process}; rm -f {GATE_HELD}")
        kill_held_launchplane()
        wait_until_stopped_at(TAKE_UP)
        assert (verdict_of(SITE, asked["generation"]), case.renames_of(asked["generation"])) == (Answer(Verdict.NONE), 0)
        yield InFlight(asked, process, left)
    finally:
        in_proxy(f"kill -CONT {process} 2>/dev/null || true")


def test_case_1_a_rename_that_lands_between_recoverys_reading_of_the_routes_file_and_of_the_proxy_ends_done_and_switched(
    case: Case, switch_in_flight: InFlight
) -> None:
    generation = switch_in_flight.asked["generation"]
    hold("after.proxy.read_folder")
    let_go(TAKE_UP)
    wait_until_stopped_at("after.proxy.read_folder")
    in_proxy(f"kill -CONT {switch_in_flight.process}")
    wait_for(lambda: case.now_serves(B), seconds=30)
    let_go()
    case.visitors_get(A, B)
    assert ended_as(switch_in_flight.asked["id"]) == ("done", "switched")
    assert (verdict_of(SITE, generation), case.renames_of(generation)) == (Answer(Verdict.COMMITTED, switch_in_flight.left), 1)
    assert [each["container"] for each in instances_of(SITE)] == [switch_in_flight.left]


def test_case_2_a_rename_whose_reply_alone_is_lost_ends_done_and_switched_on_the_same_candidate_with_one_rename(
    case: Case,
) -> None:
    hold("before.proxy.switch")
    asked = asked_to_move(B)
    wait_until_stopped_at("before.proxy.switch", seconds=120)
    hold("lose.host.run_in?only=" + quote(f"'{asked['generation']}', 'switch'"))
    let_go("before.proxy.switch")
    case.visitors_get(A, B)
    ended = request_ended(asked["id"])
    assert (ended["stands"], ended["answer"]["outcome"]) == ("done", "switched")
    assert [attempt["ended"] for attempt in ended["attempts"]] == ["unfinished", "done"]
    assert (committed_to_the_one_instance_of(SITE, asked["generation"], B), case.renames_of(asked["generation"])) == (True, 1)


def test_case_3_a_release_renamed_and_held_from_loading_across_four_kills_stays_working_and_is_finished_once_it_loads(
    case: Case,
) -> None:
    hold("proxy-delivery")
    hold("after.proxy.switch")
    asked = asked_to_move(B)
    wait_until_stopped_at("after.proxy.switch")
    let_go("after.proxy.switch")
    for _ in range(4):
        kill_held_launchplane()
    too_late = cancel(asked["id"])
    assert (too_late[0], TOO_LATE in too_late[1]["error"], how_it_stands(asked["id"])) == (409, True, "working")
    assert (case.now_serves(A), sorted(builds_of(SITE))) == (True, sorted([A, B]))
    let_go()
    case.visitors_get(A, B)
    assert ended_as(asked["id"]) == ("done", "switched")
    assert (committed_to_the_one_instance_of(SITE, asked["generation"], B), case.renames_of(asked["generation"])) == (True, 1)


def test_case_4_a_stale_rename_resumed_after_recovery_discarded_its_candidate_renames_nothing_and_a_fresh_one_switches_once(
    case: Case, switch_in_flight: InFlight
) -> None:
    let_go()
    case.visitors_get(A, B)
    assert ended_as(switch_in_flight.asked["id"]) == ("done", "switched")
    generation = switch_in_flight.asked["generation"]
    in_proxy(f"kill -CONT {switch_in_flight.process}")
    wait_for(lambda: not switch_waiting_for_the_lock(switch_in_flight.left), seconds=30)
    [fresh] = instances_of(SITE)
    assert (fresh["container"] != switch_in_flight.left, case.renames_of(generation)) == (True, 1)
    assert verdict_of(SITE, generation) == Answer(Verdict.COMMITTED, fresh["container"])


def test_case_5_a_cancel_that_won_while_the_host_cannot_be_read_stays_working_and_ends_cancelled_once_it_can(case: Case) -> None:
    hold("before.proxy.request_instance")
    asked = asked_to_move(B)
    wait_until_stopped_at("before.proxy.request_instance")
    assert cancel(asked["id"])[0] == 202
    hold("fail.host.read")
    let_go("before.proxy.request_instance")
    wait_for(lambda: len(ask(f"/requests/{asked['id']}")[1]["attempts"]) >= 4, seconds=120)
    assert how_it_stands(asked["id"]) == "working"
    let_go()
    assert (ended_as(asked["id"])[0], cancel(asked["id"])[0], builds_of(SITE)) == ("cancelled", 200, [A])
    staged = in_proxy(f"ls {VOLUME_FOLDER / GATE_FOLDER_NAME} | grep {asked['generation']} | grep next || true")
    assert (staged, verdict_of(SITE, asked["generation"]), case.renames_of(asked["generation"])) == ("", ABANDONED, 0)


def test_case_6_a_cancel_that_wins_while_failed_is_about_to_be_saved_ends_the_move_as_cancelled_with_no_rename(case: Case) -> None:
    hold("before.record.end")
    asked = asked_to_move(FAILS_ITS_CHECK)
    wait_until_stopped_at("before.record.end", seconds=120)
    assert cancel(asked["id"])[0] == 202
    let_go()
    ended = request_ended(asked["id"])
    assert (ended["stands"], ended["cancel_refused"], cancel(asked["id"])[0]) == ("cancelled", None, 200)
    assert (builds_of(SITE), case.renames_of(asked["generation"])) == ([A], 0)


def test_case_7_a_cancel_handler_resumed_late_changes_nothing_of_a_later_release_and_both_replayed_switches_rename_nothing(
    case: Case,
) -> None:
    hold("before.proxy.request_instance")
    first = asked_to_move(B)
    wait_until_stopped_at("before.proxy.request_instance")
    hold(f"after.record.cancel?only=%28{first['id']}%2C")
    answers: list[tuple[int, dict[str, Any]]] = []
    late_handler = threading.Thread(target=lambda: answers.append(cancel(first["id"], wait=90)))
    late_handler.start()
    wait_until_stopped_at("after.record.cancel")
    let_go("before.proxy.request_instance")
    assert ended_as(first["id"])[0] == "cancelled"
    hold("before.proxy.switch")
    second = asked_to_move(C)
    wait_until_stopped_at("before.proxy.switch", seconds=120)
    assert cancel(second["id"])[0] == 202
    let_go("before.proxy.switch")
    assert ended_as(second["id"])[0] == "cancelled"
    let_go("after.record.cancel")
    late_handler.join(timeout=120)
    replayed = [
        GATE.switch(SITE.web_address, each["generation"], 10**12, "replayed", "no route", route_file_of(SITE.web_address))
        for each in (first, second)
    ]
    assert ([status for status, _ in answers], replayed) == ([200], [ABANDONED, ABANDONED])
    assert (builds_of(SITE), [case.renames_of(each["generation"]) for each in (first, second)]) == ([A], [0, 0])


def test_case_8_a_cancel_that_won_and_whose_candidate_docker_refuses_to_remove_ends_failed_saying_it_won(case: Case) -> None:
    hold("before.proxy.switch")
    asked = asked_to_move(B)
    wait_until_stopped_at("before.proxy.switch", seconds=120)
    assert cancel(asked["id"])[0] == 202
    hold("refuse.host.retire_instance")
    let_go("before.proxy.switch")
    ended = request_ended(asked["id"])
    again = cancel(asked["id"])
    [left] = [each["container"] for each in instances_of(SITE) if each["build"] == B]
    assert (ended["stands"], ended["cancel_refused"], ended["error"].startswith(f"The cancel won, and {left} could not")) == (
        "failed",
        None,
        True,
    )
    assert (again[0], ended["error"] in again[1]["error"], case.renames_of(asked["generation"])) == (409, True, 0)


BACK_SOON = "the back soon page"


# What a visitor gets, where the back soon page is an answer and no failure.
def answering_or_back_soon(lane: Lane) -> str:
    connection = http.client.HTTPConnection(scratch_ip(), 80, timeout=5)
    try:
        connection.request("GET", "/", headers={"Host": lane.web_address})
        response = connection.getresponse()
        response.read()
        return BACK_SOON if (response.status, response.getheader("Retry-After")) == (503, "60") else answering(lane)
    finally:
        connection.close()


def edit_waiting_for_the_lock(generation: str) -> set[str]:
    return processes_whose("comm", "flock") & processes_whose("cmdline", generation)


def routes_file_of(lane: Lane) -> str:
    return in_proxy(f"stat -c %i {route_file_of(lane.web_address)} && cat {route_file_of(lane.web_address)}")


def test_a_retire_frozen_before_the_gates_lock_and_let_go_with_its_lane_in_maintenance_removes_nothing_and_back_soon_stays() -> None:
    let_go()
    lane = f"/lanes/{SITE.product}/{SITE.lane}"
    assert move_lane(SITE.product, SITE.lane, A)[0] == 200
    serving = instances_of(SITE)
    hold("before.proxy.edit")
    status, asked = ask(f"{lane}/retire", {})
    process = ""
    try:
        wait_until_stopped_at("before.proxy.edit")
        in_proxy(HOLD_THE_GATE, flags="--detach")
        hold(TAKE_UP)
        let_go("before.proxy.edit")
        wait_for(lambda: bool(edit_waiting_for_the_lock(asked["generation"])), seconds=60)
        [process] = edit_waiting_for_the_lock(asked["generation"])
        in_proxy(f"kill -STOP {process}; rm -f {GATE_HELD}")
        kill_held_launchplane()
        wait_until_stopped_at(TAKE_UP)
        marked = cancel(asked["id"])
        let_go()
        assert (status, marked[0], ended_as(asked["id"])[0]) == (202, 202, "cancelled")
        assert ask_and_wait(f"{lane}/maintenance/begin", {})[1]["outcome"] == "in maintenance"
        in_maintenance = routes_file_of(SITE)
        with request_loop(lambda: answering_or_back_soon(SITE)) as loop:
            in_proxy(f"kill -CONT {process}")
            wait_for(lambda: not edit_waiting_for_the_lock(asked["generation"]), seconds=30)
            time.sleep(SECONDS_THE_PROXY_TAKES_TO_LOAD)
        assert (routes_file_of(SITE), loop.failures, loop.answers_in_order()) == (in_maintenance, [], [BACK_SOON])
        assert [each["container"] for each in instances_of(SITE)] == [each["container"] for each in serving]
    finally:
        in_proxy(f"kill -CONT {process} 2>/dev/null; rm -f {GATE_HELD}")
        let_go()
    assert ask_and_wait(f"{lane}/maintenance/end", {})[1]["outcome"] == "ended"


def test_beginning_maintenance_whose_route_change_is_made_and_its_reply_lost_goes_on_until_no_part_of_the_lane_runs() -> None:
    let_go()
    lane = f"/lanes/{LEDGER.product}/{LEDGER.lane}"
    assert move_lane(LEDGER.product, LEDGER.lane, LEDGER.builds[0])[0] == 200
    hold("lose.proxy.edit")
    try:
        ended = request_ended(ask(f"{lane}/maintenance/begin", {})[1]["id"])
    finally:
        let_go()
    assert (ended["stands"], ended["answer"]["outcome"], [each["ended"] for each in ended["attempts"]]) == (
        "done",
        "in maintenance",
        ["unfinished", "done"],
    )
    parts = [each for kind in ("instances", "background_services") for each in running()[kind] if each["lane"] == LEDGER.lane]
    assert (answering_or_back_soon(LEDGER), {each["state"] for each in parts if each["product"] == LEDGER.product}) == (
        BACK_SOON,
        {"stopped"},
    )
    assert ask_and_wait(f"{lane}/maintenance/end", {})[1]["outcome"] == "ended"
