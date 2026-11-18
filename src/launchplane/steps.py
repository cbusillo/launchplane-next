from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from launchplane.instance import DataSet, DataSetUse, LaneBuild, Verdict


class Outcome(StrEnum):
    SWITCHED = "switched"
    UNCHANGED = "unchanged"
    REPAIRED = "repaired"
    CHECK_FAILED = "check failed"
    BACKGROUND_SERVICE_FAILED = "background service failed"


class MoveResult(BaseModel):
    product: str
    lane: str
    build: str
    web_address: str
    outcome: Outcome
    container_receiving_visitors: str | None
    data_set: str | None = None
    did: list[str] = []
    error: str | None = None


class RetireOutcome(StrEnum):
    RETIRED = "retired"
    DELETED = "deleted"
    UNCHANGED = "unchanged"


class RetireResult(BaseModel):
    product: str
    lane: str
    web_address: str
    outcome: RetireOutcome
    route_removed_to_container: str | None
    retired_containers: list[str]
    deleted_data_set: str | None = None
    kept_data_sets: list[str] = []


class MaintenanceOutcome(StrEnum):
    IN_MAINTENANCE = "in maintenance"
    DATABASE_KILLED = "database killed"
    ENDED = "ended"
    NOT_IN_MAINTENANCE = "not in maintenance"
    WEB_PART_FAILED = "web part failed"
    BACKGROUND_SERVICE_FAILED = "background service failed"


class MaintenanceResult(BaseModel):
    product: str
    lane: str
    web_address: str
    outcome: MaintenanceOutcome
    in_maintenance: bool
    since: datetime | None = None
    error: str | None = None


# The answer of a request that a cancel ended: what was done to put its lane back as it was.
class PutBack(BaseModel):
    put_back: list[str] = []
    # Data sets the request made. Putting a lane back never deletes data, so they stay.
    data_sets_kept: list[str] = []
    # What was found on the lane when the cancel ended, whoever left it.
    findings: list[str] = []


@dataclass(frozen=True)
class FreshNames:
    container: str
    data_set: str


# The `generation` of a step is the stamp put on what it makes.
@dataclass(frozen=True)
class StartProxy:
    pass


@dataclass(frozen=True)
class AwaitRoute:
    web_address: str
    container: str | None


@dataclass(frozen=True)
class CreateDataSetNetwork:
    product: str
    lane: str
    data_set: str
    generation: str | None = None


@dataclass(frozen=True)
class CreateDataSetVolume:
    product: str
    lane: str
    data_set: str


@dataclass(frozen=True)
class CreateDatabase:
    product: str
    lane: str
    data_set: str
    network: str
    volume: str
    image: str


@dataclass(frozen=True)
class StartDatabase:
    container: str


@dataclass(frozen=True)
class AwaitDatabase:
    container: str


@dataclass(frozen=True)
class StartInstance:
    target: LaneBuild
    container: str
    use: DataSetUse | None = None
    generation: str | None = None


@dataclass(frozen=True)
class Check:
    container: str
    health_path: str = "/"


@dataclass(frozen=True)
class WriteRoute:
    web_address: str
    container: str


# What passes the gate, and what each is once taken: the verdict the gate gave, which the call acts on from then on.
@dataclass(frozen=True)
class Switch:
    web_address: str
    container: str
    generation: str


@dataclass(frozen=True)
class Switched:
    container: str
    verdict: Verdict


# An instance that receives no visitors, taken through the gate and then retired: a rename on its way to it is refused
# from there on. Where the rename had already happened, the verdict is committed and the instance stays.
@dataclass(frozen=True)
class Discard:
    web_address: str
    container: str


@dataclass(frozen=True)
class Discarded:
    container: str
    verdict: Verdict


# A cancel carried to the gate.
@dataclass(frozen=True)
class Abandon:
    web_address: str
    generation: str


@dataclass(frozen=True)
class Abandoned:
    verdict: Verdict


# Waiting for the proxy to load a route that is already renamed: the release is real, so not loading yet is waited
# for again and is never its failure.
@dataclass(frozen=True)
class AwaitSwitch:
    web_address: str
    container: str


@dataclass(frozen=True)
class NotLoadedYet:
    container: str
    report: str


@dataclass(frozen=True)
class StartBackgroundService:
    target: LaneBuild
    instance: str
    use: DataSetUse | None
    command: tuple[str, ...]
    generation: str | None = None


@dataclass(frozen=True)
class AwaitBackgroundService:
    container: str


@dataclass(frozen=True)
class RemoveRoute:
    web_address: str
    container: str | None


@dataclass(frozen=True)
class Retire:
    container: str


@dataclass(frozen=True)
class RemoveInstance:
    container: str


@dataclass(frozen=True)
class DeleteDataSet:
    found: DataSet


@dataclass(frozen=True)
class RemoveLogins:
    database: str


@dataclass(frozen=True)
class ShowBackSoon:
    web_address: str


@dataclass(frozen=True)
class StopPart:
    container: str


@dataclass(frozen=True)
class StopDatabase:
    container: str


@dataclass(frozen=True)
class StartStopped:
    container: str


@dataclass(frozen=True)
class ReadLog:
    container: str


@dataclass(frozen=True)
class LogRead:
    container: str
    tail: str
    unreadable: str | None = None


@dataclass(frozen=True)
class StartRefused:
    container: str
    report: str


@dataclass(frozen=True)
class CheckRefused:
    container: str
    report: str


@dataclass(frozen=True)
class BackgroundServiceRefused:
    container: str
    report: str


@dataclass(frozen=True)
class Done[Answer]:
    answer: Answer


@dataclass(frozen=True)
class Stop:
    reason: str


# A cancel that came after the point where the lane can be put back. The call goes on as asked.
@dataclass(frozen=True)
class TooLate:
    reason: str


# A cancel was asked of the call. `refused` is whether the host has shown it was too late.
@dataclass(frozen=True)
class Cancel:
    refused: bool = False


type Action = (
    StartProxy
    | AwaitRoute
    | WriteRoute
    | Switch
    | Discard
    | Abandon
    | AwaitSwitch
    | RemoveRoute
    | CreateDataSetNetwork
    | CreateDataSetVolume
    | CreateDatabase
    | StartDatabase
    | AwaitDatabase
    | DeleteDataSet
    | StartInstance
    | StartBackgroundService
    | Retire
    | RemoveInstance
    | RemoveLogins
    | Check
    | AwaitBackgroundService
    | ShowBackSoon
    | StopPart
    | StopDatabase
    | StartStopped
    | ReadLog
)
type Event = (
    Action | CheckRefused | BackgroundServiceRefused | StartRefused | LogRead | Switched | Discarded | Abandoned | NotLoadedYet
)
