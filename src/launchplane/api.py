import os
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, Path, Response
from pydantic import BaseModel

from launchplane.container_host import ContainerHost, ContainerHostError
from launchplane.move import Mover, MoveResult, Outcome
from launchplane.name_switch import NameSwitch, NameSwitchError
from launchplane.running_copy import LaneCopy, RunningCopy

type CopyReader = Callable[[], list[RunningCopy]]
type ServingReader = Callable[[], dict[str, str]]
type MoveRequester = Callable[[LaneCopy], MoveResult]

SLUG = Path(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
UNREADABLE = (ContainerHostError, NameSwitchError)


class CopyAnswer(RunningCopy):
    serves: str | None


class RunningAnswer(BaseModel):
    read_at: datetime
    copies: list[CopyAnswer]


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


def create_app(read: CopyReader, read_serving: ServingReader, move: MoveRequester) -> FastAPI:
    app = FastAPI(title="Launchplane")

    @app.get("/running", responses=RUNNING_ERROR_RESPONSES)
    def running(response: Response) -> RunningAnswer | HostUnreadable:
        read_at = datetime.now(UTC)
        try:
            copies = answers_for(read(), read_serving())
        except UNREADABLE as error:
            response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
            return HostUnreadable(read_at=read_at, error=str(error))
        return RunningAnswer(read_at=read_at, copies=copies)

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
    app = create_app(host.read_copies, switch.read_serving, Mover(host, switch).move)
    uvicorn.run(app, host=address, port=port)
