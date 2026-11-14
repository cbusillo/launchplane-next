import hashlib
import json
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
        identity = json.dumps([self.product, self.lane, self.build])
        return f"{self.product}-{self.lane}-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
