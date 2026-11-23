from collections.abc import Callable, Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from launchplane import steps
from launchplane.instance import LaneRoute
from launchplane.steps import Action, Command, Event, Sent, Verb

type Tell = Callable[[str, str | None], None]


# What the gate answered a change of the route, or Docker a command. Only `changed` is a change the sender made.
class Said(StrEnum):
    CHANGED = "changed"
    ALREADY = "already as asked"
    REFUSED = "refused"


# Told once a change was sent: what the gate or Docker answered, None for no answer, and the real error.
type Answered = Callable[[Said | None, str | None], None]


def not_written(_did: str, _web_address: str, _to: str | None) -> Answered:
    return lambda _made, _error: None


# Who a call works for. `hold` is asked before each action on the host and raises to stop the call without acting.
# `cancel_asked` is asked between actions, and `refuse_cancel` is told why when the lane can no longer be put back.
# `write` writes the line of a change of the route or of a command for a part before it is sent, and what it gives is
# told the answer. `about_to` does so for a change of the route, from the web address and where it sends visitors.
# `look` is shown the route of each reading and the containers read as made under `generation`, and nothing is decided
# from what it does. `credit` is given each command Docker answered as done. `purpose` ends the line of a command sent
# to put a lane back. `unmade` is whether a command of the step was not answered as done.
@dataclass
class Witness:
    tell: Tell = lambda _did, _error: None
    hold: Callable[[], None] = lambda: None
    cancel_asked: Callable[[], bool] = lambda: False
    refuse_cancel: Callable[[str], None] = lambda _why: None
    generation: str = ""
    attempt: int = 0
    about_to: Callable[[str, str, str | None], Answered] = not_written
    look: Callable[[LaneRoute, Collection[str]], None] = lambda _route, _made: None
    write: Callable[[str, Sent | Command], Answered] = lambda _did, _sent: lambda _made, _error: None
    credit: Callable[[Command], None] = lambda _command: None
    purpose: str = ""
    unmade: bool = False

    def __post_init__(self) -> None:
        if self.about_to is not_written:
            self.about_to = lambda did, web_address, to: self.write(did, Sent(web_address=web_address, to=to))

    def __repr__(self) -> str:
        return f"the witness of attempt {self.attempt} of {self.generation or 'no request'}"

    # One command for one named container. A line that cannot be written raises here, so the command is not sent.
    def sending(self, verb: Verb, container: str) -> Answered:
        command = Command(verb=verb, container=container)
        answered = self.write(f"{verb} {container}{self.purpose}", command)

        def heard(said: Said | None, error: str | None) -> None:
            answered(said, error)
            if said is Said.CHANGED:
                self.credit(command)
            elif said is not Said.ALREADY:
                self.unmade = True

        return heard

    # A step for a part is told only where no command of it was refused or left unanswered: that line has said so.
    def tell_of_step(self, did: str, error: str | None) -> None:
        if not self.unmade:
            self.tell(did, error)


NOBODY = Witness()

# Ends the line of a change that puts a lane back. What a cancel put back is told from these lines.
TO_PUT_BACK = ", to put the lane back"


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


# The steps for a part. Each command has its own line, so the step is told only where no command's line says why.
OF_A_PART = (steps.StartInstance, steps.StartBackgroundService, steps.Retire, steps.Discard, steps.StopPart, steps.StartStopped)

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
