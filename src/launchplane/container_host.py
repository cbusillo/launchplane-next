import os
import threading
import time
from collections.abc import Callable, Set
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.tls import TLSConfig
from docker.types import EndpointConfig

from launchplane.data_set import (
    CLIENT_PASSWORD_VARIABLE,
    DATA_PATH,
    DATABASE_ADDRESS,
    READY_COMMAND,
    SET_UP_PATH,
    SET_UP_SCRIPT,
    THROWAWAY_PASSWORD_VARIABLE,
    server_environment,
)
from launchplane.docker_answers import (
    BUILD_LABEL,
    DATA_SET_LABEL,
    DOCKER_CONTAINER_DETAILS,
    DOCKER_NETWORK,
    DOCKER_SYSTEM_TIME,
    GATEWAY_MODE_OPTION,
    GENERATION_LABEL,
    INSTANCE_LABEL,
    LANE_LABEL,
    PART_LABEL,
    PRODUCT_LABEL,
    QUIET_GATEWAY_MODE,
    CommandResult,
    ContainerHostError,
    ContainerHostRefused,
    DockerContainerDetails,
    Listed,
    Part,
    TornRead,
    attempting,
    checked,
    command_result,
    container_summaries,
    files_in,
    is_quiet,
    observing,
    on_host_from,
    read_whole,
    running_for,
    sent,
    tar_of,
)
from launchplane.host_logins import HostLogins
from launchplane.host_secrets import SECRET_VARIABLES_LABEL, HostSecrets, logins_held, secrets_of
from launchplane.instance import (
    LANE_NETWORK,
    DataSet,
    DataSetDatabase,
    DataSetUse,
    InstanceState,
    LaneBuild,
    OnHost,
    Unread,
    background_service_container_of,
    new_password,
)
from launchplane.telling import NOBODY, Said, Witness

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
# The states Docker gives a container that a stop finds stopped.
STOPPED = ("created", "exited", "dead")
ALWAYS_RUNS: RestartPolicy = "always"

type Lane = tuple[str, str]


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
    marks = {PART_LABEL: "database", SECRET_VARIABLES_LABEL: THROWAWAY_PASSWORD_VARIABLE}
    return RunOptions(
        image=image,
        container=f"{product}-{lane}-database-{data_set}",
        labels=data_set_labels(product, lane, data_set) | marks,
        networks={network: [DATABASE_ADDRESS]},
        volumes={volume: DATA_PATH},
    )


def part_options(target: LaneBuild, container: str, part: Part, networks: list[str], command: list[str] | None) -> RunOptions:
    labels = {PRODUCT_LABEL: target.product, LANE_LABEL: target.lane, BUILD_LABEL: target.build, PART_LABEL: part}
    return RunOptions(target.build, container, labels, {network: [] for network in networks}, command)


def on_data_set(options: RunOptions, use: DataSetUse, login: dict[str, str]) -> RunOptions:
    labels = options.labels | {DATA_SET_LABEL: use.data_set, SECRET_VARIABLES_LABEL: CLIENT_PASSWORD_VARIABLE}
    return replace(options, labels=labels, environment=login)


# What a generation makes is stamped with it as it is made, so the host shows what each release started.
def stamp_of(generation: str | None) -> dict[str, str]:
    return {} if generation is None else {GENERATION_LABEL: generation}


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
        self.secrets = HostSecrets()
        self.logins = HostLogins(client, address, self.secrets)
        # Every lane shares the proxy, the back soon page and their networks. Each is looked for and made while this is
        # held and no longer, so two lanes that find one missing do not both make it.
        self.making_what_lanes_share = threading.RLock()

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

    def list_marked(self, kind: Literal["containers", "volumes"], lane: Lane | None) -> object:
        marks = PRODUCT_LABEL if lane is None else [f"{PRODUCT_LABEL}={lane[0]}", f"{LANE_LABEL}={lane[1]}"]
        if kind == "containers":
            return self.client.api.containers(all=True, filters={"label": marks})  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        return self.client.api.volumes(filters={"label": marks})  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def list_networks(self) -> object:
        return self.client.api.networks()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def describe(self, container: str) -> object:
        return self.client.api.inspect_container(container)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def system_information(self) -> object:
        return self.client.api.info()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    # One pass over one lane, or over every lane: each container is listed once and described, and that list says what
    # each one holds.
    def read(self, lane: Lane | None = None) -> OnHost:
        return read_whole(lambda: self.read_once(lane), self.address)

    def read_once(self, lane: Lane | None) -> OnHost:
        with attempting(f"Listing the containers and volumes marked {PRODUCT_LABEL}, and the networks", self.address):
            found = self.described(lane)
            networks, volumes = self.list_networks(), self.list_marked("volumes", lane)
            system = self.system_information() if found.has_part("background-service") else None
        on_host, held = on_host_from(found, networks, volumes, system), logins_held(found)
        data_sets = [self.probed(each, held.get((each.product, each.data_set), set())) for each in on_host.data_sets]
        return replace(on_host, data_sets=data_sets)

    # A container's secrets are noted as it is described, before any later work that can fail and say something of it.
    def described(self, lane: Lane | None) -> Listed:
        summaries = container_summaries(self.list_marked("containers", lane))
        details: dict[str, DockerContainerDetails] = {}
        for summary in summaries:
            answer = self.describe_listed(summary.name)
            details[summary.name] = checked(DOCKER_CONTAINER_DETAILS, answer, f"description of {summary.name}")
            self.secrets.note(secrets_of(summary, details[summary.name]))
        return Listed(summaries, details)

    # Asked before an answer is made that reads nothing else, so that answer too is cleaned of what every container holds.
    @observing()
    def read_secrets(self) -> frozenset[str]:
        def describe_every_container() -> None:
            with attempting("Reading the secrets Launchplane has given to what is", self.address):
                self.described(None)

        read_whole(describe_every_container, self.address)
        return self.secrets.seen

    def secrets_seen(self) -> frozenset[str]:
        return self.secrets.seen

    # A refusal about one container Docker has just listed means the host changed during the read.
    def describe_listed(self, container: str) -> object:
        try:
            return self.describe(container)
        except APIError as error:
            raise TornRead(f"{container} was listed, and Docker refused to describe it: {error}") from error

    def probed(self, found: DataSet, held: Set[str]) -> DataSet:
        databases = tuple(self.asked(each, held) if each.state == InstanceState.RUNNING else each for each in found.databases)
        return found.model_copy(update={"databases": databases})

    def asked(self, database: DataSetDatabase, held: Set[str]) -> DataSetDatabase:
        if not self.accepts_connections(database.container):
            return database.model_copy(update={"accepts_connections": False})
        try:
            with attempting(f"Reading the logins of the database {database.container}", self.address):
                unheld: tuple[str, ...] | Unread = tuple(sorted(set(self.logins.read_logins(database.container)) - held))
        except ContainerHostError as error:
            unheld = Unread(asked=f"The logins of the database {database.container}", why=str(error))
        return database.model_copy(update={"accepts_connections": True, "unheld_logins": unheld})

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
        with self.making_what_lanes_share:
            try:
                found = self.client.networks.get(name)
            except NotFound:
                self.create_network(name, None, quiet=quiet)
                return
        if quiet and not is_quiet(checked(DOCKER_NETWORK, found.attrs, f"description of the network {name}")):
            raise ContainerHostRefused(
                f"The network {name} exists and is not quiet: it lets what is attached to it reach out. "
                "Launchplane attaches nothing to it until it is removed and made again."
            )

    def create(self, options: RunOptions) -> Container:
        # The stubs name only two of Docker's restart policies; Docker takes unless-stopped.
        restart: Any = {"Name": options.restart}
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
            restart_policy=restart,
        )

    def run_part(
        self, options: RunOptions, use: DataSetUse | None, login_on: Callable[[DataSetUse], dict[str, str]], by: Witness
    ) -> None:
        if LANE_NETWORK in options.networks:
            self.ensure_network(LANE_NETWORK, quiet=True)
        ready = options if use is None else on_data_set(options, use, login_on(use))
        self.ensure_image(ready.image)
        with sent(by, "make", ready.container):
            made = self.create(ready)
        with sent(by, "start", ready.container):
            made.start()

    def start_instance(
        self, target: LaneBuild, container: str, use: DataSetUse | None, generation: str | None = None, by: Witness = NOBODY
    ) -> None:
        networks = [LANE_NETWORK] if use is None else [LANE_NETWORK, use.network]
        with attempting(f"Starting the instance {container} from build {target.build}", self.address):
            options = part_options(target, container, "web", networks, None)
            stamped = replace(options, labels=options.labels | stamp_of(generation))
            self.run_part(stamped, use, lambda on: self.logins.make(container, on), by)

    def start_background_service(
        self,
        target: LaneBuild,
        instance: str,
        use: DataSetUse | None,
        command: list[str],
        generation: str | None = None,
        by: Witness = NOBODY,
    ) -> None:
        networks = [LANE_NETWORK] if use is None else [use.network]
        options = part_options(target, background_service_container_of(instance), "background-service", networks, command)
        with attempting(f"Starting the background service of the instance {instance}", self.address):
            marked = replace(options, labels=options.labels | {INSTANCE_LABEL: instance} | stamp_of(generation))
            self.run_part(marked, use, lambda _on: self.logins.given_to(instance), by)

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

    def create_data_set_network(self, product: str, lane: str, data_set: str, generation: str | None = None) -> None:
        with attempting(f"Making the network of the data set {data_set}", self.address):
            labels = data_set_labels(product, lane, data_set) | stamp_of(generation)
            self.create_network(f"launchplane-data-{data_set}", labels, quiet=True)

    def create_data_set_volume(self, product: str, lane: str, data_set: str) -> None:
        with attempting(f"Making the volume of the data set {data_set}", self.address):
            self.client.volumes.create(f"launchplane-data-{data_set}-database", labels=data_set_labels(product, lane, data_set))

    def create_database(self, product: str, lane: str, data_set: str, network: str, volume: str, image: str) -> None:
        with attempting(f"Making the database of the data set {data_set}", self.address):
            throwaway_password = new_password()
            self.secrets.note([throwaway_password])
            options = database_options(product, lane, data_set, network, volume, image)
            self.ensure_image(image)
            self.start_set_up(self.create(replace(options, environment=server_environment(throwaway_password))))

    def start_database(self, container: str) -> None:
        with attempting(f"Starting the database {container}", self.address):
            self.start_set_up(self.client.containers.get(container))

    # A server made and never started has no set-up yet, and one with data does not run it again.
    @staticmethod
    def start_set_up(database: Container) -> None:
        database.put_archive(str(SET_UP_PATH.parent), tar_of(SET_UP_PATH.name, SET_UP_SCRIPT.encode()))  # pyright: ignore[reportUnknownMemberType]
        database.start()

    def start_part(self, container: str, by: Witness = NOBODY) -> None:
        with attempting(f"Starting {container}", self.address):
            found = self.client.containers.get(container)
            with sent(by, "start", container, already=found.status == "running"):
                found.start()

    def stop_part(self, container: str, by: Witness = NOBODY) -> None:
        self.stop(container, RETIRE_GRACE_SECONDS, by)

    def stop_database(self, container: str) -> None:
        self.stop(container, DATABASE_STOP_GRACE_SECONDS)

    def stop(self, container: str, grace_seconds: int, by: Witness = NOBODY) -> None:
        with attempting(f"Stopping {container}", self.address):
            found = self.find_container(container)
            if found is not None:
                with sent(by, "stop", container, already=found.status in STOPPED):
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

    # A database server is retired with nobody told: only a command for a lane's instance or background service has a line.
    def retire_container(self, container: str, by: Witness = NOBODY) -> None:
        found = self.find_container(container)
        if found is None:
            by.sending("remove", container)(Said.ALREADY, None)
            return
        with sent(by, "stop", container, already=found.status in STOPPED):
            found.stop(timeout=RETIRE_GRACE_SECONDS)
        with sent(by, "remove", container):
            found.remove()

    def retire_instance(self, container: str, by: Witness = NOBODY) -> None:
        with attempting(f"Stopping and removing {container}", self.address):
            self.retire_container(container, by)

    # A read that fails is answered as unread and nothing is removed. A removal that fails is raised.
    def remove_unheld_logins(self, database: str) -> Unread | None:
        try:
            with attempting(f"Reading the logins of the database {database} and who holds them", self.address):
                unheld = self.logins.unheld(database)
        except ContainerHostError as error:
            return Unread(asked=f"Which logins of the database {database} no container holds", why=str(error))
        with attempting(f"Removing the logins no container holds from the database {database}", self.address):
            self.logins.remove(database, unheld)
        return None

    def read_log_tail(self, container: str) -> str:
        with attempting(f"Reading the log of {container}", self.address):
            found = self.find_container(container)
            if found is None:
                return ""
            return found.logs(tail=LOG_TAIL_LINES).decode(errors="replace")

    def ensure_helper(self, helper: HelperContainer) -> None:
        with attempting(f"Starting Launchplane's own container {helper.container}", self.address), self.making_what_lanes_share:
            found = self.find_container(helper.container)
            if found is None:
                if helper.outside_network is not None:
                    self.ensure_network(helper.outside_network, quiet=False)
                for network in helper.quiet_networks:
                    self.ensure_network(network, quiet=True)
                self.ensure_image(helper.image)
                self.create(helper_options(helper)).start()
            elif found.status != "running":
                found.start()

    @observing()
    def is_running(self, container: str) -> bool:
        with attempting(f"Reading the state of {container}", self.address):
            found = self.find_container(container)
            return found is not None and found.status == "running"

    def run_in(self, container: str, command: list[str]) -> CommandResult:
        with attempting(f"Running {command[0]} in {container}", self.address):
            return self.command_result_in(container, command)

    def command_result_in(self, container: str, command: list[str]) -> CommandResult:
        return command_result(self.client.containers.get(container), command)

    def read_folder(self, container: str, folder: PurePosixPath) -> dict[str, FileRead]:
        with attempting(f"Reading {folder} in {container}", self.address):
            found = self.find_container(container)
            if found is None:
                return {}
            try:
                chunks, _ = found.get_archive(str(folder))
            except NotFound:
                return {}
            return {
                name: FileRead(content.decode(), changed_at) for name, (content, changed_at) in files_in(b"".join(chunks)).items()
            }
