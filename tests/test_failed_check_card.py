from typing import Any

from launchplane.act import Lanes
from launchplane.judge import Work
from launchplane.page import Card, cards, in_one_line
from launchplane.plan import failure_report
from launchplane.record import Asked, Record, Stands
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS
from tests.odoo_product import TK
from tests.pretend_host import NEW, OLD, Given, PretendHost, PretendProxy, numbered_names
from tests.test_worker import CALLER, kept_of

MOVE = Asked(product="site", lane="testing", work=Work.MOVE, build="site:b")
NOT_ANSWERING = (
    "http://site-testing-1:80/web/login did not answer the proxy with success within 20 s: HTTP/1.1 500 INTERNAL SERVER ERROR"
)
ODOO_LOG = """\
    raise ImportError(f"Database {cr.dbname} not initialized, you can force it with `-i base`")
ImportError: Database app not initialized, you can force it with `-i base`
2026-11-24 14:36:17,597 1 INFO app odoo.http.server: 172.16.1.8 - "GET /web/login HTTP/1.1" 500 265 11 0.002 0.002 -
"""
QUIET_LOG = (
    '2026-11-24 15:25:49,816 1 INFO app odoo.http.server: 172.16.1.6 - "GET /web/login HTTP/1.1" 200 24224 17 0.005 0.017 rw\n'
)


def test_a_failed_check_is_drawn_as_what_failed_and_the_last_error_line_of_the_log_and_never_the_log() -> None:
    drawn = in_one_line(failure_report(NOT_ANSWERING, ODOO_LOG))
    said = "ImportError: Database app not initialized, you can force it with `-i base`"
    assert drawn == f"{NOT_ANSWERING} The last error in its log: {said}"


def test_a_log_that_speaks_of_no_error_adds_nothing_to_what_failed() -> None:
    assert in_one_line(failure_report("The check of its kind failed: the attachment has none.", QUIET_LOG)) == (
        "The check of its kind failed: the attachment has none."
    )
    assert in_one_line(failure_report("Connection refused", "")) == "Connection refused It wrote no log."


# A new lane of an Odoo product whose first instance fails its check, and the request ended.
def after_a_failed_first_move(record: Record) -> tuple[Lanes, PretendProxy, int]:
    proxy = PretendProxy(PretendHost())
    proxy.failing = {f"site-testing-{number}" for number in range(100)}
    lanes = Lanes(proxy.host, proxy, lambda _product: TK, numbered_names(), record.unfinished, record.ended_lately)
    request = record.write(MOVE, CALLER, None)
    assert Worker(record, lanes, NO_SECRETS).work_next() is True
    return lanes, proxy, request.id


def card_after_a_failed_first_move(record: Record) -> tuple[Card, dict[str, Any], int]:
    lanes, _, request = after_a_failed_first_move(record)
    kept = kept_of(record, request)
    assert (kept.stands, kept.answer is not None) == (Stands.FAILED, True)
    [card] = cards(lanes.read_status())
    return card, kept.answer or {}, request


def test_the_card_of_a_lane_whose_new_instance_failed_its_check_reads_as_failed_with_what_would_change_the_answer(
    record: Record,
) -> None:
    card, _, request = card_after_a_failed_first_move(record)
    found = [row for row in card.rows if row.name == "Found"]
    assert ([pill.text for pill in card.pills], {pill.text for row in found for pill in row.pills}) == (
        ["move failed"],
        {"move failed"},
    )
    assert found[-1].note == (
        f"Request {request} started an instance that failed its check, so nothing was switched and that instance is gone. "
        "Asking for that build again fails the same way until what the check found is put right."
    )


def test_the_note_of_a_request_that_failed_its_check_is_one_line_and_its_answer_keeps_the_log(record: Record) -> None:
    card, answer, request = card_after_a_failed_first_move(record)
    [row] = [row for row in card.rows if row.name == f"Request {request}"]
    assert row.note is not None
    assert ("\n" in row.note, row.note.startswith("Connection refused Its lines: ")) == (False, True)
    assert "Connection refused\nIts last log lines:\nsite:b listening on :5678" in answer["error"]


def test_each_part_of_a_data_set_is_named_on_the_card_and_in_the_answer(record: Record) -> None:
    card, answer, request = card_after_a_failed_first_move(record)
    [row] = [row for row in card.rows if row.name == f"Request {request}"]
    [data_set] = {line.rsplit(" ", 1)[1] for line in (row.note or "").split("; ") if " of the data set " in line}
    parts = ("network", "database volume", "files volume", "database server")
    assert [f"make the {part} of the data set {data_set}" in (row.note or "") for part in parts] == [True] * 4
    assert answer["did"][:4] == [f"made the {part} of the data set {data_set}" for part in parts]


def found_on(card: Card) -> list[tuple[str, str | None]]:
    return [(row.pills[0].text, row.note) for row in card.rows if row.name == "Found"]


def test_a_request_that_waits_behind_a_failed_check_leaves_the_card_saying_the_check_failed(record: Record) -> None:
    lanes, _, _ = after_a_failed_first_move(record)
    [failed] = cards(lanes.read_status())
    waiting = record.write(MOVE, CALLER, None)
    [card] = cards(lanes.read_status())
    assert kept_of(record, waiting.id).stands is Stands.WAITING
    assert (card.pills, found_on(card)) == (failed.pills, found_on(failed))
    assert [pill.text for pill in card.pills] == ["move failed"]


def test_a_proxy_that_stopped_after_a_failed_check_is_not_drawn_as_that_failed_move(record: Record) -> None:
    lanes, proxy, _ = after_a_failed_first_move(record)
    proxy.running = False
    [card] = cards(lanes.read_status())
    assert ([pill.text for pill in card.pills], [pill for pill, _ in found_on(card)]) == (["unfinished"], ["left unfinished"])
    assert found_on(card)[-1][1] == "No call is at work on this lane. Asking for a build again finishes what was left unfinished."


def test_a_lane_no_failed_move_explains_still_reads_as_left_unfinished() -> None:
    proxy = Given((OLD, NEW), ("old", "old")).proxy()
    [card] = cards(Lanes(proxy.host, proxy).read_status())
    found = [row for row in card.rows if row.name == "Found"]
    assert ([pill.text for pill in card.pills], [pill.text for pill in found[-1].pills]) == (["unfinished"], ["left unfinished"])
    assert found[-1].note == "No call is at work on this lane. Asking for a build again finishes what was left unfinished."
