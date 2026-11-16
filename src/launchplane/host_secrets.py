import threading
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath

import docker
from docker.errors import APIError, NotFound
from docker.models.containers import Container

from launchplane.data_set import DATA_FOLDER_VARIABLE, MADE_WITH_THE_DATA, variable_value
from launchplane.docker_answers import (
    DATA_SET_LABEL,
    DOCKER_CONTAINER_DETAILS,
    PRODUCT_LABEL,
    ContainerHostError,
    TornRead,
    attempting,
    checked,
    only_file_in,
    read_whole,
    tar_of,
)
from launchplane.instance import DataSetUse, new_password

SECRET_VARIABLES_LABEL = "launchplane.secret-variables"  # noqa: S105 the name of a label, not a secret
PASSWORD_FILE_LABEL = "launchplane.password-file"  # noqa: S105 the name of a label, not a password
OWNER_ONLY = 0o600


def secrets_held(labels: Mapping[str, str], details: object, what: str) -> set[str]:
    variables = [variable for variable in labels.get(SECRET_VARIABLES_LABEL, "").split(",") if variable]
    environment = checked(DOCKER_CONTAINER_DETAILS, details, f"description of {what}").config.environment or []
    held: set[str] = set()
    for variable in variables:
        value = variable_value(environment, variable)
        if value:
            held.add(value)
    return held


def data_is_made(database: Container) -> bool:
    environment = checked(DOCKER_CONTAINER_DETAILS, database.attrs, f"description of {database.name}").config.environment
    folder = variable_value(environment or [], DATA_FOLDER_VARIABLE)
    if folder is None:
        raise ContainerHostError(
            f"The database {database.name} does not say where its data is ({DATA_FOLDER_VARIABLE}), so Launchplane "
            "cannot tell whether its data set is new and may be given a password. It was not started."
        )
    try:
        database.get_archive(str(PurePosixPath(folder) / MADE_WITH_THE_DATA))
    except NotFound:
        return False
    return True


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
        read_whole(self.read_once, self.address)
        return self.seen

    def read_once(self) -> None:
        with attempting("Reading the secrets Launchplane has given to what is", self.address):
            marked: dict[str, str | list[str] | bool] = {"label": PRODUCT_LABEL}
            for holder in self.client.containers.list(all=True, filters=marked, ignore_removed=True):
                self.note(secrets_held(holder.labels, holder.attrs, str(holder.name)))
                try:
                    self.kept_password(holder)
                except APIError as error:
                    raise TornRead(f"{holder.name} was listed, and Docker refused to read its password file: {error}") from error

    def kept_password(self, database: Container) -> str | None:
        path = database.labels.get(PASSWORD_FILE_LABEL)
        if path is None:
            return None
        try:
            chunks, _ = database.get_archive(path)
        except NotFound:
            return None
        password = only_file_in(b"".join(chunks)).decode()
        self.note([password])
        return password or None

    def password_of(self, use: DataSetUse) -> str:
        password = self.kept_password(self.client.containers.get(use.database_container))
        if password is None:
            raise ContainerHostError(
                f"The database {use.database_container} of the data set {use.data_set} keeps no password with its data, "
                "so no instance can be given one."
            )
        return password

    def start_with_password(self, database: Container) -> None:
        if PASSWORD_FILE_LABEL not in database.labels:
            raise ContainerHostError(
                f"The database {database.name} does not say where its data keeps its password ({PASSWORD_FILE_LABEL}), "
                "so it was not started."
            )
        if self.kept_password(database) is None:
            self.make_password(database)
        database.start()

    def make_password(self, database: Container) -> None:
        path = PurePosixPath(database.labels[PASSWORD_FILE_LABEL])
        # Data that exists accepts only the password it was made with, so only a new data set is given one.
        if data_is_made(database):
            raise ContainerHostError(
                f"The data set {database.labels.get(DATA_SET_LABEL)} has data and no password: {path} is not on its volume. "
                f"A password is made only for a new data set, because data that exists accepts only the one it was made with. "
                f"The database {database.name} was not started. Put the password back in that file, or delete the data set by name."
            )
        password = new_password()
        self.note([password])
        database.put_archive(str(path.parent), tar_of(path.name, password.encode(), OWNER_ONLY))  # pyright: ignore[reportUnknownMemberType]
