import secrets
import uuid
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


# No answer: the question was put and what came back cannot be used. Never taken for nothing there.
class Unread(BaseModel):
    model_config = ConfigDict(frozen=True)

    asked: str
    why: str

    def __str__(self) -> str:
        return f"{self.asked} could not be read: {self.why}"


# More than one thing where the question has room for one. No one of them is taken for the answer.
@dataclass(frozen=True)
class Conflicting[Seen]:
    asked: str
    seen: tuple[Seen, ...]

    def __str__(self) -> str:
        return f"{self.asked} has {len(self.seen)} answers where it must have one: {'; '.join(map(str, self.seen))}"


# What a question asked of the host was answered: the value, None for nothing there, or one of the two that are no
# answer. It stays one of the four from the reading to whatever is decided from it.
type Answered[Value] = Value | Unread | Conflicting[Value] | None
# The generation that made a part, where one did. A stamp that is not a generation is unread.
type Stamp = Answered[str]


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
    generation: Stamp = None


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
    generation: Stamp = None


class HostNetwork(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    is_quiet: bool


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
    # Asked only of a server that accepts connections. A list that was read and is empty is ().
    unheld_logins: Answered[tuple[str, ...]] = None


class DataSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    lane: str | None
    data_set: str
    networks: tuple[HostNetwork, ...]
    volumes: tuple[str, ...]
    databases: tuple[DataSetDatabase, ...]
    # Stamped on its network, the first part of it that is made.
    generation: Stamp = None


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


# What the gate has decided for a generation, and the only thing it is told apart by from then on.
class Verdict(StrEnum):
    COMMITTED = "committed"
    ABANDONED = "abandoned"
    NONE = "none"


class RouteStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    WARNING = "warning"


@dataclass(frozen=True)
class LoadedRoute:
    container: str
    status: RouteStatus
    # Which of the proxy's routers it is: what tells two for one web address apart, and nothing else.
    router: str = field(default="", compare=False)

    def __str__(self) -> str:
        return f"the router {self.router} to {self.container}, {self.status}"


@dataclass(frozen=True)
class WrittenRoute:
    container: str
    written_at: datetime


# What the proxy has loaded and what the routes file says are two observations. A file ahead of the proxy is a wait.
@dataclass(frozen=True)
class LaneRoute:
    loaded: LoadedRoute | Conflicting[LoadedRoute] | None
    file: Answered[WrittenRoute]
    proxy_runs: bool = True
    back_soon_runs: bool = True

    @property
    def written(self) -> str | None:
        return self.file.container if isinstance(self.file, WrittenRoute) else None

    # The mark is the lane's own route file, so it reads the same while the proxy is down.
    @property
    def in_maintenance(self) -> bool:
        return self.written == BACK_SOON

    @property
    def back_soon_since(self) -> datetime | None:
        return self.file.written_at if isinstance(self.file, WrittenRoute) and self.in_maintenance else None

    # What visitors get is what the running proxy has loaded, never what the file says.
    @property
    def sends_visitors_to(self) -> str | None:
        if not self.proxy_runs or not isinstance(self.loaded, LoadedRoute) or self.loaded.status != RouteStatus.ENABLED:
            return None
        return self.loaded.container

    # Every container the routes file or a loaded router names, so each side of a conflict too.
    @property
    def names(self) -> set[str]:
        loaded = self.loaded.seen if isinstance(self.loaded, Conflicting) else (self.loaded,)
        return {each.container for each in (*loaded, self.file) if isinstance(each, LoadedRoute | WrittenRoute)}


@dataclass(frozen=True)
class ProxySeen:
    runs: bool
    back_soon_runs: bool
    written: Mapping[str, WrittenRoute | Unread] = field(default_factory=dict[str, WrittenRoute | Unread])
    loaded: Mapping[str, LoadedRoute | Conflicting[LoadedRoute]] = field(
        default_factory=dict[str, LoadedRoute | Conflicting[LoadedRoute]]
    )

    def route_of(self, web_address: str) -> LaneRoute:
        return LaneRoute(self.loaded.get(web_address), self.written.get(web_address), self.runs, self.back_soon_runs)

    def sends_visitors_to(self) -> dict[str, str]:
        one = {web_address: route for web_address, route in self.loaded.items() if isinstance(route, LoadedRoute)}
        return {web_address: route.container for web_address, route in one.items() if route.status == RouteStatus.ENABLED}


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


# A number no release has had before, also when Launchplane's own record is lost: it is made from the time and from chance,
# never counted up from what the record or the host holds.
def new_generation() -> str:
    return str(uuid.uuid7())


def is_generation(text: str) -> bool:
    try:
        return str(uuid.UUID(text)) == text
    except ValueError:
        return False


def new_password() -> str:
    return secrets.token_urlsafe(32)


def background_service_container_of(instance_container: str) -> str:
    return f"{instance_container}-background-service"
