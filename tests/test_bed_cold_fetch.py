import io
import json
import re
import shlex
import subprocess
import zipfile
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from urllib.parse import urlsplit

import pytest

from launchplane.product import Product
from launchplane.workflow import Publisher
from tests import bed
from tests.bed import ask, ask_and_wait, launchplane_answers, needs_test_bed, on_control, on_scratch, wait_for
from tests.odoo_product import TK

pytestmark = needs_test_bed

DAEMON = "lp-cold-fetch-daemon"
DOCKER = f"docker exec {DAEMON} docker --host unix:///var/run/docker.sock"
UNIT = "launchplane-cold-watch"
FOLDER = f"/run/{UNIT}"
DATABASE = "launchplane_cold_watch"
PORT = 8876
SERVICE = "/opt/launchplane/.venv/bin"
WORKFLOW = ".github/workflows/build.yml"


@dataclass(frozen=True)
class Publication:
    product: str
    repository: str
    run: int
    commit: str

    @property
    def image(self) -> str:
        return f"ghcr.io/{self.repository}"

    def published_image(self) -> str:
        actions = f"repos/{self.repository}/actions"

        def github(path: str) -> bytes:
            return subprocess.check_output(["gh", "api", f"{actions}/{path}"])  # noqa: S603,S607

        run = json.loads(github(f"runs/{self.run}"))
        assert (run["repository"]["full_name"], run["head_sha"]) == (self.repository, self.commit)
        artifacts = json.loads(github(f"runs/{self.run}/artifacts"))["artifacts"]
        [artifact] = [row for row in artifacts if row["name"] == f"artifact-manifest-{run['run_attempt']}" and not row["expired"]]
        with zipfile.ZipFile(io.BytesIO(github(f"artifacts/{artifact['id']}/zip"))) as archive:
            manifest = json.loads(archive.read("artifact-manifest.json"))
        assert (manifest["source_commit"], manifest["image"]["repository"]) == (self.commit, self.image)
        digest = manifest["image"]["digest"]
        assert re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
        return f"{self.image}@{digest}"

    def describe(self, description: Product) -> None:
        on_control(f"cat > {FOLDER}/{self.product}.json", given=description.model_dump_json(exclude_defaults=True))

    @property
    def move(self) -> str:
        return f"/lanes/{self.product}/testing/move"


SITE = Publication("cold-site", "cbusillo/sellyouroutboard", 44324709893, "676d047f66176709c72b5105c4d821123be7a114")
ODOO = Publication("cold-odoo", "cbusillo/odoo-tenant-tk", 46069914315, "785a68a9e55186a2c411c8151aebd5febfef79e4")


@pytest.fixture
def empty_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with ExitStack() as cleanup:
        on_scratch(
            f"docker run -d --name {DAEMON} --privileged --env DOCKER_TLS_CERTDIR= -p 2377:2376 "
            "-v /etc/docker/tls:/bed-tls:ro docker:29-dind dockerd -H unix:///var/run/docker.sock -H tcp://0.0.0.0:2376 "
            "--tlsverify --tlscacert=/bed-tls/ca.pem --tlscert=/bed-tls/cert.pem --tlskey=/bed-tls/key.pem "
            "--bip=10.252.0.1/24 --default-address-pool=base=10.251.0.0/16,size=24"
        )
        cleanup.callback(on_scratch, f"docker rm -fv {DAEMON}")
        on_scratch(f"for n in $(seq 60); do {DOCKER} info >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1")
        assert not on_scratch(f"{DOCKER} image ls --all --quiet").strip()
        on_control(f"mkdir -m 755 {FOLDER}")
        cleanup.callback(on_control, f"rm -r {FOLDER}")
        on_control(f"runuser -u postgres -- createdb -T template0 -E UTF8 --locale=C.UTF-8 -O launchplane {DATABASE}")
        cleanup.callback(on_control, f"runuser -u postgres -- dropdb {DATABASE}")
        on_control(
            f"cat > {FOLDER}/host.env",
            given=f"LAUNCHPLANE_CONTAINER_HOST=tcp://{bed.SCRATCH_ADDRESS}:2377\n"
            f"LAUNCHPLANE_DATABASE=postgresql:///{DATABASE}\nLAUNCHPLANE_PRODUCTS={FOLDER}\n"
            "LAUNCHPLANE_CONTAINER_HOST_CREDENTIALS=/var/lib/launchplane/tls\n"
            f"LAUNCHPLANE_LISTEN_PORT={PORT}\nLAUNCHPLANE_LISTEN_ADDRESS=0.0.0.0\nLAUNCHPLANE_GIT_HOST=https://api.github.com\n",
        )
        on_control(
            f"touch {FOLDER}/deny.env && cd /opt/launchplane && runuser -u launchplane -- "
            f"env LAUNCHPLANE_DATABASE=postgresql:///{DATABASE} {SERVICE}/alembic upgrade head"
        )
        on_control(
            f"systemd-run --unit={UNIT} --collect --uid=launchplane -p WorkingDirectory=/var/lib/launchplane "
            f"-p EnvironmentFile=/etc/launchplane/bed.env -p EnvironmentFile=-/etc/launchplane/publisher.env "
            f"-p EnvironmentFile={FOLDER}/host.env "
            f"-p EnvironmentFile={FOLDER}/deny.env {SERVICE}/launchplane"
        )
        cleanup.callback(on_control, f"systemctl stop {UNIT}")
        url = urlsplit(bed.LAUNCHPLANE_URL)
        monkeypatch.setattr(bed, "LAUNCHPLANE_URL", url._replace(netloc=f"{url.hostname}:{PORT}").geturl())
        wait_for(launchplane_answers, 30)
        yield


def check_delivery(publication: Publication, image: str) -> None:
    status, kept = ask(publication.move, {"run": publication.run})
    assert status == 202 and kept["build"] == image, kept
    wait_for(lambda: ask(f"/requests/{kept['id']}")[1]["stands"] in ("failed", "done"), 600)
    received = json.loads(on_scratch(f"{DOCKER} image inspect --format '{{{{json .RepoDigests}}}}' {shlex.quote(image)}"))
    assert image in received, received
    entries = on_control(f"journalctl -o cat --no-pager _SYSTEMD_INVOCATION_ID=$(systemctl show -p InvocationID --value {UNIT})")
    times = re.findall(rf"Fetched {re.escape(image)} in ([\d.]+) s", entries)
    assert times, f"The host has {image} but Launchplane did not report its pull time."
    host_image = next(ref for ref in received if ref == image)
    print(f"After {publication.repository} {publication.commit} publisher {image} host {host_image} {times[-1]} s", flush=True)  # noqa: T201


@pytest.mark.usefixtures("empty_cache")
def test_cold_published_build() -> None:
    builds = {each: each.published_image() for each in (SITE, ODOO)}
    for each, image in builds.items():
        assert not on_scratch(f"{DOCKER} image ls --quiet --filter reference={shlex.quote(image)}").strip()
        print(f"Before {each.repository} {each.commit} absent {image}", flush=True)  # noqa: T201

    assert ask_and_wait(SITE.move, {"build": "nginx:1.30.6-alpine"}, 240)[0] == 200
    SITE.describe(Product(publisher=Publisher(repository=SITE.repository, workflow=WORKFLOW, image=SITE.image)))
    ODOO.describe(TK)
    on_control(f"echo LAUNCHPLANE_REGISTRY_PASSWORD=invalid > {FOLDER}/deny.env && systemctl restart {UNIT}")
    wait_for(launchplane_answers, 30)
    status, denied = ask_and_wait(SITE.move, {"run": SITE.run}, 240)
    error = denied.get("error") or ""
    assert status != 200 and f"Fetching image {builds[SITE]}" in error, denied
    assert not on_scratch(f"{DOCKER} ps -aq --filter label=launchplane.build={shlex.quote(builds[SITE])}").strip()
    previous = on_scratch(f"{DOCKER} exec launchplane-proxy wget -qO- --header 'Host: testing.cold-site.test' http://127.0.0.1")
    assert "Welcome to nginx!" in previous
    on_control(f": > {FOLDER}/deny.env && systemctl restart {UNIT}")
    wait_for(launchplane_answers, 30)
    for each, image in builds.items():
        check_delivery(each, image)
    print(f"Refused pull {error}. Previous site still served its nginx page.", flush=True)  # noqa: T201
