import json

import pytest
from requests import ReadTimeout

from launchplane.container_host import ContainerHost
from launchplane.data_set import RULES_AS_READ, login_of
from launchplane.docker_answers import ContainerHostError, ContainerHostRefused, LoginRemovalRefused
from launchplane.instance import LaneBuild, Unread
from launchplane.page import line_of, lines_of
from launchplane.record import Asked, Event, Interrupted, Record, Sent, Stands
from launchplane.steps import Command, MoveResult, Outcome
from launchplane.telling import NOBODY, Answered, Said, Witness
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS
from tests.pretend_host import WEB_ADDRESS, LanesNeverAsked, LaunchplaneKilled
from tests.recording_docker import ExecResult, RecordingContainer, RecordingDocker
from tests.test_act import BUILD_B, lane_brought_up_with_a
from tests.test_command_lines import Told, killed_before_it_answered, killed_once_it_wrote
from tests.test_container_host import DATABASE, DROP_REFUSED, LINK, ROLES, TARGET, WEB, database_made, given_to, holding_a_login
from tests.test_host_answers import DATABASE as SERVER
from tests.test_host_answers import MOVE_A, RETIRE, worker_on
from tests.test_worker import CALLER, MOVE_B, kept_of

RULES = ExecResult(0, json.dumps(RULES_AS_READ).encode())
LEFT, LEFT_TOO = f"remove the login i_left from {DATABASE}", f"remove the login i_left_too from {DATABASE}"
MADE = f"make the login i_aaaaaaaaaaaa on {DATABASE}"


# A server with the login an instance holds and two no container holds, and what it answers from here on.
def server_with_two_logins_left(told: Told, *answers: ExecResult) -> RecordingContainer:
    database_made(told.docker)
    holding_a_login(told.docker, "shop-testing-holder")
    server = told.docker.containers.by_name[DATABASE]
    server.answers = list(answers)
    return server


def statements(server: RecordingContainer, since: int = 0) -> list[str]:
    said = {"pg_hba_file_rules": "read rules", "pg_auth_members": "read logins", "DROP ROLE": "remove", "CREATE ROLE": "make"}
    sent = [(call, next(word for mark, word in said.items() if mark in call)) for call in server.calls[since:] if "psql" in call]
    return [word if word.startswith("read") else f"{word} {call.split('--set=login=')[1].split()[0]}" for call, word in sent]


def test_starting_an_instance_asks_the_server_its_rules_and_logins_and_removes_what_none_holds_before_it_makes_its_own() -> None:
    docker = RecordingDocker()
    host = database_made(docker)
    holding_a_login(docker, "shop-testing-holder")
    server = docker.containers.by_name[DATABASE]
    server.answers, asked_before = [RULES, ROLES, ExecResult(0), ExecResult(0), ExecResult(0)], len(server.calls)
    host.start_instance(TARGET, WEB, LINK)
    assert statements(server, asked_before) == [
        "read rules",
        "read logins",
        "remove i_left",
        "remove i_left_too",
        "make i_aaaaaaaaaaaa",
    ]
    assert docker.containers.by_name[WEB].calls == ["start"]


@pytest.mark.parametrize(("asked", "outcome"), [(MOVE_A, "repaired"), (RETIRE, "retired")], ids=["a move", "a retire"])
def test_a_request_that_changed_nothing_but_a_login_no_container_held_does_not_answer_unchanged(
    record: Record, asked: Asked, outcome: str
) -> None:
    proxy = lane_brought_up_with_a()
    if asked is RETIRE:
        record.write(RETIRE, CALLER, None)
        assert worker_on(proxy, record).work_next() is True
    proxy.host.change_database(SERVER, unheld_logins=("i_left",))
    request = record.write(asked, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    assert kept.answer is not None and (kept.stands, kept.answer["outcome"]) == (Stands.DONE, outcome)
    unheld = [each.unheld_logins for each in proxy.host.data_sets["data-1"].databases]
    assert (kept.answer.get("retired_containers", []), unheld) == ([], [()])


def test_two_logins_no_container_holds_are_removed_and_the_second_is_refused_so_the_first_is_done_and_the_second_refused() -> None:
    told = Told()
    server = server_with_two_logins_left(told, ROLES, ExecResult(0), DROP_REFUSED, ExecResult(0))
    host, asked_before = database_host(told), len(server.calls)
    with pytest.raises(LoginRemovalRefused):
        host.remove_unheld_logins(DATABASE, told.witness())
    assert told.lines == [(LEFT, Said.CHANGED), (LEFT_TOO, Said.REFUSED)]
    assert told.credited == [Command(verb="remove", container=DATABASE, login="i_left")]
    assert told.errors == {LEFT: None, LEFT_TOO: 'ERROR:  role "i_left_too" cannot be dropped because some objects depend on it'}
    assert statements(server, asked_before) == ["read logins", "remove i_left", "remove i_left_too"]


def database_host(told: Told) -> ContainerHost:
    return ContainerHost(told.docker.as_client(), "test")


def test_a_removal_the_server_does_not_answer_is_neither_done_nor_refused_and_earns_no_credit() -> None:
    told = Told()
    server_with_two_logins_left(told, ROLES, ExecResult(2, b"psql: connection to server was lost"))
    with pytest.raises(ContainerHostError) as failed:
        database_host(told).remove_unheld_logins(DATABASE, told.witness())
    assert not isinstance(failed.value, ContainerHostRefused)
    assert (told.lines, told.credited) == ([(LEFT, None)], [])
    assert told.errors == {LEFT: "psql: connection to server was lost"}


def test_a_statement_docker_never_answers_for_leaves_its_line_with_no_answer_and_the_real_error() -> None:
    told = Told()
    server = server_with_two_logins_left(told, ROLES)
    run = server.exec_run

    def time_out_on_a_removal(command: list[str]) -> ExecResult:
        if "DROP ROLE" in " ".join(command):
            raise ReadTimeout("no answer within 5 s")
        return run(command)

    server.exec_run = time_out_on_a_removal
    with pytest.raises(ContainerHostError):
        database_host(told).remove_unheld_logins(DATABASE, told.witness())
    assert (told.lines, told.errors, told.credited) == ([(LEFT, None)], {LEFT: "no answer within 5 s"}, [])


def test_a_removal_of_a_login_the_server_no_longer_has_when_it_is_asked_sends_nothing_and_earns_no_credit() -> None:
    told = Told()
    server = server_with_two_logins_left(told, ExecResult(0, b'["i_aaaaaaaaaaaa"]'))
    asked_before = len(server.calls)
    assert database_host(told).remove_unheld_logins(DATABASE, told.witness()) is None
    assert (told.lines, told.credited, statements(server, asked_before)) == ([], [], ["read logins"])


def test_logins_that_could_not_be_read_have_no_line_since_nothing_was_chosen_to_be_removed() -> None:
    told = Told()
    server_with_two_logins_left(told, ExecResult(2, b"psql: could not connect"))
    assert isinstance(database_host(told).remove_unheld_logins(DATABASE, told.witness()), Unread)
    assert told.lines == []


def test_each_login_is_sent_to_the_server_only_once_its_line_is_written_and_the_line_before_it_is_answered() -> None:
    told = Told()
    server = server_with_two_logins_left(told, RULES, ROLES, ExecResult(0), ExecResult(0), ExecResult(0))
    asked_before, sent_when_written = len(server.calls), list[tuple[list[str], list[tuple[str, Said | str | None]]]]()
    witness = told.witness()
    write = witness.write

    def written_and_looked(did: str, command: Sent | Command) -> Answered:
        sent_when_written.append((statements(server, asked_before), list(told.lines)))
        return write(did, command)

    witness.write = written_and_looked
    database_host(told).start_instance(TARGET, WEB, LINK, None, witness)
    assert [did for did, _ in told.lines] == [LEFT, LEFT_TOO, MADE, f"make {WEB}", f"start {WEB}"]
    assert sent_when_written[:3] == [
        (["read rules", "read logins"], []),
        (["read rules", "read logins", "remove i_left"], [(LEFT, Said.CHANGED)]),
        (["read rules", "read logins", "remove i_left", "remove i_left_too"], [(LEFT, Said.CHANGED), (LEFT_TOO, Said.CHANGED)]),
    ]
    assert told.credited[:3] == [
        Command(verb="remove", container=DATABASE, login="i_left"),
        Command(verb="remove", container=DATABASE, login="i_left_too"),
        Command(verb="make", container=DATABASE, login="i_aaaaaaaaaaaa"),
    ]


def test_a_login_whose_line_cannot_be_written_is_neither_made_nor_removed() -> None:
    def not_written(did: str, _command: Sent | Command) -> Answered:
        raise Interrupted(f"The attempt has ended in the record, so it was not written and not done: {did}")

    told = Told()
    server = server_with_two_logins_left(told, ROLES, RULES, ExecResult(0, b'["i_aaaaaaaaaaaa"]'))
    asked_before = len(server.calls)
    with pytest.raises(Interrupted):
        database_host(told).remove_unheld_logins(DATABASE, Witness(write=not_written))
    with pytest.raises(Interrupted):
        database_host(told).start_instance(TARGET, WEB, LINK, None, Witness(write=not_written))
    assert statements(server, asked_before) == ["read logins", "read rules", "read logins"]
    assert WEB not in told.docker.containers.by_name


def test_a_making_the_server_refuses_is_refused_on_its_line_in_the_servers_words_and_the_instance_is_not_made() -> None:
    told = Told()
    exists = 'ERROR:  role "i_aaaaaaaaaaaa" already exists'
    server_with_two_logins_left(told, RULES, ExecResult(0, b'["i_aaaaaaaaaaaa"]'), ExecResult(3, exists.encode()))
    with pytest.raises(ContainerHostError, match="Making the login i_aaaaaaaaaaaa in the database"):
        database_host(told).start_instance(TARGET, WEB, LINK, None, told.witness())
    assert (told.lines, told.errors, told.credited) == ([(MADE, Said.REFUSED)], {MADE: exists}, [])
    assert WEB not in told.docker.containers.by_name


def test_no_line_of_a_login_holds_a_statement_a_verifier_a_password_or_anything_else_the_instance_is_given() -> None:
    told = Told()
    server = server_with_two_logins_left(told, RULES, ROLES, ExecResult(0), ExecResult(0), ExecResult(0))
    database_host(told).start_instance(TARGET, WEB, LINK, None, told.witness())
    given, run_in_the_server = given_to(told.docker, WEB), " ".join(server.calls)
    verifier = run_in_the_server.split("--set=verifier=")[1].split()[0]
    written = " ".join(f"{did} {told.errors[did]}" for did, _ in told.lines)
    kept_out = [verifier, given["PGPASSWORD"], "ROLE", "SELECT", "psql"]
    assert verifier.startswith("SCRAM-SHA-256$") and [each for each in kept_out if each in written] == []


def test_a_request_shows_the_first_of_two_removals_as_done_and_the_second_as_the_server_refused_it(record: Record) -> None:
    proxy = lane_brought_up_with_a()
    proxy.host.change_database(SERVER, unheld_logins=("i_left", "i_left_too"))
    proxy.host.removals_refused.add("i_left_too")
    request = record.write(MOVE_A, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    first, second = f"remove the login i_left from {SERVER}", f"remove the login i_left_too from {SERVER}"
    assert (kept.stands, lines_of(kept)) == (Stands.FAILED, [f"{first}: done", f"{second}: refused"])
    assert (kept.events[-1].error, proxy.host.unheld_logins(SERVER)) == ("it owns objects", ["i_left_too"])
    assert (kept.error or "").endswith(f"it owns objects Of its commands for the lane's parts, {first} was done.")


def logins_made(kept_events: list[Event]) -> list[Event]:
    return [each for each in kept_events if each.command is not None and each.did.startswith("make the login ")]


def test_a_making_whose_line_was_written_and_that_was_never_sent_is_not_credited_and_the_next_attempt_makes_the_login_once(
    record: Record, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = lane_brought_up_with_a()
    sent_before = len(proxy.host.logins_sent)
    request = record.write(MOVE_B, CALLER, None)
    killed_once_it_wrote(monkeypatch, record, lambda did: did.startswith("make the login "))
    with pytest.raises(LaunchplaneKilled):
        worker_on(proxy, record).work_next()
    assert proxy.host.logins_sent[sent_before:] == []
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    never_sent, sent = logins_made(kept.events)
    assert kept.answer is not None and never_sent.command == sent.command and sent.command is not None
    assert (kept.stands, proxy.host.logins_sent[sent_before:]) == (Stands.DONE, [f"make {sent.command.login}"])
    assert (never_sent.attempt, never_sent.is_own, sent.attempt, sent.is_own) == (1, False, 2, True)
    assert kept.answer["did"].count(sent.did) == 1 and kept.answer["not_answered"] == [
        f"{sent.did} may have been sent, and Docker did not answer"
    ]
    assert line_of(never_sent, attempt_is_open=False) == f"{sent.did}: may have been sent, no answer"
    assert line_of(sent, attempt_is_open=False) == f"{sent.did}: done"


@pytest.mark.parametrize("carries_the_generation", [True, False], ids=["of this request", "of another release"])
def test_a_making_with_no_answer_is_the_requests_own_only_where_an_instance_of_its_generation_has_the_name_the_login_is_of(
    record: Record, monkeypatch: pytest.MonkeyPatch, carries_the_generation: bool
) -> None:
    proxy = lane_brought_up_with_a()
    request = record.write(MOVE_B, CALLER, None)
    killed_before_it_answered(monkeypatch, record, lambda did: did.startswith("make the login "))
    with pytest.raises(LaunchplaneKilled):
        worker_on(proxy, record).work_next()
    [unanswered] = logins_made(kept_of(record, request.id).events)
    assert unanswered.command is not None and unanswered.command.login is not None
    instance = f"site-testing-{unanswered.command.login.removeprefix('i_')}"
    proxy.host.place(BUILD_B, instance, generation=request.generation if carries_the_generation else "another")
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    read_back = kept.events[0]
    assert (kept.stands, read_back.did, read_back.is_own) == (Stands.DONE, unanswered.did, carries_the_generation)
    assert line_of(read_back, attempt_is_open=False) == unanswered.did + (
        ": may have been sent, no answer, then read as made by this request"
        if carries_the_generation
        else ": may have been sent, no answer"
    )


def test_a_removal_of_a_login_that_is_gone_by_the_time_the_server_is_asked_earns_the_request_no_credit(record: Record) -> None:
    proxy = lane_brought_up_with_a()
    proxy.host.change_database(SERVER, unheld_logins=("i_left",))
    remove = proxy.host.remove_unheld_logins

    def gone_by_now(database: str, by: Witness = NOBODY) -> Unread | None:
        proxy.host.change_database(database, unheld_logins=())
        return remove(database, by)

    proxy.host.remove_unheld_logins = gone_by_now
    request = record.write(MOVE_A, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    assert kept.answer is not None and proxy.host.actions == [f"remove logins from {SERVER}"]
    assert (kept.stands, kept.answer["outcome"], kept.answer["did"], lines_of(kept)) == (Stands.DONE, "unchanged", [], [])


def test_a_move_whose_new_build_fails_its_check_lists_the_login_its_clean_up_removed_as_its_card_does(record: Record) -> None:
    proxy = lane_brought_up_with_a()
    proxy.failing, retire = {f"site-testing-{number}" for number in range(9)}, proxy.host.retire_instance

    # As the server shows it: the login of an instance that has gone is held by no container.
    def retire_and_leave_its_login(container: str, by: Witness = NOBODY) -> None:
        retire(container, by)
        proxy.host.change_database(SERVER, unheld_logins=(login_of(container),))

    proxy.host.retire_instance = retire_and_leave_its_login
    request = record.write(MOVE_B, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    assert kept.answer is not None and (kept.stands, kept.answer["outcome"]) == (Stands.FAILED, "check failed")
    assert kept.answer["did"][-1] == f"remove the login i_0 from {SERVER}"
    assert [f"{each}: done" for each in kept.answer["did"]] == [each for each in lines_of(kept) if each.endswith(": done")]


# A move that answers a failure and leaves what it made to be cleaned up: the container is removed, and then its login.
class FailedAndCleanedUp(LanesNeverAsked):
    def move(self, target: LaneBuild, generation: str, attempt: int, witness: Witness, /) -> MoveResult:
        assert generation and attempt and witness
        failed = {"web_address": WEB_ADDRESS, "outcome": Outcome.CHECK_FAILED, "container_receiving_visitors": "a"}
        return MoveResult.model_validate(target.model_dump() | failed | {"error": "Connection refused"})

    def clean_up(self, product: str, lane: str, generation: str, attempt: int, witness: Witness, /, *, logins: bool) -> list[str]:
        assert product and lane and generation and attempt and logins
        witness.sending("remove", "new")(Said.CHANGED, None)
        witness.sending("remove", SERVER, "i_new")(Said.CHANGED, None)
        return []


def test_the_account_of_a_failed_move_is_built_after_its_clean_up_so_it_has_the_container_and_the_login_that_removed(
    record: Record,
) -> None:
    request = record.write(MOVE_B, CALLER, None)
    assert Worker(record, FailedAndCleanedUp(), NO_SECRETS).work_next() is True
    kept = kept_of(record, request.id)
    assert kept.answer is not None and (kept.stands, kept.error) == (Stands.FAILED, "Connection refused")
    assert (kept.answer["outcome"], kept.answer["did"]) == ("check failed", ["remove new", f"remove the login i_new from {SERVER}"])
