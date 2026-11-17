import threading
from collections.abc import Iterable, Mapping

import docker

from launchplane.data_set import variable_value
from launchplane.docker_answers import DOCKER_CONTAINER_DETAILS, PRODUCT_LABEL, attempting, checked

SECRET_VARIABLES_LABEL = "launchplane.secret-variables"  # noqa: S105 the name of a label, not a secret


def secrets_held(labels: Mapping[str, str], details: object, what: str) -> set[str]:
    variables = [variable for variable in labels.get(SECRET_VARIABLES_LABEL, "").split(",") if variable]
    environment = checked(DOCKER_CONTAINER_DETAILS, details, f"description of {what}").config.environment or []
    held: set[str] = set()
    for variable in variables:
        value = variable_value(environment, variable)
        if value:
            held.add(value)
    return held


class HostSecrets:
    def __init__(self, client: docker.DockerClient, address: str) -> None:
        self.client = client
        self.address = address
        self.seen: frozenset[str] = frozenset()
        self.noting = threading.Lock()

    def note(self, secrets: Iterable[str]) -> None:
        with self.noting:
            self.seen = self.seen.union(secrets)

    def secrets_seen(self) -> frozenset[str]:
        return self.seen

    def read_secrets(self) -> frozenset[str]:
        with attempting("Reading the secrets Launchplane has given to what is", self.address):
            marked: dict[str, str | list[str] | bool] = {"label": PRODUCT_LABEL}
            for holder in self.client.containers.list(all=True, filters=marked, ignore_removed=True):
                self.note(secrets_held(holder.labels, holder.attrs, str(holder.name)))
        return self.seen
