import secrets
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class InstanceState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    RESTARTING = "restarting"
    STOPPED = "stopped"


class RunningInstance(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    lane: str | None
    build: str | None
    container: str
    state: InstanceState
    is_quiet: bool
    data_set: str | None = None


class RunningJob(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    lane: str | None
    build: str | None
    container: str
    state: InstanceState
    is_quiet: bool
    instance: str | None


class DataSetNetwork(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    is_quiet: bool


class DataSetDatabase(BaseModel):
    model_config = ConfigDict(frozen=True)

    container: str
    state: InstanceState


class DataSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    lane: str | None
    data_set: str
    networks: tuple[DataSetNetwork, ...]
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
