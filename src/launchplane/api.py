import json
import os
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from launchplane.container_host import ContainerHostError, RunningCopy, read_copies

type CopyReader = Callable[[], list[RunningCopy]]


def answer_what_is_running(read: CopyReader, host_name: str) -> tuple[HTTPStatus, dict[str, object]]:
    read_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        copies = read()
    except ContainerHostError as error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"host": host_name, "read_at": read_at, "error": str(error)}
    return HTTPStatus.OK, {"host": host_name, "read_at": read_at, "copies": [asdict(copy) for copy in copies]}


def handler_for(read: CopyReader, host_name: str) -> type[BaseHTTPRequestHandler]:
    class LaunchplaneHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/running":
                self.send_json(*answer_what_is_running(read, host_name))
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": f"Nothing answers {self.path}. Ask GET /running."})

        def send_json(self, status: HTTPStatus, body: dict[str, object]) -> None:
            payload = json.dumps(body, indent=2).encode() + b"\n"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return LaunchplaneHandler


def serve() -> None:
    docker_host = os.environ["LAUNCHPLANE_DOCKER_HOST"]
    address = os.environ.get("LAUNCHPLANE_LISTEN_ADDRESS", "127.0.0.1")
    port = int(os.environ.get("LAUNCHPLANE_LISTEN_PORT", "8700"))
    handler = handler_for(lambda: read_copies(docker_host), docker_host)
    ThreadingHTTPServer((address, port), handler).serve_forever()
