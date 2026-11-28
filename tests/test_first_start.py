from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call

import pytest
from pydantic import ValidationError

from launchplane.container_host import ContainerHost, data_set_labels
from launchplane.docker_answers import PART_LABEL, CommandResult, ContainerHostError, ContainerHostRefused
from launchplane.page import kept_notes
from launchplane.plan import lane_data_set
from launchplane.product import Product
from launchplane.steps import Initialize, Stop
from launchplane.telling import NOBODY, Witness
from tests.bed_odoo import ADMIN_PASSWORD
from tests.odoo_product import ATTACHMENT_BYTES, TK, first_start_product
from tests.pretend_host import D1, EARLIER, GENERATION, Given, lanes_on
from tests.recording_docker import RecordingContainer, RecordingDocker
from tests.test_odoo_kind import BUILD_A, D1_WITH_FILES, OLD_WITH_FILES, SERVING


def test_a_description_can_supply_the_bytes_its_unattended_first_start_checks() -> None:
    product = first_start_product()
    assert product.odoo is not None and product.odoo.first_start is not None
    assert product.odoo.first_start.attachment == ATTACHMENT_BYTES
    with pytest.raises(ValidationError, match="SHA-256"):
        first_start_product(b"other bytes")


@pytest.mark.parametrize("product", [first_start_product(), TK, Product()], ids=["opt-in", "legacy", "website"])
def test_only_an_opted_in_first_move_initializes_before_web_and_repeats_leave_it_alone(product: Product) -> None:
    proxy = Given().proxy()
    lanes = lanes_on(proxy, product)
    result = lanes.move(BUILD_A, GENERATION, 1)
    actions = proxy.host.actions
    starts = [action for action in actions if action.startswith("initialize ")]
    assert starts == ([f"initialize {result.data_set}"] if product.odoo and product.odoo.first_start else [])
    if starts:
        assert actions.index(starts[0]) < actions.index(f"start {result.container_receiving_visitors}")
    actions.clear()
    lanes.read_status()
    assert lanes.move(BUILD_A, EARLIER, 1).container_receiving_visitors == result.container_receiving_visitors
    assert not actions


def test_factory_password_is_gone_when_the_attachment_and_completion_row_commit() -> None:
    odoo = first_start_product().odoo
    assert odoo is not None
    shell = odoo.initialize("one")[-1].split("<<'ODOO'\n")[1].removesuffix("ODOO\n")
    env = MagicMock()
    factory = SimpleNamespace(password=ADMIN_PASSWORD)
    attachment, parameters = Mock(), Mock()
    env.ref.return_value = factory
    env.__getitem__.side_effect = {"ir.attachment": attachment, "ir.config_parameter": parameters}.__getitem__

    def commit() -> None:
        assert factory.password != ADMIN_PASSWORD
        attachment.create.assert_called_once_with({"name": odoo.attachment, "raw": ATTACHMENT_BYTES})
        parameters.set_str.assert_called_once_with("launchplane.first_start", "one")

    env.cr.commit.side_effect = commit
    exec(shell, {"env": env})  # noqa: S102 the actual Odoo shell input
    env.cr.commit.assert_called_once()
    env.ref.assert_called_with("base.user_admin")


@pytest.mark.parametrize("state", ["running", "exited"])
@pytest.mark.parametrize("committed", [True, False])
@pytest.mark.parametrize("cancel", [True, False])
def test_an_interrupted_writer_never_rejoins_and_only_its_commit_keeps_the_pair(
    state: str, committed: bool, cancel: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = ContainerHost(RecordingDocker().as_client(), "bed")
    writer = RecordingContainer("old", data_set_labels("site", "testing", "d1") | {PART_LABEL: "initializer"}, [], {}, status=state)
    monkeypatch.setattr(host, "find_container", Mock(return_value=writer))
    monkeypatch.setattr(host, "await_database", Mock())

    def read_commit(_database: str, _command: list[str]) -> CommandResult:
        assert writer.status == "exited"
        return CommandResult(0, "t" if committed else "f")

    monkeypatch.setattr(host, "run_in", read_commit)
    start, aside = Mock(), Mock()
    monkeypatch.setattr(host, "run_initializer", start)
    monkeypatch.setattr(host, "set_aside", aside)
    step = Initialize(BUILD_A, D1, GENERATION, () if cancel else ("install",))
    host.initialize(step, Witness(cancel_asked=lambda: cancel))
    start.assert_not_called()
    assert aside.call_args_list == ([] if committed else [call(step)])
    assert writer.removed


@pytest.mark.parametrize("committed", [True, False])
def test_a_known_initializer_error_is_reported_only_when_its_transaction_did_not_commit(
    committed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = ContainerHost(RecordingDocker().as_client(), "bed")
    monkeypatch.setattr(host, "await_database", Mock())
    responses = iter([False, committed])
    monkeypatch.setattr(host, "first_start_committed", Mock(side_effect=responses))
    install, aside = Mock(return_value="initialization refused"), Mock()
    monkeypatch.setattr(host, "run_initializer", install)
    monkeypatch.setattr(host, "set_aside", aside)
    step = Initialize(BUILD_A, D1, GENERATION, ("install",))
    if committed:
        host.initialize(step, NOBODY)
        aside.assert_not_called()
    else:
        with pytest.raises(ContainerHostRefused, match="initialization refused"):
            host.initialize(step, NOBODY)
        aside.assert_called_once_with(step)
    install.assert_called_once()


def test_an_unreadable_completion_row_neither_starts_a_writer_nor_sets_the_pair_aside(monkeypatch: pytest.MonkeyPatch) -> None:
    host = ContainerHost(RecordingDocker().as_client(), "bed")
    monkeypatch.setattr(host, "await_database", Mock())
    monkeypatch.setattr(host, "run_in", Mock(return_value=CommandResult(1, "connection lost")))
    install, aside = Mock(), Mock()
    monkeypatch.setattr(host, "run_initializer", install)
    monkeypatch.setattr(host, "set_aside", aside)
    with pytest.raises(ContainerHostError, match="connection lost"):
        host.initialize(Initialize(BUILD_A, D1, GENERATION, ("install",)), NOBODY)
    install.assert_not_called()
    aside.assert_not_called()


def test_set_aside_data_is_named_on_a_healthy_lane_and_does_not_create_an_ambiguous_choice() -> None:
    aside = D1.model_copy(update={"data_set": "interrupted", "set_aside": "marker", "first_start": True, "databases": ()})
    given = Given((OLD_WITH_FILES,), SERVING, data_sets=(D1_WITH_FILES, aside))
    judged = given.judged(None, TK)
    assert not judged.findings
    assert "set aside" in kept_notes(judged, [])[aside.data_set].lower()
    assert lane_data_set(given.reading(), None, "new") == D1.data_set
    outsider = aside.model_copy(update={"first_start": False})
    assert isinstance(lane_data_set(Given(data_sets=(outsider,)).reading(), None, "new"), Stop)
