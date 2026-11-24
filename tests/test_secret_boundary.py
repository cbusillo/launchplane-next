import io
import json
import logging
import logging.config
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from githubkit import GitHub

from launchplane import api
from launchplane.act import Lanes, Status, Stopped
from launchplane.api import cleaned_log_config, create_app
from launchplane.container_host import ContainerHost
from launchplane.docker_answers import ContainerHostError, ContainerHostRefused
from launchplane.git_host import Builds
from launchplane.instance import (
    InstanceState,
    LaneBuild,
    LoadedRoute,
    Mount,
    OnHost,
    ProxySeen,
    Reading,
    RouteStatus,
    RunningInstance,
)
from launchplane.judge import judge, web_addresses_judged
from launchplane.product import Product
from launchplane.secret_boundary import SECRET_REMOVED, CleanedStream, Secrets, cleaned_body
from launchplane.steps import MaintenanceOutcome, MaintenanceResult, MoveResult, Outcome, RetireOutcome, RetireResult
from launchplane.telling import Witness
from launchplane.worker import Carries, Worker
from tests.given_secrets import GivenSecrets
from tests.odoo_product import TK
from tests.pretend_host import SINCE, GateNeverAsked, PretendHost, PretendProxy
from tests.pretend_record import PretendRecord
from tests.recorded_git_host import RUN, SIGNATURE, answers, build_of, serve
from tests.recording_docker import RecordingDocker

SECRET = "Zx3-secret_given-to-a-lane-0123456789abcdef"  # noqa: S105 a pretend one
LEAK = f"wget: server returned error: HTTP/1.1 503 password={SECRET}"
PROXY = ProxySeen(runs=True, back_soon_runs=True, loaded={"testing.site.test": LoadedRoute("site-testing-1", RouteStatus.ENABLED)})


def leaking_instance() -> RunningInstance:
    return RunningInstance(
        product="site",
        lane="testing",
        build=f"site:{SECRET}",
        container="site-testing-1",
        state=InstanceState.RUNNING,
        networks=(),
        mounts=(Mount(source=SECRET, path="/data"),),
    )


def running_with_leaks() -> Status:
    reading = Reading(SINCE, PROXY, OnHost([leaking_instance()], [], []), {"testing.site.test": LEAK})
    return Status(reading, [judge(reading.lane("site", "testing"), Product(), None)], web_addresses_judged(reading))


def unreadable_host() -> Status:
    raise ContainerHostError(f"Docker refused: {SECRET}")


def move_answering(outcome: Outcome) -> Callable[[LaneBuild, str, int, Witness], MoveResult]:
    def move(target: LaneBuild, _generation: str, _attempt: int, _witness: Witness) -> MoveResult:
        return MoveResult(
            product=target.product,
            lane=target.lane,
            build=target.build,
            web_address="testing.site.test",
            outcome=outcome,
            container_receiving_visitors=f"site-testing-{SECRET}",
            error=None if outcome == Outcome.SWITCHED else f"The check failed. Its last log lines:\nPGPASSWORD={SECRET}",
        )

    return move


def move_stopped(_target: LaneBuild, _generation: str, _attempt: int, _witness: Witness) -> MoveResult:
    raise Stopped(f"The lane is not as it must be: {SECRET}")


def retire_answering(product: str, lane: str, _delete_data_set: str | None, _witness: Witness) -> RetireResult:
    return RetireResult(
        product=product,
        lane=lane,
        web_address="testing.site.test",
        outcome=RetireOutcome.RETIRED,
        route_removed_to_container=f"site-testing-{SECRET}",
        retired_containers=[],
    )


def retire_stopped(product: str, lane: str, _delete_data_set: str | None, _witness: Witness) -> RetireResult:
    raise ContainerHostRefused(f"Stopping {lane} of {product} was refused: {SECRET}")


def maintenance_answering(product: str, lane: str, _witness: Witness) -> MaintenanceResult:
    return MaintenanceResult(
        product=product,
        lane=lane,
        web_address="testing.site.test",
        outcome=MaintenanceOutcome.WEB_PART_FAILED,
        in_maintenance=True,
        error=f"The check failed. Its last log lines:\nPGPASSWORD={SECRET}",
    )


def maintenance_stopped(product: str, lane: str, _witness: Witness) -> MaintenanceResult:
    raise ContainerHostRefused(f"Stopping a part of {lane} of {product} was refused: {SECRET}")


@dataclass(frozen=True)
class Asked(GateNeverAsked):
    method: str
    path: str
    status: int
    body: dict[str, Any] | None = None
    read: Callable[[], Status] = running_with_leaks
    move: Callable[[LaneBuild, str, int, Witness], MoveResult] = move_stopped
    retire: Callable[[str, str, str | None, Witness], RetireResult] = retire_stopped
    maintenance: Callable[[str, str, Witness], MaintenanceResult] = maintenance_stopped

    def begin_maintenance(self, product: str, lane: str, witness: Witness, /) -> MaintenanceResult:
        return self.maintenance(product, lane, witness)

    def end_maintenance(self, product: str, lane: str, witness: Witness, /) -> MaintenanceResult:
        return self.maintenance(product, lane, witness)


# Each request is worked within the call that asked for it, so the call answers with how it ended.
def app_working_at_once(read: Callable[[], Status], lanes: Carries, secrets: Secrets) -> FastAPI:
    record = PretendRecord()
    return create_app(read, record, Worker(record, lanes, secrets).worked_one, secrets, lanes)


STOPPED = Asked("POST", "/", 503)
MOVE = "/lanes/site/testing/move?wait=1"
RETIRE = "/lanes/site/testing/retire?wait=1"
MAINTENANCE_BEGIN = "/lanes/site/testing/maintenance/begin?wait=1"
MAINTENANCE_END = "/lanes/site/testing/maintenance/end?wait=1"
ANSWERS = {
    "what is running, with a web address whose error carries it": Asked("GET", "/running", 200),
    "a host that could not be read": Asked("GET", "/running", 503, read=unreadable_host),
    "a move that switched": Asked("POST", MOVE, 200, {"build": "site:b"}, move=move_answering(Outcome.SWITCHED)),
    "a move whose check failed": Asked("POST", MOVE, 422, {"build": "site:b"}, move=move_answering(Outcome.CHECK_FAILED)),
    "a move whose background service failed": Asked(
        "POST", MOVE, 422, {"build": "site:b"}, move=move_answering(Outcome.BACKGROUND_SERVICE_FAILED)
    ),
    "a move that stopped": Asked("POST", MOVE, 503, {"build": "site:b"}),
    "a retirement": Asked("POST", RETIRE, 200, {}, retire=retire_answering),
    "a retirement that stopped": Asked("POST", RETIRE, 503, {}),
    "a lane whose web part failed coming out of maintenance": Asked("POST", MAINTENANCE_END, 422, maintenance=maintenance_answering),
    "a lane that could not be put in maintenance": Asked("POST", MAINTENANCE_BEGIN, 503),
    "a request Launchplane refuses, repeating what was sent": Asked("POST", MOVE, 422, {"build": [SECRET]}),
    "a lane name Launchplane refuses, repeating it": Asked("POST", f"/lanes/site/{SECRET}/retire", 422, {}),
    "a redirect to the address asked for, which carries it": Asked("POST", f"/lanes/site/{SECRET}/move/", 307, {}),
}


def answer_to(asked: Asked, secrets: Secrets) -> tuple[int, str]:
    app = app_working_at_once(asked.read, asked, secrets)
    response = TestClient(app, follow_redirects=False).request(asked.method, asked.path, json=asked.body)
    headers = "".join(f"{name}: {value}\n" for name, value in response.headers.items())
    return response.status_code, headers + response.text


@pytest.mark.parametrize("asked", ANSWERS.values(), ids=ANSWERS.keys())
def test_no_answer_of_any_kind_carries_a_secret_out(asked: Asked) -> None:
    status, text = answer_to(asked, GivenSecrets(on_host=frozenset({SECRET})))
    assert status == asked.status
    assert SECRET not in text
    assert SECRET_REMOVED in text


@pytest.mark.parametrize("asked", ANSWERS.values(), ids=ANSWERS.keys())
def test_no_answer_is_made_when_the_secrets_to_clean_it_of_cannot_be_read(asked: Asked) -> None:
    status, text = answer_to(asked, GivenSecrets(on_host=frozenset({SECRET}), readable=False))
    assert status == 503
    assert SECRET not in text
    assert "made no answer" in text
    assert "Connection refused" in text


def test_nothing_is_moved_when_the_secrets_cannot_be_read() -> None:
    def move(target: LaneBuild, _generation: str, _attempt: int, _witness: Witness) -> MoveResult:
        raise AssertionError(f"Moved to {target.build} with no way to clean the answer")

    asked = Asked("POST", MOVE, 503, {"build": "site:b"}, move=move)
    assert answer_to(asked, GivenSecrets(readable=False))[0] == 503


def test_the_reason_no_answer_was_made_is_cleaned_of_the_secrets_already_seen() -> None:
    class SecretsWhoseReadFailsCarryingOne(GivenSecrets):
        def read_secrets(self) -> frozenset[str]:
            raise ContainerHostError(f"Docker answered {SECRET}")

    status, text = answer_to(ANSWERS["a retirement"], SecretsWhoseReadFailsCarryingOne(seen=frozenset({SECRET})))
    assert (status, SECRET in text, SECRET_REMOVED in text) == (503, False, True)


def test_a_secret_that_json_writes_differently_is_still_removed() -> None:
    awkward = 'pass"word\\with ünicode\n'

    def read() -> Status:
        raise ContainerHostError(f"Docker refused: {awkward}")

    status, text = answer_to(Asked("GET", "/running", 503, read=read), GivenSecrets(on_host=frozenset({awkward})))
    assert (status, "ünicode" in text or "\\u00fcnicode" in text, SECRET_REMOVED in text) == (503, False, True)


def test_a_secret_in_the_name_of_a_json_value_is_removed_like_one_in_the_value() -> None:
    body = json.dumps({f"variable {SECRET}": {f"inner {SECRET}": [SECRET]}}).encode()
    assert json.loads(cleaned_body(body, "application/json", {SECRET})) == {
        f"variable {SECRET_REMOVED}": {f"inner {SECRET_REMOVED}": [SECRET_REMOVED]}
    }


def test_cleaning_replaces_only_the_secret_and_keeps_the_explanation_around_it() -> None:
    status, text = answer_to(ANSWERS["a move that stopped"], GivenSecrets(on_host=frozenset({SECRET})))
    explanation = "The move of testing of site to site:b stopped, and is safe to ask again: The lane is not as it must be: "
    assert (status, json.loads(text.split("\n")[-1])) == (503, {"error": explanation + SECRET_REMOVED})


def test_the_page_opens_when_the_secrets_cannot_be_read() -> None:
    status, text = answer_to(Asked("GET", "/", 200), GivenSecrets(readable=False))
    assert (status, "<html" in text) == (200, True)


def test_the_secrets_are_read_before_the_answer_is_made() -> None:
    secrets = GivenSecrets(on_host=frozenset({SECRET}))
    seen_when_the_host_was_read: list[frozenset[str]] = []

    def read() -> Status:
        seen_when_the_host_was_read.append(secrets.secrets_seen())
        raise RuntimeError(f"not an error Launchplane expects: {SECRET}")

    app = app_working_at_once(read, STOPPED, secrets)
    assert TestClient(app, raise_server_exceptions=False).get("/running").status_code == 500
    assert seen_when_the_host_was_read == [{SECRET}]


def failure_carrying_the_secret() -> RuntimeError:
    try:
        raise RuntimeError(f"connecting with {SECRET}")
    except RuntimeError as error:
        return error


def test_a_log_line_has_no_secret_in_its_message_arguments_or_traceback() -> None:
    written = io.StringIO()
    log = logging.Logger("a log of its own")
    log.addHandler(CleanedStream(GivenSecrets(seen=frozenset({SECRET})), written))
    log.error("message %s with argument %s", SECRET, SECRET, exc_info=failure_carrying_the_secret())
    assert SECRET not in written.getvalue()
    assert written.getvalue().count(SECRET_REMOVED) == 3


def test_a_log_line_that_cannot_be_written_is_not_repeated_uncleaned_by_the_logging_library(
    capsys: pytest.CaptureFixture[str],
) -> None:
    closed = io.StringIO()
    closed.close()
    log = logging.Logger("a log of its own")
    log.addHandler(CleanedStream(GivenSecrets(), closed))
    log.error("connecting with %s", SECRET)
    complaint = str(capsys.readouterr().err)
    assert "could not be written" in complaint
    assert SECRET not in complaint


SERVICE_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture
def service_loggers() -> Iterator[list[logging.Logger]]:
    loggers = [logging.root, *map(logging.getLogger, SERVICE_LOGGERS)]
    before = [(logger, logger.handlers[:], logger.propagate, logger.level) for logger in loggers]
    yield loggers
    for logger, handlers, propagate, level in before:
        logger.handlers[:], logger.propagate, logger.level = handlers, propagate, level


@pytest.fixture
def written_by_the_service_log(service_loggers: list[logging.Logger]) -> io.StringIO:
    logging.config.dictConfig(cleaned_log_config(GivenSecrets(seen=frozenset({SECRET}))))
    written = io.StringIO()
    for logger in service_loggers:
        for handler in logger.handlers:
            assert isinstance(handler, CleanedStream), f"{logger.name} writes through {handler}, which cleans nothing"
            handler.setStream(written)
    return written


@pytest.mark.usefixtures("service_loggers")
def test_a_line_logged_as_the_service_starts_is_cleaned_before_the_worker_or_the_server_has_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    docker = RecordingDocker()
    holding_the_secret(docker)
    host = ContainerHost(docker.as_client(), "test")
    writing_through: list[bool] = []

    def start_and_fail(_worker: Worker) -> None:
        host.read_secrets()
        writing_through.extend(isinstance(handler, CleanedStream) for handler in logging.root.handlers)
        logging.getLogger("launchplane.worker").error("not an error Launchplane expects: %s", SECRET)

    def record_in_memory(_database: str, _secrets: Secrets) -> PretendRecord:
        return PretendRecord()

    def serve_nothing(_server: uvicorn.Server) -> None:
        pass

    monkeypatch.setattr(ContainerHost, "from_environment", lambda: host)
    monkeypatch.setattr("launchplane.api.PostgresRecord", record_in_memory)
    monkeypatch.setattr(Worker, "start", start_and_fail)
    monkeypatch.setattr(uvicorn.Server, "run", serve_nothing)
    api.serve()
    written: str = capsys.readouterr().err
    assert (writing_through, SECRET in written, written.count(SECRET_REMOVED)) == ([True], False, 1)


def test_every_log_launchplane_writes_as_a_service_is_cleaned_whichever_library_wrote_it(
    written_by_the_service_log: io.StringIO,
) -> None:
    logging.getLogger("uvicorn.error").error("Exception in ASGI application: %s", SECRET)
    logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "192.168.1.9:5", "GET", f"/running?{SECRET}", "1.1", 200)
    logging.getLogger("docker.some.library").warning("retrying after %s", SECRET)
    assert SECRET not in written_by_the_service_log.getvalue()
    assert written_by_the_service_log.getvalue().count(SECRET_REMOVED) == 3


def test_no_part_of_the_signed_link_a_manifest_is_downloaded_from_is_logged(written_by_the_service_log: io.StringIO) -> None:
    git_host = serve(answers(), 0)
    threading.Thread(target=git_host.serve_forever, daemon=True).start()
    builds = Builds(lambda _product: TK, GitHub(base_url=f"http://127.0.0.1:{git_host.server_port}"))
    assert builds.asked("tk", None, RUN) == build_of(RUN)
    git_host.shutdown()
    written = written_by_the_service_log.getvalue()
    assert (SIGNATURE in written, "/signed/" in written) == (False, False)


def holding_the_secret(docker: RecordingDocker, **labels: str) -> None:
    marks = {"launchplane.product": "site", "launchplane.secret-variables": "PGPASSWORD"} | labels
    docker.containers.create(
        "site:a", name="site-1", labels=marks, environment={"PGPASSWORD": SECRET}, volumes={}, restart_policy={"Name": ""}
    )


def just_restarted(docker: RecordingDocker) -> TestClient:
    host = ContainerHost(docker.as_client(), "test")
    lanes = Lanes(host, PretendProxy(PretendHost()))
    return TestClient(app_working_at_once(lanes.read_status, STOPPED, host))


def test_an_error_about_the_read_itself_is_cleaned_of_a_secret_that_same_read_found() -> None:
    docker = RecordingDocker()
    holding_the_secret(docker, **{"launchplane.part": SECRET})
    response = just_restarted(docker).get("/running")
    assert (response.status_code, SECRET in response.text, SECRET_REMOVED in response.text) == (503, False, True)


def test_a_request_refused_before_any_reading_is_cleaned_of_a_secret_a_container_holds_just_after_a_restart() -> None:
    docker = RecordingDocker()
    holding_the_secret(docker)
    response = just_restarted(docker).post(MOVE, json={"build": [SECRET]})
    assert (response.status_code, SECRET in response.text, SECRET_REMOVED in response.text) == (422, False, True)


def test_a_secret_is_noted_from_each_container_as_it_is_described_before_a_later_one_refuses() -> None:
    docker = RecordingDocker()
    holding_the_secret(docker)
    later = f"site-{SECRET}"
    docker.containers.create(
        "site:a", name=later, labels={"launchplane.product": "site"}, environment={}, volumes={}, restart_policy={"Name": ""}
    )
    docker.api.refuses_to_describe.add(later)
    response = just_restarted(docker).get("/running")
    assert (response.status_code, SECRET in response.text, SECRET_REMOVED in response.text) == (503, False, True)
