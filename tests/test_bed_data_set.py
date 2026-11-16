import http.client
import io
import json
import socket
import tarfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from launchplane.secret_boundary import SECRET_REMOVED
from tests.bed import (
    SCRATCH_ADDRESS,
    UnexpectedAnswer,
    ask,
    kill_launchplane,
    move_lane,
    needs_test_bed,
    on_control,
    on_scratch,
    on_scratch_with_bytes,
    request_loop,
    retire,
    wait_for,
)

pytestmark = needs_test_bed

PRODUCT = "test-bed-ledger"
LANE = "testing"
OTHER_LANE = "other"
BUILD_A = "launchplane-test-ledger:a"
BUILD_B = "launchplane-test-ledger:b"
BUILD_TELLTALE = "launchplane-test-ledger:telltale"
PRETEND_PRODUCT = Path(__file__).parent / "pretend_product"
DESCRIPTION = {
    "health_path": "/health",
    "database": {"image": "postgres:18-alpine"},
    "background_service": {"command": ["python", "/app.py", "background-service"]},
}
LEAKY_PRODUCT = "test-bed-leaky"
LEAKY_DESCRIPTION = DESCRIPTION | {
    "background_service": {"command": ["sh", "-c", "echo connecting with password $PGPASSWORD; exit 1"]}
}
LISTENER = "test-bed-call-listener"
LISTENER_PORT = 8099
CONTROL_HOST_NAME = "launchplane.shiny"
OUTSIDE_ADDRESS = "1.1.1.1"


def web_address_of(lane: str) -> str:
    return f"{lane}.{PRODUCT}.test"


def scratch_ip() -> str:
    return socket.gethostbyname(SCRATCH_ADDRESS)


def call_out_url() -> str:
    # noinspection HttpUrlsUsage
    return f"http://{scratch_ip()}:{LISTENER_PORT}/"


def pretend_product_archive() -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name in ("Dockerfile", "app.py"):
            tar.add(PRETEND_PRODUCT / name, arcname=name)
    return archive.getvalue()


def build_pretend_product(tag: str, version: str) -> None:
    arguments = f"--build-arg VERSION={version} --build-arg CALL_OUT_URL={call_out_url()}"
    on_scratch_with_bytes(f"docker build --quiet --tag {tag} {arguments} -", pretend_product_archive())


def start_listener() -> None:
    on_scratch(f"docker rm --force {LISTENER} >/dev/null 2>&1 || true")
    on_scratch(f"docker run --detach --name {LISTENER} --publish {LISTENER_PORT}:8000 {BUILD_A} python -u -m http.server 8000")


def calls_heard() -> str:
    return on_scratch(f"docker logs {LISTENER} 2>&1")


def place_description(product: str, description: dict[str, Any]) -> None:
    file = f"/var/lib/launchplane/products/{product}.json"
    on_control(
        f"install -d -o launchplane /var/lib/launchplane/products && cat > {file} && chmod 644 {file}",
        given=json.dumps(description),
    )


def read_running() -> dict[str, Any]:
    status, body = ask("/running")
    assert status == 200, body
    return body


@dataclass(frozen=True)
class LaneView:
    instances: list[dict[str, Any]]
    background_services: list[dict[str, Any]]
    data_sets: list[dict[str, Any]]

    def receiving(self) -> dict[str, Any]:
        [instance] = [instance for instance in self.instances if instance["receives_visitors_for"]]
        return instance


def lane_view(lane: str = LANE) -> LaneView:
    running = read_running()

    def of_lane(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [part for part in parts if (part["product"], part["lane"]) == (PRODUCT, lane)]

    return LaneView(of_lane(running["instances"]), of_lane(running["background_services"]), of_lane(running["data_sets"]))


def retire_lane_and_its_data(lane: str) -> None:
    for data_set in lane_view(lane).data_sets:
        status, body = retire(PRODUCT, lane, data_set["data_set"])
        assert status == 200, body
    status, body = retire(PRODUCT, lane)
    assert status == 200, body


def move(build: str, lane: str = LANE) -> dict[str, Any]:
    status, body = move_lane(PRODUCT, lane, build)
    assert status == 200, body
    return body


def request(lane: str, method: str, path: str, body: str | None = None) -> tuple[int, str]:
    connection = http.client.HTTPConnection(SCRATCH_ADDRESS, 80, timeout=5)
    try:
        connection.request(method, path, body=body, headers={"Host": web_address_of(lane)})
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


def write_row(text: str) -> None:
    assert request(LANE, "POST", "/rows", text) == (201, "written\n")


def rows_read() -> list[str]:
    status, body = request(LANE, "GET", "/rows")
    assert status == 200, body
    return [row["body"] for row in json.loads(body)]


def in_container(container: str, command: str) -> tuple[bool, str]:
    output = on_scratch(f"docker exec {container} {command} 2>&1; echo exit=$?")
    return output.rstrip().endswith("exit=0"), output


def reaches(container: str, host: str, port: int) -> bool:
    return in_container(container, f"python /app.py reach {host} {port}")[0]


def database_of(data_set: dict[str, Any]) -> str:
    [database] = data_set["databases"]
    return database["container"]


def environment_of(container: str) -> dict[str, str]:
    lines = on_scratch(f"docker inspect --format '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' {container}").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def password_kept_with(data_set: dict[str, Any]) -> str:
    return on_scratch(f"docker exec {database_of(data_set)} cat /var/lib/postgresql/launchplane-password")


def beats(database_container: str) -> list[str]:
    query = "psql --username=postgres --tuples-only --no-align --command='SELECT run FROM beats ORDER BY at'"
    return on_scratch(f"docker exec {database_container} {query}").split()


def runs_in_order(runs: list[str]) -> list[str]:
    return [run for index, run in enumerate(runs) if index == 0 or runs[index - 1] != run]


def version_answering() -> str:
    status, body = request(LANE, "GET", "/")
    if status != 200:
        raise UnexpectedAnswer(f"status {status}: {body}")
    return body.split()[-1]


@pytest.fixture(scope="module", autouse=True)
def pretend_product() -> Iterator[None]:
    build_pretend_product(BUILD_A, "a")
    build_pretend_product(BUILD_B, "b")
    build_pretend_product(BUILD_TELLTALE, "telltale")
    start_listener()
    place_description(PRODUCT, DESCRIPTION)
    place_description(LEAKY_PRODUCT, LEAKY_DESCRIPTION)
    yield
    for lane in (LANE, OTHER_LANE):
        retire_lane_and_its_data(lane)
    on_scratch(f"docker rm --force {LISTENER}")


@pytest.fixture
def fresh_lane() -> Iterator[None]:
    retire_lane_and_its_data(LANE)
    yield
    retire_lane_and_its_data(LANE)


@pytest.fixture
def lane_on_a() -> Iterator[LaneView]:
    retire_lane_and_its_data(LANE)
    move(BUILD_A)
    yield lane_view()
    retire_lane_and_its_data(LANE)


@pytest.mark.usefixtures("fresh_lane")
def test_a_lane_brought_up_with_a_fresh_data_set_shows_it_and_keeps_rows() -> None:
    body = move(BUILD_A)
    view = lane_view()
    [data_set] = view.data_sets
    assert (body["outcome"], body["data_set"]) == ("switched", data_set["data_set"])
    assert [(network["is_quiet"], len(data_set["volumes"])) for network in data_set["networks"]] == [(True, 1)]
    assert [database["state"] for database in data_set["databases"]] == ["running"]
    assert view.receiving()["data_set"] == data_set["data_set"]
    assert [(each["instance"], each["state"]) for each in view.background_services] == [(view.receiving()["container"], "running")]
    write_row("first")
    write_row("second")
    assert rows_read() == ["first", "second"]


def test_a_release_on_the_same_data_set_fails_no_request_and_keeps_the_rows(lane_on_a: LaneView) -> None:
    write_row("before the release")
    with request_loop(version_answering) as loop:
        body = move(BUILD_B)
    view = lane_view()
    assert (body["outcome"], body["data_set"]) == ("switched", lane_on_a.data_sets[0]["data_set"])
    assert loop.failures == []
    assert loop.answers_in_order() == ["a", "b"]
    assert [instance["build"] for instance in view.instances] == [BUILD_B]
    assert [data_set["data_set"] for data_set in view.data_sets] == [lane_on_a.data_sets[0]["data_set"]]
    assert rows_read() == ["before the release"]


def verdict_now() -> str:
    [verdict] = [lane for lane in read_running()["lanes"] if (lane["product"], lane["lane"]) == (PRODUCT, LANE)]
    kinds = {"wrong": "problems", "release": "release_under_way", "unfinished": "left_unfinished"}
    return json.dumps({kind: verdict[field] for kind, field in kinds.items()} | {"moving": verdict["a_move_is_at_work"]})


@pytest.mark.usefixtures("lane_on_a")
def test_a_good_release_is_shown_as_a_release_under_way_and_never_as_something_wrong() -> None:
    with request_loop(verdict_now) as loop:
        move(BUILD_B)
    verdicts = [json.loads(verdict) for verdict in loop.answers_in_order()]
    assert loop.failures == []
    at_rest: dict[str, Any] = {"wrong": [], "release": [], "unfinished": [], "moving": False}
    assert [verdict for verdict in verdicts if verdict["wrong"] or verdict["unfinished"]] == []
    assert any("runs beside the active one" in state for verdict in verdicts for state in verdict["release"])
    assert (verdicts[0], verdicts[-1]) == (at_rest, at_rest)


def test_a_release_killed_halfway_shows_at_once_as_left_unfinished_not_as_under_way(lane_on_a: LaneView) -> None:
    kill_once(BUILD_B, RELEASE_POINTS["the new instance exists"])
    left = json.loads(verdict_now())
    assert (left["moving"], left["release"], left["wrong"]) == (False, [], [])
    assert any("runs beside the active one" in state for state in left["unfinished"])
    move(BUILD_B)
    assert json.loads(verdict_now()) == {"wrong": [], "release": [], "unfinished": [], "moving": False}
    assert lane_view().data_sets == lane_on_a.data_sets


def test_data_left_without_its_password_is_reported_by_name_and_given_no_new_password(lane_on_a: LaneView) -> None:
    [data_set] = lane_on_a.data_sets
    on_scratch(f"docker exec {database_of(data_set)} rm /var/lib/postgresql/launchplane-password")
    on_scratch(f"docker rm --force {database_of(data_set)}")
    status, body = move_lane(PRODUCT, LANE, BUILD_A)
    assert status == 503, body
    assert f"The data set {data_set['data_set']} has data and no password" in body["error"]
    [remade] = lane_view().data_sets[0]["databases"]
    kept = on_scratch(f"docker cp {remade['container']}:/var/lib/postgresql/launchplane-password - 2>&1 || true")
    assert (remade["state"], "launchplane-password" in kept and "Could not find" not in kept) == ("stopped", False)


def test_during_a_release_the_background_service_never_runs_twice_at_once(lane_on_a: LaneView) -> None:
    move(BUILD_B)
    time.sleep(2)
    runs = beats(database_of(lane_on_a.data_sets[0]))
    assert len(runs_in_order(runs)) == len(set(runs)) == 2, runs_in_order(runs)


def test_a_quiet_instance_reaches_nothing_outside_until_given_a_way_out(lane_on_a: LaneView) -> None:
    instance = lane_on_a.receiving()["container"]
    [background_service] = lane_on_a.background_services
    attempts = {
        "the internet": (OUTSIDE_ADDRESS, 80),
        "the local network": (socket.gethostbyname(CONTROL_HOST_NAME), 8700),
        "the Docker host": (scratch_ip(), 2376),
        "the listener on the Docker host": (scratch_ip(), LISTENER_PORT),
    }
    assert {what: reaches(instance, *where) for what, where in attempts.items()} == dict.fromkeys(attempts, False)
    assert {what: reaches(LISTENER, *where) for what, where in attempts.items()} == dict.fromkeys(attempts, True)
    time.sleep(3)
    assert "run=" not in calls_heard()
    on_scratch(f"docker network connect bridge {background_service['container']}")
    wait_for(lambda: "run=" in calls_heard(), seconds=15)


def test_an_instance_cannot_reach_another_data_sets_database(lane_on_a: LaneView) -> None:
    move(BUILD_A, OTHER_LANE)
    other = lane_view(OTHER_LANE).data_sets[0]
    other_address = on_scratch(
        "docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' " + database_of(other)
    ).strip()
    instance = lane_on_a.receiving()["container"]
    assert reaches(instance, "database", 5432)
    assert not reaches(instance, other_address, 5432)
    assert not reaches(instance, database_of(other), 5432)
    retire_lane_and_its_data(OTHER_LANE)


@pytest.mark.usefixtures("fresh_lane")
def test_retiring_a_lane_keeps_its_data_set_until_it_is_named() -> None:
    move(BUILD_A)
    write_row("kept")
    [data_set] = lane_view().data_sets
    status, body = retire(PRODUCT, LANE)
    assert (status, body["deleted_data_set"], body["kept_data_sets"]) == (200, None, [data_set["data_set"]])
    assert (lane_view().instances, lane_view().background_services) == ([], [])
    move(BUILD_B)
    assert rows_read() == ["kept"]
    retire(PRODUCT, LANE)
    status, body = retire(PRODUCT, LANE, data_set["data_set"])
    assert (status, body["outcome"], body["deleted_data_set"], body["kept_data_sets"]) == (200, "deleted", data_set["data_set"], [])
    assert lane_view().data_sets == []
    assert data_set["data_set"] not in on_scratch("docker network ls --format '{{.Name}}'; docker volume ls --format '{{.Name}}'")


def move_cut_short_by_the_kill(build: str) -> None:
    with suppress(OSError):
        move_lane(PRODUCT, LANE, build)


def kill_once(build: str, seen: Callable[[LaneView], bool]) -> None:
    mover = threading.Thread(target=move_cut_short_by_the_kill, args=(build,))
    mover.start()
    wait_for(lambda: seen(lane_view()), seconds=90)
    kill_launchplane()
    mover.join()


def has_background_service_of(build: str) -> Callable[[LaneView], bool]:
    return lambda view: any(each["build"] == build for each in view.background_services)


def receives_visitors(build: str) -> Callable[[LaneView], bool]:
    return lambda view: any(instance["build"] == build and instance["receives_visitors_for"] for instance in view.instances)


BRING_UP_POINTS: dict[str, Callable[[LaneView], bool]] = {
    "the data set exists": lambda view: bool(view.data_sets),
    "its database runs": lambda view: any(
        database["state"] == "running" for data_set in view.data_sets for database in data_set["databases"]
    ),
    "the instance exists": lambda view: bool(view.instances),
    "the instance receives visitors": receives_visitors(BUILD_A),
}
RELEASE_POINTS: dict[str, Callable[[LaneView], bool]] = {
    "the new instance exists": lambda view: any(instance["build"] == BUILD_B for instance in view.instances),
    "the new instance receives visitors": receives_visitors(BUILD_B),
    "the old background service is gone": lambda view: not has_background_service_of(BUILD_A)(view),
    "the new background service exists": has_background_service_of(BUILD_B),
}


def assert_lane_finished_on(build: str) -> LaneView:
    view = lane_view()
    assert [(instance["build"], instance["state"]) for instance in view.instances] == [(build, "running")]
    assert [(each["instance"], each["state"]) for each in view.background_services] == [(view.receiving()["container"], "running")]
    assert len(view.data_sets) == 1
    return view


@pytest.mark.usefixtures("fresh_lane")
@pytest.mark.parametrize("point", BRING_UP_POINTS.values(), ids=BRING_UP_POINTS.keys())
def test_a_bring_up_killed_at_this_point_reaches_the_same_end_when_asked_again(point: Callable[[LaneView], bool]) -> None:
    kill_once(BUILD_A, point)
    move(BUILD_A)
    assert_lane_finished_on(BUILD_A)
    write_row("after the kill")
    assert rows_read() == ["after the kill"]


@pytest.mark.parametrize("point", RELEASE_POINTS.values(), ids=RELEASE_POINTS.keys())
def test_a_release_killed_at_this_point_reaches_the_same_end_on_the_same_data_when_asked_again(
    lane_on_a: LaneView, point: Callable[[LaneView], bool]
) -> None:
    write_row("before the release")
    kill_once(BUILD_B, point)
    move(BUILD_B)
    view = assert_lane_finished_on(BUILD_B)
    assert view.data_sets == lane_on_a.data_sets
    assert rows_read() == ["before the release"]
    runs = beats(database_of(view.data_sets[0]))
    assert len(runs_in_order(runs)) == len(set(runs)), runs_in_order(runs)


def test_each_data_set_has_its_own_password_given_to_its_instance_and_never_shown(lane_on_a: LaneView) -> None:
    move(BUILD_A, OTHER_LANE)
    other = lane_view(OTHER_LANE)
    passwords = [password_kept_with(view.data_sets[0]) for view in (lane_on_a, other)]
    instance = lane_on_a.receiving()["container"]
    [background_service] = lane_on_a.background_services
    assert passwords[0] != passwords[1]
    assert "PASSWORD=" not in on_scratch(f"docker inspect --format '{{{{.Config.Env}}}}' {database_of(lane_on_a.data_sets[0])}")
    assert environment_of(instance)["PGPASSWORD"] == environment_of(background_service["container"])["PGPASSWORD"] == passwords[0]
    assert not in_container(instance, "env PGPASSWORD=wrong python -c 'import psycopg; psycopg.connect(\"connect_timeout=3\")'")[0]
    shown = json.dumps(read_running()) + on_control("journalctl --unit launchplane --no-pager --since '-1 hour'")
    assert [password in shown for password in passwords] == [False, False]
    retire_lane_and_its_data(OTHER_LANE)


def test_a_database_server_removed_by_hand_is_made_again_with_the_same_password(lane_on_a: LaneView) -> None:
    write_row("before the server was removed")
    [data_set] = lane_on_a.data_sets
    password = password_kept_with(data_set)
    on_scratch(f"docker rm --force {database_of(data_set)}")
    body = move(BUILD_A)
    view = assert_lane_finished_on(BUILD_A)
    assert (body["data_set"], password_kept_with(view.data_sets[0])) == (data_set["data_set"], password)
    assert view.receiving()["container"] == lane_on_a.receiving()["container"]
    assert rows_read() == ["before the server was removed"]


def test_a_database_with_another_volume_mounted_is_refused_and_shown_so(lane_on_a: LaneView) -> None:
    [data_set] = lane_on_a.data_sets
    [network] = data_set["networks"]
    labels = " ".join(
        f"--label {label}"
        for label in (
            f"launchplane.product={PRODUCT}",
            f"launchplane.lane={LANE}",
            f"launchplane.data-set={data_set['data_set']}",
            "launchplane.part=database",
        )
    )
    on_scratch(f"docker rm --force {database_of(data_set)}")
    on_scratch(
        f"docker run --detach --name test-bed-impostor {labels} --network {network['name']} --network-alias database "
        "--env POSTGRES_PASSWORD=impostor --volume test-bed-another-volume:/var/lib/postgresql postgres:18-alpine"
    )
    try:
        status, body = move_lane(PRODUCT, LANE, BUILD_A)
        [verdict] = [lane for lane in read_running()["lanes"] if (lane["product"], lane["lane"]) == (PRODUCT, LANE)]
        assert status == 503, body
        assert "has test-bed-another-volume mounted at /var/lib/postgresql, which it must not have" in body["error"]
        assert any("has test-bed-another-volume mounted" in problem for problem in verdict["problems"])
    finally:
        on_scratch("docker rm --force test-bed-impostor; docker volume rm test-bed-another-volume")


def test_a_check_whose_failure_carries_the_password_is_answered_without_it(lane_on_a: LaneView) -> None:
    password = password_kept_with(lane_on_a.data_sets[0])
    status, body = move_lane(PRODUCT, LANE, BUILD_TELLTALE)
    assert (status, body["outcome"]) == (422, "check failed"), body
    assert f"my password is {SECRET_REMOVED}" in body["error"]
    journal = on_control("journalctl --unit launchplane --no-pager --since '-10 min'")
    assert password not in json.dumps(body) + json.dumps(read_running()) + journal


def test_a_background_service_that_prints_its_password_and_fails_has_its_log_returned_without_the_password() -> None:
    try:
        status, body = move_lane(LEAKY_PRODUCT, LANE, BUILD_A)
        [data_set] = [found for found in read_running()["data_sets"] if found["product"] == LEAKY_PRODUCT]
        password = password_kept_with(data_set)
        assert (status, body["outcome"]) == (422, "background service failed"), body
        assert f"connecting with password {SECRET_REMOVED}" in body["error"]
        journal = on_control("journalctl --unit launchplane --no-pager --since '-10 min'")
        shown = json.dumps(body) + json.dumps(read_running()) + journal
        assert password not in shown
    finally:
        for found in [each for each in read_running()["data_sets"] if each["product"] == LEAKY_PRODUCT]:
            retire(LEAKY_PRODUCT, LANE, found["data_set"])
        retire(LEAKY_PRODUCT, LANE)
