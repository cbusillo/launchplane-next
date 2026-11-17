import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

LANE_NETWORK = "launchplane-lanes"
LANE_DOMAIN = "test"
BACK_SOON = "launchplane-back-soon"


class InstanceState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    RESTARTING = "restarting"
    STOPPED = "stopped"


class Mount(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    path: str


class RunningInstance(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    lane: str | None
    build: str | None
    container: str
    state: InstanceState
    networks: tuple[str, ...]
    data_set: str | None = None
    mounts: tuple[Mount, ...] = ()


class RunningBackgroundService(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    lane: str | None
    build: str | None
    container: str
    state: InstanceState
    networks: tuple[str, ...]
    running_for_seconds: float | None
    instance: str | None
    mounts: tuple[Mount, ...] = ()
    data_set: str | None = None


class HostNetwork(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    is_quiet: bool


class Unread(BaseModel):
    model_config = ConfigDict(frozen=True)

    why: str


class DataSetDatabase(BaseModel):
    model_config = ConfigDict(frozen=True)

    container: str
    state: InstanceState
    networks: tuple[str, ...]
    # Asked only of a running server; while nothing was asked it is unknown, never no.
    accepts_connections: bool | None = None
    mounts: tuple[Mount, ...] = ()
    exit_code: int | None = None
    image: str | None = None
    unheld_logins: tuple[str, ...] | Unread | None = None


class DataSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    lane: str | None
    data_set: str
    networks: tuple[HostNetwork, ...]
    volumes: tuple[str, ...]
    databases: tuple[DataSetDatabase, ...]


class LaneBuild(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    lane: str
    build: str


@dataclass(frozen=True)
class DataSetUse:
    data_set: str
    network: str
    database_container: str


class RouteStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    WARNING = "warning"


@dataclass(frozen=True)
class LoadedRoute:
    container: str
    status: RouteStatus


@dataclass(frozen=True)
class WrittenRoute:
    container: str
    written_at: datetime


@dataclass(frozen=True)
class LaneRoute:
    loaded: LoadedRoute | None
    written: str | None
    back_soon_since: datetime | None = None
    proxy_runs: bool = True
    back_soon_runs: bool = True

    # The mark is the lane's own route file, so it reads the same while the proxy is down.
    @property
    def in_maintenance(self) -> bool:
        return self.written == BACK_SOON

    # What visitors get is what the running proxy has loaded, never what the file says.
    @property
    def sends_visitors_to(self) -> str | None:
        if not self.proxy_runs or self.loaded is None or self.loaded.status != RouteStatus.ENABLED:
            return None
        return self.loaded.container


@dataclass(frozen=True)
class ProxySeen:
    runs: bool
    back_soon_runs: bool
    written: Mapping[str, WrittenRoute] = field(default_factory=dict[str, WrittenRoute])
    loaded: Mapping[str, LoadedRoute] = field(default_factory=dict[str, LoadedRoute])

    def route_of(self, web_address: str) -> LaneRoute:
        written = self.written.get(web_address)
        return LaneRoute(
            loaded=self.loaded.get(web_address),
            written=None if written is None else written.container,
            back_soon_since=written.written_at if written is not None and written.container == BACK_SOON else None,
            proxy_runs=self.runs,
            back_soon_runs=self.back_soon_runs,
        )

    def sends_visitors_to(self) -> dict[str, str]:
        return {web_address: route.container for web_address, route in self.loaded.items() if route.status == RouteStatus.ENABLED}


def web_address_of(product: str, lane: str) -> str:
    return f"{lane}.{product}.{LANE_DOMAIN}"


@dataclass(frozen=True)
class OnHost:
    instances: list[RunningInstance]
    background_services: list[RunningBackgroundService]
    data_sets: list[DataSet]
    networks: list[HostNetwork] = field(default_factory=list[HostNetwork])


@dataclass(frozen=True)
class LaneReading:
    product: str
    lane: str
    route: LaneRoute
    instances: tuple[RunningInstance, ...] = ()
    background_services: tuple[RunningBackgroundService, ...] = ()
    data_sets: tuple[DataSet, ...] = ()
    network_quiet: Mapping[str, bool] = field(default_factory=dict[str, bool])
    # Why the web address did not answer, where it was asked and did not.
    web_address_failure: str | None = None

    @property
    def web_address(self) -> str:
        return web_address_of(self.product, self.lane)

    @property
    def active(self) -> RunningInstance | None:
        return next((instance for instance in self.instances if instance.container == self.route.sends_visitors_to), None)

    @property
    def databases(self) -> list[DataSetDatabase]:
        return [database for found in self.data_sets for database in found.databases]

    def data_set(self, name: str | None) -> DataSet | None:
        return next((found for found in self.data_sets if found.data_set == name), None)


class PartOfLane(Protocol):
    @property
    def product(self) -> str: ...
    @property
    def lane(self) -> str | None: ...


@dataclass(frozen=True)
class Reading:
    read_at: datetime
    proxy: ProxySeen
    # Absent when only the routes were asked for.
    on_host: OnHost | None = None
    # Why each web address asked through the proxy did not answer, or None where it did.
    web_address_failures: Mapping[str, str | None] = field(default_factory=dict[str, str | None])

    @property
    def parts(self) -> OnHost:
        if self.on_host is None:
            raise ValueError("Only the routes were read, and the parts of a lane were asked of that reading.")
        return self.on_host

    def lanes(self) -> list[tuple[str, str]]:
        parts: list[PartOfLane] = [*self.parts.instances, *self.parts.background_services, *self.parts.data_sets]
        found: set[tuple[str, str]] = set()
        for part in parts:
            lane = part.lane
            if lane is not None:
                found.add((part.product, lane))
        return sorted(found)

    def lane(self, product: str, lane: str) -> LaneReading:
        def of_lane[Part: PartOfLane](parts: list[Part]) -> tuple[Part, ...]:
            return tuple(part for part in parts if (part.product, part.lane) == (product, lane))

        return LaneReading(
            product,
            lane,
            self.proxy.route_of(web_address_of(product, lane)),
            of_lane(self.parts.instances),
            of_lane(self.parts.background_services),
            of_lane(self.parts.data_sets),
            {network.name: network.is_quiet for network in self.parts.networks},
            self.web_address_failures.get(web_address_of(product, lane)),
        )


def new_container_name(target: LaneBuild) -> str:
    return f"{target.product}-{target.lane}-{secrets.token_hex(6)}"


def new_data_set_name() -> str:
    return secrets.token_hex(6)


def new_password() -> str:
    return secrets.token_urlsafe(32)


def background_service_container_of(instance_container: str) -> str:
    return f"{instance_container}-background-service"
