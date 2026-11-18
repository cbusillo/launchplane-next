import http.client
import time
from collections.abc import Iterator
from typing import Any

import pytest

from tests.bed import (
    SCRATCH_ADDRESS,
    UnexpectedAnswer,
    ask,
    kill_launchplane,
    move_lane,
    needs_test_bed,
    request_ended,
    request_loop,
    retire,
    wait_for,
)

pytestmark = needs_test_bed

PRODUCT = "test-bed-site"
LANES = ("side-one", "side-two")
LANE = LANES[0]
BUILD_A = "nginx:1.30.6-alpine"
BUILD_B = "nginx:1.31.8-alpine"
ANSWER_A = "nginx/1.30.6"
SECONDS_ONE_MOVE_TAKES = 6.3


@pytest.fixture
def lanes_on_a() -> Iterator[None]:
    for lane in LANES:
        assert move_lane(PRODUCT, lane, BUILD_A)[0] == 200
    yield
    for lane in LANES:
        assert retire(PRODUCT, lane)[0] == 200


def server_answering() -> str:
    connection = http.client.HTTPConnection(SCRATCH_ADDRESS, 80, timeout=5)
    try:
        connection.request("GET", "/", headers={"Host": f"{LANE}.{PRODUCT}.test"})
        response = connection.getresponse()
        response.read()
        if response.status != 200:
            raise UnexpectedAnswer(f"status {response.status}")
        return response.getheader("Server", "")
    finally:
        connection.close()


def running() -> dict[str, Any]:
    status, body = ask("/running")
    assert status == 200, body
    return body


def builds_of(lane: str) -> list[str]:
    return [each["build"] for each in running()["instances"] if (each["product"], each["lane"]) == (PRODUCT, lane)]


def lanes_being_moved() -> set[str]:
    return {each["lane"] for each in running()["lanes"] if each["product"] == PRODUCT and each["work"] == "moving"}


def both_lanes_are_being_moved() -> bool:
    return lanes_being_moved() == set(LANES)


def build_b_is_being_checked() -> bool:
    return BUILD_B in builds_of(LANE)


def asked_to_move(lane: str, build: str) -> int:
    status, request = ask(f"/lanes/{PRODUCT}/{lane}/move", {"build": build})
    assert status == 202, request
    return request["id"]


def request_row_of(request: int) -> tuple[list[str], str]:
    card = next(card for card in running()["cards"] if card["title"] == f"{PRODUCT} · {LANE}")
    row = next(row for row in card["rows"] if row["name"] == f"Request {request}")
    return [pill["text"] for pill in row["pills"]], row["note"]


@pytest.mark.usefixtures("lanes_on_a")
def test_two_lanes_asked_at_once_are_at_work_at_the_same_time_and_take_about_as_long_as_one() -> None:
    began = time.monotonic()
    asked = [asked_to_move(lane, BUILD_B) for lane in LANES]
    wait_for(both_lanes_are_being_moved, seconds=10)
    ended = [request_ended(each) for each in asked]
    took = time.monotonic() - began
    assert [(each["stands"], each["answer"]["outcome"]) for each in ended] == [("done", "switched")] * 2, ended
    assert took < 1.5 * SECONDS_ONE_MOVE_TAKES, f"two lanes took {took:.1f} s"
    assert [builds_of(lane) for lane in LANES] == [[BUILD_B], [BUILD_B]]


@pytest.mark.usefixtures("lanes_on_a")
def test_a_newer_request_cancels_the_move_being_checked_and_no_visitor_reaches_its_build() -> None:
    with request_loop(server_answering) as loop:
        older = asked_to_move(LANE, BUILD_B)
        wait_for(build_b_is_being_checked, seconds=30)
        newer = asked_to_move(LANE, BUILD_A)
        cancelled, done = request_ended(older), request_ended(newer)
    assert (cancelled["stands"], cancelled["cancel_asked_by"], done["stands"]) == ("cancelled", f"request {newer}", "done"), (
        cancelled
    )
    assert (loop.failures, loop.answers_in_order(), builds_of(LANE)) == ([], [ANSWER_A], [BUILD_A])
    pills, note = request_row_of(older)
    assert (pills, f"was cancelled by request {newer}. Done to put the lane back: remove" in note) == (["cancelled"], True)


@pytest.mark.usefixtures("lanes_on_a")
def test_a_move_cancelled_by_hand_before_the_switch_leaves_the_lane_as_it_was() -> None:
    with request_loop(server_answering) as loop:
        request = asked_to_move(LANE, BUILD_B)
        wait_for(build_b_is_being_checked, seconds=30)
        status, cancelled = ask(f"/requests/{request}/cancel?wait=60", {})
    assert (status, cancelled["stands"], len(cancelled["answer"]["put_back"])) == (200, "cancelled", 1), cancelled
    assert (loop.failures, loop.answers_in_order(), builds_of(LANE)) == ([], [ANSWER_A], [BUILD_A])
    judged = next(each for each in running()["lanes"] if (each["product"], each["lane"]) == (PRODUCT, LANE))
    assert (judged["condition"], ask(f"/requests/{request}/cancel", {})[0]) == ("good", 200), judged


@pytest.mark.usefixtures("lanes_on_a")
def test_a_request_being_cancelled_when_launchplane_is_killed_is_still_cancelled_and_put_back_after_the_restart() -> None:
    request = asked_to_move(LANE, BUILD_B)
    wait_for(build_b_is_being_checked, seconds=30)
    status, marked = ask(f"/requests/{request}/cancel", {})
    kill_launchplane()
    ended = request_ended(request)
    assert (status, marked["stands"]) == (202, "working"), marked
    assert (ended["stands"], [attempt["ended"] for attempt in ended["attempts"]]) == ("cancelled", ["interrupted", "cancelled"]), (
        ended
    )
    assert builds_of(LANE) == [BUILD_A]
