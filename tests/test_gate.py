import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest

from launchplane.docker_answers import CommandResult
from launchplane.gate import ALREADY, DONE, GATE_FOLDER_NAME, LOST, Answer, Gate, GateError
from launchplane.instance import BACK_SOON, Verdict
from launchplane.proxy import installed_text, route_text
from launchplane.record import Record, Stands
from tests.pretend_host import EARLIER, GENERATION
from tests.test_worker import CALLER, MOVE_B, kept_of, serving_a

WEB = "testing.site.test"
NONE, ABANDONED = Answer(Verdict.NONE), Answer(Verdict.ABANDONED)


def committed(candidate: str) -> Answer:
    return Answer(Verdict.COMMITTED, candidate)


# The gate's command run as it is, by a real shell, on a folder that stands for the one the proxy's routes folder is in. The lock is
# the host's and is left out: these tests send one command at a time.
@dataclass
class FolderHost:
    folder: Path
    sent: list[list[str]] = field(default_factory=list[list[str]])
    runs: bool = True
    loses_replies: int = 0

    def run_in(self, container: str, command: list[str]) -> CommandResult:
        self.sent.append([container, *command])
        if not self.runs:
            return CommandResult(0, "")
        ran = subprocess.run(command[command.index("sh") :], capture_output=True, text=True, check=False)  # noqa: S603
        if self.loses_replies:
            self.loses_replies -= 1
            raise ConnectionError("the reply was lost")
        return CommandResult(ran.returncode, (ran.stdout or ran.stderr).strip())


@dataclass
class Lane:
    host: FolderHost
    gate: Gate
    route: Path
    renames: int = 0

    def switch(self, candidate: str, generation: str = GENERATION, attempt: int = 1) -> Answer:
        before = self.route.stat().st_ino if self.route.exists() else None
        try:
            return self.gate.switch(WEB, generation, attempt, candidate, f"to {candidate}", PurePosixPath(self.route))
        finally:
            self.renames += (self.route.stat().st_ino if self.route.exists() else None) != before

    def staged(self) -> list[str]:
        return sorted(each.name for each in (self.host.folder / GATE_FOLDER_NAME).glob("*.next"))

    # A change of the route that is no release, asked by one attempt of one request from the routes file as it reads now.
    def edit(self, to: str | None, generation: str, attempt: int = 1, read: str | None = None) -> str:
        installed = self.installed() if read is None else read
        text = "" if to is None else installed_text(WEB, to, generation, attempt)
        return self.gate.edit(WEB, generation, attempt, installed, text, PurePosixPath(self.route))

    def installed(self) -> str:
        return self.route.read_text() if self.route.exists() else ""

    # The command is sent and does not run: it is on its way, and is run by `arrive` as it was sent.
    def sent_and_held(self, to: str | None, generation: str) -> list[str]:
        self.host.runs = False
        with pytest.raises(GateError):
            self.edit(to, generation)
        self.host.runs = True
        return self.host.sent[-1]

    def arrive(self, command: list[str]) -> str:
        return self.host.run_in(command[0], command[1:]).output

    def as_it_is(self) -> tuple[str, int]:
        return self.route.read_text(), self.route.stat().st_ino


@pytest.fixture
def lane(tmp_path: Path) -> Lane:
    host = FolderHost(tmp_path)
    (tmp_path / "routes").mkdir()
    return Lane(host, Gate(host, "proxy", PurePosixPath(tmp_path)), tmp_path / "routes" / f"{WEB}.yml")


def test_a_generation_nothing_was_asked_for_has_no_verdict_and_asking_changes_nothing(lane: Lane) -> None:
    assert (lane.gate.verdict(WEB, GENERATION), lane.staged(), lane.route.exists()) == (NONE, [], False)


def test_a_switch_renames_the_routes_file_to_its_candidate_and_the_generation_is_committed_to_it_for_good(lane: Lane) -> None:
    assert lane.switch("b") == committed("b")
    assert (lane.route.read_text(), lane.gate.verdict(WEB, GENERATION), lane.renames) == ("to b", committed("b"), 1)


def test_a_rename_proves_itself_with_nothing_written_after_it(lane: Lane) -> None:
    lane.switch("b")
    evidence = sorted(each.name for each in (lane.host.folder / GATE_FOLDER_NAME).iterdir())
    assert evidence == [f"{WEB}.{GENERATION}.armed"]


def test_a_switch_whose_reply_is_lost_is_committed_when_asked_and_asked_again_renames_nothing_more(lane: Lane) -> None:
    lane.host.loses_replies = 1
    with pytest.raises(ConnectionError):
        lane.switch("b")
    assert (lane.gate.verdict(WEB, GENERATION), lane.switch("b"), lane.switch("c", attempt=2)) == (committed("b"),) * 3
    assert (lane.renames, lane.route.read_text(), lane.staged()) == (1, "to b", [])


def test_a_cancel_that_wins_is_answered_abandoned_every_time_and_no_switch_of_that_generation_renames(lane: Lane) -> None:
    asked = [lane.gate.abandon(WEB, GENERATION), lane.switch("b"), lane.gate.abandon(WEB, GENERATION), lane.switch("c", attempt=2)]
    assert (asked, lane.renames, lane.staged()) == ([ABANDONED] * 4, 0, [])


def test_a_cancel_after_the_rename_is_answered_committed_and_changes_nothing(lane: Lane) -> None:
    lane.switch("b")
    assert (lane.gate.abandon(WEB, GENERATION), lane.gate.verdict(WEB, GENERATION)) == (committed("b"), committed("b"))


def test_a_switch_sent_before_its_candidate_was_discarded_and_run_after_renames_nothing(lane: Lane) -> None:
    lane.host.runs = False
    with pytest.raises(GateError):
        lane.switch("b")
    lane.host.runs = True
    assert lane.gate.discard(WEB, GENERATION, 1, "b") == NONE
    late = lane.host.run_in(lane.host.sent[0][0], lane.host.sent[0][1:])
    assert (late.output, lane.route.exists(), lane.staged()) == ("none", False, [])


def test_a_discarded_candidate_is_never_armed_again_and_a_later_attempt_switches_to_a_fresh_one_once(lane: Lane) -> None:
    lane.gate.discard(WEB, GENERATION, 1, "b")
    assert (lane.switch("b", attempt=2), lane.switch("b2", attempt=2), lane.renames) == (NONE, committed("b2"), 1)


def test_a_discard_of_the_candidate_that_was_renamed_to_learns_that_it_won_and_takes_nothing_away(lane: Lane) -> None:
    lane.switch("b")
    assert (lane.gate.discard(WEB, GENERATION, 1, "b"), lane.gate.verdict(WEB, GENERATION)) == (committed("b"), committed("b"))
    assert lane.gate.discard(WEB, GENERATION, 1, "other") == committed("b")


def test_a_cancel_that_was_cut_short_after_its_mark_was_kept_still_answers_abandoned_and_finishes_when_sent_again(
    lane: Lane,
) -> None:
    gate_folder = lane.host.folder / GATE_FOLDER_NAME
    gate_folder.mkdir()
    for kind, text in ((".b.next", "to b"), (".armed", "1 b"), (".abandoned", "")):
        (gate_folder / f"{WEB}.{GENERATION}{kind}").write_text(text)
    assert (lane.gate.verdict(WEB, GENERATION), lane.switch("b"), lane.renames) == (ABANDONED, ABANDONED, 0)
    assert (lane.gate.abandon(WEB, GENERATION), lane.staged()) == (ABANDONED, [])


def test_recovery_sweeps_a_stage_after_its_arming_mark_and_candidate_are_gone_before_saving_cancelled(
    lane: Lane, record: Record, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, _, proxy = serving_a(record)
    monkeypatch.setattr(proxy, "gate", lane.gate)
    request = record.write(MOVE_B, CALLER, None)
    assert record.take_up(request.id) is not None
    record.cancel(request.id, CALLER)
    gate_folder = lane.host.folder / GATE_FOLDER_NAME
    gate_folder.mkdir()
    for kind, text in ((".b.next", "to b"), (".abandoned", "")):
        (gate_folder / f"{WEB}.{request.generation}{kind}").write_text(text)
    (gate_folder / f"{WEB}.{EARLIER}.a.next").write_text("to a")
    assert worker.work_next() and kept_of(record, request.id).stands is Stands.CANCELLED
    assert lane.staged() == [f"{WEB}.{EARLIER}.a.next"]


def test_a_discard_cut_short_after_its_revocation_was_kept_already_refuses_the_switch_it_was_sent_against(lane: Lane) -> None:
    gate_folder = lane.host.folder / GATE_FOLDER_NAME
    gate_folder.mkdir()
    for kind, text in ((".b.next", "to b"), (".armed", "1 b"), (".b.revoked", "")):
        (gate_folder / f"{WEB}.{GENERATION}{kind}").write_text(text)
    assert (lane.switch("b"), lane.renames, lane.gate.discard(WEB, GENERATION, 1, "b"), lane.staged()) == (NONE, 0, NONE, [])


def test_nothing_done_for_one_generation_changes_what_another_is_answered(lane: Lane) -> None:
    lane.switch("b", EARLIER)
    lane.gate.abandon(WEB, GENERATION)
    replayed = [lane.gate.abandon(WEB, EARLIER), lane.switch("c", GENERATION), lane.switch("b", EARLIER)]
    assert replayed == [committed("b"), ABANDONED, committed("b")]
    assert (lane.gate.verdict(WEB, EARLIER), lane.gate.verdict(WEB, GENERATION), lane.renames) == (committed("b"), ABANDONED, 1)


def test_a_later_release_and_the_removal_of_the_routes_file_leave_an_earlier_generations_answer_as_it_was(lane: Lane) -> None:
    lane.switch("b", EARLIER)
    lane.switch("c", GENERATION)
    lane.route.unlink()
    assert (lane.gate.verdict(WEB, EARLIER), lane.switch("b", EARLIER), lane.route.exists()) == (
        committed("b"),
        committed("b"),
        False,
    )


def test_only_a_later_attempt_arms_another_candidate_and_the_one_it_replaces_can_never_rename(lane: Lane) -> None:
    gate_folder = lane.host.folder / GATE_FOLDER_NAME
    gate_folder.mkdir()
    for kind, text in ((".b.next", "to b"), (".armed", "2 b")):
        (gate_folder / f"{WEB}.{GENERATION}{kind}").write_text(text)
    assert (lane.switch("old", attempt=1), lane.renames) == (NONE, 0)
    assert (lane.switch("c", attempt=3), lane.switch("b", attempt=2), lane.renames) == (committed("c"), committed("c"), 1)


def test_an_attempt_that_is_fenced_and_every_earlier_one_can_no_longer_switch_and_a_later_attempt_can(lane: Lane) -> None:
    lane.host.runs = False
    with pytest.raises(GateError):
        lane.switch("b", attempt=2)
    lane.host.runs = True
    assert lane.gate.fence(WEB, GENERATION, 2) == NONE
    in_transit = lane.host.run_in(lane.host.sent[0][0], lane.host.sent[0][1:])
    assert (in_transit.output, lane.switch("a", attempt=1), lane.renames) == ("none", NONE, 0)
    assert (lane.switch("c", attempt=3), lane.renames) == (committed("c"), 1)


def test_a_folder_that_cannot_be_looked_in_gives_no_answer_and_is_never_taken_as_none(lane: Lane) -> None:
    lane.switch("b")
    gate_folder = lane.host.folder / GATE_FOLDER_NAME
    gate_folder.chmod(0o000)
    try:
        with pytest.raises(GateError, match="not known"):
            lane.gate.verdict(WEB, GENERATION)
    finally:
        gate_folder.chmod(0o755)


def test_asking_for_a_verdict_where_nothing_was_ever_kept_answers_none_and_makes_nothing(lane: Lane) -> None:
    before = sorted(each.name for each in lane.host.folder.iterdir())
    assert (lane.gate.verdict(WEB, GENERATION), sorted(each.name for each in lane.host.folder.iterdir())) == (NONE, before)


def test_a_gate_path_that_exists_and_is_no_folder_gives_no_answer_and_is_never_taken_as_none(lane: Lane) -> None:
    (lane.host.folder / GATE_FOLDER_NAME).write_text("not a folder")
    with pytest.raises(GateError, match="exit code 9"):
        lane.gate.verdict(WEB, GENERATION)


def test_a_route_that_is_a_folder_is_refused_before_anything_is_staged_and_the_generation_keeps_no_verdict(lane: Lane) -> None:
    lane.route.mkdir()
    with pytest.raises(GateError, match="is not a file"):
        lane.switch("b")
    lane.route.rmdir()
    assert (lane.staged(), lane.gate.verdict(WEB, GENERATION), lane.switch("b")) == ([], NONE, committed("b"))


OLDER, OLD, NEXT = (f"0198c0de-0000-7000-8000-00000000000{number}" for number in (1, 2, 3))
# What an old request sent that had not run when the request was closed: where its lane's route was, and where it sends it.
LATE = {
    "a retire": ("a", None),
    "a maintenance begin": ("a", BACK_SOON),
    "a maintenance end": (BACK_SOON, "a"),
}
SENT_LATE = pytest.mark.parametrize(("was", "to"), LATE.values(), ids=LATE.keys())


@SENT_LATE
def test_a_command_of_a_closed_request_that_arrives_once_the_lane_is_in_maintenance_leaves_the_same_file_showing_back_soon(
    lane: Lane, was: str, to: str | None
) -> None:
    lane.route.write_text(route_text(WEB, was))
    late = lane.sent_and_held(to, OLD)
    assert (lane.gate.fence(WEB, OLD, 1), lane.edit(BACK_SOON, NEXT)) == (NONE, DONE)
    in_maintenance = lane.as_it_is()
    assert (lane.arrive(late), lane.as_it_is()) == (LOST, in_maintenance)
    assert in_maintenance[0] == installed_text(WEB, BACK_SOON, NEXT, 1)


@SENT_LATE
def test_a_command_of_a_closed_request_changes_nothing_of_a_route_still_exactly_as_it_read_it(
    lane: Lane, was: str, to: str | None
) -> None:
    lane.route.write_text(route_text(WEB, was))
    late, before = lane.sent_and_held(to, OLD), lane.as_it_is()
    assert (lane.gate.fence(WEB, OLD, 1), lane.arrive(late), lane.as_it_is()) == (NONE, LOST, before)


def test_an_old_retire_that_runs_before_its_request_is_closed_removes_the_route_and_the_next_request_reads_that_and_decides_again(
    lane: Lane,
) -> None:
    lane.route.write_text(route_text(WEB, "a"))
    read_by_the_next = lane.installed()
    late = lane.sent_and_held(None, OLD)
    assert (lane.arrive(late), lane.gate.fence(WEB, OLD, 1), lane.route.exists()) == (DONE, NONE, False)
    assert lane.edit(BACK_SOON, NEXT, read=read_by_the_next) == LOST
    assert (lane.edit(BACK_SOON, NEXT), lane.installed()) == (DONE, installed_text(WEB, BACK_SOON, NEXT, 1))


def test_a_route_put_back_to_the_same_instance_is_not_the_route_an_older_command_read_though_no_fence_stops_it(lane: Lane) -> None:
    assert lane.edit("a", OLDER) == DONE
    late = lane.sent_and_held(BACK_SOON, OLD)
    assert (lane.edit(BACK_SOON, NEXT), lane.edit("a", NEXT, attempt=2)) == (DONE, DONE)
    back_on_a = lane.as_it_is()
    assert (lane.arrive(late), lane.as_it_is()) == (LOST, back_on_a)


def test_an_edit_sent_again_after_it_was_made_answers_already_and_puts_no_other_file_in_place(lane: Lane) -> None:
    lane.route.write_text(route_text(WEB, "a"))
    read = lane.installed()
    assert lane.edit(BACK_SOON, NEXT, read=read) == DONE
    made = lane.as_it_is()
    assert (lane.edit(BACK_SOON, NEXT, read=read), lane.edit(None, OLD, read=""), lane.as_it_is()) == (ALREADY, LOST, made)


def test_an_attempt_later_than_the_fence_edits_and_the_fenced_one_never_does(lane: Lane) -> None:
    lane.route.write_text(route_text(WEB, "a"))
    assert lane.gate.fence(WEB, NEXT, 1) == NONE
    assert (lane.edit(None, NEXT, attempt=1), lane.edit(None, NEXT, attempt=2), lane.route.exists()) == (LOST, DONE, False)


def test_an_edit_takes_nothing_from_a_release_staged_and_armed_on_the_lane_and_keeps_no_verdict_of_its_own(lane: Lane) -> None:
    gate_folder = lane.host.folder / GATE_FOLDER_NAME
    gate_folder.mkdir()
    for kind, text in ((".b.next", "to b"), (".armed", "1 b")):
        (gate_folder / f"{WEB}.{GENERATION}{kind}").write_text(text)
    assert (lane.edit(BACK_SOON, NEXT), lane.edit(None, NEXT, attempt=2)) == (DONE, DONE)
    assert (lane.staged(), lane.gate.verdict(WEB, NEXT), lane.gate.verdict(WEB, GENERATION)) == (
        [f"{WEB}.{GENERATION}.b.next"],
        NONE,
        NONE,
    )
    assert (lane.switch("b"), lane.route.read_text()) == (committed("b"), "to b")


def test_an_edit_of_a_route_that_cannot_be_looked_for_gives_no_answer_and_is_never_taken_as_no_route(lane: Lane) -> None:
    lane.route.write_text(route_text(WEB, "a"))
    lane.route.parent.chmod(0o000)
    try:
        with pytest.raises(GateError, match="exit code 9"):
            lane.edit(BACK_SOON, NEXT, read="")
    finally:
        lane.route.parent.chmod(0o755)
    assert lane.installed() == route_text(WEB, "a")


def test_an_edit_of_a_route_that_is_a_folder_is_refused_and_nothing_is_put_in_place(lane: Lane) -> None:
    lane.route.mkdir()
    with pytest.raises(GateError, match="is not a file"):
        lane.edit(BACK_SOON, NEXT, read="")
    assert lane.route.is_dir()
