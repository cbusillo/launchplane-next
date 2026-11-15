import io
import os
import tarfile
import time
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import docker
import requests
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.tls import TLSConfig
from docker.types import EndpointConfig
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from launchplane.data_set import DATABASE_ADDRESS, DatabaseServer
from launchplane.instance import DataSet, InstanceState, LaneBuild, RunningInstance, RunningJob, job_container_of

PRODUCT_LABEL = "launchplane.product"
LANE_LABEL = "launchplane.lane"
BUILD_LABEL = "launchplane.build"
PART_LABEL = "launchplane.part"
DATA_SET_LABEL = "launchplane.data-set"
INSTANCE_LABEL = "launchplane.instance"
MARKED = {"label": PRODUCT_LABEL}

DOCKER_API_VERSION = "1.47"
ANSWER_TIMEOUT_SECONDS = 5
LANE_NETWORK = "launchplane-lanes"
GATEWAY_MODE_OPTION = "com.docker.network.bridge.gateway_mode_ipv4"
QUIET_GATEWAY_MODE = "isolated"
RETIRE_GRACE_SECONDS = 30
LOG_TAIL_LINES = 20
DATABASE_DEADLINE_SECONDS = 60
POLL_SECONDS = 0.5

type DockerState = Literal["created", "running", "paused", "restarting", "removing", "exited", "dead"]
type Part = Literal["web", "job", "database"]

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
    networks: list[str]


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class OnHost:
    instances: list[RunningInstance]
    jobs: list[RunningJob]
    data_sets: list[DataSet]


class DockerContainerSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    names: list[str] = Field(alias="Names", min_length=1)
    state: DockerState = Field(alias="State")
    labels: dict[str, str] = Field(alias="Labels")

    @property
    def name(self) -> str:
        return self.names[0].removeprefix("/")


class DockerNetworkSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = Field(alias="Name")
    internal: bool = Field(alias="Internal")
    options: dict[str, str] | None = Field(alias="Options")
    labels: dict[str, str] | None = Field(alias="Labels")


class DockerVolumeSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = Field(alias="Name")
    labels: dict[str, str] = Field(alias="Labels")


class DockerVolumeList(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    volumes: list[DockerVolumeSummary] | None = Field(alias="Volumes")


DOCKER_CONTAINER_LIST = TypeAdapter(list[DockerContainerSummary])
DOCKER_NETWORK_LIST = TypeAdapter(list[DockerNetworkSummary])
DOCKER_NETWORK = TypeAdapter(DockerNetworkSummary)
DOCKER_VOLUME_LIST = TypeAdapter(DockerVolumeList)


def checked[Shape](adapter: TypeAdapter[Shape], answer: object, what: str) -> Shape:
    try:
        return adapter.validate_python(answer)
    except ValidationError as error:
        raise ContainerHostError(f"Docker's {what} did not have the expected shape, so nothing was read: {error}") from error


def required_label(labels: dict[str, str], label: str, what: str) -> str:
    if label not in labels:
        raise ContainerHostError(f"Docker listed {what} for the {PRODUCT_LABEL} filter, but it has no {label} label.")
    return labels[label]


def part_of(summary: DockerContainerSummary) -> Part:
    part = summary.labels.get(PART_LABEL, "web")
    if part not in ("web", "job", "database"):
        raise ContainerHostError(f"The container {summary.name} is marked as the part {part}, which Launchplane does not know.")
    return part


def state_of(summary: DockerContainerSummary) -> InstanceState:
    return INSTANCE_STATE_FOR_DOCKER_STATE[summary.state]


def instance_from(summary: DockerContainerSummary) -> RunningInstance:
    return RunningInstance(
        product=required_label(summary.labels, PRODUCT_LABEL, summary.name),
        lane=summary.labels.get(LANE_LABEL),
        build=summary.labels.get(BUILD_LABEL),
        container=summary.name,
        state=state_of(summary),
        data_set=summary.labels.get(DATA_SET_LABEL),
    )


def job_from(summary: DockerContainerSummary) -> RunningJob:
    return RunningJob(
        product=required_label(summary.labels, PRODUCT_LABEL, summary.name),
        lane=summary.labels.get(LANE_LABEL),
        build=summary.labels.get(BUILD_LABEL),
        container=summary.name,
        state=state_of(summary),
        instance=summary.labels.get(INSTANCE_LABEL),
    )


def instances_from_answer(answer: object) -> list[RunningInstance]:
    summaries = checked(DOCKER_CONTAINER_LIST, answer, "container list")
    return [instance_from(summary) for summary in summaries if part_of(summary) == "web"]


def is_quiet(network: DockerNetworkSummary) -> bool:
    return network.internal and (network.options or {}).get(GATEWAY_MODE_OPTION) == QUIET_GATEWAY_MODE


type DataSetKey = tuple[str, str | None, str]


def data_set_key(labels: dict[str, str], what: str) -> DataSetKey:
    return (
        required_label(labels, PRODUCT_LABEL, what),
        labels.get(LANE_LABEL),
        required_label(labels, DATA_SET_LABEL, what),
    )


def data_sets_from(
    databases: Iterable[DockerContainerSummary], networks: Iterable[DockerNetworkSummary], volumes: Iterable[DockerVolumeSummary]
) -> list[DataSet]:
    quiet = {data_set_key(network.labels or {}, f"the network {network.name}"): is_quiet(network) for network in networks}
    with_volume = {data_set_key(volume.labels, f"the volume {volume.name}") for volume in volumes}
    database = {data_set_key(summary.labels, summary.name): summary for summary in databases}
    return [
        DataSet(
            product=key[0],
            lane=key[1],
            data_set=key[2],
            network_is_quiet=quiet.get(key),
            has_volume=key in with_volume,
            database_container=None if key not in database else database[key].name,
            database_state=None if key not in database else state_of(database[key]),
        )
        for key in sorted(quiet.keys() | with_volume | database.keys(), key=str)
    ]


def on_host_from_answers(containers: object, networks: object, volumes: object) -> OnHost:
    summaries = checked(DOCKER_CONTAINER_LIST, containers, "container list")
    volume_list = checked(DOCKER_VOLUME_LIST, volumes, "volume list").volumes or []
    by_part: dict[Part, list[DockerContainerSummary]] = {"web": [], "job": [], "database": []}
    for summary in summaries:
        by_part[part_of(summary)].append(summary)
    return OnHost(
        instances=[instance_from(summary) for summary in by_part["web"]],
        jobs=[job_from(summary) for summary in by_part["job"]],
        data_sets=data_sets_from(by_part["database"], checked(DOCKER_NETWORK_LIST, networks, "network list"), volume_list),
    )


def endpoints(networks: dict[str, list[str]]) -> dict[str, EndpointConfig]:
    return {network: EndpointConfig(DOCKER_API_VERSION, aliases=aliases or None) for network, aliases in networks.items()}


def data_set_network_of(data_set: str) -> str:
    return f"launchplane-data-{data_set}"


def data_set_volume_of(data_set: str) -> str:
    return f"launchplane-data-{data_set}-database"


def database_container_of(product: str, lane: str, data_set: str) -> str:
    return f"{product}-{lane}-database-{data_set}"


def data_set_labels(product: str, lane: str, data_set: str) -> dict[str, str]:
    return {PRODUCT_LABEL: product, LANE_LABEL: lane, DATA_SET_LABEL: data_set}


class ContainerHost:
    def __init__(
        self,
        client: docker.DockerClient,
        address: str,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.address = address
        self.sleep = sleep
        self.clock = clock

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

    @contextmanager
    def attempting(self, action: str) -> Generator[None]:
        try:
            yield
        except (DockerException, requests.RequestException, ValueError) as error:
            raise ContainerHostError(f"{action} on {self.address} failed: {error}") from error

    def list_marked_containers(self) -> object:
        return self.client.api.containers(all=True, filters=MARKED)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def list_marked_networks(self) -> object:
        return self.client.api.networks(filters=MARKED)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def list_marked_volumes(self) -> object:
        return self.client.api.volumes(filters=MARKED)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def read(self) -> OnHost:
        with self.attempting(f"Listing the containers, networks and volumes marked {PRODUCT_LABEL}"):
            containers, networks, volumes = self.list_marked_containers(), self.list_marked_networks(), self.list_marked_volumes()
        return on_host_from_answers(containers, networks, volumes)

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

    def ensure_network(self, name: str, labels: dict[str, str] | None = None, *, quiet: bool) -> None:
        try:
            found = self.client.networks.get(name)
        except NotFound:
            options = {GATEWAY_MODE_OPTION: QUIET_GATEWAY_MODE} if quiet else None
            self.client.networks.create(name, driver="bridge", internal=quiet, options=options, labels=labels)
            return
        if quiet and not is_quiet(checked(DOCKER_NETWORK, found.attrs, f"description of the network {name}")):
            raise ContainerHostError(
                f"The network {name} exists and is not quiet: it lets what is attached to it reach out. "
                "Launchplane attaches nothing to it until it is removed and made again."
            )

    def run_container(self, image: str, container: str, labels: dict[str, str], networks: dict[str, list[str]]) -> None:
        self.ensure_image(image)
        self.client.containers.run(
            image,
            name=container,
            detach=True,
            labels=labels,
            network=next(iter(networks)),
            networking_config=endpoints(networks),
            restart_policy={"Name": "always"},
        )

    def start_instance(self, target: LaneBuild, container: str, data_set: str | None) -> None:
        labels = {PRODUCT_LABEL: target.product, LANE_LABEL: target.lane, BUILD_LABEL: target.build, PART_LABEL: "web"}
        networks: dict[str, list[str]] = {LANE_NETWORK: []}
        if data_set is not None:
            labels[DATA_SET_LABEL] = data_set
            networks[data_set_network_of(data_set)] = []
        with self.attempting(f"Starting the instance {container} from build {target.build}"):
            self.ensure_network(LANE_NETWORK, quiet=True)
            self.run_container(target.build, container, labels, networks)

    def start_job(self, target: LaneBuild, instance: str, data_set: str | None, command: list[str]) -> None:
        container = job_container_of(instance)
        labels = {
            PRODUCT_LABEL: target.product,
            LANE_LABEL: target.lane,
            BUILD_LABEL: target.build,
            PART_LABEL: "job",
            INSTANCE_LABEL: instance,
        }
        network = LANE_NETWORK if data_set is None else data_set_network_of(data_set)
        if data_set is not None:
            labels[DATA_SET_LABEL] = data_set
        with self.attempting(f"Starting the job {container} of the instance {instance}"):
            self.ensure_network(network, quiet=True)
            self.ensure_image(target.build)
            self.client.containers.run(
                target.build,
                command,
                name=container,
                detach=True,
                labels=labels,
                network=network,
                restart_policy={"Name": "always"},
            )

    def ensure_data_set(self, product: str, lane: str, data_set: str, server: DatabaseServer) -> None:
        labels = data_set_labels(product, lane, data_set)
        container = database_container_of(product, lane, data_set)
        with self.attempting(f"Making the data set {data_set} of {lane} of {product}"):
            self.ensure_network(data_set_network_of(data_set), labels, quiet=True)
            self.client.volumes.create(data_set_volume_of(data_set), labels=labels)
            found = self.find_container(container)
            if found is None:
                self.ensure_image(server.image)
                self.client.containers.run(
                    server.image,
                    name=container,
                    detach=True,
                    labels=labels | {PART_LABEL: "database"},
                    network=data_set_network_of(data_set),
                    networking_config=endpoints({data_set_network_of(data_set): [DATABASE_ADDRESS]}),
                    environment=server.environment,
                    volumes={data_set_volume_of(data_set): {"bind": server.data_path, "mode": "rw"}},
                    restart_policy={"Name": "always"},
                )
            elif found.status != "running":
                found.start()

    def await_database(self, container: str, server: DatabaseServer) -> None:
        started = self.clock()
        while not (result := self.run_in(container, server.ready_command)).succeeded:
            if self.clock() - started >= DATABASE_DEADLINE_SECONDS:
                raise ContainerHostError(
                    f"The database {container} did not accept connections within {DATABASE_DEADLINE_SECONDS} s "
                    f"of being asked: {result.output or 'no answer'}. Its last log lines:\n{self.read_log_tail(container)}"
                )
            self.sleep(POLL_SECONDS)

    def delete_data_set(self, data_set: str) -> None:
        with self.attempting(f"Deleting the data set {data_set}"):
            of_data_set: dict[str, str | list[str] | bool] = {"label": f"{DATA_SET_LABEL}={data_set}"}
            database: dict[str, str | list[str] | bool] = {"label": [f"{DATA_SET_LABEL}={data_set}", f"{PART_LABEL}=database"]}
            for found in self.client.containers.list(all=True, filters=database):
                found.stop(timeout=RETIRE_GRACE_SECONDS)
                found.remove(v=True)
            for volume in self.client.volumes.list(filters=of_data_set):
                volume.remove()
            with suppress(NotFound):
                self.client.api.remove_network(data_set_network_of(data_set))  # pyright: ignore[reportUnknownMemberType]

    def remove_instance(self, container: str) -> None:
        with self.attempting(f"Removing the instance {container}"):
            found = self.find_container(container)
            if found is not None:
                found.remove(force=True)

    def retire_instance(self, container: str) -> None:
        with self.attempting(f"Stopping and removing {container}"):
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
                self.ensure_network(service.networks[0], quiet=False)
                for network in service.networks[1:]:
                    self.ensure_network(network, quiet=True)
                self.client.containers.run(
                    service.image,
                    service.command,
                    name=service.container,
                    detach=True,
                    network=service.networks[0],
                    networking_config=endpoints({network: [] for network in service.networks}),
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
