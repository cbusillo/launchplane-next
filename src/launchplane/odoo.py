import json

from pydantic import BaseModel, Field

from launchplane.data_set import DATABASE_NAME
from launchplane.instance import Run
from launchplane.workflow import Publisher

# What every build of the kind is, as its shared base makes it: the server, who runs it and where it keeps its files.
SERVER, USER, FILES_PATH = "/odoo/odoo-bin", "ubuntu", "/volumes/data"
# Odoo reads its settings and its database login from its environment. It looks for its database in the one its login
# may connect to, answers on the port every instance does, and runs no scheduled job: that is the background service's.
SETTINGS = {
    "ODOO_RC": "/dev/null",
    "PGDATABASE_SYSTEM": DATABASE_NAME,
    "ODOO_DATA_DIR": FILES_PATH,
    "ODOO_HTTP_INTERFACE": "0.0.0.0",  # noqa: S104 inside its container, on networks with no way out
    "ODOO_HTTP_PORT": "80",
    "ODOO_LIST_DB": "False",
    "ODOO_MAX_CRON_THREADS": "0",
}
# How long an update may take. Its lane shows the back soon page for that long.
UPDATE_DEADLINE_SECONDS = 600
# Run by Odoo's own shell in the instance, so with the registry Odoo loads for its database: each declared module's
# state, and the declared attachment read through Odoo from its file.
CHECK = """\
import hashlib, json
want = json.loads({want!r})
installed = set(env["ir.module.module"].search([("state", "=", "installed")]).mapped("name"))
if missing := sorted(set(want["modules"]) - installed):
    raise SystemExit(f"The database {{env.cr.dbname}} does not have these modules installed: {{', '.join(missing)}}.")
read = [hashlib.sha256(each.raw).hexdigest() for each in env["ir.attachment"].search([("name", "=", want["attachment"])])]
if read != [want["sha256"]]:
    raise SystemExit(f"The attachment {{want['attachment']}} of the database {{env.cr.dbname}} must be there once with the "
                     f"SHA-256 {{want['sha256']}}. Read through Odoo from its file, it has: {{', '.join(read) or 'none'}}.")
"""


# What a product of the kind declares for itself. Its builds are runs of `workflow` in `repository`, published to `image`.
class Odoo(BaseModel, Publisher, extra="forbid", frozen=True):
    addons_path: list[str] = Field(min_length=1)
    modules: list[str] = Field(min_length=1)
    attachment: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The modules a release to another build updates, on a copy of the lane's data. With none, a release copies nothing.
    update: list[str] = []

    @property
    def run(self) -> Run:
        return Run((SERVER,), SETTINGS | {"ODOO_ADDONS_PATH": ",".join(self.addons_path)}, USER, FILES_PATH)

    # Run by the build before its server is started: with a login, the files and no way out, and no scheduled job is run.
    @property
    def update_command(self) -> tuple[str, ...]:
        modules = ",".join(self.update)
        return "timeout", str(UPDATE_DEADLINE_SECONDS), SERVER, "--update", modules, "--stop-after-init", "--no-http"

    @property
    def check(self) -> tuple[str, ...]:
        script = CHECK.format(want=json.dumps(self.model_dump(include={"modules", "attachment", "sha256"})))
        return "sh", "-c", f"exec {SERVER} shell --no-http --log-level=error <<'CHECK'\n{script}CHECK\n"
