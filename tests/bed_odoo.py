"""What the test bed needs before TK's real Odoo build runs as a lane, and the one-time seed of a lane's data.

For tests/test_bed_odoo.py and for the watch in docs/the-move.md, which runs it as `python -m tests.bed_odoo`.
Nothing here is Launchplane: the seed is a person's work on a new lane, done once, with invented data only.
"""

import json
import shlex
import subprocess
import sys

from launchplane.data_set import client_environment
from launchplane.odoo import FILES_PATH, SERVER, USER
from tests.bed import ask, launchplane_answers, on_control, on_scratch, wait_for
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


def main(do: str, product: str = "watch-tk", lane: str = "testing") -> None:
    if do == "set-up":
        pull_builds()
        serve_recorded_git_host()
        describe(product)
    elif do == "seed":
        seed(product, lane)
    elif do == "tear-down":
        stop_recorded_git_host()
        on_control(f"rm -f /var/lib/launchplane/products/{product}.json")
    else:
        raise SystemExit("python -m tests.bed_odoo set-up|seed|tear-down [product] [lane]")
    print(json.dumps({"done": do, "product": product, "builds": {str(RUN): build_of(RUN), str(LATER_RUN): build_of(LATER_RUN)}}))  # noqa: T201


if __name__ == "__main__":
    try:
        main(*sys.argv[1:])
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"{error}\n{error.stderr}") from error
