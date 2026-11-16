import os
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.tls import TLSConfig
from docker.types import EndpointConfig

from launchplane.data_set import (
    CLIENT_PASSWORD_VARIABLE,
    DATA_PATH,
    DATABASE_ADDRESS,
    PASSWORD_PATH,
    READY_COMMAND,
    SERVER_ENVIRONMENT,
    client_environment,
)
from launchplane.docker_answers import (
    BUILD_LABEL,
    DATA_SET_LABEL,
    DOCKER_CONTAINER_DETAILS,
    DOCKER_NETWORK,
    DOCKER_SYSTEM_TIME,
    GATEWAY_MODE_OPTION,
    INSTANCE_LABEL,
    LANE_LABEL,
    PART_LABEL,
    PRODUCT_LABEL,
    QUIET_GATEWAY_MODE,
    ContainerHostError,
    HostAnswers,
    OnHost,
    Part,
    TornRead,
    attempting,
    checked,
    dated_file_in,
    is_quiet,
    names_of,
    on_host_from_answers,
    read_whole,
    running_for,
    tar_of,
)
from launchplane.host_secrets import PASSWORD_FILE_LABEL, SECRET_VARIABLES_LABEL, HostSecrets
from launchplane.instance import LANE_NETWORK, DataSet, DataSetUse, InstanceState, LaneBuild, background_service_container_of

MARKED = {"label": PRODUCT_LABEL}
DOCKER_API_VERSION = "1.47"
ANSWER_TIMEOUT_SECONDS = 5
RETIRE_GRACE_SECONDS = 30
DATABASE_STOP_GRACE_SECONDS = 60
LOG_TAIL_LINES = 20
DATABASE_DEADLINE_SECONDS = 60
BACKGROUND_SERVICE_DEADLINE_MARGIN_SECONDS = 15
POLL_SECONDS = 0.5
type RestartPolicy = Literal["always", "unless-stopped"]
# Docker starts a container with this policy again when it crashes, and not one that was stopped through Docker.
STAYS_STOPPED_WHEN_STOPPED: RestartPolicy = "unless-stopped"
ALWAYS_RUNS: RestartPolicy = "always"

type HasDatabase = Callable[[str], bool]


class PartStopped(Exception):
    pass


@dataclass(frozen=True)
class HelperContainer:
    container: str
    image: str
    command: list[str]
    ports: dict[int, int]
    volumes: dict[str, str]
    quiet_networks: list[str]
    outside_network: str | None = None

    @property
    def networks(self) -> list[str]:
        return [*([] if self.outside_network is None else [self.outside_network]), *self.quiet_networks]


@dataclass(frozen=True)
class FileRead:
    text: str
    changed_at: datetime


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class RunOptions:
    image: str
    container: str
    labels: dict[str, str]
    networks: dict[str, list[str]]
    command: list[str] | None = None
    environment: dict[str, str] = field(default_factory=dict[str, str])
    volumes: dict[str, str] = field(default_factory=dict[str, str])
    ports: dict[int, int] = field(default_factory=dict[int, int])
    restart: RestartPolicy = STAYS_STOPPED_WHEN_STOPPED


def endpoints(networks: dict[str, list[str]]) -> dict[str, EndpointConfig]:
    return {network: EndpointConfig(DOCKER_API_VERSION, aliases=aliases or None) for network, aliases in networks.items()}


def data_set_labels(product: str, lane: str, data_set: str) -> dict[str, str]:
    return {PRODUCT_LABEL: product, LANE_LABEL: lane, DATA_SET_LABEL: data_set}


def database_options(product: str, lane: str, data_set: str, network: str, volume: str, image: str) -> RunOptions:
    return RunOptions(
        image=image,
        container=f"{product}-{lane}-database-{data_set}",
        labels=data_set_labels(product, lane, data_set) | {PART_LABEL: "database", PASSWORD_FILE_LABEL: str(PASSWORD_PATH)},
        networks={network: [DATABASE_ADDRESS]},
        environment=SERVER_ENVIRONMENT,
        volumes={volume: DATA_PATH},
    )


def part_options(target: LaneBuild, container: str, part: Part, networks: list[str], command: list[str] | None) -> RunOptions:
    labels = {PRODUCT_LABEL: target.product, LANE_LABEL: target.lane, BUILD_LABEL: target.build, PART_LABEL: part}
    return RunOptions(target.build, container, labels, {network: [] for network in networks}, command)


def on_data_set(options: RunOptions, use: DataSetUse, password: str) -> RunOptions:
    labels = options.labels | {DATA_SET_LABEL: use.data_set, SECRET_VARIABLES_LABEL: CLIENT_PASSWORD_VARIABLE}
    return replace(options, labels=labels, environment=client_environment(password))


def helper_options(helper: HelperContainer) -> RunOptions:
    return RunOptions(
        image=helper.image,
        container=helper.container,
        labels={},
        networks={network: [] for network in helper.networks},
        command=helper.command,
        volumes=helper.volumes,
        ports=helper.ports,
        restart=ALWAYS_RUNS,
    )


class ContainerHost:
    def __init__(
        self,
        client: docker.DockerClient,
        address: str,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.address = address
        self.sleep = sleep
        self.clock = clock
        self.secrets = HostSecrets(client, address)

    @classmethod
    def from_environment(cls) -> ContainerHost:
        address = os.environ["LAUNCHPLANE_CONTAINER_HOST"]
        credentials = Path(os.environ["LAUNCHPLANE_CONTAINER_HOST_CREDENTIALS"])
        tls = TLSConfig(
            client_cert=(str(credentials / "cert.pem"), str(credentials / "key.pem")),
            ca_cert=str(credentials / "ca.pem"),
            verify=True,
        )
        client = docker.DockerClient(base_url=address, tls=tls, version=DOCKER_API_VERSION, timeout=ANSWER_TIMEOUT_SECONDS)
        return cls(client, address)

    def list_marked(self, kind: Literal["containers", "volumes"]) -> object:
        if kind == "containers":
            return self.client.api.containers(all=True, filters=MARKED)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        return self.client.api.volumes(filters=MARKED)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def list_networks(self) -> object:
        return self.client.api.networks()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def describe(self, container: str) -> object:
        return self.client.api.inspect_container(container)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def system_information(self) -> object:
        return self.client.api.info()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def read(self, has_database: HasDatabase | None = None) -> OnHost:
        return read_whole(lambda: self.read_once(has_database), self.address)

    def read_once(self, has_database: HasDatabase | None) -> OnHost:
        with attempting(f"Listing the containers and volumes marked {PRODUCT_LABEL}, and the networks", self.address):
            containers, networks, volumes = self.list_marked("containers"), self.list_networks(), self.list_marked("volumes")
            background_services = names_of(containers, "background-service")
            details = [self.describe_listed(name) for name in [*background_services, *names_of(containers, "database")]]
            system = self.system_information() if background_services else None
        on_host = on_host_from_answers(HostAnswers(containers, networks, volumes, details, system))
        if has_database is None:
            return on_host
        return replace(
            on_host, data_sets=[self.probed(found) if has_database(found.product) else found for found in on_host.data_sets]
        )

    # A refusal about one container Docker has just listed means the host changed during the read.
    def describe_listed(self, container: str) -> object:
        try:
            return self.describe(container)
        except APIError as error:
            raise TornRead(f"{container} was listed, and Docker refused to describe it: {error}") from error

    def probed(self, found: DataSet) -> DataSet:
        databases = tuple(
            each.model_copy(update={"accepts_connections": self.accepts_connections(each.container)})
            if each.state == InstanceState.RUNNING
            else each
            for each in found.databases
        )
        return found.model_copy(update={"databases": databases})

    def accepts_connections(self, container: str) -> bool:
        with attempting(f"Asking the database {container} whether it accepts connections", self.address):
            try:
                return self.command_result_in(container, READY_COMMAND).succeeded
            except APIError as error:
                raise TornRead(f"The database {container} was listed as running, and Docker refused to ask it: {error}") from error

    def background_service_running_for(self, container: str) -> float | None:
        with attempting(f"Reading how long the background service {container} has run", self.address):
            details = checked(DOCKER_CONTAINER_DETAILS, self.describe(container), f"description of {container}")
            now = checked(DOCKER_SYSTEM_TIME, self.system_information(), "system information").system_time
        return running_for(details, now)

    def find_container(self, container: str) -> Container | None:
        try:
            return self.client.containers.get(container)
        except NotFound:
            return None

    def ensure_image(self, image: str) -> None:
        try:
            self.client.images.get(image)
        except ImageNotFound:
            self.client.images.pull(image)

    def create_network(self, name: str, labels: dict[str, str] | None, *, quiet: bool) -> None:
        if quiet:
            options = {GATEWAY_MODE_OPTION: QUIET_GATEWAY_MODE}
            self.client.networks.create(name, driver="bridge", internal=True, options=options, labels=labels)
        else:
            self.client.networks.create(name, driver="bridge", labels=labels)

    def ensure_network(self, name: str, *, quiet: bool) -> None:
        try:
            found = self.client.networks.get(name)
        except NotFound:
            self.create_network(name, None, quiet=quiet)
            return
        if quiet and not is_quiet(checked(DOCKER_NETWORK, found.attrs, f"description of the network {name}")):
            raise ContainerHostError(
                f"The network {name} exists and is not quiet: it lets what is attached to it reach out. "
                "Launchplane attaches nothing to it until it is removed and made again."
            )

    def create(self, options: RunOptions) -> Container:
        self.ensure_image(options.image)
        return self.client.containers.create(
            options.image,
            options.command,
            name=options.container,
            labels=options.labels,
            network=next(iter(options.networks)),
            networking_config=endpoints(options.networks),
            environment=options.environment,
            volumes={volume: {"bind": path, "mode": "rw"} for volume, path in options.volumes.items()},
            ports={f"{inside}/tcp": outside for inside, outside in options.ports.items()},
            # The stubs name only two of Docker's restart policies; Docker takes unless-stopped.
            restart_policy={"Name": options.restart},  # pyright: ignore[reportArgumentType]
        )

    def run_part(self, options: RunOptions, use: DataSetUse | None) -> None:
        if LANE_NETWORK in options.networks:
            self.ensure_network(LANE_NETWORK, quiet=True)
        ready = options if use is None else on_data_set(options, use, self.secrets.password_of(use))
        self.create(ready).start()

    def start_instance(self, target: LaneBuild, container: str, use: DataSetUse | None) -> None:
        networks = [LANE_NETWORK] if use is None else [LANE_NETWORK, use.network]
        with attempting(f"Starting the instance {container} from build {target.build}", self.address):
            self.run_part(part_options(target, container, "web", networks, None), use)

    def start_background_service(self, target: LaneBuild, instance: str, use: DataSetUse | None, command: list[str]) -> None:
        networks = [LANE_NETWORK] if use is None else [use.network]
        options = part_options(target, background_service_container_of(instance), "background-service", networks, command)
        with attempting(f"Starting the background service of the instance {instance}", self.address):
            self.run_part(replace(options, labels=options.labels | {INSTANCE_LABEL: instance}), use)

    def await_background_service(self, container: str, steady_seconds: float) -> None:
        started = self.clock()
        while (ran := self.background_service_running_for(container)) is None or ran < steady_seconds:
            if ran is None:
                raise PartStopped(
                    f"The background service {container} is not running: it stopped before it had run {steady_seconds:g} s."
                )
            if self.clock() - started >= steady_seconds + BACKGROUND_SERVICE_DEADLINE_MARGIN_SECONDS:
                raise PartStopped(
                    f"The background service {container} did not stay up {steady_seconds:g} s in a row; it keeps starting again."
                )
            self.sleep(POLL_SECONDS)

    def create_data_set_network(self, product: str, lane: str, data_set: str) -> None:
        with attempting(f"Making the network of the data set {data_set}", self.address):
            self.create_network(f"launchplane-data-{data_set}", data_set_labels(product, lane, data_set), quiet=True)

    def create_data_set_volume(self, product: str, lane: str, data_set: str) -> None:
        with attempting(f"Making the volume of the data set {data_set}", self.address):
            self.client.volumes.create(f"launchplane-data-{data_set}-database", labels=data_set_labels(product, lane, data_set))

    def create_database(self, product: str, lane: str, data_set: str, network: str, volume: str, image: str) -> None:
        with attempting(f"Making the database of the data set {data_set}", self.address):
            made = self.create(database_options(product, lane, data_set, network, volume, image))
            self.secrets.start_with_password(made)

    def start_database(self, container: str) -> None:
        with attempting(f"Starting the database {container}", self.address):
            self.secrets.start_with_password(self.client.containers.get(container))

    def start_part(self, container: str) -> None:
        with attempting(f"Starting {container}", self.address):
            self.client.containers.get(container).start()

    def stop_part(self, container: str) -> None:
        self.stop(container, RETIRE_GRACE_SECONDS)

    def stop_database(self, container: str) -> None:
        self.stop(container, DATABASE_STOP_GRACE_SECONDS)

    def stop(self, container: str, grace_seconds: int) -> None:
        with attempting(f"Stopping {container}", self.address):
            found = self.find_container(container)
            if found is not None:
                found.stop(timeout=grace_seconds)

    def await_database(self, container: str) -> None:
        started = self.clock()
        while not (result := self.run_in(container, READY_COMMAND)).succeeded:
            if self.clock() - started >= DATABASE_DEADLINE_SECONDS:
                raise ContainerHostError(
                    f"The database {container} did not accept connections within {DATABASE_DEADLINE_SECONDS} s "
                    f"of being asked: {result.output or 'no answer'}. Its last log lines:\n{self.read_log_tail(container)}"
                )
            self.sleep(POLL_SECONDS)

    def delete_data_set(self, found: DataSet) -> None:
        with attempting(f"Deleting the data set {found.data_set}", self.address):
            for database in found.databases:
                self.retire_container(database.container)
            for volume in found.volumes:
                with suppress(NotFound):
                    self.client.volumes.get(volume).remove()
            for network in found.networks:
                with suppress(NotFound):
                    self.client.networks.get(network.name).remove()

    def retire_container(self, container: str) -> None:
        found = self.find_container(container)
        if found is not None:
            found.stop(timeout=RETIRE_GRACE_SECONDS)
            found.remove()

    def remove_instance(self, container: str) -> None:
        with attempting(f"Removing the instance {container}", self.address):
            found = self.find_container(container)
            if found is not None:
                found.remove(force=True)

    def retire_instance(self, container: str) -> None:
        with attempting(f"Stopping and removing {container}", self.address):
            self.retire_container(container)

    def read_log_tail(self, container: str) -> str:
        with attempting(f"Reading the log of {container}", self.address):
            found = self.find_container(container)
            if found is None:
                return ""
            return found.logs(tail=LOG_TAIL_LINES).decode(errors="replace")

    def ensure_helper(self, helper: HelperContainer) -> None:
        with attempting(f"Starting Launchplane's own container {helper.container}", self.address):
            found = self.find_container(helper.container)
            if found is None:
                if helper.outside_network is not None:
                    self.ensure_network(helper.outside_network, quiet=False)
                for network in helper.quiet_networks:
                    self.ensure_network(network, quiet=True)
                self.create(helper_options(helper)).start()
            elif found.status != "running":
                found.start()

    def is_running(self, container: str) -> bool:
        with attempting(f"Reading the state of {container}", self.address):
            found = self.find_container(container)
            return found is not None and found.status == "running"

    def run_in(self, container: str, command: list[str]) -> CommandResult:
        with attempting(f"Running {command[0]} in {container}", self.address):
            return self.command_result_in(container, command)

    def command_result_in(self, container: str, command: list[str]) -> CommandResult:
        result = self.client.containers.get(container).exec_run(command)
        exit_code, output = result.exit_code, result.output
        if exit_code is None or not isinstance(output, bytes):
            raise ContainerHostError(f"Running {command[0]} in {container} on {self.address} returned before the command finished.")
        return CommandResult(exit_code=exit_code, output=output.decode(errors="replace").strip())

    def read_file(self, container: str, path: PurePosixPath) -> FileRead | None:
        with attempting(f"Reading {path} in {container}", self.address):
            found = self.client.containers.get(container)
            try:
                chunks, _ = found.get_archive(str(path))
            except NotFound:
                return None
            content, changed_at = dated_file_in(b"".join(chunks))
            return FileRead(content.decode(), changed_at)

    def place_file(self, container: str, path: PurePosixPath, content: str) -> None:
        with attempting(f"Placing {path} in {container}", self.address):
            self.client.containers.get(container).put_archive(str(path.parent), tar_of(path.name, content.encode()))  # pyright: ignore[reportUnknownMemberType]
