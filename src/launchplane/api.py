import os
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus

import uvicorn
from fastapi import FastAPI, Response
from pydantic import BaseModel

from launchplane.container_host import ContainerHost, ContainerHostError
from launchplane.running_copy import RunningCopy

type CopyReader = Callable[[], list[RunningCopy]]


class RunningAnswer(BaseModel):
    read_at: datetime
    copies: list[RunningCopy]


class HostUnreadable(BaseModel):
    read_at: datetime
    error: str


def create_app(read: CopyReader) -> FastAPI:
    app = FastAPI(title="Launchplane")

    @app.get("/running", responses={HTTPStatus.SERVICE_UNAVAILABLE.value: {"model": HostUnreadable}})
    def running(response: Response) -> RunningAnswer | HostUnreadable:
        read_at = datetime.now(UTC)
        try:
            copies = read()
        except ContainerHostError as error:
            response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
            return HostUnreadable(read_at=read_at, error=str(error))
        return RunningAnswer(read_at=read_at, copies=copies)

    return app


def serve() -> None:
    address = os.environ.get("LAUNCHPLANE_LISTEN_ADDRESS", "127.0.0.1")
    port = int(os.environ.get("LAUNCHPLANE_LISTEN_PORT", "8700"))
    uvicorn.run(create_app(ContainerHost.from_environment().read_copies), host=address, port=port)
