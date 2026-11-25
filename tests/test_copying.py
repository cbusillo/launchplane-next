import builtins
from types import SimpleNamespace
from typing import Any

import pytest

from launchplane.act import Cancelled, Lanes, Stopped, Waiting
from launchplane.container_host import COPY_COMMAND, ContainerHost
from launchplane.copying import Copying, decide
from launchplane.data_set import DATA_PATH
from launchplane.docker_answers import ContainerHostRefused
from launchplane.instance import BACK_SOON, DataSet, InstanceState, Reading, Verdict, VolumeCopy
from launchplane.judge import Condition, Work, judge
from launchplane.page import cards
from launchplane.plan import lane_data_set
from launchplane.product import Database, Product
from launchplane.proxy import ProxyError
from launchplane.record import Asked, Record, Stands
from launchplane.steps import Cancel, CopyOutcome, CopyVolume, Event, GiveUp, StopPart
from launchplane.telling import Witness
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS
from tests.odoo_product import TK
from tests.pretend_host import (
    EARLIER,
    GENERATION,
    SINCE,
    WEB_ADDRESS,
    Given,
    LaunchplaneKilled,
    PretendHost,
    PretendProxy,
    data_set_named,
    lane_instance,
    lanes_on,
    numbered_names,
    on_data,
)
from tests.recording_docker import as_docker_client
from tests.test_act import BUILD_A
from tests.test_container_host import DATA_SET_LABELS, container, read
from tests.test_worker import CALLER, kept_of

RUNNING, STOPPED = InstanceState.RUNNING, InstanceState.STOPPED
DATABASE = "database-data-1"
SOURCE = ("volume-data-1", "files-data-1")
WITH_A_DATABASE = Product(database=Database(image="postgres:18"))


def first(number: int = 1, **more: Any) -> Witness:  # noqa: ANN401 whatever a witness takes
    return Witness(generation=GENERATION, attempt=number, **more)


# One lane of the Odoo kind, serving: a database volume and a files volume, one instance, no background service.
def serving() -> tuple[Lanes, PretendProxy, str]:
    proxy = PretendProxy(PretendHost())
    lanes = lanes_on(proxy, TK)
    lanes.move(BUILD_A, EARLIER, 1)
    proxy.host.actions.clear()
    return lanes, proxy, proxy.written[WEB_ADDRESS]


def copy_on(proxy: PretendProxy) -> DataSet:
    [copy] = [each for each in proxy.host.data_sets.values() if each.copy_of is not None]
    return copy


def frozen(web: str) -> list[str]:
    return ["show back soon", f"stop {web}", f"stop database {DATABASE}"]


def brought_back(web: str) -> list[str]:
    return [f"start database {DATABASE}", f"await {DATABASE}", f"start {web}", f"check {web}/web/login", f"route {web}"]


def test_a_whole_copy_is_made_of_both_stopped_volumes_and_visitors_return_to_the_same_instance_with_no_release() -> None:
    lanes, proxy, web = serving()
    result = lanes.copy("site", "testing", GENERATION, first())
    copy = copy_on(proxy)
    made = [f"volume {copy.data_set}", f"files {copy.data_set}"]
    copied = [f"copy {source} to {to}" for source, to in zip(SOURCE, (*copy.volumes, *copy.files), strict=True)]
    assert proxy.host.actions == [*made, *frozen(web), *copied, *brought_back(web)]
    assert (result.outcome, result.container_receiving_visitors, result.data_set, result.copied_to, result.error) == (
        CopyOutcome.COPIED,
        web,
        "data-1",
        copy.data_set,
        None,
    )
    assert (copy.is_whole_copy, copy.networks, copy.databases) == (True, (), ())
    assert (proxy.loaded_to(WEB_ADDRESS), proxy.gate.verdict(WEB_ADDRESS, GENERATION).verdict) == (web, Verdict.NONE)
    assert [each.condition for each in lanes.read_status().lanes] == [Condition.GOOD]


def test_a_back_soon_route_the_proxy_has_not_loaded_stops_nothing_and_copies_nothing() -> None:
    lanes, proxy, _ = serving()
    proxy.holds_delivery = True
    with pytest.raises(ProxyError, match="did not load"):
        lanes.copy("site", "testing", GENERATION, first())
    assert proxy.host.actions[2:] == ["show back soon"]


def frozen_by_this_request(given: Given, product: Product = WITH_A_DATABASE, done: tuple[Event, ...] = ()) -> Copying:
    proxy = given.proxy()
    proxy.edited[WEB_ADDRESS] = (BACK_SOON, f"{BACK_SOON} {GENERATION} 1")
    reading = Reading(SINCE, proxy.read(), proxy.host.read()).lane("site", "testing")
    return Copying(judge(reading, product, Work.COPY), product, GENERATION, "fresh", done)


def web_part(state: InstanceState) -> Any:  # noqa: ANN401 a part of a given lane
    return on_data(lane_instance("web", "b", state), "d1")


def copy_of_d1(**more: object) -> DataSet:
    made = DataSet(product="site", lane="testing", data_set="d2", networks=(), volumes=("volume-d2",), databases=())
    return made.model_copy(update={"generation": GENERATION, "copy_of": "d1"} | more)


IN_MAINTENANCE = (BACK_SOON, BACK_SOON)


def test_a_writer_that_still_runs_is_stopped_before_anything_is_copied() -> None:
    given = Given((web_part(RUNNING),), IN_MAINTENANCE, data_sets=(data_set_named("d1"), copy_of_d1()))
    assert decide(frozen_by_this_request(given)) == StopPart("web")


def test_a_database_server_that_did_not_stop_cleanly_is_never_copied() -> None:
    given = Given((web_part(STOPPED),), IN_MAINTENANCE, data_sets=(data_set_named("d1", STOPPED, exit_code=137), copy_of_d1()))
    step = decide(frozen_by_this_request(given))
    assert isinstance(step, GiveUp) and "did not stop cleanly: it exited with code 137" in step.why


def test_each_volume_is_copied_from_the_data_set_the_copy_names_once_everything_is_stopped() -> None:
    given = Given((web_part(STOPPED),), IN_MAINTENANCE, data_sets=(data_set_named("d1", STOPPED), copy_of_d1()))
    assert decide(frozen_by_this_request(given)) == CopyVolume("site", "testing", "d2", "volume-d1", "volume-d2", "postgres:18")


def test_a_copy_whose_volume_is_a_volume_of_what_it_copies_is_given_up_before_anything_is_written() -> None:
    given = Given(
        (web_part(STOPPED),), IN_MAINTENANCE, data_sets=(data_set_named("d1", STOPPED), copy_of_d1(volumes=("volume-d1",)))
    )
    step = decide(frozen_by_this_request(given))
    assert isinstance(step, GiveUp) and "are not one fresh volume, still to be written, for each volume of d1" in step.why


def test_a_copy_another_attempt_began_is_given_up_and_never_gone_on_with() -> None:
    half = VolumeCopy(container="volume-d2-copier", volume="volume-d2", finished=True, stopped=True)
    files = {"files": ("files-d1",)}
    given = Given(
        (web_part(STOPPED),),
        IN_MAINTENANCE,
        data_sets=(data_set_named("d1", STOPPED).model_copy(update=files), copy_of_d1(files=("files-d2",), copies=(half,))),
    )
    step = decide(frozen_by_this_request(given))
    assert isinstance(step, GiveUp) and "An earlier attempt began the copy d2 and did not finish it" in step.why


def test_a_copier_still_running_is_stopped_before_the_volume_it_reads_is_written_again() -> None:
    running = VolumeCopy(container="volume-d2-copier", volume="volume-d2", finished=False, stopped=False)
    given = Given((web_part(STOPPED),), IN_MAINTENANCE, data_sets=(data_set_named("d1", STOPPED), copy_of_d1(copies=(running,))))
    assert decide(frozen_by_this_request(given), Cancel()) == StopPart("volume-d2-copier")


def test_a_lane_starts_on_its_own_data_set_and_never_on_a_copy_or_on_what_was_left_of_one() -> None:
    lane = Given(data_sets=(data_set_named("d1"), copy_of_d1())).reading()
    only_a_copy = Given(data_sets=(copy_of_d1(),)).reading()
    assert (lane_data_set(lane, None, "fresh"), lane_data_set(only_a_copy, None, "fresh")) == ("d1", "fresh")


def test_an_unclean_stop_gives_the_copy_up_and_the_lane_comes_back_with_nothing_copied() -> None:
    lanes, proxy, web_part = serving()
    proxy.host.databases_that_must_be_killed = {DATABASE}
    result = lanes.copy("site", "testing", GENERATION, first())
    assert proxy.host.actions[2:] == [*frozen(web_part), *brought_back(web_part)]
    assert (result.outcome, proxy.loaded_to(WEB_ADDRESS), copy_on(proxy).copies) == (CopyOutcome.NOT_COPIED, web_part, ())
    assert "did not stop cleanly" in (result.error or "")


def test_a_copy_refused_at_its_second_volume_is_never_whole_and_the_lane_comes_back() -> None:
    lanes, proxy, web_part = serving()
    proxy.host.copies_refused = {"files-data-21"}
    result = lanes.copy("site", "testing", GENERATION, first())
    assert proxy.host.actions[-5:] == brought_back(web_part)
    assert (result.outcome, copy_on(proxy).is_whole_copy) == (CopyOutcome.NOT_COPIED, False)
    assert "ended with code 1" in (result.error or "")


def killed_between_the_volumes() -> tuple[Lanes, PretendProxy, str]:
    lanes, proxy, web_part = serving()
    proxy.host.killed_after = 6
    with pytest.raises(LaunchplaneKilled):
        lanes.copy("site", "testing", GENERATION, first())
    assert proxy.host.actions[-1] == "copy volume-data-1 to volume-data-21"
    proxy.host.killed_after = None
    proxy.host.actions.clear()
    return lanes, proxy, web_part


def test_after_a_kill_between_the_volumes_the_next_attempt_copies_nothing_more_and_brings_the_same_instance_back() -> None:
    lanes, proxy, web_part = killed_between_the_volumes()
    result = lanes.copy("site", "testing", GENERATION, first(2))
    assert proxy.host.actions == brought_back(web_part)
    assert (result.outcome, result.container_receiving_visitors, copy_on(proxy).is_whole_copy) == (
        CopyOutcome.NOT_COPIED,
        web_part,
        False,
    )
    assert "An earlier attempt began the copy data-21 and did not finish it" in (result.error or "")


def test_recovery_run_again_once_the_lane_is_back_stops_nothing_and_copies_nothing() -> None:
    lanes, proxy, _ = killed_between_the_volumes()
    lanes.copy("site", "testing", GENERATION, first(2))
    proxy.host.actions.clear()
    again = lanes.copy("site", "testing", GENERATION, first(3))
    assert (again.outcome, proxy.host.actions) == (CopyOutcome.NOT_COPIED, [])


def test_a_kill_after_both_volumes_were_copied_still_ends_as_copied_from_what_the_host_shows() -> None:
    lanes, proxy, web_part = serving()
    proxy.host.killed_after = 7
    with pytest.raises(LaunchplaneKilled):
        lanes.copy("site", "testing", GENERATION, first())
    proxy.host.killed_after = None
    proxy.host.actions.clear()
    result = lanes.copy("site", "testing", GENERATION, first(2))
    assert (result.outcome, proxy.host.actions) == (CopyOutcome.COPIED, brought_back(web_part))


def test_a_cancel_during_the_copy_brings_the_same_instance_back_and_keeps_what_was_copied() -> None:
    lanes, proxy, web_part = serving()

    def asked_once_a_volume_is_copied() -> bool:
        return any(each.startswith("copy ") for each in proxy.host.actions)

    with pytest.raises(Cancelled) as cancelled:
        lanes.copy("site", "testing", GENERATION, first(cancel_asked=asked_once_a_volume_is_copied))
    assert proxy.host.actions[-5:] == brought_back(web_part)
    assert (cancelled.value.put_back.data_sets_kept, copy_on(proxy).is_whole_copy) == (["data-21"], False)
    assert proxy.loaded_to(WEB_ADDRESS) == web_part


def test_a_lane_another_request_put_in_maintenance_is_left_as_it_is() -> None:
    lanes, proxy, _ = serving()
    lanes.begin_maintenance("site", "testing")
    proxy.host.actions.clear()
    with pytest.raises(Stopped, match="this request did not put it there"):
        lanes.copy("site", "testing", GENERATION, first())
    assert (proxy.host.actions, proxy.written[WEB_ADDRESS]) == ([], BACK_SOON)


def test_while_the_instance_fails_its_check_the_back_soon_page_stays_and_the_request_waits_to_try_again() -> None:
    lanes, proxy, web_part = serving()
    proxy.failing = {web_part}
    with pytest.raises(Waiting, match="could not be brought back yet"):
        lanes.copy("site", "testing", GENERATION, first())
    assert (proxy.written[WEB_ADDRESS], proxy.host.is_in(web_part, STOPPED)) == (BACK_SOON, True)
    proxy.failing.clear()
    result = lanes.copy("site", "testing", GENERATION, first(2))
    assert (result.outcome, proxy.loaded_to(WEB_ADDRESS)) == (CopyOutcome.COPIED, web_part)


def details(name: str, status: str, exit_code: int = 0) -> dict[str, object]:
    state = {"Running": status == "running", "Restarting": False, "Paused": False, "StartedAt": "2026-11-15T20:00:00Z"}
    return {"Name": f"/{name}", "Config": {"Env": []}, "State": state | {"ExitCode": exit_code, "Status": status}}


def test_a_copier_is_read_as_finished_only_once_its_description_says_it_exited_with_code_0_whatever_the_list_says() -> None:
    mounts = [
        {"Name": "source", "Source": "", "Destination": "/from", "RW": False},
        {"Name": "copy", "Source": "", "Destination": "/to"},
    ]
    listed = [container(name, "copier", [], mounts) for name in ("done", "failed", "made", "running")]
    described = [
        details("done", "exited"),
        details("failed", "exited", 1),
        details("made", "created"),
        details("running", "running"),
    ]
    volumes = [{"Name": "copy", "Labels": DATA_SET_LABELS | {"launchplane.copy-of": "d0", "launchplane.generation": GENERATION}}]
    [found] = read(listed, volumes=volumes, described=described).data_sets
    assert (found.copy_of, found.generation, found.is_whole_copy) == ("d0", GENERATION, False)
    assert [(each.container, each.volume, each.finished, each.stopped) for each in found.copies] == [
        ("done", "copy", True, True),
        ("failed", "copy", False, True),
        ("made", "copy", False, True),
        ("running", "copy", False, False),
    ]


class Copier:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code

    def start(self) -> None:
        pass

    def wait(self, timeout: int) -> dict[str, int]:
        assert timeout
        return {"StatusCode": self.exit_code}

    @staticmethod
    def logs(tail: int) -> bytes:
        assert tail
        return b"cp: lost"


class Copiers:
    def __init__(self, writers: builtins.list[object], exit_code: int = 0) -> None:
        self.writers, self.exit_code = writers, exit_code
        self.made: builtins.list[tuple[object, ...]] = []
        self.asked: dict[str, object] = {}

    def list(self, filters: dict[str, object]) -> builtins.list[object]:
        self.asked = filters
        return self.writers

    def create(self, image: str, command: builtins.list[str], **options: object) -> Copier:
        self.made.append((image, command, options["volumes"], options["network_mode"]))
        return Copier(self.exit_code)


COPY = CopyVolume("shop", "testing", "d2", "source", "copy", "postgres:18-alpine")


def test_the_copier_can_only_read_what_it_copies_reaches_no_network_and_writes_the_fresh_volume_alone() -> None:
    copiers = Copiers([])
    ContainerHost(as_docker_client(SimpleNamespace(containers=copiers)), "test").copy_volume(COPY)
    mounted = {"source": {"bind": "/from", "mode": "ro"}, "copy": {"bind": DATA_PATH, "mode": "rw"}}
    assert copiers.made == [("postgres:18-alpine", COPY_COMMAND, mounted, "none")]
    assert copiers.asked == {"volume": "source", "status": "running"}


def test_a_volume_a_running_container_has_mounted_is_refused_and_no_copier_is_made() -> None:
    copiers = Copiers([SimpleNamespace(name="a-writer")])
    with pytest.raises(ContainerHostRefused, match="a-writer runs with the volume source mounted"):
        ContainerHost(as_docker_client(SimpleNamespace(containers=copiers)), "test").copy_volume(COPY)
    assert copiers.made == []


def test_a_copier_that_ends_with_an_error_is_a_refusal_with_what_it_wrote_to_its_log() -> None:
    copiers = Copiers([], exit_code=1)
    with pytest.raises(ContainerHostRefused, match="ended with code 1: cp: lost"):
        ContainerHost(as_docker_client(SimpleNamespace(containers=copiers)), "test").copy_volume(COPY)


def worked(record: Record, refused: set[str]) -> tuple[Any, Lanes]:
    proxy = PretendProxy(PretendHost())
    lanes = Lanes(
        proxy.host, proxy, lambda _product: TK, numbered_names(), read_unfinished=record.unfinished, read_ended=record.ended_lately
    )
    lanes.move(BUILD_A, EARLIER, 1)
    proxy.host.copies_refused = refused
    request = record.write(Asked(product="site", lane="testing", work=Work.COPY), CALLER, None)
    assert Worker(record, lanes, NO_SECRETS).work_next()
    return kept_of(record, request.id), lanes


def test_a_copy_asked_for_is_worked_as_a_request_and_ends_done_with_where_the_copy_is(record: Record) -> None:
    kept, lanes = worked(record, set())
    assert (kept.stands, kept.answer and kept.answer["outcome"], kept.answer and kept.answer["copied_to"]) == (
        Stands.DONE,
        "copied",
        "data-21",
    )
    rows = [row for card in cards(lanes.read_status()) for row in card.rows if row.name == "Data set"]
    assert (
        "".join(piece.text for piece in rows[0].value)
        == "data-21 is a whole copy of data-1, kept offline: no instance is started on it"
    )


def test_a_copy_that_was_not_finished_ends_its_request_as_failed_with_why_and_the_page_says_what_was_left(record: Record) -> None:
    kept, lanes = worked(record, {"files-data-21"})
    assert (kept.stands, kept.answer and kept.answer["outcome"]) == (Stands.FAILED, "not copied")
    assert "ended with code 1" in (kept.error or "")
    assert "What was copied is kept, and no instance is started on it." in (kept.error or "")
    rows = [row for card in cards(lanes.read_status()) for row in card.rows if row.name == "Data set"]
    assert "is a copy that was not finished of data-1" in "".join(piece.text for piece in rows[0].value)


def test_a_data_set_is_not_deleted_while_a_copy_of_it_is_on_the_host_and_goes_once_the_copy_has() -> None:
    lanes, proxy, _ = serving()
    copied_to = lanes.copy("site", "testing", GENERATION, first()).copied_to
    proxy.host.actions.clear()
    with pytest.raises(Stopped, match=f"The data set data-1 has the copy {copied_to}.* Delete the copy first, by name"):
        lanes.retire("site", "testing", "data-1")
    assert not [each for each in proxy.host.actions if each.startswith("delete")]
    lanes.retire("site", "testing", copied_to)
    lanes.retire("site", "testing", "data-1")
    assert (proxy.host.actions[-2:], proxy.host.data_sets) == ([f"delete {copied_to}", "delete data-1"], {})
