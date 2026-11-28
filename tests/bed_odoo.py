"""What the test bed needs before TK's real Odoo build runs as a lane, and the seed by hand of a lane whose product
declares no first start.

For tests/test_bed_odoo.py and for the watch in docs/the-move.md, which runs it as `python -m tests.bed_odoo`.
Nothing here is Launchplane: the seed is a person's work on a new lane, done once, with invented data only.
"""

import base64
import hashlib
import json
import shlex
import subprocess
import sys
import urllib.request
from http import HTTPStatus
from typing import Any

from launchplane.data_set import client_environment
from launchplane.odoo import FILES_PATH, SERVER, USER
from launchplane.product import Product
from tests.bed import SCRATCH_ADDRESS, ask, launchplane_answers, on_control, on_scratch, retire, wait_for
from tests.odoo_product import ATTACHMENT, ATTACHMENT_BYTES, MODULES, TEST_LOGIN, TK, TK_STARTING, TK_UPDATING
from tests.recorded_git_host import LATER_RUN, PORT, RUN, build_of

GIT_HOST_UNIT = "launchplane-recorded-git-host"
# Launchplane as the service and as its test harness runs it both read the recorded git host.
DROP_INS = " ".join(f"/etc/systemd/system/{unit}.service.d/recorded-git-host.conf" for unit in ("launchplane", "launchplane-held"))
SEED_LOGIN, SEED_PASSWORD = "i_seed", "seed-password"
# Launchplane removes the login by itself at the next move: no container holds it.
MAKE_SEED_LOGIN = f"""
DROP ROLE IF EXISTS {SEED_LOGIN};
CREATE ROLE {SEED_LOGIN} LOGIN IN ROLE app PASSWORD '{SEED_PASSWORD}';
ALTER ROLE {SEED_LOGIN} SET role = 'app';
"""
MAKE_ATTACHMENT = f"""
if not env["ir.attachment"].search([("name", "=", {ATTACHMENT!r})]):
    env["ir.attachment"].create({{"name": {ATTACHMENT!r}, "raw": {ATTACHMENT_BYTES!r}, "mimetype": "application/octet-stream"}})
    env.cr.commit()
"""
# What the copy watch reads before and after: an invented lead with one attachment, and each lead written since.
LEAD, LEAD_FILE = "Invented lead of the copy watch", "invented-lead.txt"
LEAD_BYTES: bytes = b"invented bytes of the lead's attachment\n"
WRITTEN = "Written "
# The watch's product once its lead is written: its check reads the lead's own attachment, and its releases update a module.
UPDATING = TK.model_copy(
    update={
        "odoo": TK_UPDATING.odoo
        and TK_UPDATING.odoo.model_copy(update={"attachment": LEAD_FILE, "sha256": hashlib.sha256(LEAD_BYTES).hexdigest()})
    }
)
# Run by Odoo's own shell in an instance: the lead and its attachment as that instance's Odoo reads them.
WITNESS = f"""
import hashlib, json
lead, file = env["crm.lead"].search([("name", "=", {LEAD!r})]), env["ir.attachment"].search([("name", "=", {LEAD_FILE!r})])
print("witness=" + json.dumps({{"lead": lead.id, "team": lead.team_id.id, "type": lead.type, "attachment": file.id,
    "attached_to": f"{{file.res_model}},{{file.res_id}}", "sha256": hashlib.sha256(file.raw).hexdigest()}}))
"""
# Odoo's own first login, which a lane seeded by hand keeps.
ADMIN_LOGIN, ADMIN_PASSWORD = "admin", "admin"
# Run in a container of the database image that can only read the two volumes of a copy and reaches no network: the
# database is copied once more, into the container itself, where a server is started and answers. The copy is not written.
# noinspection SqlResolve
READ_COPY = """
cp -a /from/. /var/lib/postgresql/ && su postgres -c "pg_ctl --wait --silent --log=/tmp/log start" >&2 || exit 1
ask() {
    echo "$1" | psql --username=postgres --dbname=app --tuples-only --no-align --quiet \
        --set=lead="$LEAD" --set=file="$LEAD_FILE" --set=written="$WRITTEN%"
}
stored=$(ask "select store_fname from ir_attachment where name = :'file'")
echo "lead=$(ask "select type from crm_lead where name = :'lead'")"
echo "attachment=$(sha256sum "/files/filestore/app/$stored" | cut -c1-64)"
echo "written=$(ask "select coalesce(string_agg(name, ',' order by id), '') from crm_lead where name like :'written'")"
"""


# The scratch host pulls with its own login to the registry. Launchplane holds none, so it runs only what is there.
def pull_builds(*runs: int) -> None:
    for build in map(build_of, runs or (RUN, LATER_RUN)):
        on_scratch(f"docker image inspect {build} >/dev/null 2>&1 || docker pull --quiet {build}")


def serve_recorded_git_host() -> None:
    on_control(
        f"systemctl stop {GIT_HOST_UNIT} 2>/dev/null; systemctl reset-failed {GIT_HOST_UNIT} 2>/dev/null; "
        f"systemd-run --quiet --unit={GIT_HOST_UNIT} --uid=launchplane --working-directory=/opt/launchplane "
        "--setenv=PYTHONPATH=/opt/launchplane /opt/launchplane/.venv/bin/python -m tests.recorded_git_host && "
        f"for each in {DROP_INS}; do mkdir -p ${{each%/*}} && "
        f"printf '[Service]\\nEnvironment=LAUNCHPLANE_GIT_HOST=http://127.0.0.1:{PORT}\\n' > $each; done && "
        "systemctl daemon-reload && systemctl restart launchplane"
    )
    wait_for(launchplane_answers, seconds=30)


def stop_recorded_git_host() -> None:
    on_control(
        f"rm -f {DROP_INS}; systemctl daemon-reload; systemctl stop {GIT_HOST_UNIT} 2>/dev/null; systemctl restart launchplane"
    )
    wait_for(launchplane_answers, seconds=30)


def describe(product: str, description: Product = TK) -> None:
    file = f"/var/lib/launchplane/products/{product}.json"
    on_control(
        f"install -d -o launchplane /var/lib/launchplane/products && cat > {file} && chmod 644 {file}",
        given=description.model_dump_json(exclude_defaults=True),
    )


def data_set_of(product: str, lane: str) -> str:
    [found] = [each["data_set"] for each in ask("/running")[1]["data_sets"] if (each["product"], each["lane"]) == (product, lane)]
    return found


# Odoo as the kind runs it, once, on the lane's own database and files, from the build the lane will run.
def odoo_on(data_set: str, build: str, *arguments: str) -> str:
    settings = dict(TK.run.environment) | client_environment(SEED_LOGIN, SEED_PASSWORD)
    given = [argument for name, value in settings.items() for argument in ("--env", f"{name}={value}")]
    on = ["--network", f"launchplane-data-{data_set}", "--volume", f"launchplane-data-{data_set}-files:{FILES_PATH}", "--user", USER]
    return shlex.join(["docker", "run", "--rm", "--interactive", *on, *given, build, SERVER, *arguments])


def seed(product: str, lane: str, modules: list[str] = MODULES, *, attachment: bool = True, build: str = build_of(RUN)) -> None:
    data_set = data_set_of(product, lane)
    psql = "psql --username=postgres --dbname=app --quiet --set=ON_ERROR_STOP=1"
    on_scratch(f"docker exec --interactive {product}-{lane}-database-{data_set} {psql}", given=MAKE_SEED_LOGIN)
    on_scratch(odoo_on(data_set, build, "--init", ",".join(modules), "--stop-after-init", "--no-http", "--log-level=warn"))
    if attachment:
        on_scratch(odoo_on(data_set, build, "shell", "--no-http", "--log-level=error"), given=MAKE_ATTACHMENT)


# A person signed in at a lane's web address, so what is read and written goes through Odoo as its own pages do.
class SignedIn:
    def __init__(self, product: str, lane: str, login: str = ADMIN_LOGIN, password: str = ADMIN_PASSWORD) -> None:
        self.web_address = f"{lane}.{product}.test"
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        self.ask("/web/session/authenticate", db="app", login=login, password=password)

    # noinspection HttpUrlsUsage
    def ask(self, path: str, **given: object) -> Any:  # noqa: ANN401 whatever Odoo answers
        body = json.dumps({"jsonrpc": "2.0", "params": given}).encode()
        headers = {"Host": self.web_address, "Content-Type": "application/json"}
        request = urllib.request.Request(f"http://{SCRATCH_ADDRESS}{path}", body, headers)
        with self.opener.open(request, timeout=60) as response:
            answer = json.loads(response.read())
        if "error" in answer:
            raise SystemExit(f"Odoo at {self.web_address} answered {path} with an error: {answer['error']['data']['message']}")
        return answer["result"]

    def call(self, model: str, method: str, *arguments: object, **named: object) -> Any:  # noqa: ANN401 whatever Odoo answers
        return self.ask(f"/web/dataset/call_kw/{model}/{method}", model=model, method=method, args=list(arguments), kwargs=named)

    # The bytes of the declared attachment, read through Odoo by whoever is signed in.
    def attachment(self) -> bytes:
        [file] = self.call("ir.attachment", "search_read", [("name", "=", ATTACHMENT)], fields=["raw"])
        return base64.b64decode(file["raw"]["content"])

    def make_lead(self) -> None:
        team = self.call("crm.team", "search", [], limit=1)[0]
        leads = self.call("crm.lead", "search", [("name", "=", LEAD)]) or self.call(
            "crm.lead", "create", [{"name": LEAD, "type": "lead", "team_id": team}]
        )
        if not self.call("ir.attachment", "search", [("name", "=", LEAD_FILE)]):
            file = {"name": LEAD_FILE, "res_model": "crm.lead", "res_id": leads[0], "raw": base64.b64encode(LEAD_BYTES).decode()}
            self.call("ir.attachment", "create", [file])

    def write(self, text: str) -> None:
        self.call("crm.lead", "create", [{"name": f"{WRITTEN}{text}", "type": "lead"}])

    # The one record the update watch follows: which lead, on which team, of what type, and its attachment, linked and read.
    def witness(self) -> dict[str, Any]:
        [lead] = self.call("crm.lead", "search_read", [("name", "=", LEAD)], fields=["type", "team_id"])
        [file] = self.call("ir.attachment", "search_read", [("name", "=", LEAD_FILE)], fields=["res_model", "res_id", "raw"])
        return {
            "lead": lead["id"],
            "team": lead["team_id"][0],
            "type": lead["type"],
            "attachment": file["id"],
            "attached_to": f"{file['res_model']},{file['res_id']}",
            "sha256": hashlib.sha256(base64.b64decode(file["raw"]["content"])).hexdigest(),
        }

    # Where under `filestore/app` of the lane's files volume Odoo keeps the bytes of the lead's attachment.
    def stored_as(self) -> str:
        [file] = self.call("ir.attachment", "search_read", [("name", "=", LEAD_FILE)], fields=["store_fname"])
        return file["store_fname"]

    # The first scheduled job, made due long ago. No instance of a lane runs scheduled jobs, so it must stay due.
    def due_job(self, *, make_due: bool = False) -> dict[str, object]:
        [job] = self.call("ir.cron", "search_read", [], fields=["cron_name", "nextcall", "lastcall"], order="id", limit=1)
        if make_due:
            self.call("ir.cron", "write", [job["id"]], {"nextcall": "2020-01-01 00:00:00"})
            return self.due_job()
        return job

    # The lead's type, the SHA-256 of its attachment's bytes as Odoo reads them from its file, and each lead written since.
    def read(self) -> dict[str, str]:
        leads = self.call("crm.lead", "search_read", [("name", "=", LEAD)], fields=["type"])
        files = self.call("ir.attachment", "search_read", [("name", "=", LEAD_FILE)], fields=["raw"])
        written = self.call("crm.lead", "search_read", [("name", "like", f"{WRITTEN}%")], fields=["name"], order="id")
        return {
            "lead": ",".join(each["type"] for each in leads),
            "attachment": ",".join(hashlib.sha256(base64.b64decode(each["raw"]["content"])).hexdigest() for each in files),
            "written": ",".join(each["name"] for each in written),
        }


# The instance the web address is sent to, its build and its volumes, as the host shows them, then what Odoo reads there.
def read_lane(product: str, lane: str) -> dict[str, str]:
    running = ask("/running")[1]
    [instance] = [each for each in running["instances"] if each["receives_visitors_for"] == f"{lane}.{product}.test"]
    [data_set] = [each for each in running["data_sets"] if each["data_set"] == instance["data_set"]]
    volumes = ",".join((*data_set["volumes"], *data_set["files"]))
    return {"instance": instance["container"], "build": instance["build"], "volumes": volumes} | SignedIn(product, lane).read()


# The same record read by the Odoo of one instance, which need not receive visitors.
def witness_in(container: str) -> dict[str, Any]:
    said = on_scratch(f"docker exec --interactive {container} {SERVER} shell --no-http --log-level=error", given=WITNESS)
    return json.loads(next(line for line in said.splitlines() if line.startswith("witness=")).removeprefix("witness="))


def copies_of(product: str, lane: str) -> list[dict[str, Any]]:
    kept = [each for each in ask("/running")[1]["data_sets"] if (each["product"], each["lane"]) == (product, lane)]
    return [each for each in kept if each["copy_of"] is not None]


# Read from the copy's two volumes and from nothing else.
def read_copy(copy: dict[str, Any]) -> dict[str, str]:
    mounted = ["--volume", f"{copy['volumes'][0]}:/from:ro", "--volume", f"{copy['files'][0]}:/files:ro"]
    named = ["--env", f"LEAD={LEAD}", "--env", f"LEAD_FILE={LEAD_FILE}", "--env", f"WRITTEN={WRITTEN}"]
    run = ["docker", "run", "--rm", "--interactive", "--network", "none", *mounted, *named, "postgres:18-alpine", "sh"]
    read = dict(line.split("=", 1) for line in on_scratch(shlex.join(run), given=READ_COPY).splitlines())
    return {"data_set": copy["data_set"], "volumes": ",".join((*copy["volumes"], *copy["files"]))} | read


SECRETS = "/var/lib/launchplane/secrets"
CHANGED = "changed-by-the-watch-0123456789"


# The password of a lane's testing login, read where Launchplane keeps it and from nowhere else. It is never printed, and
# no test keeps it in a name of its own: a failing assertion shows the values it compared.
def kept_password(product: str, lane: str) -> str:
    return on_control(f"cat {SECRETS}/{product}/{lane}/test-login")


def password_is_kept(product: str, lane: str) -> bool:
    return on_control(f"test -f {SECRETS}/{product}/{lane}/test-login && echo yes || true").strip() == "yes"


# The file is moved aside on the control container and moved back, so what it holds never leaves that container.
def lose_password(product: str, lane: str, *, back: bool = False) -> None:
    kept, aside = f"{SECRETS}/{product}/{lane}/test-login", f"{SECRETS}/{product}/{lane}/lost-by-the-watch"
    on_control(f"mv {aside} {kept}" if back else f"mv {kept} {aside}")


def forget_test_password(product: str) -> None:
    on_control(f"rm -rf {SECRETS}/{product}")


# Whether what Launchplane said anywhere carries the lane's password. Only yes or no leaves this function.
def says_the_password(product: str, lane: str, *said: object) -> bool:
    password = kept_password(product, lane)
    return any(password in str(each) for each in said)


def default_admin_is_refused(product: str, lane: str) -> bool:
    try:
        SignedIn(product, lane)
    except SystemExit as refused:
        return "Access Denied" in str(refused)
    return False


# The testing login signed in at the lane's web address, with the password kept for the lane or with `password`.
def tester(product: str, lane: str, password: str = "") -> SignedIn:
    return SignedIn(product, lane, TEST_LOGIN, password or kept_password(product, lane))


def tester_is_refused(product: str, lane: str, password: str = "") -> bool:
    try:
        tester(product, lane, password)
    except SystemExit as refused:
        return "Access Denied" in str(refused)
    return False


# The tester gives the login another password in Odoo, or gives it the lane's own back.
def change_tester_password(product: str, lane: str, *, back: bool = False) -> None:
    kept = kept_password(product, lane)
    old, new = (CHANGED, kept) if back else (kept, CHANGED)
    tester(product, lane, old).call("res.users", "change_password", old, new)


# The description is what holds a lane to verified builds and to its check, so it outlives everything of the lane.
def retire_with_its_data(product: str, lane: str) -> None:
    found = [each for each in ask("/running")[1]["data_sets"] if (each["product"], each["lane"]) == (product, lane)]
    # A copy goes before the data set it is of, whose volumes its copiers hold.
    kept = [each["data_set"] for each in sorted(found, key=lambda each: each["copy_of"] is None)]
    for data_set in kept or [None]:
        status, answer = retire(product, lane, data_set)
        if status != HTTPStatus.OK:
            raise SystemExit(
                f"Retiring {lane} of {product} did not end, so its description and the recorded git host were left as they "
                f"are. Run the tear-down again once it has: {answer}"
            )


def shown(read: dict[str, str]) -> None:
    print(*(f"{name}: {value}" for name, value in read.items()), sep="\n")  # noqa: T201


def show(do: str, product: str, lane: str, text: str) -> bool:
    if do == "lead":
        SignedIn(product, lane).make_lead()
    elif do == "write":
        SignedIn(product, lane).write(text)
    elif do == "show-copy":
        shown(read_copy(next(each for each in copies_of(product, lane) if each["data_set"] == text)))
    elif do == "sign-in":
        read = hashlib.sha256(tester(product, lane).attachment()).hexdigest()
        refused = "refused" if default_admin_is_refused(product, lane) else "signed in"
        shown({f"signed in as {TEST_LOGIN}, attachment": read, f"{ADMIN_LOGIN} with Odoo's default password": refused})
    if do in ("lead", "write", "show"):
        shown(read_lane(product, lane))
    return do in ("lead", "write", "show", "show-copy", "sign-in")


def main(do: str, product: str = "watch-tk", lane: str = "testing", text: str = "") -> None:
    if show(do, product, lane, text):
        return
    if do in ("set-up", "set-up-first-start"):
        pull_builds()
        serve_recorded_git_host()
        describe(product, TK if do == "set-up" else TK_STARTING)
    elif do == "seed":
        seed(product, lane)
    elif do == "tear-down":
        retire_with_its_data(product, lane)
        stop_recorded_git_host()
        on_control(f"rm -f /var/lib/launchplane/products/{product}.json")
        forget_test_password(product)
    else:
        raise SystemExit(
            "python -m tests.bed_odoo set-up|set-up-first-start|seed|tear-down|lead|show|write|show-copy|sign-in "
            "[product] [lane] [text or copy]"
        )
    print(json.dumps({"done": do, "product": product, "builds": {str(RUN): build_of(RUN), str(LATER_RUN): build_of(LATER_RUN)}}))  # noqa: T201


if __name__ == "__main__":
    try:
        main(*sys.argv[1:])
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"{error}\n{error.stderr}") from error
