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

from launchplane.instance import InstanceState, LaneBuild, RunningInstance

PRODUCT_LABEL = "launchplane.product"
LANE_LABEL = "launchplane.lane"
BUILD_LABEL = "launchplane.build"

DOCKER_API_VERSION = "1.47"
ANSWER_TIMEOUT_SECONDS = 5
LANE_NETWORK = "launchplane-lanes"
RETIRE_GRACE_SECONDS = 30
LOG_TAIL_LINES = 20

type DockerState = Literal["created", "running", "paused", "restarting", "removing", "exited", "dead"]

INSTANCE_STATE_FOR_DOCKER_STATE: dict[DockerState, InstanceState] = {
    "created": InstanceState.STOPPED,
    "running": InstanceState.RUNNING,
    "paused": InstanceState.PAUSED,
    "restarting": InstanceState.RESTARTING,
    "removing": InstanceState.STOPPED,
    "exited": InstanceState.STOPPED,
    "dead": InstanceState.STOPPED,
}


class ContainerHostError(Exception):
    pass


@dataclass(frozen=True)
class Service:
    container: str
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


def instance_from(summary: DockerContainerSummary) -> RunningInstance:
    if PRODUCT_LABEL not in summary.labels:
        raise ContainerHostError(f"Docker listed {summary.names[0]} for the {PRODUCT_LABEL} filter, but it has no such label.")
    return RunningInstance(
        product=summary.labels[PRODUCT_LABEL],
        lane=summary.labels.get(LANE_LABEL),
        build=summary.labels.get(BUILD_LABEL),
        container=summary.names[0].removeprefix("/"),
        state=INSTANCE_STATE_FOR_DOCKER_STATE[summary.state],
    )


def instances_from_answer(answer: object) -> list[RunningInstance]:
    try:
        summaries = DOCKER_CONTAINER_LIST.validate_python(answer)
    except ValidationError as error:
        raise ContainerHostError(f"Docker's container list did not have the expected shape, so nothing was read: {error}") from error
    return [instance_from(summary) for summary in summaries]


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

    def read_instances(self) -> list[RunningInstance]:
        with self.attempting(f"Listing the containers marked {PRODUCT_LABEL}"):
            answer = self.list_marked_containers()
        return instances_from_answer(answer)

    def find_container(self, container: str) -> Container | None:
        try:
            return self.client.containers.get(container)
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

    def start_instance(self, target: LaneBuild, container: str) -> None:
        with self.attempting(f"Starting the instance {container} from build {target.build}"):
            self.ensure_lane_network()
            self.ensure_image(target.build)
            self.client.containers.run(
                target.build,
                name=container,
                detach=True,
                labels={PRODUCT_LABEL: target.product, LANE_LABEL: target.lane, BUILD_LABEL: target.build},
                network=LANE_NETWORK,
                restart_policy={"Name": "always"},
            )

    def remove_instance(self, container: str) -> None:
        with self.attempting(f"Removing the instance {container}"):
            found = self.find_container(container)
            if found is not None:
                found.remove(force=True)

    def retire_instance(self, container: str) -> None:
        with self.attempting(f"Stopping and removing the instance {container}"):
            found = self.find_container(container)
            if found is not None:
                found.stop(timeout=RETIRE_GRACE_SECONDS)
                found.remove()

    def read_log_tail(self, container: str) -> str:
        with self.attempting(f"Reading the log of {container}"):
            found = self.find_container(container)
            if found is None:
                return ""
            return found.logs(tail=LOG_TAIL_LINES).decode(errors="replace")

    def ensure_service(self, service: Service) -> None:
        with self.attempting(f"Starting the service {service.container}"):
            found = self.find_container(service.container)
            if found is None:
                self.ensure_lane_network()
                self.ensure_image(service.image)
                self.client.containers.run(
                    service.image,
                    service.command,
                    name=service.container,
                    detach=True,
                    network=LANE_NETWORK,
                    ports={f"{inside}/tcp": outside for inside, outside in service.ports.items()},
                    volumes={volume: {"bind": path, "mode": "rw"} for volume, path in service.volumes.items()},
                    restart_policy={"Name": "always"},
                )
            elif found.status != "running":
                found.start()

    def is_running(self, container: str) -> bool:
        with self.attempting(f"Reading the state of {container}"):
            found = self.find_container(container)
            return found is not None and found.status == "running"

    def run_in(self, container: str, command: list[str]) -> CommandResult:
        with self.attempting(f"Running {command[0]} in {container}"):
            result = self.client.containers.get(container).exec_run(command)
        exit_code, output = result.exit_code, result.output
        if exit_code is None or not isinstance(output, bytes):
            raise ContainerHostError(f"Running {command[0]} in {container} on {self.address} returned before the command finished.")
        return CommandResult(exit_code=exit_code, output=output.decode(errors="replace").strip())

    def read_file(self, container: str, path: PurePosixPath) -> str | None:
        with self.attempting(f"Reading {path} in {container}"):
            found = self.client.containers.get(container)
            try:
                chunks, _ = found.get_archive(str(path))
            except NotFound:
                return None
            return only_file_in(b"".join(chunks)).decode()

    def place_file(self, container: str, path: PurePosixPath, content: str) -> None:
        with self.attempting(f"Placing {path} in {container}"):
            self.client.containers.get(container).put_archive(str(path.parent), tar_of(path.name, content.encode()))  # pyright: ignore[reportUnknownMemberType]


def tar_of(file_name: str, content: bytes) -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        member = tarfile.TarInfo(file_name)
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))
    return archive.getvalue()


def only_file_in(archive: bytes) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        member = tar.next()
        content = None if member is None else tar.extractfile(member)
        if content is None:
            raise ContainerHostError("Docker answered a file read with an archive that holds no file.")
        return content.read()
