import json
import os
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

LAUNCHPLANE_URL = os.environ.get("LAUNCHPLANE_URL", "")
SCRATCH_SHELL = shlex.split(os.environ.get("LAUNCHPLANE_SCRATCH_SHELL", "lp-scratch"))
CONTROL_HOST_NAME = "launchplane.shiny"
PRODUCT = "test-bed-site"
AWKWARD_BUILD = 'build "1", with a comma'

pytestmark = pytest.mark.skipif(
    not LAUNCHPLANE_URL, reason="needs the test bed: set LAUNCHPLANE_URL, e.g. http://launchplane.shiny:8700"
)


def on_scratch(command: str) -> str:
    return subprocess.run([*SCRATCH_SHELL, command], capture_output=True, text=True, check=True).stdout  # noqa: S603


def ask_what_is_running() -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"{LAUNCHPLANE_URL}/running", timeout=60) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def copies_by_lane() -> dict[str, dict[str, str]]:
    status, body = ask_what_is_running()
    assert status == 200, body
    return {copy["lane"]: copy for copy in body["copies"] if copy["product"] == PRODUCT}


def place_copy(lane: str, build: str) -> None:
    labels = f"--label launchplane.product={PRODUCT} --label launchplane.lane={lane} --label launchplane.build={shlex.quote(build)}"
    on_scratch(f"docker run --detach --name {PRODUCT}-{lane} {labels} busybox:1.37 sleep infinity")


def remove_copies() -> None:
    on_scratch(f"docker rm --force $(docker ps --all --quiet --filter label=launchplane.product={PRODUCT}) 2>/dev/null || true")


@pytest.fixture
def clean_scratch() -> Iterator[None]:
    remove_copies()
    yield
    remove_copies()


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
