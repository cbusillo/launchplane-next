import builtins
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import docker
from docker.errors import NotFound

from launchplane.docker_answers import files_in, tar_of

STARTED = "2026-11-15T20:00:00Z"
NOT_READY = 2


def as_docker_client(fake: object) -> docker.DockerClient:
    return cast("docker.DockerClient", fake)


@dataclass
class ExecResult:
    exit_code: int
    output: bytes = b""


@dataclass
class RecordingContainer:
    name: str
    labels: dict[str, str]
    environment: list[str]
    files: dict[str, bytes]
    removed: bool = False
    status: str = "created"
    calls: list[str] = field(default_factory=list[str])
    restart_policy: str = ""
    # What each command run in it answers, in the order they are run; a command past the end is refused.
    answers: list[ExecResult] = field(default_factory=list[ExecResult])

    def get_archive(self, path: str) -> tuple[list[bytes], dict[str, Any]]:
        if path not in self.files:
            raise NotFound(f"Could not find the file {path} in container {self.name}")
        return [tar_of(path.rsplit("/", 1)[1], self.files[path])], {}

    def put_archive(self, folder: str, archive: bytes) -> None:
        for name, (content, _changed_at) in files_in(archive).items():
            self.calls.append(f"write {folder}/{name}")
            self.files[f"{folder}/{name}"] = content

    @property
    def attrs(self) -> dict[str, Any]:
        state = {"Running": True, "Restarting": False, "Paused": False, "StartedAt": STARTED, "ExitCode": 0}
        return {"Name": f"/{self.name}", "Config": {"Env": self.environment}, "State": state}

    @property
    def listed(self) -> dict[str, Any]:
        return {
            "Names": [f"/{self.name}"],
            "Image": "image",
            "State": self.status,
            "Labels": self.labels,
            "NetworkSettings": {"Networks": {}},
            "Mounts": [],
        }

    def logs(self, tail: int) -> bytes:
        self.calls.append(f"logs {tail}")
        return b""

    def start(self) -> None:
        self.status = "running"
        self.calls.append("start")

    def stop(self, timeout: int) -> None:
        self.status = "exited"
        self.calls.append(f"stop within {timeout} s")

    def remove(self, force: bool = False) -> None:
        self.removed = True
        self.calls.append("remove by force" if force else "remove")

    def exec_run(self, command: list[str]) -> ExecResult:
        self.calls.append(" ".join(command))
        return self.answers.pop(0) if self.answers else ExecResult(NOT_READY)


def has_labels(found: RecordingContainer, wanted: object) -> bool:
    labels = [wanted] if isinstance(wanted, str) else cast("list[str]", wanted or [])
    named = [label.partition("=") for label in labels]
    return all(found.labels.get(name) == value if separator else name in found.labels for name, separator, value in named)


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
            [f"{key}={value}" for key, value in given.items()],
            self.files_by_volume.setdefault(volume, {}),
            restart_policy=cast("dict[str, str]", options["restart_policy"])["Name"],
        )
        self.by_name[name] = made
        return made

    def get(self, name: str) -> RecordingContainer:
        if name not in self.by_name or self.by_name[name].removed:
            raise NotFound(f"No such container: {name}")
        return self.by_name[name]

    def list(self, **options: object) -> builtins.list[RecordingContainer]:
        filters = cast("dict[str, object]", options.get("filters") or {})
        status = filters.get("status")
        return [
            found
            for found in self.by_name.values()
            if not found.removed and has_labels(found, filters.get("label")) and status in (None, found.status)
        ]


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
    made: RecordingContainers
    system_times: Iterator[str] = field(default_factory=lambda: iter(()))

    def containers(self, **_options: object) -> list[dict[str, Any]]:
        return [found.listed for found in self.made.list()]

    @staticmethod
    def networks() -> list[object]:
        return []

    @staticmethod
    def volumes(**_options: object) -> dict[str, object]:
        return {"Volumes": []}

    def inspect_container(self, container: str) -> dict[str, Any]:
        if container in self.made.by_name:
            return self.made.by_name[container].attrs
        state = {"Running": True, "Restarting": False, "Paused": False, "StartedAt": STARTED, "ExitCode": 0}
        return {"Name": f"/{container}", "Config": {"Env": []}, "State": state}

    def info(self) -> dict[str, Any]:
        return {"SystemTime": next(self.system_times)}


class RecordingDocker:
    def __init__(self) -> None:
        self.containers = RecordingContainers()
        self.networks = RecordingNetworks()
        self.images = RecordingImages()
        self.api = RecordingApi(self.containers)

    def as_client(self) -> docker.DockerClient:
        return as_docker_client(self)
