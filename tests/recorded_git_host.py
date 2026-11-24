"""A git host that answers what GitHub answered for two publishing runs of TK's `Build` workflow.

The answers are in tests/answers/git_host, one folder per run: the run, its artifacts, and its manifest cut to what
Launchplane reads. The manifest is zipped as GitHub hands it out. `python -m tests.recorded_git_host` serves them on
the control container, so a bed test needs no key to GitHub and does not fail when GitHub drops the artifact.
"""

import io
import json
import os
import re
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ANSWERS = Path(__file__).parent / "answers" / "git_host"
REPOSITORY = "cbusillo/odoo-tenant-tk"
WORKFLOW = ".github/workflows/build.yml"
IMAGE = "ghcr.io/cbusillo/odoo-tenant-tk"
RUN, LATER_RUN = 45278647986, 45291692474
PORT = int(os.environ.get("LAUNCHPLANE_RECORDED_GIT_HOST_PORT", "8702"))
ASKED = re.compile(rf"/repos/{REPOSITORY}/actions/(?:runs/(?P<run>\d+)(?P<artifacts>/artifacts)?|artifacts/(?P<artifact>\d+)/zip)")


def recorded(run: int, name: str) -> Any:  # noqa: ANN401 whatever GitHub answered
    return json.loads((ANSWERS / str(run) / f"{name}.json").read_text())


def build_of(run: int) -> str:
    return f"{IMAGE}@{recorded(run, 'manifest')['image']['digest']}"


def zipped(manifest: object) -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as file:
        file.writestr("artifact-manifest.json", json.dumps(manifest))
    return archive.getvalue()


# What the git host answers for each run, by run number. A test changes a copy to make a run GitHub never had.
def answers() -> dict[int, dict[str, Any]]:
    return {run: {name: recorded(run, name) for name in ("run", "artifacts", "manifest")} for run in (RUN, LATER_RUN)}


def serve(runs: dict[int, dict[str, Any]], port: int) -> ThreadingHTTPServer:
    manifests = {each["id"]: found["manifest"] for found in runs.values() for each in found["artifacts"]["artifacts"]}

    class Recorded(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            asked = ASKED.fullmatch(self.path.split("?")[0])
            if asked is not None and asked["artifact"] and int(asked["artifact"]) in manifests:
                self.answer("application/zip", zipped(manifests[int(asked["artifact"])]))
            elif asked is not None and asked["run"] and int(asked["run"]) in runs:
                found = runs[int(asked["run"])]["artifacts" if asked["artifacts"] else "run"]
                self.answer("application/json", json.dumps(found).encode())
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def answer(self, kind: str, body: bytes) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Recorded)


if __name__ == "__main__":
    serve(answers(), PORT).serve_forever()
