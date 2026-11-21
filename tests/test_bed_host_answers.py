import json
import time
from collections.abc import Iterator
from typing import Any

import pytest

from launchplane.proxy import PROXY_NAME, ROUTES_FOLDER, instance_url
from tests.bed import (
    ask,
    ask_and_wait,
    hold,
    launchplane_held,
    let_go,
    move_lane,
    needs_test_bed,
    on_scratch,
    request_ended,
    request_loop,
    retire,
    wait_for,
    wait_until_stopped_at,
)
from tests.test_bed_data_set import DESCRIPTION, build_pretend_product, place_description
from tests.test_bed_maintenance import BACK_SOON, shows_back_soon, visit

pytestmark = needs_test_bed

PRODUCT = "test-bed-answers"
LANE = "testing"
WEB_ADDRESS = f"{LANE}.{PRODUCT}.test"
BUILD_A, BUILD_B = "launchplane-test-ledger:a", "launchplane-test-ledger:b"
LOGIN_READ, WATCHED = "fail.logins.read_logins", "after.host.await_background_service"
BACK_SOON_UP, LANE_READ = "after.proxy.edit", f"fail.host.read?only={PRODUCT}"
IMPOSTOR, ANOTHER_VOLUME = "test-bed-answers-impostor", "test-bed-answers-another-volume"
LONGER_THAN_A_REQUEST_IS_LEFT_BEFORE_IT_IS_TAKEN_UP_AGAIN = 12


def running() -> dict[str, Any]:
    status, body = ask("/running")
    assert status == 200, body
    return body


def of_lane(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [part for part in parts if (part["product"], part["lane"]) == (PRODUCT, LANE)]


def judged() -> dict[str, Any]:
    [lane] = of_lane(running()["lanes"])
    return lane


def retire_lane_and_its_data() -> None:
    for data_set in of_lane(running()["data_sets"]):
        status, body = retire(PRODUCT, LANE, data_set["data_set"])
        assert status == 200, body
    status, body = retire(PRODUCT, LANE)
    assert status == 200, body


@pytest.fixture(scope="module", autouse=True)
def held_launchplane_and_the_pretend_product() -> Iterator[None]:
    build_pretend_product(BUILD_A, "a")
    build_pretend_product(BUILD_B, "b")
    place_description(PRODUCT, DESCRIPTION)
    with launchplane_held():
        yield
        let_go()
        retire_lane_and_its_data()


@pytest.fixture
def lane_on_a() -> Iterator[None]:
    retire_lane_and_its_data()
    status, body = move_lane(PRODUCT, LANE, BUILD_A)
    assert status == 200, body
    yield
    let_go()
    retire_lane_and_its_data()


def container_id(container: str) -> str:
    return on_scratch(f"docker inspect --format '{{{{.Id}}}} {{{{.State.Running}}}}' {container}").strip()


def beats_written(database: str) -> int:
    count = 'psql --username=postgres --dbname=app --tuples-only --no-align --command="SELECT count(*) FROM beats"'
    return int(on_scratch(f"docker exec {database} {count}"))


@pytest.mark.usefixtures("lane_on_a")
def test_a_repair_whose_login_read_is_refused_keeps_its_background_service_writing_and_ends_done_once_the_read_answers() -> None:
    [data_set], [before] = of_lane(running()["data_sets"]), of_lane(running()["background_services"])
    [database] = data_set["databases"]
    on_scratch(f"docker rm --force {before['container']}")
    hold(WATCHED)
    asked = ask(f"/lanes/{PRODUCT}/{LANE}/move", {"build": BUILD_A})[1]
    wait_until_stopped_at(WATCHED)
    hold(LOGIN_READ)
    let_go(WATCHED)

    def left_working_over_the_login_read() -> bool:
        now = ask(f"/requests/{asked['id']}")[1]
        assert now["stands"] == "working", now
        return "The logins of the database" in (now["error"] or "")

    wait_for(left_working_over_the_login_read, seconds=30)
    [repaired] = of_lane(running()["background_services"])
    started, beats = container_id(repaired["container"]), beats_written(database["container"])
    time.sleep(LONGER_THAN_A_REQUEST_IS_LEFT_BEFORE_IT_IS_TAKEN_UP_AGAIN)
    kept, shown = ask(f"/requests/{asked['id']}")[1], judged()
    assert (kept["stands"], {each["ended"] for each in kept["attempts"]}) == ("working", {"unfinished"}), kept
    assert (container_id(repaired["container"]), repaired["generation"]) == (started, asked["generation"])
    assert started.endswith("true") and beats_written(database["container"]) > beats + 20
    assert shown["condition"] == "could not be read"
    assert any(f"The logins of the database {database['container']} could not be read" in each for each in shown["problems"])
    let_go(LOGIN_READ)
    ended = request_ended(asked["id"])
    assert (ended["stands"], container_id(repaired["container"]), judged()["condition"]) == ("done", started, "good"), ended
    assert (ended["answer"]["outcome"], ended["answer"]["did"], "is not known" in ended["answer"]["not_known"]) == (
        "not known",
        [],
        True,
    ), ended


@pytest.mark.usefixtures("lane_on_a")
def test_a_start_on_a_database_with_another_volume_ends_failed_with_its_data_kept_and_says_so_and_the_retire_after_it_ends() -> None:
    [data_set] = of_lane(running()["data_sets"])
    [database], [network], [volume] = data_set["databases"], data_set["networks"], data_set["volumes"]
    labels = " ".join(
        f"--label launchplane.{label}"
        for label in (f"product={PRODUCT}", f"lane={LANE}", f"data-set={data_set['data_set']}", "part=database")
    )
    assert retire(PRODUCT, LANE)[0] == 200
    on_scratch(f"docker rm --force {database['container']}")
    on_scratch(
        f"docker run --detach --name {IMPOSTOR} {labels} --network {network['name']} --network-alias database "
        f"--env POSTGRES_PASSWORD=impostor --volume {ANOTHER_VOLUME}:/var/lib/postgresql postgres:18-alpine"
    )
    not_read = f"The logins of the database {IMPOSTOR} could not be read"
    try:
        wait_for(lambda: not_read in json.dumps(judged()), seconds=30)
        moved, failed = move_lane(PRODUCT, LANE, BUILD_A)
        retired, answer = retire(PRODUCT, LANE)
        assert moved == 503 and f"has {ANOTHER_VOLUME} mounted" in failed["error"], failed
        assert f"The clean-up after it is not known to be whole: {not_read}" in failed["error"], failed
        assert (retired, answer["outcome"], [not_read in each for each in answer["logins_not_read"]]) == (200, "unchanged", [True])
        assert (of_lane(running()["instances"]), on_scratch(f"docker volume ls --quiet --filter name={volume}").split()) == (
            [],
            [volume],
        )
    finally:
        on_scratch(f"docker rm --force {IMPOSTOR}; docker volume rm {ANOTHER_VOLUME}")


def states_of_the_lane() -> set[str]:
    shown = running()
    databases = [database for data_set in of_lane(shown["data_sets"]) for database in data_set["databases"]]
    return {part["state"] for part in (*of_lane(shown["instances"]), *of_lane(shown["background_services"]), *databases)}


@pytest.mark.usefixtures("lane_on_a")
def test_beginning_maintenance_whose_first_read_behind_back_soon_fails_stays_working_and_ends_with_every_part_stopped() -> None:
    hold(BACK_SOON_UP)
    asked = ask(f"/lanes/{PRODUCT}/{LANE}/maintenance/begin", {})[1]
    wait_until_stopped_at(BACK_SOON_UP)
    wait_for(lambda: shows_back_soon(WEB_ADDRESS), seconds=30)
    with request_loop(lambda: str(visit(WEB_ADDRESS, "GET", "/"))) as visitors:
        hold(LANE_READ)
        let_go(BACK_SOON_UP)

        def left_working_over_the_read() -> bool:
            now = ask(f"/requests/{asked['id']}")[1]
            assert now["stands"] == "working", now
            return "host.read failed" in (now["error"] or "")

        wait_for(left_working_over_the_read, seconds=30)
        time.sleep(LONGER_THAN_A_REQUEST_IS_LEFT_BEFORE_IT_IS_TAKEN_UP_AGAIN)
        kept = ask(f"/requests/{asked['id']}")[1]
        assert (kept["stands"], {each["ended"] for each in kept["attempts"]}) == ("working", {"unfinished"}), kept
        assert (len(kept["attempts"]) > 1, "running" in states_of_the_lane(), judged()["work"]) == (
            True,
            True,
            "beginning maintenance",
        )
        let_go(LANE_READ)
        ended = request_ended(asked["id"])
    assert (ended["stands"], ended["answer"]["outcome"]) == ("done", "in maintenance"), ended
    assert (states_of_the_lane(), judged()["condition"]) == ({"stopped"}, "in maintenance")
    assert (visitors.answers_in_order(), visitors.failures) == ([str(BACK_SOON)], [])
    status, back = ask_and_wait(f"/lanes/{PRODUCT}/{LANE}/maintenance/end", {})
    assert (status, back["outcome"]) == (200, "ended"), back


def second_router(name: str, container: str) -> str:
    router = {"rule": f"Host(`{WEB_ADDRESS}`)", "entryPoints": ["web"], "service": name}
    service = {"loadBalancer": {"servers": [{"url": instance_url(container)}]}}
    return json.dumps({"http": {"routers": {name: router}, "services": {name: service}}})


SECOND_ROUTERS = {
    "listed first, to the instance that serves": ("aaa-second", None),
    "listed last, to another container": ("zzz-second", "nobody-home"),
}


@pytest.mark.usefixtures("lane_on_a")
@pytest.mark.parametrize(("name", "target"), SECOND_ROUTERS.values(), ids=SECOND_ROUTERS.keys())
def test_two_routers_on_one_web_address_show_as_one_conflict_naming_both_and_a_move_changes_nothing_until_there_is_one(
    name: str, target: str | None
) -> None:
    [serving] = of_lane(running()["instances"])
    file = ROUTES_FOLDER / f"{name}.yaml"
    given = second_router(name, target or serving["container"])
    on_scratch(f"docker exec --interactive {PROXY_NAME} sh -c 'cat > {file}'", given=given)
    try:
        wait_for(lambda: judged()["condition"] == "conflicting", seconds=30)
        status, kept = ask_and_wait(f"/lanes/{PRODUCT}/{LANE}/move", {"build": BUILD_B}, timeout=25)
        shown = running()
        [conflict] = [each["sentence"] for each in judged()["findings"] if each["shown"] == "conflicting"]
        [web_address] = [each for each in shown["web_addresses"] if each["web_address"] == WEB_ADDRESS]
        [card] = [each for each in shown["cards"] if each["title"] == f"{PRODUCT} · {LANE}"]
        assert f"the router {name}@file to {target or serving['container']}" in conflict
        assert f"the router {WEB_ADDRESS}@file to {serving['container']}" in conflict
        assert (web_address["says"], web_address["error"], card["rows"][0]["note"]) == ("conflicting", conflict, conflict)
        assert (status, kept["stands"], kept["answer"], conflict in kept["error"]) == (202, "working", None, True), kept
        assert of_lane(shown["instances"]) == [serving | {"receives_visitors_for": None}]
    finally:
        on_scratch(f"docker exec {PROXY_NAME} rm -f {file}")
    ended = request_ended(kept["id"])
    assert (ended["stands"], ended["answer"]["outcome"], judged()["condition"]) == ("done", "switched", "good"), ended
