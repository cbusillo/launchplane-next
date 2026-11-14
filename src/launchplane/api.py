import os
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from importlib.resources import files
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, Path, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from launchplane.container_host import ContainerHost, ContainerHostError
from launchplane.move import Mover, MoveResult, Outcome, lane_name
from launchplane.name_switch import NameSwitch, NameSwitchError
from launchplane.running_copy import LaneCopy, RunningCopy

type CopyReader = Callable[[], list[RunningCopy]]
type ServingReader = Callable[[], dict[str, str]]
type NameChecker = Callable[[str], str | None]
type MoveRequester = Callable[[LaneCopy], MoveResult]

SLUG = Path(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
UNREADABLE = (ContainerHostError, NameSwitchError)


class CopyAnswer(RunningCopy):
    serves: str | None


class NameAnswer(BaseModel):
    name: str
    product: str | None
    lane: str | None
    sends_to: str | None
    answers: bool
    error: str | None


class RunningAnswer(BaseModel):
    read_at: datetime
    copies: list[CopyAnswer]
    names: list[NameAnswer]


class HostUnreadable(BaseModel):
    read_at: datetime
    error: str


class MoveRequest(BaseModel):
    build: str


class MoveNotFinished(BaseModel):
    error: str


RUNNING_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {HTTPStatus.SERVICE_UNAVAILABLE.value: {"model": HostUnreadable}}
MOVE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNPROCESSABLE_CONTENT.value: {"model": MoveResult},
    HTTPStatus.SERVICE_UNAVAILABLE.value: {"model": MoveNotFinished},
}


def answers_for(copies: list[RunningCopy], serving: dict[str, str]) -> list[CopyAnswer]:
    name_served_by = {copy_name: name for name, copy_name in serving.items()}
    return [CopyAnswer(**copy.model_dump(), serves=name_served_by.get(copy.name)) for copy in copies]


def lane_owners(copies: list[RunningCopy], serving: dict[str, str]) -> dict[str, RunningCopy | None]:
    copies_by_name = {copy.name: copy for copy in copies}
    routed = {name: copies_by_name.get(copy_name) for name, copy_name in serving.items()}
    marked = {lane_name(copy.product, copy.lane): copy for copy in copies if copy.lane is not None}
    return routed | marked


def name_answer(name: str, owner: RunningCopy | None, sends_to: str | None, why_name_fails: NameChecker) -> NameAnswer:
    error = f"The proxy sends {name} to no copy." if sends_to is None else why_name_fails(name)
    return NameAnswer(
        name=name,
        product=None if owner is None else owner.product,
        lane=None if owner is None else owner.lane,
        sends_to=sends_to,
        answers=error is None,
        error=error,
    )


def name_answers(copies: list[RunningCopy], serving: dict[str, str], why_name_fails: NameChecker) -> list[NameAnswer]:
    owners = lane_owners(copies, serving)
    return [name_answer(name, owners[name], serving.get(name), why_name_fails) for name in sorted(owners)]


def running_page() -> str:
    return files("launchplane").joinpath("running.html").read_text()


def create_app(read: CopyReader, read_serving: ServingReader, why_name_fails: NameChecker, move: MoveRequester) -> FastAPI:
    app = FastAPI(title="Launchplane")
    page = running_page()

    @app.get("/", response_class=HTMLResponse)
    def show_running() -> str:
        return page

    @app.get("/running", responses=RUNNING_ERROR_RESPONSES)
    def running(response: Response) -> RunningAnswer | HostUnreadable:
        read_at = datetime.now(UTC)
        try:
            copies, serving = read(), read_serving()
            names = name_answers(copies, serving, why_name_fails)
        except UNREADABLE as error:
            response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
            return HostUnreadable(read_at=read_at, error=str(error))
        return RunningAnswer(read_at=read_at, copies=answers_for(copies, serving), names=names)

    @app.post("/lanes/{product}/{lane}/move", responses=MOVE_ERROR_RESPONSES)
    def move_lane(
        product: Annotated[str, SLUG], lane: Annotated[str, SLUG], request: MoveRequest, response: Response
    ) -> MoveResult | MoveNotFinished:
        try:
            result = move(LaneCopy(product=product, lane=lane, build=request.build))
        except UNREADABLE as error:
            response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
            return MoveNotFinished(
                error=f"The move of {lane} of {product} to {request.build} stopped, and is safe to ask again: {error}"
            )
        if result.outcome == Outcome.CHECK_FAILED:
            response.status_code = HTTPStatus.UNPROCESSABLE_CONTENT
        return result

    return app


def serve() -> None:
    address = os.environ.get("LAUNCHPLANE_LISTEN_ADDRESS", "127.0.0.1")
    port = int(os.environ.get("LAUNCHPLANE_LISTEN_PORT", "8700"))
    host = ContainerHost.from_environment()
    switch = NameSwitch(host)
    app = create_app(host.read_copies, switch.read_serving, switch.why_name_fails, Mover(host, switch).move)
    uvicorn.run(app, host=address, port=port)
