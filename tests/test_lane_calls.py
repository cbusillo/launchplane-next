from fastapi.testclient import TestClient

from launchplane.act import Judged
from launchplane.api import MoveRequester, RetireRequester, create_app
from launchplane.container_host import OnHost
from launchplane.decide import MoveResult, Outcome, RetireResult
from launchplane.instance import LaneBuild
from launchplane.proxy import ProxyError
from tests.given_secrets import NO_SECRETS


def nothing_running(_routes: dict[str, str]) -> Judged:
    return Judged(OnHost([], [], []), [])


def unreadable_proxy_on_move(target: LaneBuild) -> MoveResult:
    raise ProxyError(f"Reading the proxy's loaded routes failed while moving to {target.build}: Connection refused")


def unreadable_proxy_on_retire(product: str, lane: str, _delete_data_set: str | None) -> RetireResult:
    raise ProxyError(f"Reading the proxy's loaded routes failed while retiring {lane} of {product}: Connection refused")


def failed_check(target: LaneBuild) -> MoveResult:
    return MoveResult(
        product=target.product,
        lane=target.lane,
        build=target.build,
        web_address="testing.site.test",
        outcome=Outcome.CHECK_FAILED,
        container_receiving_visitors=None,
        error="Connection refused",
    )


def client(move: MoveRequester = failed_check, retire: RetireRequester = unreadable_proxy_on_retire) -> TestClient:
    return TestClient(create_app(nothing_running, dict, lambda _web_address: None, move, retire, NO_SECRETS))


def test_a_move_that_could_not_read_the_proxy_answers_503_with_the_real_error_not_success() -> None:
    response = client(move=unreadable_proxy_on_move).post("/lanes/site/testing/move", json={"build": "site:b"})
    assert response.status_code == 503
    assert "Connection refused" in response.json()["error"]
    assert "safe to ask again" in response.json()["error"]


def test_a_retire_that_could_not_read_the_proxy_answers_503_not_retired() -> None:
    response = client().post("/lanes/site/testing/retire", json={})
    assert response.status_code == 503
    assert "outcome" not in response.json()


def test_a_failed_check_answers_422_not_200() -> None:
    response = client().post("/lanes/site/testing/move", json={"build": "site:b"})
    assert (response.status_code, response.json()["outcome"]) == (422, "check failed")
