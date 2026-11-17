import builtins
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import docker
from docker.errors import NotFound

from launchplane.data_set import MAKE_LOGIN, READ_LOGINS, READ_RULES, READY_COMMAND, REMOVE_LOGIN, RULES_AS_READ
from launchplane.docker_answers import files_in, tar_of

STARTED = "2026-11-15T20:00:00Z"
NOT_READY = 2
NO_SUCH_LOGIN = 3
NO_SUCH_DATABASE = 2


def as_docker_client(fake: object) -> docker.DockerClient:
    return cast("docker.DockerClient", fake)


@dataclass
class ExecResult:
    exit_code: int
    output: bytes


@dataclass
class RecordingServer:
    rules: object = field(default_factory=lambda: RULES_AS_READ)
    logins: set[str] = field(default_factory=set[str])
    ready: bool = False
    has_its_database: bool = True
    asked: list[tuple[str, dict[str, str]]] = field(default_factory=list[tuple[str, dict[str, str]]])

    def answer(self, script: str, variables: dict[str, str]) -> ExecResult:
        if READ_RULES.statements in script:
            return ExecResult(0, json.dumps(self.rules).encode())
        if READ_LOGINS.statements in script and not self.has_its_database:
            return ExecResult(NO_SUCH_DATABASE, b'FATAL:  database "app" does not exist')
        if READ_LOGINS.statements in script:
            return ExecResult(0, json.dumps(sorted(self.logins)).encode())
        if MAKE_LOGIN.statements in script:
            self.asked.append(("make", variables))
            self.logins.add(variables["login"])
            return ExecResult(0, b"")
        if REMOVE_LOGIN.statements in script:
            self.asked.append(("remove", variables))
            if variables["login"] not in self.logins:
                return ExecResult(NO_SUCH_LOGIN, f'ERROR:  role "{variables["login"]}" does not exist'.encode())
            self.logins.remove(variables["login"])
            return ExecResult(0, b"t")
        return ExecResult(NOT_READY, b"")


@dataclass
class RecordingContainer:
    name: str
    labels: dict[str, str]
    environment: list[str]
    files: dict[str, bytes]
    server: RecordingServer
    removed: bool = False
    status: str = "created"
    calls: list[str] = field(default_factory=list[str])
    restart_policy: str = ""

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
        if command == READY_COMMAND:
            return ExecResult(0 if self.server.ready else NOT_READY, b"")
        if command[:2] != ["sh", "-c"] or len(command) < 4:
            return ExecResult(NOT_READY, b"")
        variables = dict(option.removeprefix("--set=").split("=", 1) for option in command[4:])
        return self.server.answer(command[2], variables)


def has_labels(found: RecordingContainer, wanted: object) -> bool:
    labels = [wanted] if isinstance(wanted, str) else cast("list[str]", wanted or [])
    named = [label.partition("=") for label in labels]
    return all(found.labels.get(name) == value if separator else name in found.labels for name, separator, value in named)


@dataclass
class RecordingContainers:
    by_name: dict[str, RecordingContainer] = field(default_factory=dict[str, RecordingContainer])
    files_by_volume: dict[str, dict[str, bytes]] = field(default_factory=dict[str, dict[str, bytes]])
    servers_by_volume: dict[str, RecordingServer] = field(default_factory=dict[str, RecordingServer])

    def create(self, _image: str, _command: builtins.list[str] | None = None, **options: object) -> RecordingContainer:
        name = cast("str", options["name"])
        given = cast("dict[str, str]", options["environment"])
        volume = next(iter(cast("dict[str, object]", options["volumes"])), name)
        made = RecordingContainer(
            name,
            dict(cast("dict[str, str]", options["labels"])),
            [f"{key}={value}" for key, value in given.items()],
            self.files_by_volume.setdefault(volume, {}),
            self.servers_by_volume.setdefault(volume, RecordingServer()),
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
    system_times: Iterator[str] = field(default_factory=lambda: iter(()))

    @staticmethod
    def inspect_container(container: str) -> dict[str, Any]:
        state = {"Running": True, "Restarting": False, "Paused": False, "StartedAt": STARTED, "ExitCode": 0}
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
