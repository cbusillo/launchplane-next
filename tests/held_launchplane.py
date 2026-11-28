"""Launchplane as it is, with every call it makes on the container host, its logins, the proxy and the record able to be held.

A test harness, run on the control container in place of the service: `python -m tests.held_launchplane`. It changes
nothing Launchplane does. A hold stops one call for as long as the hold stands: before the call is forwarded, or after
it has taken effect and before its caller has the reply. Holds are files, so they outlast a kill of Launchplane.
The calls that set a hold and let go of it are in docs/the-move.md.
"""

import inspect
import json
import os
import re
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

import docker
from docker.models.containers import Container

from launchplane import api
from launchplane.act import Lanes
from launchplane.container_host import ContainerHost, RunOptions
from launchplane.docker_answers import CommandResult, ContainerHostError, ContainerHostRefused
from launchplane.host_logins import HostLogins
from launchplane.proxy import PROXY_NAME, ROUTES_FOLDER, TraefikProxy
from launchplane.record import PostgresRecord

HOLDS = Path(os.environ.get("LAUNCHPLANE_HOLDS", "/var/lib/launchplane/holds"))
PORT = int(os.environ.get("LAUNCHPLANE_HOLDS_PORT", "8701"))
POINT = re.compile(
    r"(before|after|lose|fail|refuse)\.(host|logins|proxy|record|lanes)\.[a-z_]+|proxy-delivery|interrupt-switch|first-start-(?:before|after|refuse)"
)
M3_BIN = "/tmp/launchplane-m3-bin"  # noqa: S108 inside the test proxy
PROXY_DELIVERY = "proxy-delivery"
# Traefik reads every file of its routes folder or none: one it cannot read keeps it on the routes it has loaded.
UNREADABLE_ROUTES_FILE = ROUTES_FOLDER / "held.toml"
SECONDS_BETWEEN_LOOKS = 0.02
stopped_at: set[str] = set()


def is_held(point: str, arguments: str) -> bool:
    try:
        only = (HOLDS / point).read_text()
    except OSError:
        return False
    return only in arguments


def pause(point: str, arguments: str) -> None:
    if not is_held(point, arguments):
        return
    stopped_at.add(point)
    while is_held(point, arguments):
        time.sleep(SECONDS_BETWEEN_LOOKS)
    stopped_at.discard(point)


def held_once(point: str, arguments: str) -> bool:
    held = is_held(point, arguments)
    if held:
        (HOLDS / point).unlink(missing_ok=True)
    return held


# `before.` and `after.` stop a call for as long as they stand. `fail.` makes it the host that cannot be reached, and
# `refuse.` the host that answers no, for as long as they stand and before anything is forwarded. `lose.` lets the call
# take effect once and, where it succeeded, gives its caller the error of a lost reply in place of the reply.
def held_around(point: str, call: Callable[..., Any]) -> Callable[..., Any]:
    def held(*arguments: object, **named: object) -> object:
        given = repr((arguments, named))
        pause(f"before.{point}", given)
        if is_held(f"fail.{point}", given):
            raise ContainerHostError(f"{point} failed: the test harness holds the host as one that cannot be reached")
        if is_held(f"refuse.{point}", given):
            raise ContainerHostRefused(f"{point} was refused: the test harness holds the host as one that answers no")
        reply = call(*arguments, **named)
        if getattr(reply, "succeeded", True) and held_once(f"lose.{point}", given):
            raise ContainerHostError(f"{point} failed: the test harness lost the reply after the call took effect")
        pause(f"after.{point}", given)
        return reply

    return held


# Every method of the part, also one it calls on itself, passes its two holds.
class Held:
    held_as: ClassVar[str]

    def __getattribute__(self, name: str) -> Any:  # noqa: ANN401 whatever the part's attribute is
        value = object.__getattribute__(self, name)
        if name.startswith("_") or not inspect.ismethod(value):
            return value
        return held_around(f"{type(self).held_as}.{name}", value)


class HeldLogins(Held, HostLogins):
    held_as = "logins"


class HeldHost(Held, ContainerHost):
    held_as = "host"

    def __init__(self, client: docker.DockerClient, address: str) -> None:
        super().__init__(client, address)
        self.logins = HeldLogins(client, address, self.secrets)

    def create(self, options: RunOptions) -> Container:
        command = options.command
        if command and options.labels.get("launchplane.part") == "initializer":
            commit = "env.cr.commit()"
            pause_at_commit = "from pathlib import Path\nimport time\nPath('/tmp/held-at-commit').touch()\ntime.sleep(300)"
            for when, replacement in (
                ("before", f"{pause_at_commit}\n{commit}"),
                ("after", f"{commit}\n{pause_at_commit}"),
                ("refuse", "raise SystemExit('The bed refused the final transaction.')"),
            ):
                if held_once(f"first-start-{when}", options.container):
                    assert commit in command[-1]
                    options = replace(options, command=[*command[:-1], command[-1].replace(commit, replacement)])
        return super().create(options)

    def run_in(self, container: str, command: list[str]) -> CommandResult:
        if held_once("interrupt-switch", repr(command)):
            command = ["env", f"PATH={M3_BIN}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", *command]
        return super().run_in(container, command)


class HeldProxy(Held, TraefikProxy):
    held_as = "proxy"


class HeldRecord(Held, PostgresRecord):
    held_as = "record"


class HeldLanes(Held, Lanes):
    held_as = "lanes"


def hold_proxy_delivery(host: ContainerHost, held: bool) -> None:
    if held:
        host.run_in(PROXY_NAME, ["sh", "-c", f"printf '[' > {UNREADABLE_ROUTES_FILE}"])
    else:
        host.run_in(PROXY_NAME, ["rm", "-f", str(UNREADABLE_ROUTES_FILE)])


def answer(to: BaseHTTPRequestHandler, status: HTTPStatus, body: object) -> None:
    encoded = json.dumps(body).encode()
    to.send_response(status)
    to.send_header("Content-Type", "application/json")
    to.send_header("Content-Length", str(len(encoded)))
    to.end_headers()
    to.wfile.write(encoded)


def what_is_held() -> dict[str, object]:
    return {"held": sorted(each.name for each in HOLDS.iterdir()), "stopped_at": sorted(stopped_at)}


def point_named(to: BaseHTTPRequestHandler) -> str | None:
    name = urlsplit(to.path).path.removeprefix("/holds").removeprefix("/")
    if name and POINT.fullmatch(name) is None:
        answer(to, HTTPStatus.NOT_FOUND, {"error": f"{name} is not a hold: {POINT.pattern}"})
        return None
    return name


def set_hold(host: ContainerHost, point: str, only: str) -> None:
    if point == PROXY_DELIVERY:
        hold_proxy_delivery(host, held=True)
    (HOLDS / point).write_text(only)


def let_go(host: ContainerHost, point: str) -> None:
    for each in [HOLDS / point] if point else list(HOLDS.iterdir()):
        if each.name == PROXY_DELIVERY and each.exists():
            hold_proxy_delivery(host, held=False)
        each.unlink(missing_ok=True)


def holds_handler(host: ContainerHost) -> type[BaseHTTPRequestHandler]:
    class Holds(BaseHTTPRequestHandler):
        # noinspection PyPep8Naming
        def do_GET(self) -> None:
            answer(self, HTTPStatus.OK, what_is_held())

        # noinspection PyPep8Naming
        def do_PUT(self) -> None:
            point = point_named(self)
            if point:
                set_hold(host, point, parse_qs(urlsplit(self.path).query).get("only", [""])[0])
                answer(self, HTTPStatus.OK, what_is_held())

        # noinspection PyPep8Naming
        def do_DELETE(self) -> None:
            point = point_named(self)
            if point is not None:
                let_go(host, point)
                answer(self, HTTPStatus.OK, what_is_held())

        # noinspection PyPep8Naming
        def do_POST(self) -> None:
            if urlsplit(self.path).path == "/kill":
                os.kill(os.getpid(), signal.SIGKILL)
            answer(self, HTTPStatus.NOT_FOUND, {"error": "POST /kill is the only call to post."})

    return Holds


def main() -> None:
    HOLDS.mkdir(parents=True, exist_ok=True)
    holds = ThreadingHTTPServer(("", PORT), holds_handler(ContainerHost.from_environment()))
    threading.Thread(target=holds.serve_forever, daemon=True).start()
    api.ContainerHost, api.TraefikProxy, api.PostgresRecord, api.Lanes = HeldHost, HeldProxy, HeldRecord, HeldLanes
    api.serve()


if __name__ == "__main__":
    main()
