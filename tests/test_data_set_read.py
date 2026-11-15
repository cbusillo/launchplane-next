from pathlib import Path

import pytest

from launchplane.container_host import ContainerHostError, on_host_from_answers
from launchplane.instance import DataSet, InstanceState
from launchplane.product import Product, ProductError, ProductFolder

DATA_SET_LABELS = {"launchplane.product": "shop", "launchplane.lane": "testing", "launchplane.data-set": "d1"}
QUIET_OPTIONS = {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}


def network(internal: bool, options: dict[str, str] | None) -> dict[str, object]:
    return {"Name": "launchplane-data-d1", "Internal": internal, "Options": options, "Labels": DATA_SET_LABELS}


def database(state: str) -> dict[str, object]:
    return {"Names": ["/shop-testing-database-d1"], "State": state, "Labels": DATA_SET_LABELS | {"launchplane.part": "database"}}


def data_sets_read(containers: list[object], networks: list[object], volumes: list[object]) -> list[DataSet]:
    return on_host_from_answers(containers, networks, {"Volumes": volumes, "Warnings": None}).data_sets


def test_a_data_set_is_read_from_its_network_volume_and_database_together() -> None:
    volume = {"Name": "launchplane-data-d1-database", "Labels": DATA_SET_LABELS}
    assert data_sets_read([database("running")], [network(True, QUIET_OPTIONS)], [volume]) == [
        DataSet(
            product="shop",
            lane="testing",
            data_set="d1",
            network_is_quiet=True,
            has_volume=True,
            database_container="shop-testing-database-d1",
            database_state=InstanceState.RUNNING,
        )
    ]


NOT_QUIET = {
    "a bridge that is not internal": network(False, QUIET_OPTIONS),
    "an internal network that still reaches the host": network(True, {}),
    "an internal network with no options at all": network(True, None),
}


@pytest.mark.parametrize("answer", NOT_QUIET.values(), ids=NOT_QUIET.keys())
def test_a_data_set_network_that_lets_an_instance_reach_out_reads_as_not_quiet(answer: dict[str, object]) -> None:
    assert data_sets_read([], [answer], [])[0].network_is_quiet is False


def test_a_data_set_with_only_a_database_left_reads_with_its_network_and_volume_missing() -> None:
    [found] = data_sets_read([database("exited")], [], [])
    assert (found.network_is_quiet, found.has_volume, found.database_state) == (None, False, InstanceState.STOPPED)


def test_a_database_and_a_job_are_not_read_as_instances() -> None:
    job = {"Names": ["/shop-testing-1-job"], "State": "running", "Labels": DATA_SET_LABELS | {"launchplane.part": "job"}}
    on_host = on_host_from_answers([database("running"), job], [], {"Volumes": None})
    assert (on_host.instances, [found.container for found in on_host.jobs]) == ([], ["shop-testing-1-job"])


def test_a_container_marked_with_a_part_launchplane_does_not_know_is_an_error_not_skipped() -> None:
    unknown = {"Names": ["/shop-x"], "State": "running", "Labels": DATA_SET_LABELS | {"launchplane.part": "cache"}}
    with pytest.raises(ContainerHostError, match="part cache"):
        on_host_from_answers([unknown], [], {"Volumes": None})


def test_a_product_with_no_description_is_a_web_product_with_no_data(tmp_path: Path) -> None:
    assert ProductFolder(tmp_path).read("site") == Product()


def test_a_description_launchplane_cannot_read_is_an_error_naming_the_file(tmp_path: Path) -> None:
    (tmp_path / "shop.json").write_text('{"database": {"image": "postgres:18"}, "datbase": {}}')
    with pytest.raises(ProductError, match=r"shop\.json"):
        ProductFolder(tmp_path).read("shop")
