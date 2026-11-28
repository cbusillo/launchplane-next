import hashlib
import json
from typing import Self

from pydantic import Base64Bytes, BaseModel, Field, model_validator

from launchplane.data_set import DATABASE_NAME
from launchplane.instance import Run

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
# How long installing the declared modules in an empty database may take.
FIRST_START_DEADLINE_SECONDS = 600
TEST_PASSWORD_VARIABLE = "LAUNCHPLANE_TEST_PASSWORD"  # noqa: S105 the name of the variable, not a password
# Run once by the build on a data set its own request made, before any web part starts there. It writes only where
# nothing is: a table in any schema or a file in the files volume ends it with nothing changed. It installs the declared
# modules, and then in one commit makes the testing login, the declared attachment as that login's own, and Odoo's
# administrator one nobody signs in as. It ends with code 0 only after that commit, and says no password when it fails.
HELD_QUERY = (
    "SELECT count(*) FROM pg_class JOIN pg_namespace ON pg_namespace.oid = relnamespace "
    "WHERE nspname !~ '^(pg_|information_schema$)'"
)
FIRST_START = """\
set -e
held=$(psql --no-psqlrc --quiet --tuples-only --no-align --set=ON_ERROR_STOP=1 --command="{held_query}")
files=$(ls -A {files})
if [ "$held" != 0 ] || [ -n "$files" ]; then
    echo "The database {database} holds $held tables and indexes, and its files volume holds: ${{files:-nothing}}. A first \
start writes only where both hold nothing, so nothing was changed."
    exit 1
fi
timeout {deadline} {server} --init {modules} --stop-after-init --no-http
exec {server} shell --no-http --log-level=error <<'SEED'
import base64, json, os, secrets
want, password = json.loads({want!r}), os.environ["{password_variable}"]
try:
    groups = [(6, 0, [env.ref(each).id for each in ("base.group_user", "base.group_partner_manager")])]
    signs_in = {{"name": want["test_login"], "login": want["test_login"], "password": password, "group_ids": groups}}
    tester = env["res.users"].create(signs_in)
    file = {{"name": want["attachment"], "raw": base64.b64decode(want["bytes"]), "mimetype": "application/octet-stream"}}
    env["ir.attachment"].with_user(tester).create(file)
    env.ref("base.user_admin").password = secrets.token_urlsafe(32)
    env.cr.commit()
except Exception as error:
    raise SystemExit(f"The seed failed: {{type(error).__name__}}: {{str(error).replace(password, '[the password]')}}")
SEED
"""
# Run in an instance: one sign-in as a person makes it, asked of that instance alone. It succeeds only with a session.
SIGN_IN = """\
import json, os, sys, urllib.request
ask = {{"jsonrpc": "2.0", "params": {{"db": {database!r}, "login": {login!r}, "password": os.environ["{password_variable}"]}}}}
asked = urllib.request.Request("http://127.0.0.1:80/web/session/authenticate", json.dumps(ask).encode())
asked.add_header("Content-Type", "application/json")
try:
    answer = json.load(urllib.request.urlopen(asked, timeout=20))
except (OSError, ValueError) as error:
    sys.exit(f"Odoo gave no answer to a sign-in: {{type(error).__name__}}.")
sys.exit(None if (answer.get("result") or {{}}).get("uid") else "Odoo refused the testing login " + {login!r} + ".")
"""
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


# What a first start gives the data of a testing lane: the bytes of the declared attachment, and the name of the testing
# login, which signs in with that lane's own password.
class FirstStart(BaseModel, extra="forbid", frozen=True):
    attachment: Base64Bytes
    test_login: str = Field(min_length=1)


# What a product of the kind declares for itself. Its builds are runs of `workflow` in `repository`, published to `image`.
class Odoo(BaseModel, extra="forbid", frozen=True):
    repository: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    workflow: str
    image: str
    addons_path: list[str] = Field(min_length=1)
    modules: list[str] = Field(min_length=1)
    attachment: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The modules a release to another build updates, on a copy of the lane's data. With none, a release copies nothing.
    update: list[str] = []
    first_start: FirstStart | None = None

    @model_validator(mode="after")
    def first_start_gives_the_declared_bytes(self) -> Self:
        given = self.first_start
        if given is not None and hashlib.sha256(given.attachment).hexdigest() != self.sha256:
            raise ValueError("first_start.attachment does not hold the bytes whose SHA-256 is sha256")
        return self

    @property
    def run(self) -> Run:
        return Run((SERVER,), SETTINGS | {"ODOO_ADDONS_PATH": ",".join(self.addons_path)}, USER, FILES_PATH)

    # Run by the build before its server is started: with a login, the files and no way out, and no scheduled job is run.
    @property
    def update_command(self) -> tuple[str, ...]:
        modules = ",".join(self.update)
        return "timeout", str(UPDATE_DEADLINE_SECONDS), SERVER, "--update", modules, "--stop-after-init", "--no-http"

    @property
    def first_start_command(self) -> tuple[str, ...]:
        given = self.first_start
        if given is None:
            return ()
        want = {"attachment": self.attachment, "bytes": given.model_dump(mode="json")["attachment"], "test_login": given.test_login}
        script = FIRST_START.format(
            held_query=HELD_QUERY,
            files=FILES_PATH,
            database=DATABASE_NAME,
            deadline=FIRST_START_DEADLINE_SECONDS,
            server=SERVER,
            modules=",".join(self.modules),
            want=json.dumps(want),
            password_variable=TEST_PASSWORD_VARIABLE,
        )
        return "sh", "-c", script

    @property
    def sign_in_command(self) -> tuple[str, ...]:
        if self.first_start is None:
            return ()
        login = self.first_start.test_login
        return "python3", "-c", SIGN_IN.format(database=DATABASE_NAME, login=login, password_variable=TEST_PASSWORD_VARIABLE)

    @property
    def check(self) -> tuple[str, ...]:
        script = CHECK.format(want=json.dumps(self.model_dump(include={"modules", "attachment", "sha256"})))
        return "sh", "-c", f"exec {SERVER} shell --no-http --log-level=error <<'CHECK'\n{script}CHECK\n"
