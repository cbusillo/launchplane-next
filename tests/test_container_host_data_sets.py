import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
import requests
from docker.errors import APIError, NotFound

from launchplane.container_host import (
    LANE_NETWORK,
    SECRET_VARIABLES_LABEL,
    ContainerHost,
    ContainerHostError,
    DatabaseLink,
    HostAnswers,
    OnHost,
    PartStopped,
    on_host_from_answers,
    secrets_held,
)
from launchplane.data_set import DatabaseServer, postgres_server
from launchplane.instance import DataSet, DataSetDatabase, HostNetwork, InstanceState, LaneBuild, Mount
from launchplane.product import Database, Product, ProductError, ProductFolder
from launchplane.proxy import PROXY
from tests.recording_docker import RecordingDocker, as_docker_client

DATA_SET_LABELS = {"launchplane.product": "shop", "launchplane.lane": "testing", "launchplane.data-set": "d1"}
QUIET_OPTIONS = {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}
SERVER = postgres_server(Database(image="postgres:18-alpine"))
TARGET = LaneBuild(product="shop", lane="testing", build="shop:a")
DATA_SET_NETWORK = "launchplane-data-d1"
DATABASE = "shop-testing-database-d1"
LINK = DatabaseLink("d1", DATA_SET_NETWORK, DATABASE, SERVER)
STARTED = "2026-11-15T20:00:00.000000001Z"
MADE = 1794772790
READ_AT = {"SystemTime": "2026-11-15T20:00:00Z"}


def network(
    internal: bool, options: dict[str, str] | None, name: str = DATA_SET_NETWORK, labels: dict[str, str] | None = None
) -> dict[str, object]:
    return {"Name": name, "Internal": internal, "Options": options, "Labels": DATA_SET_LABELS if labels is None else labels}


def container(name: str, part: str, networks: list[str], mounts: Sequence[object] = ()) -> dict[str, object]:
    return {
        "Names": [f"/{name}"],
        "State": "running",
        "Labels": DATA_SET_LABELS | {"launchplane.part": part},
        "NetworkSettings": {"Networks": {each: {} for each in networks}},
        "Mounts": list(mounts),
        "Created": MADE,
    }


def job_details(name: str, running: bool = True, restarting: bool = False) -> dict[str, object]:
    state = {"Running": running, "Restarting": restarting, "Paused": False, "StartedAt": STARTED}
    return {"Name": f"/{name}", "Config": {"Env": []}, "State": state}


def read(
    containers: Sequence[object],
    networks: Sequence[object] = (),
    volumes: Sequence[object] = (),
    described: Sequence[object] = (),
    system: object = READ_AT,
) -> OnHost:
    return on_host_from_answers(HostAnswers(list(containers), list(networks), {"Volumes": list(volumes)}, list(described), system))


def data_sets_read(containers: Sequence[object], networks: Sequence[object], volumes: Sequence[object]) -> list[DataSet]:
    return read(containers, networks, volumes).data_sets


def test_a_data_set_is_read_from_its_network_volume_and_database_together() -> None:
    volume = {"Name": "launchplane-data-d1-database", "Labels": DATA_SET_LABELS}
    assert data_sets_read([container(DATABASE, "database", [DATA_SET_NETWORK])], [network(True, QUIET_OPTIONS)], [volume]) == [
        DataSet(
            product="shop",
            lane="testing",
            data_set="d1",
            networks=(HostNetwork(name=DATA_SET_NETWORK, is_quiet=True),),
            volumes=("launchplane-data-d1-database",),
            databases=(
                DataSetDatabase(container=DATABASE, state=InstanceState.RUNNING, networks=(DATA_SET_NETWORK,), age_seconds=10),
            ),
        )
    ]


def test_a_container_is_read_with_what_it_really_has_mounted_whatever_its_labels_say() -> None:
    volume = {"Type": "volume", "Name": "another-volume", "Source": "/var/lib/docker/volumes/another-volume/_data"}
    bind = {"Type": "bind", "Source": "/etc", "Destination": "/host-etc"}
    mounted = [volume | {"Destination": "/var/lib/postgresql"}, bind]
    on_host = read([container(DATABASE, "database", [], mounted), container("shop-testing-1", "web", [], [bind])])
    assert on_host.data_sets[0].databases[0].mounts == (
        Mount(source="/etc", path="/host-etc"),
        Mount(source="another-volume", path="/var/lib/postgresql"),
    )
    assert on_host.instances[0].mounts == (Mount(source="/etc", path="/host-etc"),)


def test_two_databases_carrying_one_data_sets_labels_are_both_read_not_folded_into_one() -> None:
    [found] = data_sets_read([container(name, "database", []) for name in ("database-b", "database-a")], [], [])
    assert [database.container for database in found.databases] == ["database-a", "database-b"]


NOT_QUIET = {
    "a bridge that is not internal": network(False, QUIET_OPTIONS),
    "an internal network that still reaches the host": network(True, {}),
    "an internal network with no options at all": network(True, None),
}


@pytest.mark.parametrize("answer", NOT_QUIET.values(), ids=NOT_QUIET.keys())
def test_a_data_set_network_that_lets_an_instance_reach_out_reads_as_not_quiet(answer: dict[str, object]) -> None:
    assert data_sets_read([], [answer], [])[0].networks[0].is_quiet is False


def test_an_instance_is_read_with_every_network_it_is_attached_to_not_a_summary_of_them() -> None:
    on_host = read([container("shop-testing-1", "web", [LANE_NETWORK, "launchplane-data-d2", "bridge"])])
    assert on_host.instances[0].networks == ("bridge", "launchplane-data-d2", LANE_NETWORK)


def test_a_job_is_read_with_how_long_it_has_run_by_dockers_own_clock() -> None:
    described: list[object] = [job_details("a-job"), job_details("b-job", running=False)]
    containers: list[object] = [container("a-job", "job", []), container("b-job", "job", [])]
    jobs = read(containers, described=described, system={"SystemTime": "2026-11-15T20:00:02.5Z"}).jobs
    assert {job.container: job.running_for_seconds for job in jobs} == {"a-job": pytest.approx(2.5), "b-job": None}


def test_a_job_docker_calls_running_while_it_waits_to_restart_has_not_run_at_all() -> None:
    described: list[object] = [job_details("a-job", restarting=True)]
    jobs = read([container("a-job", "job", [])], described=described, system={"SystemTime": "2026-11-15T20:00:09Z"}).jobs
    assert jobs[0].running_for_seconds is None


def test_a_job_docker_lists_but_does_not_describe_is_an_error_not_a_job_of_unknown_age() -> None:
    with pytest.raises(ContainerHostError, match="did not describe it"):
        read([container("a-job", "job", [])], described=[], system={"SystemTime": "2026-11-15T20:00:02Z"})


def test_a_database_and_a_job_are_not_read_as_instances() -> None:
    containers: list[object] = [container("db", "database", []), container("a-job", "job", [])]
    on_host = read(containers, described=[job_details("a-job")], system={"SystemTime": "2026-11-15T20:00:02Z"})
    assert (on_host.instances, [found.container for found in on_host.jobs]) == ([], ["a-job"])


def test_a_container_marked_with_a_part_launchplane_does_not_know_is_an_error_not_skipped() -> None:
    with pytest.raises(ContainerHostError, match="part cache"):
        read([container("shop-x", "cache", [])])


@pytest.mark.parametrize("make", ["data set", "lanes"])
def test_every_network_an_instance_joins_is_made_quiet(make: str) -> None:
    docker = RecordingDocker()
    host = ContainerHost(docker.as_client(), "test")
    if make == "data set":
        host.create_data_set_network("shop", "testing", "d1")
    else:
        host.ensure_network(LANE_NETWORK, quiet=True)
    [made] = docker.networks.made
    assert read([], [made]).networks[0].is_quiet


PASSWORD_PATH = "/var/lib/postgresql/launchplane-password"  # noqa: S105 where a password is kept, not one
DATA_MADE_PATH = "/var/lib/postgresql/18/docker/PG_VERSION"


def database_made(docker: RecordingDocker, data_set: str = "d1") -> ContainerHost:
    host = ContainerHost(docker.as_client(), "test")
    host.create_database(
        "shop", "testing", data_set, f"launchplane-data-{data_set}", f"launchplane-data-{data_set}-database", SERVER
    )
    return host


def password_kept(docker: RecordingDocker, data_set: str = "d1") -> str:
    return docker.containers.files_by_volume[f"launchplane-data-{data_set}-database"][PASSWORD_PATH].decode()


def test_the_database_keeps_its_data_on_its_volume_where_the_postgres_18_image_keeps_it() -> None:
    docker = RecordingDocker()
    database_made(docker)
    assert docker.last_run()["volumes"] == {"launchplane-data-d1-database": {"bind": "/var/lib/postgresql", "mode": "rw"}}


def test_the_database_answers_on_its_data_set_network_at_the_address_its_instances_are_told() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    aliases = {name: endpoint.get("Aliases") for name, endpoint in docker.last_run()["networking_config"].items()}
    host.start_instance(TARGET, "shop-testing-1", LINK)
    assert aliases == {DATA_SET_NETWORK: [docker.last_run()["environment"]["PGHOST"]]}


def test_each_data_set_is_made_with_a_password_of_its_own() -> None:
    docker = RecordingDocker()
    database_made(docker, "d1")
    database_made(docker, "d2")
    passwords = [password_kept(docker, "d1"), password_kept(docker, "d2")]
    assert len(set(passwords)) == 2
    assert all(len(password) >= 32 for password in passwords)


def test_the_password_is_written_to_the_data_sets_volume_for_its_owner_only_before_the_database_first_starts() -> None:
    docker = RecordingDocker()
    database_made(docker)
    database = docker.containers.by_name[DATABASE]
    assert database.calls == [f"read {PASSWORD_PATH}", f"read {DATA_MADE_PATH}", f"write {PASSWORD_PATH}", "start"]
    assert database.file_modes == {PASSWORD_PATH: 0o600}


def test_the_database_server_is_told_where_the_password_is_kept_and_holds_no_password_itself() -> None:
    docker = RecordingDocker()
    database_made(docker)
    assert docker.last_run()["environment"] == {"POSTGRES_PASSWORD_FILE": PASSWORD_PATH}


def test_a_database_server_removed_by_hand_is_made_again_with_the_password_its_kept_data_accepts() -> None:
    docker = RecordingDocker()
    database_made(docker)
    first = password_kept(docker)
    docker.containers.by_name[DATABASE].removed = True
    database_made(docker)
    assert password_kept(docker) == first
    assert f"write {PASSWORD_PATH}" not in docker.containers.by_name[DATABASE].calls


def test_a_database_made_and_never_given_its_password_gets_one_when_it_is_started() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    del docker.containers.files_by_volume["launchplane-data-d1-database"][PASSWORD_PATH]
    host.start_database(DATABASE, SERVER)
    assert len(password_kept(docker)) >= 32


def test_data_that_exists_without_its_password_is_an_error_naming_the_data_set_not_given_a_new_password() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    files = docker.containers.files_by_volume["launchplane-data-d1-database"]
    files[DATA_MADE_PATH] = b"18\n"
    del files[PASSWORD_PATH]
    docker.containers.by_name[DATABASE].calls.clear()
    with pytest.raises(ContainerHostError, match="The data set d1 has data and no password"):
        host.start_database(DATABASE, SERVER)
    assert PASSWORD_PATH not in files
    assert "start" not in docker.containers.by_name[DATABASE].calls


def test_a_database_server_made_again_on_data_without_its_password_is_not_started_either() -> None:
    docker = RecordingDocker()
    database_made(docker)
    files = docker.containers.files_by_volume["launchplane-data-d1-database"]
    files[DATA_MADE_PATH] = b"18\n"
    del files[PASSWORD_PATH]
    docker.containers.by_name[DATABASE].removed = True
    with pytest.raises(ContainerHostError, match="has data and no password"):
        database_made(docker)
    assert "start" not in docker.containers.by_name[DATABASE].calls


def test_a_database_that_does_not_say_where_its_password_is_kept_is_refused_not_started_without_one() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    database = docker.containers.by_name[DATABASE]
    del database.labels["launchplane.password-file"]
    database.calls.clear()
    with pytest.raises(ContainerHostError, match="does not say where its data keeps its password"):
        host.start_database(DATABASE, SERVER)
    assert database.calls == []


def test_a_database_that_does_not_say_where_its_data_is_is_refused_because_it_cannot_be_told_new_from_made() -> None:
    docker = RecordingDocker()
    docker.containers.environment_of_every_image = ()
    with pytest.raises(ContainerHostError, match="cannot tell whether its data set is new"):
        database_made(docker)


def test_an_instance_and_its_job_are_given_everything_a_postgresql_client_needs_and_say_which_data_set_they_run_against() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    reaching_the_database = {
        "PGHOST": "database",
        "PGPORT": "5432",
        "PGUSER": "postgres",
        "PGDATABASE": "postgres",
        "PGPASSWORD": password_kept(docker),
    }
    host.start_instance(TARGET, "shop-testing-1", LINK)
    host.start_job(TARGET, "shop-testing-1", LINK, ["job"])
    for started in docker.containers.runs[1:]:
        assert started["environment"] == reaching_the_database
        assert started["labels"]["launchplane.data-set"] == "d1"


def test_a_job_is_read_with_the_data_set_it_says_it_runs_against() -> None:
    jobs = read([container("a-job", "job", [])], described=[job_details("a-job")]).jobs
    assert [job.data_set for job in jobs] == ["d1"]


def test_deleting_a_data_set_whose_parts_have_already_gone_finishes_as_a_delete_cut_short_and_asked_again_must() -> None:
    gone = DataSet(
        product="shop",
        lane="testing",
        data_set="d1",
        networks=(HostNetwork(name="network-d1", is_quiet=True),),
        volumes=("volume-d1",),
        databases=(DataSetDatabase(container="database-d1", state=InstanceState.RUNNING, networks=("network-d1",)),),
    )
    ContainerHost(RecordingDocker().as_client(), "test").delete_data_set(gone)


def test_a_database_that_never_accepts_connections_is_an_error_after_its_deadline_not_awaited_without_end() -> None:
    docker = RecordingDocker()
    database_made(docker)
    docker.containers.by_name[DATABASE].ready = False
    seconds, waits_before_the_test_gives_up = iter(range(1000)), iter([None] * 200)
    host = ContainerHost(
        docker.as_client(), "test", sleep=lambda _seconds: next(waits_before_the_test_gives_up), clock=lambda: next(seconds)
    )
    with pytest.raises(ContainerHostError, match="did not accept connections within 60 s"):
        host.await_database(DATABASE, SERVER)


def test_the_web_part_joins_the_lanes_and_its_data_set_with_its_databases_password() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    password = password_kept(docker)
    host.start_instance(TARGET, "shop-testing-1", LINK)
    started = docker.last_run()
    assert (list(started["networking_config"]), started["network"]) == ([LANE_NETWORK, DATA_SET_NETWORK], LANE_NETWORK)
    assert started["environment"]["PGPASSWORD"] == password


def test_the_job_joins_only_its_data_set_runs_its_command_and_has_its_databases_password() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    password = password_kept(docker)
    host.start_job(TARGET, "shop-testing-1", LINK, ["python", "/app.py", "job"])
    started = docker.last_run()
    assert (list(started["networking_config"]), started["command"]) == ([DATA_SET_NETWORK], ["python", "/app.py", "job"])
    assert started["environment"]["PGPASSWORD"] == password


def watching(docker: RecordingDocker, system_times: list[str], running: bool = True) -> ContainerHost:
    docker.api.started_at["a-job"] = STARTED
    docker.api.running["a-job"] = running
    docker.api.system_times = iter(system_times)
    clock = iter(range(1000))
    return ContainerHost(docker.as_client(), "test", sleep=lambda _seconds: None, clock=lambda: next(clock))


def test_awaiting_a_job_returns_only_once_it_has_run_its_whole_watch() -> None:
    docker = RecordingDocker()
    times = ["2026-11-15T20:00:00.5Z", "2026-11-15T20:00:01.5Z", "2026-11-15T20:00:03.5Z"]
    watching(docker, times).await_job("a-job", 3)
    assert next(docker.api.system_times, None) is None


def test_awaiting_a_job_that_is_not_running_fails_with_why() -> None:
    with pytest.raises(PartStopped, match="not running"):
        watching(RecordingDocker(), ["2026-11-15T20:00:01Z"], running=False).await_job("a-job", 3)


def test_a_job_that_keeps_starting_again_fails_its_watch_instead_of_being_watched_without_end() -> None:
    never_past_half_a_second = ["2026-11-15T20:00:00.5Z"] * 100
    with pytest.raises(PartStopped, match="keeps starting again"):
        watching(RecordingDocker(), never_past_half_a_second).await_job("a-job", 3)


def test_everything_launchplane_starts_is_started_again_by_docker_when_it_exits() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    host.start_instance(TARGET, "shop-testing-1", LINK)
    host.start_job(TARGET, "shop-testing-1", LINK, ["job"])
    host.ensure_service(PROXY)
    assert [run["restart_policy"] for run in docker.containers.runs] == [{"Name": "always"}] * 4


def test_nothing_is_attached_to_a_lanes_network_that_exists_and_is_not_quiet() -> None:
    docker = RecordingDocker()
    docker.networks.made.append(network(True, {}, name=LANE_NETWORK, labels={}))
    with pytest.raises(ContainerHostError, match="not quiet"):
        ContainerHost(docker.as_client(), "test").start_instance(TARGET, "shop-testing-1", None)
    assert docker.containers.runs == []


def test_a_password_is_drawn_at_random_so_one_does_not_give_away_the_next() -> None:
    docker = RecordingDocker()
    for number in range(20):
        database_made(docker, f"d{number}")
    passwords = [password_kept(docker, f"d{number}") for number in range(20)]
    assert len({password[:8] for password in passwords}) == len({password[-8:] for password in passwords}) == 20


FIRST_PASSWORD = "from launchplane.instance import new_password; print(new_password())"  # noqa: S105 a program, not a password


def test_the_first_password_of_one_launchplane_is_not_the_first_of_the_next_as_a_counter_would_make_it() -> None:
    first = [subprocess.run([sys.executable, "-c", FIRST_PASSWORD], capture_output=True, text=True, check=True) for _ in range(2)]  # noqa: S603
    assert first[0].stdout != first[1].stdout
    assert len(first[0].stdout.strip()) >= 32


def restarted_launchplane(docker: RecordingDocker) -> ContainerHost:
    return ContainerHost(docker.as_client(), "test")


@pytest.mark.parametrize("left", [DATABASE, "shop-testing-1", "shop-testing-1-job"])
def test_a_secret_is_read_from_whatever_still_holds_it_whichever_containers_are_gone(left: str) -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    password = password_kept(docker)
    host.start_instance(TARGET, "shop-testing-1", LINK)
    host.start_job(TARGET, "shop-testing-1", LINK, ["job"])
    for name, holder in docker.containers.by_name.items():
        holder.removed = name != left
    assert restarted_launchplane(docker).read_secrets() == {password}


def test_a_secret_this_launchplane_gave_out_stays_one_after_everything_holding_it_is_gone() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    password = password_kept(docker)
    docker.containers.by_name[DATABASE].removed = True
    assert (host.read_secrets(), host.secrets_seen()) == ({password}, {password})


def test_a_password_read_to_give_to_an_instance_is_a_secret_before_the_host_is_asked_for_secrets() -> None:
    docker = RecordingDocker()
    database_made(docker)
    password = password_kept(docker)
    host = restarted_launchplane(docker)
    host.start_instance(TARGET, "shop-testing-1", LINK)
    assert host.secrets_seen() == {password}


def test_a_description_of_unexpected_shape_is_an_error_that_repeats_nothing_docker_answered() -> None:
    described = {"Name": "/shop-testing-1", "Config": {"Env": ["PGPASSWORD=hunter2-hunter2"]}}
    with pytest.raises(ContainerHostError, match="State: Field required") as refused:
        secrets_held({SECRET_VARIABLES_LABEL: "PGPASSWORD"}, described, "shop-testing-1")
    assert "hunter2" not in str(refused.value)


JOB = container("a-job", "job", [])
RUNNING_DATABASE = container(DATABASE, "database", [DATA_SET_NETWORK])
STOPPED_DATABASE = RUNNING_DATABASE | {"State": "exited"}


def servers_of_shop(product: str) -> DatabaseServer | None:
    return SERVER if product == "shop" else None


def test_a_job_gone_between_being_listed_and_described_is_read_again_not_called_a_host_that_cannot_be_read() -> None:
    docker = RecordingDocker()
    docker.api.container_lists = iter([[JOB], []])
    docker.api.gone_before_described = {"a-job"}
    assert ContainerHost(docker.as_client(), "test").read().jobs == []


def test_a_host_that_changes_during_three_reads_in_a_row_is_an_error_not_read_without_end() -> None:
    docker = RecordingDocker()
    docker.api.container_lists = iter([[JOB]] * 10)
    docker.api.gone_before_described = {"a-job"}
    with pytest.raises(ContainerHostError, match="changed during each of 3 reads"):
        ContainerHost(docker.as_client(), "test").read()
    assert len(list(docker.api.container_lists)) == 7


def refusal(status: int) -> APIError:
    response = requests.Response()
    response.status_code = status
    return APIError("the container is not running", response=response)


NO_LONGER_AS_LISTED = {"gone": NotFound("No such container"), "not running any more": refusal(409)}


@pytest.mark.parametrize("answer", NO_LONGER_AS_LISTED.values(), ids=NO_LONGER_AS_LISTED.keys())
def test_a_database_listed_as_running_and_found_otherwise_when_asked_is_read_again(answer: APIError) -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    docker.containers.by_name[DATABASE].refuses_commands_with = answer
    docker.api.container_lists = iter([[RUNNING_DATABASE], [STOPPED_DATABASE]])
    [found] = host.read(servers_of_shop).data_sets
    assert [(database.state, database.accepts_connections) for database in found.databases] == [(InstanceState.STOPPED, None)]


def test_a_database_that_cannot_be_asked_for_another_reason_is_an_error_at_once_not_read_again() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    docker.containers.by_name[DATABASE].refuses_commands_with = refusal(500)
    docker.api.container_lists = iter([[RUNNING_DATABASE]] * 3)
    with pytest.raises(ContainerHostError, match="whether it accepts connections"):
        host.read(servers_of_shop)
    assert len(list(docker.api.container_lists)) == 2


def test_only_the_databases_of_a_product_whose_server_is_known_are_asked_whether_they_accept_connections() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    labels = DATA_SET_LABELS | {"launchplane.product": "other", "launchplane.part": "database"}
    docker.api.container_lists = iter([[RUNNING_DATABASE, container("other-database", "database", []) | {"Labels": labels}]])
    databases = [database for found in host.read(servers_of_shop).data_sets for database in found.databases]
    assert {database.container: database.accepts_connections for database in databases} == {DATABASE: True, "other-database": None}


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
        networks=(HostNetwork(name="network-d1", is_quiet=True),),
        volumes=("volume-a", "volume-b"),
        databases=(DataSetDatabase(container="database-d1", state=InstanceState.RUNNING, networks=("network-d1",)),),
    )
    ContainerHost(as_docker_client(client), "test").delete_data_set(found)
    assert client.removed == [
        "stop database-d1 within 30",
        "remove database-d1",
        "remove volume-a",
        "remove volume-b",
        "remove network-d1",
    ]


def test_a_product_with_no_description_is_a_web_product_with_no_data(tmp_path: Path) -> None:
    assert ProductFolder(tmp_path).read("site") == Product()


def test_a_description_launchplane_cannot_read_is_an_error_naming_the_file(tmp_path: Path) -> None:
    (tmp_path / "shop.json").write_text('{"database": {"image": "postgres:18"}, "datbase": {}}')
    with pytest.raises(ProductError, match=r"shop\.json"):
        ProductFolder(tmp_path).read("shop")
