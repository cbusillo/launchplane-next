import builtins
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import docker
from docker.errors import NotFound


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
    log: bytes = b""
    status: str = "running"
    ready: bool = True
    removed: bool = False
    calls: builtins.list[str] = field(default_factory=builtins.list[str])

    @property
    def attrs(self) -> dict[str, Any]:
        return {
            "Name": f"/{self.name}",
            "Config": {"Env": self.environment},
            "State": {"Running": True, "StartedAt": "2026-11-15T20:00:00Z"},
        }

    def logs(self, tail: int) -> bytes:
        self.calls.append(f"logs {tail}")
        return self.log

    def start(self) -> None:
        self.status = "running"

    def stop(self, timeout: int) -> None:
        self.calls.append(f"stop within {timeout}")
        self.status = "exited"

    def remove(self, v: bool = False, force: bool = False) -> None:
        self.calls.append(f"remove volumes={v} force={force}")
        self.removed = True

    def exec_run(self, command: builtins.list[str]) -> ExecResult:
        self.calls.append(" ".join(command))
        return ExecResult(0 if self.ready else 2, b"")


def labels_match(labels: dict[str, str], wanted: str | list[str]) -> bool:
    for each in [wanted] if isinstance(wanted, str) else wanted:
        key, _, value = each.partition("=")
        if key not in labels or (value and labels[key] != value):
            return False
    return True


@dataclass
class RecordingContainers:
    runs: builtins.list[dict[str, Any]] = field(default_factory=builtins.list[dict[str, Any]])
    by_name: dict[str, RecordingContainer] = field(default_factory=dict[str, RecordingContainer])
    listed: builtins.list[bool] = field(default_factory=builtins.list[bool])

    def run(self, image: str, command: builtins.list[str] | None = None, **options: object) -> RecordingContainer:
        self.runs.append({"image": image, "command": command, **options})
        given = cast("dict[str, str]", options.get("environment") or {})
        labels = cast("dict[str, str]", options.get("labels") or {})
        made = RecordingContainer(cast("str", options["name"]), dict(labels), [f"{key}={value}" for key, value in given.items()])
        self.by_name[made.name] = made
        return made

    def get(self, name: str) -> RecordingContainer:
        if name not in self.by_name or self.by_name[name].removed:
            raise NotFound(f"No such container: {name}")
        return self.by_name[name]

    def list(self, **options: object) -> builtins.list[RecordingContainer]:
        self.listed.append(options.get("all") is True)
        wanted = cast("dict[str, str | builtins.list[str]]", options.get("filters") or {}).get("label", [])
        return [found for found in self.by_name.values() if not found.removed and labels_match(found.labels, wanted)]


@dataclass
class RecordingNetworks:
    made: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def get(self, name: str) -> object:
        found = next((network for network in self.made if network["Name"] == name), None)
        if found is None:
            raise NotFound(f"network {name} not found")
        return type("Network", (), {"attrs": found})()

    def create(
        self,
        name: str,
        driver: str,
        internal: bool = False,
        options: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        self.made.append({"Name": name, "Driver": driver, "Internal": internal, "Options": options, "Labels": labels or {}})


@dataclass
class RecordingVolumes:
    made: list[str] = field(default_factory=list[str])

    labels: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])

    def create(self, name: str, labels: dict[str, str] | None = None) -> None:
        self.made.append(name)
        self.labels[name] = labels or {}


@dataclass
class RecordingImages:
    asked: list[str] = field(default_factory=list[str])

    def get(self, image: str) -> str:
        self.asked.append(image)
        return image


@dataclass
class RecordingApi:
    started_at: dict[str, str] = field(default_factory=dict[str, str])
    running: dict[str, bool] = field(default_factory=dict[str, bool])
    system_times: Iterator[str] = field(default_factory=lambda: iter(()))

    def inspect_container(self, container: str) -> dict[str, Any]:
        state = {"Running": self.running.get(container, True), "StartedAt": self.started_at[container]}
        return {"Name": f"/{container}", "Config": {"Env": []}, "State": state}

    def info(self) -> dict[str, Any]:
        return {"SystemTime": next(self.system_times)}


@dataclass
class RecordingDocker:
    containers: RecordingContainers = field(default_factory=RecordingContainers)
    networks: RecordingNetworks = field(default_factory=RecordingNetworks)
    volumes: RecordingVolumes = field(default_factory=RecordingVolumes)
    images: RecordingImages = field(default_factory=RecordingImages)
    api: RecordingApi = field(default_factory=RecordingApi)

    def as_client(self) -> docker.DockerClient:
        return as_docker_client(self)

    def last_run(self) -> dict[str, Any]:
        return self.containers.runs[-1]
