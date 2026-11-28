import base64
import json
import secrets
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import docker
import pytest

from launchplane import host_images
from launchplane.container_host import DOCKER_API_VERSION, ContainerHost
from launchplane.docker_answers import ContainerHostRefused
from launchplane.instance import LaneBuild
from launchplane.judge import Work
from launchplane.record import Asked, Record, Stands
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS
from tests.pretend_host import WEB_ADDRESS
from tests.recorded_git_host import RUN, build_of
from tests.test_updating import serving
from tests.test_worker import CALLER, kept_of


@dataclass
class Registry:
    password: str
    failure: str = ""
    present: set[str] = field(default_factory=set[str])
    pulls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    release: threading.Event = field(default_factory=threading.Event)


def handler(registry: Registry) -> type[BaseHTTPRequestHandler]:
    class Engine(BaseHTTPRequestHandler):
        def reply(self, status: int, body: object) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_GET(self) -> None:
            if self.path == "/version":
                self.reply(200, {"ApiVersion": DOCKER_API_VERSION})
                return
            image = unquote(urlsplit(self.path).path).split("/images/")[1].removesuffix("/json")
            if image in registry.present:
                self.reply(200, {"Id": image})
            else:
                self.reply(404, {"message": f"No such image: {image}"})

        def do_POST(self) -> None:
            image = parse_qs(urlsplit(self.path).query)["fromImage"][0]
            auth = self.headers.get("X-Registry-Auth")
            password = json.loads(base64.urlsafe_b64decode(auth))["password"] if auth else ""
            registry.pulls.append((image, password))
            if registry.failure == "stall":
                self.send_response(200)
                self.end_headers()
                registry.release.wait(5)
                return
            if registry.failure == "broken stream":
                self.send_response(200)
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b'{"status":"downloading"}\n')
                return
            if registry.failure == "absent":
                self.reply(200, {"status": "downloaded"})
                return
            if registry.failure or password != registry.password:
                reason = f"access denied for {registry.password}"
                self.reply(403 if registry.failure == "http" else 200, {"error": reason, "message": reason})
                return
            registry.present.add(image)
            self.reply(200, {"status": "downloaded"})

    return Engine


@pytest.fixture
def private_host(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[ContainerHost, Registry]]:
    registry = Registry(secrets.token_hex(16))
    monkeypatch.setenv("LAUNCHPLANE_REGISTRY", "ghcr.io")
    monkeypatch.setenv("LAUNCHPLANE_REGISTRY_USER", "reader")
    monkeypatch.setenv("LAUNCHPLANE_REGISTRY_PASSWORD", registry.password)
    with ThreadingHTTPServer(("127.0.0.1", 0), handler(registry)) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        address = f"http://127.0.0.1:{server.server_port}"
        client = docker.DockerClient(base_url=address, version=DOCKER_API_VERSION)
        try:
            yield ContainerHost(client, address), registry
        finally:
            registry.release.set()
            server.shutdown()
            thread.join()
            client.close()


@pytest.mark.parametrize(
    ("failure", "why"),
    [("http", "denied"), ("stream", "denied"), ("broken stream", "payload"), ("stall", "deadline"), ("absent", "no such image")],
)
def test_a_failed_fetch_ends_the_updating_move_with_its_reason_while_the_original_keeps_serving(
    private_host: tuple[ContainerHost, Registry], record: Record, failure: str, why: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, registry = private_host
    lanes, proxy, original = serving()
    registry.failure = failure
    proxy.host.prepare_build = host.prepare_build
    monkeypatch.setattr(host_images, "PULL_SECONDS", 0.25 if failure == "stall" else 3.0)
    image = build_of(RUN)
    request = record.write(Asked(product="site", lane="testing", work=Work.MOVE, build=image, run=RUN), CALLER, None)
    assert Worker(record, lanes, NO_SECRETS).work_next()
    kept = kept_of(record, request.id)
    assert kept.stands is Stands.FAILED and len(kept.attempts) == 1
    assert kept.error and image in kept.error and why in kept.error.lower() and registry.password not in kept.error
    assert registry.pulls == [(image, registry.password)]
    assert proxy.loaded_to(WEB_ADDRESS) == original and not proxy.host.actions


def test_a_verified_cold_image_is_pulled_once_and_checked_on_the_host(private_host: tuple[ContainerHost, Registry]) -> None:
    host, registry = private_host
    target = LaneBuild(product="site", lane="testing", build=build_of(RUN), published=True)
    host.prepare_build(target)
    host.prepare_build(target)
    assert registry.pulls == [(target.build, registry.password)]


@pytest.mark.parametrize(("domain", "published"), [("ghcr.io.example", True), ("ghcr.io", False)])
def test_only_a_verified_request_to_its_registry_gets_the_private_login(
    private_host: tuple[ContainerHost, Registry], domain: str, *, published: bool
) -> None:
    host, registry = private_host
    image = build_of(RUN).replace("ghcr.io/", f"{domain}/")
    with pytest.raises(ContainerHostRefused, match="denied"):
        host.prepare_build(LaneBuild(product="site", lane="testing", build=image, published=published))
    assert registry.pulls == [(image, "")]
