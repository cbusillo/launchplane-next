import docker
from pydantic import TypeAdapter, ValidationError

from launchplane.data_set import (
    CLIENT_PASSWORD_VARIABLE,
    LOGIN_VARIABLE,
    MAKE_LOGIN,
    READ_LOGINS,
    READ_RULES,
    REMOVE_LOGIN,
    RULES_AS_READ,
    RULES_FILE,
    Ask,
    RuleRead,
    client_environment,
    login_of,
    psql_command,
    variable_value,
    verifier_of,
)
from launchplane.docker_answers import (
    DATA_SET_LABEL,
    DOCKER_CONTAINER_DETAILS,
    PRODUCT_LABEL,
    ContainerHostError,
    ContainerHostRefused,
    checked,
    command_result,
)
from launchplane.host_secrets import HostSecrets
from launchplane.instance import DataSetUse, new_password

RULES_READ = TypeAdapter(list[RuleRead])
LOGINS_READ = TypeAdapter(list[str])

type Filters = dict[str, str | list[str] | bool]


def of_data_set(product: str, data_set: str) -> list[str]:
    return [f"{PRODUCT_LABEL}={product}", f"{DATA_SET_LABEL}={data_set}"]


class HostLogins:
    def __init__(self, client: docker.DockerClient, address: str, secrets: HostSecrets) -> None:
        self.client = client
        self.address = address
        self.secrets = secrets

    def answer(self, database: str, attempt: str, ask: Ask, **variables: str) -> str:
        result = command_result(self.client.containers.get(database), psql_command(ask, **variables))
        if not result.succeeded:
            raise ContainerHostError(
                f"{attempt} in the database {database} on {self.address} failed: {result.output or 'no answer'}"
            )
        return result.output

    def read[Shape](self, database: str, attempt: str, ask: Ask, shape: TypeAdapter[Shape]) -> Shape:
        answer = self.answer(database, attempt, ask)
        try:
            return shape.validate_json(answer)
        except ValidationError as error:
            raise ContainerHostError(
                f"{attempt} in the database {database} on {self.address} was answered with something else than was asked: {answer}"
            ) from error

    def require_rules(self, use: DataSetUse) -> None:
        found = self.read(use.database_container, "Reading the access rules", READ_RULES, RULES_READ)
        if found != RULES_AS_READ:
            raise ContainerHostRefused(
                f"The database {use.database_container} of the data set {use.data_set} does not have Launchplane's access rules, "
                f"so no instance was started on it. Nothing was deleted. It must have exactly:\n{RULES_FILE}\nIt has, as "
                f"PostgreSQL reads them: {found}. Its set-up did not finish or its rules were changed. "
                "Put the rules right by hand, or delete the data set by name."
            )

    def held(self, product: str, data_set: str) -> set[str]:
        filters: Filters = {"label": of_data_set(product, data_set)}
        held: set[str] = set()
        for holder in self.client.containers.list(all=True, filters=filters, ignore_removed=True):
            environment = checked(DOCKER_CONTAINER_DETAILS, holder.attrs, f"description of {holder.name}").config.environment
            login = variable_value(environment or [], LOGIN_VARIABLE)
            if login is not None:
                held.add(login)
        return held

    def read_logins(self, database: str) -> list[str]:
        return self.read(database, "Reading the logins", READ_LOGINS, LOGINS_READ)

    def remove_unheld(self, database: str) -> None:
        labels = self.client.containers.get(database).labels
        product, data_set = labels.get(PRODUCT_LABEL), labels.get(DATA_SET_LABEL)
        if product is None or data_set is None:
            raise ContainerHostRefused(
                f"The database {database} does not say which product and data set it is, so Launchplane cannot tell "
                "which containers hold its logins. No login was removed."
            )
        # The logins are read before the containers, so one made between the two reads is not taken for one nothing holds.
        logins = self.read_logins(database)
        for login in sorted(set(logins) - self.held(product, data_set)):
            self.answer(database, f"Removing the login {login}", REMOVE_LOGIN, login=login)

    def make(self, instance: str, use: DataSetUse) -> dict[str, str]:
        self.require_rules(use)
        # A start that failed after its login was made left one that no call has answered for yet.
        self.remove_unheld(use.database_container)
        login, password = login_of(instance), new_password()
        self.secrets.note([password])
        # A statement that fails is written whole into the server's log, so the server is never sent the password.
        self.answer(use.database_container, f"Making the login {login}", MAKE_LOGIN, login=login, verifier=verifier_of(password))
        return client_environment(login, password)

    def given_to(self, instance: str) -> dict[str, str]:
        holder = self.client.containers.get(instance)
        environment = checked(DOCKER_CONTAINER_DETAILS, holder.attrs, f"description of {instance}").config.environment or []
        login = variable_value(environment, LOGIN_VARIABLE)
        password = variable_value(environment, CLIENT_PASSWORD_VARIABLE)
        if not login or not password:
            raise ContainerHostRefused(
                f"The instance {instance} holds no database login ({LOGIN_VARIABLE} and {CLIENT_PASSWORD_VARIABLE}), "
                "so its background service cannot be given it. Moving the lane to a build again makes an instance that holds one."
            )
        self.secrets.note([password])
        return client_environment(login, password)
