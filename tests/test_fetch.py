import secrets

import pytest
from docker.errors import APIError, ImageNotFound

from launchplane.container_host import ContainerHost
from launchplane.docker_answers import ContainerHostRefused
from launchplane.instance import LaneBuild
from tests.recorded_git_host import RUN, build_of
from tests.recording_docker import RecordingDocker


@pytest.mark.parametrize("denial", ["", "http", "stream"])
def test_a_private_build_is_fetched_by_digest_or_refused_before_its_container_is_made(
    monkeypatch: pytest.MonkeyPatch, denial: str
) -> None:
    docker = RecordingDocker()
    reference = build_of(RUN)
    password = secrets.token_hex(16)
    monkeypatch.setenv("LAUNCHPLANE_REGISTRY", "ghcr.io")
    monkeypatch.setenv("LAUNCHPLANE_REGISTRY_USER", "reader")
    monkeypatch.setenv("LAUNCHPLANE_REGISTRY_PASSWORD", password)
    present: set[str] = set()
    pulled: list[str] = []

    def get(image: str) -> str:
        if image not in present:
            raise ImageNotFound(image)
        return image

    def pull(image: str, *, stream: bool, decode: bool, auth_config: dict[str, str]) -> list[dict[str, str]]:
        pulled.append(image)
        if denial == "http":
            raise APIError(f"registry denied access for {password}")
        if denial or auth_config != {"username": "reader", "password": password}:
            return [{"error": f"registry denied access for {password}"}]
        present.add(image)
        return []

    monkeypatch.setattr(docker.images, "get", get)
    monkeypatch.setattr(docker.api, "pull", pull, raising=False)
    host = ContainerHost(docker.as_client(), "scratch")
    target = LaneBuild(product="shop", lane="testing", build=reference)
    if denial:
        with pytest.raises(ContainerHostRefused, match="denied") as refusal:
            host.start_instance(target, "candidate", None)
        assert reference in str(refusal.value) and password not in str(refusal.value)
        assert not docker.containers.by_name
    else:
        host.start_instance(target, "candidate", None)
        host.images.ensure(reference)
        assert password not in repr(docker.containers.by_name["candidate"].environment)
    assert pulled == [reference]


def test_another_registry_never_receives_the_private_registry_login(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = RecordingDocker()
    monkeypatch.setenv("LAUNCHPLANE_REGISTRY", "ghcr.io")
    monkeypatch.setenv("LAUNCHPLANE_REGISTRY_USER", "reader")
    monkeypatch.setenv("LAUNCHPLANE_REGISTRY_PASSWORD", secrets.token_hex(16))
    supplied: list[dict[str, str]] = []

    def pull(_image: str, *, stream: bool, decode: bool, auth_config: dict[str, str]) -> list[dict[str, str]]:
        supplied.append(auth_config)
        return []

    monkeypatch.setattr(docker.api, "pull", pull, raising=False)
    host = ContainerHost(docker.as_client(), "scratch")
    host.images.fetch(build_of(RUN).replace("ghcr.io/", "ghcr.io.example/"))
    assert supplied == [{}]
