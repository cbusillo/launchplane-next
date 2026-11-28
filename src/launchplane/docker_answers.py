import io
import tarfile
import time
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import requests
from docker.errors import APIError, DockerException
from docker.models.containers import Container
from pydantic import AwareDatetime, BaseModel, Field, TypeAdapter, ValidationError

from launchplane.instance import (
    DataSet,
    DataSetDatabase,
    HostNetwork,
    InstanceState,
    Mount,
    OnHost,
    RunningBackgroundService,
    RunningInstance,
    Stamp,
    Unread,
    VolumeCopy,
    is_generation,
)
from launchplane.steps import Verb
from launchplane.telling import Said, Witness

PRODUCT_LABEL = "launchplane.product"
LANE_LABEL = "launchplane.lane"
BUILD_LABEL = "launchplane.build"
PART_LABEL = "launchplane.part"
DATA_SET_LABEL = "launchplane.data-set"
INSTANCE_LABEL = "launchplane.instance"
GENERATION_LABEL = "launchplane.generation"
FIRST_START_LABEL = "launchplane.first-start"
COPY_OF_LABEL = "launchplane.copy-of"
GATEWAY_MODE_OPTION = "com.docker.network.bridge.gateway_mode_ipv4"
QUIET_GATEWAY_MODE = "isolated"
MOST_READS = 3

type DockerState = Literal["created", "running", "paused", "restarting", "removing", "exited", "dead"]
type Part = Literal["web", "update", "background-service", "database", "copier", "initializer"]

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


class TornRead(Exception):
    pass


# The host answered, and the answer is no: Docker will not do it, or what was read of the host forbids it.
class ContainerHostRefused(ContainerHostError):
    pass


# A database server answered no to the removal of a login. The removal failed: nothing about it is unread.
class LoginRemovalRefused(ContainerHostRefused):
    pass


@contextmanager
def observing() -> Generator[None]:
    try:
        yield
    except ContainerHostRefused as error:
        raise ContainerHostError(str(error)) from error


@contextmanager
def attempting(action: str, address: str) -> Generator[None]:
    try:
        yield
    except APIError as error:
        raise ContainerHostRefused(f"{action} on {address} was refused: {error}") from error
    except (DockerException, requests.RequestException) as error:
        raise ContainerHostError(f"{action} on {address} failed: {error}") from error


# One command for one named container: its line is written before it is sent, and answered before anything else is sent.
@contextmanager
def sent(by: Witness, verb: Verb, container: str, *, already: bool = False) -> Generator[None]:
    answered = by.sending(verb, container)
    try:
        yield
    except APIError as error:
        # Docker's client reads a container back once Docker has made it. A refusal of that read is no answer to the make.
        if verb == "make" and error.response is not None and error.response.request.method == "GET":
            answered(None, str(error))
            raise ContainerHostError(
                f"Docker was sent the make of {container} and did not describe what it made: {error}"
            ) from error
        answered(Said.REFUSED, str(error))
        raise
    except (DockerException, requests.RequestException) as error:
        answered(None, str(error))
        raise
    answered(Said.ALREADY if already else Said.CHANGED, None)


def read_whole[Read](read_once: Callable[[], Read], address: str) -> Read:
    torn: TornRead | None = None
    for _ in range(MOST_READS):
        try:
            return read_once()
        except TornRead as error:
            torn = error
    raise ContainerHostError(
        f"What is on {address} changed, or Docker refused to say, during each of {MOST_READS} reads in a row, "
        f"so nothing was read. The last time: {torn}"
    ) from torn


class DockerAttachedNetworks(BaseModel, extra="ignore", frozen=True):
    networks: dict[str, object] = Field(alias="Networks")


class DockerMount(BaseModel, extra="ignore", frozen=True):
    name: str | None = Field(default=None, alias="Name")
    source: str = Field(alias="Source")
    destination: str = Field(alias="Destination")
    writable: bool = Field(default=True, alias="RW")


class DockerContainerSummary(BaseModel, extra="ignore", frozen=True):
    names: list[str] = Field(alias="Names", min_length=1)
    image: str = Field(alias="Image")
    image_id: str = Field(default="", alias="ImageID")
    state: DockerState = Field(alias="State")
    labels: dict[str, str] = Field(alias="Labels")
    network_settings: DockerAttachedNetworks = Field(alias="NetworkSettings")
    mounts: list[DockerMount] = Field(alias="Mounts")

    @property
    def name(self) -> str:
        return self.names[0].removeprefix("/")


class DockerNetworkSummary(BaseModel, extra="ignore", frozen=True):
    name: str = Field(alias="Name")
    internal: bool = Field(alias="Internal")
    options: dict[str, str] | None = Field(alias="Options")
    labels: dict[str, str] | None = Field(alias="Labels")


class DockerVolumeSummary(BaseModel, extra="ignore", frozen=True):
    name: str = Field(alias="Name")
    labels: dict[str, str] = Field(alias="Labels")


class DockerVolumeList(BaseModel, extra="ignore", frozen=True):
    volumes: list[DockerVolumeSummary] | None = Field(alias="Volumes")


class DockerContainerConfig(BaseModel, extra="ignore", frozen=True):
    environment: list[str] | None = Field(alias="Env")


class DockerContainerState(BaseModel, extra="ignore", frozen=True):
    running: bool = Field(alias="Running")
    restarting: bool = Field(alias="Restarting")
    paused: bool = Field(alias="Paused")
    started_at: AwareDatetime = Field(alias="StartedAt")
    exit_code: int = Field(alias="ExitCode")
    status: str = Field(default="", alias="Status")


class DockerContainerDetails(BaseModel, extra="ignore", frozen=True):
    name: str = Field(alias="Name")
    config: DockerContainerConfig = Field(alias="Config")
    state: DockerContainerState = Field(alias="State")
    restart_count: int = Field(default=0, alias="RestartCount")


class DockerSystemTime(BaseModel, extra="ignore", frozen=True):
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
    if part not in ("web", "update", "background-service", "database", "copier", "initializer"):
        raise ContainerHostError(f"The container {summary.name} is marked as the part {part}, which Launchplane does not know.")
    return part


def state_of(summary: DockerContainerSummary) -> InstanceState:
    return INSTANCE_STATE_FOR_DOCKER_STATE[summary.state]


def is_quiet(network: DockerNetworkSummary) -> bool:
    return network.internal and (network.options or {}).get(GATEWAY_MODE_OPTION) == QUIET_GATEWAY_MODE


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
        generation=generation_in(summary.labels, summary.name),
        is_update=part_of(summary) == "update",
    )


def generation_in(labels: dict[str, str], part: str) -> Stamp:
    stamped = labels.get(GENERATION_LABEL)
    if stamped is None or is_generation(stamped):
        return stamped
    why = f"its label {GENERATION_LABEL}, {len(stamped)} characters long, is not a generation"
    return Unread(asked=f"Which generation made {part}", why=why)


def running_for(details: DockerContainerDetails, now: datetime) -> float | None:
    if not details.state.running or details.state.restarting or details.state.paused:
        return None
    return (now - details.state.started_at).total_seconds()


def background_service_from(
    summary: DockerContainerSummary, details: Mapping[str, DockerContainerDetails], now: datetime
) -> RunningBackgroundService:
    return RunningBackgroundService(
        product=required_label(summary.labels, PRODUCT_LABEL, summary.name),
        lane=summary.labels.get(LANE_LABEL),
        build=summary.labels.get(BUILD_LABEL),
        container=summary.name,
        state=state_of(summary),
        networks=attached_networks(summary),
        running_for_seconds=running_for(described(summary, details, "background service"), now),
        instance=summary.labels.get(INSTANCE_LABEL),
        mounts=mounts_of(summary),
        data_set=summary.labels.get(DATA_SET_LABEL),
        generation=generation_in(summary.labels, summary.name),
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


def described(summary: DockerContainerSummary, details: Mapping[str, DockerContainerDetails], part: str) -> DockerContainerDetails:
    if summary.name not in details:
        raise ContainerHostError(
            f"Docker listed the {part} {summary.name} but did not describe it, so how it runs and what it holds is unknown."
        )
    return details[summary.name]


def database_part(summary: DockerContainerSummary, details: Mapping[str, DockerContainerDetails]) -> DataSetDatabase:
    state = state_of(summary)
    return DataSetDatabase(
        container=summary.name,
        state=state,
        networks=attached_networks(summary),
        mounts=mounts_of(summary),
        exit_code=described(summary, details, "database").state.exit_code if state == InstanceState.STOPPED else None,
        image=summary.image,
    )


# Its state is read from its description: the list can still say running just after it has exited.
def volume_copy(summary: DockerContainerSummary, details: Mapping[str, DockerContainerDetails]) -> VolumeCopy:
    written = [mount.name or mount.source for mount in summary.mounts if mount.writable]
    state = described(summary, details, "copier").state
    finished = (state.status, state.exit_code) == ("exited", 0)
    return VolumeCopy(container=summary.name, volume="".join(written[:1]), finished=finished, stopped=not state.running)


def network_from(network: DockerNetworkSummary) -> HostNetwork:
    return HostNetwork(name=network.name, is_quiet=is_quiet(network))


def data_sets_from(
    databases: Iterable[tuple[DataSetKey, DataSetDatabase]],
    networks: Iterable[DockerNetworkSummary],
    volumes: Iterable[DockerVolumeSummary],
    copies: Iterable[tuple[DataSetKey, VolumeCopy]],
) -> list[DataSet]:
    marked = [(data_set_key(network.labels or {}, f"the network {network.name}"), network) for network in networks]
    by_network = grouped((key, network_from(network)) for key, network in marked)
    made_by = {key: generation_in(network.labels or {}, f"the network {network.name}") for key, network in marked}
    keyed = [(data_set_key(volume.labels, f"the volume {volume.name}"), volume) for volume in volumes]
    stamped = [(key, volume) for key, volume in keyed if GENERATION_LABEL in volume.labels]
    made_by = {key: generation_in(volume.labels, f"the volume {volume.name}") for key, volume in stamped} | made_by
    copy_of = {key: volume.labels[COPY_OF_LABEL] for key, volume in keyed if COPY_OF_LABEL in volume.labels}
    aside = {key: volume.name for key, volume in keyed if volume.labels.get(PART_LABEL) == "set-aside"}
    first = {key for key, network in marked if FIRST_START_LABEL in (network.labels or {})}
    first.update(key for key, volume in keyed if FIRST_START_LABEL in volume.labels)
    for key, labels in [(k, n.labels or {}) for k, n in marked] + [(k, v.labels) for k, v in keyed]:
        if key in first and (
            FIRST_START_LABEL not in labels
            or not isinstance(made_by.get(key), str)
            or labels.get(GENERATION_LABEL) != made_by.get(key)
        ):
            raise ContainerHostError(f"The first-start labels of data set {key[2]} disagree. Its data was left alone.")
    by_copier = grouped(copies)
    by_volume = grouped((key, volume.name) for key, volume in keyed if volume.labels.get(PART_LABEL) not in ("files", "set-aside"))
    by_files = grouped((key, volume.name) for key, volume in keyed if volume.labels.get(PART_LABEL) == "files")
    by_database = grouped(databases)
    return [
        DataSet(
            product=key[0],
            lane=key[1],
            data_set=key[2],
            networks=tuple(sorted(by_network.get(key, []), key=lambda network: network.name)),
            volumes=tuple(sorted(by_volume.get(key, []))),
            databases=tuple(sorted(by_database.get(key, []), key=lambda database: database.container)),
            generation=made_by.get(key),
            first_start=key in first,
            set_aside=aside.get(key),
            files=tuple(sorted(by_files.get(key, []))),
            copy_of=copy_of.get(key),
            copies=tuple(sorted(by_copier.get(key, []), key=lambda copy: copy.container)),
        )
        for key in sorted(by_network.keys() | by_volume.keys() | by_files.keys() | by_database.keys() | aside.keys(), key=str)
    ]


def details_by_name(answers: list[object]) -> dict[str, DockerContainerDetails]:
    each = [checked(DOCKER_CONTAINER_DETAILS, answer, "description of a container") for answer in answers]
    return {details.name.removeprefix("/"): details for details in each}


@dataclass(frozen=True)
class Listed:
    summaries: list[DockerContainerSummary]
    details: dict[str, DockerContainerDetails]

    def has_part(self, part: Part) -> bool:
        return any(summary.labels.get(PART_LABEL) == part for summary in self.summaries)


def container_summaries(containers: object) -> list[DockerContainerSummary]:
    return checked(DOCKER_CONTAINER_LIST, containers, "container list")


def on_host_from(found: Listed, networks: object, volumes: object, system: object) -> OnHost:
    every_network = checked(DOCKER_NETWORK_LIST, networks, "network list")
    volume_list = checked(DOCKER_VOLUME_LIST, volumes, "volume list").volumes or []
    marked_networks = [network for network in every_network if PRODUCT_LABEL in (network.labels or {})]
    by_part: dict[Part, list[DockerContainerSummary]] = {
        name: [] for name in ("web", "update", "background-service", "database", "copier", "initializer")
    }
    for summary in found.summaries:
        by_part[part_of(summary)].append(summary)
    details = found.details
    background_services: list[RunningBackgroundService] = []
    if by_part["background-service"]:
        now = checked(DOCKER_SYSTEM_TIME, system, "system information").system_time
        background_services = [background_service_from(summary, details, now) for summary in by_part["background-service"]]
    databases = [(data_set_key(summary.labels, summary.name), database_part(summary, details)) for summary in by_part["database"]]
    copies = [(data_set_key(summary.labels, summary.name), volume_copy(summary, details)) for summary in by_part["copier"]]
    return OnHost(
        instances=[instance_from(summary) for summary in (*by_part["web"], *by_part["update"])],
        background_services=background_services,
        data_sets=data_sets_from(databases, marked_networks, volume_list, copies),
        networks=[network_from(network) for network in every_network],
    )


def tar_of(file_name: str, content: bytes) -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        member = tarfile.TarInfo(file_name)
        member.size = len(content)
        member.mtime = int(time.time())
        tar.addfile(member, io.BytesIO(content))
    return archive.getvalue()


def files_in(archive: bytes) -> dict[str, tuple[bytes, datetime]]:
    found: dict[str, tuple[bytes, datetime]] = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar:
            content = tar.extractfile(member)
            if content is not None:
                found[member.name.rsplit("/", 1)[-1]] = (content.read(), datetime.fromtimestamp(member.mtime, UTC))
    return found


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


def command_result(container: Container, command: list[str]) -> CommandResult:
    result = container.exec_run(command)
    exit_code, output = result.exit_code, result.output
    if exit_code is None or not isinstance(output, bytes):
        raise ContainerHostError(f"Running {command[0]} in {container.name} returned before the command finished.")
    return CommandResult(exit_code=exit_code, output=output.decode(errors="replace").strip())
