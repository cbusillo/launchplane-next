import http.client
import os
import threading
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.bed import ask, needs_test_bed, on_control, on_scratch, remove_product_copies

pytestmark = needs_test_bed

SCRATCH_ADDRESS = os.environ.get("LAUNCHPLANE_SCRATCH_ADDRESS", "docker-lp-scratch.shiny")
PRODUCT = "test-bed-site"
LANE = "testing"
NAME = f"{LANE}.{PRODUCT}.test"
BUILD_A = "nginx:1.30.6-alpine"
BUILD_B = "nginx:1.31.8-alpine"
BUILD_C = "hashicorp/http-echo:1.0"
ANSWER_A = "nginx/1.30.6"
ANSWER_B = "nginx/1.31.8"


@dataclass
class RequestLoop:
    answers: list[str] = field(default_factory=list[str])
    failures: list[str] = field(default_factory=list[str])
    running: bool = True

    def request_once(self) -> None:
        connection = http.client.HTTPConnection(SCRATCH_ADDRESS, 80, timeout=5)
        try:
            connection.request("GET", "/", headers={"Host": NAME})
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                self.answers.append(response.getheader("Server", ""))
            else:
                self.failures.append(f"status {response.status}")
        except OSError as error:
            self.failures.append(repr(error))
        finally:
            connection.close()

    def run(self) -> None:
        while self.running:
            self.request_once()

    def answers_in_order(self) -> list[str]:
        return [answer for index, answer in enumerate(self.answers) if index == 0 or self.answers[index - 1] != answer]


@contextmanager
def request_loop() -> Generator[RequestLoop]:
    loop = RequestLoop()
    thread = threading.Thread(target=loop.run)
    thread.start()
    time.sleep(1)
    try:
        yield loop
    finally:
        time.sleep(1)
        loop.running = False
        thread.join()


def move(build: str) -> tuple[int, dict[str, Any]]:
    return ask(f"/lanes/{PRODUCT}/{LANE}/move", {"build": build})


def lane_copies() -> dict[str, dict[str, Any]]:
    status, body = ask("/running")
    assert status == 200, body
    return {copy["build"]: copy for copy in body["copies"] if (copy["product"], copy["lane"]) == (PRODUCT, LANE)}


def serving_builds() -> list[str]:
    return [build for build, copy in lane_copies().items() if copy["serves"] == NAME]


def copy_of_build_b_is_on_scratch() -> bool:
    filters = f"--filter label=launchplane.product={PRODUCT} --filter label=launchplane.build={BUILD_B}"
    return bool(on_scratch(f"docker ps --quiet {filters}"))


def wait_for(condition: Callable[[], bool], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, f"waited {seconds} s for {condition.__name__}"
        time.sleep(0.1)


def launchplane_answers() -> bool:
    try:
        return ask("/running", timeout=2)[0] == 200
    except OSError:
        return False


def kill_launchplane_once_build_b_has_started() -> None:
    mover = threading.Thread(target=move_to_b_cut_short_by_the_kill)
    mover.start()
    wait_for(copy_of_build_b_is_on_scratch, seconds=30)
    on_control("systemctl kill --signal=KILL launchplane")
    mover.join()
    wait_for(launchplane_answers, seconds=30)


def move_to_b_cut_short_by_the_kill() -> None:
    with suppress(OSError):
        move(BUILD_B)


@pytest.fixture
def lane_serving_a() -> Iterator[None]:
    remove_product_copies(PRODUCT)
    status, body = move(BUILD_A)
    assert (status, body["outcome"]) == (200, "switched"), body
    yield
    remove_product_copies(PRODUCT)


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
def test_a_move_killed_before_the_switch_is_finished_by_asking_again() -> None:
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
def test_a_move_killed_before_the_switch_is_cleaned_up_by_asking_for_the_serving_build() -> None:
    with request_loop() as loop:
        kill_launchplane_once_build_b_has_started()
        status, body = move(BUILD_A)
    assert (status, body["outcome"]) == (200, "cleaned up"), body
    assert loop.failures == []
    assert loop.answers_in_order() == [ANSWER_A]
    assert list(lane_copies()) == [BUILD_A]
