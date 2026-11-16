import io
import os
import tarfile
import threading
import time
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

import docker
import requests
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.tls import TLSConfig
from docker.types import EndpointConfig
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from launchplane.data_set import DATABASE_ADDRESS, DatabaseServer, variable_value
from launchplane.instance import (
    LANE_NETWORK,
    DataSet,
    DataSetDatabase,
    HostNetwork,
    InstanceState,
    LaneBuild,
    Mount,
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
SECRET_VARIABLES_LABEL = "launchplane.secret-variables"  # noqa: S105 the name of a label, not a secret
PASSWORD_FILE_LABEL = "launchplane.password-file"  # noqa: S105 the name of a label, not a password
ONLY_ITS_OWNER_READS = 0o600
MARKED = {"label": PRODUCT_LABEL}

DOCKER_API_VERSION = "1.47"
ANSWER_TIMEOUT_SECONDS = 5
GATEWAY_MODE_OPTION = "com.docker.network.bridge.gateway_mode_ipv4"
QUIET_GATEWAY_MODE = "isolated"
RETIRE_GRACE_SECONDS = 30
LOG_TAIL_LINES = 20
DATABASE_DEADLINE_SECONDS = 60
JOB_DEADLINE_MARGIN_SECONDS = 15
POLL_SECONDS = 0.5
MOST_READS = 3

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


class TornRead(Exception):
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
    networks: list[HostNetwork] = field(default_factory=list[HostNetwork])
    host_time: datetime | None = None


@dataclass(frozen=True)
class DatabaseLink:
    data_set: str
    network: str
    database_container: str
    server: DatabaseServer


type ServerOfProduct = Callable[[str], DatabaseServer | None]


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
    secret_variables: tuple[str, ...] = ()


@dataclass(frozen=True)
class NetworkSettings:
    internal: bool
    options: dict[str, str]


class DockerAttachedNetworks(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    networks: dict[str, object] = Field(alias="Networks")


class DockerMount(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str | None = Field(default=None, alias="Name")
    source: str = Field(alias="Source")
    destination: str = Field(alias="Destination")


class DockerContainerSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    names: list[str] = Field(alias="Names", min_length=1)
    state: DockerState = Field(alias="State")
    labels: dict[str, str] = Field(alias="Labels")
    network_settings: DockerAttachedNetworks = Field(alias="NetworkSettings")
    mounts: list[DockerMount] = Field(alias="Mounts")
    created: AwareDatetime = Field(alias="Created")

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


class DockerContainerState(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    running: bool = Field(alias="Running")
    restarting: bool = Field(alias="Restarting")
    paused: bool = Field(alias="Paused")
    started_at: AwareDatetime = Field(alias="StartedAt")


class DockerContainerDetails(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = Field(alias="Name")
    config: DockerContainerConfig = Field(alias="Config")
    state: DockerContainerState = Field(alias="State")


class DockerSystemTime(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    system_time: AwareDatetime = Field(alias="SystemTime")


DOCKER_CONTAINER_DETAILS = TypeAdapter(DockerContainerDetails)
DOCKER_SYSTEM_TIME = TypeAdapter(DockerSystemTime)
DOCKER_CONTAINER_LIST = TypeAdapter(list[DockerContainerSummary])
DOCKER_NETWORK_LIST = TypeAdapter(list[DockerNetworkSummary])
DOCKER_NETWORK = TypeAdapter(DockerNetworkSummary)
DOCKER_VOLUME_LIST = TypeAdapter(DockerVolumeList)


def checked[Shape](adapter: TypeAdapter[Shape], answer: object, what: str) -> Shape:
    try:
        return adapter.validate_python(answer)
    except ValidationError as error:
        raise ContainerHostError(
            f"Docker's {what} did not have the expected shape, so nothing was read: {shape_faults(error)}"
        ) from error


def shape_faults(error: ValidationError) -> str:
    faults = error.errors(include_input=False, include_url=False, include_context=False)
    return "; ".join(f"{'.'.join(map(str, fault['loc'])) or 'the answer'}: {fault['msg']}" for fault in faults)


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


def attached_networks(summary: DockerContainerSummary) -> tuple[str, ...]:
    return tuple(sorted(summary.network_settings.networks))


def mounts_of(summary: DockerContainerSummary) -> tuple[Mount, ...]:
    mounts = (Mount(source=mount.name or mount.source, path=mount.destination) for mount in summary.mounts)
    return tuple(sorted(mounts, key=lambda mount: (mount.path, mount.source)))


def instance_from(summary: DockerContainerSummary) -> RunningInstance:
    return RunningInstance(
        product=required_label(summary.labels, PRODUCT_LABEL, summary.name),
        lane=summary.labels.get(LANE_LABEL),
        build=summary.labels.get(BUILD_LABEL),
        container=summary.name,
        state=state_of(summary),
        networks=attached_networks(summary),
        data_set=summary.labels.get(DATA_SET_LABEL),
        mounts=mounts_of(summary),
        made_at=summary.created,
    )


def running_for(details: DockerContainerDetails, now: datetime) -> float | None:
    if not details.state.running or details.state.restarting or details.state.paused:
        return None
    return (now - details.state.started_at).total_seconds()


def job_from(summary: DockerContainerSummary, details: Mapping[str, DockerContainerDetails], now: datetime) -> RunningJob:
    if summary.name not in details:
        raise ContainerHostError(f"Docker listed the job {summary.name} but did not describe it, so how long it has run is unknown.")
    return RunningJob(
        product=required_label(summary.labels, PRODUCT_LABEL, summary.name),
        lane=summary.labels.get(LANE_LABEL),
        build=summary.labels.get(BUILD_LABEL),
        container=summary.name,
        state=state_of(summary),
        networks=attached_networks(summary),
        running_for_seconds=running_for(details[summary.name], now),
        instance=summary.labels.get(INSTANCE_LABEL),
        mounts=mounts_of(summary),
        data_set=summary.labels.get(DATA_SET_LABEL),
        made_at=summary.created,
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


def database_part(summary: DockerContainerSummary) -> DataSetDatabase:
    return DataSetDatabase(
        container=summary.name,
        state=state_of(summary),
        networks=attached_networks(summary),
        mounts=mounts_of(summary),
        made_at=summary.created,
    )


def network_from(network: DockerNetworkSummary) -> HostNetwork:
    return HostNetwork(name=network.name, is_quiet=is_quiet(network))


def data_sets_from(
    databases: Iterable[DockerContainerSummary],
    networks: Iterable[DockerNetworkSummary],
    volumes: Iterable[DockerVolumeSummary],
) -> list[DataSet]:
    by_network = grouped(
        (data_set_key(network.labels or {}, f"the network {network.name}"), network_from(network)) for network in networks
    )
    by_volume = grouped((data_set_key(volume.labels, f"the volume {volume.name}"), volume.name) for volume in volumes)
    by_database = grouped((data_set_key(summary.labels, summary.name), database_part(summary)) for summary in databases)
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


@dataclass(frozen=True)
class HostAnswers:
    containers: object
    networks: object
    volumes: object
    job_details: list[object] = field(default_factory=list[object])
    system: object = None


def job_details_by_name(answers: list[object]) -> dict[str, DockerContainerDetails]:
    described = [checked(DOCKER_CONTAINER_DETAILS, answer, "description of a job") for answer in answers]
    return {details.name.removeprefix("/"): details for details in described}


def on_host_from_answers(answers: HostAnswers) -> OnHost:
    summaries = checked(DOCKER_CONTAINER_LIST, answers.containers, "container list")
    every_network = checked(DOCKER_NETWORK_LIST, answers.networks, "network list")
    volume_list = checked(DOCKER_VOLUME_LIST, answers.volumes, "volume list").volumes or []
    marked_networks = [network for network in every_network if PRODUCT_LABEL in (network.labels or {})]
    by_part: dict[Part, list[DockerContainerSummary]] = {"web": [], "job": [], "database": []}
    for summary in summaries:
        by_part[part_of(summary)].append(summary)
    now = checked(DOCKER_SYSTEM_TIME, answers.system, "system information").system_time
    details = job_details_by_name(answers.job_details)
    return OnHost(
        instances=[instance_from(summary) for summary in by_part["web"]],
        jobs=[job_from(summary, details, now) for summary in by_part["job"]],
        data_sets=data_sets_from(by_part["database"], marked_networks, volume_list),
        host_time=now,
        networks=[network_from(network) for network in every_network],
    )


def job_names(containers: object) -> list[str]:
    summaries = checked(DOCKER_CONTAINER_LIST, containers, "container list")
    return [summary.name for summary in summaries if part_of(summary) == "job"]


def secrets_held(labels: Mapping[str, str], details: object, what: str) -> set[str]:
    variables = [variable for variable in labels.get(SECRET_VARIABLES_LABEL, "").split(",") if variable]
    environment = checked(DOCKER_CONTAINER_DETAILS, details, f"description of {what}").config.environment or []
    held: set[str] = set()
    for variable in variables:
        value = variable_value(environment, variable)
        if value:
            held.add(value)
    return held


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


def database_options(product: str, lane: str, data_set: str, network: str, volume: str, server: DatabaseServer) -> RunOptions:
    return RunOptions(
        image=server.image,
        container=database_container_of(product, lane, data_set),
        labels=data_set_labels(product, lane, data_set) | {PART_LABEL: "database", PASSWORD_FILE_LABEL: str(server.password_path)},
        networks={network: [DATABASE_ADDRESS]},
        environment=server.server_environment(),
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
        secret_variables=link.server.client_secret_variables,
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
        secret_variables=link.server.client_secret_variables,
    )


def labels_naming_secrets(options: RunOptions) -> dict[str, str]:
    if not options.secret_variables:
        return options.labels
    return options.labels | {SECRET_VARIABLES_LABEL: ",".join(options.secret_variables)}


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


def data_is_made(database: Container, server: DatabaseServer) -> bool:
    environment = checked(DOCKER_CONTAINER_DETAILS, database.attrs, f"description of {database.name}").config.environment
    made_with_the_data = server.path_made_with_the_data(environment or [])
    if made_with_the_data is None:
        raise ContainerHostError(
            f"The database {database.name} does not say where its data is ({server.data_folder_variable}), so Launchplane "
            "cannot tell whether its data set is new and may be given a password. It was not started."
        )
    try:
        database.get_archive(str(made_with_the_data))
    except NotFound:
        return False
    return True


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
        self.seen_secrets: frozenset[str] = frozenset()
        self.noting_secrets = threading.Lock()

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

    def describe(self, container: str) -> object:
        return self.client.api.inspect_container(container)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def system_information(self) -> object:
        return self.client.api.info()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def read_until_whole[Read](self, read_once: Callable[[], Read]) -> Read:
        torn: TornRead | None = None
        for _ in range(MOST_READS):
            try:
                return read_once()
            except TornRead as error:
                torn = error
        raise ContainerHostError(
            f"What is on {self.address} changed, or Docker refused to say, during each of {MOST_READS} reads in a row, "
            f"so nothing was read. The last time: {torn}"
        ) from torn

    def read(self, probing: ServerOfProduct | None = None) -> OnHost:
        return self.read_until_whole(lambda: self.read_once(probing))

    def read_once(self, probing: ServerOfProduct | None) -> OnHost:
        with self.attempting(f"Listing the containers and volumes marked {PRODUCT_LABEL}, and the networks"):
            containers, networks, volumes = self.list_marked_containers(), self.list_networks(), self.list_marked_volumes()
            jobs = job_names(containers)
            job_details = [self.describe_listed(job) for job in jobs]
            system = self.system_information()
        on_host = on_host_from_answers(HostAnswers(containers, networks, volumes, job_details, system))
        return on_host if probing is None else self.probed(on_host, probing)

    def describe_listed(self, container: str) -> object:
        try:
            return self.describe(container)
        except APIError as error:
            raise TornRead(f"{container} was listed, and Docker refused to describe it: {error}") from error

    def probed(self, on_host: OnHost, server_of: ServerOfProduct) -> OnHost:
        return replace(
            on_host, data_sets=[self.with_databases_asked(found, server_of(found.product)) for found in on_host.data_sets]
        )

    def with_databases_asked(self, found: DataSet, server: DatabaseServer | None) -> DataSet:
        if server is None:
            return found
        databases = tuple(
            each.model_copy(update={"accepts_connections": self.listed_database_accepts_connections(each.container, server)})
            if each.state == InstanceState.RUNNING
            else each
            for each in found.databases
        )
        return found.model_copy(update={"databases": databases})

    def listed_database_accepts_connections(self, container: str, server: DatabaseServer) -> bool:
        with self.attempting(f"Asking the database {container} whether it accepts connections"):
            try:
                return self.command_result_in(container, server.ready_command).succeeded
            except APIError as error:
                raise TornRead(f"The database {container} was listed as running, and Docker refused to ask it: {error}") from error

    def job_running_for(self, container: str) -> float | None:
        with self.attempting(f"Reading how long the job {container} has run"):
            details = checked(DOCKER_CONTAINER_DETAILS, self.describe(container), f"description of {container}")
            now = checked(DOCKER_SYSTEM_TIME, self.system_information(), "system information").system_time
        return running_for(details, now)

    def note_secrets(self, secrets: Iterable[str]) -> None:
        with self.noting_secrets:
            self.seen_secrets = self.seen_secrets.union(secrets)

    def secrets_seen(self) -> frozenset[str]:
        return self.seen_secrets

    def read_secrets(self) -> frozenset[str]:
        self.read_until_whole(self.read_secrets_once)
        return self.seen_secrets

    def read_secrets_once(self) -> None:
        with self.attempting("Reading the secrets Launchplane has given to what is"):
            marked: dict[str, str | list[str] | bool] = {"label": PRODUCT_LABEL}
            for holder in self.client.containers.list(all=True, filters=marked, ignore_removed=True):
                self.note_secrets(secrets_held(holder.labels, holder.attrs, str(holder.name)))
                try:
                    self.password_kept_with_the_data_of(holder)
                except APIError as error:
                    raise TornRead(f"{holder.name} was listed, and Docker refused to read its password file: {error}") from error

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
        self.create(options).start()

    def create(self, options: RunOptions) -> Container:
        self.ensure_image(options.image)
        return self.client.containers.create(
            options.image,
            options.command,
            name=options.container,
            labels=labels_naming_secrets(options),
            network=next(iter(options.networks)),
            networking_config=endpoints(options.networks),
            environment=options.environment,
            volumes={volume: {"bind": path, "mode": "rw"} for volume, path in options.volumes.items()},
            ports={f"{inside}/tcp": outside for inside, outside in options.ports.items()},
            restart_policy={"Name": "always"},
        )

    def password_kept_with_the_data_of(self, database: Container) -> str | None:
        path = database.labels.get(PASSWORD_FILE_LABEL)
        if path is None:
            return None
        try:
            chunks, _ = database.get_archive(path)
        except NotFound:
            return None
        password = only_file_in(b"".join(chunks)).decode()
        self.note_secrets([password])
        return password or None

    def password_of(self, link: DatabaseLink) -> str:
        password = self.password_kept_with_the_data_of(self.client.containers.get(link.database_container))
        if password is None:
            raise ContainerHostError(
                f"The database {link.database_container} of the data set {link.data_set} keeps no password with its data, "
                "so no instance can be given one."
            )
        return password

    def start_with_the_password_its_data_keeps(self, database: Container, server: DatabaseServer) -> None:
        if PASSWORD_FILE_LABEL not in database.labels:
            raise ContainerHostError(
                f"The database {database.name} does not say where its data keeps its password ({PASSWORD_FILE_LABEL}), "
                "so it was not started."
            )
        if self.password_kept_with_the_data_of(database) is None:
            self.give_a_new_data_set_its_password(database, server)
        database.start()

    def give_a_new_data_set_its_password(self, database: Container, server: DatabaseServer) -> None:
        path = PurePosixPath(database.labels[PASSWORD_FILE_LABEL])
        if data_is_made(database, server):
            raise ContainerHostError(
                f"The data set {database.labels.get(DATA_SET_LABEL)} has data and no password: {path} is not on its volume. "
                f"A password is made only for a new data set, because data that exists accepts only the one it was made with. "
                f"The database {database.name} was not started. Put the password back in that file, or delete the data set by name."
            )
        password = new_password()
        self.note_secrets([password])
        database.put_archive(str(path.parent), tar_of(path.name, password.encode(), ONLY_ITS_OWNER_READS))  # pyright: ignore[reportUnknownMemberType]

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

    def await_job(self, container: str, steady_seconds: float) -> None:
        started = self.clock()
        while (ran := self.job_running_for(container)) is None or ran < steady_seconds:
            if ran is None:
                raise PartStopped(f"The job {container} is not running: it stopped before it had run {steady_seconds:g} s.")
            if self.clock() - started >= steady_seconds + JOB_DEADLINE_MARGIN_SECONDS:
                raise PartStopped(f"The job {container} did not stay up {steady_seconds:g} s in a row; it keeps starting again.")
            self.sleep(POLL_SECONDS)

    def create_data_set_network(self, product: str, lane: str, data_set: str) -> None:
        with self.attempting(f"Making the network of the data set {data_set}"):
            self.create_network(data_set_network_of(data_set), data_set_labels(product, lane, data_set), quiet_network_settings())

    def create_data_set_volume(self, product: str, lane: str, data_set: str) -> None:
        with self.attempting(f"Making the volume of the data set {data_set}"):
            self.client.volumes.create(data_set_volume_of(data_set), labels=data_set_labels(product, lane, data_set))

    def create_database(self, product: str, lane: str, data_set: str, network: str, volume: str, server: DatabaseServer) -> None:
        with self.attempting(f"Making the database of the data set {data_set}"):
            made = self.create(database_options(product, lane, data_set, network, volume, server))
            self.start_with_the_password_its_data_keeps(made, server)

    def start_database(self, container: str, server: DatabaseServer) -> None:
        with self.attempting(f"Starting the database {container}"):
            self.start_with_the_password_its_data_keeps(self.client.containers.get(container), server)

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
            return self.command_result_in(container, command)

    def command_result_in(self, container: str, command: list[str]) -> CommandResult:
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


def tar_of(file_name: str, content: bytes, mode: int = 0o644) -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        member = tarfile.TarInfo(file_name)
        member.size = len(content)
        member.mode = mode
        tar.addfile(member, io.BytesIO(content))
    return archive.getvalue()


def only_file_in(archive: bytes) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        member = tar.next()
        content = None if member is None else tar.extractfile(member)
        if content is None:
            raise ContainerHostError("Docker answered a file read with an archive that holds no file.")
        return content.read()
