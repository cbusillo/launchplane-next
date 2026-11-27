import docker
import requests
from docker.errors import DockerException
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
    CommandResult,
    ContainerHostError,
    ContainerHostRefused,
    LoginRemovalRefused,
    attempting,
    checked,
    command_result,
    observing,
)
from launchplane.host_secrets import HostSecrets
from launchplane.instance import DataSetUse, new_password
from launchplane.steps import Verb
from launchplane.telling import Said, Witness

RULES_READ = TypeAdapter(list[RuleRead])
LOGINS_READ = TypeAdapter(list[str])
PSQL_STATEMENT_REFUSED = 3

type Filters = dict[str, str | list[str] | bool]


def of_data_set(product: str, data_set: str) -> list[str]:
    return [f"{PRODUCT_LABEL}={product}", f"{DATA_SET_LABEL}={data_set}"]


class HostLogins:
    def __init__(self, client: docker.DockerClient, address: str, secrets: HostSecrets) -> None:
        self.client = client
        self.address = address
        self.secrets = secrets

    def answer(self, database: str, attempt: str, ask: Ask) -> str:
        result = command_result(self.client.containers.get(database), psql_command(ask))
        if not result.succeeded:
            raise ContainerHostError(
                f"{attempt} in the database {database} on {self.address} failed: {result.output or 'no answer'}"
            )
        return result.output

    def read[Shape](self, database: str, attempt: str, ask: Ask, shape: TypeAdapter[Shape]) -> Shape:
        with observing(), attempting(attempt, self.address):
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

    def unheld(self, database: str) -> list[str]:
        labels = self.client.containers.get(database).labels
        product, data_set = labels.get(PRODUCT_LABEL), labels.get(DATA_SET_LABEL)
        if product is None or data_set is None:
            raise ContainerHostError(f"The database {database} does not say its data set, so who holds its logins is not known.")
        # The logins are read before the containers, so one made between the two reads is not taken for one nothing holds.
        logins = self.read_logins(database)
        return sorted(set(logins) - self.held(product, data_set))

    # A statement the server refuses is refused in its own words. Any other failure is no answer: it may have run.
    def change(self, by: Witness, verb: Verb, database: str, login: str, ask: Ask, **variables: str) -> CommandResult:
        answered = by.sending(verb, database, login)
        try:
            result = command_result(self.client.containers.get(database), psql_command(ask, login=login, **variables))
        except (DockerException, requests.RequestException, ContainerHostError) as error:
            answered(None, str(error))
            raise
        refused = Said.REFUSED if result.exit_code == PSQL_STATEMENT_REFUSED else None
        answered(Said.CHANGED if result.succeeded else refused, None if result.succeeded else result.output or "no answer")
        return result

    # Only logins read as unheld just now are removed. A statement the server refuses is its answer, with its own words.
    def remove(self, database: str, logins: list[str], by: Witness) -> None:
        for login in logins:
            result = self.change(by, "remove", database, login, REMOVE_LOGIN)
            failure = LoginRemovalRefused if result.exit_code == PSQL_STATEMENT_REFUSED else ContainerHostError
            if not result.succeeded:
                raise failure(f"The database {database} did not remove the login {login}: {result.output or 'no answer'}")

    def make(self, instance: str, use: DataSetUse, by: Witness) -> dict[str, str]:
        self.require_rules(use)
        database = use.database_container
        self.remove(database, self.unheld(database), by)
        login, password = login_of(instance), new_password()
        self.secrets.note([password])
        # A statement that fails is written whole into the server's log, so the server is never sent the password.
        result = self.change(by, "make", database, login, MAKE_LOGIN, verifier=verifier_of(password))
        if not result.succeeded:
            why = result.output or "no answer"
            raise ContainerHostError(f"Making the login {login} in the database {database} on {self.address} failed: {why}")
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
