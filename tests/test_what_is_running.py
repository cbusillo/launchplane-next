import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import docker
import pytest
from fastapi.testclient import TestClient

from launchplane.api import WebAddressChecker, create_app
from launchplane.container_host import DOCKER_API_VERSION, ContainerHost, ContainerHostError, OnHost, instances_from_answer
from launchplane.decide import MoveResult, RetireResult
from launchplane.instance import InstanceState, LaneBuild, RunningInstance

SAVED_CONTAINER_LIST = Path(__file__).parent / "answers" / "container-list.json"


UNEXPECTED_ANSWERS: dict[str, object] = {
    "an object instead of a list": {"message": "page not found"},
    "a container without a state": [{"Names": ["/site"], "Labels": {"launchplane.product": "site"}}],
    "a state Docker never had": [{"Names": ["/site"], "State": "hibernating", "Labels": {"launchplane.product": "site"}}],
    "a container the filter should not have let through": [{"Names": ["/site"], "State": "running", "Labels": {}}],
    "a container without a name": [{"Names": [], "State": "running", "Labels": {"launchplane.product": "site"}}],
}


def saved_container_list() -> object:
    return json.loads(SAVED_CONTAINER_LIST.read_text())


def refusing_host() -> ContainerHost:
    client = docker.DockerClient(base_url="tcp://127.0.0.1:1", version=DOCKER_API_VERSION, timeout=5)
    return ContainerHost(client, "tcp://127.0.0.1:1")


def no_move(target: LaneBuild) -> MoveResult:
    raise AssertionError(f"Reading what is running asked for a move to {target.build}")


def no_retire(product: str, lane: str, _delete_data_set: str | None) -> RetireResult:
    raise AssertionError(f"Reading what is running asked to retire {lane} of {product}")


def every_web_address_answers(_web_address: str) -> str | None:
    return None


type InstanceReader = Callable[[], list[RunningInstance]]


def app_reading(
    read: InstanceReader, routes: dict[str, str] | None = None, why_fails: WebAddressChecker = every_web_address_answers
) -> TestClient:
    return TestClient(create_app(lambda: OnHost(read(), [], []), lambda: routes or {}, why_fails, no_move, no_retire))


def ask_running(read: InstanceReader) -> tuple[int, dict[str, Any]]:
    response = app_reading(read).get("/running")
    return response.status_code, response.json()


def test_a_saved_docker_answer_reads_as_instances_in_launchplane_terms() -> None:
    instances = {instance.container: instance for instance in instances_from_answer(saved_container_list())}
    assert instances == {
        "site-production": RunningInstance(
            product="site", lane="production", build="b2", container="site-production", state=InstanceState.RUNNING
        ),
        "site-testing": RunningInstance(
            product="site", lane="testing", build="b3", container="site-testing", state=InstanceState.STOPPED
        ),
        "site-preview-7": RunningInstance(
            product="site", lane="preview-7", build="b4", container="site-preview-7", state=InstanceState.PAUSED
        ),
        "site-unmarked-lane": RunningInstance(
            product="site", lane=None, build=None, container="site-unmarked-lane", state=InstanceState.RUNNING
        ),
    }


@pytest.mark.parametrize("answer", UNEXPECTED_ANSWERS.values(), ids=UNEXPECTED_ANSWERS.keys())
def test_an_answer_of_unexpected_shape_is_an_error_not_an_empty_list(answer: object) -> None:
    with pytest.raises(ContainerHostError, match="Docker"):
        instances_from_answer(answer)


def test_a_refused_connection_raises_with_the_real_error_instead_of_listing_nothing() -> None:
    with pytest.raises(ContainerHostError, match="Connection refused"):
        refusing_host().read()


def test_the_running_call_answers_unavailable_with_the_real_error_when_the_host_cannot_be_read() -> None:
    status, body = ask_running(lambda: refusing_host().read().instances)
    assert status == 503
    assert "Connection refused" in body["error"]
    assert "instances" not in body


def test_the_running_call_reads_the_host_on_every_request() -> None:
    answers = iter([[], instances_from_answer(saved_container_list())])
    client = app_reading(lambda: next(answers))
    assert client.get("/running").json()["instances"] == []
    assert len(client.get("/running").json()["instances"]) == 4


def test_the_running_call_marks_the_instance_the_proxy_sends_visitors_to_and_no_other() -> None:
    instances = instances_from_answer(saved_container_list())
    body = app_reading(lambda: instances, routes={"production.site.test": "site-production"}).get("/running").json()
    assert {instance["container"]: instance["receives_visitors_for"] for instance in body["instances"]} == {
        "site-production": "production.site.test",
        "site-testing": None,
        "site-preview-7": None,
        "site-unmarked-lane": None,
    }


def web_addresses_answer(
    instances: list[RunningInstance], routes: dict[str, str], why_fails: WebAddressChecker
) -> dict[str, dict[str, Any]]:
    body = app_reading(lambda: instances, routes, why_fails).get("/running").json()
    return {entry["web_address"]: entry for entry in body["web_addresses"]}


def test_a_lane_the_proxy_routes_nowhere_says_so_without_asking_through_the_proxy() -> None:
    def no_request(web_address: str) -> str | None:
        raise AssertionError(f"Asked through the proxy for {web_address}, which it routes nowhere")

    instance = RunningInstance(product="site", lane="testing", build="b1", container="site-testing-1", state=InstanceState.RUNNING)
    entry = web_addresses_answer([instance], {}, no_request)["testing.site.test"]
    assert (entry["product"], entry["lane"], entry["sends_to_container"], entry["answers"]) == ("site", "testing", None, False)
    assert "no instance" in entry["error"]


def test_a_stopped_instance_receiving_visitors_leaves_its_web_address_not_answering_with_the_proxys_error() -> None:
    instance = RunningInstance(product="site", lane="testing", build="b1", container="site-testing-1", state=InstanceState.STOPPED)
    routes = {"testing.site.test": instance.container}
    entry = web_addresses_answer([instance], routes, lambda _: "502 Bad Gateway")["testing.site.test"]
    assert (entry["sends_to_container"], entry["answers"], entry["error"]) == (instance.container, False, "502 Bad Gateway")


def test_a_route_to_an_instance_that_is_gone_is_shown_not_dropped() -> None:
    entry = web_addresses_answer([], {"testing.gone.test": "gone-testing-1"}, lambda _: "502 Bad Gateway")["testing.gone.test"]
    assert (entry["product"], entry["sends_to_container"], entry["answers"]) == (None, "gone-testing-1", False)


def test_the_page_loads_without_reading_the_host_so_it_can_say_the_host_is_unreadable() -> None:
    def unreadable() -> list[RunningInstance]:
        raise AssertionError("Serving the page read the host")

    response = app_reading(unreadable).get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_web_addresses_are_asked_at_once_so_ones_that_hang_do_not_add_up() -> None:
    routes = {f"testing.site{number}.test": f"site{number}-testing" for number in range(3)}
    all_waiting = threading.Barrier(len(routes), timeout=5)

    def hang_until_every_web_address_is_asked(_web_address: str) -> str | None:
        all_waiting.wait()
        return "download timed out"

    entries = web_addresses_answer([], routes, hang_until_every_web_address_is_asked)
    assert {entry["error"] for entry in entries.values()} == {"download timed out"}
