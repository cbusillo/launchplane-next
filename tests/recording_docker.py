import builtins
import io
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import docker
from docker.errors import NotFound

from launchplane.docker_answers import only_file_in, tar_of

STARTED = "2026-11-15T20:00:00Z"
IMAGE_ENVIRONMENT = ("PGDATA=/var/lib/postgresql/18/docker",)
NOT_READY = 2


def as_docker_client(fake: object) -> docker.DockerClient:
    return cast("docker.DockerClient", fake)


@dataclass
class ExecResult:
    exit_code: int
    output: bytes


@dataclass
class RecordingContainer:
    name: str
    labels: dict[str, str]
    environment: list[str]
    files: dict[str, bytes]
    removed: bool = False
    refuses_reads_with: Iterator[Exception] = field(default_factory=lambda: iter(()))
    calls: list[str] = field(default_factory=list[str])
    file_modes: dict[str, int] = field(default_factory=dict[str, int])

    def get_archive(self, path: str) -> tuple[list[bytes], dict[str, Any]]:
        self.calls.append(f"read {path}")
        for refusal in self.refuses_reads_with:
            raise refusal
        if path not in self.files:
            raise NotFound(f"Could not find the file {path} in container {self.name}")
        return [tar_of(path.rsplit("/", 1)[1], self.files[path])], {}

    def put_archive(self, folder: str, archive: bytes) -> None:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            [member] = tar.getmembers()
        path = f"{folder}/{member.name}"
        self.calls.append(f"write {path}")
        self.file_modes[path] = member.mode
        self.files[path] = only_file_in(archive)

    @property
    def attrs(self) -> dict[str, Any]:
        state = {"Running": True, "Restarting": False, "Paused": False, "StartedAt": STARTED}
        return {"Name": f"/{self.name}", "Config": {"Env": self.environment}, "State": state}

    def logs(self, tail: int) -> bytes:
        self.calls.append(f"logs {tail}")
        return b""

    def start(self) -> None:
        self.calls.append("start")

    def exec_run(self, command: list[str]) -> ExecResult:
        self.calls.append(" ".join(command))
        return ExecResult(NOT_READY, b"")


@dataclass
class RecordingContainers:
    by_name: dict[str, RecordingContainer] = field(default_factory=dict[str, RecordingContainer])
    files_by_volume: dict[str, dict[str, bytes]] = field(default_factory=dict[str, dict[str, bytes]])

    def create(self, _image: str, _command: builtins.list[str] | None = None, **options: object) -> RecordingContainer:
        name = cast("str", options["name"])
        given = cast("dict[str, str]", options["environment"])
        volume = next(iter(cast("dict[str, object]", options["volumes"])), name)
        made = RecordingContainer(
            name,
            dict(cast("dict[str, str]", options["labels"])),
            [*(f"{key}={value}" for key, value in given.items()), *IMAGE_ENVIRONMENT],
            self.files_by_volume.setdefault(volume, {}),
        )
        self.by_name[name] = made
        return made

    def get(self, name: str) -> RecordingContainer:
        if name not in self.by_name or self.by_name[name].removed:
            raise NotFound(f"No such container: {name}")
        return self.by_name[name]

    def list(self, **_options: object) -> builtins.list[RecordingContainer]:
        return [found for found in self.by_name.values() if not found.removed]


@dataclass
class RecordingNetworks:
    made: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])

    def get(self, name: str) -> object:
        if name not in self.made:
            raise NotFound(f"network {name} not found")
        return type("Network", (), {"attrs": self.made[name]})()

    def create(self, name: str, **options: object) -> None:
        self.made[name] = {"Name": name, "Internal": options.get("internal", False), "Options": options.get("options"), "Labels": {}}


class RecordingImages:
    @staticmethod
    def get(image: str) -> str:
        return image


@dataclass
class RecordingApi:
    system_times: Iterator[str] = field(default_factory=lambda: iter(()))

    @staticmethod
    def inspect_container(container: str) -> dict[str, Any]:
        state = {"Running": True, "Restarting": False, "Paused": False, "StartedAt": STARTED}
        return {"Name": f"/{container}", "Config": {"Env": []}, "State": state}

    def info(self) -> dict[str, Any]:
        return {"SystemTime": next(self.system_times)}


@dataclass
class RecordingDocker:
    containers: RecordingContainers = field(default_factory=RecordingContainers)
    networks: RecordingNetworks = field(default_factory=RecordingNetworks)
    images: RecordingImages = field(default_factory=RecordingImages)
    api: RecordingApi = field(default_factory=RecordingApi)

    def as_client(self) -> docker.DockerClient:
        return as_docker_client(self)
