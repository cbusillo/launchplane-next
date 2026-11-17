from collections.abc import Sequence
from pathlib import Path

import pytest

from launchplane.container_host import ContainerHost, NewDatabase, PartStopped
from launchplane.data_set import REMOVE_LOGIN, RULES_FILE, login_of
from launchplane.docker_answers import ContainerHostError, HostAnswers, OnHost, on_host_from_answers
from launchplane.host_secrets import SECRET_VARIABLES_LABEL, secrets_held
from launchplane.instance import LANE_NETWORK, DataSet, DataSetDatabase, DataSetUse, HostNetwork, InstanceState, LaneBuild, Mount
from launchplane.product import Product, ProductError, ProductFolder
from tests.recording_docker import RecordingDocker, RecordingServer

DATA_SET_LABELS = {"launchplane.product": "shop", "launchplane.lane": "testing", "launchplane.data-set": "d1"}
QUIET_OPTIONS = {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}
TARGET = LaneBuild(product="shop", lane="testing", build="shop:a")
DATA_SET_NETWORK = "launchplane-data-d1"
DATABASE = "shop-testing-database-d1"
LINK = DataSetUse("d1", DATA_SET_NETWORK, DATABASE)
STARTED = "2026-11-15T20:00:00.000000001Z"


def network(
    internal: bool, options: dict[str, str] | None, name: str = DATA_SET_NETWORK, labels: dict[str, str] | None = None
) -> dict[str, object]:
    return {"Name": name, "Internal": internal, "Options": options, "Labels": DATA_SET_LABELS if labels is None else labels}


def container(name: str, part: str, networks: list[str], mounts: Sequence[object] = ()) -> dict[str, object]:
    return {
        "Names": [f"/{name}"],
        "Image": "postgres:18-alpine",
        "State": "running",
        "Labels": DATA_SET_LABELS | {"launchplane.part": part},
        "NetworkSettings": {"Networks": {each: {} for each in networks}},
        "Mounts": list(mounts),
    }


def background_service_details(name: str, running: bool = True, restarting: bool = False, exit_code: int = 0) -> dict[str, object]:
    state = {"Running": running, "Restarting": restarting, "Paused": False, "StartedAt": STARTED, "ExitCode": exit_code}
    return {"Name": f"/{name}", "Config": {"Env": []}, "State": state}


def stopped(listed: dict[str, object]) -> dict[str, object]:
    return listed | {"State": "exited"}


def read(
    containers: Sequence[object],
    networks: Sequence[object] = (),
    volumes: Sequence[object] = (),
    described: Sequence[object] = (),
    system_time: str = "2026-11-15T20:00:00Z",
) -> OnHost:
    answers = HostAnswers(list(containers), list(networks), {"Volumes": list(volumes)}, list(described), {"SystemTime": system_time})
    return on_host_from_answers(answers)


def test_a_stopped_database_is_read_with_how_it_exited_so_a_killed_one_is_told_from_a_clean_stop() -> None:
    listed: list[object] = [
        stopped(container("database-clean", "database", [])),
        stopped(container("database-killed", "database", [])),
    ]
    described: list[object] = [
        background_service_details("database-clean", running=False),
        background_service_details("database-killed", running=False, exit_code=137),
    ]
    [found] = read(listed, described=described).data_sets
    assert {database.container: database.exit_code for database in found.databases} == {"database-clean": 0, "database-killed": 137}


def test_a_stopped_database_docker_lists_but_does_not_describe_is_an_error_not_one_that_stopped_cleanly() -> None:
    with pytest.raises(ContainerHostError, match="did not describe it"):
        read([stopped(container("database-d1", "database", []))])


def test_a_database_is_given_longer_to_stop_than_a_part_before_docker_kills_it() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    host.start_instance(TARGET, "shop-testing-1", LINK)
    host.stop_part("shop-testing-1")
    host.stop_database(DATABASE)
    assert docker.containers.by_name["shop-testing-1"].calls[-1] == "stop within 30 s"
    assert docker.containers.by_name[DATABASE].calls[-1] == "stop within 60 s"


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
                DataSetDatabase(
                    container=DATABASE, state=InstanceState.RUNNING, networks=(DATA_SET_NETWORK,), image="postgres:18-alpine"
                ),
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


def test_a_background_service_is_read_with_how_long_it_has_run_by_dockers_own_clock() -> None:
    described: list[object] = [
        background_service_details("a-background-service"),
        background_service_details("b-background-service", running=False),
    ]
    containers: list[object] = [
        container("a-background-service", "background-service", []),
        container("b-background-service", "background-service", []),
    ]
    background_services = read(containers, described=described, system_time="2026-11-15T20:00:02.5Z").background_services
    assert {background_service.container: background_service.running_for_seconds for background_service in background_services} == {
        "a-background-service": pytest.approx(2.5),
        "b-background-service": None,
    }


def test_a_background_service_docker_calls_running_while_it_waits_to_restart_has_not_run_at_all() -> None:
    described: list[object] = [background_service_details("a-background-service", restarting=True)]
    background_services = read(
        [container("a-background-service", "background-service", [])], described=described, system_time="2026-11-15T20:00:09Z"
    ).background_services
    assert background_services[0].running_for_seconds is None


def test_a_background_service_docker_lists_but_does_not_describe_is_an_error_not_a_background_service_of_unknown_age() -> None:
    with pytest.raises(ContainerHostError, match="did not describe it"):
        read([container("a-background-service", "background-service", [])], described=[], system_time="2026-11-15T20:00:02Z")


def test_a_database_and_a_background_service_are_not_read_as_instances() -> None:
    containers: list[object] = [container("db", "database", []), container("a-background-service", "background-service", [])]
    on_host = read(containers, described=[background_service_details("a-background-service")], system_time="2026-11-15T20:00:02Z")
    assert (on_host.instances, [found.container for found in on_host.background_services]) == ([], ["a-background-service"])


def test_a_container_marked_with_a_part_launchplane_does_not_know_is_an_error_not_skipped() -> None:
    with pytest.raises(ContainerHostError, match="part cache"):
        read([container("shop-x", "cache", [])])


def test_a_background_service_is_read_with_the_data_set_it_says_it_runs_against() -> None:
    background_services = read(
        [container("a-background-service", "background-service", [])], described=[background_service_details("a-background-service")]
    ).background_services
    assert [background_service.data_set for background_service in background_services] == ["d1"]


VOLUME = "launchplane-data-d1-database"
SET_UP = "/docker-entrypoint-initdb.d/launchplane.sh"
WEB, OTHER_WEB = "shop-testing-aaaaaaaaaaaa", "shop-testing-bbbbbbbbbbbb"
BACKGROUND_SERVICE = f"{WEB}-background-service"
RULES_OF_THE_IMAGE = [["local", ["all"], ["all"], None, "trust", None], ["host", ["all"], ["all"], "all", "reject", None]]


def database_made(docker: RecordingDocker) -> ContainerHost:
    host = ContainerHost(docker.as_client(), "test")
    host.create_database(NewDatabase("shop", "testing", "d1", DATA_SET_NETWORK, VOLUME, "postgres:18-alpine"))
    return host


def server_of(docker: RecordingDocker) -> RecordingServer:
    return docker.containers.servers_by_volume[VOLUME]


def given_to(docker: RecordingDocker, container: str) -> dict[str, str]:
    return dict(entry.split("=", 1) for entry in docker.containers.by_name[container].environment)


def test_a_new_database_gets_its_set_up_before_it_first_starts_and_refuses_every_address_until_it_has_run() -> None:
    docker = RecordingDocker()
    database_made(docker)
    database = docker.containers.by_name[DATABASE]
    assert database.calls == [f"write {SET_UP}", "start"]
    assert RULES_FILE in database.files[SET_UP].decode()
    assert given_to(docker, DATABASE)["POSTGRES_HOST_AUTH_METHOD"] == "reject"


def test_no_instance_is_started_and_no_login_made_on_a_data_set_whose_access_rules_are_not_launchplanes() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    server_of(docker).rules = RULES_OF_THE_IMAGE
    with pytest.raises(ContainerHostError, match="of the data set d1 does not have Launchplane's access rules"):
        host.start_instance(TARGET, WEB, LINK)
    assert (WEB in docker.containers.by_name, server_of(docker).asked) == (False, [])


def test_an_instance_gets_a_login_of_its_own_and_the_server_is_sent_a_verifier_never_the_password() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    host.start_instance(TARGET, WEB, LINK)
    given = given_to(docker, WEB)
    [(_, sent)] = server_of(docker).asked
    assert (given["PGUSER"], given["PGDATABASE"], sent["login"]) == ("i_aaaaaaaaaaaa", "app", "i_aaaaaaaaaaaa")
    assert sent["verifier"].startswith("SCRAM-SHA-256$4096:")
    assert given["PGPASSWORD"] not in " ".join(docker.containers.by_name[DATABASE].calls)


def test_two_instances_on_one_data_set_each_have_their_own_login_and_password() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    host.start_instance(TARGET, WEB, LINK)
    host.start_instance(TARGET, OTHER_WEB, LINK)
    first, second = given_to(docker, WEB), given_to(docker, OTHER_WEB)
    assert server_of(docker).logins == {first["PGUSER"], second["PGUSER"]}
    assert (first["PGUSER"] != second["PGUSER"], first["PGPASSWORD"] != second["PGPASSWORD"]) == (True, True)


def test_the_longest_product_and_lane_names_still_give_two_instances_logins_postgresql_does_not_cut_into_one() -> None:
    longest = f"{'p' * 31}-{'l' * 31}"
    logins = {login_of(f"{longest}-aaaaaaaaaaaa"), login_of(f"{longest}-bbbbbbbbbbbb")}
    assert (len(logins), max(map(len, logins)) <= 63) == (2, True)


def test_a_background_service_is_given_the_login_its_instance_holds_not_one_of_its_own() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    host.start_instance(TARGET, WEB, LINK)
    host.start_background_service(TARGET, WEB, LINK, ["work"])
    assert given_to(docker, BACKGROUND_SERVICE) == given_to(docker, WEB)
    assert len(server_of(docker).logins) == 1


def test_a_background_service_whose_instance_holds_no_login_is_not_started() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    host.start_instance(TARGET, WEB, None)
    with pytest.raises(ContainerHostError, match="holds no database login"):
        host.start_background_service(TARGET, WEB, LINK, ["work"])
    assert BACKGROUND_SERVICE not in docker.containers.by_name


def test_a_login_goes_when_the_last_container_holding_it_is_retired_and_not_before() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    host.start_instance(TARGET, WEB, LINK)
    host.start_background_service(TARGET, WEB, LINK, ["work"])
    host.start_instance(TARGET, OTHER_WEB, LINK)
    host.retire_instance(BACKGROUND_SERVICE)
    while_the_web_part_holds_it = set(server_of(docker).logins)
    host.retire_instance(WEB)
    assert (while_the_web_part_holds_it, server_of(docker).logins) == ({"i_aaaaaaaaaaaa", "i_bbbbbbbbbbbb"}, {"i_bbbbbbbbbbbb"})


def test_an_instance_removed_after_a_failed_check_loses_its_login() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    host.start_instance(TARGET, WEB, LINK)
    host.remove_instance(WEB)
    assert server_of(docker).logins == set()


def test_a_login_no_container_holds_is_removed_when_the_next_login_is_made() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    server_of(docker).logins.add("i_left_by_a_crash")
    host.start_instance(TARGET, WEB, LINK)
    assert server_of(docker).logins == {"i_aaaaaaaaaaaa"}


def test_an_instance_retired_while_its_database_is_stopped_is_retired_and_its_login_goes_when_the_next_is_made() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    host.start_instance(TARGET, WEB, LINK)
    host.stop_database(DATABASE)
    host.retire_instance(WEB)
    left = set(server_of(docker).logins)
    host.start_database(DATABASE)
    host.start_instance(TARGET, OTHER_WEB, LINK)
    assert (docker.containers.by_name[WEB].removed, left, server_of(docker).logins) == (True, {"i_aaaaaaaaaaaa"}, {"i_bbbbbbbbbbbb"})


def test_a_login_is_closed_before_its_sessions_are_ended_and_owns_nothing_before_it_is_dropped() -> None:
    order = [
        REMOVE_LOGIN.statements.index(word)
        for word in ("NOLOGIN", "pg_terminate_backend", "REASSIGN OWNED", "DROP OWNED", "DROP ROLE")
    ]
    assert order == sorted(order)


def test_a_database_that_never_accepts_connections_is_an_error_after_its_deadline() -> None:
    docker = RecordingDocker()
    database_made(docker)
    seconds, waits_before_the_test_gives_up = iter(range(1000)), iter([None] * 200)
    host = ContainerHost(
        docker.as_client(), "test", sleep=lambda _seconds: next(waits_before_the_test_gives_up), clock=lambda: next(seconds)
    )
    with pytest.raises(ContainerHostError, match="did not accept connections within 60 s"):
        host.await_database(DATABASE)


def test_a_background_service_that_keeps_starting_again_fails_its_watch() -> None:
    docker = RecordingDocker()
    docker.api.system_times = iter(["2026-11-15T20:00:00.5Z"] * 100)
    clock = iter(range(1000))
    host = ContainerHost(docker.as_client(), "test", sleep=lambda _seconds: None, clock=lambda: next(clock))
    with pytest.raises(PartStopped, match="keeps starting again"):
        host.await_background_service("shop-testing-1-background-service", 3)


def test_nothing_is_attached_to_a_lanes_network_that_exists_and_is_not_quiet() -> None:
    docker = RecordingDocker()
    docker.networks.made[LANE_NETWORK] = network(True, {}, name=LANE_NETWORK, labels={})
    with pytest.raises(ContainerHostError, match="not quiet"):
        ContainerHost(docker.as_client(), "test").start_instance(TARGET, "shop-testing-1", None)
    assert docker.containers.by_name == {}


@pytest.mark.parametrize("left", [WEB, BACKGROUND_SERVICE])
def test_a_secret_is_read_from_whatever_still_holds_it_whichever_containers_are_gone(left: str) -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    host.start_instance(TARGET, WEB, LINK)
    host.start_background_service(TARGET, WEB, LINK, ["work"])
    password = given_to(docker, WEB)["PGPASSWORD"]
    for name, holder in docker.containers.by_name.items():
        holder.removed = name != left
    restarted_launchplane = ContainerHost(docker.as_client(), "test")
    assert restarted_launchplane.secrets.read_secrets() == {password}


def test_the_password_a_database_was_first_started_with_is_read_as_a_secret_though_its_set_up_removes_it() -> None:
    docker = RecordingDocker()
    database_made(docker)
    restarted_launchplane = ContainerHost(docker.as_client(), "test")
    assert restarted_launchplane.secrets.read_secrets() == {given_to(docker, DATABASE)["POSTGRES_PASSWORD"]}


def test_a_secret_this_launchplane_gave_out_stays_one_after_everything_holding_it_is_gone() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    host.start_instance(TARGET, WEB, LINK)
    password = given_to(docker, WEB)["PGPASSWORD"]
    host.retire_instance(WEB)
    assert password in host.secrets.read_secrets()


def test_a_password_read_to_give_to_a_background_service_is_a_secret_at_once() -> None:
    docker = RecordingDocker()
    database_made(docker).start_instance(TARGET, WEB, LINK)
    restarted_launchplane = ContainerHost(docker.as_client(), "test")
    restarted_launchplane.start_background_service(TARGET, WEB, LINK, ["work"])
    assert restarted_launchplane.secrets.secrets_seen() == {given_to(docker, WEB)["PGPASSWORD"]}


def test_a_description_of_unexpected_shape_is_an_error_that_repeats_nothing_docker_answered() -> None:
    described = {"Name": "/shop-testing-1", "Config": {"Env": ["PGPASSWORD=hunter2-hunter2"]}}
    with pytest.raises(ContainerHostError, match="State: Field required") as refused:
        secrets_held({SECRET_VARIABLES_LABEL: "PGPASSWORD"}, described, "shop-testing-1")
    assert "hunter2" not in str(refused.value)


def test_a_product_with_no_description_is_a_web_product_with_no_data(tmp_path: Path) -> None:
    assert ProductFolder(tmp_path).read("site") == Product()


def test_a_description_launchplane_cannot_read_is_an_error_naming_the_file(tmp_path: Path) -> None:
    (tmp_path / "shop.json").write_text('{"database": {"image": "postgres:18"}, "datbase": {}}')
    with pytest.raises(ProductError, match=r"shop\.json"):
        ProductFolder(tmp_path).read("shop")
