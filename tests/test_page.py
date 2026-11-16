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


def page_showing(browser: Browser, judged: LaneVerdict | None) -> Page:
    def read(_routes: dict[str, str]) -> Judged:
        if judged is None:
            raise ContainerHostError("Connection refused")
        return Judged(OnHost([SERVING], [], []), [judged])

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


def verdict(problems: list[str], release_under_way: list[str]) -> LaneVerdict:
    return LaneVerdict(
        product="site", lane="testing", problems=problems, release_under_way=release_under_way, seconds_since_anything_was_made=4.2
    )


def test_the_page_shows_what_the_judge_finds_wrong_with_a_lane_in_the_judges_words_as_wrong(browser: Browser) -> None:
    page = page_showing(browser, verdict([WRONG], []))
    assert page.locator("[data-health=bad]").all_inner_texts() == [WRONG]
    assert page.locator("[data-finding=release]").count() == 0


def test_the_page_shows_a_release_under_way_as_that_and_marks_none_of_it_as_wrong(browser: Browser) -> None:
    page = page_showing(browser, verdict([], [BESIDE]))
    assert page.locator("[data-finding=release]").all_inner_texts() == [
        "A release is under way. The last thing made in this lane was made 4 s ago.",
        BESIDE,
    ]
    assert page.locator("[data-health=bad]").count() == 0


def test_the_page_shows_what_is_wrong_during_a_release_as_wrong_beside_the_release(browser: Browser) -> None:
    page = page_showing(browser, verdict([WRONG], [BESIDE]))
    assert page.locator("[data-health=bad]").all_inner_texts() == [WRONG]
    assert BESIDE in page.locator("[data-finding=release]").all_inner_texts()


def test_the_page_shows_a_lane_with_nothing_wrong_and_no_release_with_neither(browser: Browser) -> None:
    page = page_showing(browser, verdict([], []))
    assert page.locator("[data-finding]").count() == 0
    assert page.locator("[data-health=bad]").count() == 0
    assert "answering" in page.inner_text("main")


def test_the_page_says_the_host_cannot_be_read_with_the_real_error_and_lists_nothing(browser: Browser) -> None:
    page = page_showing(browser, None)
    assert "Connection refused" in page.inner_text("body")
    assert page.locator("main section").count() == 0
