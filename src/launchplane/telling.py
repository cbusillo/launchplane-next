from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from launchplane import steps
from launchplane.instance import LaneRoute
from launchplane.steps import Action, Event

type Tell = Callable[[str, str | None], None]


# What the gate answered a change of the route. A change with no answer has none of these, whatever the host shows later.
class Said(StrEnum):
    CHANGED = "changed"
    ALREADY = "already as asked"
    REFUSED = "refused"


# What a later reading showed of a change of the route that had no answer. Only a routes file that names the request
# and the attempt that sent the change puts it down to the request.
class Seen(StrEnum):
    OURS = "made by this request"
    AS_ASKED = "as asked, by whom is not known"
    NOT_AS_ASKED = "not as asked"


# Told once a change of the route was answered: what the gate said, and the real error.
type Answered = Callable[[Said | None, str | None], None]


# Who a call works for. `hold` is asked before each action on the host and raises to stop the call without acting.
# `cancel_asked` is asked between actions, and `refuse_cancel` is told why when the lane can no longer be put back.
# `about_to` writes a change of the route down before it is sent, with where it sends visitors and where they went, and
# what it gives is told the answer. `look` is shown the route of each reading. `again` is a request taken up before:
# what its earlier attempts changed is not known to this one.
@dataclass
class Witness:
    tell: Tell = lambda _did, _error: None
    hold: Callable[[], None] = lambda: None
    cancel_asked: Callable[[], bool] = lambda: False
    refuse_cancel: Callable[[str], None] = lambda _why: None
    generation: str = ""
    attempt: int = 0
    about_to: Callable[[str, str | None, str | None], Answered] = lambda _did, _to, _was: lambda _said, _error: None
    look: Callable[[LaneRoute], None] = lambda _route: None
    again: bool = False


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
