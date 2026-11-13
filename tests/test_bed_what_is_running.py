import shlex
import socket
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from tests.bed import ask, needs_test_bed, on_scratch, remove_product_copies

CONTROL_HOST_NAME = "launchplane.shiny"
PRODUCT = "test-bed-site"
AWKWARD_BUILD = 'build "1", with a comma'

pytestmark = needs_test_bed


def ask_what_is_running() -> tuple[int, dict[str, Any]]:
    return ask("/running")


def copies_by_lane() -> dict[str, dict[str, str]]:
    status, body = ask_what_is_running()
    assert status == 200, body
    return {copy["lane"]: copy for copy in body["copies"] if copy["product"] == PRODUCT}


def place_copy(lane: str, build: str) -> None:
    labels = f"--label launchplane.product={PRODUCT} --label launchplane.lane={lane} --label launchplane.build={shlex.quote(build)}"
    on_scratch(f"docker run --detach --name {PRODUCT}-{lane} {labels} busybox:1.37 sleep infinity")


@pytest.fixture
def clean_scratch() -> Iterator[None]:
    remove_product_copies(PRODUCT)
    yield
    remove_product_copies(PRODUCT)


@contextmanager
def control_cut_off_from_scratch() -> Generator[None]:
    control_address = socket.gethostbyname(CONTROL_HOST_NAME)
    rule = f"INPUT --source {control_address} --protocol tcp --dport 2376 --jump DROP"
    on_scratch(f"iptables --insert {rule}")
    try:
        yield
    finally:
        on_scratch(f"iptables --delete {rule}")


@pytest.mark.usefixtures("clean_scratch")
def test_the_running_call_follows_the_host_through_stop_start_and_a_second_lane() -> None:
    place_copy("testing", AWKWARD_BUILD)
    testing = copies_by_lane()["testing"]
    assert (testing["build"], testing["state"]) == (AWKWARD_BUILD, "running")

    on_scratch(f"docker stop --time 1 {PRODUCT}-testing")
    assert copies_by_lane()["testing"]["state"] == "stopped"

    on_scratch(f"docker start {PRODUCT}-testing")
    assert copies_by_lane()["testing"]["state"] == "running"

    place_copy("production", "build-2")
    assert {lane: copy["state"] for lane, copy in copies_by_lane().items()} == {"testing": "running", "production": "running"}


def test_an_unreachable_scratch_container_is_an_error_within_seconds_not_an_empty_answer() -> None:
    with control_cut_off_from_scratch():
        started = time.monotonic()
        status, body = ask_what_is_running()
        seconds = time.monotonic() - started
    assert status == 503, body
    assert seconds < 8
    assert "copies" not in body
    assert "timed out" in body["error"]
