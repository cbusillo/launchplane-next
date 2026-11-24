import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from docker.errors import ImageNotFound

from launchplane.container_host import ContainerHost
from launchplane.data_set import RULES_AS_READ
from launchplane.docker_answers import CommandResult
from launchplane.instance import DataSet, DataSetUse, LaneBuild
from launchplane.odoo import FILES_PATH
from launchplane.proxy import CheckFailed
from tests.odoo_product import TK
from tests.recording_docker import ExecResult, RecordingContainer, RecordingDocker, as_docker_client
from tests.test_container_host import (
    DATA_SET_LABELS,
    DATA_SET_NETWORK,
    DATABASE,
    QUIET_OPTIONS,
    TARGET,
    WEB,
    database_made,
    network,
    read,
)
from tests.test_container_host import container as listed
from tests.test_proxy import ProxyHost, ticking

DATABASE_VOLUME, FILES_VOLUME = "launchplane-data-d1-database", "launchplane-data-d1-files"
FILES_LABELS = DATA_SET_LABELS | {"launchplane.part": "files"}
ON_D1 = DataSetUse("d1", DATA_SET_NETWORK, DATABASE, FILES_VOLUME)
DIGEST = "sha256:" + "a" * 64
BUILD = f"ghcr.io/cbusillo/odoo-tenant-tk@{DIGEST}"
OTHER_IMAGE = "sha256:" + "b" * 64
CHECK = TK.check(WEB).command


def test_a_files_volume_is_read_as_the_data_sets_files_and_never_as_a_second_database_volume() -> None:
    volumes = [{"Name": DATABASE_VOLUME, "Labels": DATA_SET_LABELS}, {"Name": FILES_VOLUME, "Labels": FILES_LABELS}]
    [found] = read([listed(DATABASE, "database", [DATA_SET_NETWORK])], [network(True, QUIET_OPTIONS)], volumes).data_sets
    assert (found.volumes, found.files) == ((DATABASE_VOLUME,), (FILES_VOLUME,))


@dataclass
class Volumes:
    made: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])

    def create(self, name: str, labels: dict[str, str]) -> None:
        self.made[name] = labels

    def get(self, name: str) -> object:
        return SimpleNamespace(remove=lambda: self.made.pop(name))


class DockerWithVolumes(RecordingDocker):
    def __init__(self) -> None:
        super().__init__()
        self.volumes = Volumes()


def test_the_files_volume_is_marked_as_files_and_the_database_volume_is_made_as_it_always_was() -> None:
    docker = DockerWithVolumes()
    host = ContainerHost(docker.as_client(), "test")
    host.create_data_set_volume("shop", "testing", "d1")
    host.create_data_set_volume("shop", "testing", "d1", files=True)
    assert docker.volumes.made == {DATABASE_VOLUME: DATA_SET_LABELS, FILES_VOLUME: FILES_LABELS}


def test_deleting_a_data_set_deletes_its_files_with_its_database() -> None:
    docker = DockerWithVolumes()
    host = ContainerHost(docker.as_client(), "test")
    host.create_data_set_volume("shop", "testing", "d1")
    host.create_data_set_volume("shop", "testing", "d1", files=True)
    found = DataSet(product="shop", lane="testing", data_set="d1", networks=(), volumes=(DATABASE_VOLUME,), databases=())
    host.delete_data_set(found.model_copy(update={"files": (FILES_VOLUME,)}))
    assert docker.volumes.made == {}


# What Docker was asked to make the one web part from.
def made_from(target: LaneBuild, use: DataSetUse | None) -> tuple[list[str] | None, dict[str, object]]:
    docker = RecordingDocker()
    host = database_made(docker)
    docker.containers.by_name[DATABASE].answers = [
        ExecResult(0, json.dumps(RULES_AS_READ).encode()),
        ExecResult(0, b"[]"),
        ExecResult(0),
    ]
    create, asked = docker.containers.create, list[tuple[list[str] | None, dict[str, object]]]()

    def create_and_keep(_image: str, _command: list[str] | None = None, **options: object) -> RecordingContainer:
        asked.append((_command, options))
        return create(_image, _command, **options)

    docker.containers.create = create_and_keep
    host.start_instance(target, WEB, use)
    return asked[0]


def test_an_odoo_instance_is_started_by_the_kinds_command_as_its_user_on_its_files_with_its_settings_and_its_login() -> None:
    command, options = made_from(TARGET.model_copy(update={"run": TK.run}), ON_D1)
    given = cast("dict[str, str]", options["environment"])
    assert (command, options["user"]) == (["/odoo/odoo-bin"], "ubuntu")
    assert options["volumes"] == {FILES_VOLUME: {"bind": FILES_PATH, "mode": "rw"}}
    assert (given["ODOO_MAX_CRON_THREADS"], given["PGUSER"], bool(given["PGPASSWORD"])) == ("0", "i_aaaaaaaaaaaa", True)


def test_a_build_that_starts_by_itself_is_given_no_command_user_or_volume_also_on_a_data_set_with_files() -> None:
    command, options = made_from(TARGET, ON_D1)
    assert (command, options["user"], options["volumes"]) == (None, None, {})
    assert set(cast("dict[str, str]", options["environment"])) == {"PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"}


@dataclass
class Images:
    by_reference: dict[str, str]
    asked: list[str] = field(default_factory=list[str])

    def get(self, reference: str) -> object:
        self.asked.append(reference)
        if reference not in self.by_reference:
            raise ImageNotFound(f"No such image: {reference}")
        return SimpleNamespace(id=self.by_reference[reference])


# The image the one instance is read to run that is not its build's, where the host has `images` and it runs `image`.
def other_image_read(build: str, image: str, images: dict[str, str]) -> tuple[str | None, list[str]]:
    docker = RecordingDocker()
    ContainerHost(docker.as_client(), "test").start_instance(TARGET.model_copy(update={"build": build}), WEB, None)
    on_host = Images(images)

    def listed_running_the_image(**options: object) -> list[dict[str, Any]]:
        return [each | {"ImageID": image} for each in docker.api.containers(**options)]

    client = SimpleNamespace(
        api=SimpleNamespace(
            containers=listed_running_the_image,
            networks=docker.api.networks,
            volumes=docker.api.volumes,
            inspect_container=docker.api.inspect_container,
        ),
        images=on_host,
    )
    [instance] = ContainerHost(as_docker_client(client), "test").read().instances
    return instance.other_image, on_host.asked


def test_an_instance_is_read_as_its_build_only_while_it_runs_the_image_the_host_has_for_that_digest() -> None:
    assert other_image_read(BUILD, DIGEST, {BUILD: DIGEST}) == (None, [BUILD])
    assert other_image_read(BUILD, OTHER_IMAGE, {BUILD: DIGEST}) == (OTHER_IMAGE, [BUILD])


def test_an_instance_whose_builds_digest_the_host_no_longer_has_is_not_read_as_that_build() -> None:
    assert other_image_read(BUILD, DIGEST, {})[0] == DIGEST


def test_a_build_named_by_a_tag_is_not_held_to_an_image_since_a_tag_moves() -> None:
    assert other_image_read("shop:a", OTHER_IMAGE, {"shop:a": DIGEST}) == (None, [])


class InstanceAndProxyHost(ProxyHost):
    def __init__(self, answers: list[bool], check: CommandResult) -> None:
        super().__init__({}, answers=answers)
        self.check = check
        self.run_in_the_instance: list[list[str]] = []

    def run_in(self, container: str, command: list[str]) -> CommandResult:
        if container != "site-t2":
            return super().run_in(container, command)
        self.run_in_the_instance.append(command)
        return self.check


def test_the_check_of_its_kind_is_run_in_the_instance_once_it_has_answered_steadily() -> None:
    host = InstanceAndProxyHost([False, True, True, True], CommandResult(0, ""))
    ticking(host, steady_seconds=2, check_deadline_seconds=100).check_instance("site-t2", "/web/login", CHECK)
    assert host.run_in_the_instance == [list(CHECK)]


def test_an_instance_that_answers_and_fails_the_check_of_its_kind_fails_with_what_the_check_said() -> None:
    host = InstanceAndProxyHost([True] * 5, CommandResult(1, "The database app does not have these modules installed: crm."))
    with pytest.raises(
        CheckFailed, match="site-t2 answers the proxy, and the check of its kind, run in it, failed: The database app"
    ):
        ticking(host, steady_seconds=2, check_deadline_seconds=100).check_instance("site-t2", "/web/login", CHECK)


def test_the_check_of_its_kind_is_not_run_in_an_instance_that_never_answers() -> None:
    host = InstanceAndProxyHost([False] * 10, CommandResult(0, ""))
    with pytest.raises(CheckFailed, match="Connection refused"):
        ticking(host, steady_seconds=2, check_deadline_seconds=3).check_instance("site-t2", "/web/login", CHECK)
    assert host.run_in_the_instance == []
