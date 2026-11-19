import shlex
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from launchplane.docker_answers import CommandResult, ContainerHostError
from launchplane.gate import GATE_FOLDER_NAME, Answer, Gate, GateError
from launchplane.instance import Verdict, new_generation
from launchplane.proxy import INSTANCE_URL, PROXY_NAME, ROUTES_FOLDER, route_file_of, route_text
from tests.bed import SCRATCH_SHELL, needs_test_bed, on_scratch, wait_for

pytestmark = needs_test_bed

# The gate alone, on the real container host and the real proxy: its own commands, sent as Launchplane sends them, with
# no worker, planner or record. A web address no lane has.
WEB = "proof.gate-proof.test"
ROUTE = route_file_of(WEB)
GATE_FOLDER = ROUTES_FOLDER / GATE_FOLDER_NAME
NONE, ABANDONED = Answer(Verdict.NONE), Answer(Verdict.ABANDONED)
HELD_BIN = "/tmp/gate-proof-bin"  # noqa: S108 a folder inside the proxy's container
AFTER_THE_RENAME, HOLD = "/tmp/gate-proof-after-the-rename", "/tmp/gate-proof-hold"  # noqa: S108
LOCK_HELD = "/tmp/gate-proof-lock-held"  # noqa: S108
# The first thing the gate's command does after the rename is make the routes file durable. Held there, the rename has
# happened and nothing after it has.
SYNC_HELD_AFTER_THE_RENAME = f"""#!/bin/sh
case "$1" in *.yml) touch {AFTER_THE_RENAME}; while [ -e {HOLD} ]; do sleep 0.1; done ;; esac
exec /bin/busybox sync "$@"
"""


def committed(candidate: str) -> Answer:
    return Answer(Verdict.COMMITTED, candidate)


def in_proxy(command: str, flags: str = "") -> str:
    return on_scratch(f"docker exec {flags} {PROXY_NAME} sh -c {shlex.quote(command)}")


# The commands go to Docker on the scratch host as they are. A reply can be lost after the command has run, and a command
# can be run with a held `sync` first on its path.
@dataclass
class ScratchHost:
    loses_replies: int = 0
    held_after_the_rename: bool = False

    def run_in(self, container: str, command: list[str]) -> CommandResult:
        path = (
            f"-e PATH={HELD_BIN}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin " if self.held_after_the_rename else ""
        )
        sent = f"docker exec {path}{container} {shlex.join(command)}"
        ran = subprocess.run([*SCRATCH_SHELL, sent], capture_output=True, text=True, check=False)  # noqa: S603
        if self.loses_replies:
            self.loses_replies -= 1
            raise ContainerHostError("Running flock in the proxy failed: the connection was lost before the reply")
        return CommandResult(ran.returncode, (ran.stdout or ran.stderr).strip())


def route_inode() -> str:
    return in_proxy(f"stat -c %i {ROUTE} 2>/dev/null || echo none").strip()


def routed_to() -> str:
    found = INSTANCE_URL.search(in_proxy(f"cat {ROUTE} 2>/dev/null || true"))
    return "" if found is None else found["container"]


@dataclass
class Proof:
    host: ScratchHost
    gate: Gate
    generation: str
    inodes: list[str] = field(default_factory=list[str])
    answers: list[Answer | Exception] = field(default_factory=list[Answer | Exception])

    def switch(self, candidate: str, attempt: int = 1, generation: str | None = None) -> Answer:
        made_by = generation or self.generation
        try:
            return self.gate.switch(WEB, made_by, attempt, candidate, route_text(WEB, candidate), ROUTE)
        finally:
            self.look()

    def switch_and_keep_the_answer(self, candidate: str) -> None:
        try:
            self.answers.append(self.switch(candidate))
        except (GateError, ContainerHostError) as error:
            self.answers.append(error)

    # The routes file is a new file after each rename into place, so each rename on the host shows as a new inode.
    def look(self) -> None:
        inode = route_inode()
        if self.inodes[-1:] != [inode]:
            self.inodes.append(inode)

    def renames(self) -> int:
        self.look()
        return len(self.inodes) - 1

    def evidence(self, generation: str | None = None) -> list[str]:
        prefix = f"{WEB}.{generation or self.generation}."
        return sorted(name.removeprefix(prefix) for name in in_proxy(f"ls {GATE_FOLDER}").split() if name.startswith(prefix))

    def processes_of_its_commands(self) -> set[str]:
        found = in_proxy(f"grep -l {self.generation} /proc/[0-9]*/cmdline 2>/dev/null || true").split()
        return {path.split("/")[2] for path in found}

    def waiting_for_the_lock(self) -> set[str]:
        flocks = {path.split("/")[2] for path in in_proxy("grep -l flock /proc/[0-9]*/comm 2>/dev/null || true").split()}
        return flocks & self.processes_of_its_commands()


@pytest.fixture
def proof() -> Iterator[Proof]:
    clean = f"rm -rf {HELD_BIN} {AFTER_THE_RENAME} {HOLD} {LOCK_HELD} {ROUTE}; rm -f {GATE_FOLDER}/{WEB}.*"
    in_proxy(clean)
    host = ScratchHost()
    made = Proof(host, Gate(host, PROXY_NAME, ROUTES_FOLDER), new_generation())
    made.look()
    yield made
    in_proxy(clean)


def test_a_rename_whose_host_command_is_killed_just_after_it_is_committed_with_nothing_written_after_the_rename(
    proof: Proof,
) -> None:
    place = f"mkdir -p {HELD_BIN} && cat > {HELD_BIN}/sync && chmod +x {HELD_BIN}/sync && touch {HOLD}"
    on_scratch(f"docker exec -i {PROXY_NAME} sh -c {shlex.quote(place)}", given=SYNC_HELD_AFTER_THE_RENAME)
    proof.host.held_after_the_rename = True
    sender = threading.Thread(target=proof.switch_and_keep_the_answer, args=("b",))
    sender.start()
    wait_for(lambda: in_proxy(f"ls {AFTER_THE_RENAME} 2>/dev/null || true") != "", seconds=30)
    in_proxy(f"kill -9 {' '.join(sorted(proof.processes_of_its_commands()))} 2>/dev/null; rm -f {HOLD}")
    sender.join(timeout=30)
    proof.host.held_after_the_rename = False
    assert [type(answer) for answer in proof.answers] == [GateError], "the killed command gave its sender no verdict"
    assert (proof.gate.verdict(WEB, proof.generation), proof.evidence()) == (committed("b"), ["armed"])
    assert (proof.switch("b"), proof.switch("c", attempt=2), proof.renames(), routed_to()) == (
        committed("b"),
        committed("b"),
        1,
        "b",
    )


def test_a_rename_whose_reply_alone_is_lost_is_committed_when_asked_again_and_happened_once_on_the_host(proof: Proof) -> None:
    proof.host.loses_replies = 1
    with pytest.raises(ContainerHostError):
        proof.switch("b")
    asked_again = [proof.gate.verdict(WEB, proof.generation), proof.switch("b"), proof.switch("c", attempt=2)]
    assert (asked_again, proof.renames(), routed_to(), proof.evidence()) == ([committed("b")] * 3, 1, "b", ["armed"])


def test_a_switch_accepted_and_held_before_the_lock_renames_nothing_once_its_candidate_was_discarded_meanwhile(
    proof: Proof,
) -> None:
    lock = ROUTES_FOLDER
    in_proxy(f"touch {LOCK_HELD}; flock {lock} sh -c 'while [ -e {LOCK_HELD} ]; do sleep 0.1; done'", flags="--detach")
    # The held switch is another sender's: one Launchplane sends its gate commands one at a time.
    stale_sender = Proof(proof.host, Gate(proof.host, PROXY_NAME, ROUTES_FOLDER), proof.generation, proof.inodes, proof.answers)
    sender = threading.Thread(target=stale_sender.switch_and_keep_the_answer, args=("b",))
    sender.start()
    wait_for(lambda: bool(proof.waiting_for_the_lock()), seconds=30)
    [stale] = proof.waiting_for_the_lock()
    in_proxy(f"kill -STOP {stale}; rm -f {LOCK_HELD}")
    discarded = proof.gate.discard(WEB, proof.generation, 1, "b")
    in_proxy(f"kill -CONT {stale}")
    sender.join(timeout=30)
    assert (discarded, proof.answers, proof.renames(), proof.evidence()) == (NONE, [NONE], 0, ["b.revoked"])
    assert (proof.switch("b", attempt=2), proof.switch("b2", attempt=2), proof.renames(), routed_to()) == (
        NONE,
        committed("b2"),
        1,
        "b2",
    )


def test_a_cancel_that_wins_is_answered_abandoned_however_often_it_and_the_switch_are_sent_again_and_nothing_renames(
    proof: Proof,
) -> None:
    asked = [proof.gate.abandon(WEB, proof.generation), proof.switch("b")]
    again = [proof.gate.abandon(WEB, proof.generation), proof.switch("b"), proof.switch("c", attempt=2)]
    assert (asked, again, proof.gate.verdict(WEB, proof.generation)) == ([ABANDONED] * 2, [ABANDONED] * 3, ABANDONED)
    assert (proof.renames(), proof.evidence()) == (0, ["abandoned"])


def test_nothing_done_for_one_generation_of_a_lane_changes_what_the_other_is_answered(proof: Proof) -> None:
    earlier, later = proof.generation, new_generation()
    assert (proof.switch("b"), proof.gate.abandon(WEB, later)) == (committed("b"), ABANDONED)
    sent_again = [
        proof.gate.abandon(WEB, earlier),
        proof.switch("c", generation=later),
        proof.switch("b"),
        proof.gate.abandon(WEB, later),
    ]
    assert sent_again == [committed("b"), ABANDONED, committed("b"), ABANDONED]
    assert (proof.gate.verdict(WEB, earlier), proof.gate.verdict(WEB, later)) == (committed("b"), ABANDONED)
    assert (proof.renames(), routed_to(), proof.evidence(earlier), proof.evidence(later)) == (1, "b", ["armed"], ["abandoned"])
