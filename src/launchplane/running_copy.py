import secrets
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class CopyState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    RESTARTING = "restarting"
    STOPPED = "stopped"


class RunningCopy(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    lane: str | None
    build: str | None
    name: str
    state: CopyState


class LaneCopy(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    lane: str
    build: str


def new_copy_name(copy: LaneCopy) -> str:
    return f"{copy.product}-{copy.lane}-{secrets.token_hex(6)}"
