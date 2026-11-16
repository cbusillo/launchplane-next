from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright

from launchplane.act import Judged, LaneVerdict
from launchplane.api import create_app
from launchplane.container_host import ContainerHostError, OnHost
from launchplane.decide import MoveResult, RetireResult
from launchplane.instance import LANE_NETWORK, InstanceState, LaneBuild, RunningInstance
from tests.given_secrets import NO_SECRETS

ADDRESS = "http://launchplane.test"
WEB_ADDRESS = "testing.site.test"
WRONG = "The database site-testing-database-d1 has another-volume mounted at /var/lib/postgresql, which it must not have"
BESIDE = "The instance site-testing-2 runs beside the active one"
SERVING = RunningInstance(
    product="site", lane="testing", build="site:a", container="site-testing-1", state=InstanceState.RUNNING, networks=(LANE_NETWORK,)
)


def no_move(target: LaneBuild) -> MoveResult:
    raise AssertionError(f"The page asked for a move to {target.build}")


def no_retire(product: str, lane: str, _delete_data_set: str | None) -> RetireResult:
    raise AssertionError(f"The page asked to retire {lane} of {product}")


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        launched = playwright.chromium.launch()
        yield launched
        launched.close()


def page_showing(browser: Browser, judged: LaneVerdict | None, instance: RunningInstance = SERVING) -> Page:
    def read(_routes: dict[str, str]) -> Judged:
        if judged is None:
            raise ContainerHostError("Connection refused")
        return Judged(OnHost([instance], [], []), [judged])

    app = TestClient(
        create_app(read, lambda: {WEB_ADDRESS: SERVING.container}, lambda _web_address: None, no_move, no_retire, NO_SECRETS)
    )

    def answer_from_launchplane(route: Route) -> None:
        response = app.get(route.request.url.removeprefix(ADDRESS))
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

    page = browser.new_page()
    page.route(f"{ADDRESS}/**", answer_from_launchplane)
    page.goto(f"{ADDRESS}/")
    page.wait_for_selector("main section, #reading[role=alert]")
    return page


def verdict(problems: list[str], under_way: list[str], unfinished: list[str]) -> LaneVerdict:
    return LaneVerdict(product="site", lane="testing", problems=problems, release_under_way=under_way, left_unfinished=unfinished)


def findings_shown(page: Page, kind: str) -> list[str]:
    return page.locator(f"[data-finding={kind}]").all_inner_texts()[1:]


def test_the_page_shows_what_the_judge_finds_wrong_with_a_lane_in_the_judges_words_as_wrong(browser: Browser) -> None:
    page = page_showing(browser, verdict([WRONG], [], []))
    assert page.locator("[data-health=bad]").all_inner_texts() == [WRONG]


def test_the_page_shows_a_release_under_way_as_that_and_marks_none_of_it_as_wrong(browser: Browser) -> None:
    page = page_showing(browser, verdict([], [BESIDE], []))
    assert findings_shown(page, "release") == [BESIDE]
    assert "moving this lane now" in page.inner_text("main")
    assert page.locator("[data-health=bad]").count() == 0


def test_the_page_shows_a_release_left_unfinished_as_wrong_and_says_asking_again_finishes_it(browser: Browser) -> None:
    page = page_showing(browser, verdict([], [], [BESIDE]))
    assert findings_shown(page, "unfinished") == [BESIDE]
    assert page.locator("[data-health=bad]").count() == 2
    assert "Asking for a build again finishes it" in page.inner_text("main")


def test_the_page_shows_what_is_wrong_during_a_release_as_wrong_beside_the_release(browser: Browser) -> None:
    page = page_showing(browser, verdict([WRONG], [BESIDE], []))
    assert page.locator("[data-health=bad]").all_inner_texts() == [WRONG]
    assert findings_shown(page, "release") == [BESIDE]


def test_the_page_colours_nothing_as_wrong_that_the_verdict_does_not_call_wrong_whatever_state_an_instance_is_in(
    browser: Browser,
) -> None:
    page = page_showing(browser, verdict([], [], []), SERVING.model_copy(update={"state": InstanceState.STOPPED}))
    assert "stopped" in page.inner_text("main")
    assert page.locator("[data-finding]").count() == 0
    assert page.locator("[data-health=bad]").count() == 0


def test_the_page_says_the_host_cannot_be_read_with_the_real_error_and_lists_nothing(browser: Browser) -> None:
    page = page_showing(browser, None)
    assert "Connection refused" in page.inner_text("body")
    assert page.locator("main section").count() == 0
