"""The update watch: one seeded lane of TK moved from its first verified build to the next, whose module update turns
leads into opportunities. The three failures come first, so each write that survives one is evidence the next must keep.

Then the lane is made again from two builds of tests/bed_fixture.py. The new one reads, in a website bootstrap that
comes before its HTTP server, a stored field the old one's data lacks: its update fails once, and then is committed
before the first web process of the new build is started.

Every move is asked through the API as any caller asks it. Faults are made on the scratch host alone, and Launchplane is
held only to be looked at where a point must be read between two of its steps. With `-s` each observation is printed.
"""

import subprocess
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from tests.bed import (
    ask,
    ask_and_wait,
    hold,
    kill_launchplane,
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
from tests.bed_fixture import FIELD, FIXTURE, NEW_RUN, OLD_RUN, publish_builds, remove_builds
from tests.bed_odoo import (
    LEAD_FILE,
    UPDATING,
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
    witness_in,
)
from tests.recorded_git_host import LATER_RUN, RUN, build_of

pytestmark = needs_test_bed

PRODUCT, LANE = "watch-update", "testing"
WEB_ADDRESS = f"{LANE}.{PRODUCT}.test"
MOVE = f"/lanes/{PRODUCT}/{LANE}/move"
NEXT = {"run": LATER_RUN}
SAMPLED_EVERY = 0.05
SHA256 = UPDATING.odoo.sha256 if UPDATING.odoo else ""
# What the points before left for the points after: the witness as first read, and each write made after it.
kept: dict[str, Any] = {"written": []}
PSQL = "psql --username=postgres --dbname=app --quiet --tuples-only --no-align --set=ON_ERROR_STOP=1"
# noinspection SqlResolve
REFUSE_THE_PIPELINE = """
create function refuse_the_pipeline() returns trigger language plpgsql as
$$ begin raise exception 'the update watch lets no lead become an opportunity on this copy'; end $$;
create trigger refuse_the_pipeline before update of type on crm_lead
for each row when (new.type = 'opportunity') execute function refuse_the_pipeline();
"""


# By the scratch host's own clock: when the web address first answered as the back soon page, and when it first answered
# with success after that.
def sample(path: str) -> str:
    return (
        "first=; for each in $(seq 6000); do "
        f"code=$(curl --silent --output /dev/null --write-out '%{{http_code}}' --max-time 20 --header 'Host: {WEB_ADDRESS}' "
        f"http://127.0.0.1{path}); now=$(date +%s.%N); "
        'if [ -z "$first" ]; then [ "$code" != 503 ] || first=$now; elif [ "$code" = 200 ]; then echo "$first $now"; exit; fi; '
        f"sleep {SAMPLED_EVERY}; done; exit 1"
    )


# How long visitors got the back soon page at `path` while `release` ran: from its first answer to the first success after.
def back_soon_seconds[Answer](path: str, release: Callable[[], Answer]) -> tuple[float, Answer]:
    sampled: list[str] = []
    sampling = threading.Thread(target=lambda: sampled.append(on_scratch(sample(path))))
    sampling.start()
    answer = release()
    sampling.join()
    first_back_soon, first_served = (float(each) for each in sampled[0].split())
    return first_served - first_back_soon, answer


def said(point: str, **observed: Any) -> None:  # noqa: ANN401 whatever was observed
    print(f"\n{point}", *(f"  {name.replace('_', ' ')}: {value}" for name, value in observed.items()), sep="\n")  # noqa: T201


def site_status(path: str = "/web/login", lane: str = LANE) -> str:
    asked = (
        f"curl --silent --output /dev/null --write-out '%{{http_code}}' --max-time 20 --header 'Host: {lane}.{PRODUCT}.test' "
        f"http://127.0.0.1{path}"
    )
    return on_scratch(asked).strip()


# The lane's card as the page draws it: its pills, how its web address reads, and the note of one request.
def card(request: int | None = None, lane: str = LANE) -> dict[str, Any]:
    [found] = [each for each in ask("/running")[1]["cards"] if each["title"] == f"{PRODUCT} · {lane}"]
    rows = {row["name"]: row for row in found["rows"]}
    return {
        "reads": [pill["text"] for pill in found["pills"]],
        "web_address": [pill["text"] for pill in rows["Web address"]["pills"]],
        "instances": [[pill["text"] for pill in row["pills"]] for name, row in rows.items() if name.startswith("Instance")],
        "request": [pill["text"] for pill in rows.get(f"Request {request}", {}).get("pills", [])],
        "note": rows.get(f"Request {request}", {}).get("note"),
        "data_sets": [row["note"] for row in found["rows"] if row["name"] == "Data set"],
    }


def lines_of(request: dict[str, Any], begins: str) -> list[str]:
    return [each["did"] for each in request["events"] if each["did"].startswith(begins)]


def of_this_lane(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [each for each in parts if (each["product"], each["lane"]) == (PRODUCT, LANE)]


def candidate_of(request: dict[str, Any]) -> str:
    [candidate] = [
        each["container"] for each in of_this_lane(ask("/running")[1]["instances"]) if each["generation"] == request["generation"]
    ]
    return candidate


def copy_of(request: dict[str, Any]) -> dict[str, Any]:
    [copy] = [each for each in copies_of(PRODUCT, LANE) if each["generation"] == request["generation"]]
    return copy


def write(text: str) -> None:
    SignedIn(PRODUCT, LANE).write(text)
    kept["written"].append(f"{WRITTEN}{text}")


# The original instance, build and volumes serve the lead as first read, still a lead, with every write made after it.
def original_serves_everything_written() -> dict[str, str]:
    lane = read_lane(PRODUCT, LANE)
    assert {name: lane[name] for name in ("instance", "build", "volumes")} == kept["serving"], lane
    assert SignedIn(PRODUCT, LANE).witness() == kept["witness"]
    assert lane["written"].split(",") == kept["written"], lane
    return lane


def failed_with_no_switch(request: int, outcome: str) -> dict[str, Any]:
    ended = request_ended(request)
    assert (ended["stands"], ended["answer"]["outcome"], ended["release_verdict"]) == ("failed", outcome, "none"), ended
    assert lines_of(ended, "switch ") == []
    return ended


def clear_the_lane() -> None:
    retire_with_its_data(PRODUCT, LANE)
    remove_product_copies(PRODUCT)


# One seeded lane of TK on its first build, serving, with the invented lead on a team and its attachment. From there on its
# description has the check read that attachment, and has a release update TK's own module.
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
        ask_and_wait(MOVE, {"run": RUN})
        seed(PRODUCT, LANE)
        status, moved = ask_and_wait(MOVE, {"run": RUN})
        assert (status, moved["outcome"]) == (200, "switched"), moved
        SignedIn(PRODUCT, LANE).make_lead()
        describe(PRODUCT, UPDATING)
        yield
    finally:
        clear_the_lane()
        stop_recorded_git_host()
        on_control(f"rm -f /var/lib/launchplane/products/{PRODUCT}.json")


def test_1_the_original_serves_the_lead_on_its_team_with_its_attachment_s_bytes(lane: None) -> None:
    serving, witness = read_lane(PRODUCT, LANE), SignedIn(PRODUCT, LANE).witness()
    kept["serving"] = {name: serving[name] for name in ("instance", "build", "volumes")}
    kept["witness"] = witness
    kept["stored_as"] = SignedIn(PRODUCT, LANE).stored_as()
    said("1. Original", card=card(), **kept["serving"], witness=witness)
    assert (card()["reads"], card()["web_address"], card()["instances"]) == (["good"], ["answering"], [["running", "active"]])
    assert (serving["build"], len(serving["volumes"].split(","))) == (build_of(RUN), 2)
    assert (witness["type"], witness["sha256"], witness["attached_to"]) == ("lead", SHA256, f"crm.lead,{witness['lead']}")


def test_2_and_3_an_update_that_fails_on_the_copy_leaves_no_switch_and_the_original_serves_and_takes_a_write(lane: None) -> None:
    with launchplane_held():
        hold("before.host.create_data_set_network")
        status, asked = ask(MOVE, NEXT)
        assert status == 202, asked
        wait_until_stopped_at("before.host.create_data_set_network", seconds=120)
        frozen, copy = card(asked["id"]), copy_of(asked)
        in_the_copy = read_copy(copy)
        said(
            "2. Frozen and copied",
            answered=status,
            request=ask(f"/requests/{asked['id']}")[1]["stands"],
            visitor=site_status(),
            card=frozen,
            the_copy=in_the_copy,
        )
        assert (frozen["reads"], frozen["web_address"]) == (["in maintenance", "work under way"], ["shows the back soon page"])
        assert "Once both volumes are copied, the new build updates the copy, and is started on it only once" in frozen["note"]
        assert ([each["finished"] for each in copy["copies"]], copy["networks"], copy["databases"]) == ([True, True], [], [])
        assert (in_the_copy["lead"], in_the_copy["attachment"]) == ("lead", SHA256)
        assert not set(in_the_copy["volumes"].split(",")) & set(kept["serving"]["volumes"].split(","))

        hold("before.host.run_once")
        let_go("before.host.create_data_set_network")
        wait_until_stopped_at("before.host.run_once", seconds=120)
        server = f"{PRODUCT}-{LANE}-database-{copy['data_set']}"
        on_scratch(f"docker exec --interactive {server} {PSQL}", given=REFUSE_THE_PIPELINE)
        hold("before.host.start_database")
        let_go("before.host.run_once")
        wait_until_stopped_at("before.host.start_database", seconds=180)
        recovering = card(asked["id"])
        said("3. Update fails: recovering", visitor=site_status(), request=recovering["request"], note=recovering["note"])
        assert "update the data of " in recovering["note"] and "ended with code" in recovering["note"]
        assert "Launchplane is bringing the original instance back on its own data." in recovering["note"]
        let_go()
        ended = failed_with_no_switch(asked["id"], "not updated")
    write("after the update failed")
    restored = card(ended["id"])
    said("3. Update fails: restored", request=ended["stands"], error=ended["error"], card=restored)
    said("3. The original serves, and took a write", **original_serves_everything_written())
    assert "the update watch lets no lead become an opportunity on this copy" in ended["error"]
    assert (len(lines_of(ended, "update the data of ")), lines_of(ended, "check ")[0]) == (
        1,
        f"check {kept['serving']['instance']} at /web/login",
    )
    assert (restored["reads"], restored["web_address"], restored["request"]) == (["good"], ["answering"], ["failed"])


def test_4_a_check_that_fails_on_the_updated_copy_leaves_no_switch_and_the_original_keeps_its_lead_and_writes(lane: None) -> None:
    with launchplane_held():
        hold("before.proxy.check_instance")
        status, asked = ask(MOVE, NEXT)
        assert status == 202, asked
        wait_until_stopped_at("before.proxy.check_instance", seconds=180)
        candidate, copy = candidate_of(asked), copy_of(asked)
        updated, checking = witness_in(candidate), card(asked["id"])
        said(
            "4. Updated, before the check: the witness read by the candidate's own Odoo",
            candidate=candidate,
            witness=updated,
            visitor=site_status(),
            note=checking["note"],
        )
        assert updated == kept["witness"] | {"type": "opportunity"}
        assert "The copy is updated, and the new build is started on it and is being checked." in checking["note"]
        damage = f"printf damaged > /files/filestore/app/{kept['stored_as']}"
        on_scratch(f"docker run --rm --network none --volume {copy['files'][0]}:/files busybox:1.37 sh -c '{damage}'")
        # noinspection HttpUrlsUsage
        answers = on_scratch(f"docker exec launchplane-proxy wget -q -O /dev/null http://{candidate}/web/login && echo 200")
        said("4. Check fails: the attachment's file damaged on the copy alone", candidate_at_web_login=answers.strip())
        assert answers.strip() == "200"
        let_go()
        ended = failed_with_no_switch(asked["id"], "check failed")
    restored = card(ended["id"])
    said("4. Check fails: restored", request=ended["stands"], error=ended["error"].splitlines()[0], card=restored)
    assert f"The attachment {LEAD_FILE} of the database app must be there once with the SHA-256 {SHA256}" in ended["error"]
    assert "the check of its kind, run in it, failed" in restored["note"]
    assert (restored["reads"], restored["web_address"], restored["request"]) == (["good"], ["answering"], ["failed"])
    original_serves_everything_written()
    write("after the check failed")
    said("4. The original serves, and took a write", **original_serves_everything_written())


def update_runs_in(product: str) -> bool:
    web = f"docker ps --quiet --filter label=launchplane.product={product} --filter label=launchplane.part=web"
    return "found" in on_scratch(f"for each in $({web}); do docker top $each | grep -q -- --update && echo found; done; true")


def test_5_a_kill_during_the_update_gives_that_copy_up_and_the_original_serves_every_write(lane: None) -> None:
    status, asked = ask(MOVE, NEXT)
    assert status == 202, asked
    wait_for(lambda: update_runs_in(PRODUCT), seconds=120)
    kill_launchplane()
    ended = failed_with_no_switch(asked["id"], "not updated")
    restored = card(ended["id"])
    said("5. Killed during the update", attempts=[each["ended"] for each in ended["attempts"]], error=ended["error"], card=restored)
    assert [each["ended"] for each in ended["attempts"]] == ["interrupted", "failed"]
    assert "so whether its update ran is not known and its copy is not used" in ended["error"]
    assert (
        "whether its update ran is not known" in restored["note"] and "the lane serves from its own data again" in restored["note"]
    )
    assert (restored["reads"], restored["web_address"], restored["request"]) == (["good"], ["answering"], ["failed"])
    assert not update_runs_in(PRODUCT)
    original_serves_everything_written()
    write("after the kill")
    said("5. The original serves, and took a write", **original_serves_everything_written())


def size_of(volume: str) -> str:
    count = 'echo "$(find /copy -type f | wc -l) files, $(find /copy -type f -exec cat {} + | wc -c) bytes"'
    return on_scratch(f"docker run --rm --network none --volume {volume}:/copy:ro busybox:1.37 sh -c '{count}'").strip()


def test_6_an_uninterrupted_release_updates_once_switches_once_and_keeps_everything_written(lane: None) -> None:
    due = SignedIn(PRODUCT, LANE).due_job(make_due=True)
    old_database, old_files = kept["serving"]["volumes"].split(",")
    # noinspection SqlResolve
    database_bytes = on_scratch(
        f"docker exec {PRODUCT}-{LANE}-database-{old_database.split('-')[2]} {PSQL} --command \"select pg_database_size('app')\""
    ).strip()
    files = size_of(old_files)
    old_copies = sorted(each["data_set"] for each in copies_of(PRODUCT, LANE))
    key = {"Idempotency-Key": f"watch-update-after-{ask('/requests')[1]['ended'][0]['id']}"}
    behind_back_soon, (status, moved) = back_soon_seconds(
        "/web/login", lambda: ask(f"{MOVE}?wait=290", NEXT, timeout=300, headers=key)
    )
    assert (status, moved["outcome"]) == (200, "switched"), moved
    [ended] = [each for each in ask("/requests")[1]["ended"] if each["product"] == PRODUCT][:1]
    ended = ask(f"/requests/{ended['id']}")[1]
    after, witness, lane_card = read_lane(PRODUCT, LANE), SignedIn(PRODUCT, LANE).witness(), card(ended["id"])
    said(
        "6. Successful switch",
        back_soon_to_the_new_instance_s_first_answer=f"{behind_back_soon:.1f} s, sampled every {SAMPLED_EVERY} s",
        database=f"{database_bytes} bytes",
        files_volume=files,
        request=f"{ended['stands']}, {moved['outcome']}",
        card=lane_card,
        witness=witness,
        **after,
    )
    assert (after["build"], after["instance"]) == (build_of(LATER_RUN), moved["container_receiving_visitors"])
    assert not set(after["volumes"].split(",")) & {old_database, old_files}
    assert (len(lines_of(ended, "update the data of ")), len(lines_of(ended, "switch ")), ended["release_verdict"]) == (
        1,
        1,
        "committed",
    )
    assert witness == kept["witness"] | {"type": "opportunity"}
    assert after["written"].split(",") == kept["written"]
    assert SignedIn(PRODUCT, LANE).due_job() == due
    assert (lane_card["reads"], lane_card["web_address"], lane_card["instances"]) == (
        ["good"],
        ["answering"],
        [["running", "active"]],
    )

    running = ask("/running")[1]
    [instance] = of_this_lane(running["instances"])
    [used] = [each for each in of_this_lane(running["data_sets"]) if each["data_set"] == instance["data_set"]]
    [old] = [each for each in of_this_lane(running["data_sets"]) if old_database in each["volumes"]]
    quiet = {each["name"]: each["is_quiet"] for each in running["networks"]}
    assert (used["generation"], [each["source"] for each in instance["mounts"]]) == (ended["generation"], used["files"])
    assert [quiet[each] for each in instance["networks"]] == [True, True]
    assert (old["files"], [(each["state"], each["exit_code"]) for each in old["databases"]]) == ([old_files], [("stopped", 0)])

    gone = lines_of(ended, "delete the data set ")
    volumes = on_scratch(f"docker volume ls --quiet --filter label=launchplane.product={PRODUCT}").split()
    said(
        "6. The older copies are gone",
        deleted=gone,
        data_sets_on_the_card=lane_card["data_sets"],
        volumes_on_the_scratch_host=volumes,
    )
    assert (len(old_copies), sorted(gone)) == (3, [f"delete the data set {each}" for each in old_copies])
    assert sorted(each["data_set"] for each in of_this_lane(running["data_sets"])) == sorted([used["data_set"], old["data_set"]])
    assert sorted(volumes) == sorted([*used["volumes"], *used["files"], *old["volumes"], *old["files"]])
    assert sorted(each.split(" ", 1)[1] for each in lane_card["data_sets"]) == [
        "Kept: the lane serves from it.",
        "Kept: the last release replaced it.",
    ]

    write("after the switch")
    replayed = ask(MOVE, NEXT, headers=key)[1]
    unchanged = request_ended(ask(MOVE, NEXT)[1]["id"])
    said(
        "6. The key sent again, then the serving build asked for again",
        request_that_switched=ended["id"],
        the_key_answers_request=replayed["id"],
        asked_again=f"request {unchanged['id']}, {unchanged['answer']['outcome']}, {len(unchanged['events'])} lines",
        card=card()["reads"],
    )
    assert (replayed["id"], replayed["stands"]) == (ended["id"], "done")
    assert (unchanged["id"] != ended["id"], unchanged["answer"]["outcome"], unchanged["events"]) == (True, "unchanged", [])
    assert len(lines_of(ask(f"/requests/{ended['id']}")[1], "update the data of ")) == 1
    assert read_lane(PRODUCT, LANE)["written"].split(",") == kept["written"]
    assert card()["reads"] == ["good"]


FRESH = "fresh"
PARTNER = "Written to the fixture "
# noinspection SqlResolve
REFUSE_THE_FIELD = """
create function refuse_the_field() returns event_trigger language plpgsql as
$$ begin raise exception 'the update watch lets no table be altered in this database'; end $$;
create event trigger refuse_the_field on ddl_command_start when tag in ('ALTER TABLE') execute function refuse_the_field();
"""
ALLOW_THE_FIELD = "drop event trigger refuse_the_field; drop function refuse_the_field();"
# noinspection SqlResolve
READ_THE_FIELD = """
select coalesce((select 'column ' || column_name || ', ' || data_type from information_schema.columns
    where (table_name, column_name) = ('website', :'field')), 'no column ' || :'field');
"""


def in_database(data_set: str, statements: str, lane: str = LANE) -> str:
    asked = f"docker exec --interactive {PRODUCT}-{lane}-database-{data_set} {PSQL} --set=field={FIELD}"
    return on_scratch(asked, given=statements).strip()


def serving_data_set(lane: str = LANE) -> str:
    [instance] = [each for each in ask("/running")[1]["instances"] if each["receives_visitors_for"] == f"{lane}.{PRODUCT}.test"]
    return instance["data_set"]


# Every container the host has of one release, running or not, each with how often Docker started it again and since when.
def containers_of(request: dict[str, Any]) -> list[str]:
    listed = f"docker ps --all --quiet --filter label=launchplane.generation={request['generation']}"
    shown = "{{.Name}} {{.State.Status}} restarts={{.RestartCount}} started={{.State.StartedAt}} {{.Path}} {{.Args}}"
    return on_scratch(f"for each in $({listed}); do docker inspect --format '{shown}' $each; done").splitlines()


def partners_written() -> list[str]:
    found = SignedIn(PRODUCT, LANE).call(
        "res.partner", "search_read", [("name", "like", f"{PARTNER}%")], fields=["name"], order="id"
    )
    return [each["name"] for each in found]


# The fixture's normal initialization of a new lane: its data set made by a first move, its modules installed by the
# build's own `--init`, and then the move that starts it.
def bootstrap(lane: str, run: int, build: str) -> dict[str, Any]:
    move = f"/lanes/{PRODUCT}/{lane}/move"
    ask_and_wait(move, {"run": run})
    seed(PRODUCT, lane, FIXTURE.odoo.modules if FIXTURE.odoo else [], build=build)
    status, moved = ask_and_wait(move, {"run": run})
    assert (status, moved["outcome"], moved["build"]) == (200, "switched", build), moved
    return moved


# The watch's lane made again from the fixture's old build, with both of its builds on the scratch host.
@pytest.fixture(scope="module")
def builds(lane: None) -> Iterator[dict[int, str]]:
    clear_the_lane()
    made = publish_builds()
    serve_recorded_git_host()
    describe(PRODUCT, FIXTURE)
    try:
        kept["original"] = bootstrap(LANE, OLD_RUN, made[OLD_RUN])["container_receiving_visitors"]
        yield made
    finally:
        retire_with_its_data(PRODUCT, FRESH)
        clear_the_lane()
        remove_builds()


def test_7_the_old_build_serves_the_website_route_on_data_that_lacks_the_field(builds: dict[int, str]) -> None:
    serving, field = read_fixture_lane(), in_database(serving_data_set(), READ_THE_FIELD)
    said("7. Original", card=card(), website=site_status("/"), field_in_its_database=field, **serving)
    assert (card()["reads"], card()["web_address"], card()["instances"]) == (["good"], ["answering"], [["running", "active"]])
    assert (site_status("/"), field, serving["build"]) == ("200", f"no column {FIELD}", builds[OLD_RUN])


def read_fixture_lane() -> dict[str, str]:
    running = ask("/running")[1]
    [instance] = [each for each in running["instances"] if each["receives_visitors_for"] == WEB_ADDRESS]
    [data_set] = [each for each in running["data_sets"] if each["data_set"] == instance["data_set"]]
    return {
        "instance": instance["container"],
        "build": instance["build"],
        "volumes": ",".join((*data_set["volumes"], *data_set["files"])),
    }


def test_8_an_update_that_fails_starts_no_web_process_of_the_new_build_and_the_old_build_serves_and_takes_a_write(
    builds: dict[int, str],
) -> None:
    original = read_fixture_lane()
    in_database(serving_data_set(), REFUSE_THE_FIELD)
    behind_back_soon, (status, ended) = back_soon_seconds("/", lambda: ask(f"{MOVE}?wait=290", {"run": NEW_RUN}, timeout=300))
    in_database(serving_data_set(), ALLOW_THE_FIELD)
    [ended] = [each for each in ask("/requests")[1]["ended"] if each["product"] == PRODUCT][:1]
    ended = failed_with_no_switch(ended["id"], "not updated")
    SignedIn(PRODUCT, LANE).call("res.partner", "create", [{"name": f"{PARTNER}after its update failed"}])
    restored = card(ended["id"])
    said(
        "8. The fixture's update fails",
        answered=status,
        back_soon_to_the_original_s_first_answer=f"{behind_back_soon:.1f} s, sampled every {SAMPLED_EVERY} s",
        error=ended["error"],
        started=lines_of(ended, "start "),
        left_of_the_release=containers_of(ended),
        card=restored,
        website=site_status("/"),
        written=partners_written(),
        **read_fixture_lane(),
    )
    assert "the update watch lets no table be altered in this database" in ended["error"]
    assert [each for each in lines_of(ended, "start ") if "-update" not in each] == [
        f"start the database {PRODUCT}-{LANE}-database-{serving_data_set()}",
        f"start {original['instance']}",
    ]
    assert (containers_of(ended), read_fixture_lane(), site_status("/")) == ([], original, "200")
    assert (restored["reads"], restored["web_address"], restored["request"]) == (["good"], ["answering"], ["failed"])
    assert partners_written() == [f"{PARTNER}after its update failed"]


def test_9_the_update_is_committed_before_the_first_web_process_of_the_new_build_which_then_serves_the_website_route(
    builds: dict[int, str],
) -> None:
    with launchplane_held():
        hold("before.host.run_once")
        status, asked = ask(MOVE, {"run": NEW_RUN})
        assert status == 202, asked
        wait_until_stopped_at("before.host.run_once", seconds=120)
        copy, updating = copy_of(asked), card(asked["id"])
        said("9. Copied, before the update", visitor=site_status("/"), card=updating, of_the_release=containers_of(asked))
        assert (updating["reads"], site_status("/"), containers_of(asked)) == (["in maintenance", "work under way"], "503", [])
        assert "the new build is updating the copy. Nothing of the new build is started on it until" in updating["note"]

        hold("before.host.start_instance")
        let_go("before.host.run_once")
        wait_until_stopped_at("before.host.start_instance", seconds=180)
        committed = in_database(copy["data_set"], READ_THE_FIELD)
        said(
            "9. Updated, before the first web start: the field read over a connection of its own",
            field_in_the_copy=committed,
            field_in_the_original=in_database(serving_data_set(), READ_THE_FIELD),
            visitor=site_status("/"),
            of_the_release=containers_of(asked),
            update_lines=lines_of(ask(f"/requests/{asked['id']}")[1], "update the data of "),
        )
        assert (committed, containers_of(asked), site_status("/")) == (f"column {FIELD}, character varying", [], "503")

        hold("before.proxy.check_instance")
        let_go("before.host.start_instance")
        wait_until_stopped_at("before.proxy.check_instance", seconds=120)
        candidate, checking = candidate_of(asked), card(asked["id"])
        # noinspection HttpUrlsUsage
        wait_for(
            lambda: "200" in on_scratch(f"docker exec launchplane-proxy wget -q -O /dev/null http://{candidate}/ && echo 200; true"),
            60,
        )
        [first] = containers_of(asked)
        said("9. Started once, before the check", first_web_process=first, visitor=site_status("/"), note=checking["note"])
        assert (first.split()[:3], site_status("/")) == ([f"/{candidate}", "running", "restarts=0"], "503")
        assert "The copy is updated, and the new build is started on it and is being checked." in checking["note"]
        let_go()
        ended = request_ended(asked["id"])
    after, lane_card = read_fixture_lane(), card(ended["id"])
    said("9. Switched", request=ended["stands"], website=site_status("/"), still=containers_of(asked), card=lane_card, **after)
    assert (ended["stands"], ended["answer"]["outcome"], len(lines_of(ended, "switch "))) == ("done", "switched", 1)
    assert (containers_of(asked), after["instance"], after["build"]) == ([first], candidate, builds[NEW_RUN])
    assert (site_status("/"), lane_card["reads"], lane_card["web_address"]) == ("200", ["good"], ["answering"])
    assert partners_written() == [f"{PARTNER}after its update failed"]


def test_10_the_new_build_serves_again_after_a_restart_and_on_a_database_it_initialized_itself(builds: dict[int, str]) -> None:
    serving = read_fixture_lane()["instance"]
    on_scratch(f"docker restart {serving}")
    wait_for(lambda: site_status("/") == "200", seconds=60)
    said("10. Restarted on the updated database", website=site_status("/"), card=card()["reads"], **read_fixture_lane())
    assert (card()["reads"], read_fixture_lane()["instance"]) == (["good"], serving)

    bootstrap(FRESH, NEW_RUN, builds[NEW_RUN])
    fresh = card(lane=FRESH)
    said("10. A fresh database, initialized by the new build", website=site_status("/", FRESH), card=fresh)
    assert (site_status("/", FRESH), fresh["reads"], fresh["web_address"]) == ("200", ["good"], ["answering"])
    assert in_database(serving_data_set(FRESH), READ_THE_FIELD, FRESH) == f"column {FIELD}, character varying"
