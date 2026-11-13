import hashlib
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

    @property
    def name(self) -> str:
        return f"{self.product}-{self.lane}-{hashlib.sha256(self.build.encode()).hexdigest()[:12]}"
