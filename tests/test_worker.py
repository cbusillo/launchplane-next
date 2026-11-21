import threading
import time
from collections.abc import Callable, Mapping
from functools import partial
from typing import NoReturn

import pytest

from launchplane.act import Lanes
from launchplane.docker_answers import ContainerHostError, ContainerHostRefused
from launchplane.instance import DataSetUse, LaneBuild, OnHost
from launchplane.judge import Condition, Work
from launchplane.page import INTERRUPTED, RECORD_UNREAD, cards
from launchplane.record import ENDED, Asked, Interrupted, Kept, PostgresRecord, Record, RecordError, Stands
from launchplane.secret_boundary import SECRET_REMOVED
from launchplane.telling import Witness
from launchplane.worker import MOST_LANES_AT_ONCE, Worker
from tests.given_secrets import NO_SECRETS, GivenSecrets
from tests.pretend_host import WEB_ADDRESS, LaunchplaneKilled, PretendHost, PretendProxy, numbered_names
from tests.pretend_record import PretendRecord, begun, lose_every_turn

BUILD_A = LaneBuild(product="site", lane="testing", build="site:a")
MOVE_B = Asked(product="site", lane="testing", work=Work.MOVE, build="site:b")
TO_B = LaneBuild(product="site", lane="testing", build="site:b")
CALLER = "the address 192.0.2.1"
SECRET = "Zx3-secret_given-to-a-lane-0123456789abcdef"  # noqa: S105 a pretend one


def serving_a(record: Record) -> tuple[Worker, Lanes, PretendProxy]:
    proxy = PretendProxy(PretendHost())
    proxy.host.place(BUILD_A, "a")
    proxy.route(WEB_ADDRESS, "a")
    lanes = Lanes(proxy.host, proxy, name_fresh=numbered_names(), read_unfinished=record.unfinished)
    return Worker(record, lanes, NO_SECRETS), lanes, proxy


def wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "waited 5 s"
        time.sleep(0.01)


def kept_of(record: Record, request: int) -> Kept:
    kept = record.read(request)
    assert kept is not None
    return kept


def delete_every_event(record: Record, request: int) -> None:
    if isinstance(record, PretendRecord):
        record.change(request, events=[])
    if isinstance(record, PostgresRecord):
        with record.session("deleting every event") as session:
            # noinspection SqlResolve,SqlWithoutWhere
            session.execute("delete from event")


def other_lane(proxy: PretendProxy, lane: str) -> Asked:
    proxy.host.place(LaneBuild(product="site", lane=lane, build="site:a"), f"{lane}-a")
    proxy.route(f"{lane}.site.test", f"{lane}-a")
    return Asked(product="site", lane=lane, work=Work.MOVE, build="site:b")


def stopped(worker: Worker, threads: list[threading.Thread]) -> None:
    worker.stop()
    for thread in threads:
        thread.join(timeout=5)


def killed_while_checking(record: Record, lanes: Lanes, proxy: PretendProxy) -> int:
    def kill(_container: str) -> None:
        raise LaunchplaneKilled

    request = record.write(MOVE_B, CALLER, None)
    attempt = begun(record, request.id)
    proxy.while_checking = kill
    with pytest.raises(LaunchplaneKilled):
        lanes.move(TO_B, request.generation, attempt, Witness(tell=partial(record.note, attempt)))
    proxy.while_checking = lambda _container: None
    proxy.host.actions.clear()
    return request.id


def test_a_request_is_carried_through_and_its_answer_and_each_thing_done_are_kept(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    request = record.write(MOVE_B, CALLER, None)
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    new = proxy.written[WEB_ADDRESS]
    assert kept.answer is not None
    assert (kept.stands, kept.answer["outcome"], kept.answer["container_receiving_visitors"], kept.error) == (
        Stands.DONE,
        "switched",
        new,
        None,
    )
    assert [(each.number, each.ended) for each in kept.attempts] == [(1, "done")]
    assert [each.did for each in kept.events] == [
        f"start the instance {new} of site:b",
        f"check {new} at /",
        f"switch {WEB_ADDRESS} to {new}",
        f"wait for the proxy to load the route of {WEB_ADDRESS}",
        "retire a",
    ]
    assert worker.work_next() is False


@pytest.mark.parametrize("events_deleted", [False, True], ids=["with its events", "with every event deleted"])
def test_a_request_left_working_is_taken_up_and_finished_from_what_is_on_the_host_whatever_the_events_say(
    record: Record, events_deleted: bool
) -> None:
    worker, lanes, proxy = serving_a(record)
    request = killed_while_checking(record, lanes, proxy)
    left = next(container for container in proxy.host.instances if container != "a")
    if events_deleted:
        delete_every_event(record, request)
    assert worker.work_next() is True
    kept = kept_of(record, request)
    [fresh] = proxy.host.instances
    assert proxy.host.actions == [f"retire {left}", f"start {fresh}", f"check {fresh}/", f"route {fresh}", "retire a"]
    assert (kept.stands, [(each.number, each.ended) for each in kept.attempts]) == (Stands.DONE, [(1, "interrupted"), (2, "done")])
    assert kept.answer is not None and kept.answer["outcome"] == "switched"


def note_on_the_page(lanes: Lanes, request: int) -> str:
    [card] = cards(lanes.read_status())
    [row] = [row for row in card.rows if row.name == f"Request {request}"]
    return row.note or ""


def test_a_move_interrupted_five_times_is_taken_up_again_until_it_ends_and_the_page_says_how_often_and_why_the_last_attempt_ended(
    record: Record,
) -> None:
    worker, lanes, proxy = serving_a(record)
    request = record.write(MOVE_B, CALLER, None)
    for _ in range(5):
        assert record.take_up(request.id) is not None
    after_interruptions = note_on_the_page(lanes, request.id)
    proxy.gate.unreadable = "The routes folder could not be read"
    assert worker.work_next() is True
    kept_from_ending = note_on_the_page(lanes, request.id)
    proxy.gate.unreadable = None
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert (kept.stands, (kept.answer or {})["outcome"], [each.ended for each in kept.attempts]) == (
        Stands.DONE,
        "switched",
        [*["interrupted"] * 5, "unfinished", "done"],
    )
    assert ("Taken up 5 times" in after_interruptions, INTERRUPTED in after_interruptions) == (True, True)
    assert ("Taken up 6 times" in kept_from_ending, "The routes folder could not be read" in kept_from_ending) == (True, True)


def test_a_step_the_host_refuses_before_the_switch_fails_the_request_with_the_real_error_and_it_is_not_run_again(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)

    def refuse(target: LaneBuild, container: str, use: DataSetUse | None, generation: str | None, login: Mapping[str, str]) -> None:
        assert use is None and generation is not None and not login
        raise ContainerHostRefused(f"Starting the instance {container} from build {target.build} on test failed: no such image")

    proxy.host.start_instance = refuse
    request = record.write(MOVE_B, CALLER, None)
    assert (worker.work_next(), worker.work_next()) == (True, False)
    kept = kept_of(record, request.id)
    assert kept.error is not None
    assert (kept.stands, kept.answer, "is safe to ask again: Starting the instance" in kept.error) == (Stands.FAILED, None, True)


def test_a_step_the_host_refuses_after_the_switch_leaves_the_request_working_with_why_until_it_can_be_finished(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    retire = proxy.host.retire_instance

    def refuse(container: str) -> NoReturn:
        raise ContainerHostError(f"Retiring {container} on test failed: the daemon is busy")

    proxy.host.retire_instance = refuse
    request = record.write(MOVE_B, CALLER, None)
    assert (worker.work_next(), worker.work_next()) == (True, True)
    left_open = kept_of(record, request.id)
    assert left_open.error is not None and "Retiring a on test failed: the daemon is busy" in left_open.error
    assert (left_open.stands, {each.ended for each in left_open.attempts}, proxy.written) == (
        Stands.WORKING,
        {"unfinished"},
        {WEB_ADDRESS: "site-testing-0"},
    )
    proxy.host.retire_instance = retire
    assert (worker.work_next(), worker.work_next()) == (True, False)
    finished = kept_of(record, request.id)
    assert finished.answer is not None
    assert (finished.stands, finished.answer["outcome"], list(proxy.host.instances)) == (Stands.DONE, "switched", ["site-testing-0"])


def test_a_check_that_fails_ends_the_request_as_failed_and_keeps_the_answer(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    proxy.failing = {"site-testing-0"}
    request = record.write(MOVE_B, CALLER, None)
    worker.work_next()
    kept = kept_of(record, request.id)
    assert kept.answer is not None and kept.error is not None
    assert (kept.stands, kept.answer["outcome"], "Connection refused" in kept.error) == (Stands.FAILED, "check failed", True)
    assert [(each.did, each.error is None) for each in kept.events][1] == ("check site-testing-0 at /", False)


def test_a_failure_launchplane_does_not_know_fails_its_request_and_the_next_request_is_still_worked(record: Record) -> None:
    worker, _, proxy = serving_a(record)

    def break_once(_container: str) -> None:
        proxy.while_checking = lambda _container: None
        raise KeyError("a mistake in Launchplane")

    proxy.while_checking = break_once
    broken = record.write(MOVE_B, CALLER, None)
    assert worker.work_next() is True
    after = record.write(Asked(product="site", lane="testing", work=Work.MOVE, build="site:a"), CALLER, None)
    assert worker.work_next() is True
    error = kept_of(record, broken.id).error
    assert error is not None and "a mistake in Launchplane" in error
    assert (kept_of(record, broken.id).stands, kept_of(record, after.id).stands) == (Stands.FAILED, Stands.DONE)


def test_a_secret_in_an_error_or_an_answer_is_removed_before_it_is_written_to_the_record(
    record: Record, secrets: GivenSecrets
) -> None:
    worker, _, proxy = serving_a(record)
    secrets.seen = frozenset({SECRET})
    proxy.failing = {"site-testing-0"}
    proxy.host.read_log_tail = lambda container: f"{container} PGPASSWORD={SECRET}"
    request = record.write(MOVE_B, CALLER, None)
    worker.work_next()
    written = kept_of(record, request.id).model_dump_json()
    assert (SECRET in written, written.count(SECRET_REMOVED)) == (False, 3)


def test_a_lane_is_at_work_only_while_its_request_is_worked_and_no_other_lane_is(record: Record) -> None:
    worker, lanes, proxy = serving_a(record)
    proxy.host.place(LaneBuild(product="site", lane="other", build="site:a"), "other-1")
    during: list[dict[str, Work | None]] = []

    def at_work_by_lane() -> dict[str, Work | None]:
        return {judged.lane.lane: judged.work for judged in lanes.read_status().lanes}

    proxy.while_checking = lambda _container: during.append(at_work_by_lane())
    record.write(MOVE_B, CALLER, None)
    before = at_work_by_lane()
    worker.work_next()
    assert (before, during, at_work_by_lane()) == (
        {"other": None, "testing": None},
        [{"other": None, "testing": Work.MOVE}],
        {"other": None, "testing": None},
    )


def test_the_request_being_worked_shows_on_its_lanes_card_with_what_it_has_done_so_far(record: Record) -> None:
    worker, lanes, proxy = serving_a(record)
    shown: list[tuple[str, list[str], str | None]] = []

    def read_the_card(_container: str) -> None:
        [card] = cards(lanes.read_status())
        shown.extend((row.value[0].text, [pill.text for pill in row.pills], row.note) for row in card.rows if "Request" in row.name)

    proxy.while_checking = read_the_card
    record.write(MOVE_B, CALLER, None)
    worker.work_next()
    assert shown == [("moving to site:b", ["working"], "So far: start the instance site-testing-0 of site:b: done.")]


def test_a_request_for_a_lane_with_nothing_on_the_host_still_shows_on_the_page(record: Record) -> None:
    _, lanes, _ = serving_a(record)
    record.write(Asked(product="shop", lane="testing", work=Work.MOVE, build="shop:a"), CALLER, None)
    titles = {card.title: [row.value[0].text for row in card.rows if "Request" in row.name] for card in cards(lanes.read_status())}
    assert titles == {"site · testing": [], "shop · testing": ["moving to shop:a"]}


def test_with_the_record_unreachable_the_host_is_still_shown_and_the_page_says_the_record_cannot_be_read() -> None:
    record = PretendRecord(reachable=False)
    worker, lanes, proxy = serving_a(record)
    status = lanes.read_status()
    assert status.record_error is not None and "could not be reached" in status.record_error
    assert ([card.title for card in cards(status)], status.lanes[0].condition) == (
        [RECORD_UNREAD, "site · testing"],
        Condition.UNREAD,
    )
    assert (worker.worked_one(), proxy.host.actions) == (False, [])


def test_losing_the_record_for_one_write_stops_the_work_and_it_is_taken_up_again_as_interrupted_not_failed() -> None:
    record = PretendRecord()
    worker, _, proxy = serving_a(record)
    note = record.note

    def lose_the_record(_container: str) -> None:
        def unreachable_once(attempt: int, did: str, error: str | None) -> None:
            record.note = note
            raise RecordError(f"Launchplane's record could not be reached while writing what was done: {attempt} {did} {error}")

        record.note = unreachable_once

    proxy.while_checking = lose_the_record
    request = record.write(MOVE_B, CALLER, None)
    with pytest.raises(RecordError):
        worker.work_next()
    done_before_the_loss = list(proxy.host.actions)
    proxy.while_checking = lambda _container: None
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert done_before_the_loss == ["start site-testing-0", "check site-testing-0/"]
    assert (kept.stands, [each.ended for each in kept.attempts]) == (Stands.DONE, ["interrupted", "done"])


def test_the_request_at_work_finishes_as_asked_then_the_newest_waiting_runs_and_one_that_waited_between_never_starts(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    at_work = record.write(MOVE_B, CALLER, None)
    arrived: list[int] = []

    def ask_for_c_and_then_d_while_b_is_checked(_container: str) -> None:
        if not arrived:
            arrived.extend(
                record.write(MOVE_B.model_copy(update={"build": build}), CALLER, None).id for build in ("site:c", "site:d")
            )

    proxy.while_checking = ask_for_c_and_then_d_while_b_is_checked
    assert (worker.work_next(), worker.work_next(), worker.work_next()) == (True, True, False)
    first, between, newest = (kept_of(record, each) for each in (at_work.id, *arrived))
    assert (first.stands, first.answer and first.answer["outcome"], first.cancel_asked_by) == (Stands.DONE, "switched", None)
    assert (between.stands, between.replaced_by, between.attempts, between.events) == (Stands.REPLACED, newest.id, [], [])
    assert (newest.stands, [each.build for each in proxy.host.instances.values()]) == (Stands.DONE, ["site:d"])
    started = [action.removeprefix("start ") for action in proxy.host.actions if action.startswith("start ")]
    assert [action for action in proxy.host.actions if action.startswith("route ")] == [f"route {each}" for each in started]
    assert (len(started), proxy.written) == (2, {WEB_ADDRESS: started[1]})


def test_a_worker_whose_turn_was_lost_and_taken_by_another_does_nothing_more_on_the_host(record: Record) -> None:
    worker, lanes, proxy = serving_a(record)
    other = Worker(record, lanes, NO_SECRETS)
    read = proxy.host.read
    moved = record.write(MOVE_B, CALLER, None)
    done_by_the_other: list[str] = []
    lost: list[bool] = []

    def read_then_lose_the_turn_before_the_route_is_written(lane: tuple[str, str] | None = None) -> OnHost:
        on_host = read(lane)
        if proxy.host.actions[-1:] == ["check site-testing-0/"] and not lost:
            lost.append(True)
            lose_every_turn(record)
            assert other.work_next() is True
            done_by_the_other.extend(proxy.host.actions)
        return on_host

    proxy.host.read = read_then_lose_the_turn_before_the_route_is_written
    with pytest.raises(Interrupted):
        worker.work_next()
    kept = kept_of(record, moved.id)
    assert [action.split()[0] for action in done_by_the_other[-3:]] == ["check", "route", "retire"]
    assert proxy.host.actions == done_by_the_other
    assert (kept.stands, [each.ended for each in kept.attempts]) == (Stands.DONE, ["interrupted", "done"])


def test_requests_for_different_lanes_are_worked_at_the_same_time(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    both_checking = threading.Barrier(2, timeout=3)

    def wait_for_the_other_lane(_container: str) -> None:
        both_checking.wait()

    proxy.while_checking = wait_for_the_other_lane
    asked = [record.write(MOVE_B, CALLER, None), record.write(other_lane(proxy, "other"), CALLER, None)]
    threads = worker.start()
    wait_until(lambda: all(kept_of(record, each.id).stands in ENDED for each in asked))
    stopped(worker, threads)
    assert [kept_of(record, each.id).stands for each in asked] == [Stands.DONE, Stands.DONE]


def test_no_more_lanes_are_worked_at_once_than_the_fixed_number(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    checking: list[str] = []
    go_on = threading.Event()

    def hold_the_check(container: str) -> None:
        checking.append(container)
        assert go_on.wait(timeout=5)

    proxy.while_checking = hold_the_check
    asked = [record.write(other_lane(proxy, f"lane-{number}"), CALLER, None) for number in range(MOST_LANES_AT_ONCE + 2)]
    threads = worker.start()
    wait_until(lambda: len(checking) == MOST_LANES_AT_ONCE)
    time.sleep(0.3)
    held_at_once = len(checking)
    go_on.set()
    wait_until(lambda: all(kept_of(record, each.id).stands in ENDED for each in asked))
    stopped(worker, threads)
    assert (held_at_once, {kept_of(record, each.id).stands for each in asked}) == (MOST_LANES_AT_ONCE, {Stands.DONE})


def test_two_workers_that_want_one_lane_work_it_one_at_a_time_and_take_each_of_its_requests_up_once(record: Record) -> None:
    worker, lanes, proxy = serving_a(record)
    other = Worker(record, lanes, NO_SECRETS)
    inside: list[str] = []
    most_inside: list[int] = []

    def count_who_is_checking(container: str) -> None:
        inside.append(container)
        most_inside.append(len(inside))
        time.sleep(0.2)
        inside.remove(container)

    proxy.while_checking = count_who_is_checking
    moved = record.write(MOVE_B, CALLER, None)
    threads = [*worker.start(), *other.start()]
    wait_until(lambda: kept_of(record, moved.id).stands in ENDED)
    retired = record.write(Asked(product="site", lane="testing", work=Work.RETIRE), CALLER, None)
    wait_until(lambda: kept_of(record, retired.id).stands in ENDED)
    other.stop()
    stopped(worker, threads)
    assert (most_inside, [[each.ended for each in kept_of(record, asked.id).attempts] for asked in (moved, retired)]) == (
        [1],
        [["done"], ["done"]],
    )
    assert [action.split()[0] for action in proxy.host.actions] == ["start", "check", "route", "retire", "unroute", "retire"]
