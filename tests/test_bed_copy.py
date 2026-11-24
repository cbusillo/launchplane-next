import hashlib
import subprocess
import threading
from collections.abc import Iterator
from typing import Any

import pytest

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
    wait_for,
    wait_until_stopped_at,
)
from tests.bed_odoo import (
    LEAD_BYTES,
    WRITTEN,
    SignedIn,
    copies_of,
    describe,
    pull_builds,
    read_copy,
    read_lane,
    retire_with_its_data,
    seed,
    serve_recorded_git_host,
    stop_recorded_git_host,
)
from tests.recorded_git_host import RUN

pytestmark = needs_test_bed

PRODUCT, LANE = "test-bed-copy", "testing"
WEB_ADDRESS = f"{LANE}.{PRODUCT}.test"
COPY = f"/lanes/{PRODUCT}/{LANE}/copy"
ATTACHMENT = hashlib.sha256(LEAD_BYTES).hexdigest()
BACK_SOON = "503"
# By the scratch host's own clock, as the time a copier finished is: when the web address first answered as the back soon page.
FIRST_BACK_SOON = (
    f"for each in $(seq 1200); do [ \"$(curl --silent --output /dev/null --write-out '%{{http_code}}' --max-time 2 "
    f"--header 'Host: {WEB_ADDRESS}' http://127.0.0.1/)\" = {BACK_SOON} ] && date +%s.%N && exit; sleep 0.05; done; exit 1"
)


def site_status() -> str:
    asked = f"curl --silent --output /dev/null --write-out '%{{http_code}}' --max-time 20 --header 'Host: {WEB_ADDRESS}' http://127.0.0.1/"
    return on_scratch(asked).strip()


def finished_at(container: str) -> float:
    return float(on_scratch(f"date --date=\"$(docker inspect --format '{{{{.State.FinishedAt}}}}' {container})\" +%s.%N"))


def started_at(container: str) -> str:
    return on_scratch(f"docker inspect --format '{{{{.State.StartedAt}}}}' {container}").strip()


def files_in(volume: str) -> str:
    return on_scratch(f"docker run --rm --network none --volume {volume}:/copy:ro busybox:1.37 ls -A /copy").strip()


def clear_the_lane() -> None:
    retire_with_its_data(PRODUCT, LANE)
    remove_product_copies(PRODUCT)


def lines_of(request: dict[str, Any], attempt: int | None = None) -> list[str]:
    return [each["did"] for each in request["events"] if attempt in (None, each["attempt"])]


def last_request() -> dict[str, Any]:
    ended = [each for each in ask("/requests")[1]["ended"] if (each["product"], each["work"]) == (PRODUCT, "copying")]
    return ask(f"/requests/{max(each['id'] for each in ended)}")[1]


# One seeded lane of TK, serving, with the invented lead and its attachment written through Odoo at its web address.
@pytest.fixture(scope="module")
def lane() -> Iterator[None]:
    try:
        pull_builds()
    except subprocess.CalledProcessError as error:
        pytest.skip(f"the scratch host does not have TK's builds and could not pull them: {error.stderr}")
    serve_recorded_git_host()
    describe(PRODUCT)
    clear_the_lane()
    try:
        ask_and_wait(f"/lanes/{PRODUCT}/{LANE}/move", {"run": RUN})
        seed(PRODUCT, LANE)
        status, moved = ask_and_wait(f"/lanes/{PRODUCT}/{LANE}/move", {"run": RUN})
        assert (status, moved["outcome"]) == (200, "switched"), moved
        SignedIn(PRODUCT, LANE).make_lead()
        yield
    finally:
        clear_the_lane()
        stop_recorded_git_host()
        on_control(f"rm -f /var/lib/launchplane/products/{PRODUCT}.json")


def test_a_whole_copy_holds_the_lead_and_its_bytes_in_volumes_of_its_own_and_the_same_instance_serves_and_takes_a_write(
    lane: None,
) -> None:
    serving = read_lane(PRODUCT, LANE)
    seen: list[float] = []
    watching = threading.Thread(target=lambda: seen.append(float(on_scratch(FIRST_BACK_SOON))))
    watching.start()
    status, answer = ask_and_wait(COPY, {})
    watching.join()
    assert (status, answer["outcome"], answer["container_receiving_visitors"]) == (200, "copied", serving["instance"]), answer
    [copy] = [each for each in copies_of(PRODUCT, LANE) if each["data_set"] == answer["copied_to"]]
    confirmed = max(finished_at(each["container"]) for each in copy["copies"])
    print(f"\nfrom the first back soon answer to both copies confirmed: {confirmed - seen[0]:.1f} s")  # noqa: T201
    assert 0 < confirmed - seen[0] < 60
    in_the_copy = read_copy(copy)
    assert (in_the_copy["lead"], in_the_copy["attachment"]) == ("lead", ATTACHMENT) == (serving["lead"], serving["attachment"])
    assert len(in_the_copy["volumes"].split(",")) == 2
    assert not set(in_the_copy["volumes"].split(",")) & set(serving["volumes"].split(","))
    assert ([each["finished"] for each in copy["copies"]], copy["networks"], copy["databases"]) == ([True, True], [], [])
    assert read_lane(PRODUCT, LANE) == serving
    SignedIn(PRODUCT, LANE).write("after the whole copy")
    assert read_lane(PRODUCT, LANE)["written"].split(",")[-1] == f"{WRITTEN}after the whole copy"
    lines = lines_of(last_request())
    assert not [each for each in lines if each.startswith(("switch ", "remove the login", "make the login"))], lines
    [judged] = [each for each in ask("/running")[1]["lanes"] if each["product"] == PRODUCT]
    assert (judged["condition"], judged["findings"]) == ("good", [])


def test_a_copy_killed_between_its_volumes_is_left_unused_and_the_same_instance_serves_again_and_keeps_a_write_made_then(
    lane: None,
) -> None:
    serving = read_lane(PRODUCT, LANE)
    with launchplane_held():
        hold("before.host.copy_volume?only=-files")
        status, kept = ask(COPY, {})
        assert status == 202, kept
        wait_until_stopped_at("before.host.copy_volume")
        [half] = [each for each in copies_of(PRODUCT, LANE) if each["generation"] == kept["generation"]]
        assert (site_status(), [each["finished"] for each in half["copies"]], files_in(half["files"][0])) == (BACK_SOON, [True], "")
        hold(f"after.proxy.edit?only={WEB_ADDRESS}")
        kill_held_launchplane()
        wait_until_stopped_at("after.proxy.edit", seconds=120)
        wait_for(lambda: site_status() == "200", seconds=30)
        assert read_lane(PRODUCT, LANE) == serving
        SignedIn(PRODUCT, LANE).write("after the kill")
        started = started_at(serving["instance"])
        kill_held_launchplane()
        let_go()
        ended = request_ended(kept["id"])
    assert (ended["stands"], ended["answer"]["outcome"]) == ("failed", "not copied"), ended
    assert [each["ended"] for each in ended["attempts"]] == ["interrupted", "interrupted", "failed"]
    assert f"An earlier attempt began the copy {half['data_set']} and did not finish it" in lines_of(ended, 2)[0]
    assert lines_of(ended, 3) == []
    after = read_lane(PRODUCT, LANE)
    assert (after["instance"], after["build"], after["volumes"]) == (serving["instance"], serving["build"], serving["volumes"])
    assert (after["lead"], after["attachment"], after["written"].split(",")[-1]) == ("lead", ATTACHMENT, f"{WRITTEN}after the kill")
    assert started_at(serving["instance"]) == started
    [left] = [each for each in copies_of(PRODUCT, LANE) if each["data_set"] == half["data_set"]]
    assert ([each["finished"] for each in left["copies"]], files_in(left["files"][0]), left["databases"]) == ([True], "", [])
