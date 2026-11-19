import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest

from launchplane.docker_answers import CommandResult
from launchplane.gate import GATE_FOLDER_NAME, Answer, Gate, GateError
from launchplane.instance import Verdict
from tests.pretend_host import EARLIER, GENERATION

WEB = "testing.site.test"
NONE, ABANDONED = Answer(Verdict.NONE), Answer(Verdict.ABANDONED)


def committed(candidate: str) -> Answer:
    return Answer(Verdict.COMMITTED, candidate)


# The gate's command run as it is, by a real shell, on a folder that stands for the proxy's routes folder. The lock is
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


@pytest.fixture
def lane(tmp_path: Path) -> Lane:
    host = FolderHost(tmp_path)
    return Lane(host, Gate(host, "proxy", PurePosixPath(tmp_path)), tmp_path / f"{WEB}.yml")


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
