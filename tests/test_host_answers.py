from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from launchplane.act import Lanes, Waiting
from launchplane.api import create_app
from launchplane.instance import Mount, Unread
from launchplane.judge import Work
from launchplane.record import Asked, Record, Stands
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS
from tests.pretend_host import EARLIER, GENERATION, WEB_ADDRESS, PretendProxy
from tests.test_act import lane_brought_up_with_a, lanes_on
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
    assert kept_of(record, request.id).stands is Stands.DONE
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


def test_a_start_on_a_wrongly_set_up_data_set_ends_failed_with_its_data_kept_and_says_the_logins_were_not_read_and_a_retire_ends(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()
    host = proxy.host
    lanes_on(proxy).retire("site", "testing")
    host.change_database(DATABASE, mounts=(Mount(source="another-volume", path="/var/lib/postgresql"),), unheld_logins=NOT_TOLD)
    worker = worker_on(proxy, record)
    move = record.write(MOVE_A, CALLER, None)
    assert worker.work_next() is True
    retire = record.write(RETIRE, CALLER, None)
    assert worker.work_next() is True
    failed, retired = kept_of(record, move.id), kept_of(record, retire.id)
    assert failed.stands is Stands.FAILED and failed.error is not None
    assert "another-volume mounted" in failed.error
    assert f"Whether it left a login no container holds is not known: {NOT_TOLD}." in failed.error
    assert (retired.stands, retired.answer and retired.answer["logins_not_read"]) == (Stands.DONE, [str(NOT_TOLD)])
    assert (list(host.data_sets), host.instances) == (["data-1"], {})


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
