import threading
from collections.abc import Iterable

from launchplane.data_set import LOGIN_VARIABLE, variable_value
from launchplane.docker_answers import (
    DATA_SET_LABEL,
    PRODUCT_LABEL,
    DockerContainerDetails,
    DockerContainerSummary,
    Listed,
    described,
)

SECRET_VARIABLES_LABEL = "launchplane.secret-variables"  # noqa: S105 the name of a label, not a secret


def environment_of(summary: DockerContainerSummary, found: Listed) -> list[str]:
    return described(summary, found.details, "container").config.environment or []


def secrets_of(summary: DockerContainerSummary, details: DockerContainerDetails) -> set[str]:
    variables = [variable for variable in summary.labels.get(SECRET_VARIABLES_LABEL, "").split(",") if variable]
    environment = details.config.environment or []
    held: set[str] = set()
    for variable in variables:
        value = variable_value(environment, variable)
        if value:
            held.add(value)
    return held


def logins_held(found: Listed) -> dict[tuple[str | None, str], set[str]]:
    held: dict[tuple[str | None, str], set[str]] = {}
    for summary in found.summaries:
        login, data_set = variable_value(environment_of(summary, found), LOGIN_VARIABLE), summary.labels.get(DATA_SET_LABEL)
        if login and data_set:
            held.setdefault((summary.labels.get(PRODUCT_LABEL), data_set), set()).add(login)
    return held


class HostSecrets:
    def __init__(self) -> None:
        self.seen: frozenset[str] = frozenset()
        self.noting = threading.Lock()

    def note(self, secrets: Iterable[str]) -> None:
        with self.noting:
            self.seen = self.seen.union(secrets)
