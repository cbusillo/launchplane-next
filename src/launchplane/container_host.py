import io
import os
import tarfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import docker
import requests
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.tls import TLSConfig
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from launchplane.running_copy import CopyState, LaneCopy, RunningCopy

PRODUCT_LABEL = "launchplane.product"
LANE_LABEL = "launchplane.lane"
BUILD_LABEL = "launchplane.build"

DOCKER_API_VERSION = "1.47"
ANSWER_TIMEOUT_SECONDS = 5
LANE_NETWORK = "launchplane-lanes"
RETIRE_GRACE_SECONDS = 10
LOG_TAIL_LINES = 20

type DockerState = Literal["created", "running", "paused", "restarting", "removing", "exited", "dead"]

COPY_STATE_FOR_DOCKER_STATE: dict[DockerState, CopyState] = {
    "created": CopyState.STOPPED,
    "running": CopyState.RUNNING,
    "paused": CopyState.PAUSED,
    "restarting": CopyState.RESTARTING,
    "removing": CopyState.STOPPED,
    "exited": CopyState.STOPPED,
    "dead": CopyState.STOPPED,
}


class ContainerHostError(Exception):
    pass


@dataclass(frozen=True)
class Service:
    name: str
    image: str
    command: list[str]
    ports: dict[int, int]
    volumes: dict[str, str]


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


class DockerContainerSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    names: list[str] = Field(alias="Names", min_length=1)
    state: DockerState = Field(alias="State")
    labels: dict[str, str] = Field(alias="Labels")


DOCKER_CONTAINER_LIST = TypeAdapter(list[DockerContainerSummary])


def copy_from(summary: DockerContainerSummary) -> RunningCopy:
    if PRODUCT_LABEL not in summary.labels:
        raise ContainerHostError(f"Docker listed {summary.names[0]} for the {PRODUCT_LABEL} filter, but it has no such label.")
    return RunningCopy(
        product=summary.labels[PRODUCT_LABEL],
        lane=summary.labels.get(LANE_LABEL),
        build=summary.labels.get(BUILD_LABEL),
        name=summary.names[0].removeprefix("/"),
        state=COPY_STATE_FOR_DOCKER_STATE[summary.state],
    )


def copies_from_answer(answer: object) -> list[RunningCopy]:
    try:
        summaries = DOCKER_CONTAINER_LIST.validate_python(answer)
    except ValidationError as error:
        raise ContainerHostError(f"Docker's container list did not have the expected shape, so nothing was read: {error}") from error
    return [copy_from(summary) for summary in summaries]


class ContainerHost:
    def __init__(self, client: docker.DockerClient, address: str) -> None:
        self.client = client
        self.address = address

    @classmethod
    def from_environment(cls) -> ContainerHost:
        address = os.environ["LAUNCHPLANE_CONTAINER_HOST"]
        credentials = Path(os.environ["LAUNCHPLANE_CONTAINER_HOST_CREDENTIALS"])
        tls = TLSConfig(
            client_cert=(str(credentials / "cert.pem"), str(credentials / "key.pem")),
            ca_cert=str(credentials / "ca.pem"),
            verify=True,
        )
        client = docker.DockerClient(base_url=address, tls=tls, version=DOCKER_API_VERSION, timeout=ANSWER_TIMEOUT_SECONDS)
        return cls(client, address)

    def list_marked_containers(self) -> object:
        return self.client.api.containers(all=True, filters={"label": PRODUCT_LABEL})  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    @contextmanager
    def attempting(self, action: str) -> Generator[None]:
        try:
            yield
        except (DockerException, requests.RequestException, ValueError) as error:
            raise ContainerHostError(f"{action} on {self.address} failed: {error}") from error

    def read_copies(self) -> list[RunningCopy]:
        with self.attempting(f"Listing the containers marked {PRODUCT_LABEL}"):
            answer = self.list_marked_containers()
        return copies_from_answer(answer)

    def find_container(self, name: str) -> Container | None:
        try:
            return self.client.containers.get(name)
        except NotFound:
            return None

    def ensure_image(self, image: str) -> None:
        try:
            self.client.images.get(image)
        except ImageNotFound:
            self.client.images.pull(image)

    def ensure_lane_network(self) -> None:
        try:
            self.client.networks.get(LANE_NETWORK)
        except NotFound:
            self.client.networks.create(LANE_NETWORK, driver="bridge")

    def start_copy(self, copy: LaneCopy) -> None:
        with self.attempting(f"Starting {copy.name} from build {copy.build}"):
            self.ensure_lane_network()
            self.ensure_image(copy.build)
            self.client.containers.run(
                copy.build,
                name=copy.name,
                detach=True,
                labels={PRODUCT_LABEL: copy.product, LANE_LABEL: copy.lane, BUILD_LABEL: copy.build},
                network=LANE_NETWORK,
                restart_policy={"Name": "always"},
            )

    def remove_copy(self, name: str) -> None:
        with self.attempting(f"Removing {name}"):
            container = self.find_container(name)
            if container is not None:
                container.remove(force=True)

    def retire_copy(self, name: str) -> None:
        with self.attempting(f"Stopping and removing {name}"):
            container = self.find_container(name)
            if container is not None:
                container.stop(timeout=RETIRE_GRACE_SECONDS)
                container.remove()

    def read_log_tail(self, name: str) -> str:
        with self.attempting(f"Reading the log of {name}"):
            container = self.find_container(name)
            if container is None:
                return ""
            return container.logs(tail=LOG_TAIL_LINES).decode(errors="replace")

    def ensure_service(self, service: Service) -> None:
        with self.attempting(f"Starting the service {service.name}"):
            container = self.find_container(service.name)
            if container is None:
                self.ensure_lane_network()
                self.ensure_image(service.image)
                self.client.containers.run(
                    service.image,
                    service.command,
                    name=service.name,
                    detach=True,
                    network=LANE_NETWORK,
                    ports={f"{inside}/tcp": outside for inside, outside in service.ports.items()},
                    volumes={volume: {"bind": path, "mode": "rw"} for volume, path in service.volumes.items()},
                    restart_policy={"Name": "always"},
                )
            elif container.status != "running":
                container.start()

    def is_running(self, name: str) -> bool:
        with self.attempting(f"Reading the state of {name}"):
            container = self.find_container(name)
            return container is not None and container.status == "running"

    def run_in(self, name: str, command: list[str]) -> CommandResult:
        with self.attempting(f"Running {command[0]} in {name}"):
            result = self.client.containers.get(name).exec_run(command)
        exit_code, output = result.exit_code, result.output
        if exit_code is None or not isinstance(output, bytes):
            raise ContainerHostError(f"Running {command[0]} in {name} on {self.address} returned before the command finished.")
        return CommandResult(exit_code=exit_code, output=output.decode(errors="replace").strip())

    def place_file(self, name: str, path: PurePosixPath, content: str) -> None:
        with self.attempting(f"Placing {path} in {name}"):
            self.client.containers.get(name).put_archive(str(path.parent), tar_of(path.name, content.encode()))  # pyright: ignore[reportUnknownMemberType]


def tar_of(file_name: str, content: bytes) -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        member = tarfile.TarInfo(file_name)
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))
    return archive.getvalue()
