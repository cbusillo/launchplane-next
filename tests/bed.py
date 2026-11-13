import json
import os
import shlex
import subprocess
import urllib.error
import urllib.request
from typing import Any

import pytest

LAUNCHPLANE_URL = os.environ.get("LAUNCHPLANE_URL", "")
SCRATCH_SHELL = shlex.split(os.environ.get("LAUNCHPLANE_SCRATCH_SHELL", "lp-scratch"))
CONTROL_SHELL = shlex.split(os.environ.get("LAUNCHPLANE_CONTROL_SHELL", "lp-control"))

needs_test_bed = pytest.mark.skipif(
    not LAUNCHPLANE_URL, reason="needs the test bed: set LAUNCHPLANE_URL, e.g. http://launchplane.shiny:8700"
)


def on_scratch(command: str) -> str:
    return subprocess.run([*SCRATCH_SHELL, command], capture_output=True, text=True, check=True).stdout  # noqa: S603


def on_control(command: str) -> str:
    return subprocess.run([*CONTROL_SHELL, command], capture_output=True, text=True, check=True).stdout  # noqa: S603


def ask(path: str, body: dict[str, Any] | None = None, timeout: float = 60) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(f"{LAUNCHPLANE_URL}{path}", data=data, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def remove_product_copies(product: str) -> None:
    on_scratch(f"docker rm --force $(docker ps --all --quiet --filter label=launchplane.product={product}) 2>/dev/null || true")
