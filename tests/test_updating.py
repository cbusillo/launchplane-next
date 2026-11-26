from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from launchplane.act import Cancelled, Lanes, Status, Waiting
from launchplane.data_set import login_of
from launchplane.docker_answers import CommandResult, ContainerHostError, ContainerHostRefused
from launchplane.gate import Answer as GateAnswer
from launchplane.instance import BACK_SOON, DataSet, DataSetUse, InstanceState, Verdict
from launchplane.judge import Condition, Work
from launchplane.page import CHECKING, cards
from launchplane.plan import lane_data_set
from launchplane.record import Asked, Kept, Record, Stands
from launchplane.steps import CopyVolume, FreshNames, Outcome, Stop, Update
from launchplane.telling import Witness
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS
from tests.odoo_product import TK, TK_UPDATING
from tests.pretend_host import (
    EARLIER,
    GENERATION,
    WEB_ADDRESS,
    Given,
    LaunchplaneKilled,
    PretendHost,
    PretendProxy,
    data_set_named,
    lanes_on,
    numbered_names,
)
from tests.test_act import BUILD_A, BUILD_B
from tests.test_copying import DATABASE, SOURCE, brought_back, copy_of_d1, copy_on, first, frozen
from tests.test_copying import worked as copy_worked
from tests.test_worker import CALLER, kept_of

STOPPED = InstanceState.STOPPED
LATER = "01900000-0000-7000-8000-00000000000c"
UPDATE = "timeout 600 /odoo/odoo-bin --update tk_website --stop-after-init --no-http"


# One lane of TK on its first build, serving, whose releases update a module.
def serving() -> tuple[Lanes, PretendProxy, str]:
    proxy = PretendProxy(PretendHost())
    lanes = lanes_on(proxy, TK_UPDATING)
    lanes.move(BUILD_A, EARLIER, 1)
    proxy.host.actions.clear()
    return lanes, proxy, proxy.written[WEB_ADDRESS]


def updater_of(proxy: PretendProxy, old: str) -> str:
    [updater] = [each for each in proxy.host.instances if each != old]
    return updater


def updates(proxy: PretendProxy) -> list[str]:
    return [each for each in proxy.host.actions if each.startswith("update ")]


def started(proxy: PretendProxy, old: str) -> list[str]:
    return [each for each in proxy.host.actions if each.startswith("start ") and each.split()[1] not in (old, "database")]


def copied(proxy: PretendProxy) -> list[str]:
    copy = copy_on(proxy)
    made = [f"volume {copy.data_set}", f"files {copy.data_set}"]
    return [*made, *[f"copy {source} to {to}" for source, to in zip(SOURCE, (*copy.volumes, *copy.files), strict=True)]]


def test_a_release_to_another_build_updates_once_on_a_copy_made_in_its_own_freeze_and_switches_once() -> None:
    lanes, proxy, old = serving()
    result = lanes.move(BUILD_B, GENERATION, 1, first())
    copy, new = copy_on(proxy), proxy.loaded_to(WEB_ADDRESS)
    made, copies = copied(proxy)[:2], copied(proxy)[2:]
    server = f"database-{copy.data_set}"
    assert proxy.host.actions == [
        *made,
        *frozen(old),
        *copies,
        f"network {copy.data_set}",
        f"database {copy.data_set} on network-{copy.data_set} and {copy.volumes[0]} from postgres:18-alpine",
        f"await {server}",
        *updates(proxy),
        f"start {new}",
        f"check {new}/web/login",
        f"route {new}",
        f"retire {old}",
    ]
    [update] = updates(proxy)
    assert update.endswith(f"-update with {UPDATE}") and new not in update
    assert (result.outcome, result.container_receiving_visitors, result.data_set) == (Outcome.SWITCHED, new, copy.data_set)
    assert proxy.gate.verdict(WEB_ADDRESS, GENERATION) == GateAnswer(Verdict.COMMITTED, new)
    kept = proxy.host.data_sets["data-1"]
    assert (kept.databases[0].state, kept.volumes, kept.files) == (STOPPED, (SOURCE[0],), (SOURCE[1],))
    assert [each.condition for each in lanes.read_status().lanes] == [Condition.GOOD]


def test_while_the_update_is_pending_the_candidate_s_web_has_never_started_and_one_start_follows_its_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lanes, proxy, old = serving()
    before: list[list[str]] = []
    run_once = proxy.host.run_once

    def note_what_started_and_update(update: Update, by: Witness) -> CommandResult:
        before.append(started(proxy, old))
        return run_once(update, by)

    monkeypatch.setattr(proxy.host, "run_once", note_what_started_and_update)
    result = lanes.move(BUILD_B, GENERATION, 1, first())
    new = proxy.loaded_to(WEB_ADDRESS)
    assert (before, started(proxy, old), result.outcome) == ([[]], [f"start {new}"], Outcome.SWITCHED)
    after_the_update = proxy.host.actions[proxy.host.actions.index(updates(proxy)[0]) + 1 :]
    assert after_the_update[:3] == [f"start {new}", f"check {new}/web/login", f"route {new}"]


# The lane's card as it reads at each moment a release on a copy passes: its pills, its rows of parts, the request's note.
def cards_read_during_a_release(record: Record, monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[list[str], list[str], str]]:
    read: dict[str, tuple[list[str], list[str], str]] = {}
    proxy = PretendProxy(PretendHost())
    lanes = Lanes(proxy.host, proxy, lambda _product: TK_UPDATING, numbered_names(), read_unfinished=record.unfinished)

    def read_the_card(moment: str) -> None:
        [card] = cards(lanes.read_status())
        parts = [f"{row.name}: {row.pills[0].text}" for row in card.rows if row.name.startswith(("Instance", "Update"))]
        [note] = [row.note or "" for row in card.rows if row.name.startswith("Request ")]
        read.setdefault(moment, ([pill.text for pill in card.pills], parts, note))

    record_action, start_instance = proxy.host.record, proxy.host.start_instance

    def read_while_the_update_runs(action: str) -> None:
        if action.startswith("await "):
            read_the_card("copied, before the update")
        if action.startswith("update "):
            read_the_card("while the update runs")
        record_action(action)

    def read_before_the_start(*given: Any, **named: Any) -> None:  # noqa: ANN401 as start_instance takes them
        read_the_card("after the update, before the start")
        start_instance(*given, **named)

    lanes.move(BUILD_A, EARLIER, 1)
    request = record.write(Asked(product="site", lane="testing", work=Work.MOVE, build=BUILD_B.build), CALLER, None)
    monkeypatch.setattr(proxy.host, "record", read_while_the_update_runs)
    monkeypatch.setattr(proxy.host, "start_instance", read_before_the_start)
    proxy.while_checking = lambda _container: read_the_card("while the candidate is checked")
    assert Worker(record, lanes, NO_SECRETS).work_next()
    assert kept_of(record, request.id).stands is Stands.DONE
    return read


def test_while_the_update_runs_the_card_shows_it_as_an_update_that_runs_and_never_as_a_wrong_instance(
    record: Record, monkeypatch: pytest.MonkeyPatch
) -> None:
    pills, parts, note = cards_read_during_a_release(record, monkeypatch)["while the update runs"]
    assert (pills, parts) == (["in maintenance", "work under way"], ["Instance 1: stopped", "Update: running"])
    assert "the new build is updating the copy. Nothing of the new build is started on it until" in note


def test_once_the_data_is_copied_and_before_the_update_runs_the_card_says_the_update_comes_next_and_not_that_it_runs(
    record: Record, monkeypatch: pytest.MonkeyPatch
) -> None:
    pills, parts, note = cards_read_during_a_release(record, monkeypatch)["copied, before the update"]
    assert (pills, parts) == (["in maintenance", "work under way"], ["Instance 1: stopped"])
    assert "The data is copied. The new build updates the copy next" in note


def test_after_the_update_and_before_the_candidate_is_started_the_card_says_it_is_started_next_and_not_that_it_is_checked(
    record: Record, monkeypatch: pytest.MonkeyPatch
) -> None:
    pills, parts, note = cards_read_during_a_release(record, monkeypatch)["after the update, before the start"]
    assert (pills, parts) == (["in maintenance", "work under way"], ["Instance 1: stopped"])
    assert "The copy is updated. The new build is started on it next, and then checked." in note


def test_the_card_says_the_new_build_is_being_checked_only_while_its_candidate_runs_on_the_host(
    record: Record, monkeypatch: pytest.MonkeyPatch
) -> None:
    pills, parts, note = cards_read_during_a_release(record, monkeypatch)["while the candidate is checked"]
    assert (pills, sorted(parts)) == (["in maintenance", "work under way"], ["Instance 1: stopped", "Instance 2: running"])
    assert CHECKING in note


def test_a_candidate_docker_started_again_after_its_first_process_ended_fails_its_check_and_the_original_comes_back() -> None:
    lanes, proxy, old = serving()

    def crash_and_start_again(container: str) -> None:
        if container != old:
            proxy.host.restarted[container] = 1

    proxy.while_checking = crash_and_start_again
    outcome, error = given_up(lanes, proxy, old)
    assert outcome is Outcome.CHECK_FAILED
    assert "its first process ended and Docker has started it again 1 time(s) since it was started" in error
    assert [each for each in proxy.host.actions if each.startswith("route ")] == [f"route {old}"]


def test_what_ran_the_update_is_gone_before_the_candidate_starts_and_held_a_login_of_its_own_on_the_copy() -> None:
    lanes, proxy, _ = serving()
    result = lanes.move(BUILD_B, GENERATION, 1, first())
    new = proxy.loaded_to(WEB_ADDRESS)
    assert (list(proxy.host.instances), result.container_receiving_visitors) == ([new], new)
    assert proxy.host.logins_sent[-2:] == ["make i_update", f"make {login_of(new)}"]


def test_the_candidate_and_its_update_reach_only_the_copy_s_network_and_files(monkeypatch: pytest.MonkeyPatch) -> None:
    lanes, proxy, old = serving()
    updated_on: list[DataSetUse] = []
    run_once = proxy.host.run_once

    def note_and_update(update: Update, by: Witness) -> CommandResult:
        updated_on.append(update.use)
        return run_once(update, by)

    monkeypatch.setattr(proxy.host, "run_once", note_and_update)
    lanes.move(BUILD_B, GENERATION, 1, first())
    assert [(each.data_set, each.network, each.files) for each in updated_on] == [
        (copy_on(proxy).data_set, f"network-{copy_on(proxy).data_set}", copy_on(proxy).files[0])
    ]
    copy, new = copy_on(proxy), proxy.host.instances[proxy.loaded_to(WEB_ADDRESS)]
    assert (new.data_set, new.networks[1:], [each.source for each in new.mounts]) == (
        copy.data_set,
        (f"network-{copy.data_set}",),
        list(copy.files),
    )
    assert old not in proxy.host.instances


def given_up(lanes: Lanes, proxy: PretendProxy, old: str, attempt: int = 1) -> tuple[Outcome, str]:
    result = lanes.move(BUILD_B, GENERATION, attempt, first(attempt))
    assert proxy.host.actions[-5:] == brought_back(old)
    assert (proxy.loaded_to(WEB_ADDRESS), list(proxy.host.instances)) == (old, [old])
    assert (result.container_receiving_visitors, result.data_set) == (old, "data-1")
    assert proxy.gate.verdict(WEB_ADDRESS, GENERATION).verdict is Verdict.NONE
    assert copy_on(proxy).databases[0].state is STOPPED
    return result.outcome, result.error or ""


def test_an_update_that_fails_puts_visitors_back_on_the_original_instance_and_data_and_nothing_is_switched() -> None:
    lanes, proxy, old = serving()
    proxy.host.updates_that_fail = {BUILD_B.build}
    outcome, error = given_up(lanes, proxy, old)
    assert outcome is Outcome.NOT_UPDATED
    assert "ended with code 1. Nothing was switched" in error and error.endswith("Its last log lines:\nmodule not found")
    assert not [each for each in proxy.host.actions if each.startswith("check ") and old not in each]
    assert (started(proxy, old), [each for each in proxy.host.actions if each.startswith("route ")]) == ([], [f"route {old}"])
    assert [each.condition for each in lanes.read_status().lanes] == [Condition.GOOD]


def test_a_candidate_that_fails_its_check_after_the_update_is_never_switched_to_and_the_original_comes_back() -> None:
    lanes, proxy, old = serving()

    def fail_the_candidate(container: str) -> None:
        if container != old:
            proxy.failing_its_kind.add(container)

    proxy.while_checking = fail_the_candidate
    outcome, error = given_up(lanes, proxy, old)
    assert (outcome, "The check of its kind failed" in error) == (Outcome.CHECK_FAILED, True)
    assert len([each for each in proxy.host.actions if each.startswith("update ")]) == 1


def test_an_update_whose_reply_was_lost_is_not_run_or_looked_into_again_and_the_next_attempt_brings_the_original_back() -> None:
    lanes, proxy, old = serving()
    proxy.host.updates_unanswered = {BUILD_B.build}
    with pytest.raises(ContainerHostError, match="the reply was lost"):
        lanes.move(BUILD_B, GENERATION, 1, first())
    assert proxy.written[WEB_ADDRESS] == BACK_SOON
    proxy.host.actions.clear()
    outcome, error = given_up(lanes, proxy, old, attempt=2)
    assert (outcome, "whether its update ran is not known" in error) == (Outcome.NOT_UPDATED, True)
    assert not [each for each in proxy.host.actions if each.startswith(("update ", "copy "))]


def test_a_copy_whose_files_volume_is_gone_is_never_given_a_server_though_its_database_volume_was_copied_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lanes, proxy, old = serving()
    copy_volume = proxy.host.copy_volume

    def copy_and_lose_the_files_volume(copy: CopyVolume) -> None:
        copy_volume(copy)
        if copy.destination.startswith("files-"):
            found = proxy.host.data_sets[copy.data_set]
            proxy.host.data_sets[copy.data_set] = found.model_copy(update={"files": (), "copies": found.copies[:1]})

    monkeypatch.setattr(proxy.host, "copy_volume", copy_and_lose_the_files_volume)
    result = lanes.move(BUILD_B, GENERATION, 1, first())
    half = copy_on(proxy)
    assert (half.is_whole_copy, half.networks, half.databases) == (True, (), ())
    assert (result.outcome, proxy.loaded_to(WEB_ADDRESS), list(proxy.host.instances)) == (Outcome.NOT_UPDATED, old, [old])
    assert "does not have one volume for each volume of data-1" in (result.error or "")


def killed_during_the_update() -> tuple[Lanes, PretendProxy, str]:
    lanes, proxy, old = serving()
    proxy.host.killed_after = 11
    with pytest.raises(LaunchplaneKilled):
        lanes.move(BUILD_B, GENERATION, 1, first())
    assert proxy.host.actions[-1].startswith("update ")
    proxy.host.killed_after = None
    proxy.host.actions.clear()
    return lanes, proxy, old


def test_after_a_kill_during_the_update_the_next_attempt_gives_that_copy_up_and_the_original_serves_again() -> None:
    lanes, proxy, old = killed_during_the_update()
    updater = updater_of(proxy, old)
    outcome, _ = given_up(lanes, proxy, old, attempt=2)
    assert outcome is Outcome.NOT_UPDATED
    assert updater.endswith("-update") and started(proxy, old) == []
    assert proxy.host.actions == [f"retire {updater}", f"stop database database-{copy_on(proxy).data_set}", *brought_back(old)]


def test_an_attempt_that_finds_the_lane_already_back_changes_nothing_and_begins_no_second_copy() -> None:
    lanes, proxy, old = killed_during_the_update()
    given_up(lanes, proxy, old, attempt=2)
    proxy.host.actions.clear()
    again = lanes.move(BUILD_B, GENERATION, 3, first(3))
    assert (again.outcome, proxy.host.actions, len(proxy.host.data_sets)) == (Outcome.NOT_UPDATED, [], 2)


def test_a_request_asked_after_a_failed_one_makes_a_fresh_copy_and_switches() -> None:
    lanes, proxy, old = serving()
    proxy.host.updates_that_fail = {BUILD_B.build}
    given_up(lanes, proxy, old)
    proxy.host.updates_that_fail = set()
    result = lanes.move(BUILD_B, LATER, 1, Witness(generation=LATER, attempt=1))
    used = proxy.host.instances[proxy.loaded_to(WEB_ADDRESS)].data_set
    assert (result.outcome, sorted(proxy.host.data_sets)) == (Outcome.SWITCHED, sorted(["data-1", used or ""]))
    assert proxy.host.data_sets[used or ""].generation == LATER


def test_a_second_release_copies_the_copy_the_lane_now_serves_from_and_leaves_both_earlier_data_sets_offline() -> None:
    lanes, proxy, _ = serving()
    lanes.move(BUILD_B, GENERATION, 1, first())
    first_copy = copy_on(proxy).data_set
    result = lanes.move(BUILD_A, LATER, 1, Witness(generation=LATER, attempt=1))
    second = proxy.host.data_sets[result.data_set or ""]
    assert (result.outcome, second.copy_of, second.generation) == (Outcome.SWITCHED, first_copy, LATER)
    assert [proxy.host.data_sets[each].databases[0].state for each in ("data-1", first_copy)] == [STOPPED, STOPPED]
    assert [each.condition for each in lanes.read_status().lanes] == [Condition.GOOD]


def test_the_serving_build_asked_again_copies_nothing_and_updates_nothing() -> None:
    lanes, proxy, _ = serving()
    result = lanes.move(BUILD_A, GENERATION, 1, first())
    assert (result.outcome, proxy.host.actions) == (Outcome.UNCHANGED, [])


def test_a_cancel_behind_the_back_soon_page_brings_the_original_back_before_the_request_ends_as_cancelled() -> None:
    lanes, proxy, old = serving()

    def asked_once_the_update_ran() -> bool:
        return any(each.startswith("update ") for each in proxy.host.actions)

    with pytest.raises(Cancelled) as cancelled:
        lanes.move(BUILD_B, GENERATION, 1, Witness(generation=GENERATION, attempt=1, cancel_asked=asked_once_the_update_ran))
    assert proxy.host.actions[-5:] == brought_back(old)
    assert (proxy.loaded_to(WEB_ADDRESS), list(proxy.host.instances)) == (old, [old])
    assert cancelled.value.put_back.data_sets_kept == [copy_on(proxy).data_set]
    assert proxy.gate.verdict(WEB_ADDRESS, GENERATION).verdict is Verdict.ABANDONED


# The names a pretend lane gives are counted, so a second lane brought up the same way names its candidate the same.
def name_of_the_candidate() -> str:
    lanes, proxy, _ = serving()
    lanes.move(BUILD_B, GENERATION, 1, first())
    return proxy.loaded_to(WEB_ADDRESS)


def test_a_start_docker_refuses_behind_the_back_soon_page_does_not_end_the_request_with_the_lane_frozen() -> None:
    lanes, proxy, old = serving()
    proxy.host.commands_refused = {f"make {name_of_the_candidate()}"}
    with pytest.raises(Waiting, match="behind the back soon page, so this is tried again"):
        lanes.move(BUILD_B, GENERATION, 1, first())
    proxy.host.commands_refused = set()
    proxy.host.actions.clear()
    outcome, _ = given_up(lanes, proxy, old, attempt=2)
    assert outcome is Outcome.NOT_UPDATED


def test_a_refusal_before_the_back_soon_page_shows_fails_the_request_with_the_lane_still_serving() -> None:
    lanes, proxy, old = serving()
    proxy.host.refuses_helpers = True
    with pytest.raises(ContainerHostRefused, match="pull access denied"):
        lanes.move(BUILD_B, GENERATION, 1, first())
    assert (proxy.loaded_to(WEB_ADDRESS), proxy.host.is_in(old, InstanceState.RUNNING)) == (old, True)


def test_a_lane_with_no_instance_does_not_pick_between_its_data_set_and_a_copy_a_release_may_have_switched_to() -> None:
    served = copy_of_d1(databases=data_set_named("d2", STOPPED).databases)
    lane = Given(data_sets=(data_set_named("d1", STOPPED), served)).reading()
    picked = lane_data_set(lane, None, "fresh")
    assert isinstance(picked, Stop) and "has the data sets d1, d2" in picked.reason


def test_the_first_move_of_a_lane_whose_product_updates_copies_nothing() -> None:
    proxy = PretendProxy(PretendHost())
    result = lanes_on(proxy, TK_UPDATING).move(BUILD_A, GENERATION, 1, first())
    assert result.outcome is Outcome.SWITCHED
    assert not [each for each in proxy.host.actions if each.startswith(("copy ", "update ", "show back soon"))]
    assert DATABASE in [each.container for found in proxy.host.data_sets.values() for each in found.databases]


def worked(record: Record, change: Callable[[PretendProxy], None] = lambda _proxy: None) -> tuple[Kept, Lanes, PretendProxy]:
    proxy = PretendProxy(PretendHost())
    lanes = Lanes(
        proxy.host,
        proxy,
        lambda _product: TK_UPDATING,
        numbered_names(),
        read_unfinished=record.unfinished,
        read_ended=record.ended_lately,
    )
    lanes.move(BUILD_A, EARLIER, 1)
    change(proxy)
    request = record.write(Asked(product="site", lane="testing", work=Work.MOVE, build=BUILD_B.build), CALLER, None)
    assert Worker(record, lanes, NO_SECRETS).work_next()
    return kept_of(record, request.id), lanes, proxy


def fail_the_update(of: PretendProxy) -> None:
    of.host.updates_that_fail.add(BUILD_B.build)


def note_on_the_card(lanes: Lanes, kept: Kept) -> str:
    [note] = [row.note for card in cards(lanes.read_status()) for row in card.rows if row.name == f"Request {kept.id}"]
    return note or ""


def test_a_release_on_a_copy_ends_done_as_switched_once_with_one_update_and_its_card_says_so_in_one_paragraph(
    record: Record,
) -> None:
    kept, lanes, proxy = worked(record)
    assert (kept.stands, kept.answer and kept.answer["outcome"], kept.release_verdict) == (
        Stands.DONE,
        "switched",
        Verdict.COMMITTED,
    )
    lines = [each.did for each in kept.events]
    assert len([each for each in lines if each.startswith("update the data of ")]) == 1
    assert len([each for each in lines if each.startswith("switch ")]) == 1
    assert kept.answer and [each for each in kept.answer["did"] if each.startswith("updated the data of ")]
    assert note_on_the_card(lanes, kept) == (
        f"Visitors are on the new build and the updated copy. Every line is in GET /requests/{kept.id}."
    )
    assert copy_on(proxy).data_set == proxy.host.instances[proxy.loaded_to(WEB_ADDRESS)].data_set


def test_a_failed_update_ends_the_request_failed_with_no_switch_and_its_card_says_what_failed_and_that_the_lane_serves(
    record: Record,
) -> None:
    kept, lanes, _ = worked(record, fail_the_update)
    assert (kept.stands, kept.answer and kept.answer["outcome"], kept.release_verdict) == (
        Stands.FAILED,
        "not updated",
        Verdict.NONE,
    )
    assert not [each for each in kept.events if each.did.startswith("switch ")]
    note = note_on_the_card(lanes, kept)
    assert "ended with code 1. Nothing was switched, and the lane serves from its own data again." in note
    assert "Its lines:" not in note and note.endswith(f"Every line is in GET /requests/{kept.id}.")
    assert [each.condition for each in lanes.read_status().lanes] == [Condition.GOOD]


def test_while_the_original_cannot_come_back_the_request_stays_working_and_its_card_says_what_failed_and_what_comes_next(
    record: Record,
) -> None:
    def fail_the_update_and_the_original(of: PretendProxy) -> None:
        of.host.updates_that_fail.add(BUILD_B.build)
        of.failing = {of.written[WEB_ADDRESS]}

    kept, lanes, proxy = worked(record, fail_the_update_and_the_original)
    assert (kept.stands, proxy.written[WEB_ADDRESS]) == (Stands.WORKING, BACK_SOON)
    assert "The lane could not be brought back yet, and it is tried again" in (kept.error or "")
    note = note_on_the_card(lanes, kept)
    assert "update the data of " in note and "ended with code 1." in note
    assert "Nothing is switched: Launchplane is bringing the original instance back on its own data." in note


def test_once_switched_the_card_of_a_request_still_at_work_says_so_and_never_that_the_original_is_coming_back(
    record: Record,
) -> None:
    proxy = PretendProxy(PretendHost())
    lanes = Lanes(proxy.host, proxy, lambda _product: TK_UPDATING, numbered_names(), read_unfinished=record.unfinished)
    lanes.move(BUILD_A, EARLIER, 1)
    old = proxy.written[WEB_ADDRESS]
    request = record.write(Asked(product="site", lane="testing", work=Work.MOVE, build=BUILD_B.build), CALLER, None)
    proxy.host.killed_after = len(proxy.host.actions) + 15
    with pytest.raises(LaunchplaneKilled):
        Worker(record, lanes, NO_SECRETS).work_next()
    kept = kept_of(record, request.id)
    assert proxy.host.actions[-1] == f"retire {old}" and [each.did for each in kept.events if each.error] == [f"retire {old}"]
    assert "Visitors are switched to the new build on the updated copy." in note_on_the_card(lanes, kept)


def test_a_copy_asked_for_by_itself_keeps_every_line_on_its_card(record: Record) -> None:
    kept, lanes = copy_worked(record, set())
    [note] = [row.note for card in cards(lanes.read_status()) for row in card.rows if row.name == f"Request {kept.id}"]
    assert "Its lines: make the database volume of the data set" in (note or "")


def copy_generation(number: int) -> str:
    return f"01900000-0000-7000-8000-0000000000{number:02}"


# One serving lane of TK with three whole copies of its data, each asked for by itself, and the release that comes next.
def released_with_three_old_copies(record: Record) -> tuple[Kept, Lanes, PretendProxy, list[str]]:
    def copy_three_times(of: PretendProxy) -> None:
        fresh = FreshNames("site-testing-old", "old")
        asking = Lanes(
            of.host, of, lambda _product: TK_UPDATING, lambda _target: replace(fresh, data_set=f"old-{len(of.host.data_sets)}")
        )
        for number in range(3):
            generation = copy_generation(number)
            assert asking.copy("site", "testing", generation, Witness(generation=generation, attempt=1)).error is None

    kept, lanes, proxy = worked(record, copy_three_times)
    return kept, lanes, proxy, ["old-1", "old-2", "old-3"]


def deleted(kept: Kept) -> list[tuple[str, str | None]]:
    return [(each.did, each.error) for each in kept.events if each.did.startswith("delete the data set ")]


def test_a_release_that_switched_leaves_what_the_lane_serves_from_and_what_it_replaced_and_deletes_each_older_copy_with_a_line(
    record: Record,
) -> None:
    kept, lanes, proxy, old = released_with_three_old_copies(record)
    serving_from = proxy.host.instances[proxy.loaded_to(WEB_ADDRESS)].data_set or ""
    assert (kept.stands, sorted(proxy.host.data_sets)) == (Stands.DONE, sorted(["data-1", serving_from]))
    assert deleted(kept) == [(f"delete the data set {each}", None) for each in old]
    notes = {row.note for card in cards(lanes.read_status()) for row in card.rows if row.name == "Data set"}
    assert notes == {
        f"database-{serving_from} Kept: the lane serves from it.",
        "database-data-1 Kept: the last release replaced it.",
    }


def test_until_a_release_switches_the_card_says_of_an_old_copy_that_the_next_one_deletes_it() -> None:
    lanes, _, _ = serving()
    lanes.copy("site", "testing", GENERATION, first())
    notes = [row.note for card in cards(lanes.read_status()) for row in card.rows if row.name == "Data set"]
    assert notes == ["An old copy: the next release that switches deletes it.", "database-data-1 Kept: the lane serves from it."]


def test_a_release_that_fails_deletes_nothing_and_the_copy_it_made_is_kept_until_one_switches(record: Record) -> None:
    kept, _, proxy = worked(record, fail_the_update)
    assert (kept.stands, deleted(kept), len(proxy.host.data_sets)) == (Stands.FAILED, [], 2)


NO_ANSWERS = {
    "refused": ContainerHostRefused("Deleting the data set on test was refused: volume is in use"),
    "not answered": ContainerHostError("Deleting the data set on test failed: Read timed out."),
}


@pytest.mark.parametrize("no", NO_ANSWERS.values(), ids=NO_ANSWERS.keys())
def test_an_old_copy_docker_will_not_or_does_not_delete_is_asked_for_twice_never_holds_the_switched_release_and_goes_with_the_next(
    record: Record, monkeypatch: pytest.MonkeyPatch, no: ContainerHostError
) -> None:
    def refuse(_host: PretendHost, _found: DataSet) -> None:
        raise no

    with monkeypatch.context() as refusing:
        refusing.setattr(PretendHost, "delete_data_set", refuse)
        kept, lanes, proxy, old = released_with_three_old_copies(record)
    assert (kept.stands, kept.answer and kept.answer["outcome"], kept.error) == (Stands.DONE, "switched", None)
    assert [did for did, error in deleted(kept) if error == str(no)] == [
        f"delete the data set {each}" for each in old for _ in range(2)
    ]
    assert (set(old) < set(proxy.host.data_sets), record.unfinished()) == (True, [])
    lanes.move(BUILD_A, LATER, 1, Witness(generation=LATER, attempt=1))
    assert not set(old) & set(proxy.host.data_sets)


def test_an_old_copy_is_deleted_with_the_copiers_of_the_copy_made_from_it_which_still_hold_its_volumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lanes, proxy, _ = serving()
    gone: list[DataSet] = []
    delete = proxy.host.delete_data_set

    def note_and_delete(found: DataSet) -> None:
        gone.append(found)
        delete(found)

    monkeypatch.setattr(proxy.host, "delete_data_set", note_and_delete)
    lanes.move(BUILD_B, GENERATION, 1, first())
    first_copy = copy_on(proxy)
    second = lanes.move(BUILD_A, LATER, 1, Witness(generation=LATER, attempt=1)).data_set or ""
    readers = {each.container for each in proxy.host.data_sets[second].copies}
    assert (gone, readers) == ([], {f"{each}-copier" for each in (f"volume-{second}", f"files-{second}")})
    third = lanes.move(BUILD_B, copy_generation(9), 1, Witness(generation=copy_generation(9), attempt=1)).data_set or ""
    assert [each.data_set for each in gone] == [first_copy.data_set]
    assert readers < {each.container for each in gone[0].copies}
    assert sorted(proxy.host.data_sets) == sorted(["data-1", second, third])


def test_an_old_copy_left_half_deleted_with_no_database_server_never_keeps_the_next_release_from_updating_and_switching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lanes, proxy, old = serving()
    proxy.host.updates_that_fail = {BUILD_B.build}
    given_up(lanes, proxy, old)
    proxy.host.updates_that_fail = set()
    half = copy_on(proxy).data_set

    def remove_the_server_and_refuse_the_network(_host: PretendHost, found: DataSet) -> None:
        proxy.host.data_sets[found.data_set] = proxy.host.data_sets[found.data_set].model_copy(update={"databases": ()})
        raise ContainerHostRefused(f"Deleting the data set {found.data_set} on test was refused: network has active endpoints")

    with monkeypatch.context() as refusing:
        refusing.setattr(PretendHost, "delete_data_set", remove_the_server_and_refuse_the_network)
        switched = lanes.move(BUILD_B, LATER, 1, Witness(generation=LATER, attempt=1))
    assert (switched.outcome, proxy.host.data_sets[half].databases, len(proxy.host.data_sets[half].networks)) == (
        Outcome.SWITCHED,
        (),
        1,
    )
    again = copy_generation(9)
    result = lanes.move(BUILD_A, again, 1, Witness(generation=again, attempt=1))
    assert (result.outcome, sorted(proxy.host.data_sets)) == (
        Outcome.SWITCHED,
        sorted(["data-1", switched.data_set or "", result.data_set or ""]),
    )


# The sentence under each data set of the card, by data set: a row with a server names its container before it.
def data_set_rows(status: Status) -> dict[str, str]:
    rows = [row for card in cards(status) for row in card.rows if row.name == "Data set"]
    with_a_server = [(row.note or "").removeprefix("database-").split(" ", 1) for row in rows if row.value[0].drawn_as != "code"]
    without = [(row.value[0].text, row.note or "") for row in rows if row.value[0].drawn_as == "code"]
    return dict([*((name, note) for name, note in with_a_server), *without])


def data_set_notes(lanes: Lanes) -> dict[str, str]:
    return data_set_rows(lanes.read_status())


def test_after_a_release_that_kept_the_lane_s_data_the_card_no_longer_says_the_last_release_replaced_what_it_was_copied_from() -> (
    None
):
    lanes, proxy, _ = serving()
    copied_to = lanes.move(BUILD_B, GENERATION, 1, first()).data_set or ""
    assert data_set_notes(lanes) == {copied_to: "Kept: the lane serves from it.", "data-1": "Kept: the last release replaced it."}
    keeping = Lanes(proxy.host, proxy, lambda _product: TK, lambda _target: FreshNames("site-testing-later", "data-later"))
    kept = keeping.move(BUILD_A, LATER, 1, Witness(generation=LATER, attempt=1))
    assert (kept.outcome, kept.data_set, sorted(proxy.host.data_sets)) == (
        Outcome.SWITCHED,
        copied_to,
        sorted(["data-1", copied_to]),
    )
    assert data_set_notes(keeping) == {
        copied_to: "Kept: the lane serves from it.",
        "data-1": "Kept: the lane's data was copied from it by an earlier release.",
    }


def test_while_a_release_is_copying_the_card_says_its_new_copy_is_being_made_and_never_that_it_is_an_old_copy(
    record: Record, monkeypatch: pytest.MonkeyPatch
) -> None:
    read: list[dict[str, str]] = []
    edit = PretendProxy.edit

    def read_the_card_as_the_freeze_begins(freezing: PretendProxy, web_address: str, *sent: Any) -> str:  # noqa: ANN401 as edit takes them
        if sent[-1] == BACK_SOON and not read:
            status = Lanes(freezing.host, freezing, lambda _product: TK_UPDATING, read_unfinished=record.unfinished).read_status()
            read.append(data_set_rows(status))
        return edit(freezing, web_address, *sent)

    monkeypatch.setattr(PretendProxy, "edit", read_the_card_as_the_freeze_begins)
    kept, _, proxy = worked(record)
    serving_from = proxy.host.instances[proxy.loaded_to(WEB_ADDRESS)].data_set or ""
    assert (kept.stands, read) == (
        Stands.DONE,
        [{"data-1": "Kept: the lane serves from it.", serving_from: "A request at work on the lane is making it."}],
    )
