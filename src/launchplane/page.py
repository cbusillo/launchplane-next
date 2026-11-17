from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from launchplane.act import Status
from launchplane.instance import InstanceState, PartOfLane
from launchplane.judge import Condition, Judgement, Shown, Standing, WebAddressJudged

TONE = {
    Condition.GOOD: Standing.GOOD,
    Condition.IN_MAINTENANCE: Standing.ON_PURPOSE,
    Condition.UNDER_WAY: Standing.ON_PURPOSE,
    Shown.UNDER_WAY: Standing.ON_PURPOSE,
    Condition.UNFINISHED: Standing.WRONG,
    Shown.LEFT_UNFINISHED: Standing.WRONG,
    Shown.WRONG: Standing.WRONG,
    Shown.UNREAD: Standing.UNREAD,
}
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


def cards(status: Status) -> list[Card]:
    drawn = [*[lane_card(entry, status) for entry in status.web_addresses], *no_lane_card(status)]
    return drawn or [Card(title=NOTHING_ON_THE_HOST)]
