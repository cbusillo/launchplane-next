import io
import json
import logging
import os
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from launchplane.act import Lanes
from launchplane.api import create_app
from launchplane.docker_answers import CommandResult
from launchplane.host_secrets import HostSecrets
from launchplane.instance import InstanceState, LaneBuild
from launchplane.judge import Condition
from launchplane.odoo import SERVER, TEST_PASSWORD_VARIABLE
from launchplane.product import Product, ProductError, ProductFolder, SecretFolder
from launchplane.record import Gate
from launchplane.secret_boundary import SECRET_REMOVED, CleanedStream
from launchplane.steps import Initialize, Outcome, Update
from launchplane.telling import Witness
from launchplane.worker import Worker
from tests.odoo_product import ATTACHMENT, ATTACHMENT_BYTES, MODULES, TEST_LOGIN, TK, TK_STARTING
from tests.pretend_host import GENERATION, WEB_ADDRESS, LaunchplaneKilled, PretendHost, PretendProxy, numbered_names
from tests.pretend_record import PretendRecord
from tests.test_act import BUILD_A, BUILD_B

LATER = "01900000-0000-7000-8000-00000000000c"
MADE_BEFORE_THE_FIRST_START = ("network", "database volume", "files volume", "database server", "server accepting connections")
PRODUCTION = LaneBuild(product="site", lane="production", build="site:a")


class Noted(HostSecrets):
    def read_secrets(self) -> frozenset[str]:
        return self.seen

    def secrets_seen(self) -> frozenset[str]:
        return self.seen


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

    # Each request is worked within the call that asked for it, and what it says is cleaned of what was noted.
    def asked_through_the_api(self) -> tuple[TestClient, PretendRecord]:
        record = PretendRecord(gate=Gate(self.noted))
        self.lanes.read_unfinished, self.lanes.read_ended = record.unfinished, record.ended_lately
        worker = Worker(record, self.lanes, self.noted)
        return TestClient(create_app(self.lanes.read_status, record, worker.worked_one, self.noted, self.lanes)), record


def described_as(product: Product) -> Callable[[str], Product]:
    return lambda _product: product


@pytest.fixture
def bed(tmp_path: Path) -> Bed:
    proxy, noted = PretendProxy(PretendHost()), Noted()
    kept = SecretFolder(tmp_path / "secrets", noted.note)
    lanes = Lanes(proxy.host, proxy, described_as(TK_STARTING), numbered_names(), read_test_password=kept.test_password)
    return Bed(proxy, lanes, kept, noted)


def test_the_first_move_runs_the_first_start_on_the_data_set_it_made_and_starts_the_web_part_only_after_it(
    bed: Bed, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran_on: list[tuple[str, list[str]]] = []
    run_once = bed.host.run_once

    def note_and_run(run: Update | Initialize, by: Witness, given: Mapping[str, str]) -> CommandResult:
        ran_on.append((run.use.data_set, bed.web_starts()))
        return run_once(run, by, given)

    monkeypatch.setattr(bed.host, "run_once", note_and_run)
    result = bed.lanes.move(BUILD_A, GENERATION, 1)
    new = bed.proxy.loaded_to(WEB_ADDRESS)
    assert (ran_on, result.outcome, result.data_set) == ([("data-1", [])], Outcome.SWITCHED, "data-1")
    [first_start] = bed.first_starts()
    assert bed.host.actions[-4:] == [first_start, f"start {new}", f"check {new}/web/login", f"route {new}"]
    assert list(bed.host.instances) == [new]
    assert [each.condition for each in bed.lanes.read_status().lanes] == [Condition.GOOD]


def test_a_first_start_that_fails_starts_no_web_part_and_leaves_no_route(bed: Bed) -> None:
    bed.host.updates_that_fail = {BUILD_A.build}
    result = bed.lanes.move(BUILD_A, GENERATION, 1)
    assert (result.outcome, result.container_receiving_visitors) == (Outcome.NOT_INITIALIZED, None)
    [container] = [each.removeprefix("first start ") for each in bed.first_starts()]
    said = f"The first start of the data, run in {container}, ended with code 1.\nIts last log lines:\nmodule not found"
    assert (result.error, container.endswith("-first-start")) == (said, True)
    assert (bed.web_starts(), bed.proxy.written, bed.proxy.loaded, list(bed.host.instances)) == ([], {}, {}, [])


def test_a_move_taken_up_again_after_its_first_start_gives_the_same_password_and_starts_one_web_part(bed: Bed) -> None:
    bed.host.killed_after = len(MADE_BEFORE_THE_FIRST_START) + 1
    with pytest.raises(LaunchplaneKilled):
        bed.lanes.move(BUILD_A, GENERATION, 1)
    assert (len(bed.first_starts()), bed.web_starts()) == (1, [])
    bed.host.killed_after = None
    result = bed.lanes.move(BUILD_A, GENERATION, 2)
    first, again = bed.host.given_once
    assert (result.outcome, len(bed.first_starts()), len(bed.web_starts())) == (Outcome.SWITCHED, 2, 1)
    assert (first, bool(first[TEST_PASSWORD_VARIABLE])) == (again, True)


def test_a_lane_that_serves_is_given_no_first_start_by_the_same_build_asked_again_or_by_another_build(bed: Bed) -> None:
    bed.lanes.move(BUILD_A, GENERATION, 1)
    bed.host.actions.clear()
    again = bed.lanes.move(BUILD_A, LATER, 1)
    assert (again.outcome, bed.host.actions) == (Outcome.UNCHANGED, [])
    released = bed.lanes.move(BUILD_B, LATER, 1)
    assert (released.outcome, bed.first_starts()) == (Outcome.SWITCHED, [])


def test_a_product_that_declares_no_first_start_is_started_on_its_data_set_as_it_is(bed: Bed) -> None:
    bed.lanes.read_product = described_as(TK)
    assert (bed.lanes.move(BUILD_A, GENERATION, 1).outcome, bed.first_starts()) == (Outcome.SWITCHED, [])


def test_only_the_lane_testing_is_given_a_testing_login_and_each_lane_has_a_password_of_its_own(bed: Bed) -> None:
    bed.lanes.move(PRODUCTION, GENERATION, 1)
    bed.lanes.move(BUILD_A, LATER, 1)
    assert bed.host.given_once == [{}, {TEST_PASSWORD_VARIABLE: bed.kept.test_password("site", "testing")}]
    others = {bed.kept.test_password("other", "testing"), bed.kept.test_password("site", "preview")}
    assert (len(others), bed.kept.test_password("site", "testing") in others) == (2, False)


def test_a_first_start_that_fails_saying_its_password_leaves_it_out_of_the_answer_the_request_the_card_and_the_log(bed: Bed) -> None:
    password = bed.kept.test_password("site", "testing")
    bed.host.updates_that_fail = {"site:b"}
    bed.host.failure_says = f"psycopg2.OperationalError: could not write the user {TEST_LOGIN} with password {password}"
    asking, record = bed.asked_through_the_api()
    answer = asking.post("/lanes/site/testing/move?wait=1", json={"build": "site:b"})
    request, running = asking.get("/requests/1").text, asking.get("/running").text
    assert (answer.status_code, answer.json()["outcome"], bed.host.given_once) == (
        422,
        "not initialized",
        [{TEST_PASSWORD_VARIABLE: password}],
    )
    for said in (answer.text, request, running, str(record.kept)):
        assert (password in said, f"with password {SECRET_REMOVED}" in said) == (False, True)
    written = io.StringIO()
    log = logging.Logger("a log of its own")
    log.addHandler(CleanedStream(bed.noted, written))
    log.error("the first start said: %s", bed.host.failure_says)
    assert (password in written.getvalue(), SECRET_REMOVED in written.getvalue()) == (False, True)


def test_a_password_is_kept_where_only_launchplane_reads_it_and_is_noted_before_it_is_given(tmp_path: Path) -> None:
    noted: list[str] = []
    kept = SecretFolder(tmp_path, noted.extend)
    password = kept.test_password("site", "testing")
    file = tmp_path / "site" / "testing" / "test-login"
    assert (file.read_text(), noted, stat.S_IMODE(file.stat().st_mode)) == (password, [password], 0o600)
    assert (kept.test_password("site", "testing"), len(password) >= 32) == (password, True)


def test_every_password_kept_is_noted_when_launchplane_starts(tmp_path: Path) -> None:
    earlier = SecretFolder(tmp_path, lambda _secrets: None)
    kept = {earlier.test_password("site", "testing"), earlier.test_password("shop", "testing")}
    noted = Noted()
    SecretFolder(tmp_path, noted.note).note_all()
    assert noted.seen == kept


def test_a_password_that_cannot_be_kept_stops_the_move_before_anything_is_run(bed: Bed) -> None:
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


PSQL = """#!/bin/sh
echo "psql $*" >> "$CALLS"
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


# The first start as the kind's build runs it, with what it calls stood in for: psql answers how much the database holds.
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


def run_first_start(folder: Path, held: str, init_ends: int = 0) -> Ran:
    for name, text in (("psql", PSQL), ("timeout", TIMEOUT), ("odoo-bin", ODOO)):
        (folder / name).write_text(text)
        (folder / name).chmod(0o755)
    shell, flag, script = TK_STARTING.first_start
    given = {"PATH": f"{folder}:{os.environ['PATH']}", "HELD": held, "INIT_ENDS": str(init_ends)}
    given |= {"CALLS": str(folder / "calls"), "SEED": str(folder / "seed")}
    command = [shell, flag, script.replace(SERVER, str(folder / "odoo-bin"))]
    ran = subprocess.run(command, env=given, capture_output=True, text=True, check=False)  # noqa: S603
    return Ran(folder, ran.returncode, ran.stdout)


@dataclass
class OdooAsSeeded:
    created: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    written: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    committed: list[bool] = field(default_factory=list[bool])

    @property
    def cr(self) -> SimpleNamespace:
        return SimpleNamespace(commit=lambda: self.committed.append(True))

    def __getitem__(self, model: str) -> OdooAsSeeded:
        assert model == "ir.attachment"
        return self

    def ref(self, name: str) -> OdooAsSeeded:
        assert name == "base.user_admin"
        return self

    def create(self, values: dict[str, Any]) -> None:
        self.created.append(values)

    def write(self, values: dict[str, Any]) -> None:
        self.written.append(values)


def seeded(script: str) -> OdooAsSeeded:
    odoo = OdooAsSeeded()
    exec(script, {"env": odoo})  # noqa: S102 the kind's own seed script
    return odoo


def test_an_empty_database_gets_the_declared_modules_then_the_attachment_and_the_testing_login_in_one_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = run_first_start(tmp_path, held="0")
    assert (ran.exit_code, [each.split(" --")[0] for each in ran.calls]) == (0, ["psql", "odoo", "odoo shell"])
    assert ran.calls[1] == f"odoo --init {','.join(MODULES)} --stop-after-init --no-http"
    monkeypatch.setenv(TEST_PASSWORD_VARIABLE, "the lane's own")
    odoo = seeded(ran.seed)
    assert odoo.created == [{"name": ATTACHMENT, "raw": ATTACHMENT_BYTES, "mimetype": "application/octet-stream"}]
    assert (odoo.written, odoo.committed) == ([{"login": TEST_LOGIN, "password": "the lane's own"}], [True])


def test_a_lane_given_no_password_keeps_no_default_one_and_gets_no_testing_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TEST_PASSWORD_VARIABLE, raising=False)
    [signs_in] = seeded(run_first_start(tmp_path, held="0").seed).written
    assert (list(signs_in), len(signs_in["password"]) >= 32) == (["password"], True)


@pytest.mark.parametrize("held", ["1", "734"])
def test_a_database_that_holds_anything_is_left_as_it_is_whatever_it_lacks(tmp_path: Path, held: str) -> None:
    ran = run_first_start(tmp_path, held)
    assert (ran.exit_code, len(ran.calls), ran.calls[0].startswith("psql ")) == (0, 1, True)
    assert f"holds {held} tables and indexes already, so nothing was initialized in it" in ran.said


def test_a_database_that_cannot_be_asked_what_it_holds_is_not_initialized(tmp_path: Path) -> None:
    ran = run_first_start(tmp_path, held="unreadable")
    assert (ran.exit_code, len(ran.calls)) == (2, 1)


def test_modules_that_do_not_install_end_the_first_start_before_anything_is_seeded(tmp_path: Path) -> None:
    ran = run_first_start(tmp_path, held="0", init_ends=3)
    assert (ran.exit_code, [each.split(" ")[1] for each in ran.calls]) == (3, ["--no-psqlrc", "--init"])


def test_what_runs_the_first_start_is_no_instance_and_is_no_finding_while_it_runs(bed: Bed) -> None:
    seen: list[tuple[list[str], list[bool]]] = []
    record = bed.host.record

    def read_while_it_runs(action: str) -> None:
        if action.startswith("first start "):
            [lane] = bed.lanes.read_status().lanes
            seen.append(
                (
                    [each.sentence for each in lane.findings if "first start" in each.sentence],
                    [each.is_first_start for each in lane.lane.instances],
                )
            )
        record(action)

    bed.host.record = read_while_it_runs
    bed.lanes.move(BUILD_A, GENERATION, 1)
    assert seen == [([], [True])]
    assert bed.host.is_in(bed.proxy.loaded_to(WEB_ADDRESS), InstanceState.RUNNING)


def test_the_card_of_a_lane_whose_first_start_failed_says_so_and_that_only_an_empty_data_set_is_initialized_again(bed: Bed) -> None:
    bed.host.updates_that_fail = {"site:b"}
    asking, _ = bed.asked_through_the_api()
    assert asking.post("/lanes/site/testing/move?wait=1", json={"build": "site:b"}).status_code == 422
    [card] = asking.get("/running").json()["cards"]
    [found] = [row for row in card["rows"] if row["name"] == "Found"]
    assert ([pill["text"] for pill in card["pills"]], [pill["text"] for pill in found["pills"]]) == (
        ["move failed"],
        ["move failed"],
    )
    assert found["note"].startswith("Request 1 ran the first start of the lane's data, which failed, so no instance was started")
    assert "initializes the data set only if it is still empty" in found["note"]
