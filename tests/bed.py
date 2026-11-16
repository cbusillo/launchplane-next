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


def ask(path: str, body: dict[str, Any] | None = None, timeout: float = 60) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(f"{LAUNCHPLANE_URL}{path}", data=data, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def remove_product_copies(product: str) -> None:
    on_scratch(f"docker rm --force $(docker ps --all --quiet --filter label=launchplane.product={product}) 2>/dev/null || true")


def move_lane(product: str, lane: str, build: str) -> tuple[int, dict[str, Any]]:
    return ask(f"/lanes/{product}/{lane}/move", {"build": build})


def retire(product: str, lane: str, delete_data_set: str | None = None) -> tuple[int, dict[str, Any]]:
    return ask(f"/lanes/{product}/{lane}/retire", {"delete_data_set": delete_data_set})


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


def kill_once(call: Callable[[], object], seen: Callable[[], bool]) -> None:
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
    kill_launchplane()
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
                self.failures.append(repr(error))
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
