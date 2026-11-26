import http.client
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from typing import Any

import pytest

from tests.bed import (
    SCRATCH_ADDRESS,
    ask,
    ask_and_wait,
    kill_once,
    move_lane,
    needs_test_bed,
    on_scratch,
    read_running,
    retire,
    wait_for,
)
from tests.test_bed_data_set import (
    BUILD_A,
    BUILD_B,
    DESCRIPTION,
    LANE,
    PRODUCT,
    LaneView,
    asked_of_server,
    build_pretend_product,
    database_of,
    environment_of,
    lane_on_a,
    lane_view,
    logins_on,
    place_description,
    retire_lane_and_its_data,
    rows_read,
    web_address_of,
    write_row,
)

pytestmark = needs_test_bed

SITE = "test-bed-site"
SITE_LANE = "maintenance"
SITE_BUILD = "nginx:1.30.6-alpine"
BEAT_SECONDS = 0.2


def begin(product: str = PRODUCT, lane: str = LANE) -> tuple[int, dict[str, Any]]:
    return ask_and_wait(f"/lanes/{product}/{lane}/maintenance/begin", {})


def end(product: str = PRODUCT, lane: str = LANE) -> tuple[int, dict[str, Any]]:
    return ask_and_wait(f"/lanes/{product}/{lane}/maintenance/end", {})


def visit(web_address: str, method: str, path: str, body: str | None = None) -> tuple[int, str | None, str | None]:
    connection = http.client.HTTPConnection(SCRATCH_ADDRESS, 80, timeout=5)
    try:
        connection.request(method, path, body=body, headers={"Host": web_address})
        response = connection.getresponse()
        response.read()
        return response.status, response.getheader("Retry-After"), response.getheader("Cache-Control")
    finally:
        connection.close()


BACK_SOON = (503, "60", "no-store")


def shows_back_soon(web_address: str) -> bool:
    with suppress(OSError):
        return visit(web_address, "GET", "/") == BACK_SOON
    return False


def ids(database: str, table: str, where: str = "true") -> list[int]:
    found = asked_of_server(database, f"SELECT id FROM {table} WHERE {where} ORDER BY id")  # noqa: S608 a test's own tables
    return [int(line) for line in found]


def scratch_time() -> str:
    return on_scratch("date --utc +%Y-%m-%dT%H:%M:%S.%N+00").strip()


def database_accepts_a_connection(network: str) -> bool:
    probe = f"docker run --rm --network {network} postgres:18-alpine pg_isready --host=database --timeout=3"
    return on_scratch(f"{probe} >/dev/null 2>&1; echo exit=$?").strip() == "exit=0"


def states(view: LaneView) -> set[str]:
    databases = [database for data_set in view.data_sets for database in data_set["databases"]]
    return {part["state"] for part in [*view.instances, *view.background_services, *databases]}


def running_containers_of(product: str, lane: str) -> str:
    return on_scratch(f"docker ps --quiet --filter label=launchplane.product={product} --filter label=launchplane.lane={lane}")


def verdict_of(product: str, lane: str) -> dict[str, Any]:
    [verdict] = [each for each in read_running()["lanes"] if (each["product"], each["lane"]) == (product, lane)]
    return verdict


def restart_docker_on_scratch() -> None:
    on_scratch("systemctl restart docker")
    wait_for(lambda: ask("/running", timeout=10)[0] == 200, seconds=60)


@pytest.fixture(scope="module", autouse=True)
def pretend_product() -> Iterator[None]:
    build_pretend_product(BUILD_A, "a")
    build_pretend_product(BUILD_B, "b")
    place_description(PRODUCT, DESCRIPTION)
    yield
    retire_lane_and_its_data(LANE)


ledger = lane_on_a


@pytest.fixture
def site() -> Iterator[str]:
    status, body = move_lane(SITE, SITE_LANE, SITE_BUILD)
    assert status == 200, body
    yield f"{SITE_LANE}.{SITE}.test"
    status, body = retire(SITE, SITE_LANE)
    assert status == 200, body


def test_a_ledger_lane_in_maintenance_takes_no_write_even_across_a_docker_restart_and_comes_out_with_every_row(
    ledger: LaneView,
) -> None:
    database = database_of(ledger.data_sets[0])
    [network] = ledger.data_sets[0]["networks"]
    web_address = web_address_of(LANE)
    write_row("before")
    wait_for(lambda: bool(ids(database, "beats")), seconds=10)
    entries_before, beats_before = ids(database, "entries"), ids(database, "beats")
    assert database_accepts_a_connection(network["name"])

    status, body = begin()
    assert (status, body["outcome"], body["in_maintenance"]) == (200, "in maintenance", True), body
    began = scratch_time()
    assert visit(web_address, "GET", "/rows") == BACK_SOON
    assert visit(web_address, "POST", "/rows", "during") == BACK_SOON
    assert not database_accepts_a_connection(network["name"])
    assert states(lane_view()) == {"stopped"}
    time.sleep(2 * BEAT_SECONDS)

    restart_docker_on_scratch()
    wait_for(lambda: shows_back_soon(web_address), seconds=30)
    assert states(lane_view()) == {"stopped"}
    assert running_containers_of(PRODUCT, LANE) == ""
    verdict = verdict_of(PRODUCT, LANE)
    assert (verdict["in_maintenance"], verdict["problems"]) == (True, [])
    before_the_end = scratch_time()

    status, body = end()
    assert (status, body["outcome"], body["in_maintenance"]) == (200, "ended", False), body
    assert ids(database, "entries") == entries_before
    assert set(beats_before) <= set(ids(database, "beats"))
    assert ids(database, "beats", f"at > '{began}' AND at < '{before_the_end}'") == []
    write_row("after")
    assert rows_read() == ["before", "after"]
    wait_for(lambda: bool(ids(database, "beats", f"at > '{before_the_end}'")), seconds=10)
    assert verdict_of(PRODUCT, LANE)["in_maintenance"] is False


def test_a_plain_site_lane_goes_into_maintenance_and_out_and_asking_again_changes_nothing(site: str) -> None:
    first = begin(SITE, SITE_LANE)
    assert (first[0], first[1]["outcome"]) == (200, "in maintenance"), first
    stopped_at = on_scratch(f"docker inspect --format '{{{{.State.FinishedAt}}}}' $({containers_of(SITE, SITE_LANE)})")
    assert begin(SITE, SITE_LANE) == first
    assert on_scratch(f"docker inspect --format '{{{{.State.FinishedAt}}}}' $({containers_of(SITE, SITE_LANE)})") == stopped_at
    assert visit(site, "DELETE", "/any/path") == BACK_SOON
    [entry] = [each for each in read_running()["web_addresses"] if each["web_address"] == site]
    assert (entry["shows_back_soon_page"], entry["answers"]) == (True, False)

    status, body = end(SITE, SITE_LANE)
    assert (status, body["outcome"]) == (200, "ended"), body
    assert visit(site, "GET", "/")[0] == 200
    status, body = end(SITE, SITE_LANE)
    assert (status, body["outcome"]) == (200, "not in maintenance"), body


def containers_of(product: str, lane: str) -> str:
    return f"docker ps --all --quiet --filter label=launchplane.product={product} --filter label=launchplane.lane={lane}"


def test_a_move_is_refused_while_the_lane_is_in_maintenance_and_retiring_it_still_works(site: str) -> None:
    begin(SITE, SITE_LANE)
    status, body = move_lane(SITE, SITE_LANE, "nginx:1.31.8-alpine")
    assert status == 503, body
    assert "End maintenance first" in body["error"]
    assert running_containers_of(SITE, SITE_LANE) == ""
    status, body = retire(SITE, SITE_LANE)
    assert (status, body["outcome"], body["route_removed_to_container"]) == (200, "retired", "launchplane-back-soon"), body
    assert visit(site, "GET", "/")[0] == 404


def test_a_part_that_crashes_is_started_again_by_docker(ledger: LaneView) -> None:
    web = ledger.receiving()["container"]
    on_scratch(f"kill -KILL $(docker inspect --format '{{{{.State.Pid}}}}' {web})")
    wait_for(lambda: visit(web_address_of(LANE), "GET", "/")[0] == 200, seconds=30)
    assert on_scratch(f"docker inspect --format '{{{{.RestartCount}}}}' {web}").strip() == "1"


def stopped(parts: list[dict[str, Any]]) -> bool:
    return bool(parts) and all(part["state"] == "stopped" for part in parts)


BEGIN_POINTS: dict[str, Callable[[], bool]] = {
    "the web address shows the back soon page": lambda: shows_back_soon(web_address_of(LANE)),
    "the background service has stopped": lambda: stopped(lane_view().background_services),
    "the web part has stopped": lambda: stopped(lane_view().instances),
}
END_POINTS: dict[str, Callable[[], bool]] = {
    "the database runs again": lambda: not stopped(lane_view().data_sets[0]["databases"]),
    "the web part runs again": lambda: not stopped(lane_view().instances),
    "the web address is switched back": lambda: not shows_back_soon(web_address_of(LANE)),
}


@pytest.mark.usefixtures("ledger")
@pytest.mark.parametrize("point", BEGIN_POINTS.values(), ids=BEGIN_POINTS.keys())
def test_beginning_maintenance_killed_at_this_point_is_finished_by_asking_again(point: Callable[[], bool]) -> None:
    write_row("before the kill")
    kill_once(begin, point)
    status, body = begin()
    assert (status, body["outcome"]) == (200, "in maintenance"), body
    assert states(lane_view()) == {"stopped"}
    assert shows_back_soon(web_address_of(LANE))
    assert end()[0] == 200
    assert rows_read() == ["before the kill"]


@pytest.mark.usefixtures("ledger")
@pytest.mark.parametrize("point", END_POINTS.values(), ids=END_POINTS.keys())
def test_ending_maintenance_killed_at_this_point_is_finished_by_asking_again(point: Callable[[], bool]) -> None:
    write_row("before the kill")
    assert begin()[0] == 200
    kill_once(end, point)
    status, body = end()
    assert (status, body["outcome"]) in {(200, "ended"), (200, "not in maintenance")}, body
    assert states(lane_view()) == {"running"}
    verdict = verdict_of(PRODUCT, LANE)
    assert (verdict["in_maintenance"], verdict["problems"], verdict["left_unfinished"]) == (False, [], [])
    assert rows_read() == ["before the kill"]


def test_with_the_proxy_stopped_a_lane_in_maintenance_says_so_itself_as_its_web_address_does_and_beginning_again_starts_it(
    site: str,
) -> None:
    begin(SITE, SITE_LANE)
    on_scratch("docker stop launchplane-proxy")
    try:
        verdict = verdict_of(SITE, SITE_LANE)
        [entry] = [each for each in read_running()["web_addresses"] if each["web_address"] == site]
    finally:
        status, body = begin(SITE, SITE_LANE)
    assert (verdict["condition"], verdict["in_maintenance"]) == ("unfinished", True)
    assert ["The proxy is not running" in said for said in (*verdict["left_unfinished"], entry["error"])] == [True, True]
    assert (status, body["outcome"], verdict_of(SITE, SITE_LANE)["condition"]) == (200, "in maintenance", "in maintenance")


def test_ending_maintenance_removes_a_login_no_container_holds_so_asking_again_answers_over_nothing_left(ledger: LaneView) -> None:
    [data_set] = ledger.data_sets
    own = environment_of(ledger.receiving()["container"])["PGUSER"]
    asked_of_server(database_of(data_set), "CREATE ROLE i_left_by_a_failed_start LOGIN IN ROLE app")
    assert begin()[0] == 200
    first, again = end(), end()
    assert [(status, body["outcome"]) for status, body in (first, again)] == [(200, "ended"), (200, "not in maintenance")]
    assert (logins_on(data_set), verdict_of(PRODUCT, LANE)["condition"]) == ([own], "good")
