import secrets
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

LANE_NETWORK = "launchplane-lanes"


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


class RunningJob(BaseModel):
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


def new_container_name(target: LaneBuild) -> str:
    return f"{target.product}-{target.lane}-{secrets.token_hex(6)}"


def new_data_set_name() -> str:
    return secrets.token_hex(6)


def new_password() -> str:
    return secrets.token_urlsafe(32)


def job_container_of(instance_container: str) -> str:
    return f"{instance_container}-job"
