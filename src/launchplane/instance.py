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
    data_set: str | None = None


class RunningJob(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    lane: str | None
    build: str | None
    container: str
    state: InstanceState
    instance: str | None


class DataSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    lane: str | None
    data_set: str
    network_is_quiet: bool | None
    has_volume: bool
    database_container: str | None
    database_state: InstanceState | None


class LaneBuild(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    lane: str
    build: str


def new_container_name(target: LaneBuild) -> str:
    return f"{target.product}-{target.lane}-{secrets.token_hex(6)}"


def new_data_set_name() -> str:
    return secrets.token_hex(6)


def job_container_of(instance_container: str) -> str:
    return f"{instance_container}-job"
