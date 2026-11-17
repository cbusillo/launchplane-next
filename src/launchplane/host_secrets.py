import threading
from collections.abc import Iterable
from dataclasses import dataclass, field

from launchplane.data_set import LOGIN_VARIABLE, variable_value
from launchplane.docker_answers import (
    DATA_SET_LABEL,
    DOCKER_CONTAINER_LIST,
    PRODUCT_LABEL,
    HostAnswers,
    checked,
    described,
    details_by_name,
    part_of,
)

SECRET_VARIABLES_LABEL = "launchplane.secret-variables"  # noqa: S105 the name of a label, not a secret


@dataclass(frozen=True)
class Held:
    secrets: set[str] = field(default_factory=set[str])
    logins: dict[tuple[str | None, str], set[str]] = field(default_factory=dict[tuple[str | None, str], set[str]])


# What every listed container holds comes from the one list a reading is made from, so none is taken to hold nothing.
def held_from_answers(answers: HostAnswers) -> Held:
    held = Held()
    details = details_by_name(answers.details)
    for summary in checked(DOCKER_CONTAINER_LIST, answers.containers, "container list"):
        environment = described(summary, details, part_of(summary)).config.environment or []
        variables = [variable for variable in summary.labels.get(SECRET_VARIABLES_LABEL, "").split(",") if variable]
        held.secrets.update(value for variable in variables if (value := variable_value(environment, variable)))
        login, data_set = variable_value(environment, LOGIN_VARIABLE), summary.labels.get(DATA_SET_LABEL)
        if login and data_set:
            held.logins.setdefault((summary.labels.get(PRODUCT_LABEL), data_set), set()).add(login)
    return held


class HostSecrets:
    def __init__(self) -> None:
        self.seen: frozenset[str] = frozenset()
        self.noting = threading.Lock()

    def note(self, secrets: Iterable[str]) -> None:
        with self.noting:
            self.seen = self.seen.union(secrets)
