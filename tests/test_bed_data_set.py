import http.client
import io
import json
import socket
import tarfile
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from launchplane.secret_boundary import SECRET_REMOVED
from tests.bed import (
    SCRATCH_ADDRESS,
    UnexpectedAnswer,
    ask,
    move_lane,
    needs_test_bed,
    on_control,
    on_scratch,
    on_scratch_with_bytes,
    request_loop,
    retire,
    wait_for,
)
from tests.bed import kill_once as kill_the_call_once

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


CONNECT = "python -c 'import psycopg; psycopg.connect(\"connect_timeout=3\").close()'"
RUN_GIVEN_SQL = "python -c 'import sys, psycopg; psycopg.connect(\"connect_timeout=3\", autocommit=True).execute(sys.stdin.read())'"


def login_attempt(container: str, without: str | None = None, **given: str) -> tuple[bool, str]:
    changes = [*([f"-u {without}"] if without else []), *(f"{name}={value}" for name, value in given.items())]
    return in_container(container, f"env {' '.join(changes)} {CONNECT}")


def run_sql(container: str, statements: str) -> None:
    on_scratch(f"docker exec --interactive {container} {RUN_GIVEN_SQL}", given=statements)


def asked_of_server(database_container: str, query: str) -> list[str]:
    psql = f'psql --username=postgres --dbname=app --tuples-only --no-align --command="{query}"'
    return on_scratch(f"docker exec {database_container} {psql}").split()


def logins_on(data_set: dict[str, Any]) -> list[str]:
    members = "SELECT member::regrole FROM pg_auth_members WHERE roleid = 'app'::regrole"
    return asked_of_server(database_of(data_set), members)


def beats(database_container: str) -> list[str]:
    # noinspection SqlResolve
    return asked_of_server(database_container, "SELECT run FROM beats ORDER BY at")


def shown_by_launchplane() -> str:
    return json.dumps(read_running()) + on_control("journalctl --unit launchplane --no-pager --since '-1 hour'")


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


def kill_once(build: str, seen: Callable[[LaneView], bool]) -> None:
    kill_the_call_once(lambda: move_lane(PRODUCT, LANE, build), lambda: seen(lane_view()))


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


def test_only_an_instances_own_login_opens_its_database_from_its_network_and_nothing_does_from_outside(lane_on_a: LaneView) -> None:
    instance = lane_on_a.receiving()["container"]
    [background_service] = lane_on_a.background_services
    [data_set] = lane_on_a.data_sets
    own = environment_of(instance)
    address = on_scratch(
        "docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' " + database_of(data_set)
    ).strip()
    refused = {
        "no password": login_attempt(instance, without="PGPASSWORD"),
        "a wrong password": login_attempt(instance, PGPASSWORD="wrong"),
        "postgres with no password": login_attempt(instance, without="PGPASSWORD", PGUSER="postgres", PGDATABASE="postgres"),
        "postgres with the instance's password": login_attempt(instance, PGUSER="postgres"),
        "the instance's login on the postgres database": login_attempt(instance, PGDATABASE="postgres"),
        "the instance's login from outside the network": login_attempt(
            LISTENER, PGHOST=address, PGDATABASE="app", PGUSER=own["PGUSER"], PGPASSWORD=own["PGPASSWORD"]
        ),
    }
    why = {
        "no password": "no password supplied",
        "a wrong password": "password authentication failed",
        "postgres with no password": "pg_hba.conf rejects connection",
        "postgres with the instance's password": "pg_hba.conf rejects connection",
        "the instance's login on the postgres database": "pg_hba.conf rejects connection",
        "the instance's login from outside the network": "",
    }
    assert {what: (opened, why[what] in said) for what, (opened, said) in refused.items()} == dict.fromkeys(why, (False, True))
    assert login_attempt(instance)[0]
    assert login_attempt(background_service["container"])[0]
    assert (own["PGUSER"], own["PGDATABASE"]) == (logins_on(data_set)[0], "app")
    assert environment_of(background_service["container"])["PGUSER"] == own["PGUSER"]
    throwaway = environment_of(database_of(data_set))["POSTGRES_PASSWORD"]
    assert [secret in shown_by_launchplane() for secret in (own["PGPASSWORD"], throwaway)] == [False, False]


def test_after_a_release_the_retired_instances_login_is_gone_and_its_password_opens_nothing(lane_on_a: LaneView) -> None:
    old = environment_of(lane_on_a.receiving()["container"])
    move(BUILD_B)
    view = lane_view()
    instance = view.receiving()["container"]
    new = environment_of(instance)
    opened, said = login_attempt(instance, PGUSER=old["PGUSER"], PGPASSWORD=old["PGPASSWORD"])
    assert (opened, "pg_hba.conf rejects connection" in said) == (False, True), said
    assert login_attempt(instance)[0]
    assert logins_on(view.data_sets[0]) == [new["PGUSER"]] != [old["PGUSER"]]
    assert new["PGPASSWORD"] != old["PGPASSWORD"]


def write_many(container: str) -> None:
    # noinspection SqlResolve
    run_sql(container, "INSERT INTO made_by_new (id) SELECT generate_series(1, 200)")


def test_two_instances_write_at_once_each_changes_what_the_other_made_and_all_of_it_belongs_to_app(lane_on_a: LaneView) -> None:
    kill_once(BUILD_B, RELEASE_POINTS["the new instance exists"])
    view = lane_view()
    old = view.receiving()["container"]
    [new] = [instance["container"] for instance in view.instances if instance["container"] != old]
    database = database_of(view.data_sets[0])
    assert sorted(logins_on(view.data_sets[0])) == sorted(environment_of(each)["PGUSER"] for each in (old, new))
    run_sql(old, "CREATE TABLE made_by_old (id int)")
    # noinspection SqlResolve
    run_sql(new, "CREATE TABLE made_by_new (id int); ALTER TABLE made_by_old ADD COLUMN note text")
    # noinspection SqlResolve
    run_sql(old, "ALTER TABLE made_by_new ADD COLUMN note text")
    with ThreadPoolExecutor(max_workers=2) as pool:
        for written in [pool.submit(write_many, old), pool.submit(write_many, new)]:
            written.result()
    not_of_app = (
        "SELECT relname FROM pg_class JOIN pg_namespace ON pg_namespace.oid = relnamespace "
        "WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' AND relowner <> 'app'::regrole "
        "UNION ALL SELECT datname FROM pg_database WHERE datname = 'app' AND datdba <> 'app'::regrole"
    )
    # noinspection SqlResolve
    assert asked_of_server(database, "SELECT count(*) FROM made_by_new") == ["400"]
    assert asked_of_server(database, not_of_app) == []
    # noinspection SqlResolve
    run_sql(new, "DROP TABLE made_by_old; DROP TABLE made_by_new")
    move(BUILD_B)
    finished = assert_lane_finished_on(BUILD_B)
    assert logins_on(finished.data_sets[0]) == [environment_of(new)["PGUSER"]]


def files_holding(volume: str, text: str) -> list[str]:
    search = f"docker run --rm --user 0 --volume {volume}:/data:ro postgres:18-alpine grep -rlF -e {text} /data"
    *files, ended = on_scratch(f"{search}; echo exit=$?").split()
    assert ended in ("exit=0", "exit=1"), f"the search could not read every file: {ended}"
    return files


def test_no_password_is_anywhere_in_the_data_sets_volume(lane_on_a: LaneView) -> None:
    write_row("written before the volume is searched")
    [data_set] = lane_on_a.data_sets
    [volume] = data_set["volumes"]
    passwords = [
        environment_of(lane_on_a.receiving()["container"])["PGPASSWORD"],
        environment_of(database_of(data_set))["POSTGRES_PASSWORD"],
    ]
    status, body = ask(f"/lanes/{PRODUCT}/{LANE}/maintenance/begin", {})
    assert (status, body["outcome"]) == (200, "in maintenance"), body
    assert [files_holding(volume, password) for password in passwords] == [[], []]
    assert files_holding(volume, "SCRAM-SHA-256") != []


def test_starts_that_fail_after_their_login_was_made_never_leave_more_than_the_last_one_and_a_good_move_removes_it(
    lane_on_a: LaneView,
) -> None:
    [data_set] = lane_on_a.data_sets
    own = environment_of(lane_on_a.receiving()["container"])["PGUSER"]
    refusals = [move_lane(PRODUCT, LANE, "launchplane-test-ledger:no-such-build")[0] for _ in range(3)]
    left = logins_on(data_set)
    move(BUILD_A)
    assert (refusals, len(left), own in left, logins_on(data_set)) == ([503, 503, 503], 2, True, [own])


SESSION_HOLDER = "test-bed-session-holder"
HOLD_A_SESSION = """
import time, psycopg
session = psycopg.connect("connect_timeout=3", autocommit=True)
session.execute("SET ROLE NONE; CREATE TABLE owned_by_the_login (id int)")
print("holding", flush=True)
try:
    while True:
        session.execute("SELECT 1")
        time.sleep(0.2)
except psycopg.Error as error:
    print("session ended:", error, flush=True)
"""


def session_holder_log() -> str:
    return on_scratch(f"docker exec {SESSION_HOLDER} cat /hold.log")


def hold_a_session(network: str, login: dict[str, str]) -> None:
    given = " ".join(f"--env {name}={value}" for name, value in login.items() if name.startswith("PG"))
    on_scratch(f"docker run --detach --name {SESSION_HOLDER} --network {network} {given} {BUILD_A} sleep 600")
    on_scratch(f"docker exec --interactive {SESSION_HOLDER} sh -c 'cat > /hold.py'", given=HOLD_A_SESSION)
    on_scratch(f"docker exec --detach {SESSION_HOLDER} sh -c 'python /hold.py > /hold.log 2>&1'")
    wait_for(lambda: "holding" in session_holder_log(), seconds=15)


def test_retiring_an_instance_ends_a_session_held_as_its_login_and_keeps_what_the_login_owned_for_app(lane_on_a: LaneView) -> None:
    [data_set] = lane_on_a.data_sets
    database = database_of(data_set)
    login = environment_of(lane_on_a.receiving()["container"])
    # noinspection SqlResolve
    owner = "SELECT tableowner FROM pg_tables WHERE tablename = 'owned_by_the_login'"
    try:
        hold_a_session(data_set["networks"][0]["name"], login)
        owned_by = asked_of_server(database, owner)
        status, body = retire(PRODUCT, LANE)
        assert status == 200, body
        wait_for(lambda: "session ended" in session_holder_log(), seconds=10)
        assert (owned_by, asked_of_server(database, owner), logins_on(data_set)) == ([login["PGUSER"]], ["app"], [])
    finally:
        on_scratch(f"docker rm --force {SESSION_HOLDER}")


def test_a_login_no_container_holds_shows_as_left_unfinished_and_the_next_move_and_the_next_retire_remove_it(
    lane_on_a: LaneView,
) -> None:
    [data_set] = lane_on_a.data_sets
    database = database_of(data_set)
    own = environment_of(lane_on_a.receiving()["container"])["PGUSER"]
    asked_of_server(database, "CREATE ROLE i_left_by_a_kill LOGIN IN ROLE app")
    left = json.loads(verdict_now())
    moved = move(BUILD_A)
    after_the_move = logins_on(data_set)
    asked_of_server(database, "CREATE ROLE i_left_again LOGIN IN ROLE app")
    status, body = retire(PRODUCT, LANE)
    assert any("has the login i_left_by_a_kill, which no container holds" in state for state in left["unfinished"]), left
    assert (moved["outcome"], after_the_move) == ("cleaned up", [own])
    assert (status, logins_on(data_set)) == (200, []), body


@pytest.mark.usefixtures("fresh_lane")
def test_a_data_set_whose_access_rules_are_not_launchplanes_gets_no_instance_and_is_not_deleted() -> None:
    move(BUILD_A)
    write_row("kept")
    retire(PRODUCT, LANE)
    [data_set] = lane_view().data_sets
    on_scratch(f'docker exec {database_of(data_set)} sh -c \'echo "host all all all trust" >> "$PGDATA/pg_hba.conf"\'')
    status, body = move_lane(PRODUCT, LANE, BUILD_A)
    assert status == 503, body
    assert f"of the data set {data_set['data_set']} does not have Launchplane's access rules" in body["error"]
    view = lane_view()
    assert (view.instances, [found["data_set"] for found in view.data_sets]) == ([], [data_set["data_set"]])
    assert logins_on(data_set) == []


def test_a_database_server_removed_by_hand_is_made_again_on_its_data_and_the_instance_still_logs_in(lane_on_a: LaneView) -> None:
    write_row("before the server was removed")
    [data_set] = lane_on_a.data_sets
    on_scratch(f"docker rm --force {database_of(data_set)}")
    body = move(BUILD_A)
    view = assert_lane_finished_on(BUILD_A)
    assert body["data_set"] == data_set["data_set"]
    assert view.receiving()["container"] == lane_on_a.receiving()["container"]
    wait_for(lambda: request(LANE, "GET", "/rows")[0] == 200, seconds=15)
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
        wait_for(lambda: "The logins of the database test-bed-impostor could not be read" in verdict_now(), seconds=30)
    finally:
        on_scratch("docker rm --force test-bed-impostor; docker volume rm test-bed-another-volume")


def test_a_running_part_attached_where_it_must_not_be_is_shown_as_wrong_and_a_move_stops_in_the_same_words(
    lane_on_a: LaneView,
) -> None:
    database = database_of(lane_on_a.data_sets[0])
    on_scratch(f"docker network connect bridge {database}")
    [verdict] = [lane for lane in read_running()["lanes"] if (lane["product"], lane["lane"]) == (PRODUCT, LANE)]
    status, body = move_lane(PRODUCT, LANE, BUILD_A)
    assert (verdict["condition"], verdict["parts"][database], len(verdict["problems"])) == ("wrong", "wrong", 2)
    assert (status, [problem in body["error"] for problem in verdict["problems"]]) == (503, [True, True])


def test_a_check_whose_failure_carries_the_password_is_answered_without_it(lane_on_a: LaneView) -> None:
    status, body = move_lane(PRODUCT, LANE, BUILD_TELLTALE)
    assert (status, body["outcome"]) == (422, "check failed"), body
    assert f"my password is {SECRET_REMOVED}\n" in body["error"]
    assert "my password is" not in shown_by_launchplane().replace(f"my password is {SECRET_REMOVED}", "")
    assert len(logins_on(lane_on_a.data_sets[0])) == 1


def test_a_background_service_that_prints_its_password_and_fails_has_its_log_returned_without_the_password() -> None:
    try:
        status, body = move_lane(LEAKY_PRODUCT, LANE, BUILD_A)
        [data_set] = [found for found in read_running()["data_sets"] if found["product"] == LEAKY_PRODUCT]
        [instance] = [found for found in read_running()["instances"] if found["product"] == LEAKY_PRODUCT]
        password = environment_of(instance["container"])["PGPASSWORD"]
        assert data_set["data_set"] == instance["data_set"]
        assert (status, body["outcome"]) == (422, "background service failed"), body
        assert f"connecting with password {SECRET_REMOVED}" in body["error"]
        assert password not in json.dumps(body) + shown_by_launchplane()
    finally:
        for found in [each for each in read_running()["data_sets"] if each["product"] == LEAKY_PRODUCT]:
            retire(LEAKY_PRODUCT, LANE, found["data_set"])
        retire(LEAKY_PRODUCT, LANE)
