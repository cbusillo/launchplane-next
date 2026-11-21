from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from launchplane.act import Status
from launchplane.gate import Answer as GateAnswer
from launchplane.instance import InstanceState, PartOfLane, Verdict
from launchplane.judge import Condition, Judgement, Shown, Standing, WebAddressJudged, Work
from launchplane.record import ENDED, Event, Kept, Request, Stands, how_cancelled, how_not_cancelled, how_replaced
from launchplane.telling import Said

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


def said(text: Condition | Shown) -> Pill:
    return Pill(text=text, tone=TONE[text])


def code(text: str) -> Piece:
    return Piece(text=text, drawn_as="code")


def lane_pills(judged: Judgement) -> list[Pill]:
    in_maintenance = judged.lane.route.in_maintenance and judged.condition != Condition.IN_MAINTENANCE
    return [*([said(Condition.IN_MAINTENANCE)] if in_maintenance else []), said(judged.condition)]


def finding_rows(judged: Judgement) -> list[Row]:
    rows = [Row(name="Found", value=[Piece(text=finding.sentence)], pills=[said(finding.shown)]) for finding in judged.findings]
    if judged.shown_as(Shown.LEFT_UNFINISHED):
        again = "Beginning maintenance again" if judged.lane.route.in_maintenance else "Asking for a build again"
        rows[-1].note = f"No call is at work on this lane. {again} finishes what was left unfinished."
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


HEARD = {Said.CHANGED: "done", Said.ALREADY: "found already as asked", Said.REFUSED: "refused"}
NO_ANSWER = "may have been sent, no answer"


# A change says how far it is known to have got: about to be sent, no answer, or what the host answered, and then what
# a later attempt read of the host. With no answer, nothing says it was sent. Any other line says what was looked at,
# and whether that was refused.
def line_of(event: Event, attempt_is_open: bool) -> str:
    if event.action is None:
        return event.did if event.error is None else f"{event.did}, refused"
    did = f"{event.did}, to put the lane back" if event.action.back else event.did
    reply = event.outcome
    if reply is None:
        return f"about to {did}" if attempt_is_open and event.error is None else f"{did}: {NO_ANSWER}"
    heard = NO_ANSWER if reply.said is None else HEARD[reply.said]
    return f"{did}: {heard}" if reply.seen is None else f"{did}: {heard}, then read as {reply.seen}"


def lines_of(request: Kept) -> list[str]:
    open_now = {attempt.number for attempt in request.attempts if attempt.ended is None}
    return [line_of(event, event.attempt in open_now) for event in request.events]


def done_so_far(request: Kept) -> str:
    lines = lines_of(request)
    return f"{taken_up(request)}So far: {'; '.join(lines)}." if lines else f"{taken_up(request)}Nothing done so far."


# A request that has just ended keeps its lines on the page, so the last of them can be read after it has ended.
def how_it_ended(request: Kept) -> str:
    lines = "; ".join(lines_of(request)) or "none"
    return f"{request.error or f'It ended as {request.stands}.'} Its lines: {lines}."


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


def request_note(request: Request, decided: Mapping[int, GateAnswer | None]) -> str | None:
    if request.stands is Stands.CANCELLED:
        return how_cancelled(request)
    if request.stands is Stands.REPLACED:
        return how_replaced(request)
    if isinstance(request, Kept) and request.stands in ENDED and request.cancel_asked_at is None:
        return how_it_ended(request)
    if request.stands in ENDED:
        why = how_not_cancelled(request) or f"A cancel by {request.cancel_asked_by} was asked and not carried out."
        return f"{why} It ended as {request.stands}."
    if isinstance(request, Kept) and request.stands is Stands.WORKING:
        return f"{done_so_far(request)}{being_cancelled(request, decided)}"
    return None


def request_rows(requests: Sequence[Request], decided: Mapping[int, GateAnswer | None]) -> list[Row]:
    return [
        Row(
            name=f"Request {request.id}",
            value=[Piece(text=asked_for(request))],
            pills=[Pill(text=request.stands, tone=STANDS_TONE.get(request.stands, Standing.WRONG))],
            note=request_note(request, decided),
        )
        for request in sorted(requests, key=lambda each: each.id)
    ]


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
    return Card(
        title=f"{entry.product} · {entry.lane or 'no lane'}" if entry.product else "A web address with no instance on the host",
        pills=[] if judged is None else lane_pills(judged),
        in_maintenance_since=None if judged is None else judged.lane.route.back_soon_since,
        rows=[
            Row(
                name="Web address",
                value=[code(entry.web_address)],
                pills=[Pill(text=entry.says, tone=entry.standing)],
                note=entry.error,
            ),
            *request_rows(requests, status.cancels_decided),
            *([] if judged is None else finding_rows(judged)),
            *[
                part_row("Background service", "build", each.build, each.container, [state_pill(each.state, each.container)])
                for each in of_lane(on_host.background_services)
            ],
            *[
                part_row(
                    f"Instance {number}",
                    "build",
                    each.build,
                    each.container,
                    [state_pill(each.state, each.container), *(receiving if each.container == active else [])],
                )
                for number, each in enumerate(instances, start=1)
            ],
            *[
                Row(name="Data set", value=[code(found.data_set), Piece(text=" has no database server")])
                for found in data_sets
                if not found.databases
            ],
            *[
                part_row("Data set", "image", each.image, each.container, [state_pill(each.state, each.container)])
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
def shown_requests(status: Status) -> list[Request]:
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
