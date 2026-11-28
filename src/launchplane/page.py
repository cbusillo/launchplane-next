import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from launchplane.act import Status
from launchplane.gate import Answer as GateAnswer
from launchplane.instance import BACK_SOON, DataSet, InstanceState, PartOfLane, RunningInstance, Verdict
from launchplane.judge import Condition, Finding, Judgement, Kind, Shown, Standing, WebAddressJudged, Work
from launchplane.record import ENDED, Event, Kept, Request, Stands, how_cancel_won, how_cancelled, how_replaced
from launchplane.steps import Outcome
from launchplane.updating import why_kept

TONE = {
    Condition.GOOD: Standing.GOOD,
    Condition.IN_MAINTENANCE: Standing.ON_PURPOSE,
    Condition.UNDER_WAY: Standing.ON_PURPOSE,
    Shown.UNDER_WAY: Standing.ON_PURPOSE,
    Condition.UNFINISHED: Standing.WRONG,
    Shown.LEFT_UNFINISHED: Standing.WRONG,
    Shown.WRONG: Standing.WRONG,
    Shown.UNREAD: Standing.UNREAD,
    Shown.CONFLICTING: Standing.CONFLICTING,
}
STANDS_TONE = {
    Stands.WAITING: Standing.ON_PURPOSE,
    Stands.WORKING: Standing.ON_PURPOSE,
    Stands.CANCELLED: Standing.ON_PURPOSE,
    Stands.REPLACED: Standing.ON_PURPOSE,
    Stands.DONE: Standing.GOOD,
}
RECORD_UNREAD = "Launchplane's record cannot be read"
NOT_KNOWN_AT_WORK = (
    "What is below is read from the host. Whether a request is at work on a lane is not known, so what would be under way "
    "or left unfinished shows as could not be read. A call that changes something is refused until the record can be read."
)
NOTHING_ON_THE_HOST = "The host answered, and nothing marked for Launchplane is on it."
NO_LANE = "Marked for Launchplane, and in no lane"
UNMARKED = "no lane label"


class Pill(BaseModel):
    text: str
    tone: Standing


class Piece(BaseModel):
    text: str
    drawn_as: Literal["text", "code", "build"] = "text"


class Row(BaseModel):
    name: str
    value: list[Piece]
    pills: list[Pill] = []
    note: str | None = None


class Card(BaseModel):
    title: str
    pills: list[Pill] = []
    in_maintenance_since: datetime | None = None
    rows: list[Row] = []


MOVE_FAILED = Pill(text="move failed", tone=Standing.WRONG)
LOG_FOLLOWS = "\nIts last log lines:\n"
SAYS_AN_ERROR = re.compile("error|exception|critical|fatal", re.IGNORECASE)


# What failed, and the last line of the log after it that speaks of an error. The log whole is in the request's answer.
def in_one_line(error: str) -> str:
    what, _, log = error.partition(LOG_FOLLOWS)
    last = next((line.strip() for line in reversed(log.splitlines()) if SAYS_AN_ERROR.search(line)), None)
    return what if last is None else f"{what} The last error in its log: {last}"


def said(text: Condition | Shown) -> Pill:
    return Pill(text=text, tone=TONE[text])


def code(text: str) -> Piece:
    return Piece(text=text, drawn_as="code")


# What a move that started nothing it left behind says of the lane it could not bring up, by how it ended.
LEFT_NOTHING = {
    Outcome.CHECK_FAILED: (
        "Request {id} started an instance that failed its check, so nothing was switched and that instance is gone. "
        "Asking for that build again fails the same way until what the check found is put right."
    ),
    Outcome.NOT_INITIALIZED: (
        "Request {id} ran the first start of the lane's data, which failed, so no instance was started and nothing was "
        "switched. A data set it began in is never initialized again: retire the lane naming that data set to delete, and "
        "the next move makes a new one."
    ),
}


# The last request taken up for the lane, where it is a move that ended because its new instance failed its check or its
# first start failed. Such a move stopped nowhere part way: nothing was switched and what it started is gone. One that
# waits has changed nothing yet.
def failed_check(requests: Sequence[Kept]) -> Kept | None:
    last = max((each for each in requests if each.attempts), key=lambda each: each.id, default=None)
    failed = last is not None and last.stands is Stands.FAILED and (last.answer or {}).get("outcome") in LEFT_NOTHING
    return last if failed else None


# A failed check leaves a new lane with no instance to send visitors to. Whatever else is found, that move did not leave.
def left_by(failed: Kept | None, judged: Judgement) -> list[Finding]:
    left = [each for each in judged.findings if (each.kind, each.shown) == (Kind.NO_VISITORS, Shown.LEFT_UNFINISHED)]
    return left if failed else []


def left_unfinished(judged: Judgement) -> list[Finding]:
    return [each for each in judged.findings if each.shown is Shown.LEFT_UNFINISHED]


def lane_pills(judged: Judgement, failed: Kept | None) -> list[Pill]:
    in_maintenance = judged.lane.route.in_maintenance and judged.condition != Condition.IN_MAINTENANCE
    only_the_failed_move = judged.condition is Condition.UNFINISHED and left_unfinished(judged) == left_by(failed, judged)
    condition = MOVE_FAILED if only_the_failed_move else said(judged.condition)
    return [*([said(Condition.IN_MAINTENANCE)] if in_maintenance else []), condition]


def finding_rows(judged: Judgement, failed: Kept | None) -> list[Row]:
    explained = left_by(failed, judged)
    rows = [
        Row(name="Found", value=[Piece(text=each.sentence)], pills=[MOVE_FAILED if each in explained else said(each.shown)])
        for each in judged.findings
    ]
    if left_unfinished(judged) != explained:
        again = "Beginning maintenance again" if judged.lane.route.in_maintenance else "Asking for a build again"
        rows[-1].note = f"No call is at work on this lane. {again} finishes what was left unfinished."
    if failed and explained:
        note = LEFT_NOTHING[Outcome((failed.answer or {})["outcome"])]
        rows[judged.findings.index(explained[-1])].note = note.format(id=failed.id)
    return rows


def asked_for(request: Request) -> str:
    if request.work is Work.MOVE:
        return f"{request.work} to {request.build}"
    deleting = "" if request.delete_data_set is None else f", deleting the data set {request.delete_data_set}"
    return f"{request.work}{deleting}"


INTERRUPTED = "Launchplane stopped, or lost its record, while it was at work on it."


# How often a request was taken up, and why its last attempt that is over did not end it: what keeps one that never ends.
def taken_up(request: Kept) -> str:
    over = [attempt for attempt in request.attempts if attempt.ended is not None]
    if not over:
        return ""
    why = INTERRUPTED if over[-1].ended == "interrupted" else request.error
    return f"Taken up {len(request.attempts)} times, and again by itself until it ends. The last attempt did not end it: {why} "


NO_ANSWER = "may have been sent, no answer"


# A change of the route and a command for a part say how far they are known to have got: about to be sent, what the gate
# or Docker answered, or no answer, and then that a later attempt read it as the request's own. Any other line says what
# was done, or that it was refused.
def line_of(event: Event, attempt_is_open: bool) -> str:
    if (event.action or event.command) is None:
        return event.did if event.error is None else f"{event.did}, refused"
    reply = event.outcome
    if reply is None:
        return f"about to {event.did}" if attempt_is_open else f"{event.did}: {NO_ANSWER}"
    heard = "found already as asked" if reply.already else {None: NO_ANSWER, True: "done", False: "refused"}[reply.made]
    return f"{event.did}: {heard}{'' if reply.read_back_at is None else ', then read as made by this request'}"


def lines_of(request: Kept, *, only_sent: bool = False) -> list[str]:
    open_now = {attempt.number for attempt in request.attempts if attempt.ended is None}
    shown = [event for event in request.events if (event.action or event.command) is not None or not only_sent]
    return [line_of(event, event.attempt in open_now) for event in shown]


def done_so_far(request: Kept) -> str:
    lines = lines_of(request)
    return f"{taken_up(request)}So far: {'; '.join(lines)}." if lines else f"{taken_up(request)}Nothing done so far."


# A request that has ended keeps its lines on the page, so the last of them can be read after it has ended.
def with_lines(note: str, request: Request, *, only_sent: bool = False) -> str:
    lines = lines_of(request, only_sent=only_sent) if isinstance(request, Kept) else []
    return f"{note} Its lines: {'; '.join(lines)}." if lines else note


NOT_SWITCHED = "Visitors get the back soon page, and nothing is switched."
COPYING = f"A copy of the lane's data is being made. It has no network of its own yet, so nothing runs on it. {NOT_SWITCHED}"
COPIED = f"The lane's data is copied. The copy is not updated, and nothing of the new build is started on it. {NOT_SWITCHED}"
UPDATING = f"The lane's data is copied, and the new build is updating the copy. No web part of it is started. {NOT_SWITCHED}"
UPDATE_STOPPED = (
    "The container of the update is on the host and does not run. How the update ended is not recorded yet, and no web "
    f"part of the new build is started. {NOT_SWITCHED}"
)
UPDATED = f"The update of the copy ended well. No web part of the new build is started on it yet. {NOT_SWITCHED}"
CHECKING = f"The copy is updated, and the new build runs on it. It has not passed its check. {NOT_SWITCHED}"
CHECKED = f"The copy is updated, and the new build runs on it and passed its check. {NOT_SWITCHED}"
COMING_BACK = "Nothing is switched: Launchplane is bringing the original instance back on its own data."
ROUTE_WRITTEN = (
    "The route is written to the new build on the updated copy. The proxy is not yet seen to send visitors there, so "
    "they may still get the back soon page."
)
SWITCHED = "The proxy sends visitors to the new build on the updated copy."
EVERY_LINE = "Every line is in GET /requests/{id}."


# Where a release on a copy is before its switch: what it has on the host now, and what its lines say has ended.
def copy_phase(request: Kept, instances: Sequence[RunningInstance]) -> str:
    own = [each for each in instances if each.generation == request.generation]
    told = [line.did for line in request.events if line.error is None]
    if any(each.is_update for each in own):
        return UPDATING if any(each.is_update and each.state is InstanceState.RUNNING for each in own) else UPDATE_STOPPED
    if any(each.state is InstanceState.RUNNING for each in own):
        return CHECKED if any(each.startswith("check ") for each in told) else CHECKING
    if any(each.startswith("update the data of ") for each in told):
        return UPDATED
    return COPIED if any(each.startswith("make the network of the data set") for each in told) else COPYING


# A route that is written sends nobody anywhere until the proxy has loaded it, which the line after the switch says.
def switch_phase(lines: Sequence[Event]) -> str | None:
    switched = next((number for number, line in enumerate(lines) if line.did.startswith("switch ") and line.is_own), None)
    if switched is None:
        return None
    served = any(line.did.startswith("wait for the proxy to load the route") and line.error is None for line in lines[switched:])
    return SWITCHED if served else ROUTE_WRITTEN


# A move that put its lane behind the back soon page has too many lines for a card. Its note is where it is, told from
# what is on the host and from what its own lines say has happened, and the lines are in the request.
def frozen_note(request: Kept, instances: Sequence[RunningInstance]) -> str | None:
    lines = request.events
    froze = any(line.action is not None and line.action.to == BACK_SOON for line in lines)
    if request.work is not Work.MOVE or not froze:
        return None
    stopped = next((line for line in lines if line.error is not None or line.did.startswith("give the copy up")), None)
    if request.stands in ENDED:
        paragraph = in_one_line(request.error) if request.error else "Visitors are on the new build and the updated copy."
    elif (switching := switch_phase(lines)) is not None:
        paragraph = switching
    elif stopped is not None:
        paragraph = f"{in_one_line(': '.join(filter(None, (stopped.did, stopped.error)))).rstrip('.')}. {COMING_BACK}"
    else:
        paragraph = copy_phase(request, instances)
    again = "" if request.stands in ENDED else taken_up(request)
    return f"{again}{paragraph.rstrip('.')}. {EVERY_LINE.format(id=request.id)}"


# A cancel of a move is decided at the gate, so what is said of it is what the gate answered when the status was read.
CANCEL_AT_THE_GATE: dict[Verdict | None, str] = {
    None: "the gate could not be asked whether that came in time.",
    Verdict.NONE: "the gate has not been asked yet, and its worker carries the cancel there.",
    Verdict.ABANDONED: "the cancel won at the gate, and its worker puts the lane back.",
    Verdict.COMMITTED: "the gate says the routes file was already renamed, so the move is finished as asked.",
}


def being_cancelled(request: Kept, decided: Mapping[int, GateAnswer | None]) -> str:
    if request.cancel_refused is not None:
        return f" {request.cancel_refused}"
    if request.cancel_asked_by is None:
        return ""
    if request.id not in decided:
        return f" To be cancelled by {request.cancel_asked_by}: its worker puts the lane back, unless it is too late."
    answer = decided[request.id]
    return f" To be cancelled by {request.cancel_asked_by}: {CANCEL_AT_THE_GATE[None if answer is None else answer.verdict]}"


def request_note(
    request: Request, decided: Mapping[int, GateAnswer | None], instances: Sequence[RunningInstance] = ()
) -> str | None:
    if isinstance(request, Kept) and request.cancel_asked_at is None and (frozen := frozen_note(request, instances)) is not None:
        return frozen
    if request.stands is Stands.CANCELLED:
        # What a cancel put back is in its own note, so only the lines of what it sent are added.
        return with_lines(how_cancelled(request), request, only_sent=True)
    if request.stands is Stands.REPLACED:
        return how_replaced(request)
    if isinstance(request, Kept) and (won := how_cancel_won(request)) is not None:
        return with_lines(won, request)
    if request.stands in ENDED and request.cancel_asked_at is None:
        return with_lines(in_one_line(request.error or f"It ended as {request.stands}."), request)
    if request.stands in ENDED:
        why = request.cancel_refused or f"A cancel by {request.cancel_asked_by} was asked and not carried out."
        return with_lines(f"{why} It ended as {request.stands}.", request)
    if isinstance(request, Kept) and request.stands is Stands.WORKING:
        return f"{done_so_far(request)}{being_cancelled(request, decided)}"
    return None


def request_rows(
    requests: Sequence[Request], decided: Mapping[int, GateAnswer | None], instances: Sequence[RunningInstance] = ()
) -> list[Row]:
    return [
        Row(
            name=f"Request {request.id}",
            value=[Piece(text=asked_for(request))],
            pills=[Pill(text=request.stands, tone=STANDS_TONE.get(request.stands, Standing.WRONG))],
            note=request_note(request, decided, instances),
        )
        for request in sorted(requests, key=lambda each: each.id)
    ]


def without_a_server(found: DataSet) -> str:
    if found.copy_of is None:
        return " has no database server"
    whole = "a whole copy" if found.is_whole_copy else "a copy, not whole,"
    return f" is {whole} of {found.copy_of}, kept offline: no instance is started on it"


GOES = "An old copy: the next release that switches deletes it."
BEING_MADE = "A request at work on the lane is making it."


# Said only of a lane with an instance receiving visitors. A copy is old only where no request at work made it.
def kept_notes(judged: Judgement | None, requests: Sequence[Kept]) -> dict[str, str]:
    if judged is None or judged.lane.active is None:
        return {}
    at_work = {each.generation for each in requests if each.stands is Stands.WORKING}
    return {
        found.data_set: why_kept(judged.lane, found) or (BEING_MADE if found.generation in at_work else GOES)
        for found in judged.lane.data_sets
    }


def part_row(name: str, made_from: str, build: str | None, container: str, pills: list[Pill]) -> Row:
    built = code("not marked") if build is None else Piece(text=build, drawn_as="build")
    return Row(name=name, value=[Piece(text=f"{made_from} "), built], pills=pills, note=container)


def lane_card(entry: WebAddressJudged, status: Status) -> Card:
    def of_lane[Part: PartOfLane](parts: list[Part]) -> list[Part]:
        return [part for part in parts if entry.product is not None and (part.product, part.lane) == (entry.product, entry.lane)]

    def state_pill(state: InstanceState, container: str) -> Pill:
        return Pill(text=state, tone=Standing.WRONG if judged is None else judged.parts.get(container, Standing.WRONG))

    on_host, active = status.reading.parts, entry.sends_to_container
    judged = next((each for each in status.lanes if (each.lane.product, each.lane.lane) == (entry.product, entry.lane)), None)
    instances = sorted(of_lane(on_host.instances), key=lambda each: (each.container != active, each.container))
    data_sets = of_lane(on_host.data_sets)
    receiving = [Pill(text="active", tone=Standing.GOOD)]
    requests = [each for each in shown_requests(status) if (each.product, each.lane) == (entry.product, entry.lane)]
    failed = failed_check(requests)
    kept = kept_notes(judged, requests)
    return Card(
        title=f"{entry.product} · {entry.lane or 'no lane'}" if entry.product else "A web address with no instance on the host",
        pills=[] if judged is None else lane_pills(judged, failed),
        in_maintenance_since=None if judged is None else judged.lane.route.back_soon_since,
        rows=[
            Row(
                name="Web address",
                value=[code(entry.web_address)],
                pills=[Pill(text=entry.says, tone=entry.standing)],
                note=entry.error,
            ),
            *request_rows(requests, status.cancels_decided, instances),
            *([] if judged is None else finding_rows(judged, failed)),
            *[
                part_row("Background service", "build", each.build, each.container, [state_pill(each.state, each.container)])
                for each in of_lane(on_host.background_services)
            ],
            *[
                part_row(
                    each.called.capitalize() if each.runs_once else f"Instance {number}",
                    "build",
                    each.build,
                    each.container,
                    [state_pill(each.state, each.container), *(receiving if each.container == active else [])],
                )
                for number, each in enumerate(instances, start=1)
            ],
            *[
                Row(
                    name="Data set", value=[code(found.data_set), Piece(text=without_a_server(found))], note=kept.get(found.data_set)
                )
                for found in data_sets
                if not found.databases
            ],
            *[
                part_row(
                    "Data set",
                    "image",
                    each.image,
                    " ".join(filter(None, (each.container, kept.get(found.data_set)))),
                    [state_pill(each.state, each.container)],
                )
                for found in data_sets
                for each in found.databases
            ],
        ],
    )


def no_lane_card(status: Status) -> list[Card]:
    on_host = status.reading.parts
    unmarked = Pill(text=UNMARKED, tone=Standing.WRONG)
    containers = [
        *[("Web part", each) for each in on_host.instances if each.lane is None],
        *[("Background service", each) for each in on_host.background_services if each.lane is None],
    ]
    rows = [
        *[
            Row(
                name=name,
                value=[code(each.container), Piece(text=f" of {each.product}")],
                pills=[Pill(text=each.state, tone=Standing.WRONG), unmarked],
            )
            for name, each in containers
        ],
        *[
            Row(name="Data set", value=[code(found.data_set), Piece(text=f" of {found.product}")], pills=[unmarked])
            for found in on_host.data_sets
            if found.lane is None
        ],
    ]
    return [Card(title=NO_LANE, rows=rows)] if rows else []


# Every request that has not ended, then those that ended a short while ago and are still shown.
def shown_requests(status: Status) -> list[Kept]:
    return [*status.requests, *status.cancels]


# A request for a lane with nothing on the host yet has no card of the host's to show on.
def request_cards(status: Status) -> list[Card]:
    on_host = {(entry.product, entry.lane) for entry in status.web_addresses}
    requests = shown_requests(status)
    lanes = sorted({(each.product, each.lane) for each in requests} - on_host)
    of = {lane: [each for each in requests if (each.product, each.lane) == lane] for lane in lanes}
    return [
        Card(title=f"{product} · {lane}", rows=request_rows(of[product, lane], status.cancels_decided)) for product, lane in lanes
    ]


def record_card(status: Status) -> list[Card]:
    if status.record_error is None:
        return []
    unread = Pill(text=Shown.UNREAD, tone=Standing.UNREAD)
    return [
        Card(
            title=RECORD_UNREAD,
            pills=[unread],
            rows=[Row(name="Record", value=[Piece(text=status.record_error)], note=NOT_KNOWN_AT_WORK)],
        )
    ]


def cards(status: Status) -> list[Card]:
    drawn = [
        *record_card(status),
        *[lane_card(entry, status) for entry in status.web_addresses],
        *request_cards(status),
        *no_lane_card(status),
    ]
    return drawn or [Card(title=NOTHING_ON_THE_HOST)]
