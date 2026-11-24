import hashlib
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from launchplane.odoo import FILES_PATH, USER
from tests.bed import ask, ask_and_wait, needs_test_bed, on_scratch, remove_product_copies, retire
from tests.bed_odoo import data_set_of, describe, pull_builds, seed, serve_recorded_git_host, stop_recorded_git_host
from tests.odoo_product import ATTACHMENT_SHA256
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
    assert answer["did"].count(f"made part of the data set {data_set_of(PRODUCT, LANE)}") == 4


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
