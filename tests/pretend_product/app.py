import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import urllib.request
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType

import psycopg

DATABASE = "connect_timeout=3"
CREATE_ENTRIES = "CREATE TABLE IF NOT EXISTS entries (id bigserial PRIMARY KEY, body text NOT NULL, build text NOT NULL)"
CREATE_BEATS = """
    CREATE TABLE IF NOT EXISTS beats
    (id bigserial PRIMARY KEY, run text NOT NULL, build text NOT NULL, at timestamptz NOT NULL DEFAULT clock_timestamp())
"""
BEAT_SECONDS = 0.2
log = logging.getLogger("pretend")
CALL_OUT_SECONDS = 1
REACH_SECONDS = 3
TELLTALE_VERSION = "telltale"


def version() -> str:
    return os.environ["PRETEND_VERSION"]


def connect() -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(DATABASE, autocommit=True)


def create_tables_once_the_database_answers() -> None:
    while True:
        try:
            with connect() as connection, connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(1)")
                connection.execute(CREATE_ENTRIES)
                connection.execute(CREATE_BEATS)
                return
        except psycopg.OperationalError as error:
            log.info("waiting for the database: %s", error)
            time.sleep(1)


class Pages(BaseHTTPRequestHandler):
    def answer(self, status: HTTPStatus, body: str, content_type: str = "text/plain") -> None:
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    # noinspection PyPep8Naming
    def do_GET(self) -> None:
        if version() == TELLTALE_VERSION:
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE, f"my password is {os.environ['PGPASSWORD']}")
            self.end_headers()
            return
        try:
            with connect() as connection:
                if self.path == "/rows":
                    # noinspection SqlResolve
                    rows = connection.execute("SELECT body, build FROM entries ORDER BY id").fetchall()
                    self.answer(
                        HTTPStatus.OK, json.dumps([{"body": body, "build": build} for body, build in rows]), "application/json"
                    )
                else:
                    connection.execute("SELECT 1")
                    self.answer(HTTPStatus.OK, f"pretend product {version()}\n")
        except psycopg.Error as error:
            self.answer(HTTPStatus.SERVICE_UNAVAILABLE, f"the database did not answer: {error}\n")

    # noinspection PyPep8Naming
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        try:
            with connect() as connection:
                # noinspection SqlResolve
                connection.execute("INSERT INTO entries (body, build) VALUES (%s, %s)", (body, version()))
            self.answer(HTTPStatus.CREATED, "written\n")
        except psycopg.Error as error:
            self.answer(HTTPStatus.SERVICE_UNAVAILABLE, f"the row was not written: {error}\n")


def serve_web() -> None:
    create_tables_once_the_database_answers()
    server = ThreadingHTTPServer(("", 80), Pages)
    server.daemon_threads = False

    def finish_held_requests_then_exit(_signal: int, _frame: FrameType | None) -> None:
        threading.Thread(target=server.shutdown).start()

    signal.signal(signal.SIGTERM, finish_held_requests_then_exit)
    server.serve_forever()
    server.server_close()


def call_out_forever(run: str) -> None:
    while True:
        url = f"{os.environ['PRETEND_CALL_OUT_URL']}?run={run}&build={version()}"
        try:
            with urllib.request.urlopen(url, timeout=CALL_OUT_SECONDS):  # noqa: S310
                log.info("called out to %s", url)
        except OSError as error:
            log.info("could not call out to %s: %s", url, error)
        time.sleep(CALL_OUT_SECONDS)


def run_job() -> None:
    signal.signal(signal.SIGTERM, lambda _signal, _frame: sys.exit(0))
    create_tables_once_the_database_answers()
    run = uuid.uuid4().hex
    threading.Thread(target=call_out_forever, args=(run,), daemon=True).start()
    with connect() as connection:
        while True:
            # noinspection SqlResolve
            connection.execute("INSERT INTO beats (run, build) VALUES (%s, %s)", (run, version()))
            time.sleep(BEAT_SECONDS)


def reach(host: str, port: str) -> None:
    try:
        socket.create_connection((host, int(port)), timeout=REACH_SECONDS).close()
    except OSError as error:
        log.info("could not reach %s:%s: %s", host, port, error)
        sys.exit(1)
    log.info("reached %s:%s", host, port)


def main(arguments: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    match arguments:
        case ["web"]:
            serve_web()
        case ["job"]:
            run_job()
        case ["reach", host, port]:
            reach(host, port)
        case _:
            sys.exit("usage: app.py web | job | reach <host> <port>")


if __name__ == "__main__":
    main(sys.argv[1:])
