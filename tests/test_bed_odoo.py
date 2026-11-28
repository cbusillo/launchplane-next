import hashlib
import shlex
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from launchplane.odoo import FILES_PATH, USER
from launchplane.product import Product
from tests.bed import (
    ask,
    ask_and_wait,
    hold,
    kill_held_launchplane,
    launchplane_held,
    let_go,
    needs_test_bed,
    on_control,
    on_scratch,
    remove_product_copies,
    request_ended,
    retire,
    wait_for,
    wait_until_stopped_at,
)
from tests.bed_odoo import (
    SignedIn,
    data_set_of,
    describe,
    pull_builds,
    retire_with_its_data,
    seed,
    serve_recorded_git_host,
    stop_recorded_git_host,
)
from tests.odoo_product import ATTACHMENT_SHA256, first_start_product
from tests.recorded_git_host import LATER_RUN, RUN, build_of

pytestmark = needs_test_bed

PRODUCT, LANE = "test-bed-tk", "testing"
WEB_ADDRESS = f"{LANE}.{PRODUCT}.test"
OUTSIDE_ADDRESS = "1.1.1.1"
NO_BYTES = hashlib.sha256(b"").hexdigest()
# noinspection SqlResolve
JOBS_RUN = "select count(*) from ir_cron where lastcall is not null"
# noinspection SqlResolve
STORED_FILE = "select store_fname from ir_attachment where name = 'launchplane-check'"


def move_to(run: int) -> dict[str, Any]:
    return ask_and_wait(f"/lanes/{PRODUCT}/{LANE}/move", {"run": run})[1]


def clear_the_lane() -> None:
    kept = [each["data_set"] for each in ask("/running")[1]["data_sets"] if each["product"] == PRODUCT]
    for data_set in kept or [None]:
        status, body = retire(PRODUCT, LANE, data_set)
        assert status == 200, body
    remove_product_copies(PRODUCT)


def site_answer() -> str:
    return on_scratch(f"curl --silent --show-error --max-time 20 --header 'Host: {WEB_ADDRESS}' http://127.0.0.1/")


def in_the_lanes_database(statement: str) -> str:
    database = f"{PRODUCT}-{LANE}-database-{data_set_of(PRODUCT, LANE)}"
    return on_scratch(
        f'docker exec {database} psql --username=postgres --dbname=app --tuples-only --no-align --command="{statement}"'
    ).strip()


# One lane of TK from nothing to serving, with each answer kept: a lane's seed alone takes most of a minute.
@dataclass(frozen=True)
class Story:
    never_installed: dict[str, Any]
    base_alone: dict[str, Any]
    seeded: dict[str, Any]
    again: dict[str, Any]
    serving: str


@pytest.fixture(scope="module")
def story() -> Iterator[Story]:
    try:
        pull_builds()
    except subprocess.CalledProcessError as error:
        pytest.skip(f"the scratch host does not have TK's builds and could not pull them: {error.stderr}")
    serve_recorded_git_host()
    describe(PRODUCT)
    clear_the_lane()
    try:
        never_installed = move_to(RUN)
        seed(PRODUCT, LANE, ["base"], attachment=False)
        base_alone = move_to(RUN)
        seed(PRODUCT, LANE)
        seeded = move_to(RUN)
        again = move_to(RUN)
        yield Story(never_installed, base_alone, seeded, again, seeded["container_receiving_visitors"] or "")
    finally:
        clear_the_lane()
        stop_recorded_git_host()


def test_a_lane_odoo_was_never_installed_in_fails_its_check_with_odoos_words_and_keeps_the_data_set_it_made(story: Story) -> None:
    answer = story.never_installed
    assert (answer["outcome"], answer["container_receiving_visitors"]) == ("check failed", None)
    assert "Database app not initialized" in answer["error"]
    data_set = data_set_of(PRODUCT, LANE)
    made = ("network", "database volume", "files volume", "database server")
    assert answer["did"][:4] == [f"made the {part} of the data set {data_set}" for part in made]


def test_a_database_without_the_declared_modules_fails_the_check_of_the_kind_though_the_login_page_answers(story: Story) -> None:
    answer = story.base_alone
    assert (answer["outcome"], answer["container_receiving_visitors"]) == ("check failed", None)
    assert "answers the proxy, and the check of its kind, run in it, failed" in answer["error"]
    assert "does not have these modules installed" in answer["error"]
    assert "tk_website" in answer["error"]


def test_a_move_to_a_verified_build_serves_that_digest_as_the_kind_runs_it_on_the_lanes_own_files(story: Story) -> None:
    assert (story.seeded["outcome"], story.seeded["build"]) == ("switched", build_of(RUN))
    assert story.seeded["did"][-3:] == [f"make {story.serving}", f"start {story.serving}", f"wrote the route to {story.serving}"]
    form = "{{.Image}} {{.Config.User}} {{range .Mounts}}{{.Name}}:{{.Destination}}{{end}}"
    image, user, mounted = on_scratch(f"docker inspect --format '{form}' {story.serving}").split()
    on_host = on_scratch(f"docker image inspect --format '{{{{.Id}}}}' {build_of(RUN)}").strip()
    assert (image, user, mounted) == (on_host, USER, f"launchplane-data-{data_set_of(PRODUCT, LANE)}-files:{FILES_PATH}")
    assert "Coastal Towne Realty" in site_answer()


def test_a_second_move_to_the_same_build_changes_nothing(story: Story) -> None:
    assert (story.again["outcome"], story.again["did"], story.again["container_receiving_visitors"]) == (
        "unchanged",
        [],
        story.serving,
    )


def test_the_lane_shows_good_with_no_finding_and_its_instance_is_read_as_its_build(story: Story) -> None:
    running = ask("/running")[1]
    [lane] = [each for each in running["lanes"] if each["product"] == PRODUCT]
    [instance] = [each for each in running["instances"] if each["product"] == PRODUCT]
    assert (lane["condition"], lane["findings"], instance["other_image"]) == ("good", [], None)


def test_the_instance_has_no_way_out_and_runs_no_scheduled_job(story: Story) -> None:
    reached = on_scratch(
        f"docker exec {story.serving} bash -c 'timeout 5 bash -c \"echo > /dev/tcp/{OUTSIDE_ADDRESS}/443\" 2>&1; echo exit=$?'"
    )
    assert "exit=0" not in reached
    assert on_scratch(f"docker exec {story.serving} printenv ODOO_MAX_CRON_THREADS").strip() == "0"
    assert in_the_lanes_database(JOBS_RUN) == "0"


def test_a_build_given_as_text_is_refused_for_the_odoo_product_and_nothing_is_kept(story: Story) -> None:
    status, body = ask(f"/lanes/{PRODUCT}/{LANE}/move", {"build": build_of(LATER_RUN)})
    assert (status, 'names its build as {"run": <number>' in body["error"]) == (422, True)
    assert [each for each in ask("/requests")[1]["unfinished"] if each["product"] == PRODUCT] == []


def test_a_missing_attachment_file_fails_the_check_of_the_next_build_and_the_lane_keeps_serving_the_old_one(story: Story) -> None:
    stored = in_the_lanes_database(STORED_FILE)
    file, held = f"{FILES_PATH}/filestore/app/{stored}", f"{FILES_PATH}/held-by-the-test"
    on_scratch(f"docker exec {story.serving} mv {file} {held}")
    try:
        answer = move_to(LATER_RUN)
    finally:
        on_scratch(f"docker exec {story.serving} mv {held} {file}")
    assert (answer["outcome"], answer["container_receiving_visitors"]) == ("check failed", story.serving)
    assert f"must be there once with the SHA-256 {ATTACHMENT_SHA256}" in answer["error"]
    assert f"it has: {NO_BYTES}" in answer["error"]
    assert "Coastal Towne Realty" in site_answer()
    [instance] = [each for each in ask("/running")[1]["instances"] if each["product"] == PRODUCT]
    assert (instance["container"], instance["build"]) == (story.serving, build_of(RUN))
    with_the_file_back = move_to(LATER_RUN)
    assert (with_the_file_back["outcome"], with_the_file_back["build"]) == ("switched", build_of(LATER_RUN))


SITE, ODOO, FAILING = "step78-website", "step78-odoo", "step78-refused"


@pytest.fixture
def described_first_starts() -> Iterator[None]:
    serve_recorded_git_host()
    try:
        for product in (SITE, ODOO, FAILING):
            retire_with_its_data(product, LANE)
        describe(SITE, Product())
        for product in (ODOO, FAILING):
            describe(product, first_start_product())
        with launchplane_held():
            yield
    finally:
        for product in (SITE, ODOO, FAILING):
            retire_with_its_data(product, LANE)
            on_control(f"rm -f /var/lib/launchplane/products/{product}.json")
        stop_recorded_git_host()


@pytest.mark.usefixtures("described_first_starts")
def test_first_start_from_descriptions() -> None:
    print(f"Website at testing.{SITE}.test and Odoo at testing.{ODOO}.test/web/login", flush=True)  # noqa: T201
    assert ask_and_wait(f"/lanes/{SITE}/testing/move", {"build": "nginx:1.30.6-alpine"})[0] == 200
    page = on_scratch(f"curl -fsS -H 'Host: testing.{SITE}.test' http://127.0.0.1/")
    assert "Welcome to nginx!" in page
    print("Website serves its welcome page.", flush=True)  # noqa: T201
    move = f"/lanes/{ODOO}/testing/move?wait=230"
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    status, moved = ask(move, {"run": RUN}, 240, headers)
    assert status == 200 and moved.get("outcome") == "switched", moved
    container = moved["container_receiving_visitors"]
    login_page = on_scratch(f"curl -fsS -H 'Host: testing.{ODOO}.test' http://127.0.0.1/web/login")
    assert "password" in login_page
    with pytest.raises(SystemExit, match="Access Denied"):
        SignedIn(ODOO, LANE)
    database = f"{ODOO}-{LANE}-database-{moved['data_set']}"
    path = on_scratch(f'docker exec {database} psql -U postgres -d app -Atc "{STORED_FILE}"').strip()
    digest = on_scratch(f"docker exec {container} sha256sum {FILES_PATH}/filestore/app/{path}").split()[0]
    assert digest == ATTACHMENT_SHA256
    print(f"Odoo serves its login page, refuses admin/admin, and its attachment reads back as {digest}.", flush=True)  # noqa: T201
    assert ask(move, {"run": RUN}, 240, headers) == (200, moved)
    _, again = ask(move, {"run": RUN}, 240)
    assert (again["outcome"], again["data_set"], again["container_receiving_visitors"], again["did"]) == (
        "unchanged",
        moved["data_set"],
        container,
        [],
    )
    print(f"Both repeat asks kept {container} on {moved['data_set']}.", flush=True)  # noqa: T201
    hold("first-start-refuse")
    status, refused = ask(f"/lanes/{FAILING}/testing/move?wait=230", {"run": RUN}, 240)
    assert status == 503 and "The bed refused the final transaction." in refused["error"], refused
    assert on_scratch(f"curl -s -o /dev/null -w '%{{http_code}}' -H 'Host: testing.{FAILING}.test' http://127.0.0.1/") == "404"
    assert all(part["product"] != FAILING for part in ask("/running")[1]["instances"])
    print("Forced initializer refusal left its data set aside and its address returned 404.", flush=True)  # noqa: T201


def first_start_pairs() -> list[dict[str, Any]]:
    return [pair for pair in ask("/running")[1]["data_sets"] if pair["product"] == ODOO]


def query_pair(pair: str, sql: str) -> str:
    database = f"{ODOO}-{LANE}-database-{pair}"
    return on_scratch(f"docker exec {database} psql -XAt -v ON_ERROR_STOP=1 -U postgres -d app -c {shlex.quote(sql)}").strip()


@pytest.mark.usefixtures("described_first_starts")
@pytest.mark.parametrize(
    ("when", "interruption"),
    [("before", "launchplane"), ("before", "docker"), ("after", "launchplane"), ("before", "cancel"), ("after", "cancel")],
)
def test_first_start_recovers_from_the_committed_row(when: str, interruption: str) -> None:
    hold(f"first-start-{when}")
    status, requested = ask(f"/lanes/{ODOO}/testing/move", {"run": RUN})
    assert status == 202
    select_writer = f"docker ps -q --filter label=launchplane.product={ODOO} --filter label=launchplane.part=initializer"

    def at_commit() -> bool:
        return (
            on_scratch(
                f'writer=$({select_writer}); [ -n "$writer" ] && '
                'docker exec "$writer" test -e /tmp/held-at-commit && echo held || true'
            ).strip()
            == "held"
        )

    wait_for(at_commit, 160)
    [old] = first_start_pairs()
    pair = old["data_set"]
    modules = query_pair(pair, "select count(*) from ir_module_module where state = 'installed'")
    assert int(modules) > 0
    if interruption == "launchplane":
        kill_held_launchplane()
    elif interruption == "docker":
        on_scratch("systemctl restart docker")
    else:
        assert ask(f"/requests/{requested['id']}/cancel", {})[0] == 202
    ended = request_ended(requested["id"])
    if interruption == "cancel":
        assert ended["stands"] == "cancelled"
        [left] = first_start_pairs()
        assert left["data_set"] == pair and bool(left["set_aside"]) == (when == "before")
        assert not any(each["product"] == ODOO for each in ask("/running")[1]["instances"])
        status, moved = ask_and_wait(f"/lanes/{ODOO}/testing/move", {"run": RUN}, 240)
        assert status == 200, moved
    else:
        assert ended["stands"] == "done", ended["error"]
        moved = ended["answer"]
    pairs = first_start_pairs()
    [active] = [each for each in pairs if each["data_set"] == moved["data_set"]]
    assert not active["set_aside"]
    if when == "before":
        [kept] = [each for each in pairs if each["set_aside"]]
        assert kept["data_set"] == pair != active["data_set"]
        assert (kept["volumes"], kept["files"]) == (old["volumes"], old["files"])
        assert query_pair(pair, "select count(*) from ir_module_module where state = 'installed'") == modules
    else:
        assert [active["data_set"]] == [each["data_set"] for each in pairs] == [pair]
    [card] = [each for each in ask("/running")[1]["cards"] if each["title"] == f"{ODOO} · testing"]
    assert [pill["text"] for pill in card["pills"]] == ["good"]
    print(f"{interruption} {when} commit. Started on {pair}, serving from {active['data_set']}.", flush=True)  # noqa: T201


@pytest.mark.usefixtures("described_first_starts")
@pytest.mark.parametrize("plant_row", [True, False], ids=["row-and-file", "files-only"])
def test_first_start_preserves_existing_contents(plant_row: bool) -> None:
    point = "before.host.initialize"
    hold(point)
    status, request = ask(f"/lanes/{ODOO}/testing/move", {"run": RUN})
    assert status == 202
    wait_until_stopped_at(point)
    [found] = first_start_pairs()
    pair = found["data_set"]
    [volume] = found["files"]
    mount = f"docker run --rm --network none -v {volume}:/files --entrypoint sh postgres:18-alpine -c "
    on_scratch(mount + "'echo kept > /files/planted'")
    if plant_row:
        query_pair(pair, "create schema planted; create table planted.note as select 'kept'::text as value")
    let_go(point)
    ended = request_ended(request["id"])
    assert ended["stands"] == "failed" and "set aside" in ended["error"]
    [kept] = first_start_pairs()
    assert kept["set_aside"] and kept["data_set"] == pair
    assert on_scratch(mount + "'cat /files/planted'").strip() == "kept"
    if plant_row:
        assert query_pair(pair, "select value from planted.note") == "kept"
    assert query_pair(pair, "select to_regclass('ir_module_module') is null") == "t"
