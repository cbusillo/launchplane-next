import threading
from collections.abc import Callable, Iterator
from http.server import ThreadingHTTPServer
from typing import Any

import pytest
from fastapi.testclient import TestClient
from githubkit import GitHub

from launchplane.act import Lanes
from launchplane.api import create_app
from launchplane.git_host import Builds, GitHostError, NotABuild
from launchplane.product import Product
from launchplane.record import Record
from tests.given_secrets import GivenSecrets
from tests.odoo_product import TK
from tests.pretend_host import PretendHost, PretendProxy
from tests.recorded_git_host import LATER_RUN, RUN, answers, build_of, serve

type Runs = dict[int, dict[str, Any]]
FORK = "someone/odoo-tenant-tk"


@pytest.fixture
def builds_from() -> Iterator[Callable[[Runs], Builds]]:
    servers: list[ThreadingHTTPServer] = []

    def builds(runs: Runs) -> Builds:
        servers.append(serve(runs, 0))
        threading.Thread(target=servers[-1].serve_forever, daemon=True).start()
        return Builds(lambda _product: TK, GitHub(base_url=f"http://127.0.0.1:{servers[-1].server_port}"))

    yield builds
    for server in servers:
        server.shutdown()


def test_a_publishing_run_is_the_image_its_manifest_names_by_digest(builds_from: Callable[[Runs], Builds]) -> None:
    builds = builds_from(answers())
    assert (
        builds.asked("tk", None, RUN)
        == "ghcr.io/cbusillo/odoo-tenant-tk@sha256:" + "0e2ad09feaa7c0a190d47db94e49696b6a683dd437c9d31f58198ae65f12d0a4"
    )
    assert builds.asked("tk", None, LATER_RUN) == build_of(LATER_RUN) != build_of(RUN)


def with_run(change: dict[str, object]) -> Runs:
    runs = answers()
    runs[RUN]["run"] |= change
    return runs


def with_manifest(change: dict[str, object]) -> Runs:
    runs = answers()
    runs[RUN]["manifest"] |= change
    return runs


def without_a_manifest() -> Runs:
    runs = answers()
    runs[RUN]["artifacts"] = {"total_count": 0, "artifacts": []}
    return runs


REFUSED: dict[str, tuple[Runs, str]] = {
    "a run that passed and published nothing": (without_a_manifest(), "published no image"),
    "a manifest of an earlier attempt than the run's last": (with_run({"run_attempt": 2}), "no manifest for attempt 2"),
    "a run whose commit is of a fork": (
        with_run({"head_repository": answers()[RUN]["run"]["head_repository"] | {"full_name": FORK}}),
        f"the repository of its commit is {FORK}, not cbusillo/odoo-tenant-tk",
    ),
    "a run of another workflow": (with_run({"path": ".github/workflows/other.yml"}), "its workflow is .github/workflows/other.yml"),
    "a run that failed": (with_run({"conclusion": "failure"}), "its conclusion is failure, not success"),
    "a manifest for another commit": (with_manifest({"source_commit": "0" * 40}), "the commit in its manifest is 0000"),
    "a manifest that names another image repository": (
        with_manifest({"image": {"repository": "ghcr.io/someone/else", "digest": "sha256:" + "0" * 64}}),
        "the image repository in its manifest is ghcr.io/someone/else, not ghcr.io/cbusillo/odoo-tenant-tk",
    ),
}


@pytest.mark.parametrize("name", REFUSED)
def test_a_run_that_is_not_a_build_of_the_product_is_refused(name: str, builds_from: Callable[[Runs], Builds]) -> None:
    runs, why = REFUSED[name]
    with pytest.raises(NotABuild, match=why):
        builds_from(runs).asked("tk", None, RUN)


UNREAD: dict[str, tuple[Runs, int]] = {
    "a run the git host does not have": (answers(), 1),
    "a manifest whose digest is a tag": (
        with_manifest({"image": {"repository": "ghcr.io/cbusillo/odoo-tenant-tk", "digest": "latest"}}),
        RUN,
    ),
    "a manifest that names no image": (with_manifest({"image": {}}), RUN),
}


@pytest.mark.parametrize("name", UNREAD)
def test_what_cannot_be_read_as_a_run_and_its_manifest_verifies_nothing(name: str, builds_from: Callable[[Runs], Builds]) -> None:
    runs, run = UNREAD[name]
    with pytest.raises(GitHostError, match="no build was verified and nothing was changed") as refused:
        builds_from(runs).asked("tk", None, run)
    assert not isinstance(refused.value, NotABuild)


def test_a_build_given_as_text_is_refused_for_a_product_whose_builds_are_runs(builds_from: Callable[[Runs], Builds]) -> None:
    builds = builds_from(answers())
    for build, run in ((build_of(RUN), None), (build_of(RUN), RUN), (None, None)):
        with pytest.raises(NotABuild, match=r'names its build as \{"run": <number>, of a run of .github/workflows/build.yml'):
            builds.asked("tk", build, run)


def test_a_product_that_does_not_say_where_its_builds_come_from_names_an_image_and_never_a_run() -> None:
    plain = Builds(lambda _product: Product())
    assert plain.asked("site", "site:a", None) == "site:a"
    for build, run in ((None, RUN), ("site:a", RUN), (None, None)):
        with pytest.raises(NotABuild, match=r'names its build as \{"build": "<image>"\}'):
            plain.asked("site", build, run)


def app_with(builds: Builds, record: Record) -> TestClient:
    proxy = PretendProxy(PretendHost())
    lanes = Lanes(proxy.host, proxy, read_unfinished=record.unfinished)
    return TestClient(create_app(lanes.read_status, record, lambda: None, GivenSecrets(), lanes, builds))


def test_a_move_by_run_is_kept_as_the_digest_that_run_published(builds_from: Callable[[Runs], Builds], record: Record) -> None:
    answer = app_with(builds_from(answers()), record).post("/lanes/tk/testing/move", json={"run": RUN})
    assert (answer.status_code, answer.json()["build"]) == (202, build_of(RUN))


def test_a_move_that_names_no_verified_build_keeps_no_request(builds_from: Callable[[Runs], Builds], record: Record) -> None:
    app = app_with(builds_from(with_run({"conclusion": "failure"})), record)
    refused = app.post("/lanes/tk/testing/move", json={"run": RUN})
    as_text = app.post("/lanes/tk/testing/move", json={"build": build_of(RUN)})
    unread = app.post("/lanes/tk/testing/move", json={"run": 1})
    assert (refused.status_code, as_text.status_code, unread.status_code) == (422, 422, 503)
    assert record.unfinished() == []
