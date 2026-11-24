from collections.abc import Callable
from typing import NoReturn

import pytest
from fastapi.testclient import TestClient

from launchplane import plan, steps
from launchplane.act import Lanes, Stopped, Waiting
from launchplane.api import create_app
from launchplane.instance import BACK_SOON, HostNetwork, Mount, Unread
from launchplane.judge import Work
from launchplane.record import Asked, Kept, Record, Stands
from launchplane.telling import LOOKS, NAMED, named
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS
from tests.pretend_host import D1, EARLIER, GENERATION, WEB_ADDRESS, PretendProxy
from tests.pretend_record import PretendRecord
from tests.test_act import BUILD_A, lane_brought_up_with_a, lane_in_maintenance, lanes_on
from tests.test_worker import CALLER, MOVE_B, kept_of

DATABASE = "database-data-1"
MOVE_A = MOVE_B.model_copy(update={"build": "site:a"})
RETIRE = Asked(product="site", lane="testing", work=Work.RETIRE)
BEGIN = Asked(product="site", lane="testing", work=Work.BEGIN_MAINTENANCE)
END = Asked(product="site", lane="testing", work=Work.END_MAINTENANCE)
NOT_TOLD = Unread(asked=f"The logins of the database {DATABASE}", why="psql: connection refused")


def worker_on(proxy: PretendProxy, record: Record) -> Worker:
    return Worker(record, lanes_on(proxy), NO_SECRETS)


def lane_whose_background_service_is_gone() -> PretendProxy:
    proxy = lane_brought_up_with_a()
    proxy.host.background_services.clear()
    return proxy


def test_a_repair_whose_last_login_read_fails_stays_working_with_the_background_service_it_started_and_ends_done_once_read(
    record: Record,
) -> None:
    proxy = lane_whose_background_service_is_gone()
    host, watch = proxy.host, proxy.host.await_background_service

    def logins_unread_once_it_is_watched(container: str, steady_seconds: float) -> None:
        watch(container, steady_seconds)
        host.change_database(DATABASE, unheld_logins=NOT_TOLD)

    host.await_background_service = logins_unread_once_it_is_watched
    worker, request = worker_on(proxy, record), record.write(MOVE_A, CALLER, None)
    assert worker.work_next() is True
    left = kept_of(record, request.id)
    [service] = host.background_services.values()
    assert (left.stands, [each.ended for each in left.attempts], service.generation) == (
        Stands.WORKING,
        ["unfinished"],
        request.generation,
    )
    assert left.error is not None and str(NOT_TOLD) in left.error
    host.change_database(DATABASE, unheld_logins=())
    assert worker.work_next() is True
    done = kept_of(record, request.id)
    assert done.stands is Stands.DONE and done.answer is not None
    assert (done.answer["outcome"], done.answer["did"]) == ("repaired", [f"make {service.container}", f"start {service.container}"])
    assert [action.split()[0] for action in host.actions] == ["start", "await"]


def test_a_repair_that_fails_for_another_reason_leaves_the_background_service_it_started_for_the_instance_that_serves(
    record: Record,
) -> None:
    proxy = lane_whose_background_service_is_gone()
    proxy.not_answering[WEB_ADDRESS] = "HTTP 500"
    request = record.write(MOVE_A, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    assert kept.stands is Stands.FAILED and "HTTP 500" in (kept.error or "")
    assert [each.generation for each in proxy.host.background_services.values()] == [request.generation]


# A lane retired with its data kept, whose database server then has another volume: a start on it, and then a retire.
def a_start_and_then_a_retire_on_another_volume(
    record: Record, proxy: PretendProxy, unheld: object, retire_ends: Stands = Stands.DONE
) -> tuple[str, list[str]]:
    lanes_on(proxy).retire("site", "testing")
    proxy.host.change_database(DATABASE, mounts=(Mount(source="another-volume", path="/var/lib/postgresql"),), unheld_logins=unheld)
    worker = worker_on(proxy, record)
    move = record.write(MOVE_A, CALLER, None)
    assert worker.work_next() is True
    retire = record.write(RETIRE, CALLER, None)
    assert worker.work_next() is True
    failed, retired = kept_of(record, move.id), kept_of(record, retire.id)
    assert (failed.stands, retired.stands) == (Stands.FAILED, retire_ends), (failed, retired)
    assert failed.error is not None and "another-volume mounted" in failed.error
    assert (list(proxy.host.data_sets), proxy.host.instances) == (["data-1"], {})
    return failed.error, [] if retired.answer is None else retired.answer["logins_not_read"]


def test_a_start_on_a_wrongly_set_up_data_set_ends_failed_with_its_data_kept_and_says_the_logins_were_not_read_and_a_retire_ends(
    record: Record,
) -> None:
    error, not_read = a_start_and_then_a_retire_on_another_volume(record, lane_brought_up_with_a(), NOT_TOLD)
    assert f"The clean-up after it is not known to be whole: {NOT_TOLD}" in error
    assert not_read == [str(NOT_TOLD)]


def test_a_refused_lookup_of_who_holds_a_login_decides_neither_a_proved_failure_nor_a_retire_and_both_say_it_was_not_had(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()
    proxy.host.holders_unread.add(DATABASE)
    error, not_read = a_start_and_then_a_retire_on_another_volume(record, proxy, ("i_left",))
    assert f"is not known to be whole: Which logins of the database {DATABASE} no container holds could not be read" in error
    assert ["no container holds could not be read: timed out" in each for each in not_read] == [True]


REFUSED = f"The database {DATABASE} did not remove the login i_left: it owns objects"


def lane_with_a_login_the_server_will_not_remove() -> PretendProxy:
    proxy = lane_brought_up_with_a()
    proxy.host.change_database(DATABASE, unheld_logins=("i_left",))
    proxy.host.removals_refused.add(DATABASE)
    return proxy


def removals_sent(proxy: PretendProxy) -> int:
    return proxy.host.actions.count(f"remove logins from {DATABASE}")


# The request ends failed on the server's own error, and the refused removal was sent once.
def failed_on_the_refusal(record: Record, proxy: PretendProxy, asked: Asked) -> Kept:
    request = record.write(asked, CALLER, None)
    worker = worker_on(proxy, record)
    assert (worker.work_next(), worker.work_next()) == (True, False)
    kept = kept_of(record, request.id)
    assert (kept.stands, REFUSED in (kept.error or ""), removals_sent(proxy)) == (Stands.FAILED, True, 1), kept
    return kept


@pytest.mark.parametrize("asked", [MOVE_A, RETIRE], ids=["a move to the serving build", "a retire"])
def test_a_login_removal_the_server_refuses_ends_this_request_failed_with_the_servers_error_and_is_sent_once(
    record: Record, asked: Asked
) -> None:
    proxy = lane_with_a_login_the_server_will_not_remove()
    kept = failed_on_the_refusal(record, proxy, asked)
    assert "could not be read" not in (kept.error or "") and list(proxy.host.data_sets) == ["data-1"]
    assert asked is RETIRE or len(proxy.host.background_services) == 1


def test_a_removal_refused_while_cleaning_up_after_a_proved_failure_ends_failed_with_both_errors(record: Record) -> None:
    proxy = lane_brought_up_with_a()
    proxy.host.removals_refused.add(DATABASE)
    error, not_read = a_start_and_then_a_retire_on_another_volume(record, proxy, ("i_left",), retire_ends=Stands.FAILED)
    assert f"is not known to be whole: {REFUSED}" in error and not_read == []


def test_a_removal_refused_after_the_switch_ends_failed_and_the_release_stays_switched(record: Record) -> None:
    proxy = lane_with_a_login_the_server_will_not_remove()
    kept = failed_on_the_refusal(record, proxy, MOVE_B)
    assert proxy.gate.of(kept.generation).committed == proxy.written[WEB_ADDRESS] == proxy.loaded_to(WEB_ADDRESS)
    assert [each.build for each in proxy.host.instances.values()] == ["site:b"]


def cancelled_while_checking(record: Record, proxy: PretendProxy) -> Kept:
    request = record.write(MOVE_B, CALLER, None)

    def cancel(_container: str) -> None:
        record.cancel(request.id, "a test")

    proxy.while_checking = cancel
    worker = worker_on(proxy, record)
    assert (worker.work_next(), worker.work_next()) == (True, False)
    return kept_of(record, request.id)


def test_a_cancel_that_won_and_whose_login_removal_the_server_refuses_ends_failed_and_the_cancel_stays_won(record: Record) -> None:
    proxy = lane_with_a_login_the_server_will_not_remove()
    kept = cancelled_while_checking(record, proxy)
    assert (kept.stands, REFUSED in (kept.error or ""), removals_sent(proxy)) == (Stands.FAILED, True, 1), kept
    assert proxy.gate.of(kept.generation).abandoned and [each.build for each in proxy.host.instances.values()] == ["site:a"]


@pytest.mark.parametrize("holders_read", [False, True], ids=["holders not read", "holders read"])
def test_a_cancel_that_won_says_a_login_was_removed_only_where_it_was_and_names_a_lookup_that_failed(
    record: Record, holders_read: bool
) -> None:
    proxy = lane_brought_up_with_a()
    proxy.host.change_database(DATABASE, unheld_logins=("i_left",))
    if not holders_read:
        proxy.host.holders_unread.add(DATABASE)
    kept = cancelled_while_checking(record, proxy)
    assert kept.stands is Stands.CANCELLED and kept.answer is not None
    removed = [each for each in kept.answer["put_back"] if "login" in each]
    assert (len(removed), len(kept.answer["logins_not_read"])) == ((1, 0) if holders_read else (0, 1)), kept.answer
    assert proxy.host.data_sets["data-1"].databases[0].unheld_logins == (() if holders_read else ("i_left",))


def test_ending_maintenance_over_a_lookup_of_holders_that_failed_brings_the_lane_back_and_ends_saying_so(record: Record) -> None:
    proxy = lane_in_maintenance()
    proxy.host.logins_left_in[DATABASE] = ("i_left",)
    proxy.host.holders_unread.add(DATABASE)
    request = record.write(END, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    assert kept.stands is Stands.DONE and kept.answer is not None, kept
    assert (kept.answer["outcome"], len(kept.answer["logins_not_read"]), removals_sent(proxy)) == ("ended", 1, 1)
    assert proxy.written[WEB_ADDRESS] != BACK_SOON and proxy.host.data_sets["data-1"].databases[0].unheld_logins == ("i_left",)


def test_ending_maintenance_still_fails_over_another_finding_and_says_the_lookup_that_failed_too(record: Record) -> None:
    proxy = lane_in_maintenance()
    proxy.host.logins_left_in[DATABASE] = ("i_left",)
    proxy.host.holders_unread.add(DATABASE)
    edit = proxy.edit

    def back_with_a_way_out(web_address: str, generation: str, attempt: int, read: str, container: str | None) -> str:
        proxy.host.attach(container or "", "bridge", quiet=False)
        return edit(web_address, generation, attempt, read, container)

    proxy.edit = back_with_a_way_out
    request = record.write(END, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    assert kept.stands is Stands.FAILED and kept.error is not None
    assert "can reach out through bridge" in kept.error and "no container holds could not be read" in kept.error


class RecordNotRead(PretendRecord):
    def read(self, request: int) -> NoReturn:
        raise AssertionError(f"the history of request {request} was read for an answer that does not use it")


def test_beginning_maintenance_ends_without_reading_its_history() -> None:
    record = RecordNotRead()
    request = record.write(BEGIN, CALLER, None)
    assert worker_on(lane_brought_up_with_a(), record).work_next() is True
    kept = PretendRecord.read(record, request.id)
    assert kept is not None and kept.stands is Stands.DONE


NETWORKS = (HostNetwork(name="network-data-1", is_quiet=True), HostNetwork(name="network-beside-it", is_quiet=True))
DOUBLED: dict[str, tuple[tuple[object, ...], str]] = {
    "volumes": (("volume-data-1", "volume-put-beside-it"), r"2 volumes \(volume-data-1, volume-put-beside-it\)"),
    "networks": (NETWORKS, r"2 networks \(network-data-1, network-beside-it\)"),
}


@pytest.mark.parametrize("part", DOUBLED)
def test_a_retire_naming_a_data_set_that_has_two_of_these_deletes_nothing_of_it_names_both_and_retires_the_rest(part: str) -> None:
    twice, both = DOUBLED[part]
    proxy = lane_brought_up_with_a()
    host = proxy.host
    host.change_data_set("site", "testing", "data-1", **{part: twice})
    with pytest.raises(Stopped, match=f"{both}.*nothing of the data set was deleted"):
        lanes_on(proxy).retire("site", "testing", delete_data_set="data-1")
    assert (list(host.data_sets), host.instances, host.background_services) == (["data-1"], {}, {})
    assert not any(action.startswith("delete") for action in host.actions)


TWO_ROUTERS = {
    "to two instances": ("serving", "another"),
    "to the same two, listed the other way round": ("another", "serving"),
    "to the same instance": ("serving", "serving"),
}


@pytest.mark.parametrize("targets", TWO_ROUTERS.values(), ids=TWO_ROUTERS.keys())
@pytest.mark.parametrize("asked", [MOVE_A, MOVE_B], ids=["a move to the serving build", "a move to another build"])
def test_two_routers_for_a_lanes_web_address_are_the_same_conflict_to_status_and_a_move_and_nothing_is_changed_or_answered(
    record: Record, targets: tuple[str, str], asked: Asked
) -> None:
    proxy = lane_brought_up_with_a()
    serving = proxy.written[WEB_ADDRESS]
    proxy.route_twice(WEB_ADDRESS, *(serving if each == "serving" else "another" for each in targets))
    lanes = lanes_on(proxy)
    lanes.read_unfinished = record.unfinished
    worker = Worker(record, lanes, NO_SECRETS)
    request = record.write(asked, CALLER, None)
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    running = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes)).get("/running").json()
    [lane], [web_address], [card] = running["lanes"], running["web_addresses"], running["cards"]
    [conflict] = [finding for finding in lane["findings"] if finding["shown"] == "conflicting"]
    assert all(f"the router router-{number}@file to " in conflict["sentence"] for number in (0, 1))
    assert (lane["condition"], web_address["says"], web_address["error"]) == ("conflicting", "conflicting", conflict["sentence"])
    assert card["rows"][0]["note"] == conflict["sentence"]
    assert (kept.stands, kept.answer, conflict["sentence"] in (kept.error or "")) == (Stands.WORKING, None, True)
    assert (proxy.host.actions, proxy.written[WEB_ADDRESS]) == ([], serving)


NO_MOVE: dict[str, Callable[[Lanes], object]] = {
    "a retire": lambda lanes: lanes.retire("site", "testing"),
    "beginning maintenance": lambda lanes: lanes.begin_maintenance("site", "testing"),
    "ending maintenance": lambda lanes: lanes.end_maintenance("site", "testing"),
    "cleaning up after a failed move": lambda lanes: lanes.clean_up("site", "testing", GENERATION, 1, logins=True),
    "cleaning up after the move that made what serves": lambda lanes: lanes.clean_up("site", "testing", EARLIER, 1, logins=True),
}


@pytest.mark.parametrize("call", NO_MOVE.values(), ids=NO_MOVE.keys())
def test_under_two_routers_for_its_web_address_this_call_changes_nothing_and_does_not_answer(
    call: Callable[[Lanes], object],
) -> None:
    proxy = lane_brought_up_with_a()
    proxy.route_twice(WEB_ADDRESS, proxy.written[WEB_ADDRESS], "another")
    with pytest.raises(Waiting, match="has 2 answers where it must have one"):
        call(lanes_on(proxy))
    assert proxy.host.actions == []


LOOKING = (
    steps.AwaitRoute(WEB_ADDRESS, "a"),
    steps.AwaitDatabase("database"),
    steps.Check("a"),
    steps.AwaitBackgroundService("a"),
    steps.ReadLog("a"),
    steps.Abandon(WEB_ADDRESS, GENERATION),
)
CHANGING = (
    steps.StartProxy(),
    steps.EditRoute(WEB_ADDRESS, "a", ""),
    steps.CreateDataSetNetwork("site", "testing", "d1"),
    steps.CreateDataSetVolume("site", "testing", "d1"),
    steps.CreateDatabase("site", "testing", "d1", "network", "volume", "image"),
    steps.CopyVolume("site", "testing", "d2", "volume", "copy", "image"),
    steps.GiveUp("The database did not stop cleanly."),
    steps.StartDatabase("database"),
    steps.DeleteDataSet(D1),
    steps.StartInstance(BUILD_A, "a"),
    steps.StartBackgroundService(BUILD_A, "a", None, ()),
    steps.Retire("a"),
    steps.RemoveLogins("database"),
    steps.StopPart("a"),
    steps.StopDatabase("database"),
    steps.StartStopped("a"),
    steps.Switch(WEB_ADDRESS, "a", GENERATION, 1),
    steps.Discard(WEB_ADDRESS, "a", GENERATION, 1),
)


def test_every_step_is_told_as_a_change_or_as_a_look_and_a_request_s_account_of_what_it_did_takes_only_the_changes() -> None:
    assert {type(each) for each in (*LOOKING, *CHANGING)} == set(NAMED)
    assert [named(each).startswith(LOOKS) for each in LOOKING] == [True] * len(LOOKING)
    assert [named(each).startswith(LOOKS) for each in CHANGING] == [False] * len(CHANGING)
    assert [plan.change_made_by(each) is None for each in LOOKING] == [True] * len(LOOKING)
