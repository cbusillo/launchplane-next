from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from launchplane import plan, steps
from launchplane.act import Lanes, Stopped, Waiting
from launchplane.api import create_app
from launchplane.docker_answers import ContainerHostError
from launchplane.instance import Mount, Unread
from launchplane.judge import Work
from launchplane.record import Asked, Record, Stands
from launchplane.telling import LOOKS, NAMED, named
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS
from tests.pretend_host import D1, EARLIER, GENERATION, WEB_ADDRESS, PretendProxy
from tests.test_act import BUILD_A, lane_brought_up_with_a, lanes_on
from tests.test_worker import CALLER, MOVE_B, kept_of

DATABASE = "database-data-1"
MOVE_A = MOVE_B.model_copy(update={"build": "site:a"})
RETIRE = Asked(product="site", lane="testing", work=Work.RETIRE)
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
    assert (done.answer["outcome"], done.answer["did"]) == ("repaired", [f"start the background service of {service.instance}"])
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
def a_start_and_then_a_retire_on_another_volume(record: Record, proxy: PretendProxy, unheld: object) -> tuple[str, list[str]]:
    lanes_on(proxy).retire("site", "testing")
    proxy.host.change_database(DATABASE, mounts=(Mount(source="another-volume", path="/var/lib/postgresql"),), unheld_logins=unheld)
    worker = worker_on(proxy, record)
    move = record.write(MOVE_A, CALLER, None)
    assert worker.work_next() is True
    retire = record.write(RETIRE, CALLER, None)
    assert worker.work_next() is True
    failed, retired = kept_of(record, move.id), kept_of(record, retire.id)
    assert (failed.stands, retired.stands) == (Stands.FAILED, Stands.DONE), (failed, retired)
    assert failed.error is not None and "another-volume mounted" in failed.error and retired.answer is not None
    assert (list(proxy.host.data_sets), proxy.host.instances) == (["data-1"], {})
    return failed.error, retired.answer["logins_not_read"]


def test_a_start_on_a_wrongly_set_up_data_set_ends_failed_with_its_data_kept_and_says_the_logins_were_not_read_and_a_retire_ends(
    record: Record,
) -> None:
    error, not_read = a_start_and_then_a_retire_on_another_volume(record, lane_brought_up_with_a(), NOT_TOLD)
    assert f"Whether it left a login no container holds is not known: {NOT_TOLD}." in error
    assert not_read == [str(NOT_TOLD)]


def test_a_refused_lookup_of_who_holds_a_login_decides_neither_a_proved_failure_nor_a_retire_and_both_say_it_was_not_had(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()

    def holders_not_told(database: str) -> None:
        raise ContainerHostError(f"Listing the containers that hold the logins of {database} on test failed: timed out")

    proxy.host.remove_unheld_logins = holders_not_told
    error, not_read = a_start_and_then_a_retire_on_another_volume(record, proxy, ("i_left",))
    assert "Whether it left a login no container holds is not known: Listing the containers that hold the logins" in error
    assert ["Listing the containers" in each for each in not_read] == [True]


def test_a_retire_naming_a_data_set_that_has_two_volumes_deletes_nothing_of_it_names_both_and_retires_the_rest() -> None:
    proxy = lane_brought_up_with_a()
    host = proxy.host
    host.change_data_set("site", "testing", "data-1", volumes=("volume-data-1", "volume-put-beside-it"))
    with pytest.raises(Stopped, match=r"2 volumes \(volume-data-1, volume-put-beside-it\).*nothing of the data set was deleted"):
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
    "cleaning up after a failed move": lambda lanes: lanes.clean_up("site", "testing", GENERATION, 1),
    "cleaning up after the move that made the serving instance": lambda lanes: lanes.clean_up("site", "testing", EARLIER, 1),
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
    steps.WriteRoute(WEB_ADDRESS, "a"),
    steps.RemoveRoute(WEB_ADDRESS, "a"),
    steps.CreateDataSetNetwork("site", "testing", "d1"),
    steps.CreateDataSetVolume("site", "testing", "d1"),
    steps.CreateDatabase("site", "testing", "d1", "network", "volume", "image"),
    steps.StartDatabase("database"),
    steps.DeleteDataSet(D1),
    steps.StartInstance(BUILD_A, "a"),
    steps.StartBackgroundService(BUILD_A, "a", None, ()),
    steps.Retire("a"),
    steps.RemoveLogins("database"),
    steps.ShowBackSoon(WEB_ADDRESS),
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
