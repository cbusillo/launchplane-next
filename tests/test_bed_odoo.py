import hashlib
import subprocess
import time
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
    kill_launchplane,
    launchplane_held,
    let_go,
    move_lane,
    needs_test_bed,
    on_control,
    on_scratch,
    remove_product_copies,
    request_ended,
    retire,
    server_answering,
    wait_for,
    wait_until_stopped_at,
)
from tests.bed_odoo import (
    SignedIn,
    attachment_read_by_the_tester,
    data_set_of,
    default_admin_is_refused,
    describe,
    forget_test_password,
    kept_password,
    pull_builds,
    retire_with_its_data,
    seed,
    serve_recorded_git_host,
    stop_recorded_git_host,
)
from tests.odoo_product import ATTACHMENT_BYTES, ATTACHMENT_SHA256, TEST_LOGIN, TK_STARTING
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


SITE, ODOO, FAILING = "first-site", "first-odoo", "first-odoo-fails"
SITE_BUILD, SITE_ANSWERS = "nginx:1.30.6-alpine", "nginx/1.30.6"
ROW = "Invented contact of the first-start watch"
# How long each point stays on the page before the next one changes it.
READING_SECONDS = 8
REFUSED = "the first-start watch lets no table be made in this database"
REFUSE_THE_TABLES = f"""
create function refuse_the_tables() returns event_trigger language plpgsql as
$$ begin raise exception '{REFUSED}'; end $$;
create event trigger refuse_the_tables on ddl_command_start when tag in ('CREATE TABLE') execute function refuse_the_tables();
"""


def said(point: str, **observed: Any) -> None:  # noqa: ANN401 whatever was observed
    print(f"\n{point}", *(f"  {name.replace('_', ' ')}: {value}" for name, value in observed.items()), sep="\n")  # noqa: T201
    time.sleep(READING_SECONDS)


def visit(product: str, path: str = "/") -> str:
    asked = f"curl --silent --output /dev/null --write-out '%{{http_code}}' --max-time 20 --header 'Host: testing.{product}.test'"
    return on_scratch(f"{asked} http://127.0.0.1{path}").strip()


# The lane's card as the page draws it, or None where the page has none for it. One with no web address is what a retire
# that ended lately leaves for ten minutes.
def card(product: str) -> dict[str, Any] | None:
    found = [each for each in ask("/running")[1]["cards"] if each["title"] == f"{product} · testing"]
    if not found:
        return None
    rows = {row["name"]: row for row in found[0]["rows"]}
    return {
        "reads": [pill["text"] for pill in found[0]["pills"]],
        "web_address": [pill["text"] for pill in rows.get("Web address", {}).get("pills", [])],
        "found": [row["value"][0]["text"] for row in found[0]["rows"] if row["name"] == "Found"],
    }


def parts_of(product: str) -> dict[str, list[str]]:
    running = ask("/running")[1]
    return {
        "instances": [each["container"] for each in running["instances"] if each["product"] == product],
        "data_sets": [each["data_set"] for each in running["data_sets"] if each["product"] == product],
        "visitors_sent_to": [
            each["sends_to_container"]
            for each in running["web_addresses"]
            if each["web_address"] == f"testing.{product}.test" and each["sends_to_container"]
        ],
    }


def last_request(product: str) -> dict[str, Any]:
    return ask(f"/requests/{max(each['id'] for each in ask('/requests')[1]['ended'] if each['product'] == product)}")[1]


def first_starts(request: dict[str, Any]) -> list[str]:
    return [each["did"] for each in request["events"] if each["did"].startswith("run the first start on the data set ")]


# What a tester signed in at the lane's web address reads there: the declared attachment's bytes and each invented row.
def read_signed_in(password: str) -> dict[str, Any]:
    rows = SignedIn(ODOO, "testing", TEST_LOGIN, password).call("res.partner", "search_read", [("name", "=", ROW)], fields=["name"])
    return {"attachment": attachment_read_by_the_tester(ODOO, "testing"), "rows": [each["id"] for each in rows]}


def put_the_bed_back() -> None:
    for product in (ODOO, FAILING):
        retire_with_its_data(product, "testing")
        forget_test_password(product)
    retire(SITE, "testing")
    for product in (SITE, ODOO, FAILING):
        remove_product_copies(product)
        on_control(f"rm -f /var/lib/launchplane/products/{product}.json")


# Only the build the watch moves is asked for, and the watch fails where the scratch host has none: it never skips.
@pytest.fixture
def described() -> Iterator[None]:
    try:
        pull_builds(RUN)
    except subprocess.CalledProcessError as error:
        pytest.fail(f"The scratch host does not have TK's build {build_of(RUN)} and could not pull it: {error.stderr}")
    serve_recorded_git_host()
    put_the_bed_back()
    describe(SITE, Product(health_path="/"))
    describe(ODOO, TK_STARTING)
    describe(FAILING, TK_STARTING)
    try:
        yield
    finally:
        put_the_bed_back()
        stop_recorded_git_host()


@pytest.mark.usefixtures("described")
def test_first_start_from_descriptions_brings_up_a_website_and_an_initialized_odoo_lane_with_nothing_run_by_hand() -> None:
    move_odoo = f"/lanes/{ODOO}/testing/move"
    said(
        "1. Two described products, and nothing of either on the host",
        website=f"testing.{SITE}.test, {visit(SITE)}",
        odoo=f"testing.{ODOO}.test/web/login, {visit(ODOO, '/web/login')}",
        cards=[card(SITE), card(ODOO)],
        on_the_host=[parts_of(SITE), parts_of(ODOO)],
    )
    assert (visit(SITE), visit(ODOO, "/web/login")) == ("404", "404")
    assert parts_of(SITE) == parts_of(ODOO) == {"instances": [], "data_sets": [], "visitors_sent_to": []}

    status, site = move_lane(SITE, "testing", SITE_BUILD)
    said(
        "2. The website's first move",
        answered=status,
        outcome=site["outcome"],
        server=server_answering(f"testing.{SITE}.test"),
        card=card(SITE),
    )
    assert (status, site["outcome"], server_answering(f"testing.{SITE}.test")) == (200, "switched", SITE_ANSWERS)
    assert card(SITE) == {"reads": ["good"], "web_address": ["answering"], "found": []}

    key = {"Idempotency-Key": f"first-start-{uuid.uuid4()}"}
    started = time.monotonic()
    status, odoo = ask(f"{move_odoo}?wait=230", {"run": RUN}, timeout=240, headers=key)
    took = time.monotonic() - started
    assert (status, odoo["outcome"], odoo["build"]) == (200, "switched", build_of(RUN)), odoo
    request = last_request(ODOO)
    password = kept_password(ODOO, "testing")
    read = read_signed_in(password)
    said(
        "3. Odoo's first move, on its first request",
        answered=status,
        outcome=odoo["outcome"],
        seconds=round(took, 1),
        first_start=first_starts(request),
        card=card(ODOO),
        signed_in_as=TEST_LOGIN,
        attachment_read=hashlib.sha256(read["attachment"]).hexdigest(),
        default_admin_refused=default_admin_is_refused(ODOO, "testing"),
    )
    assert (len(request["attempts"]), len(first_starts(request)), read) == (1, 1, {"attachment": ATTACHMENT_BYTES, "rows": []})
    assert (card(ODOO), default_admin_is_refused(ODOO, "testing")) == (
        {"reads": ["good"], "web_address": ["answering"], "found": []},
        True,
    )
    for told in (str(odoo), str(request), str(ask("/running")[1])):
        assert password not in told
    assert on_scratch(f"docker exec {odoo['container_receiving_visitors']} printenv ODOO_MAX_CRON_THREADS").strip() == "0"

    SignedIn(ODOO, "testing", TEST_LOGIN, password).call("res.partner", "create", [{"name": ROW}])
    [row] = read_signed_in(password)["rows"]
    serving = parts_of(ODOO)

    again_site = move_lane(SITE, "testing", SITE_BUILD)[1]
    same_request = ask(f"{move_odoo}?wait=230", {"run": RUN}, timeout=240, headers=key)[1]
    status, again = ask_and_wait(move_odoo, {"run": RUN})
    said(
        "4. Asked again",
        website=again_site["outcome"],
        odoo_with_the_same_key=same_request["outcome"],
        odoo_as_a_new_request=again["outcome"],
        did=again["did"],
        on_the_host=parts_of(ODOO),
        row=read_signed_in(password)["rows"],
    )
    assert (again_site["outcome"], again_site["container_receiving_visitors"]) == ("unchanged", site["container_receiving_visitors"])
    assert (same_request, again["outcome"], again["did"]) == (odoo, "unchanged", [])
    assert (parts_of(ODOO), read_signed_in(password)) == (serving, {"attachment": ATTACHMENT_BYTES, "rows": [row]})

    # With no instance and no route, the first start runs again and finds the data: nothing the lane lacks seeds over it.
    assert retire(ODOO, "testing")[0] == 200
    status, back = ask_and_wait(move_odoo, {"run": RUN})
    brought_back = last_request(ODOO)
    said(
        "5. Retired with its data kept, and moved again",
        outcome=back["outcome"],
        first_start=first_starts(brought_back),
        data_sets=parts_of(ODOO)["data_sets"],
        row=read_signed_in(password)["rows"],
        password_is_the_same=kept_password(ODOO, "testing") == password,
        default_admin_refused=default_admin_is_refused(ODOO, "testing"),
        card=card(ODOO),
    )
    assert (status, back["outcome"], len(first_starts(brought_back))) == (200, "switched", 1)
    assert (kept_password(ODOO, "testing"), default_admin_is_refused(ODOO, "testing")) == (password, True)
    assert (parts_of(ODOO)["data_sets"], read_signed_in(password)) == (
        serving["data_sets"],
        {"attachment": ATTACHMENT_BYTES, "rows": [row]},
    )

    with launchplane_held():
        hold("before.host.run_once")
        status, asked = ask(f"/lanes/{FAILING}/testing/move", {"run": RUN})
        assert status == 202, asked
        wait_until_stopped_at("before.host.run_once", seconds=120)
        server = f"{FAILING}-testing-database-{data_set_of(FAILING, 'testing')}"
        psql = "psql --username=postgres --dbname=app --quiet --set=ON_ERROR_STOP=1"
        on_scratch(f"docker exec --interactive {server} {psql}", given=REFUSE_THE_TABLES)
        let_go()
        failed = request_ended(asked["id"])
    said(
        "6. A first start that fails",
        request=failed["stands"],
        outcome=failed["answer"]["outcome"],
        error=failed["error"],
        visitor=visit(FAILING, "/web/login"),
        on_the_host=parts_of(FAILING),
        card=card(FAILING),
    )
    assert (failed["stands"], failed["answer"]["outcome"], visit(FAILING, "/web/login")) == ("failed", "not initialized", "404")
    assert REFUSED in failed["error"]
    assert (parts_of(FAILING)["instances"], parts_of(FAILING)["visitors_sent_to"]) == ([], [])
    assert [each["did"] for each in failed["events"] if each["did"].startswith(("start the instance", "switch "))] == []
    assert card(FAILING) == {"reads": ["move failed"], "web_address": ["not answering"], "found": ["No instance receives visitors"]}
    assert kept_password(FAILING, "testing") not in str(failed)


# noinspection SqlResolve
MODULES_INSTALLED = "select count(*) from ir_module_module where state = 'installed'"
# noinspection SqlResolve
MARKED_FINISHED = "select finished from launchplane.first_start"


# What the lane's database answers, or nothing while it has no server, no such table or no answer.
def in_odoos_database(statement: str) -> str:
    psql = "psql --username=postgres --dbname=app --tuples-only --no-align"
    try:
        server = f"{ODOO}-testing-database-{data_set_of(ODOO, 'testing')}"
        return on_scratch(f'docker exec {server} {psql} --command="{statement}"').strip()
    except subprocess.CalledProcessError, ValueError:
        return ""


def modules_are_committed_and_the_first_start_is_not_finished() -> bool:
    return in_odoos_database(MODULES_INSTALLED) not in ("", "0") and in_odoos_database(MARKED_FINISHED) == "f"


@pytest.mark.usefixtures("described")
def test_first_start_from_descriptions_cut_short_by_a_kill_of_launchplane_is_made_again_and_the_lane_serves() -> None:
    status, asked = ask(f"/lanes/{ODOO}/testing/move", {"run": RUN})
    assert status == 202, asked
    wait_for(modules_are_committed_and_the_first_start_is_not_finished, seconds=120)
    cut_short_with = in_odoos_database(MODULES_INSTALLED)
    kill_launchplane()

    def taken_up_again_and_ended() -> bool:
        return ask(f"/requests/{asked['id']}")[1]["stands"] != "working"

    wait_for(taken_up_again_and_ended, seconds=240)
    ended = ask(f"/requests/{asked['id']}")[1]
    said(
        "Killed while the first start ran, with modules committed and nothing seeded",
        modules_installed_at_the_kill=cut_short_with,
        request=ended["stands"],
        outcome=(ended["answer"] or {}).get("outcome"),
        attempts=[each["ended"] for each in ended["attempts"]],
        first_start=first_starts(ended),
        marked_finished=in_odoos_database(MARKED_FINISHED),
        card=card(ODOO),
        default_admin_refused=default_admin_is_refused(ODOO, "testing"),
        attachment_read=hashlib.sha256(attachment_read_by_the_tester(ODOO, "testing")).hexdigest(),
    )
    assert (ended["stands"], ended["answer"]["outcome"], len(ended["attempts"])) == ("done", "switched", 2), ended
    assert (in_odoos_database(MARKED_FINISHED), attachment_read_by_the_tester(ODOO, "testing")) == ("t", ATTACHMENT_BYTES)
    assert (card(ODOO), default_admin_is_refused(ODOO, "testing")) == (
        {"reads": ["good"], "web_address": ["answering"], "found": []},
        True,
    )
    assert [each for each in parts_of(ODOO)["instances"] if each.endswith("-first-start")] == []
