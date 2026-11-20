import pytest

from tests.bed import holds_url_beside


@pytest.mark.parametrize("control", ["http://control.example", "http://control.example:8700", "http://control.example:8700/"])
def test_the_holds_are_asked_at_the_control_address_whether_or_not_its_url_names_a_port(control: str) -> None:
    assert holds_url_beside(control) == "http://control.example:8701"
