import json
import subprocess
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

PRODUCT_LABEL = "launchplane.product"
LANE_LABEL = "launchplane.lane"
BUILD_LABEL = "launchplane.build"

SSH_CONNECT_TIMEOUT_SECONDS = 5
ANSWER_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class RunningCopy:
    product: str
    lane: str
    build: str
    state: str
    status: str
    container: str
    image: str


class ContainerHostError(Exception):
    pass


def docker_api_command(ssh: Sequence[str], ssh_host: str) -> list[str]:
    return [
        *ssh,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
        ssh_host,
        "docker",
        "system",
        "dial-stdio",
    ]


def listing_request() -> bytes:
    filters = urllib.parse.quote(json.dumps({"label": [PRODUCT_LABEL]}))
    return f"GET /containers/json?all=1&filters={filters} HTTP/1.0\r\nHost: docker\r\n\r\n".encode()


def ask_docker(ssh: Sequence[str], ssh_host: str, request: bytes, attempt: str) -> bytes:
    try:
        result = subprocess.run(  # noqa: S603
            docker_api_command(ssh, ssh_host),
            input=request,
            capture_output=True,
            timeout=ANSWER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ContainerHostError(f"{attempt} got no answer within {ANSWER_TIMEOUT_SECONDS:g} seconds.") from error
    except OSError as error:
        raise ContainerHostError(f"{attempt} could not start ssh: {error}") from error
    if result.returncode != 0:
        reason = result.stderr.decode(errors="replace").strip() or "no message"
        raise ContainerHostError(f"{attempt} failed (exit {result.returncode}): {reason}")
    return result.stdout


def response_body(response: bytes, attempt: str) -> bytes:
    head, _, body = response.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
    if status_line.split(" ")[1:2] != ["200"]:
        raise ContainerHostError(f"{attempt}: Docker answered {status_line!r}: {body.decode(errors='replace').strip()}")
    return body


def copy_from(container: dict[str, Any]) -> RunningCopy:
    labels: dict[str, str] = container["Labels"]
    return RunningCopy(
        product=labels[PRODUCT_LABEL],
        lane=labels.get(LANE_LABEL, ""),
        build=labels.get(BUILD_LABEL, ""),
        state=container["State"],
        status=container["Status"],
        container=container["Names"][0].removeprefix("/"),
        image=container["Image"],
    )


def read_copies(ssh_host: str, ssh: Sequence[str] = ("ssh",)) -> list[RunningCopy]:
    attempt = f"Listing the containers marked {PRODUCT_LABEL} on {ssh_host}"
    body = response_body(ask_docker(ssh, ssh_host, listing_request(), attempt), attempt)
    try:
        containers: list[dict[str, Any]] = json.loads(body)
        return [copy_from(container) for container in containers]
    except (ValueError, TypeError, KeyError, IndexError) as error:
        raise ContainerHostError(f"{attempt}: Docker's answer was not the container list asked for: {error!r}") from error
