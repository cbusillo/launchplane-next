import copy
import logging.config
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

from launchplane.act import Cancelled, Lanes, Status, Stopped
from launchplane.container_host import ContainerHost
from launchplane.docker_answers import ContainerHostError
from launchplane.instance import DataSet, HostNetwork, RunningBackgroundService, RunningInstance, Verdict
from launchplane.judge import Condition, Finding, Judgement, Shown, Standing, WebAddressJudged, Work
from launchplane.page import Card, cards
from launchplane.product import ProductError, ProductFolder
from launchplane.proxy import ProxyError, TraefikProxy
from launchplane.record import (
    ENDED,
    Asked,
    CarriesASecret,
    Kept,
    PostgresRecord,
    Record,
    RecordError,
    Refused,
    Stands,
    Unconfirmed,
    database,
    how_cancelled,
    how_replaced,
)
from launchplane.record import Request as KeptRequest
from launchplane.secret_boundary import CleanedStream, Secrets, cleaned, cleaned_body
from launchplane.steps import CANCEL_WON, MaintenanceResult, MoveResult, RetireResult
from launchplane.telling import Witness
from launchplane.worker import Carries, Worker, carried_out

type StatusReader = Callable[..., Status]


log = logging.getLogger(__name__)

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
    a_move_is_at_work: bool | None
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
    cancels: list[KeptRequest]
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
        a_move_is_at_work=None if judged.work_unread else judged.work is not None,
        in_maintenance=judged.lane.route.in_maintenance,
        in_maintenance_since=judged.lane.route.back_soon_since,
        findings=list(judged.findings),
        problems=judged.shown_as(Shown.CONFLICTING, Shown.WRONG, Shown.UNREAD),
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
        cancels=status.cancels,
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


def has_ended(kept: Kept) -> bool:
    return kept.stands in ENDED


def read_until(record: Record, request: int, seconds: float, settled: Callable[[Kept], bool] = has_ended) -> Kept | None:
    deadline = time.monotonic() + seconds
    while True:
        kept = record.read(request)
        if kept is None or settled(kept):
            return kept
        if time.monotonic() >= deadline:
            return kept
        time.sleep(SECONDS_BETWEEN_READS)


# The answer a call gave before requests were kept: the final answer, with the status that goes with it.
def final_answer[Answer: BaseModel](kept: Kept, answer: type[Answer], response: Response) -> Answer | NotFinished:
    if kept.stands is Stands.CANCELLED:
        return not_finished(response, HTTPStatus.CONFLICT, how_cancelled(kept))
    if kept.stands is Stands.REPLACED:
        return not_finished(response, HTTPStatus.CONFLICT, how_replaced(kept))
    if kept.answer is None:
        return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, kept.error or f"Request {kept.id} failed and kept no error.")
    if kept.stands is Stands.FAILED:
        response.status_code = HTTPStatus.UNPROCESSABLE_CONTENT
    return answer.model_validate(kept.answer)


NOT_CONFIRMED = (
    "Whether the request was written is not known. If it was, it will be worked. Asking again with the same "
    "Idempotency-Key answers with it if it was; without a key, GET /requests lists it."
)


PART_WAY = "its lane is part way, neither as it was before the request nor as the request would leave it."


# Who a look works for: it is stopped, saying why, before anything on the host is started or changed, or a cancel refused.
def looking(why: str, *, turned_back: bool = False) -> Witness:
    def stop(*_why: str) -> None:
        raise Stopped(why)

    return Witness(hold=stop, cancel_asked=lambda: turned_back, refuse_cancel=stop)


class Calls:
    def __init__(self, record: Record, wake: Callable[[], object], gate: Carries, read: StatusReader) -> None:
        self.record = record
        self.wake = wake
        self.gate = gate
        self.read_status = read

    # A cancel of a move being worked is decided at the gate, and the call that asks carries it there itself.
    def carry_to_the_gate(self, request: KeptRequest) -> None:
        marked = request.stands is Stands.WORKING and request.cancel_asked_at is not None and request.cancel_refused is None
        if request.work is Work.MOVE and marked:
            too_late = self.gate.abandon(request.product, request.lane, request.generation)
            if too_late is not None:
                self.record.refuse_cancel(request.id, too_late)

    # Giving up is no force. With no attempt open, the request's attempts are fenced at the gate, as a take-up fences
    # them, so nothing they sent can still land. The gate's answer and the lane are then read, and one write ends it.
    def give_up(self, request: int, response: Response) -> KeptRequest | NotFinished:
        unchanged = f"Request {request} was not given up"
        try:
            kept = self.record.read(request)
            if kept is None:
                return self.none_such(request, response)
            if kept.stands is not Stands.WORKING or kept.cancel_asked_at is not None:
                why = f"{unchanged}: it is {kept.stands}{'' if kept.cancel_asked_at is None else ', and a cancel was asked of it'}."
                return not_finished(response, HTTPStatus.CONFLICT, f"{why} Only a request at work is given up.")
            last, lane = kept.attempts[-1], (kept.product, kept.lane)
            if last.ended is None:
                return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, f"{unchanged}: it is being worked at this moment.")
            proxy_stopped = looking("the proxy is stopped, and the gate is not asked without starting it.")
            decided = self.gate.fence(*lane, kept.generation, last.id, proxy_stopped).verdict
            if decided is not Verdict.NONE:
                return not_finished(response, HTTPStatus.CONFLICT, f"{unchanged}: the gate answers {decided} for its release.")
            if (why_not := self.why_not_let_go(kept, lane)) is not None:
                return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, f"{unchanged}, and is still at work: {why_not}")
            given_up = self.record.give_up(request, last.id)
        except RecordError as error:
            return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, self.not_known(request, error))
        except UNREADABLE as error:
            return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, f"{unchanged}: {error}")
        if given_up is None:
            why = "a cancel was asked of it" if self.record.cancel_asked(request) else "it was taken up again"
            return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, f"{unchanged}: {why} while its lane was read.")
        return given_up

    # None where the lane is as the request would leave it or as it was before, which for a move is nothing its release made.
    def why_not_let_go(self, kept: Kept, lane: tuple[str, str]) -> str | None:
        as_asked = self.why_it_would_act(kept, turned_back=False)
        if kept.work is Work.MOVE:
            as_before = "; ".join(why for judged in self.read_status(lane).lanes for why in judged.left_by(kept.generation)) or None
        else:
            as_before = self.why_it_would_act(kept, turned_back=True)
        return None if None in (as_asked, as_before) else as_before if as_asked == PART_WAY else as_asked

    # The request's own planner, stopped before it acts or refuses a cancel. None where it has nothing to do and read every login.
    def why_it_would_act(self, kept: Kept, *, turned_back: bool) -> str | None:
        try:
            unread = carried_out(self.gate, kept, 0, looking(PART_WAY, turned_back=turned_back)).logins_not_read
        except Stopped as stopped:
            return str(stopped)
        except Cancelled as cancelled:
            unread = cancelled.put_back.logins_not_read
        return "; ".join(unread) or None

    def keep[Answer: BaseModel](
        self, asked: Asked, answer: type[Answer], http: Request, response: Response, wait: float
    ) -> Answer | NotFinished | Kept:
        try:
            request = self.record.write(asked, asker(http), http.headers.get("idempotency-key"))
        except CarriesASecret as error:
            return not_finished(response, HTTPStatus.UNPROCESSABLE_CONTENT, str(error))
        except Refused as error:
            return not_finished(response, HTTPStatus.CONFLICT, str(error))
        except Unconfirmed as error:
            return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, f"{error} {NOT_CONFIRMED}")
        self.wake()
        response.headers["Location"] = f"/requests/{request.id}"
        try:
            kept = read_until(self.record, request.id, wait)
        except RecordError as error:
            why = f"Request {request.id} is kept, and how it stands is not known: {error}"
            return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, why)
        if kept is None:
            return self.none_such(request.id, response)
        # A call that does not wait gets the same answer even when its request has already ended.
        if wait and kept.stands in ENDED:
            return final_answer(kept, answer, response)
        response.status_code = HTTPStatus.ACCEPTED
        return kept

    @staticmethod
    def not_known(request: int, error: RecordError) -> str:
        return f"{error} Whether request {request} was changed is not known: read it with GET /requests/{request}."

    @staticmethod
    def none_such(request: int, response: Response) -> NotFinished:
        return not_finished(response, HTTPStatus.NOT_FOUND, f"Launchplane's record holds no request {request}.")

    def read(self, request: int, response: Response) -> Kept | NotFinished:
        try:
            kept = self.record.read(request)
        except RecordError as error:
            return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, str(error))
        return kept or self.none_such(request, response)

    def cancel(self, request: int, by: str, wait: float, response: Response) -> Kept | NotFinished:
        try:
            marked = self.record.cancel(request, by)
        except CarriesASecret as error:
            return not_finished(response, HTTPStatus.UNPROCESSABLE_CONTENT, str(error))
        except RecordError as error:
            return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, self.not_known(request, error))
        if marked is None:
            return self.none_such(request, response)
        try:
            self.carry_to_the_gate(marked)
        except (RecordError, *UNREADABLE) as error:
            why = (
                f"Request {request} is marked to be cancelled, and whether that came in time is not known yet: {error} "
                f"Its worker carries the cancel on. Read it with GET /requests/{request}."
            )
            return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, why)
        try:
            kept = read_until(self.record, request, wait, is_cancel_settled)
        except RecordError as error:
            why = f"Request {request} is to be cancelled if it had not ended, and how it stands is not known: {error}"
            return not_finished(response, HTTPStatus.SERVICE_UNAVAILABLE, why)
        if kept is None:
            return self.none_such(request, response)
        why_not = why_not_cancelled(kept)
        if why_not is not None:
            return not_finished(response, HTTPStatus.CONFLICT, why_not)
        if kept.stands is not Stands.CANCELLED:
            response.status_code = HTTPStatus.ACCEPTED
        return kept


# The answer to a cancel is known once it has ended the request, or once it is known that it will not.
def is_cancel_settled(kept: Kept) -> bool:
    return kept.stands is Stands.CANCELLED or why_not_cancelled(kept) is not None


# One sentence for a request no cancel will end, or None while one has ended it or still may.
def why_not_cancelled(kept: Kept) -> str | None:
    ended = f"Request {kept.id} ended as {kept.stands}"
    if kept.cancel_refused is not None:
        return f"{kept.cancel_refused} {ended}." if kept.stands in ENDED else kept.cancel_refused
    if kept.stands is Stands.FAILED and kept.cancel_asked_at is not None and (kept.error or "").startswith(CANCEL_WON):
        return f"{kept.error} {ended}."
    if kept.stands in (Stands.DONE, Stands.FAILED, Stands.REPLACED):
        return f"{ended}, so it was not cancelled. Nothing was changed by this call."
    return None


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

    @app.post("/requests/{request}/give-up", responses=responses(NOT_FOUND, *NOT_KEPT))
    def give_up_request(request: int, response: Response) -> KeptRequest | NotFinished:
        return calls.give_up(request, response)


def add_cancel_call(app: FastAPI, calls: Calls) -> None:
    @app.post("/requests/{request}/cancel", responses=responses((HTTPStatus.ACCEPTED, Kept), NOT_FOUND, *NOT_KEPT))
    def cancel_request(request: int, http: Request, response: Response, wait: Annotated[float, WAIT] = 0) -> Kept | NotFinished:
        return calls.cancel(request, f"a call from {asker(http)}", wait, response)


def create_app(read: StatusReader, record: Record, wake: Callable[[], object], secrets: Secrets, gate: Carries) -> FastAPI:
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

    calls = Calls(record, wake, gate, read)
    add_lane_calls(app, calls)
    add_request_calls(app, calls)
    add_cancel_call(app, calls)
    return app


# A caller is the address its connection came from. A forwarding header is text the caller chose, so it is not read.
def server_config(app: FastAPI, address: str, port: int, secrets: Secrets) -> uvicorn.Config:
    return uvicorn.Config(app, host=address, port=port, log_config=cleaned_log_config(secrets), proxy_headers=False)


def serve() -> None:
    address = os.environ.get("LAUNCHPLANE_LISTEN_ADDRESS", "127.0.0.1")
    port = int(os.environ.get("LAUNCHPLANE_LISTEN_PORT", "8700"))
    host = ContainerHost.from_environment()
    # Before anything that can log is started, so the first line this process writes is already cleaned.
    logging.config.dictConfig(cleaned_log_config(host))
    proxy = TraefikProxy(host)
    products = ProductFolder(Path(os.environ.get("LAUNCHPLANE_PRODUCTS", PRODUCTS_FOLDER)))
    record = PostgresRecord(database(), host)
    lanes = Lanes(host, proxy, products.read, read_unfinished=record.unfinished, read_cancels=record.cancels_answered)
    worker = Worker(record, lanes, host)
    worker.start()
    app = create_app(lanes.read_status, record, worker.wake, host, lanes)
    uvicorn.Server(server_config(app, address, port, host)).run()
