import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Protocol

from launchplane.docker_answers import CommandResult, ContainerHostError, observing
from launchplane.instance import Verdict

# The gate: the one place that answers, for a generation, none, committed or abandoned, for good.
#
# Everything that settles a release is one command on the container host, run while it holds a lock on the folder the
# routes folder is in. Its decisions are files named for the generation, in a folder beside it that retiring a lane keeps:
#
#   <web address>.<generation>.<candidate>.next     the candidate's routes file, staged
#   <web address>.<generation>.armed                "<attempt> <candidate>": the one candidate allowed to be renamed
#   <web address>.<generation>.<candidate>.revoked  the candidate may never be armed
#   <web address>.<generation>.fenced               "<attempt>": no attempt up to this one may arm
#   <web address>.<generation>.abandoned            a cancel won
#
# The staged file is finished and made durable, then the mark that arms it, and then the routes file is put in place by
# renaming the staged file. While a generation is armed, only that rename takes its staged file away. So armed with the
# staged file there is none, and armed with the staged file gone is committed to that candidate: the rename proves
# itself, with no write after it and no reply needed. Whatever takes permission away does it in this order: the
# revocation is kept first, then the arming mark goes, then the staged file. A command cut short and sent again goes in
# the same order, and at no moment is a generation armed with its staged file gone unless it was renamed.
#
# A file that cannot be looked for is never taken as absent: the command fails, and its sender has no answer.
GATE_FOLDER_NAME = "gate"
EDITS = DONE, ALREADY, LOST = "done", "already", "lost"
UNREADABLE = 9
NOT_A_FILE = 8
SCRIPT = f"""
set -eu
folder=$1 base=$1/$2.$3 operation=$4 route=${{8-}}
# Asking changes nothing: where nothing was ever kept, there is no verdict, and nothing is made to say so.
if [ ! -d "$folder" ]; then
    beside=$(ls -a "${{folder%/*}}") || exit {UNREADABLE}
    ! printf '%s\\n' "$beside" | grep -qxF "${{folder##*/}}" || exit {UNREADABLE}
    [ "$operation" != verdict ] || {{ echo none; exit 0; }}
    mkdir "$folder"
fi

has() {{
    [ ! -e "$1" ] || return 0
    names=$(ls -a "${{1%/*}}") || exit {UNREADABLE}
    ! printf '%s\\n' "$names" | grep -qxF "${{1##*/}}" || exit {UNREADABLE}
    return 1
}}

keep() {{
    printf '%s' "$2" > "$1.tmp"
    sync "$1.tmp"
    mv "$1.tmp" "$1"
    sync "$folder"
}}

armed() {{
    armed_attempt= armed_candidate=
    ! has "$base.armed" || read -r armed_attempt armed_candidate < "$base.armed" || [ -n "$armed_candidate" ]
}}

verdict() {{
    armed
    if has "$base.abandoned"; then echo abandoned
    elif [ -n "$armed_candidate" ] && ! has "$base.$armed_candidate.next"; then echo "committed $armed_candidate"
    else echo none
    fi
}}

revoke() {{
    keep "$base.$1.revoked" ""
    armed
    [ "$armed_candidate" != "$1" ] || rm "$base.armed"
    rm -f "$base.$1.next"
}}

fenced() {{
    has "$base.fenced" || return 1
    read -r through < "$base.fenced" || [ -n "$through" ]
    [ "$1" -le "$through" ]
}}

# Another candidate than the one armed is armed only by a later attempt.
may_arm() {{
    [ -z "$armed_candidate" ] || [ "$armed_candidate" = "$candidate" ] || [ "$attempt" -gt "$armed_attempt" ]
}}

now=$(verdict)
# A rename onto anything but a file would put the staged file somewhere else and replace no route.
[ ! -e "$route" ] || [ -f "$route" ] || {{ echo "$route is not a file" >&2; exit {NOT_A_FILE}; }}
case $operation in
switch)
    attempt=$5 candidate=$6 text=$7
    armed
    if [ "$now" = none ] && may_arm && ! has "$base.$candidate.revoked" && ! fenced "$attempt"; then
        [ -z "$armed_candidate" ] || [ "$armed_candidate" = "$candidate" ] || revoke "$armed_candidate"
        keep "$base.$candidate.next" "$text"
        keep "$base.armed" "$attempt $candidate"
        mv "$base.$candidate.next" "$route"
        sync "$route"
    fi ;;
abandon)
    [ "$now" != none ] || keep "$base.abandoned" ""
    # Sent again, it sweeps again: a stage is looked for by its name, not by a mark that may already be gone.
    if has "$base.abandoned"; then
        rm -f "$base.armed"
        rm -f "$base".*.next
        names=$(ls -a "$folder") || exit {UNREADABLE}
        for name in $names; do
            case $name in "${{base##*/}}".*.next) exit {UNREADABLE} ;; esac
        done
    fi ;;
discard)
    candidate=$6
    [ "$now" = "committed $candidate" ] || revoke "$candidate" ;;
edit)
    attempt=$5 read=$6 text=$7 installed=
    ! has "$route" || installed=$(cat "$route")
    if [ "$installed" = "$text" ]; then echo {ALREADY}
    elif fenced "$attempt" || [ "$installed" != "$read" ]; then echo {LOST}
    elif [ -z "$text" ]; then rm "$route"; sync "${{route%/*}}"; echo {DONE}
    else keep "$base.edit" "$text"; mv "$base.edit" "$route"; sync "$route"; echo {DONE}
    fi
    exit ;;
fence)
    attempt=$5
    if [ "$now" = none ]; then
        fenced "$attempt" || keep "$base.fenced" "$attempt"
        armed
        [ -z "$armed_candidate" ] || [ "$armed_attempt" -gt "$attempt" ] || revoke "$armed_candidate"
    fi ;;
esac
verdict
"""


# No answer from the gate. It is a failure to read the host, and never a verdict.
class GateError(ContainerHostError):
    pass


class GateHost(Protocol):
    def run_in(self, container: str, command: list[str]) -> CommandResult: ...


# What the gate answers for a generation. `candidate` is the instance a committed generation was renamed to.
@dataclass(frozen=True)
class Answer:
    verdict: Verdict
    candidate: str | None = None


def answer_from(output: str) -> Answer | None:
    words = (output.splitlines() or [""])[-1].split()
    if len(words) == 2 and words[0] == Verdict.COMMITTED:
        return Answer(Verdict.COMMITTED, words[1])
    return Answer(Verdict(words[0])) if len(words) == 1 and words[0] in (Verdict.ABANDONED, Verdict.NONE) else None


@dataclass(frozen=True)
class Gate:
    host: GateHost
    container: str
    root: PurePosixPath
    # Held with the routes folder's readers, so no reading of the routes folder finds a file half put in place.
    reading: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    @property
    def folder(self) -> PurePosixPath:
        return self.root / GATE_FOLDER_NAME

    # A command that does not come back with an answer has given none: it is never taken as none.
    def ask[T](self, told: Callable[[str], T | None], web_address: str, generation: str, *asked: str | int | PurePosixPath) -> T:
        passed = [str(self.folder), web_address, generation, *map(str, asked)]
        with self.reading:
            result = self.host.run_in(self.container, ["flock", str(self.root), "sh", "-c", SCRIPT, "sh", *passed])
        answer = told(result.output) if result.succeeded else None
        if answer is None:
            raise GateError(
                f"The gate gave no answer when asked to {asked[0]} for {web_address}, generation {generation}, so whether the route "
                f"was changed and what holds there is not known: exit code {result.exit_code}, {result.output or 'no output'}"
            )
        return answer

    # Changes nothing.
    @observing()
    def verdict(self, web_address: str, generation: str) -> Answer:
        return self.ask(answer_from, web_address, generation, "verdict")

    # Renames the routes file to the candidate's, once for a generation and only for a candidate no later attempt, no
    # discard, no fence and no cancel has taken the permission from. Asked again, it stages nothing.
    def switch(self, web_address: str, generation: str, attempt: int, candidate: str, text: str, route: PurePosixPath) -> Answer:
        return self.ask(answer_from, web_address, generation, "switch", attempt, candidate, text, route)

    def abandon(self, web_address: str, generation: str) -> Answer:
        return self.ask(answer_from, web_address, generation, "abandon")

    # Before a candidate is removed: either its rename won, and the answer says committed to it, or it can never be
    # renamed to from here on.
    def discard(self, web_address: str, generation: str, attempt: int, candidate: str) -> Answer:
        return self.ask(answer_from, web_address, generation, "discard", attempt, candidate)

    # Before an attempt proposes an ending that is no release: confirms none, and no switch of that attempt or an
    # earlier one, also one still on its way, can rename after it.
    def fence(self, web_address: str, generation: str, attempt: int) -> Answer:
        return self.ask(answer_from, web_address, generation, "fence", attempt)

    # Makes `text` the routes file, or with none removes it, only for an attempt not fenced and from the file exactly as `read`.
    def edit(self, web_address: str, generation: str, attempt: int, read: str, text: str, route: PurePosixPath) -> str:
        return self.ask(lambda said: said if said in EDITS else None, web_address, generation, "edit", attempt, read, text, route)
