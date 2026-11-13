import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from http.server import ThreadingHTTPServer

import pytest

from launchplane.api import CopyReader, handler_for
from launchplane.container_host import ContainerHostError, RunningCopy, read_copies

type ServeRunning = Callable[[CopyReader], str]

UNREACHABLE_SSH = ("sh", "-c", 'echo "ssh: connect to host scratch port 22: Connection timed out" >&2; exit 255', "ssh")
DOCKER_REFUSING_SSH = ("sh", "-c", r'printf "HTTP/1.0 500 Internal Server Error\r\n\r\n{\"message\":\"disk full\"}"', "ssh")


def unreachable_host() -> list[RunningCopy]:
    return read_copies("scratch", ssh=UNREACHABLE_SSH)


@pytest.fixture
def serve_running() -> Iterator[ServeRunning]:
    servers: list[ThreadingHTTPServer] = []

    def start(read: CopyReader) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(read, "scratch"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}/running"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_an_unreachable_host_raises_with_the_real_error_instead_of_listing_nothing() -> None:
    with pytest.raises(ContainerHostError, match="Connection timed out"):
        unreachable_host()


def test_a_docker_error_answer_raises_with_dockers_message_instead_of_listing_nothing() -> None:
    with pytest.raises(ContainerHostError, match="disk full"):
        read_copies("scratch", ssh=DOCKER_REFUSING_SSH)


def test_the_running_call_answers_unavailable_with_the_real_error_when_the_host_cannot_be_read(serve_running: ServeRunning) -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(serve_running(unreachable_host))  # noqa: S310
    body = json.loads(raised.value.read())
    assert raised.value.code == 503
    assert "Connection timed out" in body["error"]
    assert "copies" not in body


def test_the_running_call_reads_the_host_on_every_request(serve_running: ServeRunning) -> None:
    answers = iter([[], [RunningCopy("site", "testing", "abc123", "running", "Up 1 second", "site-testing", "busybox")]])
    url = serve_running(lambda: next(answers))
    first = json.loads(urllib.request.urlopen(url).read())  # noqa: S310
    second = json.loads(urllib.request.urlopen(url).read())  # noqa: S310
    assert first["copies"] == []
    assert second["copies"][0]["lane"] == "testing"
