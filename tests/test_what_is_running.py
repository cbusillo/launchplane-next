import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import docker
import pytest
from fastapi.testclient import TestClient

from launchplane.act import Status
from launchplane.api import create_app
from launchplane.container_host import DOCKER_API_VERSION, ContainerHost
from launchplane.docker_answers import ContainerHostError, Listed, container_summaries, on_host_from
from launchplane.instance import (
    BACK_SOON,
    LANE_NETWORK,
    InstanceState,
    LoadedRoute,
    OnHost,
    ProxySeen,
    Reading,
    RouteStatus,
    RunningInstance,
    WrittenRoute,
)
from launchplane.judge import Standing, judge, web_addresses_judged
from launchplane.product import Product
from tests.given_secrets import NO_SECRETS
from tests.pretend_host import SINCE, LanesNeverAsked
from tests.pretend_record import PretendRecord

SAVED_CONTAINER_LIST = Path(__file__).parent / "answers" / "container-list.json"


NO_NETWORKS: dict[str, object] = {"NetworkSettings": {"Networks": {}}, "Mounts": []}
SITE = {"launchplane.product": "site"}
UNEXPECTED_ANSWERS: dict[str, object] = {
    "an object instead of a list": {"message": "page not found"},
    "a container without a state": [{"Names": ["/site"], "Image": "site:1", "Labels": SITE} | NO_NETWORKS],
    "a state Docker never had": [{"Names": ["/site"], "Image": "site:1", "State": "hibernating", "Labels": SITE} | NO_NETWORKS],
    "a container the filter should not have let through": [
        {"Names": ["/site"], "Image": "site:1", "State": "running", "Labels": {}} | NO_NETWORKS
    ],
    "a container without a name": [{"Names": [], "Image": "site:1", "State": "running", "Labels": SITE} | NO_NETWORKS],
    "a container without its networks": [{"Names": ["/site"], "Image": "site:1", "State": "running", "Labels": SITE, "Mounts": []}],
    "a container without its mounts": [
        {"Names": ["/site"], "Image": "site:1", "State": "running", "Labels": SITE, "NetworkSettings": {"Networks": {}}}
    ],
}


def saved_container_list() -> object:
    return json.loads(SAVED_CONTAINER_LIST.read_text())


def instances_from_answer(answer: object) -> list[RunningInstance]:
    return on_host_from(Listed(container_summaries(answer), {}), [], {"Volumes": None}, None).instances


def refusing_host() -> ContainerHost:
    client = docker.DockerClient(base_url="tcp://127.0.0.1:1", version=DOCKER_API_VERSION, timeout=5)
    return ContainerHost(client, "tcp://127.0.0.1:1")


def no_wake() -> None:
    raise AssertionError("Reading what is running woke the worker")


type InstanceReader = Callable[[], list[RunningInstance]]


def proxy_sending(routes: dict[str, str] | None) -> ProxySeen:
    loaded = {web_address: LoadedRoute(container, RouteStatus.ENABLED) for web_address, container in (routes or {}).items()}
    written = {web_address: WrittenRoute(route.container, SINCE) for web_address, route in loaded.items()}
    return ProxySeen(runs=True, back_soon_runs=True, written=written, loaded=loaded)


def status_of(instances: list[RunningInstance], proxy: ProxySeen, failures: Mapping[str, str | None] | None = None) -> Status:
    answered: dict[str, str | None] = {**dict.fromkeys(proxy.sends_visitors_to()), **(failures or {})}
    reading = Reading(SINCE, proxy, OnHost(instances, [], []), answered)
    judged = [judge(reading.lane(product, lane), Product(), None) for product, lane in reading.lanes()]
    return Status(reading, judged, web_addresses_judged(reading))


def app_reading(
    read: InstanceReader, routes: dict[str, str] | None = None, failures: Mapping[str, str | None] | None = None
) -> TestClient:
    def read_status() -> Status:
        return status_of(read(), proxy_sending(routes), failures)

    return TestClient(create_app(read_status, PretendRecord(), no_wake, NO_SECRETS, LanesNeverAsked()))


def ask_running(read: InstanceReader) -> tuple[int, dict[str, Any]]:
    response = app_reading(read).get("/running")
    return response.status_code, response.json()


def test_a_saved_docker_answer_reads_as_instances_in_launchplane_terms() -> None:
    rows = [
        ("site-production", "production", "b2", InstanceState.RUNNING),
        ("site-testing", "testing", "b3", InstanceState.STOPPED),
        ("site-preview-7", "preview-7", "b4", InstanceState.PAUSED),
        ("site-unmarked-lane", None, None, InstanceState.RUNNING),
    ]
    instances = {instance.container: instance for instance in instances_from_answer(saved_container_list())}
    assert instances == {
        container: RunningInstance(product="site", lane=lane, build=build, container=container, state=state, networks=("bridge",))
        for container, lane, build, state in rows
    }


@pytest.mark.parametrize("answer", UNEXPECTED_ANSWERS.values(), ids=UNEXPECTED_ANSWERS.keys())
def test_an_answer_of_unexpected_shape_is_an_error_not_an_empty_list(answer: object) -> None:
    with pytest.raises(ContainerHostError, match="Docker"):
        instances_from_answer(answer)


def test_a_refused_connection_raises_with_the_real_error_instead_of_listing_nothing() -> None:
    with pytest.raises(ContainerHostError, match="Connection refused"):
        refusing_host().read()


def test_an_unreadable_host_answers_unavailable_with_the_real_error() -> None:
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
    instances: list[RunningInstance], routes: dict[str, str], failures: Mapping[str, str | None]
) -> dict[str, dict[str, Any]]:
    body = app_reading(lambda: instances, routes, failures).get("/running").json()
    return {entry["web_address"]: entry for entry in body["web_addresses"]}


def lane_instance(state: InstanceState = InstanceState.RUNNING) -> RunningInstance:
    return RunningInstance(
        product="site", lane="testing", build="b1", container="site-testing-1", state=state, networks=(LANE_NETWORK,)
    )


def test_a_stopped_active_instance_leaves_its_web_address_not_answering() -> None:
    routes = {"testing.site.test": "site-testing-1"}
    entry = web_addresses_answer([lane_instance(InstanceState.STOPPED)], routes, {"testing.site.test": "502 Bad Gateway"})[
        "testing.site.test"
    ]
    assert (entry["sends_to_container"], entry["answers"], entry["error"]) == (
        "site-testing-1",
        False,
        "testing.site.test does not answer with success: 502 Bad Gateway",
    )


def test_a_web_address_that_was_routed_and_not_asked_is_not_said_to_answer() -> None:
    status = status_of([lane_instance()], proxy_sending({"testing.site.test": "site-testing-1"}))
    unasked = Status(Reading(SINCE, status.reading.proxy, status.reading.on_host), [], [])
    [entry] = web_addresses_judged(unasked.reading)
    assert (entry.answers, entry.standing, entry.error) == (
        False,
        Standing.UNREAD,
        "testing.site.test was not asked, so whether it answers is unknown.",
    )


def test_a_route_to_an_instance_that_is_gone_is_shown_not_dropped() -> None:
    entry = web_addresses_answer([], {"testing.gone.test": "gone-testing-1"}, {"testing.gone.test": "502 Bad Gateway"})[
        "testing.gone.test"
    ]
    assert (entry["product"], entry["sends_to_container"], entry["answers"]) == (None, "gone-testing-1", False)


def test_the_page_loads_without_reading_the_host_so_it_can_say_the_host_is_unreadable() -> None:
    def unreadable() -> list[RunningInstance]:
        raise AssertionError("Serving the page read the host")

    response = app_reading(unreadable).get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_a_web_address_in_maintenance_says_it_shows_the_back_soon_page() -> None:
    body = app_reading(lambda: [lane_instance(InstanceState.STOPPED)], {"testing.site.test": BACK_SOON}).get("/running").json()
    [entry] = body["web_addresses"]
    assert (entry["product"], entry["lane"], entry["shows_back_soon_page"], entry["answers"], entry["error"]) == (
        "site",
        "testing",
        True,
        False,
        None,
    )
    assert [instance["receives_visitors_for"] for instance in body["instances"]] == [None]


def test_the_page_is_given_every_row_with_its_wording_and_colour_so_its_script_decides_none() -> None:
    attached = lane_instance().model_copy(update={"networks": (LANE_NETWORK, "bridge")})
    body = app_reading(lambda: [attached], {"testing.site.test": "site-testing-1"}).get("/running").json()
    [card] = body["cards"]
    drawn = [(row["name"], [(pill["text"], pill["tone"]) for pill in row["pills"]]) for row in card["rows"]]
    assert (card["title"], card["pills"]) == ("site · testing", [{"text": "wrong", "tone": "wrong"}])
    assert drawn == [
        ("Web address", [("answering", "good")]),
        ("Found", [("wrong", "wrong")]),
        ("Found", [("could not be read", "could not be read")]),
        ("Instance 1", [("running", "wrong"), ("active", "good")]),
    ]
    assert card["rows"][1]["value"] == [
        {"text": "The instance site-testing-1 is attached to bridge, which it must not be", "drawn_as": "text"}
    ]


def test_a_host_with_nothing_marked_on_it_gives_the_page_one_card_that_says_so() -> None:
    assert [card["title"] for card in ask_running(list)[1]["cards"]] == [
        "The host answered, and nothing marked for Launchplane is on it."
    ]
