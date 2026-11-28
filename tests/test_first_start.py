import json
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from launchplane.act import Lanes
from launchplane.api import create_app
from launchplane.docker_answers import ContainerHostError
from launchplane.host_secrets import HostSecrets
from launchplane.instance import InstanceState, LaneBuild
from launchplane.judge import Condition
from launchplane.odoo import FILES_PATH, SERVER, TEST_PASSWORD_VARIABLE
from launchplane.product import Product, ProductError, ProductFolder, SecretFolder
from launchplane.record import Gate
from launchplane.steps import Outcome
from launchplane.worker import Worker
from tests.odoo_product import ATTACHMENT, ATTACHMENT_BYTES, MODULES, TEST_LOGIN, TK, TK_STARTING
from tests.pretend_host import EARLIER, GENERATION, WEB_ADDRESS, LaunchplaneKilled, PretendHost, PretendProxy, numbered_names
from tests.pretend_record import PretendRecord
from tests.test_act import BUILD_A, BUILD_B

LATER = "01900000-0000-7000-8000-00000000000c"
PRODUCTION = LaneBuild(product="site", lane="production", build="site:a")
CANARY = "canary-of-the-test-0123456789abcdefghijklmnopqrstuv"
# What a first move of the lane does on the host before its first start, and with its first start.
BEFORE_THE_FIRST_START = 5
THROUGH_THE_SWITCH = BEFORE_THE_FIRST_START + 4


class Noted(HostSecrets):
    def read_secrets(self) -> frozenset[str]:
        return self.seen

    def secrets_seen(self) -> frozenset[str]:
        return self.seen


def described_as(product: Product) -> Callable[[str], Product]:
    return lambda _product: product


# Whether a text carries a secret, said as yes or no so that no failing test prints the secret it looked for.
def carries(secret: str, *said: object) -> bool:
    return any(secret in str(each) for each in said)


@dataclass
class Bed:
    proxy: PretendProxy
    lanes: Lanes
    kept: SecretFolder
    noted: Noted

    @property
    def host(self) -> PretendHost:
        return self.proxy.host

    def first_starts(self) -> list[str]:
        return [each for each in self.host.actions if each.startswith("first start ")]

    def web_starts(self) -> list[str]:
        return [each for each in self.host.actions if each.startswith("start site-")]

    def serving_data_set(self) -> str | None:
        return self.host.instances[self.proxy.loaded_to(WEB_ADDRESS)].data_set

    def password_is_kept(self) -> bool:
        return self.kept.path("site", "testing").exists()

    # Each request is worked within the call that asked for it, and what it says is cleaned of what was noted.
    def asked_through_the_api(self) -> tuple[TestClient, PretendRecord]:
        record = PretendRecord(gate=Gate(self.noted))
        self.lanes.read_unfinished, self.lanes.read_ended = record.unfinished, record.ended_lately
        worker = Worker(record, self.lanes, self.noted)
        return TestClient(create_app(self.lanes.read_status, record, worker.worked_one, self.noted, self.lanes)), record


@pytest.fixture
def bed(tmp_path: Path) -> Bed:
    proxy, noted = PretendProxy(PretendHost()), Noted()
    kept = SecretFolder(tmp_path / "secrets", noted.note)
    lanes = Lanes(proxy.host, proxy, described_as(TK_STARTING), numbered_names(), passwords=kept)
    return Bed(proxy, lanes, kept, noted)


def test_the_first_move_runs_the_first_start_on_the_data_set_it_made_and_starts_the_web_part_only_after_it(bed: Bed) -> None:
    result = bed.lanes.move(BUILD_A, GENERATION, 1)
    new = bed.proxy.loaded_to(WEB_ADDRESS)
    [first_start] = bed.first_starts()
    assert bed.host.actions[-4:] == [first_start, f"start {new}", f"check {new}/web/login", f"route {new}"]
    assert (result.outcome, result.data_set, list(bed.host.instances)) == (Outcome.SWITCHED, "data-1", [new])
    [ran] = bed.host.data_sets["data-1"].first_starts
    assert (ran.seeded, set(bed.host.sign_ins), bed.password_is_kept()) == (True, {new}, True)
    assert [each == bed.host.given_once[0] for each in bed.host.signed_in_with] == [True] * len(bed.host.sign_ins)
    assert [each.condition for each in bed.lanes.read_status().lanes] == [Condition.GOOD]


def test_a_first_start_that_fails_starts_no_web_part_leaves_no_route_and_sets_its_data_set_aside_with_its_server_stopped(
    bed: Bed,
) -> None:
    bed.host.updates_that_fail = {BUILD_A.build}
    result = bed.lanes.move(BUILD_A, GENERATION, 1)
    assert (result.outcome, result.container_receiving_visitors) == (Outcome.NOT_INITIALIZED, None)
    assert (result.error or "").endswith("-first-start, ended with code 1.\nIts last log lines:\nmodule not found")
    assert (bed.web_starts(), bed.proxy.written, bed.proxy.loaded, list(bed.host.instances)) == ([], {}, {}, [])
    aside = bed.host.data_sets["data-1"]
    assert (aside.is_set_aside, aside.databases[0].state, aside.volumes, aside.files) == (
        True,
        InstanceState.STOPPED,
        ("volume-data-1",),
        ("files-data-1",),
    )


def test_the_ask_after_a_failed_first_start_serves_from_a_new_data_set_and_the_one_set_aside_is_kept_as_it_was(bed: Bed) -> None:
    bed.host.updates_that_fail = {BUILD_A.build}
    bed.lanes.move(BUILD_A, GENERATION, 1)
    aside = bed.host.data_sets["data-1"]
    bed.host.updates_that_fail, bed.host.actions = set(), []
    result = bed.lanes.move(BUILD_A, LATER, 1)
    assert (result.outcome, result.data_set != "data-1", len(bed.first_starts())) == (Outcome.SWITCHED, True, 1)
    assert (bed.host.data_sets["data-1"], [each for each in bed.host.actions if "data-1" in each.split()]) == (aside, [])
    retired = bed.lanes.retire("site", "testing")
    back = bed.lanes.move(BUILD_A, GENERATION, 1)
    assert (sorted(retired.kept_data_sets), back.data_set, len(bed.first_starts())) == (
        sorted(bed.host.data_sets),
        result.data_set,
        1,
    )


@pytest.mark.parametrize("killed_after", range(1, THROUGH_THE_SWITCH + 1))
@pytest.mark.parametrize("left_running_seeds", [True, False])
def test_a_move_killed_anywhere_ends_with_one_lane_serving_from_one_seed_and_never_seeds_a_data_set_twice(
    bed: Bed, killed_after: int, left_running_seeds: bool
) -> None:
    bed.host.killed_after, bed.host.left_running_seeds = killed_after, left_running_seeds
    with pytest.raises(LaunchplaneKilled):
        bed.lanes.move(BUILD_A, GENERATION, 1)
    bed.host.killed_after = None
    result = bed.lanes.move(BUILD_A, GENERATION, 2)
    seeded = [name for name, each in bed.host.data_sets.items() if [ran.seeded for ran in each.first_starts] == [True]]
    assert (result.outcome, seeded, len(bed.host.instances)) == (Outcome.SWITCHED, [bed.serving_data_set()], 1)
    assert all(len(each.first_starts) <= 1 for each in bed.host.data_sets.values())
    cut_short_while_seeding = killed_after == BEFORE_THE_FIRST_START + 1 and not left_running_seeds
    assert (len(bed.host.data_sets), bed.serving_data_set() != "data-1") == ((2, True) if cut_short_while_seeding else (1, False))


@pytest.mark.parametrize("left_running_seeds", [True, False])
def test_a_first_start_whose_reply_was_lost_is_waited_for_and_never_stopped_or_run_again_where_it_was(
    bed: Bed, left_running_seeds: bool
) -> None:
    bed.host.updates_unanswered.add(BUILD_A.build)
    bed.host.left_running_seeds = left_running_seeds
    with pytest.raises(ContainerHostError, match="the reply was lost"):
        bed.lanes.move(BUILD_A, GENERATION, 1)
    [left] = [each.removeprefix("first start ") for each in bed.first_starts()]
    bed.host.updates_unanswered.clear()
    bed.host.actions.clear()
    result = bed.lanes.move(BUILD_A, GENERATION, 2)
    assert (bed.host.actions[0], result.outcome) == (f"await first start {left}", Outcome.SWITCHED)
    assert (bed.serving_data_set() == "data-1", len(bed.first_starts())) == ((True, 0) if left_running_seeds else (False, 1))
    assert [each for each in bed.host.actions if each.startswith(("stop ", "retire ")) and left in each] == []


def test_a_data_set_this_request_did_not_make_is_started_on_as_it_is_whatever_it_lacks(bed: Bed) -> None:
    bed.lanes.read_product = described_as(TK)
    bed.lanes.move(BUILD_A, GENERATION, 1)
    bed.lanes.retire("site", "testing")
    bed.lanes.read_product, bed.host.actions = described_as(TK_STARTING), []
    result = bed.lanes.move(BUILD_A, LATER, 1)
    assert (bed.first_starts(), len(bed.web_starts()), list(bed.host.data_sets)) == ([], 1, ["data-1"])
    assert (result.outcome, "has no password kept on the control machine" in (result.error or "")) == (Outcome.CHECK_FAILED, True)


def test_a_lane_that_serves_is_given_no_first_start_by_another_build_and_only_the_lane_testing_ever_is(bed: Bed) -> None:
    bed.lanes.move(BUILD_A, GENERATION, 1)
    assert bed.lanes.move(PRODUCTION, EARLIER, 1).outcome is Outcome.SWITCHED
    bed.host.actions.clear()
    bed.host.sign_ins.clear()
    released = bed.lanes.move(BUILD_B, LATER, 1)
    assert (released.outcome, bed.first_starts(), len(bed.host.given_once)) == (Outcome.SWITCHED, [], 1)
    assert bed.proxy.loaded_to(WEB_ADDRESS) in bed.host.sign_ins and not any("production" in each for each in bed.host.sign_ins)


def test_a_candidate_the_testing_login_does_not_open_never_takes_the_route_and_no_password_is_written(bed: Bed) -> None:
    bed.lanes.move(BUILD_A, GENERATION, 1)
    serving, password = bed.proxy.loaded_to(WEB_ADDRESS), bed.kept.kept("site", "testing")
    bed.host.sign_ins_refused = {f"site-testing-{number}" for number in range(100)} - {serving}
    result = bed.lanes.move(BUILD_B, LATER, 1)
    assert (result.outcome, bed.proxy.loaded_to(WEB_ADDRESS), list(bed.host.instances)) == (Outcome.CHECK_FAILED, serving, [serving])
    assert "The testing login of testing of site does not open site-testing-" in (result.error or "")
    assert (bed.kept.kept("site", "testing") == password, len(bed.host.given_once)) == (True, 1)


def test_the_same_build_asked_again_is_not_unchanged_while_the_testing_login_does_not_open_what_serves(bed: Bed) -> None:
    bed.lanes.move(BUILD_A, GENERATION, 1)
    serving = bed.proxy.loaded_to(WEB_ADDRESS)
    bed.host.actions.clear()
    assert (bed.lanes.move(BUILD_A, LATER, 1).outcome, bed.host.actions) == (Outcome.UNCHANGED, [f"check {serving}/web/login"])
    bed.host.sign_ins_refused = {serving}
    result = bed.lanes.move(BUILD_A, LATER, 2)
    assert (result.outcome, bed.proxy.loaded_to(WEB_ADDRESS), list(bed.host.instances)) == (Outcome.CHECK_FAILED, serving, [serving])
    assert len(bed.host.given_once) == 1


def test_a_lane_whose_password_is_not_kept_is_not_good_and_no_check_or_reading_makes_one(bed: Bed) -> None:
    bed.lanes.move(BUILD_A, GENERATION, 1)
    bed.kept.path("site", "testing").unlink()
    [lane] = bed.lanes.read_status().lanes
    [why] = [each.sentence for each in lane.findings]
    assert (lane.condition is Condition.GOOD, "has no password kept on the control machine" in why) == (False, True)
    assert (bed.lanes.move(BUILD_A, LATER, 1).outcome, bed.lanes.move(BUILD_B, LATER, 2).outcome) == (
        Outcome.CHECK_FAILED,
        Outcome.CHECK_FAILED,
    )
    assert (bed.password_is_kept(), len(bed.host.given_once)) == (False, 1)


def test_a_lane_is_not_good_while_odoo_refuses_its_testing_login_where_it_serves(bed: Bed) -> None:
    bed.lanes.move(BUILD_A, GENERATION, 1)
    bed.host.sign_ins_refused = {bed.proxy.loaded_to(WEB_ADDRESS)}
    [lane] = bed.lanes.read_status().lanes
    assert lane.condition is not Condition.GOOD
    assert [each.sentence.endswith("Odoo refused the testing login.") for each in lane.findings] == [True]


def test_a_first_start_that_fails_saying_its_password_leaves_it_out_of_the_answer_the_request_and_the_card(bed: Bed) -> None:
    password = bed.kept.made("site", "testing")
    bed.host.updates_that_fail = {"site:b"}
    bed.host.failure_says = f"psycopg2.OperationalError: could not write the user {TEST_LOGIN} with password {password}"
    asking, record = bed.asked_through_the_api()
    answer = asking.post("/lanes/site/testing/move?wait=1", json={"build": "site:b"})
    said = (answer.text, asking.get("/requests/1").text, asking.get("/running").text, record.kept)
    assert (answer.status_code, answer.json()["outcome"], carries(password, *said)) == (422, "not initialized", False)
    [card] = asking.get("/running").json()["cards"]
    [found] = [row for row in card["rows"] if row["name"] == "Found"]
    assert ([pill["text"] for pill in card["pills"]], [pill["text"] for pill in found["pills"]]) == (
        ["move failed"],
        ["move failed"],
    )
    assert "The data set it began in is set aside and kept, and asking again starts on a new one." in found["note"]
    [kept] = [row["note"] for row in card["rows"] if row["name"] == "Data set"]
    assert "Set aside: a first start began in it and did not finish. It is kept, and no move starts on it." in kept


def test_a_sign_in_that_fails_saying_its_password_leaves_it_out_of_the_answer_the_request_and_the_card(
    bed: Bed, monkeypatch: pytest.MonkeyPatch
) -> None:
    bed.lanes.move(BUILD_A, GENERATION, 1)
    run_in = bed.host.run_in

    def refuse_saying_the_password(container: str, command: list[str], given: dict[str, str]) -> object:
        return SimpleNamespace(
            succeeded=run_in(container, command, given).exit_code > 0, output=f"refused {given[TEST_PASSWORD_VARIABLE]}"
        )

    monkeypatch.setattr(bed.host, "run_in", refuse_saying_the_password)
    asking, record = bed.asked_through_the_api()
    answer = asking.post("/lanes/site/testing/move?wait=1", json={"build": BUILD_A.build})
    said = (answer.text, asking.get("/requests/1").text, asking.get("/running").text, record.kept)
    password = bed.kept.kept("site", "testing") or ""
    assert (answer.status_code, answer.json()["outcome"], carries(password, *said)) == (422, "check failed", False)


def test_a_password_is_made_once_kept_where_only_launchplane_reads_it_and_on_the_disk_before_anybody_is_given_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    fsync = os.fsync

    def note_and_sync(opened: int) -> None:
        order.append("synced")
        fsync(opened)

    monkeypatch.setattr(os, "fsync", note_and_sync)
    kept = SecretFolder(tmp_path, lambda _secrets: order.append("noted"))
    assert kept.kept("site", "testing") is None
    password = kept.made("site", "testing")
    assert (order, stat.S_IMODE(kept.path("site", "testing").stat().st_mode), len(password) >= 32) == (
        ["synced", "synced", "noted"],
        0o600,
        True,
    )
    assert (kept.made("site", "testing"), kept.kept("site", "testing"), order[3:]) == (password, password, ["noted", "noted"])
    assert len({password, kept.made("other", "testing"), kept.made("site", "preview")}) == 3


def test_every_password_kept_is_noted_when_launchplane_starts(tmp_path: Path) -> None:
    earlier = SecretFolder(tmp_path, lambda _secrets: None)
    kept = {earlier.made("site", "testing"), earlier.made("shop", "testing")}
    noted = Noted()
    SecretFolder(tmp_path, noted.note).note_all()
    assert noted.seen == kept


def test_a_password_that_cannot_be_kept_stops_the_move_before_its_first_start_is_run(bed: Bed) -> None:
    bed.kept.folder.write_text("a file where the folder must be")
    with pytest.raises(ProductError, match="Keeping the password of the testing login of testing of site failed"):
        bed.lanes.move(BUILD_A, GENERATION, 1)
    assert (bed.first_starts(), bed.web_starts(), bed.proxy.written) == ([], [], {})


def test_a_description_whose_first_start_gives_other_bytes_than_its_check_reads_is_not_read(tmp_path: Path) -> None:
    described = TK_STARTING.model_dump(mode="json", exclude_defaults=True)
    described["odoo"]["sha256"] = "0" * 64
    (tmp_path / "site.json").write_text(json.dumps(described))
    with pytest.raises(ProductError, match="attachment does not hold the bytes whose SHA-256 is sha256"):
        ProductFolder(tmp_path).read("site")


# psql answers how much the database holds, and the files folder holds what the test puts there.
PSQL = """#!/bin/sh
[ "$HELD" = unreadable ] && exit 2
echo "$HELD"
"""
TIMEOUT = """#!/bin/sh
shift
exec "$@"
"""
ODOO = """#!/bin/sh
echo "odoo $*" >> "$CALLS"
[ "$1" = shell ] && cat > "$SEED"
[ "$1" = --init ] && exit "$INIT_ENDS"
exit 0
"""
INSTALL = f"odoo --init {','.join(MODULES)} --stop-after-init --no-http"
SEED = "odoo shell --no-http --log-level=error"


# The first start as the kind's build runs it, with what it calls stood in for. `calls` is each run of Odoo, in order.
@dataclass
class Ran:
    folder: Path
    exit_code: int = 0
    said: str = ""

    @property
    def calls(self) -> list[str]:
        calls = self.folder / "calls"
        return calls.read_text().splitlines() if calls.exists() else []

    @property
    def seed(self) -> str:
        return (self.folder / "seed").read_text()


def run_first_start(folder: Path, held: str = "0", files: tuple[str, ...] = (), init_ends: int = 0) -> Ran:
    for name, text in (("psql", PSQL), ("timeout", TIMEOUT), ("odoo-bin", ODOO)):
        (folder / name).write_text(text)
        (folder / name).chmod(0o755)
    (folder / "files").mkdir()
    for name in files:
        (folder / "files" / name).touch()
    shell, flag, script = TK_STARTING.first_start("testing")
    given = {"PATH": f"{folder}:{os.environ['PATH']}", "HELD": held, "INIT_ENDS": str(init_ends)}
    given |= {"CALLS": str(folder / "calls"), "SEED": str(folder / "seed")}
    script = script.replace(SERVER, str(folder / "odoo-bin")).replace(FILES_PATH, str(folder / "files"))
    ran = subprocess.run([shell, flag, script], env=given, capture_output=True, text=True, check=False)  # noqa: S603
    return Ran(folder, ran.returncode, ran.stdout)


# Odoo as the seed uses it. What it did is kept in order: each record made, by whom, each password set, and the commit.
@dataclass
class OdooAsSeeded:
    refuses_users: bool = False
    did: list[object] = field(default_factory=list[object])
    model: str = ""
    user: str = "the superuser"
    id: int = 7

    @property
    def cr(self) -> SimpleNamespace:
        return SimpleNamespace(commit=lambda: self.did.append("commit"))

    def __getitem__(self, model: str) -> OdooAsSeeded:
        return OdooAsSeeded(self.refuses_users, self.did, model)

    def ref(self, name: str) -> OdooAsSeeded:
        return OdooAsSeeded(self.refuses_users, self.did, name)

    def with_user(self, user: OdooAsSeeded) -> OdooAsSeeded:
        return OdooAsSeeded(self.refuses_users, self.did, self.model, user.user)

    def create(self, values: dict[str, Any]) -> OdooAsSeeded:
        if self.model == "res.users" and self.refuses_users:
            raise ValueError(f"the login may not be made with {values['password']}")
        self.did.append((self.model, self.user, {name: value for name, value in values.items() if name != "password"}))
        return OdooAsSeeded(self.refuses_users, self.did, self.model, values.get("login", ""))

    def __setattr__(self, name: str, value: object) -> None:
        if name == "password":
            self.did.append((self.model, "password", len(str(value)) >= 32 and value != os.environ[TEST_PASSWORD_VARIABLE]))
        else:
            object.__setattr__(self, name, value)


def seeded(script: str, odoo: OdooAsSeeded, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    monkeypatch.setenv(TEST_PASSWORD_VARIABLE, CANARY)
    exec(script, {"env": odoo})  # noqa: S102 the kind's own seed script
    return odoo.did


def test_an_empty_data_set_gets_the_modules_and_then_in_one_commit_the_tester_its_attachment_and_no_factory_sign_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = run_first_start(tmp_path)
    assert (ran.exit_code, ran.calls) == (0, [INSTALL, SEED])
    groups = [(6, 0, [7, 7])]
    assert seeded(ran.seed, OdooAsSeeded(), monkeypatch) == [
        ("res.users", "the superuser", {"name": TEST_LOGIN, "login": TEST_LOGIN, "group_ids": groups}),
        ("ir.attachment", TEST_LOGIN, {"name": ATTACHMENT, "raw": ATTACHMENT_BYTES, "mimetype": "application/octet-stream"}),
        ("base.user_admin", "password", True),
        "commit",
    ]


def test_a_seed_that_fails_naming_the_password_says_why_without_it_and_commits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    odoo = OdooAsSeeded(refuses_users=True)
    with pytest.raises(SystemExit) as failed:
        seeded(run_first_start(tmp_path).seed, odoo, monkeypatch)
    assert (carries(CANARY, failed.value), "the login may not be made with [the password]" in str(failed.value), odoo.did) == (
        False,
        True,
        [],
    )


@pytest.mark.parametrize(("held", "files"), [("1", ()), ("734", ()), ("0", ("filestore",)), ("3", ("a", "b"))])
def test_a_data_set_that_holds_any_table_or_any_file_is_never_written_to_and_ends_the_first_start(
    tmp_path: Path, held: str, files: tuple[str, ...]
) -> None:
    ran = run_first_start(tmp_path, held, files)
    assert (ran.exit_code, ran.calls, sorted(each.name for each in (tmp_path / "files").iterdir())) == (1, [], sorted(files))
    assert f"holds {held} tables and indexes, and its files volume holds: {chr(10).join(sorted(files)) or 'nothing'}." in ran.said


def test_a_database_that_cannot_be_asked_what_it_holds_is_not_initialized(tmp_path: Path) -> None:
    ran = run_first_start(tmp_path, held="unreadable")
    assert (ran.exit_code, ran.calls) == (2, [])


def test_modules_that_do_not_install_end_the_first_start_before_anything_is_seeded(tmp_path: Path) -> None:
    ran = run_first_start(tmp_path, init_ends=3)
    assert (ran.exit_code, ran.calls) == (3, [INSTALL])


# Odoo's sign-in as an instance answers it: a session for the right password, an error for any other.
@pytest.fixture
def odoo_answering() -> Iterator[int]:
    class SignIn(BaseHTTPRequestHandler):
        # noinspection PyPep8Naming
        def do_POST(self) -> None:
            asked = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["params"]
            opened = (self.path, asked["db"], asked["login"], asked["password"]) == (
                "/web/session/authenticate",
                "app",
                TEST_LOGIN,
                CANARY,
            )
            body = json.dumps({"result": {"uid": 6}} if opened else {"error": {"data": {"message": f"denied {asked['password']}"}}})
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), SignIn)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_port
    server.shutdown()


def sign_in(port: int, password: str) -> tuple[int, str]:
    _, flag, script = TK_STARTING.sign_in("testing")
    command = [sys.executable, flag, script.replace("127.0.0.1:80", f"127.0.0.1:{port}")]
    ran = subprocess.run(command, env={TEST_PASSWORD_VARIABLE: password}, capture_output=True, text=True, check=False)  # noqa: S603
    return ran.returncode, ran.stdout + ran.stderr


def test_the_sign_in_succeeds_only_with_a_session_and_says_no_password_when_it_fails(odoo_answering: int) -> None:
    refused, nobody_there = sign_in(odoo_answering, "another password"), sign_in(1, CANARY)
    assert (sign_in(odoo_answering, CANARY), refused[0], nobody_there[0]) == ((0, ""), 1, 1)
    assert (refused[1].strip(), nobody_there[1].strip()) == (
        f"Odoo refused the testing login {TEST_LOGIN}.",
        "Odoo gave no answer to a sign-in: URLError.",
    )


def test_only_the_lane_testing_of_a_product_that_declares_a_first_start_has_one_and_a_sign_in() -> None:
    lanes = ("testing", "production", "preview-7")
    assert [bool(TK_STARTING.first_start(each)) for each in lanes] == [bool(TK_STARTING.sign_in(each)) for each in lanes]
    assert [bool(TK_STARTING.sign_in(each)) for each in lanes] == [True, False, False]
    assert (TK.first_start("testing"), TK.sign_in("testing"), Product().first_start("testing")) == ((), (), ())
