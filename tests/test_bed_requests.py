import time
from collections.abc import Iterator
from typing import Any

import pytest

from tests.bed import FORGET_STARTS, START_AGAIN, ask, launchplane_answers, move_lane, needs_test_bed, on_control, retire, wait_for

pytestmark = needs_test_bed

PRODUCT = "test-bed-site"
LANE = "requests"
BUILD_A = "nginx:1.30.6-alpine"
BUILD_B = "nginx:1.31.8-alpine"


@pytest.fixture
def lane_on_a() -> Iterator[None]:
    assert move_lane(PRODUCT, LANE, BUILD_A)[0] == 200
    yield
    assert retire(PRODUCT, LANE)[0] == 200


def builds_of_the_lane() -> list[str]:
    return [each["build"] for each in ask("/running")[1]["instances"] if (each["product"], each["lane"]) == (PRODUCT, LANE)]


def the_record_answers() -> bool:
    return ask("/requests")[0] == 200


@pytest.mark.usefixtures("lane_on_a")
def test_with_its_database_stopped_launchplane_refuses_a_change_changes_nothing_and_still_shows_the_host() -> None:
    # Debian's unit for PostgreSQL only groups the clusters, and stopping it returns before they have stopped.
    on_control("systemctl stop 'postgresql@*.service'")
    try:
        refused: tuple[int, dict[str, Any]] = ask(f"/lanes/{PRODUCT}/{LANE}/move", {"build": BUILD_B})
        status, running = ask("/running")
    finally:
        on_control("systemctl start postgresql")
    wait_for(the_record_answers, seconds=30)
    assert (refused[0], "could not be reached" in refused[1]["error"]) == (503, True), refused
    assert (status, "could not be reached" in running["record_error"], running["cards"][0]["title"]) == (
        200,
        True,
        "Launchplane's record cannot be read",
    )
    assert builds_of_the_lane() == [BUILD_A]
    moved = move_lane(PRODUCT, LANE, BUILD_B)
    assert (moved[0], moved[1]["outcome"]) == (200, "switched"), moved


@pytest.mark.usefixtures("lane_on_a")
def test_with_its_database_frozen_launchplane_still_shows_the_host_within_seconds_and_says_the_record_did_not_answer() -> None:
    on_control("pkill -STOP -u postgres")
    try:
        began = time.monotonic()
        status, running = ask("/running", timeout=30)
        took = time.monotonic() - began
    finally:
        on_control("pkill -CONT -u postgres")
    wait_for(the_record_answers, seconds=30)
    assert (status, "did not answer within 6 s" in running["record_error"], running["cards"][0]["title"]) == (
        200,
        True,
        "Launchplane's record cannot be read",
    ), running["record_error"]
    assert (took < 12, builds_of_the_lane()) == (True, [BUILD_A]), took


@pytest.mark.usefixtures("lane_on_a")
def test_the_same_key_asked_twice_moves_the_lane_once_and_answers_with_the_one_request() -> None:
    key = {"Idempotency-Key": f"test-bed-{ask('/requests')[1]['ended'][0]['id']}"}
    first = ask(f"/lanes/{PRODUCT}/{LANE}/move?wait=60", {"build": BUILD_B}, headers=key)
    again = ask(f"/lanes/{PRODUCT}/{LANE}/move?wait=60", {"build": BUILD_B}, headers=key)
    other = ask(f"/lanes/{PRODUCT}/{LANE}/move?wait=60", {"build": BUILD_A}, headers=key)
    assert (first[0], first[1]["outcome"], again) == (200, "switched", first)
    assert (other[0], builds_of_the_lane()) == (409, [BUILD_B]), other


def how_systemd_has_launchplane() -> str:
    return on_control("systemctl show launchplane --property=ActiveState --value").strip()


def test_a_launchplane_that_keeps_dying_is_left_failed_by_systemd_at_its_limit_and_the_documented_command_starts_it_again() -> None:
    def is_left_failed() -> bool:
        return how_systemd_has_launchplane() == "failed"

    limit = int(on_control("systemctl show launchplane --property=StartLimitBurst --value"))
    on_control(FORGET_STARTS)
    try:
        for _ in range(limit):
            on_control("systemctl kill --signal=KILL launchplane")
            wait_for(launchplane_answers, seconds=30)
        on_control("systemctl kill --signal=KILL launchplane")
        wait_for(is_left_failed, seconds=30)
        time.sleep(3)
        left_failed = (how_systemd_has_launchplane(), launchplane_answers())
    finally:
        on_control(START_AGAIN)
    wait_for(launchplane_answers, seconds=30)
    assert (limit, left_failed) == (10, ("failed", False))
