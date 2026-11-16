import copy
import os
from collections.abc import Awaitable, Callable, Collection, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, TextIO

import uvicorn
import uvicorn.config
from fastapi import FastAPI, Request, Response
from fastapi import Path as PathParameter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from launchplane.act import Judged, Lanes, Stopped, lanes_on
from launchplane.container_host import ContainerHost
from launchplane.docker_answers import ContainerHostError, OnHost
from launchplane.instance import (
    BACK_SOON,
    DataSet,
    HostNetwork,
    LaneBuild,
    ProxySeen,
    RunningBackgroundService,
    RunningInstance,
    web_address_of,
)
from launchplane.judge import LaneVerdict
from launchplane.product import ProductError, ProductFolder
from launchplane.proxy import ProxyError, TraefikProxy
from launchplane.secret_boundary import CleanedStream, Secrets, cleaned, cleaned_body
from launchplane.steps import MaintenanceOutcome, MaintenanceResult, MoveResult, Outcome, RetireResult

type HostReader = Callable[[], Judged]
type WebAddressChecker = Callable[[str], str | None]
type MoveRequester = Callable[[LaneBuild], MoveResult]
type RetireRequester = Callable[[str, str, str | None], RetireResult]
type MaintenanceRequester = Callable[[str, str], MaintenanceResult]
type Lane = tuple[str, str | None]

SLUG = PathParameter(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
UNREADABLE = (ContainerHostError, ProxyError, ProductError, Stopped)
PRODUCTS_FOLDER = "/var/lib/launchplane/products"


class InstanceAnswer(RunningInstance):
    receives_visitors_for: str | None


class WebAddressAnswer(BaseModel):
    web_address: str
    product: str | None
    lane: str | None
    sends_to_container: str | None
    answers: bool
    shows_back_soon_page: bool
    error: str | None


class RunningAnswer(BaseModel):
    read_at: datetime
    instances: list[InstanceAnswer]
    background_services: list[RunningBackgroundService]
    data_sets: list[DataSet]
    networks: list[HostNetwork]
    web_addresses: list[WebAddressAnswer]
    lanes: list[LaneVerdict]


class HostUnreadable(BaseModel):
    read_at: datetime
    error: str


class MoveRequest(BaseModel):
    build: str


class RetireRequest(BaseModel):
    delete_data_set: str | None = None


class NotFinished(BaseModel):
    error: str


RUNNING_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {HTTPStatus.SERVICE_UNAVAILABLE.value: {"model": HostUnreadable}}


def responses_when_not_done(result: type[BaseModel]) -> dict[int | str, dict[str, Any]]:
    return {
        HTTPStatus.UNPROCESSABLE_CONTENT.value: {"model": result},
        HTTPStatus.SERVICE_UNAVAILABLE.value: {"model": NotFinished},
    }


MOVE_ERROR_RESPONSES = responses_when_not_done(MoveResult)
RETIRE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {HTTPStatus.SERVICE_UNAVAILABLE.value: {"model": NotFinished}}
MAINTENANCE_ERROR_RESPONSES = responses_when_not_done(MaintenanceResult)
NOT_A_SUCCESS = {
    MaintenanceOutcome.DATABASE_KILLED,
    MaintenanceOutcome.WEB_PART_FAILED,
    MaintenanceOutcome.BACKGROUND_SERVICE_FAILED,
}


def instance_answers(instances: list[RunningInstance], routes: dict[str, str]) -> list[InstanceAnswer]:
    web_address_of_container = {container: web_address for web_address, container in routes.items()}
    return [
        InstanceAnswer(**instance.model_dump(), receives_visitors_for=web_address_of_container.get(instance.container))
        for instance in instances
    ]


def lane_owners(on_host: OnHost, routes: dict[str, str]) -> dict[str, Lane | None]:
    by_container = {instance.container: instance for instance in on_host.instances}
    owners: dict[str, Lane | None] = {
        web_address: None if (instance := by_container.get(container)) is None else (instance.product, instance.lane)
        for web_address, container in routes.items()
    }
    for product, lane in lanes_on(on_host):
        owners[web_address_of(product, lane)] = (product, lane)
    return owners


def web_address_answer(web_address: str, owner: Lane | None, proxy: ProxySeen, failure: str | None) -> WebAddressAnswer:
    sends_to = proxy.sends_visitors_to().get(web_address)
    if not proxy.runs:
        error = f"The proxy is not running, so {web_address} answers nothing."
    else:
        error = f"The proxy sends {web_address} to no instance." if sends_to is None else failure
    return WebAddressAnswer(
        web_address=web_address,
        product=None if owner is None else owner[0],
        lane=None if owner is None else owner[1],
        sends_to_container=sends_to,
        answers=error is None and sends_to != BACK_SOON,
        shows_back_soon_page=sends_to == BACK_SOON and proxy.back_soon_runs,
        error=error,
    )


def failures_at_once(web_addresses: list[str], why_web_address_fails: WebAddressChecker) -> dict[str, str | None]:
    if not web_addresses:
        return {}
    with ThreadPoolExecutor(max_workers=len(web_addresses)) as pool:
        return dict(zip(web_addresses, pool.map(why_web_address_fails, web_addresses), strict=True))


def web_address_answers(judged: Judged, why_web_address_fails: WebAddressChecker) -> list[WebAddressAnswer]:
    routes = judged.proxy.sends_visitors_to()
    owners = lane_owners(judged.on_host, routes)
    to_a_product = sorted(web_address for web_address, container in routes.items() if container != BACK_SOON)
    failures = failures_at_once(to_a_product, why_web_address_fails)
    return [
        web_address_answer(web_address, owners[web_address], judged.proxy, failures.get(web_address))
        for web_address in sorted(owners)
    ]


def not_answered(error: Exception, secrets: Secrets) -> Response:
    why = cleaned(str(error), secrets.secrets_seen())
    return JSONResponse(
        NotFinished(
            error="Launchplane could not read which secrets it has given out, so it made no answer: it could not have "
            f"cleaned one of them. Nothing was changed, and asking again is safe: {why}"
        ).model_dump(),
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
    )


def cleaned_headers(headers: Mapping[str, str], secrets: Collection[str]) -> dict[str, str]:
    return {cleaned(name, secrets): cleaned(value, secrets) for name, value in headers.items() if name.lower() != "content-length"}


def clean_answers(app: FastAPI, secrets: Secrets, static_path: str) -> None:
    @app.middleware("http")
    async def clean_the_answer(request: Request, answer: Callable[[Request], Awaitable[Any]]) -> Response:
        try:
            await run_in_threadpool(secrets.read_secrets)
        except UNREADABLE as error:
            # The page is a file of Launchplane's own, so it can open and say the host cannot be read.
            if request.url.path != static_path:
                return not_answered(error, secrets)
        response = await answer(request)
        body = b"".join([chunk async for chunk in response.body_iterator])
        given_out = secrets.secrets_seen()
        headers = cleaned_headers(response.headers, given_out)
        return Response(cleaned_body(body, headers.get("content-type", ""), given_out), response.status_code, headers)


def cleaned_log_config(secrets: Secrets) -> dict[str, Any]:
    def cleaned_stream(stream: TextIO) -> CleanedStream:
        return CleanedStream(secrets, stream)

    config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    for handler in config["handlers"].values():
        del handler["class"]
        handler["()"] = cleaned_stream
    config["root"] = {"handlers": ["default"], "level": "INFO"}
    return config


def running_page() -> str:
    return files("launchplane").joinpath("running.html").read_text()


def maintenance_answer(
    request: MaintenanceRequester, attempt: str, product: str, lane: str, response: Response
) -> MaintenanceResult | NotFinished:
    try:
        result = request(product, lane)
    except UNREADABLE as error:
        response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
        return NotFinished(error=f"{attempt} {lane} of {product} stopped, and is safe to ask again: {error}")
    if result.outcome in NOT_A_SUCCESS:
        response.status_code = HTTPStatus.UNPROCESSABLE_CONTENT
    return result


def add_maintenance_calls(app: FastAPI, begin: MaintenanceRequester, end: MaintenanceRequester) -> None:
    @app.post("/lanes/{product}/{lane}/maintenance/begin", responses=MAINTENANCE_ERROR_RESPONSES)
    def begin_lane_maintenance(
        product: Annotated[str, SLUG], lane: Annotated[str, SLUG], response: Response
    ) -> MaintenanceResult | NotFinished:
        return maintenance_answer(begin, "Putting into maintenance", product, lane, response)

    @app.post("/lanes/{product}/{lane}/maintenance/end", responses=MAINTENANCE_ERROR_RESPONSES)
    def end_lane_maintenance(
        product: Annotated[str, SLUG], lane: Annotated[str, SLUG], response: Response
    ) -> MaintenanceResult | NotFinished:
        return maintenance_answer(end, "Taking out of maintenance", product, lane, response)


def create_app(
    read: HostReader,
    why_web_address_fails: WebAddressChecker,
    move: MoveRequester,
    retire: RetireRequester,
    secrets: Secrets,
    begin_maintenance: MaintenanceRequester,
    end_maintenance: MaintenanceRequester,
) -> FastAPI:
    app = FastAPI(title="Launchplane")
    page = running_page()

    @app.get("/", response_class=HTMLResponse)
    def show_running() -> str:
        return page

    clean_answers(app, secrets, static_path=app.url_path_for(show_running.__name__))

    @app.get("/running", responses=RUNNING_ERROR_RESPONSES)
    def running(response: Response) -> RunningAnswer | HostUnreadable:
        read_at = datetime.now(UTC)
        try:
            judged = read()
            on_host = judged.on_host
            web_addresses = web_address_answers(judged, why_web_address_fails)
        except UNREADABLE as error:
            response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
            return HostUnreadable(read_at=read_at, error=str(error))
        return RunningAnswer(
            read_at=read_at,
            instances=instance_answers(on_host.instances, judged.proxy.sends_visitors_to()),
            background_services=on_host.background_services,
            data_sets=on_host.data_sets,
            networks=on_host.networks,
            web_addresses=web_addresses,
            lanes=judged.lanes,
        )

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
        if result.outcome in {Outcome.CHECK_FAILED, Outcome.BACKGROUND_SERVICE_FAILED}:
            response.status_code = HTTPStatus.UNPROCESSABLE_CONTENT
        return result

    @app.post("/lanes/{product}/{lane}/retire", responses=RETIRE_ERROR_RESPONSES)
    def retire_lane(
        product: Annotated[str, SLUG], lane: Annotated[str, SLUG], request: RetireRequest, response: Response
    ) -> RetireResult | NotFinished:
        try:
            return retire(product, lane, request.delete_data_set)
        except UNREADABLE as error:
            response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
            return NotFinished(error=f"Retiring {lane} of {product} stopped, and is safe to ask again: {error}")

    add_maintenance_calls(app, begin_maintenance, end_maintenance)
    return app


def serve() -> None:
    address = os.environ.get("LAUNCHPLANE_LISTEN_ADDRESS", "127.0.0.1")
    port = int(os.environ.get("LAUNCHPLANE_LISTEN_PORT", "8700"))
    host = ContainerHost.from_environment()
    proxy = TraefikProxy(host)
    products = ProductFolder(Path(os.environ.get("LAUNCHPLANE_PRODUCTS", PRODUCTS_FOLDER)))
    lanes = Lanes(host, proxy, products.read)
    app = create_app(
        lanes.read_judged,
        proxy.why_web_address_fails,
        lanes.move,
        lanes.retire,
        host.secrets,
        lanes.begin_maintenance,
        lanes.end_maintenance,
    )
    uvicorn.run(app, host=address, port=port, log_config=cleaned_log_config(host.secrets))
