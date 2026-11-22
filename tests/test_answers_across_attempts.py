import pytest

from launchplane.instance import OnHost
from launchplane.record import Record, Stands
from tests.pretend_host import LaunchplaneKilled
from tests.pretend_record import begun
from tests.test_act import lane_brought_up_with_a
from tests.test_host_answers import MOVE_A, RETIRE, worker_on
from tests.test_unreadable_host import lose_the_first_reply_to
from tests.test_worker import CALLER, kept_of

BY_HAND = "a call from the address 192.0.2.7"
DELETING = RETIRE.model_copy(update={"delete_data_set": "data-1"})


def test_a_retire_killed_once_it_had_deleted_its_named_data_set_and_before_it_ended_says_that_data_set_was_deleted(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()
    worker, read = worker_on(proxy, record), proxy.host.read

    def killed_once_deleted(lane: tuple[str, str] | None = None) -> OnHost:
        if "delete data-1" in proxy.host.actions:
            raise LaunchplaneKilled
        return read(lane)

    proxy.host.read = killed_once_deleted
    request = record.write(DELETING, CALLER, None)
    with pytest.raises(LaunchplaneKilled):
        worker.work_next()
    proxy.host.read = read
    assert worker.work_next() is True
    ended = kept_of(record, request.id)
    assert ended.stands is Stands.DONE and ended.answer is not None
    assert ([each.ended for each in ended.attempts], ended.answer["outcome"], ended.answer["deleted_data_set"]) == (
        ["interrupted", "done"],
        "deleted",
        "data-1",
    )


@pytest.mark.parametrize("interrupted", [False, True], ids=["on its first attempt", "interrupted before its first action"])
def test_a_retire_that_names_a_data_set_the_lane_never_had_is_refused_naming_it_however_many_attempts_it_took(
    record: Record, interrupted: bool
) -> None:
    proxy = lane_brought_up_with_a()
    request = record.write(RETIRE.model_copy(update={"delete_data_set": "no-such-set"}), CALLER, None)
    if interrupted:
        assert begun(record, request.id) > 0
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    assert (kept.stands, kept.answer, list(proxy.host.data_sets)) == (Stands.FAILED, None, ["data-1"])
    assert "named the data set no-such-set, which the lane does not have, to delete. Nothing was deleted." in (kept.error or "")


def test_a_cancel_that_comes_once_a_retire_has_deleted_its_named_data_set_does_not_say_that_data_set_is_kept(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()
    lose_the_first_reply_to(proxy.host, "delete_data_set")
    worker, request = worker_on(proxy, record), record.write(DELETING, CALLER, None)
    assert worker.work_next() is True
    record.cancel(request.id, BY_HAND)
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert kept.stands is Stands.DONE and kept.answer is not None and kept.cancel_refused is not None
    assert (kept.answer["outcome"], kept.answer["deleted_data_set"], kept.answer["kept_data_sets"]) == ("deleted", "data-1", [])
    assert "is kept" not in kept.cancel_refused


def test_a_move_taken_up_again_that_only_waited_and_checked_answers_unchanged(record: Record) -> None:
    proxy = lane_brought_up_with_a()
    proxy.loaded.clear()
    proxy.holds_delivery = True
    worker, request = worker_on(proxy, record), record.write(MOVE_A, CALLER, None)
    assert worker.work_next() is True
    proxy.holds_delivery = False
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert kept.answer is not None
    assert ([each.ended for each in kept.attempts], kept.answer["outcome"], kept.answer["did"]) == (
        ["unfinished", "done"],
        "unchanged",
        [],
    )
