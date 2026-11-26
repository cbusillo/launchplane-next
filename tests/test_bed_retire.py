import http.client
import json
import threading
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.bed import (
    SCRATCH_ADDRESS,
    kill_launchplane_and_end_the_request_working_on,
    move_lane,
    needs_test_bed,
    read_running,
    retire,
    wait_for,
)

pytestmark = needs_test_bed

PRODUCT = "test-bed-site"
LANE = "retiring"
NAME = f"{LANE}.{PRODUCT}.test"
SLOW_BUILD = "kennethreitz/httpbin@sha256:599fe5e5073102dbb0ee3dbb65f049dab44fa9fc251f6835c9990f8fb196a72b"
READ_PAUSE_SECONDS = 0.1


@dataclass(frozen=True)
class LaneSeen:
    routed: bool
    copies: int


@dataclass
class SlowRequest:
    seconds: int
    status: int | None = None
    body: dict[str, Any] = field(default_factory=dict[str, Any])
    failure: str | None = None

    def send(self) -> None:
        connection = http.client.HTTPConnection(SCRATCH_ADDRESS, 80, timeout=self.seconds + 30)
        try:
            connection.request("GET", f"/delay/{self.seconds}", headers={"Host": NAME})
            response = connection.getresponse()
            self.status, self.body = response.status, json.loads(response.read())
        except (OSError, ValueError) as error:
            self.failure = repr(error)
        finally:
            connection.close()


@contextmanager
def request_in_flight(seconds: int) -> Generator[SlowRequest]:
    request = SlowRequest(seconds)
    thread = threading.Thread(target=request.send)
    thread.start()
    time.sleep(1)
    try:
        yield request
    finally:
        thread.join()


def lane_seen(running: dict[str, Any]) -> LaneSeen:
    routed = any(entry["web_address"] == NAME and entry["sends_to_container"] for entry in running["web_addresses"])
    copies = sum(1 for copy in running["instances"] if (copy["product"], copy["lane"]) == (PRODUCT, LANE))
    return LaneSeen(routed=routed, copies=copies)


def other_lanes(running: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    names = [entry for entry in running["web_addresses"] if entry["web_address"] != NAME]
    copies = [copy for copy in running["instances"] if (copy["product"], copy["lane"]) != (PRODUCT, LANE)]
    return names, copies


def in_background(action: Callable[[], object]) -> threading.Thread:
    thread = threading.Thread(target=action)
    thread.start()
    return thread


# The lane is read once before the action is begun, so how it stood then is seen however fast the action's first change is.
def watch_lane_during(action: Callable[[], object]) -> list[LaneSeen]:
    seen = [lane_seen(read_running())]
    thread = in_background(action)
    while thread.is_alive():
        now = lane_seen(read_running())
        if not seen or seen[-1] != now:
            seen.append(now)
        time.sleep(READ_PAUSE_SECONDS)
    final = lane_seen(read_running())
    return seen if seen and seen[-1] == final else [*seen, final]


def name_is_gone_and_copy_remains() -> bool:
    return lane_seen(read_running()) == LaneSeen(routed=False, copies=1)


def retire_cut_short_by_the_kill() -> None:
    with suppress(OSError):
        retire(PRODUCT, LANE)


@pytest.fixture
def lane_serving_slow_build() -> Iterator[None]:
    status, body = move_lane(PRODUCT, LANE, SLOW_BUILD)
    assert status == 200, body
    yield
    status, body = retire(PRODUCT, LANE)
    assert status == 200, body


@pytest.mark.usefixtures("lane_serving_slow_build")
def test_retiring_takes_the_name_away_first_then_the_copies_and_no_other_lane_changes() -> None:
    before = other_lanes(read_running())
    with request_in_flight(seconds=5):
        seen = watch_lane_during(lambda: retire(PRODUCT, LANE))
    assert seen == [LaneSeen(routed=True, copies=1), LaneSeen(routed=False, copies=1), LaneSeen(routed=False, copies=0)]
    assert other_lanes(read_running()) == before


@pytest.mark.usefixtures("lane_serving_slow_build")
def test_a_request_in_flight_when_the_lane_is_retired_gets_its_answer() -> None:
    with request_in_flight(seconds=5) as request:
        status, body = retire(PRODUCT, LANE)
    assert (status, body["outcome"]) == (200, "retired"), body
    assert (request.failure, request.status) == (None, 200)
    assert request.body["headers"]["Host"] == NAME


@pytest.mark.usefixtures("lane_serving_slow_build")
def test_retiring_a_retired_lane_changes_nothing_and_says_so() -> None:
    retire(PRODUCT, LANE)
    status, body = retire(PRODUCT, LANE)
    assert (status, body["outcome"], body["route_removed_to_container"], body["retired_containers"]) == (200, "unchanged", None, [])


@pytest.mark.usefixtures("lane_serving_slow_build")
def test_a_retire_that_failed_after_the_name_is_gone_is_finished_by_asking_again() -> None:
    copy_name = next(copy["container"] for copy in read_running()["instances"] if copy["lane"] == LANE)
    with request_in_flight(seconds=10):
        retirer = in_background(retire_cut_short_by_the_kill)
        wait_for(name_is_gone_and_copy_remains, seconds=30)
        kill_launchplane_and_end_the_request_working_on(PRODUCT, LANE)
        retirer.join()
    assert name_is_gone_and_copy_remains()
    status, body = retire(PRODUCT, LANE)
    assert (status, body["outcome"], body["route_removed_to_container"], body["retired_containers"]) == (
        200,
        "retired",
        None,
        [copy_name],
    )
    assert lane_seen(read_running()) == LaneSeen(routed=False, copies=0)
