import json
import os
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

LAUNCHPLANE_URL = os.environ.get("LAUNCHPLANE_URL", "")
SCRATCH_SHELL = shlex.split(os.environ.get("LAUNCHPLANE_SCRATCH_SHELL", "lp-scratch"))
SCRATCH_ADDRESS = os.environ.get("LAUNCHPLANE_SCRATCH_ADDRESS", "docker-lp-scratch.shiny")
CONTROL_SHELL = shlex.split(os.environ.get("LAUNCHPLANE_CONTROL_SHELL", "lp-control"))

PAUSE_THAT_KEEPS_CLIENT_PORTS_FREE = 0.02

needs_test_bed = pytest.mark.skipif(
    not LAUNCHPLANE_URL, reason="needs the test bed: set LAUNCHPLANE_URL, e.g. http://launchplane.shiny:8700"
)


def on_scratch(command: str, given: str | None = None) -> str:
    return subprocess.run([*SCRATCH_SHELL, command], input=given, capture_output=True, text=True, check=True).stdout  # noqa: S603


def on_control(command: str, given: str | None = None) -> str:
    return subprocess.run([*CONTROL_SHELL, command], input=given, capture_output=True, text=True, check=True).stdout  # noqa: S603


def on_scratch_with_bytes(command: str, given: bytes) -> str:
    return subprocess.run([*SCRATCH_SHELL, command], input=given, capture_output=True, check=True).stdout.decode()  # noqa: S603


def ask(
    path: str, body: dict[str, Any] | None = None, timeout: float = 60, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} | (headers or {})
    request = urllib.request.Request(f"{LAUNCHPLANE_URL}{path}", data=data, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


# A call that changes a lane is kept as a request. The tests wait for it to end, and so read the answer it ended with.
def ask_and_wait(path: str, body: dict[str, Any], timeout: float = 120) -> tuple[int, dict[str, Any]]:
    return ask(f"{path}?wait={timeout - 10:g}", body, timeout)


def remove_product_copies(product: str) -> None:
    on_scratch(f"docker rm --force $(docker ps --all --quiet --filter label=launchplane.product={product}) 2>/dev/null || true")


def move_lane(product: str, lane: str, build: str) -> tuple[int, dict[str, Any]]:
    return ask_and_wait(f"/lanes/{product}/{lane}/move", {"build": build})


def retire(product: str, lane: str, delete_data_set: str | None = None) -> tuple[int, dict[str, Any]]:
    return ask_and_wait(f"/lanes/{product}/{lane}/retire", {"delete_data_set": delete_data_set})


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


def kill_launchplane() -> None:
    on_control("systemctl kill --signal=KILL launchplane")
    wait_for(launchplane_answers, seconds=30)


# noinspection SqlResolve
END_WHAT_WAS_ASKED = """
update attempt set ended_at = now(), ended = 'interrupted' where ended is null;
update request set stands = 'failed', ended_at = now(), error = 'Ended by a test, as a request that failed halfway.'
where stands in ('waiting', 'working');
"""


# After a kill Launchplane takes its request up again by itself. A test of what a request that failed halfway leaves behind
# ends the request in the record while Launchplane is down, so the lane stays as the kill left it.
def kill_launchplane_and_end_what_was_asked() -> None:
    on_control(
        "systemctl kill --signal=KILL launchplane; systemctl stop launchplane; "
        "runuser -u postgres -- psql --quiet --set ON_ERROR_STOP=1 --dbname launchplane; ended=$?; "
        "systemctl start launchplane; exit $ended",
        given=END_WHAT_WAS_ASKED,
    )
    wait_for(launchplane_answers, seconds=30)


def unfinished_requests() -> list[dict[str, Any]]:
    status, body = ask("/requests")
    assert status == 200, body
    return body["unfinished"]


def request_ended(request: int) -> dict[str, Any]:
    def has_ended() -> bool:
        return ask(f"/requests/{request}")[1]["stands"] in ("done", "failed", "cancelled")

    wait_for(has_ended, seconds=120)
    return ask(f"/requests/{request}")[1]


def kill_once(call: Callable[[], object], seen: Callable[[], bool], kill: Callable[[], None] = kill_launchplane) -> None:
    cut_short: list[OSError] = []

    def call_until_the_kill() -> None:
        try:
            call()
        except OSError as error:
            cut_short.append(error)

    def point_is_seen() -> bool:
        if seen():
            return True
        assert caller.is_alive(), f"the call ended before its point was seen: {cut_short or 'it was answered'}"
        return False

    caller = threading.Thread(target=call_until_the_kill)
    caller.start()
    wait_for(point_is_seen, seconds=90)
    kill()
    caller.join()


class UnexpectedAnswer(Exception):
    pass


@dataclass
class RequestLoop:
    request_once: Callable[[], str]
    answers: list[str] = field(default_factory=list[str])
    failures: list[str] = field(default_factory=list[str])
    running: bool = True

    def run(self) -> None:
        while self.running:
            try:
                self.answers.append(self.request_once())
            except (OSError, UnexpectedAnswer) as error:
                self.failures.append(f"{datetime.now(UTC):%H:%M:%S.%f} {error!r}")
            time.sleep(PAUSE_THAT_KEEPS_CLIENT_PORTS_FREE)

    def answers_in_order(self) -> list[str]:
        return [answer for index, answer in enumerate(self.answers) if index == 0 or self.answers[index - 1] != answer]


@contextmanager
def request_loop(request_once: Callable[[], str]) -> Generator[RequestLoop]:
    loop = RequestLoop(request_once)
    thread = threading.Thread(target=loop.run)
    thread.start()
    time.sleep(1)
    try:
        yield loop
    finally:
        time.sleep(1)
        loop.running = False
        thread.join()


HOLDS_URL = os.environ.get("LAUNCHPLANE_HOLDS_URL", f"{LAUNCHPLANE_URL.rsplit(':', 1)[0]}:8701")
HELD_UNIT = "/opt/launchplane/tests/launchplane-held.service"


# The holds of tests/held_launchplane.py, which runs Launchplane on the control container with its calls able to be held.
def holds(method: str = "GET", point: str = "") -> dict[str, list[str]]:
    request = urllib.request.Request(f"{HOLDS_URL}/holds{'/' if point else ''}{point}", method=method)  # noqa: S310
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.loads(response.read())


def hold(point: str) -> None:
    holds("PUT", point)


def let_go(point: str = "") -> None:
    holds("DELETE", point)


def wait_until_stopped_at(point: str, seconds: float = 60) -> None:
    def launchplane_is_stopped_at_the_hold() -> bool:
        return point.split("?")[0] in holds()["stopped_at"]

    wait_for(launchplane_is_stopped_at_the_hold, seconds)


def holds_answer() -> bool:
    try:
        return bool(holds()) and launchplane_answers()
    except OSError:
        return False


# As `kill -9`: no handler runs, and systemd starts Launchplane again.
def kill_held_launchplane() -> None:
    try:
        urllib.request.urlopen(urllib.request.Request(f"{HOLDS_URL}/kill", method="POST"), timeout=10)  # noqa: S310
    except OSError:
        pass
    wait_for(holds_answer, seconds=30)


@contextmanager
def launchplane_held() -> Generator[None]:
    on_control(f"install -m 644 {HELD_UNIT} /etc/systemd/system/ && systemctl daemon-reload && systemctl start launchplane-held")
    wait_for(holds_answer, seconds=30)
    let_go()
    try:
        yield
    finally:
        let_go()
        on_control("systemctl start launchplane")
        wait_for(launchplane_answers, seconds=30)
