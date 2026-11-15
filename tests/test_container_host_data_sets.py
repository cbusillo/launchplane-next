from pathlib import Path
from typing import cast

import docker
import pytest
from docker.errors import NotFound

from launchplane.container_host import (
    LANE_NETWORK,
    ContainerHost,
    ContainerHostError,
    DatabaseLink,
    database_options,
    instance_options,
    job_options,
    on_host_from_answers,
    quiet_network_settings,
)
from launchplane.data_set import postgres_server
from launchplane.instance import DataSet, DataSetDatabase, DataSetNetwork, InstanceState, LaneBuild
from launchplane.product import Database, Product, ProductError, ProductFolder

DATA_SET_LABELS = {"launchplane.product": "shop", "launchplane.lane": "testing", "launchplane.data-set": "d1"}
QUIET_OPTIONS = {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}
SERVER = postgres_server(Database(image="postgres:18-alpine"))
LINK = DatabaseLink("d1", "launchplane-data-d1", "shop-testing-database-d1", SERVER)
TARGET = LaneBuild(product="shop", lane="testing", build="shop:a")
PASSWORD = "a-password-made-at-random"  # noqa: S105 a value for the test, guarding nothing


def network(
    internal: bool, options: dict[str, str] | None, name: str = "launchplane-data-d1", labels: dict[str, str] | None = None
) -> dict[str, object]:
    return {"Name": name, "Internal": internal, "Options": options, "Labels": DATA_SET_LABELS if labels is None else labels}


def as_docker_client(fake: object) -> docker.DockerClient:
    return cast("docker.DockerClient", fake)


def container(name: str, part: str, networks: list[str]) -> dict[str, object]:
    return {
        "Names": [f"/{name}"],
        "State": "running",
        "Labels": DATA_SET_LABELS | {"launchplane.part": part},
        "NetworkSettings": {"Networks": {name: {} for name in networks}},
    }


def data_sets_read(containers: object, networks: object, volumes: object) -> list[DataSet]:
    return on_host_from_answers(containers, networks, {"Volumes": volumes, "Warnings": None}).data_sets


def test_a_data_set_is_read_from_its_network_volume_and_database_together() -> None:
    volume = {"Name": "launchplane-data-d1-database", "Labels": DATA_SET_LABELS}
    database = container("shop-testing-database-d1", "database", ["launchplane-data-d1"])
    assert data_sets_read([database], [network(True, QUIET_OPTIONS)], [volume]) == [
        DataSet(
            product="shop",
            lane="testing",
            data_set="d1",
            networks=(DataSetNetwork(name="launchplane-data-d1", is_quiet=True),),
            volumes=("launchplane-data-d1-database",),
            databases=(DataSetDatabase(container="shop-testing-database-d1", state=InstanceState.RUNNING),),
        )
    ]


def test_two_databases_carrying_one_data_sets_labels_are_both_read_not_folded_into_one() -> None:
    databases = [container(name, "database", []) for name in ("database-b", "database-a")]
    [found] = data_sets_read(databases, [], [])
    assert [database.container for database in found.databases] == ["database-a", "database-b"]


NOT_QUIET = {
    "a bridge that is not internal": network(False, QUIET_OPTIONS),
    "an internal network that still reaches the host": network(True, {}),
    "an internal network with no options at all": network(True, None),
}


@pytest.mark.parametrize("answer", NOT_QUIET.values(), ids=NOT_QUIET.keys())
def test_a_data_set_network_that_lets_an_instance_reach_out_reads_as_not_quiet(answer: dict[str, object]) -> None:
    assert data_sets_read([], [answer], [])[0].networks[0].is_quiet is False


def test_a_network_launchplane_makes_quiet_reads_back_as_quiet() -> None:
    settings = quiet_network_settings()
    made = {"Name": "launchplane-data-d1", "Internal": settings.internal, "Options": settings.options, "Labels": DATA_SET_LABELS}
    assert data_sets_read([], [made], [])[0].networks[0].is_quiet


def test_an_instance_attached_to_any_network_that_is_not_quiet_reads_as_not_quiet() -> None:
    lanes = network(True, QUIET_OPTIONS, LANE_NETWORK, {})
    quiet = container("shop-testing-1", "web", [LANE_NETWORK])
    reaching_out = container("shop-testing-2", "web", [LANE_NETWORK, "bridge"])
    networks = [lanes, network(False, None, "bridge", {})]
    on_host = on_host_from_answers([quiet, reaching_out], networks, {"Volumes": None})
    assert {instance.container: instance.is_quiet for instance in on_host.instances} == {
        "shop-testing-1": True,
        "shop-testing-2": False,
    }


def test_a_database_and_a_job_are_not_read_as_instances() -> None:
    parts = [container("db", "database", []), container("shop-testing-1-job", "job", [])]
    on_host = on_host_from_answers(parts, [], {"Volumes": None})
    assert (on_host.instances, [found.container for found in on_host.jobs]) == ([], ["shop-testing-1-job"])


def test_a_container_marked_with_a_part_launchplane_does_not_know_is_an_error_not_skipped() -> None:
    with pytest.raises(ContainerHostError, match="part cache"):
        on_host_from_answers([container("shop-x", "cache", [])], [], {"Volumes": None})


def test_the_database_answers_at_the_address_its_instances_are_told() -> None:
    options = database_options("shop", "testing", "d1", LINK.network, "launchplane-data-d1-database", SERVER, PASSWORD)
    told = instance_options(TARGET, "shop-testing-1", LINK, PASSWORD).environment["PGHOST"]
    assert options.networks == {LINK.network: [told]}


def test_the_database_keeps_its_data_on_the_data_sets_volume() -> None:
    options = database_options("shop", "testing", "d1", LINK.network, "launchplane-data-d1-database", SERVER, PASSWORD)
    assert options.volumes == {"launchplane-data-d1-database": SERVER.data_path}


def test_the_web_part_and_the_job_are_given_the_password_the_database_was_made_with() -> None:
    database = database_options("shop", "testing", "d1", LINK.network, "launchplane-data-d1-database", SERVER, PASSWORD)
    web = instance_options(TARGET, "shop-testing-1", LINK, PASSWORD)
    job = job_options(TARGET, "shop-testing-1", LINK, PASSWORD, ["job"])
    assert SERVER.password_in([f"{key}={value}" for key, value in database.environment.items()]) == PASSWORD
    assert web.environment["PGPASSWORD"] == job.environment["PGPASSWORD"] == PASSWORD


def test_the_web_part_joins_the_lanes_and_its_data_set_and_the_job_only_its_data_set() -> None:
    assert list(instance_options(TARGET, "shop-testing-1", LINK, PASSWORD).networks) == [LANE_NETWORK, LINK.network]
    assert list(job_options(TARGET, "shop-testing-1", LINK, PASSWORD, ["job"]).networks) == [LINK.network]


class Removable:
    def __init__(self, name: str, removed: list[str]) -> None:
        self.name = name
        self.removed = removed

    def stop(self, timeout: int) -> None:
        self.removed.append(f"stop {self.name} within {timeout}")

    def remove(self) -> None:
        self.removed.append(f"remove {self.name}")


class Collection:
    def __init__(self, removed: list[str]) -> None:
        self.removed = removed

    def get(self, name: str) -> Removable:
        return Removable(name, self.removed)


class RemovingClient:
    def __init__(self) -> None:
        self.removed: list[str] = []
        self.containers = self.volumes = self.networks = Collection(self.removed)


def test_deleting_a_data_set_removes_every_database_volume_and_network_read_of_it() -> None:
    client = RemovingClient()
    found = DataSet(
        product="shop",
        lane="testing",
        data_set="d1",
        networks=(DataSetNetwork(name="network-d1", is_quiet=True),),
        volumes=("volume-a", "volume-b"),
        databases=(DataSetDatabase(container="database-d1", state=InstanceState.RUNNING),),
    )
    ContainerHost(as_docker_client(client), "test").delete_data_set(found)
    assert client.removed == [
        "stop database-d1 within 30",
        "remove database-d1",
        "remove volume-a",
        "remove volume-b",
        "remove network-d1",
    ]


class RecordingNetworks:
    def __init__(self) -> None:
        self.made: list[dict[str, object]] = []
        self.names: list[str] = []

    def get(self, name: str) -> None:
        raise NotFound(f"network {name} not found")

    def create(
        self, name: str, driver: str, internal: bool = False, options: dict[str, str] | None = None, labels: object = None
    ) -> None:
        self.names.append(name)
        self.made.append({"Name": name, "Driver": driver, "Internal": internal, "Options": options, "Labels": labels or {}})


class NetworkMakingClient:
    def __init__(self) -> None:
        self.networks = RecordingNetworks()


@pytest.mark.parametrize("make", ["data set", "lanes"])
def test_every_network_an_instance_joins_is_made_quiet(make: str) -> None:
    client = NetworkMakingClient()
    host = ContainerHost(as_docker_client(client), "test")
    if make == "data set":
        host.create_data_set_network("shop", "testing", "d1")
    else:
        host.ensure_network(LANE_NETWORK, quiet=True)
    [made] = client.networks.made
    [name] = client.networks.names
    quiet_networks = on_host_from_answers([container("shop-testing-1", "web", [name])], [made], {"Volumes": None})
    assert quiet_networks.instances[0].is_quiet


def test_a_product_with_no_description_is_a_web_product_with_no_data(tmp_path: Path) -> None:
    assert ProductFolder(tmp_path).read("site") == Product()


def test_a_description_launchplane_cannot_read_is_an_error_naming_the_file(tmp_path: Path) -> None:
    (tmp_path / "shop.json").write_text('{"database": {"image": "postgres:18"}, "datbase": {}}')
    with pytest.raises(ProductError, match=r"shop\.json"):
        ProductFolder(tmp_path).read("shop")
