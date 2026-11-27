import sys
import time
from collections.abc import Iterator
from functools import partial

import pytest

from tests.bed import (
    ask,
    kill_launchplane,
    move_lane,
    needs_test_bed,
    read_running,
    request_ended,
    request_loop,
    retire,
    server_answering,
    wait_for,
)

pytestmark = needs_test_bed

PRODUCT = "test-bed-site"
LANES = ("side-one", "side-two")
LANE = LANES[0]
BUILD_A = "nginx:1.30.6-alpine"
BUILD_B = "nginx:1.31.8-alpine"
ANSWER_A = "nginx/1.30.6"


@pytest.fixture
def lanes_on_a() -> Iterator[None]:
    for lane in LANES:
        assert move_lane(PRODUCT, lane, BUILD_A)[0] == 200
    yield
    for lane in LANES:
        assert retire(PRODUCT, lane)[0] == 200


def builds_of(lane: str) -> list[str]:
    return [each["build"] for each in read_running()["instances"] if (each["product"], each["lane"]) == (PRODUCT, lane)]


def lanes_being_moved() -> set[str]:
    return {each["lane"] for each in read_running()["lanes"] if each["product"] == PRODUCT and each["work"] == "moving"}


def both_lanes_are_being_moved() -> bool:
    return lanes_being_moved() == set(LANES)


def build_b_is_being_checked() -> bool:
    return BUILD_B in builds_of(LANE)


def asked_to_move(lane: str, build: str) -> int:
    status, request = ask(f"/lanes/{PRODUCT}/{lane}/move", {"build": build})
    assert status == 202, request
    return request["id"]


@pytest.mark.usefixtures("lanes_on_a")
def test_two_lanes_asked_at_once_are_at_work_at_the_same_time_and_both_switch() -> None:
    began = time.monotonic()
    asked = [asked_to_move(lane, BUILD_B) for lane in LANES]
    wait_for(both_lanes_are_being_moved, seconds=10)
    ended = [request_ended(each) for each in asked]
    took = time.monotonic() - began
    assert [(each["stands"], each["answer"]["outcome"]) for each in ended] == [("done", "switched")] * 2, ended
    assert [builds_of(lane) for lane in LANES] == [[BUILD_B], [BUILD_B]]
    sys.stdout.write(f"Two lanes moved at once took {took:.1f} s.\n")


@pytest.mark.usefixtures("lanes_on_a")
def test_a_move_cancelled_by_hand_before_the_switch_leaves_the_lane_as_it_was() -> None:
    with request_loop(partial(server_answering, f"{LANE}.{PRODUCT}.test")) as loop:
        request = asked_to_move(LANE, BUILD_B)
        wait_for(build_b_is_being_checked, seconds=30)
        status, cancelled = ask(f"/requests/{request}/cancel?wait=60", {})
    assert (status, cancelled["stands"], [each.split()[0] for each in cancelled["answer"]["put_back"]]) == (
        200,
        "cancelled",
        ["stop", "remove"],
    ), cancelled
    assert (loop.failures, loop.answers_in_order(), builds_of(LANE)) == ([], [ANSWER_A], [BUILD_A])
    judged = next(each for each in read_running()["lanes"] if (each["product"], each["lane"]) == (PRODUCT, LANE))
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
