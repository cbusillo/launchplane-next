import os
from pathlib import Path
from typing import Literal, cast

import docker
import requests
from docker.errors import DockerException
from docker.tls import TLSConfig
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from launchplane.running_copy import CopyState, RunningCopy

PRODUCT_LABEL = "launchplane.product"
LANE_LABEL = "launchplane.lane"
BUILD_LABEL = "launchplane.build"

DOCKER_API_VERSION = "1.47"
ANSWER_TIMEOUT_SECONDS = 5

type DockerState = Literal["created", "running", "paused", "restarting", "removing", "exited", "dead"]

COPY_STATE_FOR_DOCKER_STATE: dict[DockerState, CopyState] = {
    "created": CopyState.STOPPED,
    "running": CopyState.RUNNING,
    "paused": CopyState.PAUSED,
    "restarting": CopyState.RESTARTING,
    "removing": CopyState.STOPPED,
    "exited": CopyState.STOPPED,
    "dead": CopyState.STOPPED,
}


class ContainerHostError(Exception):
    pass


class DockerContainerSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    names: list[str] = Field(alias="Names", min_length=1)
    state: DockerState = Field(alias="State")
    labels: dict[str, str] = Field(alias="Labels")


DOCKER_CONTAINER_LIST = TypeAdapter(list[DockerContainerSummary])


def copy_from(summary: DockerContainerSummary) -> RunningCopy:
    if PRODUCT_LABEL not in summary.labels:
        raise ContainerHostError(f"Docker listed {summary.names[0]} for the {PRODUCT_LABEL} filter, but it has no such label.")
    return RunningCopy(
        product=summary.labels[PRODUCT_LABEL],
        lane=summary.labels.get(LANE_LABEL),
        build=summary.labels.get(BUILD_LABEL),
        name=summary.names[0].removeprefix("/"),
        state=COPY_STATE_FOR_DOCKER_STATE[summary.state],
    )


def copies_from_answer(answer: object) -> list[RunningCopy]:
    try:
        summaries = DOCKER_CONTAINER_LIST.validate_python(answer)
    except ValidationError as error:
        raise ContainerHostError(f"Docker's container list did not have the expected shape, so nothing was read: {error}") from error
    return [copy_from(summary) for summary in summaries]


class ContainerHost:
    def __init__(self, client: docker.DockerClient, address: str) -> None:
        self.client = client
        self.address = address

    @classmethod
    def from_environment(cls) -> ContainerHost:
        address = os.environ["LAUNCHPLANE_CONTAINER_HOST"]
        credentials = Path(os.environ["LAUNCHPLANE_CONTAINER_HOST_CREDENTIALS"])
        tls = TLSConfig(
            client_cert=(str(credentials / "cert.pem"), str(credentials / "key.pem")),
            ca_cert=str(credentials / "ca.pem"),
            verify=True,
        )
        client = docker.DockerClient(base_url=address, tls=tls, version=DOCKER_API_VERSION, timeout=ANSWER_TIMEOUT_SECONDS)
        return cls(client, address)

    def read_copies(self) -> list[RunningCopy]:
        try:
            answer = cast(object, self.client.api.containers(all=True, filters={"label": PRODUCT_LABEL}))  # pyright: ignore[reportUnknownMemberType]
        except (DockerException, requests.RequestException, ValueError) as error:
            raise ContainerHostError(f"Listing the containers marked {PRODUCT_LABEL} on {self.address} failed: {error}") from error
        return copies_from_answer(answer)
