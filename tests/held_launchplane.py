"""Launchplane as it is, with every call it makes on the container host, the proxy and the record able to be held.

This is test harness, run on the control container in place of the service: `python -m tests.held_launchplane`. It
changes nothing Launchplane does. A hold stops one call before it is forwarded, or after it has taken effect and before
its caller has the reply, for as long as the hold stands. Holds are files, so they outlast a kill of Launchplane.

    PUT    /holds/before.proxy.switch        hold every call of that name
    PUT    /holds/after.host.run_in?only=mv  hold only a call whose arguments have that text
    PUT    /holds/proxy-delivery             the proxy goes on serving and loads no changed routes file
    GET    /holds                            what is held, and which holds a call is stopped at now
    DELETE /holds/<point>, DELETE /holds     let go of one, or of all
    POST   /kill                             kill Launchplane as `kill -9` does
"""

import inspect
import json
import os
import re
import signal
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

from launchplane import api
from launchplane.container_host import ContainerHost
from launchplane.proxy import PROXY_NAME, ROUTES_FOLDER, TraefikProxy
from launchplane.record import PostgresRecord

HOLDS = Path(os.environ.get("LAUNCHPLANE_HOLDS", "/var/lib/launchplane/holds"))
PORT = int(os.environ.get("LAUNCHPLANE_HOLDS_PORT", "8701"))
POINT = re.compile(r"(before|after)\.(host|proxy|record)\.[a-z_]+|proxy-delivery")
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


def held_around(point: str, call: Callable[..., Any]) -> Callable[..., Any]:
    def held(*arguments: object, **named: object) -> object:
        given = repr((arguments, named))
        pause(f"before.{point}", given)
        answer = call(*arguments, **named)
        pause(f"after.{point}", given)
        return answer

    return held


# Every method of the part, also one it calls on itself, passes its two holds.
class Held:
    held_as: ClassVar[str]

    def __getattribute__(self, name: str) -> Any:  # noqa: ANN401 whatever the part's attribute is
        value = object.__getattribute__(self, name)
        if name.startswith("_") or not inspect.ismethod(value):
            return value
        return held_around(f"{type(self).held_as}.{name}", value)


class HeldHost(Held, ContainerHost):
    held_as = "host"


class HeldProxy(Held, TraefikProxy):
    held_as = "proxy"


class HeldRecord(Held, PostgresRecord):
    held_as = "record"


def hold_proxy_delivery(host: ContainerHost, held: bool) -> None:
    if held:
        host.place_file(PROXY_NAME, UNREADABLE_ROUTES_FILE, "[")
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
        def log_message(self, format: str, *args: object) -> None:
            pass

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
    api.ContainerHost, api.TraefikProxy, api.PostgresRecord = HeldHost, HeldProxy, HeldRecord
    api.serve()


if __name__ == "__main__":
    main()
