import json
from collections.abc import Sequence

from launchplane.data_set import DATABASE_NAME
from launchplane.instance import Run

# What every build of the kind is, as its shared base makes them: the server, who runs it and where it keeps its files.
SERVER = "/odoo/odoo-bin"
USER = "ubuntu"
FILES_PATH = "/volumes/data"
# Odoo reads its settings from its environment, and its database login from the one every instance on a data set is
# given. It looks for its database in the one it may connect to, answers on the port every instance does, and runs no
# scheduled job: the web part is not the background service.
SETTINGS = {
    "ODOO_RC": "/dev/null",
    "PGDATABASE_SYSTEM": DATABASE_NAME,
    "ODOO_DATA_DIR": FILES_PATH,
    "ODOO_HTTP_INTERFACE": "0.0.0.0",  # noqa: S104 inside its container, on networks with no way out
    "ODOO_HTTP_PORT": "80",
    "ODOO_PROXY_MODE": "True",
    "ODOO_LIST_DB": "False",
    "ODOO_MAX_CRON_THREADS": "0",
}
# Run by Odoo's own shell in the instance: the registry it loads for its database, the state of each declared module,
# and the declared attachment read through Odoo from its file.
CHECK = """\
import hashlib, json
want = json.loads({want!r})
if env.cr.dbname != want["database"]:
    raise SystemExit(f"Odoo opened the database {{env.cr.dbname}}, not {{want['database']}}.")
installed = set(env["ir.module.module"].search([("state", "=", "installed")]).mapped("name"))
if missing := sorted(set(want["modules"]) - installed):
    raise SystemExit(f"The database {{env.cr.dbname}} does not have these modules installed: {{', '.join(missing)}}.")
read = [hashlib.sha256(each.raw).hexdigest() for each in env["ir.attachment"].search([("name", "=", want["attachment"])])]
if read != [want["sha256"]]:
    raise SystemExit(
        f"The attachment {{want['attachment']}} must be there once with the SHA-256 {{want['sha256']}}. "
        f"Read through Odoo from its files, it has: {{', '.join(read) or 'no such attachment'}}."
    )
"""


def run(addons_path: Sequence[str]) -> Run:
    return Run((SERVER,), SETTINGS | {"ODOO_ADDONS_PATH": ",".join(addons_path)}, USER, FILES_PATH)


def check_command(modules: Sequence[str], attachment: str, sha256: str) -> tuple[str, ...]:
    want = {"database": DATABASE_NAME, "modules": list(modules), "attachment": attachment, "sha256": sha256}
    script = CHECK.format(want=json.dumps(want))
    return ("sh", "-c", f"exec {SERVER} shell --no-http --log-level=error <<'CHECK'\n{script}CHECK\n")
