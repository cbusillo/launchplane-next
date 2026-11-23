import http.client
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from launchplane.proxy import PROXY_NAME, instance_url, route_file_of, route_text
from tests.bed import (
    SCRATCH_ADDRESS,
    RequestLoop,
    UnexpectedAnswer,
    ask,
    kill_launchplane,
    kill_launchplane_and_end,
    move_lane,
    needs_test_bed,
    on_scratch,
    request_ended,
    retire,
    wait_for,
)
from tests.bed import request_loop as request_loop_of

pytestmark = needs_test_bed

PRODUCT = "test-bed-site"
LANE = "testing"
NAME = f"{LANE}.{PRODUCT}.test"
BUILD_A = "nginx:1.30.6-alpine"
BUILD_B = "nginx:1.31.8-alpine"
BUILD_C = "hashicorp/http-echo:1.0"
ANSWER_A = "nginx/1.30.6"
ANSWER_B = "nginx/1.31.8"


def server_answering() -> str:
    connection = http.client.HTTPConnection(SCRATCH_ADDRESS, 80, timeout=5)
    try:
        connection.request("GET", "/", headers={"Host": NAME})
        response = connection.getresponse()
        response.read()
        if response.status != 200:
            raise UnexpectedAnswer(f"status {response.status}")
        return response.getheader("Server", "")
    finally:
        connection.close()


@contextmanager
def request_loop() -> Generator[RequestLoop]:
    with request_loop_of(server_answering) as loop:
        yield loop


def write_route_without_launchplane_keeping_every_loaded_route(copy_name: str) -> None:
    written = route_file_of(NAME)
    staged = f"{written}.next"
    on_scratch(f"docker exec -i {PROXY_NAME} sh -c 'cat > {staged} && mv {staged} {written}'", given=route_text(NAME, copy_name))


def lane_copies() -> dict[str, dict[str, Any]]:
    status, body = ask("/running")
    assert status == 200, body
    return {copy["build"]: copy for copy in body["instances"] if (copy["product"], copy["lane"]) == (PRODUCT, LANE)}


def lane_name_answer() -> dict[str, Any]:
    status, body = ask("/running")
    assert status == 200, body
    return next(entry for entry in body["web_addresses"] if entry["web_address"] == NAME)


def serving_builds() -> list[str]:
    return [build for build, copy in lane_copies().items() if copy["receives_visitors_for"] == NAME]


# Of this lane: another lane of the product on the same build must not pass for it.
def copy_of_build_b_is_on_scratch() -> bool:
    of_the_lane = f"--filter label=launchplane.product={PRODUCT} --filter label=launchplane.lane={LANE}"
    return bool(on_scratch(f"docker ps --quiet {of_the_lane} --filter label=launchplane.build={BUILD_B}"))


def copy_of_build_b_answers_the_proxy() -> bool:
    container = lane_copies()[BUILD_B]["container"]
    return on_scratch(f"docker exec {PROXY_NAME} wget -q -O /dev/null -T 2 {instance_url(container)}/ && echo yes || true") != ""


def kill_launchplane_once_build_b_has_started(kill: Callable[[int], None] = kill_launchplane_and_end) -> int:
    status, request = ask(f"/lanes/{PRODUCT}/{LANE}/move", {"build": BUILD_B})
    assert status == 202, request
    wait_for(copy_of_build_b_is_on_scratch, seconds=30)
    kill(request["id"])
    return request["id"]


def move(build: str, product: str = PRODUCT, lane: str = LANE) -> tuple[int, dict[str, Any]]:
    return move_lane(product, lane, build)


def ensure_lane_serves_a() -> None:
    status, body = move(BUILD_A)
    assert status == 200, body


@pytest.fixture
def lane_serving_a() -> Iterator[None]:
    ensure_lane_serves_a()
    yield
    ensure_lane_serves_a()


@pytest.mark.usefixtures("lane_serving_a")
def test_moving_to_a_new_build_switches_the_name_without_failing_a_request() -> None:
    with request_loop() as loop:
        status, body = move(BUILD_B)
    assert (status, body["outcome"]) == (200, "switched"), body
    assert loop.failures == []
    assert loop.answers_in_order() == [ANSWER_A, ANSWER_B]
    assert list(lane_copies()) == [BUILD_B]
    assert serving_builds() == [BUILD_B]


@pytest.mark.usefixtures("lane_serving_a")
def test_a_serving_copy_stopped_by_hand_shows_stopped_and_its_name_answers_nothing() -> None:
    assert lane_name_answer()["answers"] is True
    copy_name = lane_copies()[BUILD_A]["container"]
    on_scratch(f"docker stop --time 1 {copy_name}")
    assert lane_copies()[BUILD_A]["state"] == "stopped"
    entry = lane_name_answer()
    assert (entry["answers"], entry["sends_to_container"]) == (False, copy_name)
    assert "502" in entry["error"]


@pytest.mark.usefixtures("lane_serving_a")
def test_asking_for_a_finished_move_again_changes_nothing() -> None:
    move(BUILD_B)
    before = lane_copies()
    status, body = move(BUILD_B)
    assert (status, body["outcome"]) == (200, "unchanged"), body
    assert lane_copies() == before


@pytest.mark.usefixtures("lane_serving_a")
def test_a_build_that_fails_its_check_switches_nothing_and_leaves_no_copy() -> None:
    with request_loop() as loop:
        status, body = move(BUILD_C)
    assert (status, body["outcome"]) == (422, "check failed"), body
    assert "Connection refused" in body["error"]
    assert loop.failures == []
    assert loop.answers_in_order() == [ANSWER_A]
    assert list(lane_copies()) == [BUILD_A]


@pytest.mark.usefixtures("lane_serving_a")
def test_a_move_killed_before_the_switch_is_taken_up_again_and_finished_with_nobody_asking() -> None:
    with request_loop() as loop:
        request = request_ended(kill_launchplane_once_build_b_has_started(lambda _request: kill_launchplane()))
    assert (request["stands"], request["answer"]["outcome"]) == ("done", "switched"), request
    assert [attempt["ended"] for attempt in request["attempts"]] == ["interrupted", "done"]
    assert loop.failures == []
    assert loop.answers_in_order() == [ANSWER_A, ANSWER_B]
    assert list(lane_copies()) == [BUILD_B]


@pytest.mark.usefixtures("lane_serving_a")
def test_a_move_that_failed_before_the_switch_is_finished_by_asking_again() -> None:
    with request_loop() as loop:
        kill_launchplane_once_build_b_has_started()
        assert sorted(lane_copies()) == [BUILD_A, BUILD_B]
        assert serving_builds() == [BUILD_A]
        status, body = move(BUILD_B)
    assert (status, body["outcome"]) == (200, "switched"), body
    assert loop.failures == []
    assert loop.answers_in_order() == [ANSWER_A, ANSWER_B]
    assert list(lane_copies()) == [BUILD_B]


@pytest.mark.usefixtures("lane_serving_a")
def test_a_move_that_failed_before_the_switch_is_repaired_by_asking_for_the_serving_build() -> None:
    with request_loop() as loop:
        kill_launchplane_once_build_b_has_started()
        status, body = move(BUILD_A)
    assert (status, body["outcome"], [each.split()[0] for each in body["did"]]) == (200, "repaired", ["stop", "remove"]), body
    assert loop.failures == []
    assert loop.answers_in_order() == [ANSWER_A]
    assert list(lane_copies()) == [BUILD_A]


@pytest.mark.usefixtures("lane_serving_a")
def test_a_route_written_but_not_yet_loaded_when_launchplane_asks_is_waited_for_not_deleted() -> None:
    with request_loop() as loop:
        kill_launchplane_once_build_b_has_started()
        wait_for(copy_of_build_b_answers_the_proxy, seconds=30)
        write_route_without_launchplane_keeping_every_loaded_route(lane_copies()[BUILD_B]["container"])
        status, body = move(BUILD_A)
    assert (status, body["outcome"]) == (200, "switched"), body
    assert loop.failures == []
    assert list(lane_copies()) == [BUILD_A]
    assert serving_builds() == [BUILD_A]


def test_two_lanes_whose_product_and_lane_join_to_the_same_text_each_keep_their_own_copy() -> None:
    lanes = [("test-bed-x-y", "z"), ("test-bed-x", "y-z")]
    try:
        for product, lane in lanes:
            status, body = move(BUILD_A, product, lane)
            assert (status, body["outcome"]) == (200, "switched"), body
        status, body = ask("/running")
        assert status == 200, body
        serving_copies = {(copy["product"], copy["lane"]): copy["receives_visitors_for"] for copy in body["instances"]}
        assert {lane: serving_copies.get(lane) for lane in lanes} == {
            (product, lane): f"{lane}.{product}.test" for product, lane in lanes
        }
    finally:
        for product, lane in lanes:
            status, body = retire(product, lane)
            assert status == 200, body
