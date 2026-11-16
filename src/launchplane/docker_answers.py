import io
import tarfile
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import requests
from docker.errors import DockerException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from launchplane.instance import (
    DataSet,
    DataSetDatabase,
    HostNetwork,
    InstanceState,
    Mount,
    RunningBackgroundService,
    RunningInstance,
)

PRODUCT_LABEL = "launchplane.product"
LANE_LABEL = "launchplane.lane"
BUILD_LABEL = "launchplane.build"
PART_LABEL = "launchplane.part"
DATA_SET_LABEL = "launchplane.data-set"
INSTANCE_LABEL = "launchplane.instance"
GATEWAY_MODE_OPTION = "com.docker.network.bridge.gateway_mode_ipv4"
QUIET_GATEWAY_MODE = "isolated"
MOST_READS = 3

type DockerState = Literal["created", "running", "paused", "restarting", "removing", "exited", "dead"]
type Part = Literal["web", "background-service", "database"]

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


@contextmanager
def attempting(action: str, address: str) -> Generator[None]:
    try:
        yield
    except (DockerException, requests.RequestException) as error:
        raise ContainerHostError(f"{action} on {address} failed: {error}") from error


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


@dataclass(frozen=True)
class OnHost:
    instances: list[RunningInstance]
    background_services: list[RunningBackgroundService]
    data_sets: list[DataSet]
    networks: list[HostNetwork] = field(default_factory=list[HostNetwork])


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
    if part not in ("web", "background-service", "database"):
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
    )


def running_for(details: DockerContainerDetails, now: datetime) -> float | None:
    if not details.state.running or details.state.restarting or details.state.paused:
        return None
    return (now - details.state.started_at).total_seconds()


def background_service_from(
    summary: DockerContainerSummary, details: Mapping[str, DockerContainerDetails], now: datetime
) -> RunningBackgroundService:
    if summary.name not in details:
        raise ContainerHostError(
            f"Docker listed the background service {summary.name} but did not describe it, so how long it has run is unknown."
        )
    return RunningBackgroundService(
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
    background_service_details: list[object] = field(default_factory=list[object])
    system: object = None


def background_service_details_by_name(answers: list[object]) -> dict[str, DockerContainerDetails]:
    described = [checked(DOCKER_CONTAINER_DETAILS, answer, "description of a background service") for answer in answers]
    return {details.name.removeprefix("/"): details for details in described}


def on_host_from_answers(answers: HostAnswers) -> OnHost:
    summaries = checked(DOCKER_CONTAINER_LIST, answers.containers, "container list")
    every_network = checked(DOCKER_NETWORK_LIST, answers.networks, "network list")
    volume_list = checked(DOCKER_VOLUME_LIST, answers.volumes, "volume list").volumes or []
    marked_networks = [network for network in every_network if PRODUCT_LABEL in (network.labels or {})]
    by_part: dict[Part, list[DockerContainerSummary]] = {"web": [], "background-service": [], "database": []}
    for summary in summaries:
        by_part[part_of(summary)].append(summary)
    background_services: list[RunningBackgroundService] = []
    if by_part["background-service"]:
        now = checked(DOCKER_SYSTEM_TIME, answers.system, "system information").system_time
        details = background_service_details_by_name(answers.background_service_details)
        background_services = [background_service_from(summary, details, now) for summary in by_part["background-service"]]
    return OnHost(
        instances=[instance_from(summary) for summary in by_part["web"]],
        background_services=background_services,
        data_sets=data_sets_from(by_part["database"], marked_networks, volume_list),
        networks=[network_from(network) for network in every_network],
    )


def background_service_names(containers: object) -> list[str]:
    summaries = checked(DOCKER_CONTAINER_LIST, containers, "container list")
    return [summary.name for summary in summaries if part_of(summary) == "background-service"]


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
