import secrets

import pytest
from docker.errors import APIError, ImageNotFound

from launchplane.container_host import ContainerHost
from launchplane.docker_answers import ContainerHostRefused
from launchplane.instance import LaneBuild
from tests.recorded_git_host import RUN, build_of
from tests.recording_docker import RecordingApi, RecordingDocker


class PrivateRegistry(RecordingApi):
    def __init__(self, docker: RecordingDocker, password: str) -> None:
        super().__init__(docker.containers)
        self.login = {"username": "reader", "password": password}
        self.denial = ""
        self.present: set[str] = set()
        self.pulls: list[tuple[str, dict[str, str]]] = []
        docker.images.get = self.get
        docker.api = self

    def get(self, image: str) -> str:
        if image not in self.present:
            raise ImageNotFound(image)
        return image

    def pull(self, image: str, *, stream: bool, decode: bool, auth_config: dict[str, str]) -> list[dict[str, str]]:
        assert stream and decode
        self.pulls.append((image, auth_config))
        reason = f"registry denied access for {self.login['password']}"
        if self.denial == "http":
            raise APIError(reason)
        if self.denial or auth_config != self.login:
            return [{"error": reason}]
        self.present.add(image)
        return []


@pytest.fixture
def private_host(monkeypatch: pytest.MonkeyPatch) -> tuple[RecordingDocker, PrivateRegistry]:
    docker = RecordingDocker()
    password = secrets.token_hex(16)
    monkeypatch.setenv("LAUNCHPLANE_REGISTRY", "ghcr.io")
    monkeypatch.setenv("LAUNCHPLANE_REGISTRY_USER", "reader")
    monkeypatch.setenv("LAUNCHPLANE_REGISTRY_PASSWORD", password)
    return docker, PrivateRegistry(docker, password)


@pytest.mark.parametrize("denial", ["", "http", "stream"])
def test_a_private_build_is_fetched_by_digest_or_refused_before_its_container_is_made(
    private_host: tuple[RecordingDocker, PrivateRegistry], denial: str
) -> None:
    docker, registry = private_host
    host = ContainerHost(docker.as_client(), "scratch")
    target = LaneBuild(product="shop", lane="testing", build=build_of(RUN), published=True)
    registry.denial = denial
    if denial:
        with pytest.raises(ContainerHostRefused, match="denied") as refusal:
            host.start_instance(target, "candidate", None)
        assert target.build in str(refusal.value) and registry.login["password"] not in str(refusal.value)
        assert not docker.containers.by_name
    else:
        host.start_instance(target, "candidate", None)
        host.images.ensure(target.build)
        assert registry.login["password"] not in repr(docker.containers.by_name["candidate"].environment)
    assert registry.pulls == [(target.build, registry.login)]


@pytest.mark.parametrize(("domain", "published"), [("ghcr.io.example", True), ("ghcr.io", False)])
def test_only_a_verified_request_to_its_registry_gets_the_private_login(
    private_host: tuple[RecordingDocker, PrivateRegistry], domain: str, *, published: bool
) -> None:
    docker, registry = private_host
    image = build_of(RUN).replace("ghcr.io/", f"{domain}/")
    host = ContainerHost(docker.as_client(), "scratch")
    with pytest.raises(ContainerHostRefused, match="denied"):
        host.images.ensure(image, published=published)
    assert registry.pulls == [(image, {})]
