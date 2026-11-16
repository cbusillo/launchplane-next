import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

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


class DataSetDatabase(BaseModel):
    model_config = ConfigDict(frozen=True)

    container: str
    state: InstanceState
    networks: tuple[str, ...]
    accepts_connections: bool | None = None
    mounts: tuple[Mount, ...] = ()
    exit_code: int | None = None
    image: str | None = None


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

    @property
    def sends_visitors_to(self) -> str | None:
        if not self.proxy_runs:
            return self.written
        return self.loaded.container if self.loaded is not None and self.loaded.status == RouteStatus.ENABLED else None


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


@dataclass(frozen=True)
class LaneSeen:
    instances: tuple[RunningInstance, ...]
    route: LaneRoute
    background_services: tuple[RunningBackgroundService, ...] = ()
    data_sets: tuple[DataSet, ...] = ()
    network_quiet: Mapping[str, bool] = field(default_factory=dict[str, bool])


def web_address_of(product: str, lane: str) -> str:
    return f"{lane}.{product}.{LANE_DOMAIN}"


def new_container_name(target: LaneBuild) -> str:
    return f"{target.product}-{target.lane}-{secrets.token_hex(6)}"


def new_data_set_name() -> str:
    return secrets.token_hex(6)


def new_password() -> str:
    return secrets.token_urlsafe(32)


def background_service_container_of(instance_container: str) -> str:
    return f"{instance_container}-background-service"
