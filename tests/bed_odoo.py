"""What the test bed needs before TK's real Odoo build runs as a lane, and the one-time seed of a lane's data.

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
from tests.bed import SCRATCH_ADDRESS, ask, launchplane_answers, on_control, on_scratch, retire, wait_for
from tests.odoo_product import ATTACHMENT, ATTACHMENT_BYTES, MODULES, TK
from tests.recorded_git_host import LATER_RUN, PORT, RUN, build_of

GIT_HOST_UNIT = "launchplane-recorded-git-host"
DROP_IN = "/etc/systemd/system/launchplane.service.d/recorded-git-host.conf"
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
LEAD, LEAD_FILE, LEAD_BYTES = "Invented lead of the copy watch", "invented-lead.txt", b"invented bytes of the lead's attachment\n"
WRITTEN = "Written "
# Odoo's own first login, which a lane seeded by hand keeps.
ADMIN_LOGIN, ADMIN_PASSWORD = "admin", "admin"
# Run in a container of the database image that can only read the two volumes of a copy and reaches no network: the
# database is copied once more, into the container itself, and a server started there answers. The copy is not written.
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
def pull_builds() -> None:
    for build in (build_of(RUN), build_of(LATER_RUN)):
        on_scratch(f"docker image inspect {build} >/dev/null 2>&1 || docker pull --quiet {build}")


def serve_recorded_git_host() -> None:
    on_control(
        f"systemctl stop {GIT_HOST_UNIT} 2>/dev/null; systemctl reset-failed {GIT_HOST_UNIT} 2>/dev/null; "
        f"systemd-run --quiet --unit={GIT_HOST_UNIT} --uid=launchplane --working-directory=/opt/launchplane "
        "--setenv=PYTHONPATH=/opt/launchplane /opt/launchplane/.venv/bin/python -m tests.recorded_git_host && "
        f"mkdir -p {DROP_IN.rsplit('/', 1)[0]} && "
        f"printf '[Service]\\nEnvironment=LAUNCHPLANE_GIT_HOST=http://127.0.0.1:{PORT}\\n' > {DROP_IN} && "
        "systemctl daemon-reload && systemctl restart launchplane"
    )
    wait_for(launchplane_answers, seconds=30)


def stop_recorded_git_host() -> None:
    on_control(
        f"rm -f {DROP_IN}; systemctl daemon-reload; systemctl stop {GIT_HOST_UNIT} 2>/dev/null; systemctl restart launchplane"
    )
    wait_for(launchplane_answers, seconds=30)


def describe(product: str) -> None:
    file = f"/var/lib/launchplane/products/{product}.json"
    on_control(
        f"install -d -o launchplane /var/lib/launchplane/products && cat > {file} && chmod 644 {file}",
        given=TK.model_dump_json(exclude_defaults=True),
    )


def data_set_of(product: str, lane: str) -> str:
    [found] = [each["data_set"] for each in ask("/running")[1]["data_sets"] if (each["product"], each["lane"]) == (product, lane)]
    return found


# Odoo as the kind runs it, once, on the lane's own database and files, from the build the lane will run.
def odoo_on(data_set: str, *arguments: str) -> str:
    settings = dict(TK.run.environment) | client_environment(SEED_LOGIN, SEED_PASSWORD)
    given = [argument for name, value in settings.items() for argument in ("--env", f"{name}={value}")]
    on = ["--network", f"launchplane-data-{data_set}", "--volume", f"launchplane-data-{data_set}-files:{FILES_PATH}", "--user", USER]
    return shlex.join(["docker", "run", "--rm", "--interactive", *on, *given, build_of(RUN), SERVER, *arguments])


def seed(product: str, lane: str, modules: list[str] = MODULES, *, attachment: bool = True) -> None:
    data_set = data_set_of(product, lane)
    psql = "psql --username=postgres --dbname=app --quiet --set=ON_ERROR_STOP=1"
    on_scratch(f"docker exec --interactive {product}-{lane}-database-{data_set} {psql}", given=MAKE_SEED_LOGIN)
    on_scratch(odoo_on(data_set, "--init", ",".join(modules), "--stop-after-init", "--no-http", "--log-level=warn"))
    if attachment:
        on_scratch(odoo_on(data_set, "shell", "--no-http", "--log-level=error"), given=MAKE_ATTACHMENT)


# A person signed in at a lane's web address, so what is read and written goes through Odoo as its own pages do.
class SignedIn:
    def __init__(self, product: str, lane: str) -> None:
        self.web_address = f"{lane}.{product}.test"
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        self.ask("/web/session/authenticate", db="app", login=ADMIN_LOGIN, password=ADMIN_PASSWORD)

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

    def make_lead(self) -> None:
        leads = self.call("crm.lead", "search", [("name", "=", LEAD)]) or self.call(
            "crm.lead", "create", [{"name": LEAD, "type": "lead"}]
        )
        if not self.call("ir.attachment", "search", [("name", "=", LEAD_FILE)]):
            file = {"name": LEAD_FILE, "res_model": "crm.lead", "res_id": leads[0], "raw": base64.b64encode(LEAD_BYTES).decode()}
            self.call("ir.attachment", "create", [file])

    def write(self, text: str) -> None:
        self.call("crm.lead", "create", [{"name": f"{WRITTEN}{text}", "type": "lead"}])

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
    if do in ("lead", "write", "show"):
        shown(read_lane(product, lane))
    return do in ("lead", "write", "show", "show-copy")


def main(do: str, product: str = "watch-tk", lane: str = "testing", text: str = "") -> None:
    if show(do, product, lane, text):
        return
    if do == "set-up":
        pull_builds()
        serve_recorded_git_host()
        describe(product)
    elif do == "seed":
        seed(product, lane)
    elif do == "tear-down":
        retire_with_its_data(product, lane)
        stop_recorded_git_host()
        on_control(f"rm -f /var/lib/launchplane/products/{product}.json")
    else:
        raise SystemExit("python -m tests.bed_odoo set-up|seed|tear-down|lead|show|write|show-copy [product] [lane] [text or copy]")
    print(json.dumps({"done": do, "product": product, "builds": {str(RUN): build_of(RUN), str(LATER_RUN): build_of(LATER_RUN)}}))  # noqa: T201


if __name__ == "__main__":
    try:
        main(*sys.argv[1:])
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"{error}\n{error.stderr}") from error
