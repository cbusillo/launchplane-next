import copy
import os
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import UTC, datetime
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, TextIO

import uvicorn
import uvicorn.config
from fastapi import FastAPI, Query, Request, Response
from fastapi import Path as PathParameter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from launchplane.act import Lanes, Status, Stopped
from launchplane.container_host import ContainerHost
from launchplane.docker_answers import ContainerHostError
from launchplane.instance import DataSet, HostNetwork, RunningBackgroundService, RunningInstance
from launchplane.judge import Condition, Finding, Judgement, Shown, Standing, WebAddressJudged, Work
from launchplane.page import Card, cards
from launchplane.product import ProductError, ProductFolder
from launchplane.proxy import ProxyError, TraefikProxy
from launchplane.record import ENDED, Asked, Kept, PostgresRecord, Record, RecordError, Refused, Stands, database
from launchplane.record import Request as KeptRequest
from launchplane.secret_boundary import CleanedStream, Secrets, cleaned, cleaned_body
from launchplane.steps import MaintenanceResult, MoveResult, RetireResult
from launchplane.worker import Worker

type StatusReader = Callable[[], Status]

SLUG = PathParameter(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
UNREADABLE = (ContainerHostError, ProxyError, ProductError, Stopped)
PRODUCTS_FOLDER = "/var/lib/launchplane/products"
MOST_SECONDS_WAITED = 3600
SECONDS_BETWEEN_READS = 0.1


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
    requests: list[Kept]
    record_error: str | None
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


class RequestsAnswer(BaseModel):
    unfinished: list[Kept]
    ended: list[KeptRequest]


def responses(*models: tuple[HTTPStatus, type[BaseModel]]) -> dict[int | str, dict[str, Any]]:
    answers: dict[int | str, dict[str, Any]] = {}
    for status, model in models:
        answers[status.value] = {"model": model}
    return answers


RUNNING_ERROR_RESPONSES = responses((HTTPStatus.SERVICE_UNAVAILABLE, HostUnreadable))
NOT_KEPT = ((HTTPStatus.CONFLICT, NotFinished), (HTTPStatus.SERVICE_UNAVAILABLE, NotFinished))
NOT_FOUND = (HTTPStatus.NOT_FOUND, NotFinished)


def responses_to_a_request(answer: type[BaseModel]) -> dict[int | str, dict[str, Any]]:
    return responses((HTTPStatus.ACCEPTED, Kept), (HTTPStatus.UNPROCESSABLE_CONTENT, answer), *NOT_KEPT)


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
        requests=status.requests,
        record_error=status.record_error,
        cards=cards(status),
    )


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


# Every secret a container holds is read before the answer is made, so an answer that reads nothing itself is cleaned too.
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


def not_finished(response: Response, status: HTTPStatus, error: str) -> NotFinished:
    response.status_code = status
    return NotFinished(error=error)


# Nobody is known by name yet, so a request says where it was asked from.
def asker(http: Request) -> str:
    caller = http.client
    return "an unknown address" if caller is None else f"the address {caller.host}"


def read_until_ended(record: Record, request: int, seconds: float) -> Kept | None:
    deadline = time.monotonic() + seconds
    while True:
        kept = record.read(request)
        if kept is None or kept.stands in ENDED:
            return kept
        if time.monotonic() >= deadline:
            return kept
        time.sleep(SECONDS_BETWEEN_READS)


# The answer a call gave before requests were kept: the final answer, with the status that goes with it.
def final_answer[Answer: BaseModel](kept: Kept, answer: type[Answer], response: Response) -> Answer | NotFinished | Kept:
    if kept.stands not in ENDED:
        response.status_code = HTTPStatus.ACCEPTED
        return kept
    if kept.answer is None:
        return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, kept.error or f"Request {kept.id} failed and kept no error.")
    if kept.stands is Stands.FAILED:
        response.status_code = HTTPStatus.UNPROCESSABLE_CONTENT
    return answer.model_validate(kept.answer)


class Calls:
    def __init__(self, record: Record, wake: Callable[[], object]) -> None:
        self.record = record
        self.wake = wake

    def keep[Answer: BaseModel](
        self, asked: Asked, answer: type[Answer], http: Request, response: Response, wait: float
    ) -> Answer | NotFinished | Kept:
        try:
            request = self.record.write(asked, asker(http), http.headers.get("idempotency-key"))
        except Refused as error:
            return not_finished(response, HTTPStatus.CONFLICT, str(error))
        except RecordError as error:
            return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, f"{error} Nothing was changed, and asking again is safe.")
        self.wake()
        response.headers["Location"] = f"/requests/{request.id}"
        try:
            kept = read_until_ended(self.record, request.id, wait)
        except RecordError as error:
            why = f"Request {request.id} is kept, and how it stands is not known: {error}"
            return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, why)
        return self.none_such(request.id, response) if kept is None else final_answer(kept, answer, response)

    @staticmethod
    def none_such(request: int, response: Response) -> NotFinished:
        return not_finished(response, HTTPStatus.NOT_FOUND, f"Launchplane's record holds no request {request}.")

    def read(self, request: int, response: Response) -> Kept | NotFinished:
        try:
            kept = self.record.read(request)
        except RecordError as error:
            return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, str(error))
        return kept or self.none_such(request, response)

    # Only a request that needs a person is asked for again or given up, so any other is refused, saying how it stands.
    def answered_by_a_person(self, request: int, answered: KeptRequest | None, response: Response) -> KeptRequest | NotFinished:
        if answered is not None:
            return answered
        kept = self.read(request, response)
        if isinstance(kept, NotFinished):
            return kept
        why = f"Request {request} is {kept.stands}. Only a request that needs a person is asked for again or given up."
        return not_finished(response, HTTPStatus.CONFLICT, f"{why} Nothing was changed.")


WAIT = Query(ge=0, le=MOST_SECONDS_WAITED, description="Seconds to wait for the request to end before answering 202.")


def add_lane_calls(app: FastAPI, calls: Calls) -> None:
    @app.post("/lanes/{product}/{lane}/move", responses=responses_to_a_request(MoveResult))
    def move_lane(
        product: Annotated[str, SLUG],
        lane: Annotated[str, SLUG],
        request: MoveRequest,
        http: Request,
        response: Response,
        wait: Annotated[float, WAIT] = 0,
    ) -> MoveResult | NotFinished | Kept:
        asked = Asked(product=product, lane=lane, work=Work.MOVE, build=request.build)
        return calls.keep(asked, MoveResult, http, response, wait)

    @app.post("/lanes/{product}/{lane}/retire", responses=responses_to_a_request(RetireResult))
    def retire_lane(
        product: Annotated[str, SLUG],
        lane: Annotated[str, SLUG],
        request: RetireRequest,
        http: Request,
        response: Response,
        wait: Annotated[float, WAIT] = 0,
    ) -> RetireResult | NotFinished | Kept:
        asked = Asked(product=product, lane=lane, work=Work.RETIRE, delete_data_set=request.delete_data_set)
        return calls.keep(asked, RetireResult, http, response, wait)

    @app.post("/lanes/{product}/{lane}/maintenance/begin", responses=responses_to_a_request(MaintenanceResult))
    def begin_lane_maintenance(
        product: Annotated[str, SLUG],
        lane: Annotated[str, SLUG],
        http: Request,
        response: Response,
        wait: Annotated[float, WAIT] = 0,
    ) -> MaintenanceResult | NotFinished | Kept:
        return calls.keep(Asked(product=product, lane=lane, work=Work.BEGIN_MAINTENANCE), MaintenanceResult, http, response, wait)

    @app.post("/lanes/{product}/{lane}/maintenance/end", responses=responses_to_a_request(MaintenanceResult))
    def end_lane_maintenance(
        product: Annotated[str, SLUG],
        lane: Annotated[str, SLUG],
        http: Request,
        response: Response,
        wait: Annotated[float, WAIT] = 0,
    ) -> MaintenanceResult | NotFinished | Kept:
        return calls.keep(Asked(product=product, lane=lane, work=Work.END_MAINTENANCE), MaintenanceResult, http, response, wait)


def add_request_calls(app: FastAPI, calls: Calls) -> None:
    @app.get("/requests", responses=responses(NOT_KEPT[1]))
    def requests(response: Response) -> RequestsAnswer | NotFinished:
        try:
            return RequestsAnswer(unfinished=calls.record.unfinished(), ended=calls.record.recently_ended())
        except RecordError as error:
            return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, str(error))

    @app.get("/requests/{request}", responses=responses(NOT_FOUND, NOT_KEPT[1]))
    def request_kept(request: int, response: Response) -> Kept | NotFinished:
        return calls.read(request, response)

    @app.post("/requests/{request}/retry", status_code=HTTPStatus.ACCEPTED, responses=responses(NOT_FOUND, *NOT_KEPT))
    def retry_request(request: int, http: Request, response: Response) -> KeptRequest | NotFinished:
        try:
            again = calls.record.retry(request, asker(http))
        except RecordError as error:
            return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, f"{error} Nothing was changed.")
        if again is not None:
            calls.wake()
            response.headers["Location"] = f"/requests/{again.id}"
        return calls.answered_by_a_person(request, again, response)

    @app.post("/requests/{request}/give-up", responses=responses(NOT_FOUND, *NOT_KEPT))
    def give_up_request(request: int, response: Response) -> KeptRequest | NotFinished:
        try:
            given_up = calls.record.give_up(request)
        except RecordError as error:
            return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, f"{error} Nothing was changed.")
        calls.wake()
        return calls.answered_by_a_person(request, given_up, response)


def create_app(read: StatusReader, record: Record, wake: Callable[[], object], secrets: Secrets) -> FastAPI:
    app = FastAPI(title="Launchplane")
    page = running_page()

    @app.get("/", response_class=HTMLResponse)
    def show_running() -> str:
        return page

    clean_answers(app, secrets, static_path=app.url_path_for(show_running.__name__))

    @app.get("/running", responses=RUNNING_ERROR_RESPONSES)
    def running(response: Response) -> RunningAnswer | HostUnreadable:
        try:
            return running_answer(read())
        except UNREADABLE as error:
            response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
            return HostUnreadable(read_at=datetime.now(UTC), error=str(error))

    calls = Calls(record, wake)
    add_lane_calls(app, calls)
    add_request_calls(app, calls)
    return app


def serve() -> None:
    address = os.environ.get("LAUNCHPLANE_LISTEN_ADDRESS", "127.0.0.1")
    port = int(os.environ.get("LAUNCHPLANE_LISTEN_PORT", "8700"))
    host = ContainerHost.from_environment()
    proxy = TraefikProxy(host)
    products = ProductFolder(Path(os.environ.get("LAUNCHPLANE_PRODUCTS", PRODUCTS_FOLDER)))
    record = PostgresRecord(database())
    lanes = Lanes(host, proxy, products.read, read_unfinished=record.unfinished)
    worker = Worker(record, lanes, host)
    worker.start()
    app = create_app(lanes.read_status, record, worker.wake, host)
    uvicorn.run(app, host=address, port=port, log_config=cleaned_log_config(host))
