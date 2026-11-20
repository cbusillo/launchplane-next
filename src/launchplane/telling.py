from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from launchplane import steps
from launchplane.steps import Action, Event

type Tell = Callable[[str, str | None], None]


# Who a call works for. `hold` is asked before each action on the host and raises to stop the call without acting.
# `cancel_asked` is asked between actions, and `refuse_cancel` is told why when the lane can no longer be put back.
@dataclass(frozen=True)
class Witness:
    tell: Tell = lambda _did, _error: None
    hold: Callable[[], None] = lambda: None
    cancel_asked: Callable[[], bool] = lambda: False
    refuse_cancel: Callable[[str], None] = lambda _why: None


NOBODY = Witness()

# Ends the line written before a change that puts a lane back. What a cancel put back is told from these lines.
TO_PUT_BACK = ", to put the lane back"


NAMED: dict[type, Callable[[Any], str]] = {
    steps.StartProxy: lambda _step: "start the proxy",
    steps.AwaitRoute: lambda step: f"wait for the proxy to load the route of {step.web_address}",
    steps.WriteRoute: lambda step: f"write the route of {step.web_address} to {step.container}",
    steps.Switch: lambda step: f"switch {step.web_address} to {step.container}",
    steps.Discard: lambda step: f"retire {step.container}",
    steps.Abandon: lambda step: f"mark the release of {step.web_address} as cancelled",
    steps.RemoveRoute: lambda step: f"remove the route of {step.web_address} to {step.container}",
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
    steps.ShowBackSoon: lambda step: f"switch {step.web_address} to the back soon page",
    steps.StopPart: lambda step: f"stop {step.container}",
    steps.StopDatabase: lambda step: f"stop the database {step.container}",
    steps.StartStopped: lambda step: f"start {step.container}",
    steps.ReadLog: lambda step: f"read the log of {step.container}",
}


# How the line of a step that only looks or waits begins. Every other line told with no refusal is a change.
LOOKS = ("wait for ", "check ", "watch ", "read the log ", "mark the release ")


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
