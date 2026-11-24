import hashlib
import json
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

import pytest

from launchplane.data_set import DATABASE_NAME
from launchplane.instance import LaneBuild, Mount, Run
from launchplane.judge import Kind, Shown
from launchplane.odoo import FILES_PATH
from launchplane.product import Product
from launchplane.steps import Check, Outcome
from tests.odoo_product import ADDONS_PATH, ATTACHMENT, ATTACHMENT_BYTES, ATTACHMENT_SHA256, MODULES, TK
from tests.pretend_host import (
    D1,
    EARLIER,
    GENERATION,
    OLD_ON_D1,
    WEB_ADDRESS,
    WITH_DATA,
    Given,
    PretendHost,
    PretendProxy,
    lanes_on,
)

BUILD_A = LaneBuild(product="site", lane="testing", build="site:a")
BUILD_B = LaneBuild(product="site", lane="testing", build="site:b")
D1_WITH_FILES = D1.model_copy(update={"files": ("files-d1",)})
OLD_WITH_FILES = OLD_ON_D1.model_copy(update={"mounts": (Mount(source="files-d1", path=FILES_PATH),)})
SERVING = (OLD_WITH_FILES.container, OLD_WITH_FILES.container)
SETTINGS = TK.run.environment


def test_a_product_that_is_no_odoo_starts_as_its_build_says_and_is_checked_by_its_web_answer_alone() -> None:
    assert (Product().run, WITH_DATA.check("web")) == (Run(), Check("web", "/health"))


def test_the_web_part_of_an_odoo_product_runs_no_scheduled_job() -> None:
    assert SETTINGS["ODOO_MAX_CRON_THREADS"] == "0"


def test_odoo_looks_for_its_database_in_the_one_database_its_login_may_connect_to() -> None:
    assert SETTINGS["PGDATABASE_SYSTEM"] == DATABASE_NAME


def test_odoo_is_given_the_addons_path_its_product_declares_and_not_the_one_its_image_holds() -> None:
    assert SETTINGS["ODOO_ADDONS_PATH"] == ",".join(ADDONS_PATH)


@dataclass
class Stored:
    raw: bytes


# What Odoo holds, as the check script reads it through `env`.
@dataclass
class OdooAsRead:
    installed: list[str] = field(default_factory=lambda: [*MODULES, "base"])
    attachments: list[Stored] = field(default_factory=lambda: [Stored(ATTACHMENT_BYTES)])
    asked: list[object] = field(default_factory=list[object])

    cr = SimpleNamespace(dbname="app")

    def __getitem__(self, model: str) -> OdooAsRead:
        assert model in ("ir.module.module", "ir.attachment")
        return replace(self, asked=self.asked, attachments=self.attachments if model == "ir.attachment" else [])

    def search(self, domain: list[object]) -> OdooAsRead | list[Stored]:
        if not self.attachments and domain == [("state", "=", "installed")]:
            return self
        self.asked.append(domain)
        return self.attachments

    def mapped(self, name: str) -> list[str]:
        assert name == "name"
        return self.installed


# The script the check command hands to Odoo's shell, run as that shell runs it: with `env` given.
def run_check(read: OdooAsRead, product: Product = TK) -> None:
    command = product.check("web").command
    script = command[2].split("<<'CHECK'\n")[1].removesuffix("CHECK\n")
    exec(script, {"env": read})  # noqa: S102 the product's own check script


def test_the_check_passes_a_database_with_every_declared_module_installed_and_the_attachment_as_declared() -> None:
    read = OdooAsRead()
    run_check(read)
    assert read.asked == [[("name", "=", ATTACHMENT)]]


CHECK_FAILS = {
    "a declared module is not installed": (
        OdooAsRead(installed=["base", "crm"]),
        "does not have these modules installed: auth_passkey",
    ),
    "the attachment's file is gone, which Odoo reads as no bytes": (
        OdooAsRead(attachments=[Stored(b"")]),
        f"it has: {hashlib.sha256(b'').hexdigest()}",
    ),
    "the attachment holds other bytes": (
        OdooAsRead(attachments=[Stored(b"other")]),
        f"must be there once with the SHA-256 {ATTACHMENT_SHA256}",
    ),
    "the database has no such attachment": (OdooAsRead(attachments=[]), "it has: none"),
    "the database has the attachment twice": (OdooAsRead(attachments=[Stored(ATTACHMENT_BYTES)] * 2), "must be there once"),
}


@pytest.mark.parametrize("name", CHECK_FAILS)
def test_the_check_fails_with_what_it_read(name: str) -> None:
    read, why = CHECK_FAILS[name]
    with pytest.raises(SystemExit, match=why):
        run_check(read)


def test_a_declared_name_cannot_end_the_check_script_or_run_as_code() -> None:
    odoo = TK.odoo
    assert odoo is not None
    hostile = "x'\"\\\nCHECK\n); import os; os.abort() #"
    product = TK.model_copy(update={"odoo": odoo.model_copy(update={"attachment": hostile})})
    read = OdooAsRead()
    run_check(read, product)
    script = product.check("web").command[2]
    assert (read.asked, script.count("\nCHECK\n")) == ([[("name", "=", hostile)]], 1)
    assert json.loads(json.dumps(hostile)) == hostile


def moved_to_a(proxy: PretendProxy) -> str:
    moved = lanes_on(proxy, TK).move(BUILD_A, EARLIER, 1)
    assert (moved.outcome, moved.container_receiving_visitors) == (Outcome.SWITCHED, next(iter(proxy.host.instances)))
    return next(iter(proxy.host.instances))


def test_a_new_lane_of_an_odoo_product_gets_a_files_volume_before_its_database_and_its_instance_mounts_it() -> None:
    proxy = PretendProxy(PretendHost())
    serving = moved_to_a(proxy)
    made = [
        action.split(" on ")[0] for action in proxy.host.actions if action.startswith(("network", "volume", "files", "database"))
    ]
    assert made == ["network data-1", "volume data-1", "files data-1", "database data-1"]
    assert proxy.host.instances[serving].mounts == (Mount(source="files-data-1", path=FILES_PATH),)
    assert lanes_on(proxy, TK).read_status().lanes[0].findings == ()


def test_a_candidate_that_answers_and_fails_the_check_of_its_kind_is_removed_and_the_lane_keeps_what_it_served() -> None:
    proxy = PretendProxy(PretendHost())
    serving = moved_to_a(proxy)
    proxy.failing_its_kind = {f"site-testing-{number}" for number in range(100)} - {serving}
    moved = lanes_on(proxy, TK).move(BUILD_B, GENERATION, 1)
    assert (moved.outcome, moved.container_receiving_visitors) == (Outcome.CHECK_FAILED, serving)
    assert (list(proxy.host.instances), proxy.written[WEB_ADDRESS]) == ([serving], serving)
    assert "The check of its kind failed" in (moved.error or "")


def test_a_product_that_is_no_odoo_is_not_asked_for_a_check_of_its_kind() -> None:
    proxy = PretendProxy(PretendHost())
    proxy.failing_its_kind = {f"site-testing-{number}" for number in range(100)}
    assert lanes_on(proxy, WITH_DATA).move(BUILD_A, GENERATION, 1).outcome is Outcome.SWITCHED


def kinds_found(given: Given, product: Product) -> dict[Kind, tuple[str, Shown]]:
    return {finding.kind: (finding.sentence, finding.shown) for finding in given.judged(None, product).findings}


def test_a_data_set_of_an_odoo_product_with_no_files_volume_is_left_unfinished_and_no_other_products_is() -> None:
    given = Given((OLD_ON_D1,), SERVING, data_sets=(D1,))
    assert kinds_found(given, TK)[Kind.NO_FILES] == ("The data set d1 has no files volume", Shown.LEFT_UNFINISHED)
    assert Kind.NO_FILES not in kinds_found(given, WITH_DATA.model_copy(update={"background_service": None}))


def test_an_odoo_instance_that_does_not_mount_its_data_sets_files_is_wrong() -> None:
    found = kinds_found(Given((OLD_ON_D1,), SERVING, data_sets=(D1_WITH_FILES,)), TK)
    assert found[Kind.MISPLACED] == (f"The instance old does not have files-d1 mounted at {FILES_PATH}", Shown.WRONG)
    assert kinds_found(Given((OLD_WITH_FILES,), SERVING, data_sets=(D1_WITH_FILES,)), TK) == {}


def test_a_second_files_volume_is_a_doubled_part_and_a_files_volume_is_never_taken_for_a_second_database_volume() -> None:
    doubled = D1.model_copy(update={"files": ("files-d1", "files-other")})
    assert (
        "2 files volumes (files-d1, files-other)"
        in kinds_found(Given((OLD_WITH_FILES,), SERVING, data_sets=(doubled,)), TK)[Kind.DOUBLED][0]
    )
    assert Kind.DOUBLED not in kinds_found(Given((OLD_WITH_FILES,), SERVING, data_sets=(D1_WITH_FILES,)), TK)


def test_an_instance_that_runs_another_image_than_its_builds_digest_is_wrong_and_a_move_to_that_build_replaces_it() -> None:
    other = OLD_WITH_FILES.model_copy(update={"other_image": "sha256:" + "f" * 64})
    given = Given((other,), SERVING, data_sets=(D1_WITH_FILES,))
    sentence, shown = kinds_found(given, TK)[Kind.NOT_ITS_BUILD]
    assert (sentence, shown) == (f"The instance old runs sha256:{'f' * 64}, which is not its build", Shown.WRONG)
    proxy = given.proxy()
    moved = lanes_on(proxy, TK).move(LaneBuild(product="site", lane="testing", build="a"), GENERATION, 1)
    assert (moved.outcome, list(proxy.host.instances)) == (Outcome.SWITCHED, [moved.container_receiving_visitors])
    assert moved.container_receiving_visitors != "old"
