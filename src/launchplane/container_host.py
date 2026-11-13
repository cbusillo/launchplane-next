import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

PRODUCT_LABEL = "launchplane.product"
LANE_LABEL = "launchplane.lane"
BUILD_LABEL = "launchplane.build"

LISTING_TIMEOUT_SECONDS = 20.0


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


def json_field(name: str, template_value: str) -> str:
    return f'"{name}":{{{{json {template_value}}}}}'


def label_value(label: str) -> str:
    return f'(.Label "{label}")'


ROW_TEMPLATE = (
    "{"
    + ",".join(
        [
            json_field("product", label_value(PRODUCT_LABEL)),
            json_field("lane", label_value(LANE_LABEL)),
            json_field("build", label_value(BUILD_LABEL)),
            json_field("state", ".State"),
            json_field("status", ".Status"),
            json_field("container", ".Names"),
            json_field("image", ".Image"),
        ]
    )
    + "}"
)


def listing_command(docker: Sequence[str], docker_host: str) -> list[str]:
    return [
        *docker,
        "--host",
        docker_host,
        "container",
        "ls",
        "--all",
        "--no-trunc",
        "--filter",
        f"label={PRODUCT_LABEL}",
        "--format",
        ROW_TEMPLATE,
    ]


def parse_row(line: str) -> RunningCopy:
    try:
        fields: dict[str, str] = json.loads(line)
        return RunningCopy(**fields)
    except (ValueError, TypeError) as error:
        raise ContainerHostError(f"Docker listed a container as {line!r}, not as the row Launchplane asked for: {error}") from error


def read_copies(docker_host: str, docker: Sequence[str] = ("docker",)) -> list[RunningCopy]:
    attempt = f"Listing the containers marked {PRODUCT_LABEL} on {docker_host}"
    try:
        result = subprocess.run(  # noqa: S603
            listing_command(docker, docker_host),
            capture_output=True,
            text=True,
            timeout=LISTING_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ContainerHostError(f"{attempt} got no answer within {LISTING_TIMEOUT_SECONDS:g} seconds.") from error
    except OSError as error:
        raise ContainerHostError(f"{attempt} could not start the Docker command: {error}") from error
    if result.returncode != 0:
        raise ContainerHostError(f"{attempt} failed (exit {result.returncode}): {result.stderr.strip()}")
    return [parse_row(line) for line in result.stdout.splitlines() if line.strip()]
