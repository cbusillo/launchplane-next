import copy
import os
from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import UTC, datetime
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, TextIO

import uvicorn
import uvicorn.config
from fastapi import FastAPI, Request, Response
from fastapi import Path as PathParameter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from launchplane.act import Lanes, Status, Stopped
from launchplane.container_host import ContainerHost
from launchplane.docker_answers import ContainerHostError
from launchplane.instance import DataSet, HostNetwork, LaneBuild, RunningBackgroundService, RunningInstance
from launchplane.judge import Condition, Finding, Judgement, Shown, Standing, WebAddressJudged, Work
from launchplane.page import Card, cards
from launchplane.product import ProductError, ProductFolder
from launchplane.proxy import ProxyError, TraefikProxy
from launchplane.secret_boundary import CleanedStream, Secrets, cleaned, cleaned_body
from launchplane.steps import MaintenanceOutcome, MaintenanceResult, MoveResult, Outcome, RetireResult

type StatusReader = Callable[[], Status]
type MoveRequester = Callable[[LaneBuild], MoveResult]
type RetireRequester = Callable[[str, str, str | None], RetireResult]
type MaintenanceRequester = Callable[[str, str], MaintenanceResult]

SLUG = PathParameter(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
UNREADABLE = (ContainerHostError, ProxyError, ProductError, Stopped)
PRODUCTS_FOLDER = "/var/lib/launchplane/products"


class InstanceAnswer(RunningInstance):
    receives_visitors_for: str | None


class LaneAnswer(BaseModel):
    product: str
    lane: str
    condition: Condition
    work: Work | None
    a_move_is_at_work: bool
    in_maintenance: bool
    in_maintenance_since: datetime | None
    findings: list[Finding]
    problems: list[str]
    release_under_way: list[str]
    left_unfinished: list[str]
    parts: dict[str, Standing]


class RunningAnswer(BaseModel):
    read_at: datetime
    instances: list[InstanceAnswer]
    background_services: list[RunningBackgroundService]
    data_sets: list[DataSet]
    networks: list[HostNetwork]
    web_addresses: list[WebAddressJudged]
    lanes: list[LaneAnswer]
    cards: list[Card]


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


def lane_answer(judged: Judgement) -> LaneAnswer:
    return LaneAnswer(
        product=judged.lane.product,
        lane=judged.lane.lane,
        condition=judged.condition,
        work=judged.work,
        a_move_is_at_work=judged.work is not None,
        in_maintenance=judged.lane.route.in_maintenance,
        in_maintenance_since=judged.lane.route.back_soon_since,
        findings=list(judged.findings),
        problems=judged.shown_as(Shown.WRONG, Shown.UNREAD),
        release_under_way=judged.shown_as(Shown.UNDER_WAY),
        left_unfinished=judged.shown_as(Shown.LEFT_UNFINISHED),
        parts=dict(judged.parts),
    )


def running_answer(status: Status) -> RunningAnswer:
    on_host = status.reading.parts
    receives = {entry.sends_to_container: entry.web_address for entry in status.web_addresses}
    return RunningAnswer(
        read_at=status.reading.read_at,
        instances=[
            InstanceAnswer(**instance.model_dump(), receives_visitors_for=receives.get(instance.container))
            for instance in on_host.instances
        ],
        background_services=on_host.background_services,
        data_sets=on_host.data_sets,
        networks=on_host.networks,
        web_addresses=status.web_addresses,
        lanes=[lane_answer(judged) for judged in status.lanes],
        cards=cards(status),
    )


def cleaned_headers(headers: Mapping[str, str], secrets: Collection[str]) -> dict[str, str]:
    return {cleaned(name, secrets): cleaned(value, secrets) for name, value in headers.items() if name.lower() != "content-length"}


# A call's own reading of the host notes what is secret there before the call says anything, so the answer is cleaned after.
def clean_answers(app: FastAPI, secrets: Secrets) -> None:
    @app.middleware("http")
    async def clean_the_answer(request: Request, answer: Callable[[Request], Awaitable[Any]]) -> Response:
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
    read: StatusReader,
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

    clean_answers(app, secrets)

    @app.get("/running", responses=RUNNING_ERROR_RESPONSES)
    def running(response: Response) -> RunningAnswer | HostUnreadable:
        try:
            return running_answer(read())
        except UNREADABLE as error:
            response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
            return HostUnreadable(read_at=datetime.now(UTC), error=str(error))

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
    app = create_app(lanes.read_status, lanes.move, lanes.retire, host, lanes.begin_maintenance, lanes.end_maintenance)
    uvicorn.run(app, host=address, port=port, log_config=cleaned_log_config(host))
