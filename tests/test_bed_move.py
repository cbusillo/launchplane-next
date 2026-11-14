import http.client
import os
import threading
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import pytest

from launchplane.name_switch import PROXY_NAME, ROUTES_FILE, STAGED_ROUTES_FILE, routes_file
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
PAUSE_THAT_KEEPS_CLIENT_PORTS_FREE = 0.02
SLOW_DOWNLOAD_BYTES = 24 * 1024 * 1024
SLOW_DOWNLOAD_CHUNK = 256 * 1024
SLOW_DOWNLOAD_PAUSE = 0.05


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
            time.sleep(PAUSE_THAT_KEEPS_CLIENT_PORTS_FREE)

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


def move(build: str, product: str = PRODUCT, lane: str = LANE) -> tuple[int, dict[str, Any]]:
    return ask(f"/lanes/{product}/{lane}/move", {"build": build})


def write_route_without_launchplane(copy_name: str) -> None:
    status, body = ask("/running")
    assert status == 200, body
    loaded = {copy["serves"]: copy["name"] for copy in body["copies"] if copy["serves"]}
    staged = f"docker exec -i {PROXY_NAME} sh -c 'cat > {STAGED_ROUTES_FILE} && mv {STAGED_ROUTES_FILE} {ROUTES_FILE}'"
    on_scratch(staged, given=routes_file(loaded | {NAME: copy_name}))


@dataclass
class SlowDownload:
    received: int = 0
    error: str | None = None

    def run(self) -> None:
        connection = http.client.HTTPConnection(SCRATCH_ADDRESS, 80, timeout=30)
        try:
            connection.request("GET", "/big", headers={"Host": NAME})
            response = connection.getresponse()
            while chunk := response.read(SLOW_DOWNLOAD_CHUNK):
                self.received += len(chunk)
                time.sleep(SLOW_DOWNLOAD_PAUSE)
        except OSError as error:
            self.error = repr(error)
        finally:
            connection.close()


def give_build_a_a_big_file() -> None:
    copy_name = lane_copies()[BUILD_A]["name"]
    on_scratch(f"docker exec {copy_name} sh -c 'head -c {SLOW_DOWNLOAD_BYTES} /dev/zero > /usr/share/nginx/html/big'")


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


@pytest.mark.usefixtures("lane_serving_a")
def test_a_route_written_but_not_yet_loaded_when_launchplane_asks_is_waited_for_not_deleted() -> None:
    with request_loop() as loop:
        kill_launchplane_once_build_b_has_started()
        write_route_without_launchplane(lane_copies()[BUILD_B]["name"])
        status, body = move(BUILD_A)
    assert (status, body["outcome"]) == (200, "switched"), body
    assert loop.failures == []
    assert list(lane_copies()) == [BUILD_A]
    assert serving_builds() == [BUILD_A]


@pytest.mark.usefixtures("lane_serving_a")
def test_a_request_in_flight_on_the_old_copy_at_the_switch_finishes() -> None:
    give_build_a_a_big_file()
    download = SlowDownload()
    downloading = threading.Thread(target=download.run)
    downloading.start()
    time.sleep(1)
    status, body = move(BUILD_B)
    downloading.join()
    assert (status, body["outcome"]) == (200, "switched"), body
    assert (download.error, download.received) == (None, SLOW_DOWNLOAD_BYTES)


def test_two_lanes_whose_product_and_lane_join_to_the_same_text_each_keep_their_own_copy() -> None:
    lanes = [("test-bed-x-y", "z"), ("test-bed-x", "y-z")]
    for product, _ in lanes:
        remove_product_copies(product)
    try:
        for product, lane in lanes:
            status, body = move(BUILD_A, product, lane)
            assert (status, body["outcome"]) == (200, "switched"), body
        status, body = ask("/running")
        assert status == 200, body
        serving_copies = {(copy["product"], copy["lane"]): copy["serves"] for copy in body["copies"]}
        assert {lane: serving_copies.get(lane) for lane in lanes} == {
            (product, lane): f"{lane}.{product}.test" for product, lane in lanes
        }
    finally:
        for product, _ in lanes:
            remove_product_copies(product)
