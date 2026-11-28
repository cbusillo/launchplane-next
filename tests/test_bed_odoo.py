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
    START_AGAIN,
    ask,
    ask_and_wait,
    hold,
    kill_held_launchplane,
    kill_launchplane,
    launchplane_answers,
    launchplane_held,
    let_go,
    move_lane,
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
    CHANGED,
    change_tester_password,
    data_set_of,
    default_admin_is_refused,
    describe,
    forget_test_password,
    lose_password,
    password_is_kept,
    pull_builds,
    retire_with_its_data,
    says_the_password,
    seed,
    serve_recorded_git_host,
    stop_recorded_git_host,
    tester,
    tester_is_refused,
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
SITE_BUILD = "nginx:1.30.6-alpine"
ROW = "Invented contact of the first-start watch"
# How long each point stays on the page before the next one changes it.
READING_SECONDS = 8
REFUSED = "the first-start watch lets no table be made in this database"
REFUSE_THE_TABLES = f"""
create function refuse_the_tables() returns event_trigger language plpgsql as
$$ begin raise exception '{REFUSED}'; end $$;
create event trigger refuse_the_tables on ddl_command_start when tag in ('CREATE TABLE') execute function refuse_the_tables();
"""
# What a person might put on a data set that was set aside: it must be there, as put, whatever the lane does next.
# noinspection SqlResolve
REPAIR = "drop event trigger refuse_the_tables; create table repaired_by_a_person as select 'kept' as said"
# noinspection SqlResolve
READ_REPAIR = "select said from repaired_by_a_person"
# noinspection SqlResolve
MODULES_INSTALLED = "select count(*) from ir_module_module where state = 'installed'"
PSQL = "psql --username=postgres --dbname=app --quiet --tuples-only --no-align --set=ON_ERROR_STOP=1"


def said(point: str, **observed: Any) -> None:  # noqa: ANN401 whatever was observed
    print(f"\n{point}", *(f"  {name.replace('_', ' ')}: {value}" for name, value in observed.items()), sep="\n")  # noqa: T201
    time.sleep(READING_SECONDS)


def visit(product: str, path: str = "/", show: str = "%{http_code}") -> str:
    asked = f"curl --silent --max-time 20 --header 'Host: testing.{product}.test'"
    return on_scratch(f"{asked} {'--output /dev/null --write-out ' + repr(show) if show else ''} http://127.0.0.1{path}").strip()


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


GOOD = {"reads": ["good"], "web_address": ["answering"], "found": []}


# What the host has of the product's lane: its instances, its data sets with how each one's first start ended, the data
# sets that are set aside, and the container visitors are sent to.
def parts_of(product: str) -> dict[str, Any]:
    running = ask("/running")[1]
    data_sets = [each for each in running["data_sets"] if each["product"] == product]
    return {
        "instances": [each["container"] for each in running["instances"] if each["product"] == product],
        "data_sets": {each["data_set"]: [ran["seeded"] for ran in each["first_starts"]] for each in data_sets},
        "serves_from": [
            each["data_set"] for each in running["instances"] if each["product"] == product and each["receives_visitors_for"]
        ],
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


# What the tester reads in one session at the lane's web address: the declared attachment's bytes and each invented row.
def read_as_tester(product: str = ODOO, password: str = "") -> dict[str, Any]:
    signed_in = tester(product, "testing", password)
    rows = signed_in.call("res.partner", "search_read", [("name", "=", ROW)], fields=["name"])
    return {"attachment": signed_in.attachment(), "rows": [each["id"] for each in rows]}


def in_the_database(product: str, data_set: str, statement: str) -> str:
    server = f"{product}-testing-database-{data_set}"
    try:
        return on_scratch(f"docker exec --interactive {server} {PSQL}", given=statement).strip()
    except subprocess.CalledProcessError:
        return ""


def volumes_of(product: str, data_set: str) -> list[str]:
    of = f"--filter label=launchplane.product={product} --filter label=launchplane.data-set={data_set}"
    return sorted(on_scratch(f"docker volume ls --quiet {of}").split())


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
    nothing: dict[str, Any] = {"instances": [], "data_sets": {}, "serves_from": [], "visitors_sent_to": []}
    said(
        "1. Two described products, and nothing of either on the host",
        website=f"testing.{SITE}.test, {visit(SITE)}",
        odoo=f"testing.{ODOO}.test/web/login, {visit(ODOO, '/web/login')}",
        on_the_host=[parts_of(SITE), parts_of(ODOO)],
    )
    assert (visit(SITE), visit(ODOO, "/web/login"), parts_of(SITE), parts_of(ODOO)) == ("404", "404", nothing, nothing)

    status, site = move_lane(SITE, "testing", SITE_BUILD)
    page = visit(SITE, show="")
    said(
        "2. The website's first move",
        answered=status,
        outcome=site["outcome"],
        page="<title>Welcome to nginx!</title>" in page,
        card=card(SITE),
    )
    assert (status, site["outcome"], "<title>Welcome to nginx!</title>" in page, card(SITE)) == (200, "switched", True, GOOD)

    key = {"Idempotency-Key": f"first-start-{uuid.uuid4()}"}
    started = time.monotonic()
    status, odoo = ask(f"{move_odoo}?wait=230", {"run": RUN}, timeout=240, headers=key)
    took = time.monotonic() - started
    assert (status, odoo["outcome"], odoo["build"]) == (200, "switched", build_of(RUN)), odoo
    request, serving = last_request(ODOO), parts_of(ODOO)
    signed_in = tester(ODOO, "testing")
    read = signed_in.attachment()
    [row] = signed_in.call("res.partner", "create", [{"name": ROW}])
    leaked = says_the_password(ODOO, "testing", odoo, request, ask("/running")[1])
    said(
        "3. Odoo's first move, on its first request",
        answered=status,
        outcome=odoo["outcome"],
        seconds=round(took, 1),
        first_start=first_starts(request),
        on_the_host=serving,
        card=card(ODOO),
        signed_in_as=TEST_LOGIN,
        attachment_read=hashlib.sha256(read).hexdigest(),
        row_written=row,
        default_admin_refused=default_admin_is_refused(ODOO, "testing"),
        password_said_anywhere=leaked,
    )
    assert (len(request["attempts"]), len(first_starts(request)), read, leaked) == (1, 1, ATTACHMENT_BYTES, False)
    assert (card(ODOO), default_admin_is_refused(ODOO, "testing"), list(serving["data_sets"].values())) == (GOOD, True, [[True]])
    assert on_scratch(f"docker exec {odoo['container_receiving_visitors']} printenv ODOO_MAX_CRON_THREADS").strip() == "0"

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
        tester_reads=read_as_tester()["rows"],
    )
    assert (again_site["outcome"], again_site["container_receiving_visitors"]) == ("unchanged", site["container_receiving_visitors"])
    assert (same_request, again["outcome"], again["did"]) == (odoo, "unchanged", [])
    assert (parts_of(ODOO), read_as_tester()) == (serving, {"attachment": ATTACHMENT_BYTES, "rows": [row]})

    # With no instance and no route, the data set is one this request did not make: it is started on as it is.
    assert retire(ODOO, "testing")[0] == 200
    status, back = ask_and_wait(move_odoo, {"run": RUN})
    said(
        "5. Retired with its data kept, and moved again",
        outcome=back["outcome"],
        first_start=first_starts(last_request(ODOO)),
        on_the_host=parts_of(ODOO),
        tester_reads=read_as_tester()["rows"],
        card=card(ODOO),
    )
    assert (status, back["outcome"], first_starts(last_request(ODOO))) == (200, "switched", [])
    assert (parts_of(ODOO)["data_sets"], read_as_tester()) == (serving["data_sets"], {"attachment": ATTACHMENT_BYTES, "rows": [row]})

    lose_password(ODOO, "testing")
    lost, status, refused = card(ODOO), *ask_and_wait(move_odoo, {"run": RUN})
    said(
        "6. The password file gone",
        card=lost,
        same_build_asked=refused["outcome"],
        error=refused["error"],
        file_made_again=password_is_kept(ODOO, "testing"),
    )
    assert (lost == GOOD, status, refused["outcome"], password_is_kept(ODOO, "testing")) == (False, 422, "check failed", False)
    assert "has no password kept on the control machine" in refused["error"]
    lose_password(ODOO, "testing", back=True)
    assert (card(ODOO), read_as_tester()["rows"]) == (GOOD, [row])

    change_tester_password(ODOO, "testing")
    changed, status, refused = card(ODOO), *ask_and_wait(move_odoo, {"run": RUN})
    still_opens = read_as_tester(password=CHANGED)["rows"]
    said(
        "7. The tester's password changed in Odoo",
        card=changed,
        same_build_asked=refused["outcome"],
        error=refused["error"],
        changed_password_reads=still_opens,
    )
    assert (changed == GOOD, status, refused["outcome"], still_opens) == (False, 422, "check failed", [row])
    assert (f"Odoo refused the testing login {TEST_LOGIN}." in refused["error"], tester_is_refused(ODOO, "testing")) == (True, True)
    change_tester_password(ODOO, "testing", back=True)
    # Odoo answers nobody at an address that failed five sign-ins for a minute, so the card takes that long to read good.
    wait_for(lambda: card(ODOO) == GOOD, seconds=150)
    assert parts_of(ODOO)["serves_from"] == list(serving["data_sets"])

    with launchplane_held():
        hold("before.host.run_once")
        status, asked = ask(f"/lanes/{FAILING}/testing/move", {"run": RUN})
        assert status == 202, asked
        wait_until_stopped_at("before.host.run_once", seconds=120)
        aside = data_set_of(FAILING, "testing")
        on_scratch(f"docker exec --interactive {FAILING}-testing-database-{aside} {PSQL}", given=REFUSE_THE_TABLES)
        let_go()
        failed = request_ended(asked["id"])
    leaked = says_the_password(FAILING, "testing", failed, ask("/running")[1])
    said(
        "8. A first start that fails",
        request=failed["stands"],
        outcome=failed["answer"]["outcome"],
        error=failed["error"],
        visitor=visit(FAILING, "/web/login"),
        on_the_host=parts_of(FAILING),
        card=card(FAILING),
        password_said_anywhere=leaked,
    )
    assert (failed["stands"], failed["answer"]["outcome"], visit(FAILING, "/web/login"), leaked) == (
        "failed",
        "not initialized",
        "404",
        False,
    )
    assert REFUSED in failed["error"]
    assert parts_of(FAILING) == {"instances": [], "data_sets": {aside: [False]}, "serves_from": [], "visitors_sent_to": []}
    assert [each["did"] for each in failed["events"] if each["did"].startswith(("start the instance", "switch "))] == []
    assert card(FAILING) == {"reads": ["move failed"], "web_address": ["not answering"], "found": ["No instance receives visitors"]}

    # A person repairs the data set that was set aside. The next ask starts on a new one, and the repair stays as put.
    on_scratch(f"docker start {FAILING}-testing-database-{aside}")
    wait_for(lambda: in_the_database(FAILING, aside, "select 1") == "1", seconds=60)
    in_the_database(FAILING, aside, REPAIR)
    on_scratch(f"docker run --rm --volume launchplane-data-{aside}-files:/files busybox:1.37 touch /files/repaired-by-a-person")
    kept_volumes = volumes_of(FAILING, aside)
    status, anew = ask_and_wait(f"/lanes/{FAILING}/testing/move", {"run": RUN}, timeout=240)
    after = parts_of(FAILING)
    on_scratch(f"docker start {FAILING}-testing-database-{aside}")
    wait_for(lambda: in_the_database(FAILING, aside, "select 1") == "1", seconds=60)
    repair = in_the_database(FAILING, aside, READ_REPAIR)
    file = on_scratch(f"docker run --rm --volume launchplane-data-{aside}-files:/files busybox:1.37 ls /files").split()
    on_scratch(f"docker stop {FAILING}-testing-database-{aside}")
    said(
        "9. Asked again after a person repaired the data set that was set aside",
        outcome=anew["outcome"],
        on_the_host=after,
        set_aside=aside,
        its_repair=repair,
        its_files=file,
        tester_reads_attachment=read_as_tester(FAILING)["attachment"] == ATTACHMENT_BYTES,
        card=card(FAILING),
    )
    assert (status, anew["outcome"], after["serves_from"] != [aside], after["data_sets"][aside]) == (200, "switched", True, [False])
    assert (repair, "repaired-by-a-person" in file, volumes_of(FAILING, aside)) == ("kept", True, kept_volumes)
    assert len(after["data_sets"]) == 2
    assert (read_as_tester(FAILING), card(FAILING)) == ({"attachment": ATTACHMENT_BYTES, "rows": []}, GOOD)


def modules_are_committed(data_set: str) -> bool:
    return in_the_database(ODOO, data_set, MODULES_INSTALLED) not in ("", "0")


def first_start_container() -> str:
    return on_scratch(
        f"docker ps --all --quiet --filter label=launchplane.product={ODOO} --filter label=launchplane.part=first-start"
    ).strip()


def ended_after_it_was_taken_up_again(request: int) -> dict[str, Any]:
    def taken_up_again_and_ended() -> bool:
        return ask(f"/requests/{request}")[1]["stands"] != "working"

    wait_for(taken_up_again_and_ended, seconds=240)
    return ask(f"/requests/{request}")[1]


@pytest.mark.usefixtures("described")
def test_first_start_from_descriptions_killed_part_way_ends_with_one_lane_serving_from_one_seed() -> None:
    move_odoo = f"/lanes/{ODOO}/testing/move"
    status, asked = ask(move_odoo, {"run": RUN})
    assert status == 202, asked
    wait_for(lambda: bool(first_start_container()), seconds=120)
    began_in = data_set_of(ODOO, "testing")
    wait_for(lambda: modules_are_committed(began_in), seconds=120)
    kill_launchplane()
    ended, after = ended_after_it_was_taken_up_again(asked["id"]), parts_of(ODOO)
    said(
        "10. Launchplane killed while the first start ran, with modules committed and nothing seeded",
        request=ended["stands"],
        outcome=ended["answer"] and ended["answer"]["outcome"],
        attempts=[each["ended"] for each in ended["attempts"]],
        began_in=began_in,
        on_the_host=after,
        tester_reads_attachment=read_as_tester()["attachment"] == ATTACHMENT_BYTES,
        card=card(ODOO),
    )
    assert (ended["stands"], ended["answer"]["outcome"], after["data_sets"], after["serves_from"]) == (
        "done",
        "switched",
        {began_in: [True]},
        [began_in],
    )
    assert (read_as_tester(), card(ODOO), default_admin_is_refused(ODOO, "testing")) == (
        {"attachment": ATTACHMENT_BYTES, "rows": []},
        GOOD,
        True,
    )

    retire_with_its_data(ODOO, "testing")
    status, asked = ask(move_odoo, {"run": RUN})
    assert status == 202, asked
    wait_for(lambda: bool(first_start_container()), seconds=120)
    began_in = data_set_of(ODOO, "testing")
    wait_for(lambda: modules_are_committed(began_in), seconds=120)
    on_control("systemctl kill --signal=KILL launchplane; systemctl stop launchplane")
    on_scratch(f"docker kill {first_start_container()}")
    kept_volumes = volumes_of(ODOO, began_in)
    on_control(START_AGAIN)
    wait_for(launchplane_answers, seconds=30)
    ended, after = ended_after_it_was_taken_up_again(asked["id"]), parts_of(ODOO)
    said(
        "11. Launchplane and the first start both killed, with modules committed and nothing seeded",
        request=ended["stands"],
        outcome=ended["answer"] and ended["answer"]["outcome"],
        began_in=began_in,
        on_the_host=after,
        tester_reads_attachment=read_as_tester()["attachment"] == ATTACHMENT_BYTES,
        card=card(ODOO),
    )
    assert (ended["stands"], ended["answer"]["outcome"], after["data_sets"][began_in], len(after["data_sets"])) == (
        "done",
        "switched",
        [False],
        2,
    )
    assert (after["serves_from"] != [began_in], volumes_of(ODOO, began_in), modules_are_committed(began_in)) == (
        True,
        kept_volumes,
        False,
    )
    assert (read_as_tester(), card(ODOO)) == ({"attachment": ATTACHMENT_BYTES, "rows": []}, GOOD)

    retire_with_its_data(ODOO, "testing")
    with launchplane_held():
        hold("before.host.start_instance")
        status, asked = ask(move_odoo, {"run": RUN})
        assert status == 202, asked
        wait_until_stopped_at("before.host.start_instance", seconds=180)
        seeded_in = data_set_of(ODOO, "testing")
        kill_held_launchplane()
        let_go()
        ended = request_ended(asked["id"])
    after = parts_of(ODOO)
    said(
        "12. Launchplane killed once the seed was committed, before any web part",
        request=ended["stands"],
        outcome=ended["answer"] and ended["answer"]["outcome"],
        seeded_in=seeded_in,
        on_the_host=after,
        card=card(ODOO),
    )
    assert (ended["stands"], ended["answer"]["outcome"], after["data_sets"], after["serves_from"]) == (
        "done",
        "switched",
        {seeded_in: [True]},
        [seeded_in],
    )
    assert (read_as_tester(), card(ODOO)) == ({"attachment": ATTACHMENT_BYTES, "rows": []}, GOOD)
