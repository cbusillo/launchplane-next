from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from launchplane.instance import DataSet, DataSetUse, LaneBuild


class Outcome(StrEnum):
    SWITCHED = "switched"
    UNCHANGED = "unchanged"
    REPAIRED = "repaired"
    CHECK_FAILED = "check failed"
    BACKGROUND_SERVICE_FAILED = "background service failed"


# Every answer says which servers did not tell their logins, so that none was looked for there.
class Result(BaseModel):
    logins_not_read: list[str] = []


class MoveResult(Result):
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


class RetireResult(Result):
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


class MaintenanceResult(Result):
    product: str
    lane: str
    web_address: str
    outcome: MaintenanceOutcome
    in_maintenance: bool
    since: datetime | None = None
    error: str | None = None


# The answer of a request that a cancel ended: what was done to put its lane back as it was.
class PutBack(Result):
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
    # Whether the proxy must also have the route enabled, so that visitors are sent there.
    enabled: bool = False


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


# The gate makes it only from the routes file as it was `read`. With nothing `to` send visitors to, the route is removed.
@dataclass(frozen=True)
class EditRoute:
    web_address: str
    to: str | None
    read: str
    was: str | None = None
    # Once taken: the gate found the routes file already as asked, so this sending changed nothing.
    already: bool = field(default=False, compare=False)


# The rename of a release, asked of the gate for the candidate of one attempt. `Switched` is what it is once taken:
# whether the gate then said the generation is committed to that candidate.
@dataclass(frozen=True)
class Switch:
    web_address: str
    container: str
    generation: str
    attempt: int


@dataclass(frozen=True)
class Switched:
    container: str
    won: bool


# An instance taken through the gate and then removed: either the gate says its rename won, and it is kept, or it can
# never be switched to from there on. `generation` is the release that made it, where one did.
@dataclass(frozen=True)
class Discard:
    web_address: str
    container: str
    generation: str | None
    attempt: int


@dataclass(frozen=True)
class Discarded:
    container: str
    kept: bool


# A cancel carried to the gate.
@dataclass(frozen=True)
class Abandon:
    web_address: str
    generation: str


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
class Retire:
    container: str


@dataclass(frozen=True)
class DeleteDataSet:
    found: DataSet


@dataclass(frozen=True)
class RemoveLogins:
    database: str


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


type Verb = Literal["make", "start", "stop", "remove"]
DONE: dict[Verb, str] = {"make": "made", "start": "started", "stop": "stopped", "remove": "removed"}


# A change of the route as it is sent: the web address and where it sends visitors, or nowhere for a removal.
class Sent(BaseModel):
    web_address: str
    to: str | None


# One command sent to Docker for one named container. Among what a call has done, it is one Docker answered as done.
class Command(BaseModel, frozen=True):
    verb: Verb
    container: str

    # The words of its line, and of a request's account of what it did.
    def as_sent(self) -> str:
        return f"{self.verb} {self.container}"

    def as_done(self) -> str:
        return f"{DONE[self.verb]} {self.container}"


# What a part answered when it would not do as asked, with the real error.
@dataclass(frozen=True)
class Refused:
    container: str
    report: str


class StartRefused(Refused):
    pass


class CheckRefused(Refused):
    pass


class BackgroundServiceRefused(Refused):
    pass


# Docker answered that it will not remove the part.
class RemovalRefused(Refused):
    pass


# A read a removal of logins stands on failed, so none was sent. A removal the server refuses is an error, never this.
class LoginsUnread(Refused):
    pass


@dataclass(frozen=True)
class Done[Answer]:
    answer: Answer


@dataclass(frozen=True)
class Stop:
    reason: str


# Neither a failure nor an answer: what was read contradicts the gate, or cannot say what must be known before a part is
# touched. Nothing is done, and the move is planned again from a later reading.
class Wait(Stop):
    pass


# A cancel won at the gate and a part of its release could not be removed: the lane cannot be put back.
@dataclass(frozen=True)
class CannotPutBack:
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
    | EditRoute
    | Switch
    | Discard
    | Abandon
    | CreateDataSetNetwork
    | CreateDataSetVolume
    | CreateDatabase
    | StartDatabase
    | AwaitDatabase
    | DeleteDataSet
    | StartInstance
    | StartBackgroundService
    | Retire
    | RemoveLogins
    | Check
    | AwaitBackgroundService
    | StopPart
    | StopDatabase
    | StartStopped
    | ReadLog
)
type Event = Action | Refused | LogRead | Switched | Discarded | Command
