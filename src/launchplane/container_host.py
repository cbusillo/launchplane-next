import io
import os
import tarfile
import time
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
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
from launchplane.instance import (
    DataSet,
    DataSetDatabase,
    DataSetNetwork,
    InstanceState,
    LaneBuild,
    RunningInstance,
    RunningJob,
    job_container_of,
    new_password,
)

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


class PartStopped(Exception):
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


@dataclass(frozen=True)
class DatabaseLink:
    data_set: str
    network: str
    database_container: str
    server: DatabaseServer


@dataclass(frozen=True)
class RunOptions:
    image: str
    container: str
    labels: dict[str, str]
    networks: dict[str, list[str]]
    command: list[str] | None = None
    environment: dict[str, str] = field(default_factory=dict[str, str])
    volumes: dict[str, str] = field(default_factory=dict[str, str])
    ports: dict[int, int] = field(default_factory=dict[int, int])


@dataclass(frozen=True)
class NetworkSettings:
    internal: bool
    options: dict[str, str]


class DockerAttachedNetworks(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    networks: dict[str, object] = Field(alias="Networks")


class DockerContainerSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    names: list[str] = Field(alias="Names", min_length=1)
    state: DockerState = Field(alias="State")
    labels: dict[str, str] = Field(alias="Labels")
    network_settings: DockerAttachedNetworks = Field(alias="NetworkSettings")

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


class DockerContainerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: list[str] | None = Field(alias="Env")


class DockerContainerDetails(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    config: DockerContainerConfig = Field(alias="Config")


DOCKER_CONTAINER_DETAILS = TypeAdapter(DockerContainerDetails)
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


def is_quiet(network: DockerNetworkSummary) -> bool:
    return network.internal and (network.options or {}).get(GATEWAY_MODE_OPTION) == QUIET_GATEWAY_MODE


def quiet_network_settings() -> NetworkSettings:
    return NetworkSettings(internal=True, options={GATEWAY_MODE_OPTION: QUIET_GATEWAY_MODE})


def reaches_only_quiet_networks(summary: DockerContainerSummary, quiet_networks: set[str]) -> bool:
    return summary.network_settings.networks.keys() <= quiet_networks


def instance_from(summary: DockerContainerSummary, quiet_networks: set[str]) -> RunningInstance:
    return RunningInstance(
        product=required_label(summary.labels, PRODUCT_LABEL, summary.name),
        lane=summary.labels.get(LANE_LABEL),
        build=summary.labels.get(BUILD_LABEL),
        container=summary.name,
        state=state_of(summary),
        is_quiet=reaches_only_quiet_networks(summary, quiet_networks),
        data_set=summary.labels.get(DATA_SET_LABEL),
    )


def job_from(summary: DockerContainerSummary, quiet_networks: set[str]) -> RunningJob:
    return RunningJob(
        product=required_label(summary.labels, PRODUCT_LABEL, summary.name),
        lane=summary.labels.get(LANE_LABEL),
        build=summary.labels.get(BUILD_LABEL),
        container=summary.name,
        state=state_of(summary),
        is_quiet=reaches_only_quiet_networks(summary, quiet_networks),
        instance=summary.labels.get(INSTANCE_LABEL),
    )


type DataSetKey = tuple[str, str | None, str]


def data_set_key(labels: dict[str, str], what: str) -> DataSetKey:
    return (
        required_label(labels, PRODUCT_LABEL, what),
        labels.get(LANE_LABEL),
        required_label(labels, DATA_SET_LABEL, what),
    )


def grouped[Part](parts: Iterable[tuple[DataSetKey, Part]]) -> dict[DataSetKey, list[Part]]:
    groups: dict[DataSetKey, list[Part]] = {}
    for key, part in parts:
        groups.setdefault(key, []).append(part)
    return groups


def network_part(network: DockerNetworkSummary) -> DataSetNetwork:
    return DataSetNetwork(name=network.name, is_quiet=is_quiet(network))


def data_sets_from(
    databases: Iterable[DockerContainerSummary], networks: Iterable[DockerNetworkSummary], volumes: Iterable[DockerVolumeSummary]
) -> list[DataSet]:
    by_network = grouped(
        (data_set_key(network.labels or {}, f"the network {network.name}"), network_part(network)) for network in networks
    )
    by_volume = grouped((data_set_key(volume.labels, f"the volume {volume.name}"), volume.name) for volume in volumes)
    by_database = grouped(
        (data_set_key(summary.labels, summary.name), DataSetDatabase(container=summary.name, state=state_of(summary)))
        for summary in databases
    )
    return [
        DataSet(
            product=key[0],
            lane=key[1],
            data_set=key[2],
            networks=tuple(sorted(by_network.get(key, []), key=lambda network: network.name)),
            volumes=tuple(sorted(by_volume.get(key, []))),
            databases=tuple(sorted(by_database.get(key, []), key=lambda database: database.container)),
        )
        for key in sorted(by_network.keys() | by_volume.keys() | by_database.keys(), key=str)
    ]


def on_host_from_answers(containers: object, networks: object, volumes: object) -> OnHost:
    summaries = checked(DOCKER_CONTAINER_LIST, containers, "container list")
    every_network = checked(DOCKER_NETWORK_LIST, networks, "network list")
    volume_list = checked(DOCKER_VOLUME_LIST, volumes, "volume list").volumes or []
    quiet_networks = {network.name for network in every_network if is_quiet(network)}
    marked_networks = [network for network in every_network if PRODUCT_LABEL in (network.labels or {})]
    by_part: dict[Part, list[DockerContainerSummary]] = {"web": [], "job": [], "database": []}
    for summary in summaries:
        by_part[part_of(summary)].append(summary)
    return OnHost(
        instances=[instance_from(summary, quiet_networks) for summary in by_part["web"]],
        jobs=[job_from(summary, quiet_networks) for summary in by_part["job"]],
        data_sets=data_sets_from(by_part["database"], marked_networks, volume_list),
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


def database_options(
    product: str, lane: str, data_set: str, network: str, volume: str, server: DatabaseServer, password: str
) -> RunOptions:
    return RunOptions(
        image=server.image,
        container=database_container_of(product, lane, data_set),
        labels=data_set_labels(product, lane, data_set) | {PART_LABEL: "database"},
        networks={network: [DATABASE_ADDRESS]},
        environment=server.server_environment(password),
        volumes={volume: server.data_path},
    )


def instance_options(target: LaneBuild, container: str, link: DatabaseLink | None, password: str | None) -> RunOptions:
    labels = {PRODUCT_LABEL: target.product, LANE_LABEL: target.lane, BUILD_LABEL: target.build, PART_LABEL: "web"}
    if link is None or password is None:
        return RunOptions(image=target.build, container=container, labels=labels, networks={LANE_NETWORK: []})
    return RunOptions(
        image=target.build,
        container=container,
        labels=labels | {DATA_SET_LABEL: link.data_set},
        networks={LANE_NETWORK: [], link.network: []},
        environment=link.server.client_environment(password),
    )


def job_options(target: LaneBuild, instance: str, link: DatabaseLink | None, password: str | None, command: list[str]) -> RunOptions:
    labels = {
        PRODUCT_LABEL: target.product,
        LANE_LABEL: target.lane,
        BUILD_LABEL: target.build,
        PART_LABEL: "job",
        INSTANCE_LABEL: instance,
    }
    container = job_container_of(instance)
    if link is None or password is None:
        return RunOptions(image=target.build, container=container, labels=labels, networks={LANE_NETWORK: []}, command=command)
    return RunOptions(
        image=target.build,
        container=container,
        labels=labels | {DATA_SET_LABEL: link.data_set},
        networks={link.network: []},
        command=command,
        environment=link.server.client_environment(password),
    )


def service_options(service: Service) -> RunOptions:
    return RunOptions(
        image=service.image,
        container=service.container,
        labels={},
        networks={network: [] for network in service.networks},
        command=service.command,
        volumes=service.volumes,
        ports=service.ports,
    )


class ContainerHost:
    def __init__(
        self,
        client: docker.DockerClient,
        address: str,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        make_password: Callable[[], str] = new_password,
    ) -> None:
        self.client = client
        self.address = address
        self.sleep = sleep
        self.clock = clock
        self.make_password = make_password

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

    def list_networks(self) -> object:
        return self.client.api.networks()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def list_marked_volumes(self) -> object:
        return self.client.api.volumes(filters=MARKED)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def read(self) -> OnHost:
        with self.attempting(f"Listing the containers and volumes marked {PRODUCT_LABEL}, and the networks"):
            containers, networks, volumes = self.list_marked_containers(), self.list_networks(), self.list_marked_volumes()
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

    def create_network(self, name: str, labels: dict[str, str] | None, settings: NetworkSettings | None) -> None:
        if settings is None:
            self.client.networks.create(name, driver="bridge", labels=labels)
        else:
            self.client.networks.create(name, driver="bridge", internal=settings.internal, options=settings.options, labels=labels)

    def ensure_network(self, name: str, *, quiet: bool) -> None:
        try:
            found = self.client.networks.get(name)
        except NotFound:
            self.create_network(name, None, quiet_network_settings() if quiet else None)
            return
        if quiet and not is_quiet(checked(DOCKER_NETWORK, found.attrs, f"description of the network {name}")):
            raise ContainerHostError(
                f"The network {name} exists and is not quiet: it lets what is attached to it reach out. "
                "Launchplane attaches nothing to it until it is removed and made again."
            )

    def run(self, options: RunOptions) -> None:
        self.ensure_image(options.image)
        self.client.containers.run(
            options.image,
            options.command,
            name=options.container,
            detach=True,
            labels=options.labels,
            network=next(iter(options.networks)),
            networking_config=endpoints(options.networks),
            environment=options.environment,
            volumes={volume: {"bind": path, "mode": "rw"} for volume, path in options.volumes.items()},
            ports={f"{inside}/tcp": outside for inside, outside in options.ports.items()},
            restart_policy={"Name": "always"},
        )

    def password_of(self, link: DatabaseLink) -> str:
        details = self.client.containers.get(link.database_container).attrs
        entries = checked(DOCKER_CONTAINER_DETAILS, details, f"description of {link.database_container}").config.environment or []
        password = link.server.password_in(entries)
        if password is None:
            raise ContainerHostError(
                f"The database {link.database_container} of the data set {link.data_set} has no password, "
                "so no instance can be given one."
            )
        return password

    def start_instance(self, target: LaneBuild, container: str, link: DatabaseLink | None) -> None:
        with self.attempting(f"Starting the instance {container} from build {target.build}"):
            self.ensure_network(LANE_NETWORK, quiet=True)
            password = None if link is None else self.password_of(link)
            self.run(instance_options(target, container, link, password))

    def start_job(self, target: LaneBuild, instance: str, link: DatabaseLink | None, command: list[str]) -> None:
        with self.attempting(f"Starting the job of the instance {instance}"):
            if link is None:
                self.ensure_network(LANE_NETWORK, quiet=True)
            password = None if link is None else self.password_of(link)
            self.run(job_options(target, instance, link, password, command))

    def check_job(self, container: str, steady_seconds: float) -> None:
        started = self.clock()
        while self.clock() - started < steady_seconds:
            if not self.is_running(container):
                raise PartStopped(f"The job {container} stopped within {steady_seconds:g} s of starting.")
            self.sleep(POLL_SECONDS)

    def create_data_set_network(self, product: str, lane: str, data_set: str) -> None:
        with self.attempting(f"Making the network of the data set {data_set}"):
            self.create_network(data_set_network_of(data_set), data_set_labels(product, lane, data_set), quiet_network_settings())

    def create_data_set_volume(self, product: str, lane: str, data_set: str) -> None:
        with self.attempting(f"Making the volume of the data set {data_set}"):
            self.client.volumes.create(data_set_volume_of(data_set), labels=data_set_labels(product, lane, data_set))

    def create_database(self, product: str, lane: str, data_set: str, network: str, volume: str, server: DatabaseServer) -> None:
        with self.attempting(f"Making the database of the data set {data_set}"):
            self.run(database_options(product, lane, data_set, network, volume, server, self.make_password()))

    def start_database(self, container: str) -> None:
        with self.attempting(f"Starting the database {container}"):
            self.client.containers.get(container).start()

    def await_database(self, container: str, server: DatabaseServer) -> None:
        started = self.clock()
        while not (result := self.run_in(container, server.ready_command)).succeeded:
            if self.clock() - started >= DATABASE_DEADLINE_SECONDS:
                raise ContainerHostError(
                    f"The database {container} did not accept connections within {DATABASE_DEADLINE_SECONDS} s "
                    f"of being asked: {result.output or 'no answer'}. Its last log lines:\n{self.read_log_tail(container)}"
                )
            self.sleep(POLL_SECONDS)

    def delete_data_set(self, found: DataSet) -> None:
        with self.attempting(f"Deleting the data set {found.data_set}"):
            for database in found.databases:
                self.retire_container(database.container)
            for volume in found.volumes:
                with suppress(NotFound):
                    self.client.volumes.get(volume).remove()
            for network in found.networks:
                with suppress(NotFound):
                    self.client.networks.get(network.name).remove()

    def retire_container(self, container: str) -> None:
        found = self.find_container(container)
        if found is not None:
            found.stop(timeout=RETIRE_GRACE_SECONDS)
            found.remove()

    def remove_instance(self, container: str) -> None:
        with self.attempting(f"Removing the instance {container}"):
            found = self.find_container(container)
            if found is not None:
                found.remove(force=True)

    def retire_instance(self, container: str) -> None:
        with self.attempting(f"Stopping and removing {container}"):
            self.retire_container(container)

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
                self.run(service_options(service))
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
