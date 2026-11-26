"""Two builds made on the scratch host for the update watch, which differ by one stored field of `website`.

Both are TK's later build with the module `lp_fixture` added and with a start that does what the shared Odoo tools do for
a site: a website bootstrap first, and the HTTP server only after it. The bootstrap of the new build reads the field the
new build adds, so it ends with PostgreSQL's `UndefinedColumn` on a database the new build has not updated, and no HTTP
server is started there. Nothing here is Launchplane, and the data is invented.
"""

import io
import json
import tarfile

from launchplane.odoo import SERVER
from launchplane.product import Product
from tests.bed import on_control, on_scratch, on_scratch_with_bytes
from tests.odoo_product import TK
from tests.recorded_git_host import BED_BUILDS, LATER_RUN, build_of

IMAGE = "launchplane-test-fixture/website-field"
MODULE, FIELD = "lp_fixture", "lp_greeting"
# Runs GitHub never had: the recorded git host answers each as the later run, publishing the build made here.
OLD_RUN, NEW_RUN = 1, 2
KIND_SERVER = f"{SERVER}.kind"
MANIFEST = {"name": "Launchplane fixture", "version": "1.0", "depends": ["website"], "license": "LGPL-3"}
MODELS = """\
from odoo import fields, models


class Website(models.Model):
    _inherit = "website"
{body}
"""
READS = {OLD_RUN: "name", NEW_RUN: FIELD}
# A start with nothing asked of it is the web start. Anything else, an update or Odoo's shell, is the kind's own server.
START = f"""\
#!/bin/sh
case " $* " in *" --stop-after-init "* | *" shell "*) exec {KIND_SERVER} "$@" ;; esac
{KIND_SERVER} shell --no-http --log-level=error <<'BOOTSTRAP' || exit 1
print("website bootstrap read", env["website"].search([]).mapped("{{reads}}"))
BOOTSTRAP
exec {KIND_SERVER} "$@"
"""
DOCKERFILE = f"""\
FROM {build_of(LATER_RUN)}
USER root
RUN mv {SERVER} {KIND_SERVER}
COPY --chmod=755 start {SERVER}
COPY {MODULE} /opt/launchplane/addons/{MODULE}
USER ubuntu
"""


def context_of(run: int) -> bytes:
    field = f'    {FIELD} = fields.Char(default="hello")' if run == NEW_RUN else ""
    files = {
        "Dockerfile": DOCKERFILE,
        "start": START.format(reads=READS[run]),
        f"{MODULE}/__manifest__.py": repr(MANIFEST),
        f"{MODULE}/__init__.py": "from . import models\n",
        f"{MODULE}/models.py": MODELS.format(body=field),
    }
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content.encode())
            tar.addfile(member, io.BytesIO(content.encode()))
    return archive.getvalue()


# The scratch host keeps its images by digest, so what it builds is named by one as a published build is.
def build(run: int) -> str:
    tag = f"{IMAGE}:{run}"
    on_scratch_with_bytes(f"docker build --quiet --tag {tag} -", context_of(run))
    return f"{IMAGE}@{on_scratch(f"docker image inspect --format '{{{{.Id}}}}' {tag}").strip()}"


# Both builds, by run, told to the recorded git host before it is started.
def publish_builds() -> dict[int, str]:
    builds = {run: build(run) for run in (OLD_RUN, NEW_RUN)}
    on_control(f"cat > {BED_BUILDS} && chmod 644 {BED_BUILDS}", given=json.dumps(builds))
    return builds


def remove_builds() -> None:
    on_control(f"rm -f {BED_BUILDS}")
    on_scratch(f"docker image rm --force {IMAGE}:{OLD_RUN} {IMAGE}:{NEW_RUN} >/dev/null 2>&1 || true")


# The website route is what its check asks, and a release updates the module that adds the field.
FIXTURE = Product.model_validate(
    TK.model_dump()
    | {"health_path": "/", "odoo": TK.model_dump()["odoo"] | {"image": IMAGE, "modules": ["website", MODULE], "update": [MODULE]}}
)
