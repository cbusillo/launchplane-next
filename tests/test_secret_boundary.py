import io
import json
import logging
import logging.config
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from launchplane.act import Judged, Stopped
from launchplane.api import cleaned_log_config, create_app
from launchplane.docker_answers import ContainerHostError, OnHost
from launchplane.instance import InstanceState, LaneBuild, RunningInstance
from launchplane.judge import LaneVerdict
from launchplane.secret_boundary import SECRET_REMOVED, CleanedStream, Secrets, cleaned_body
from launchplane.steps import MoveResult, Outcome, RetireOutcome, RetireResult
from tests.given_secrets import GivenSecrets

SECRET = "Zx3-secret_given-to-a-lane-0123456789abcdef"  # noqa: S105 a pretend one
LEAK = f"wget: server returned error: HTTP/1.1 503 password={SECRET}"
ROUTES = {"testing.site.test": "site-testing-1"}


def leaking_instance() -> RunningInstance:
    return RunningInstance(
        product="site", lane="testing", build=f"site:{SECRET}", container="site-testing-1", state=InstanceState.RUNNING, networks=()
    )


def running_with_leaks() -> Judged:
    problems = [f"The instance site-testing-1 has {SECRET} mounted, which it must not have"]
    return Judged(
        OnHost([leaking_instance()], [], []),
        [LaneVerdict(product="site", lane="testing", a_move_is_at_work=False, problems=problems)],
    )


def unreadable_host() -> Judged:
    raise ContainerHostError(f"Docker refused: {SECRET}")


def move_answering(outcome: Outcome) -> Callable[[LaneBuild], MoveResult]:
    def move(target: LaneBuild) -> MoveResult:
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


def move_stopped(_target: LaneBuild) -> MoveResult:
    raise Stopped(f"The lane is not as it must be: {SECRET}")


def retire_answering(product: str, lane: str, _delete_data_set: str | None) -> RetireResult:
    return RetireResult(
        product=product,
        lane=lane,
        web_address="testing.site.test",
        outcome=RetireOutcome.RETIRED,
        route_removed_to_container=None,
        retired_containers=[f"site-testing-{SECRET}"],
    )


def retire_stopped(product: str, lane: str, _delete_data_set: str | None) -> RetireResult:
    raise ContainerHostError(f"Stopping {lane} of {product} failed: {SECRET}")


@dataclass(frozen=True)
class Asked:
    method: str
    path: str
    status: int
    body: dict[str, Any] | None = None
    read: Callable[[], Judged] = running_with_leaks
    move: Callable[[LaneBuild], MoveResult] = move_stopped
    retire: Callable[[str, str, str | None], RetireResult] = retire_stopped


MOVE = "/lanes/site/testing/move"
RETIRE = "/lanes/site/testing/retire"
ANSWERS = {
    "what is running, with a web address whose error carries it": Asked("GET", "/running", 200),
    "a host that could not be read": Asked("GET", "/running", 503, read=unreadable_host),
    "a move that switched": Asked("POST", MOVE, 200, {"build": "site:b"}, move=move_answering(Outcome.SWITCHED)),
    "a move whose check failed": Asked("POST", MOVE, 422, {"build": "site:b"}, move=move_answering(Outcome.CHECK_FAILED)),
    "a move whose job failed": Asked("POST", MOVE, 422, {"build": "site:b"}, move=move_answering(Outcome.JOB_FAILED)),
    "a move that stopped": Asked("POST", MOVE, 503, {"build": "site:b"}),
    "a retirement": Asked("POST", RETIRE, 200, {}, retire=retire_answering),
    "a retirement that stopped": Asked("POST", RETIRE, 503, {}),
    "a request Launchplane refuses, repeating what was sent": Asked("POST", MOVE, 422, {"build": [SECRET]}),
    "a lane name Launchplane refuses, repeating it": Asked("POST", f"/lanes/site/{SECRET}/retire", 422, {}),
    "a redirect to the address asked for, which carries it": Asked("POST", f"/lanes/site/{SECRET}/move/", 307, {}),
}


def answer_to(asked: Asked, secrets: Secrets) -> tuple[int, str]:
    app = create_app(lambda _routes: asked.read(), lambda: ROUTES, lambda _web_address: LEAK, asked.move, asked.retire, secrets)
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
    def move(target: LaneBuild) -> MoveResult:
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

    def read() -> Judged:
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

    def read(_routes: dict[str, str]) -> Judged:
        seen_when_the_host_was_read.append(secrets.secrets_seen())
        raise RuntimeError(f"not an error Launchplane expects: {SECRET}")

    app = create_app(read, lambda: ROUTES, lambda _web_address: LEAK, move_stopped, retire_stopped, secrets)
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
def written_by_the_service_log() -> Iterator[io.StringIO]:
    loggers = [logging.root, *map(logging.getLogger, SERVICE_LOGGERS)]
    before = [(logger, logger.handlers[:], logger.propagate, logger.level) for logger in loggers]
    logging.config.dictConfig(cleaned_log_config(GivenSecrets(seen=frozenset({SECRET}))))
    written = io.StringIO()
    for logger in loggers:
        for handler in logger.handlers:
            assert isinstance(handler, CleanedStream), f"{logger.name} writes through {handler}, which cleans nothing"
            handler.setStream(written)
    yield written
    for logger, handlers, propagate, level in before:
        logger.handlers[:], logger.propagate, logger.level = handlers, propagate, level


def test_every_log_launchplane_writes_as_a_service_is_cleaned_whichever_library_wrote_it(
    written_by_the_service_log: io.StringIO,
) -> None:
    logging.getLogger("uvicorn.error").error("Exception in ASGI application: %s", SECRET)
    logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "192.168.1.9:5", "GET", f"/running?{SECRET}", "1.1", 200)
    logging.getLogger("docker.some.library").warning("retrying after %s", SECRET)
    assert SECRET not in written_by_the_service_log.getvalue()
    assert written_by_the_service_log.getvalue().count(SECRET_REMOVED) == 3
