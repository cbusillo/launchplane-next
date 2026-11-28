import os
import threading
import time
from collections.abc import Callable, Set
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import NAMESPACE_OID, uuid5

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.tls import TLSConfig
from docker.types import EndpointConfig

from launchplane.data_set import (
    CLIENT_PASSWORD_VARIABLE,
    DATA_PATH,
    DATABASE_ADDRESS,
    FIRST_START_COMMITTED,
    READY_COMMAND,
    SET_UP_PATH,
    SET_UP_SCRIPT,
    THROWAWAY_PASSWORD_VARIABLE,
    login_of,
    psql_command,
    server_environment,
)
from launchplane.docker_answers import (
    BUILD_LABEL,
    COPY_OF_LABEL,
    DATA_SET_LABEL,
    DOCKER_CONTAINER_DETAILS,
    DOCKER_NETWORK,
    DOCKER_SYSTEM_TIME,
    FIRST_START_LABEL,
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
from launchplane.host_logins import Filters, HostLogins
from launchplane.host_secrets import SECRET_VARIABLES_LABEL, HostSecrets, logins_held, secrets_of
from launchplane.instance import (
    LANE_NETWORK,
    DataSet,
    DataSetDatabase,
    DataSetUse,
    InstanceState,
    LaneBuild,
    OnHost,
    RunningInstance,
    Unread,
    background_service_container_of,
    new_password,
)
from launchplane.steps import CopyVolume, Initialize, Update
from launchplane.telling import NOBODY, Said, Witness

DOCKER_API_VERSION = "1.47"
ANSWER_TIMEOUT_SECONDS = 5
RETIRE_GRACE_SECONDS = 30
DATABASE_STOP_GRACE_SECONDS = 60
LOG_TAIL_LINES = 20
DATABASE_DEADLINE_SECONDS = 60
BACKGROUND_SERVICE_DEADLINE_MARGIN_SECONDS = 15
POLL_SECONDS = 0.5
# How long one volume may take to copy. Its lane shows the back soon page for that long.
COPY_DEADLINE_SECONDS = 300
# debian:trixie-slim. Its cp hands each file to the kernel to copy, which shares the blocks where the file system can.
COPIER_IMAGE = "debian@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0"
# Everything in the volume as it is, then every byte read again from both: a file the copy lacks or has too many fails it.
# The copy is written where the database image keeps its one volume, so the copier is given no other volume to write.
COPY_COMMAND = ["sh", "-c", f"cp -a --reflink=auto /from/. {DATA_PATH}/ && diff -r --no-dereference /from {DATA_PATH} && sync"]
# How long the host waits for what is run once. A command that must end sooner ends itself.
RUN_ONCE_DEADLINE_SECONDS = 900
EMPTY_DATABASE = """\
dump() { pg_dump --username=postgres --schema-only --restrict-key=LaunchplaneFirstStart "$1"; }
fresh=$(dump template1)
actual=$(dump app)
[ "$fresh" = "$actual" ] || exit 42
large=$(psql -XAt --username=postgres --dbname=app -c 'select exists (select from pg_largeobject_metadata)')
[ "$large" = f ] || exit 42
"""
type RestartPolicy = Literal["always", "unless-stopped", "no"]
# Docker starts a container with this policy again when it crashes, and not one that was stopped through Docker.
STAYS_STOPPED_WHEN_STOPPED: RestartPolicy = "unless-stopped"
STOPPED = ("created", "exited", "dead")
ALWAYS_RUNS: RestartPolicy = "always"
# Docker never starts a container with this policy again, whatever becomes of it or of the host.
RUNS_ONCE: RestartPolicy = "no"

type Lane = tuple[str, str]


class PartStopped(Exception):
    pass


@dataclass(frozen=True)
class HelperContainer:
    container: str
    image: str
    command: list[str]
    quiet_networks: list[str]
    ports: dict[int, int] = field(default_factory=dict[int, int])
    volumes: dict[str, str] = field(default_factory=dict[str, str])
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
    user: str | None = None


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
    return replace(options, labels=labels, environment=options.environment | login)


# What a generation makes is stamped with it as it is made, so the host shows what each release started.
def stamp_of(generation: str | None) -> dict[str, str]:
    return {} if generation is None else {GENERATION_LABEL: generation}


def pair_stamp(data_set: str, generation: str | None) -> dict[str, str]:
    first = {FIRST_START_LABEL: "true"} if generation and data_set.startswith(f"first-{generation}-") else {}
    return stamp_of(generation) | first


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
        images = {summary.name: summary.image_id for summary in found.summaries}
        instances = [self.held_to_its_build(each, images[each.container]) for each in on_host.instances]
        return replace(on_host, data_sets=data_sets, instances=instances)

    # A build that names a digest is the image the host has for that digest, whatever the instance's label says.
    def held_to_its_build(self, instance: RunningInstance, image: str) -> RunningInstance:
        build = instance.build or ""
        with attempting(f"Reading the image of the build of {instance.container}", self.address), suppress(ImageNotFound):
            if "@sha256:" not in build or self.client.images.get(build).id == image:
                return instance
        return instance.model_copy(update={"other_image": image or "an image Docker does not name"})

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
        if found.set_aside:
            return found
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
            user=options.user,
        )

    def run_part(
        self, options: RunOptions, use: DataSetUse | None, login_on: Callable[[DataSetUse], dict[str, str]], by: Witness
    ) -> Container:
        if LANE_NETWORK in options.networks:
            self.ensure_network(LANE_NETWORK, quiet=True)
        ready = options if use is None else on_data_set(options, use, login_on(use))
        self.ensure_image(ready.image)
        with sent(by, "make", ready.container):
            made = self.create(ready)
        with sent(by, "start", ready.container):
            made.start()
        return made

    @staticmethod
    def instance_options(
        target: LaneBuild, container: str, use: DataSetUse | None, generation: str | None, part: Part = "web"
    ) -> RunOptions:
        networks, run = [LANE_NETWORK] if use is None else [LANE_NETWORK, use.network], target.run
        files = {} if use is None or use.files is None or run.files is None else {use.files: run.files}
        options = part_options(target, container, part, networks, [*run.command] or None)
        stamped = replace(options, labels=options.labels | stamp_of(generation), volumes=files, user=run.user)
        return replace(stamped, environment=dict(run.environment))

    def start_instance(
        self, target: LaneBuild, container: str, use: DataSetUse | None, generation: str | None = None, by: Witness = NOBODY
    ) -> None:
        with attempting(f"Starting the instance {container} from build {target.build}", self.address):
            ready = self.instance_options(target, container, use, generation)
            self.run_part(ready, use, lambda on: self.logins.make(container, on, by), by)

    # The build, run once to its end as an instance of it runs on the data set: its settings, its files and a login. It is
    # on the data set's network alone, where no visitor is sent. Once it has ended and answered, it is removed.
    def run_once(self, update: Update, by: Witness = NOBODY) -> CommandResult:
        target, container, use = update.target, update.container, update.use
        with attempting(f"Running {update.command[0]} of build {target.build} once in {container}", self.address):
            as_instance = self.instance_options(target, container, use, update.generation, "update")
            once = replace(as_instance, networks={use.network: []}, command=[*update.command], restart=RUNS_ONCE)
            ran = self.run_part(once, use, lambda on: self.logins.make(container, on, by), by)
            exit_code = ran.wait(timeout=RUN_ONCE_DEADLINE_SECONDS)["StatusCode"]
            output = ran.logs(tail=LOG_TAIL_LINES).decode(errors="replace").strip()
            self.retire_container(container, by)
        return CommandResult(exit_code=exit_code, output=output)

    def initialize(self, step: Initialize, by: Witness) -> None:
        found, pair = step.found, step.found.data_set
        name = f"initializer-{uuid5(NAMESPACE_OID, pair).hex}"
        with observing(), attempting(f"Resolving the first start of {pair}", self.address):
            writer = self.find_container(name)
            if writer is not None:
                expected = data_set_labels(step.target.product, step.target.lane, pair) | {PART_LABEL: "initializer"}
                if not expected.items() <= writer.labels.items():
                    raise ContainerHostError(f"The container named {name} does not belong to this first start.")
                writer.stop(timeout=RETIRE_GRACE_SECONDS)
            if not found.databases:
                if writer is not None:
                    raise ContainerHostError(f"The database of initializer {name} is missing. Its commit cannot be read.")
                self.set_aside(step)
                return
            database = found.databases[0].container
            if found.databases[0].state == InstanceState.STOPPED:
                self.start_database(database)
            self.await_database(database)
            refusal = None
            check = psql_command(FIRST_START_COMMITTED, pair=pair, login=login_of(name))
            if writer is None and step.command and not self.first_start_committed(database, check):
                refusal = self.run_initializer(step, name, database, by)
            committed = self.first_start_committed(database, check)
            if not committed:
                self.set_aside(step)
            self.retire_container(name, by)
        if refusal is not None and not committed and not by.cancel_asked():
            raise ContainerHostRefused(f"The first start of {pair} failed. The pair was set aside. {refusal}")

    def first_start_committed(self, database: str, check: list[str]) -> bool:
        result = self.run_in(database, check)
        if not result.succeeded or result.output.strip() not in ("t", "f"):
            raise ContainerHostError(f"Reading the first-start commit in {database} failed: {result.output}")
        return result.output.strip() == "t"

    def set_aside(self, step: Initialize) -> None:
        pair = step.found.data_set
        labels = data_set_labels(step.target.product, step.target.lane, pair) | pair_stamp(pair, step.generation)
        labels[PART_LABEL] = "set-aside"
        marker = self.client.volumes.create(f"launchplane-data-{pair}-set-aside", labels=labels)
        if not labels.items() <= marker.attrs["Labels"].items():
            raise ContainerHostError(f"The set-aside marker of {pair} has conflicting labels. Its data was left alone.")

    def run_initializer(self, step: Initialize, name: str, database: str, by: Witness) -> str | None:
        found = step.found
        schema = self.run_in(database, ["sh", "-ec", EMPTY_DATABASE])
        if schema.exit_code == 42:
            return "The database holds data or schema objects beyond a fresh database."
        if not schema.succeeded:
            raise ContainerHostError(f"Checking whether the database {database} is empty failed: {schema.output}")
        use = DataSetUse(found.data_set, found.networks[0].name, database, found.files[0])
        options = self.instance_options(step.target, name, use, step.generation, "initializer")
        once = replace(options, networks={use.network: []}, command=list(step.command), restart=RUNS_ONCE)
        writer = self.run_part(once, use, lambda on: self.logins.make(name, on, by), by)
        while True:
            writer.reload()
            if writer.status in STOPPED:
                code = writer.wait()["StatusCode"]
                if 0 < code < 128:
                    return writer.logs(tail=LOG_TAIL_LINES).decode(errors="replace") or f"Initializer exited {code}."
                return None
            by.hold()
            if by.cancel_asked():
                writer.stop(timeout=RETIRE_GRACE_SECONDS)
                return None
            self.sleep(POLL_SECONDS)

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
            labels = data_set_labels(product, lane, data_set) | pair_stamp(data_set, generation)
            self.create_network(f"launchplane-data-{data_set}", labels, quiet=True)

    def create_data_set_volume(
        self, product: str, lane: str, data_set: str, files: bool = False, copy_of: str | None = None, generation: str | None = None
    ) -> None:
        part, marks = ("files", {PART_LABEL: "files"}) if files else ("database", {})
        with attempting(f"Making the {part} volume of the data set {data_set}", self.address):
            copy = {} if copy_of is None else {COPY_OF_LABEL: copy_of}
            labels = data_set_labels(product, lane, data_set) | marks | copy | pair_stamp(data_set, generation)
            # Fetched while the lane still serves, not behind the back soon page.
            if copy_of is not None:
                self.ensure_image(COPIER_IMAGE)
            self.client.volumes.create(f"launchplane-data-{data_set}-{part}", labels=labels)

    # Whether anything can still write the volume is asked at the moment of the copy, of every container on the host.
    def copy_volume(self, copy: CopyVolume) -> None:
        with attempting(f"Copying the volume {copy.source} to {copy.destination}", self.address):
            mounted: Filters = {"volume": copy.source, "status": "running"}
            writers = self.client.containers.list(filters=mounted)
            if writers:
                names = ", ".join(sorted(str(each.name) for each in writers))
                raise ContainerHostRefused(f"{names} runs with the volume {copy.source} mounted, so it was not copied.")
            copier = self.client.containers.create(
                COPIER_IMAGE,
                COPY_COMMAND,
                name=f"{copy.destination}-copier",
                labels=data_set_labels(copy.product, copy.lane, copy.data_set) | {PART_LABEL: "copier"},
                volumes={copy.source: {"bind": "/from", "mode": "ro"}, copy.destination: {"bind": DATA_PATH, "mode": "rw"}},
                network_mode="none",
            )
            copier.start()
            exit_code = copier.wait(timeout=COPY_DEADLINE_SECONDS)["StatusCode"]
            if exit_code != 0:
                said = copier.logs(tail=LOG_TAIL_LINES).decode(errors="replace").strip() or "no log"
                raise ContainerHostRefused(f"The copy of {copy.source} to {copy.destination} ended with code {exit_code}: {said}")

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
            if found.initializer:
                self.retire_container(found.initializer)
            for part in (*found.databases, *found.copies):
                self.retire_container(part.container)
            for network in found.networks:
                with suppress(NotFound):
                    self.client.networks.get(network.name).remove()
            # Last, since a copy is known as one by its volumes: one Docker refuses to delete whole is still read as a copy.
            for volume in (*found.volumes, *found.files, *((found.set_aside,) if found.set_aside else ())):
                with suppress(NotFound):
                    self.client.volumes.get(volume).remove()

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
    def remove_unheld_logins(self, database: str, by: Witness = NOBODY) -> Unread | None:
        try:
            with attempting(f"Reading the logins of the database {database} and who holds them", self.address):
                unheld = self.logins.unheld(database)
        except ContainerHostError as error:
            return Unread(asked=f"Which logins of the database {database} no container holds", why=str(error))
        with attempting(f"Removing the logins no container holds from the database {database}", self.address):
            self.logins.remove(database, unheld, by)
        return None

    # How often Docker has started the container again by itself since it was started: its first process is the one
    # that runs only while this is 0.
    def restarts(self, container: str) -> int:
        with attempting(f"Reading how often Docker started {container} again", self.address):
            return checked(DOCKER_CONTAINER_DETAILS, self.describe(container), f"description of {container}").restart_count

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
