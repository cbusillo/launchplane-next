from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from launchplane import steps
from launchplane.instance import LaneReading, LaneRoute, background_service_container_of
from launchplane.steps import Action, Event

type Tell = Callable[[str, str | None], None]


# What the host answered a change sent to it. A change with no answer has none of these, whatever the host shows later.
class Said(StrEnum):
    CHANGED = "changed"
    ALREADY = "already as asked"
    REFUSED = "refused"


# What a later reading of the host showed of a change that had no answer. Only its own mark puts it down to the request.
class Seen(StrEnum):
    OURS = "made by this request"
    AS_ASKED = "as asked, by whom is not known"
    NOT_AS_ASKED = "not as asked"


# Told once a change was answered: what the host said, the real error, and what a refusal had already changed.
type Answered = Callable[[Said | None, str | None, Sequence[str]], None]
type Change = Mapping[str, str | bool | None]


# Who a call works for. `hold` is asked before each action on the host and raises to stop the call without acting.
# `about_to` writes a change down before it is sent, and what it gives is told the answer. `look` is shown each reading.
# `cancel_asked` is asked between actions, and `refuse_cancel` is told why when the lane can no longer be put back.
@dataclass
class Witness:
    tell: Tell = lambda _did, _error: None
    hold: Callable[[], None] = lambda: None
    cancel_asked: Callable[[], bool] = lambda: False
    refuse_cancel: Callable[[str], None] = lambda _why: None
    generation: str = ""
    attempt: int = 0
    about_to: Callable[[str, Change], Answered] = lambda _did, _change: lambda _said, _error, _changed: None
    look: Callable[[LaneRoute, LaneReading | None], None] = lambda _route, _lane: None


NOBODY = Witness()


NAMED: dict[type, Callable[[Any], str]] = {
    steps.StartProxy: lambda _step: "start the proxy",
    steps.AwaitRoute: lambda step: f"wait for the proxy to load the route of {step.web_address}",
    steps.EditRoute: lambda step: f"send {step.web_address} to {step.to}" if step.to else f"remove the route of {step.web_address}",
    steps.Switch: lambda step: f"switch {step.web_address} to {step.container}",
    steps.Discard: lambda step: f"retire {step.container}",
    steps.Abandon: lambda step: f"mark the release of {step.web_address} as cancelled",
    steps.CreateDataSetNetwork: lambda step: f"make the network of the data set {step.data_set}",
    steps.CreateDataSetVolume: lambda step: f"make the volume of the data set {step.data_set}",
    steps.CreateDatabase: lambda step: f"make the database server of the data set {step.data_set}",
    steps.StartDatabase: lambda step: f"start the database {step.container}",
    steps.AwaitDatabase: lambda step: f"wait for the database {step.container} to accept connections",
    steps.DeleteDataSet: lambda step: f"delete the data set {step.found.data_set}",
    steps.StartInstance: lambda step: f"start the instance {step.container} of {step.target.build}",
    steps.StartBackgroundService: lambda step: f"start the background service of {step.instance}",
    steps.Retire: lambda step: f"retire {step.container}",
    steps.RemoveLogins: lambda step: f"remove the logins no container holds from {step.database}",
    steps.Check: lambda step: f"check {step.container} at {step.health_path}",
    steps.AwaitBackgroundService: lambda step: f"watch the background service {step.container} stay up",
    steps.StopPart: lambda step: f"stop {step.container}",
    steps.StopDatabase: lambda step: f"stop the database {step.container}",
    steps.StartStopped: lambda step: f"start {step.container}",
    steps.ReadLog: lambda step: f"read the log of {step.container}",
}


def named(action: Action) -> str:
    return NAMED[type(action)](action)


def container_of(step: Any) -> str:  # noqa: ANN401 any step that names its container
    return step.container


def data_set_of(step: Any) -> str:  # noqa: ANN401 any step that names its data set
    return step.data_set


# The verb and the one subject of each step that changes the host. A step that reads, waits, checks or only asks the gate
# is not here. `make` is of a thing that carries the generation of the request that made it, so a later reading of the
# host can put it down to that request.
CHANGES: dict[type, tuple[str, Callable[[Any], str]]] = {
    steps.EditRoute: ("route", lambda step: step.web_address),
    steps.Switch: ("route", lambda step: step.web_address),
    steps.StartInstance: ("make", container_of),
    steps.StartBackgroundService: ("make", lambda step: background_service_container_of(step.instance)),
    steps.CreateDataSetNetwork: ("make", data_set_of),
    steps.CreateDataSetVolume: ("make part of", data_set_of),
    steps.CreateDatabase: ("make part of", data_set_of),
    steps.StartDatabase: ("start", container_of),
    steps.StartStopped: ("start", container_of),
    steps.StartProxy: ("start", lambda _step: "the proxy"),
    steps.StopPart: ("stop", container_of),
    steps.StopDatabase: ("stop", container_of),
    steps.Retire: ("remove", container_of),
    steps.Discard: ("remove", container_of),
    steps.DeleteDataSet: ("delete", lambda step: step.found.data_set),
    steps.RemoveLogins: ("remove logins", lambda step: step.database),
}


def change_of(action: Action) -> dict[str, str | bool | None] | None:
    if type(action) not in CHANGES:
        return None
    verb, subject = CHANGES[type(action)]
    change: dict[str, str | bool | None] = {"verb": verb, "subject": subject(action)}
    if isinstance(action, steps.EditRoute):
        return change | {"to": action.to, "was": action.was}
    return change | {"to": action.container} if isinstance(action, steps.Switch) else change


# One line for a thing done, and the real error where it was refused.
def told(action: Action, event: Event) -> tuple[str, str | None]:
    match event:
        case steps.Refused(report=report):
            return named(action), report
        case steps.LogRead(unreadable=unreadable):
            return named(action), unreadable
        case steps.Discarded(kept=True):
            return named(action), "The gate answered that the routes file was renamed to it, so it was kept."
        case steps.Switched(won=False):
            return named(action), "The gate did not rename the routes file to it."
        case _:
            return named(action), None
