import json
from pathlib import Path
from typing import Any

import docker
import pytest
from fastapi.testclient import TestClient

from launchplane.api import CopyReader, create_app
from launchplane.container_host import DOCKER_API_VERSION, ContainerHost, ContainerHostError, copies_from_answer
from launchplane.move import MoveResult
from launchplane.running_copy import CopyState, LaneCopy, RunningCopy

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


def no_move(target: LaneCopy) -> MoveResult:
    raise AssertionError(f"Reading what is running asked for a move to {target.build}")


def app_reading(read: CopyReader, serving: dict[str, str] | None = None) -> TestClient:
    return TestClient(create_app(read, lambda: serving or {}, no_move))


def ask_running(read: CopyReader) -> tuple[int, dict[str, Any]]:
    response = app_reading(read).get("/running")
    return response.status_code, response.json()


def test_a_saved_docker_answer_reads_as_copies_in_launchplane_terms() -> None:
    copies = {copy.name: copy for copy in copies_from_answer(saved_container_list())}
    assert copies == {
        "site-production": RunningCopy(
            product="site", lane="production", build="b2", name="site-production", state=CopyState.RUNNING
        ),
        "site-testing": RunningCopy(product="site", lane="testing", build="b3", name="site-testing", state=CopyState.STOPPED),
        "site-preview-7": RunningCopy(product="site", lane="preview-7", build="b4", name="site-preview-7", state=CopyState.PAUSED),
        "site-unmarked-lane": RunningCopy(product="site", lane=None, build=None, name="site-unmarked-lane", state=CopyState.RUNNING),
    }


@pytest.mark.parametrize("answer", UNEXPECTED_ANSWERS.values(), ids=UNEXPECTED_ANSWERS.keys())
def test_an_answer_of_unexpected_shape_is_an_error_not_an_empty_list(answer: object) -> None:
    with pytest.raises(ContainerHostError, match="Docker"):
        copies_from_answer(answer)


def test_a_refused_connection_raises_with_the_real_error_instead_of_listing_nothing() -> None:
    with pytest.raises(ContainerHostError, match="Connection refused"):
        refusing_host().read_copies()


def test_the_running_call_answers_unavailable_with_the_real_error_when_the_host_cannot_be_read() -> None:
    status, body = ask_running(refusing_host().read_copies)
    assert status == 503
    assert "Connection refused" in body["error"]
    assert "copies" not in body


def test_the_running_call_reads_the_host_on_every_request() -> None:
    answers = iter([[], copies_from_answer(saved_container_list())])
    client = app_reading(lambda: next(answers))
    assert client.get("/running").json()["copies"] == []
    assert len(client.get("/running").json()["copies"]) == 4


def test_the_running_call_names_the_copy_the_proxy_points_at_and_no_other() -> None:
    copies = copies_from_answer(saved_container_list())
    body = app_reading(lambda: copies, serving={"production.site.test": "site-production"}).get("/running").json()
    assert {copy["name"]: copy["serves"] for copy in body["copies"]} == {
        "site-production": "production.site.test",
        "site-testing": None,
        "site-preview-7": None,
        "site-unmarked-lane": None,
    }
