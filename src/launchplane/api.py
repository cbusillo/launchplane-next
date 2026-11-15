import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http import HTTPStatus
from importlib.resources import files
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, Path, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from launchplane.act import Lanes, Stopped
from launchplane.container_host import ContainerHost, ContainerHostError
from launchplane.decide import MoveResult, Outcome, RetireResult, web_address_of
from launchplane.instance import LaneBuild, RunningInstance
from launchplane.proxy import ProxyError, TraefikProxy

type InstanceReader = Callable[[], list[RunningInstance]]
type RouteReader = Callable[[], dict[str, str]]
type WebAddressChecker = Callable[[str], str | None]
type MoveRequester = Callable[[LaneBuild], MoveResult]
type RetireRequester = Callable[[str, str], RetireResult]

SLUG = Path(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
UNREADABLE = (ContainerHostError, ProxyError, Stopped)


class InstanceAnswer(RunningInstance):
    receives_visitors_for: str | None


class WebAddressAnswer(BaseModel):
    web_address: str
    product: str | None
    lane: str | None
    sends_to_container: str | None
    answers: bool
    error: str | None


class RunningAnswer(BaseModel):
    read_at: datetime
    instances: list[InstanceAnswer]
    web_addresses: list[WebAddressAnswer]


class HostUnreadable(BaseModel):
    read_at: datetime
    error: str


class MoveRequest(BaseModel):
    build: str


class NotFinished(BaseModel):
    error: str


RUNNING_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {HTTPStatus.SERVICE_UNAVAILABLE.value: {"model": HostUnreadable}}
MOVE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNPROCESSABLE_CONTENT.value: {"model": MoveResult},
    HTTPStatus.SERVICE_UNAVAILABLE.value: {"model": NotFinished},
}
RETIRE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {HTTPStatus.SERVICE_UNAVAILABLE.value: {"model": NotFinished}}


def instance_answers(instances: list[RunningInstance], routes: dict[str, str]) -> list[InstanceAnswer]:
    web_address_of_container = {container: web_address for web_address, container in routes.items()}
    return [
        InstanceAnswer(**instance.model_dump(), receives_visitors_for=web_address_of_container.get(instance.container))
        for instance in instances
    ]


def lane_owners(instances: list[RunningInstance], routes: dict[str, str]) -> dict[str, RunningInstance | None]:
    by_container = {instance.container: instance for instance in instances}
    routed = {web_address: by_container.get(container) for web_address, container in routes.items()}
    marked = {web_address_of(instance.product, instance.lane): instance for instance in instances if instance.lane is not None}
    return routed | marked


def web_address_answer(
    web_address: str, owner: RunningInstance | None, sends_to: str | None, failure: str | None
) -> WebAddressAnswer:
    error = f"The proxy sends {web_address} to no instance." if sends_to is None else failure
    return WebAddressAnswer(
        web_address=web_address,
        product=None if owner is None else owner.product,
        lane=None if owner is None else owner.lane,
        sends_to_container=sends_to,
        answers=error is None,
        error=error,
    )


def failures_at_once(web_addresses: list[str], why_web_address_fails: WebAddressChecker) -> dict[str, str | None]:
    if not web_addresses:
        return {}
    with ThreadPoolExecutor(max_workers=len(web_addresses)) as pool:
        return dict(zip(web_addresses, pool.map(why_web_address_fails, web_addresses), strict=True))


def web_address_answers(
    instances: list[RunningInstance], routes: dict[str, str], why_web_address_fails: WebAddressChecker
) -> list[WebAddressAnswer]:
    owners = lane_owners(instances, routes)
    failures = failures_at_once(sorted(routes), why_web_address_fails)
    return [
        web_address_answer(web_address, owners[web_address], routes.get(web_address), failures.get(web_address))
        for web_address in sorted(owners)
    ]


def running_page() -> str:
    return files("launchplane").joinpath("running.html").read_text()


def create_app(
    read: InstanceReader,
    read_routes: RouteReader,
    why_web_address_fails: WebAddressChecker,
    move: MoveRequester,
    retire: RetireRequester,
) -> FastAPI:
    app = FastAPI(title="Launchplane")
    page = running_page()

    @app.get("/", response_class=HTMLResponse)
    def show_running() -> str:
        return page

    @app.get("/running", responses=RUNNING_ERROR_RESPONSES)
    def running(response: Response) -> RunningAnswer | HostUnreadable:
        read_at = datetime.now(UTC)
        try:
            instances, routes = read(), read_routes()
            web_addresses = web_address_answers(instances, routes, why_web_address_fails)
        except UNREADABLE as error:
            response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
            return HostUnreadable(read_at=read_at, error=str(error))
        return RunningAnswer(read_at=read_at, instances=instance_answers(instances, routes), web_addresses=web_addresses)

    @app.post("/lanes/{product}/{lane}/move", responses=MOVE_ERROR_RESPONSES)
    def move_lane(
        product: Annotated[str, SLUG], lane: Annotated[str, SLUG], request: MoveRequest, response: Response
    ) -> MoveResult | NotFinished:
        try:
            result = move(LaneBuild(product=product, lane=lane, build=request.build))
        except UNREADABLE as error:
            response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
            return NotFinished(
                error=f"The move of {lane} of {product} to {request.build} stopped, and is safe to ask again: {error}"
            )
        if result.outcome == Outcome.CHECK_FAILED:
            response.status_code = HTTPStatus.UNPROCESSABLE_CONTENT
        return result

    @app.post("/lanes/{product}/{lane}/retire", responses=RETIRE_ERROR_RESPONSES)
    def retire_lane(product: Annotated[str, SLUG], lane: Annotated[str, SLUG], response: Response) -> RetireResult | NotFinished:
        try:
            return retire(product, lane)
        except UNREADABLE as error:
            response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
            return NotFinished(error=f"Retiring {lane} of {product} stopped, and is safe to ask again: {error}")

    return app


def serve() -> None:
    address = os.environ.get("LAUNCHPLANE_LISTEN_ADDRESS", "127.0.0.1")
    port = int(os.environ.get("LAUNCHPLANE_LISTEN_PORT", "8700"))
    host = ContainerHost.from_environment()
    proxy = TraefikProxy(host)
    lanes = Lanes(host, proxy)
    app = create_app(host.read_instances, proxy.read_enabled_routes, proxy.why_web_address_fails, lanes.move, lanes.retire)
    uvicorn.run(app, host=address, port=port)
