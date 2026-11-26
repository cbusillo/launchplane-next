from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from docker.errors import APIError
from requests import ReadTimeout

from launchplane.act import Lanes
from launchplane.container_host import Lane, PartStopped
from launchplane.data_set import login_of
from launchplane.docker_answers import (
    CommandResult,
    ContainerHostError,
    ContainerHostRefused,
    LoginRemovalRefused,
    attempting,
    sent,
)
from launchplane.gate import ALREADY, DONE, LOST, GateError
from launchplane.gate import Answer as GateAnswer
from launchplane.instance import (
    BACK_SOON,
    LANE_NETWORK,
    Conflicting,
    DataSet,
    DataSetDatabase,
    DataSetUse,
    HostNetwork,
    InstanceState,
    LaneBuild,
    LaneReading,
    LoadedRoute,
    Mount,
    OnHost,
    PartOfLane,
    ProxySeen,
    Reading,
    RouteStatus,
    RunningBackgroundService,
    RunningInstance,
    Unread,
    Verdict,
    VolumeCopy,
    WrittenRoute,
    background_service_container_of,
)
from launchplane.judge import Judgement, Work, judge
from launchplane.product import BackgroundService, Database, Product, ProductError
from launchplane.proxy import CheckFailed, ProxyError
from launchplane.steps import CopyResult, CopyVolume, FreshNames, MaintenanceResult, MoveResult, RetireResult, Verb
from launchplane.telling import NOBODY, Said, Witness

WEB_ADDRESS = "testing.site.test"
# The generations of a release under test and of one before it.
GENERATION = "01a1ef36-c51d-74d9-86a1-ff0199cf7a3d"
EARLIER = "01a1ef36-0000-7000-8000-000000000001"
SINCE = datetime(2026, 11, 16, 14, 2, tzinfo=UTC)
NOT_LAUNCHPLANES_ROUTE = Unread(asked="The routes file", why="it is not the route Launchplane writes")
LOGINS_NOT_TOLD = Unread(asked="The logins of the database database-d1", why="psql: no such database")
NO_GENERATION = Unread(asked="Which generation made new", why="the label is no generation")
QUIET = {LANE_NETWORK: True, "network-d1": True, "network-d2": True, "bridge": False}
WITH_DATA = Product(
    health_path="/health",
    database=Database(image="postgres:18"),
    background_service=BackgroundService(command=["work"]),
)


# Not an Exception: nothing in Launchplane may catch its own death and go on.
class LaunchplaneKilled(BaseException):
    pass


class PretendHost:
    def __init__(self) -> None:
        self.refuses_helpers = False
        self.instances: dict[str, RunningInstance] = {}
        self.background_services: dict[str, RunningBackgroundService] = {}
        self.data_sets: dict[str, DataSet] = {}
        self.networks: dict[str, bool] = {LANE_NETWORK: True}
        self.actions: list[str] = []
        self.lanes_read: list[Lane | None] = []
        self.most_background_services_running = 0
        self.background_services_that_stop: set[str] = set()
        self.killed_after: int | None = None
        self.databases_that_must_be_killed: set[str] = set()
        self.parts_that_will_not_start: set[str] = set()
        self.logins_left_in: dict[str, tuple[str, ...]] = {}
        self.holders_unread: set[str] = set()
        self.removals_refused: set[str] = set()
        self.commands_refused: set[str] = set()
        self.copies_refused: set[str] = set()
        # The builds whose update ends with an error, and those whose update runs and whose reply never comes.
        self.updates_that_fail: set[str | None] = set()
        self.updates_unanswered: set[str | None] = set()
        # The commands Docker does, once, and whose reply never comes.
        self.replies_lost: set[str] = set()
        # Each login a database server was sent the making or the removing of.
        self.logins_sent: list[str] = []
        # The logins whose next making or removing the server gives no answer to.
        self.logins_unanswered: set[str] = set()

    def record(self, action: str) -> None:
        self.actions.append(action)
        if len(self.actions) == self.killed_after:
            raise LaunchplaneKilled

    def change_database(self, container: str, **change: object) -> None:
        for name, found in self.data_sets.items():
            databases = tuple(each.model_copy(update=change) if each.container == container else each for each in found.databases)
            self.data_sets[name] = found.model_copy(update={"databases": databases})

    def place(
        self,
        target: LaneBuild,
        container: str,
        state: InstanceState = InstanceState.RUNNING,
        use: DataSetUse | None = None,
        generation: str | None = None,
    ) -> None:
        files = None if use is None else use.files
        path = target.run.files
        self.instances[container] = RunningInstance(
            product=target.product,
            lane=target.lane,
            build=target.build,
            container=container,
            state=state,
            networks=(LANE_NETWORK,) if use is None else (LANE_NETWORK, use.network),
            data_set=None if use is None else use.data_set,
            generation=generation,
            mounts=() if files is None or path is None else (Mount(source=files, path=path),),
        )

    def attach(self, container: str, network: str, *, quiet: bool) -> None:
        self.networks[network] = quiet
        found = self.instances[container]
        self.instances[container] = found.model_copy(update={"networks": (*found.networks, network)})

    def read(self, lane: Lane | None = None) -> OnHost:
        def of_lane[Part: PartOfLane](parts: Iterable[Part]) -> list[Part]:
            return [part for part in parts if lane in (None, (part.product, part.lane))]

        self.lanes_read.append(lane)
        networks = [HostNetwork(name=name, is_quiet=quiet) for name, quiet in self.networks.items()]
        return OnHost(
            of_lane(self.instances.values()), of_lane(self.background_services.values()), of_lane(self.data_sets.values()), networks
        )

    def unheld_logins(self, database: str) -> list[str]:
        servers = [each for found in self.data_sets.values() for each in found.databases if each.container == database]
        return [login for each in servers if isinstance(each.unheld_logins, tuple) for login in each.unheld_logins]

    # A pretend login is made or removed with its line written and answered as the container host does it. The server
    # refuses the removals a test has it refuse, by the name of the server or of the login.
    def change_login(self, by: Witness, verb: Verb, database: str, login: str) -> None:
        answered = by.sending(verb, database, login)
        self.logins_sent.append(f"{verb} {login}")
        if login in self.logins_unanswered:
            self.logins_unanswered.remove(login)
            answered(None, "psql: connection to server was lost")
            raise ContainerHostError(
                f"The database {database} did not {verb} the login {login}: psql: connection to server was lost"
            )
        if verb == "remove" and self.removals_refused & {database, login}:
            answered(Said.REFUSED, "it owns objects")
            raise LoginRemovalRefused(f"The database {database} did not remove the login {login}: it owns objects")
        answered(Said.CHANGED, None)

    def remove_unheld_logins(self, database: str, by: Witness = NOBODY) -> Unread | None:
        self.record(f"remove logins from {database}")
        if database in self.holders_unread:
            return Unread(asked=f"Which logins of the database {database} no container holds", why="timed out")
        for login in self.unheld_logins(database):
            self.change_login(by, "remove", database, login)
            self.change_database(database, unheld_logins=tuple(self.unheld_logins(database)[1:]))
        self.logins_left_in.pop(database, None)
        return None

    # A pretend command for a part writes its line and has its answer told as the container host does, and Docker
    # refuses the ones a test has it refuse.
    @contextmanager
    def command(self, by: Witness, verb: Verb, container: str, *, already: bool = False) -> Generator[None]:
        with attempting(f"The command to {verb} {container}", "test"), sent(by, verb, container, already=already):
            if f"{verb} {container}" in self.commands_refused:
                raise APIError("409 Conflict")
            yield
            if f"{verb} {container}" in self.replies_lost:
                self.replies_lost.remove(f"{verb} {container}")
                raise ReadTimeout("the connection was lost before the reply")

    def start_instance(
        self, target: LaneBuild, container: str, use: DataSetUse | None, generation: str | None = None, by: Witness = NOBODY
    ) -> None:
        if use is not None:
            self.change_login(by, "make", use.database_container, login_of(container))
        with self.command(by, "make", container):
            self.place(target, container, InstanceState.STOPPED, use, generation)
        with self.command(by, "start", container):
            self.place(target, container, use=use, generation=generation)
            self.record(f"start {container}")

    def start_background_service(
        self,
        target: LaneBuild,
        instance: str,
        use: DataSetUse | None,
        command: list[str],
        generation: str | None = None,
        by: Witness = NOBODY,
    ) -> None:
        container = background_service_container_of(instance)
        made = RunningBackgroundService(
            product=target.product,
            lane=target.lane,
            build=target.build,
            container=container,
            state=InstanceState.STOPPED,
            networks=(LANE_NETWORK,) if use is None else (use.network,),
            running_for_seconds=None,
            instance=instance,
            data_set=None if use is None else use.data_set,
            generation=generation,
        )
        with self.command(by, "make", container):
            self.background_services[container] = made
        with self.command(by, "start", container):
            self.change_part(container, InstanceState.RUNNING)
            self.most_background_services_running = max(self.most_background_services_running, len(self.background_services))
            self.record(f"start background service {instance} on {None if use is None else use.network} with {' '.join(command)}")

    def await_background_service(self, container: str, steady_seconds: float) -> None:
        self.record(f"await background service {container}")
        running = self.background_services[container]
        if running.build in self.background_services_that_stop:
            self.background_services[container] = running.model_copy(
                update={"state": InstanceState.RESTARTING, "running_for_seconds": None}
            )
            raise PartStopped(
                f"The background service {container} is not running: it stopped before it had run {steady_seconds:g} s."
            )
        self.background_services[container] = running.model_copy(update={"running_for_seconds": steady_seconds})

    def change_data_set(self, product: str, lane: str, data_set: str, **parts: object) -> None:
        found = self.data_sets.get(data_set) or DataSet(
            product=product, lane=lane, data_set=data_set, networks=(), volumes=(), databases=()
        )
        self.data_sets[data_set] = found.model_copy(update=parts)

    def create_data_set_network(self, product: str, lane: str, data_set: str, generation: str | None = None) -> None:
        self.networks[f"network-{data_set}"] = True
        network = HostNetwork(name=f"network-{data_set}", is_quiet=True)
        self.change_data_set(product, lane, data_set, networks=(network,), generation=generation)
        self.record(f"network {data_set}")

    def create_data_set_volume(
        self, product: str, lane: str, data_set: str, files: bool = False, copy_of: str | None = None, generation: str | None = None
    ) -> None:
        marks = {} if copy_of is None else {"copy_of": copy_of, "generation": generation}
        if files:
            self.change_data_set(product, lane, data_set, files=(f"files-{data_set}",), **marks)
        else:
            self.change_data_set(product, lane, data_set, volumes=(f"volume-{data_set}",), **marks)
        self.record(f"{'files' if files else 'volume'} {data_set}")

    # As Docker keeps it: the copier stays, and says whether it ran to its end. One that is refused wrote a part.
    def copy_volume(self, copy: CopyVolume) -> None:
        refused = copy.destination in self.copies_refused
        copier = VolumeCopy(container=f"{copy.destination}-copier", volume=copy.destination, finished=not refused, stopped=True)
        found = self.data_sets[copy.data_set]
        self.data_sets[copy.data_set] = found.model_copy(update={"copies": (*found.copies, copier)})
        self.record(f"copy {copy.source} to {copy.destination}")
        if refused:
            raise ContainerHostRefused(f"The copy of the volume {copy.source} to {copy.destination} ended with code 1")

    def create_database(self, product: str, lane: str, data_set: str, network: str, volume: str, image: str) -> None:
        database = DataSetDatabase(
            container=f"database-{data_set}",
            state=InstanceState.RUNNING,
            networks=(network,),
            accepts_connections=False,
            mounts=(Mount(source=volume, path="/var/lib/postgresql"),),
        )
        self.change_data_set(product, lane, data_set, databases=(database,))
        self.record(f"database {data_set} on {network} and {volume} from {image}")

    def start_database(self, container: str) -> None:
        self.change_database(container, state=InstanceState.RUNNING, exit_code=None, accepts_connections=False)
        self.record(f"start database {container}")

    def stop_database(self, container: str) -> None:
        exit_code = 137 if container in self.databases_that_must_be_killed else 0
        stopped = {"state": InstanceState.STOPPED, "exit_code": exit_code, "accepts_connections": None, "unheld_logins": None}
        self.change_database(container, **stopped)
        self.record(f"stop database {container}")

    def change_part(self, container: str, state: InstanceState) -> None:
        if container in self.background_services:
            ran = 0.0 if state == InstanceState.RUNNING else None
            self.background_services[container] = self.background_services[container].model_copy(
                update={"state": state, "running_for_seconds": ran}
            )
        else:
            self.instances[container] = self.instances[container].model_copy(update={"state": state})

    def is_in(self, container: str, state: InstanceState) -> bool:
        return (self.background_services.get(container) or self.instances[container]).state == state

    def start_part(self, container: str, by: Witness = NOBODY) -> None:
        if container in self.parts_that_will_not_start:
            raise ContainerHostRefused(f"Starting {container} on test was refused: no such image")
        with self.command(by, "start", container, already=self.is_in(container, InstanceState.RUNNING)):
            self.change_part(container, InstanceState.RUNNING)
            self.record(f"start {container}")

    def stop_part(self, container: str, by: Witness = NOBODY) -> None:
        with self.command(by, "stop", container, already=self.is_in(container, InstanceState.STOPPED)):
            self.change_part(container, InstanceState.STOPPED)
            self.record(f"stop {container}")

    def await_database(self, container: str) -> None:
        self.change_database(container, accepts_connections=True, unheld_logins=self.logins_left_in.get(container, ()))
        self.record(f"await {container}")

    def delete_data_set(self, found: DataSet) -> None:
        del self.data_sets[found.data_set]
        self.record(f"delete {found.data_set}")

    def retire_instance(self, container: str, by: Witness = NOBODY) -> None:
        with self.command(by, "stop", container, already=self.is_in(container, InstanceState.STOPPED)):
            self.change_part(container, InstanceState.STOPPED)
        with self.command(by, "remove", container):
            if container in self.background_services:
                del self.background_services[container]
            else:
                del self.instances[container]
            self.record(f"retire {container}")

    # What runs once is on the host as an instance of its build for as long as it runs, and is removed once it answered.
    def run_once(
        self,
        target: LaneBuild,
        container: str,
        use: DataSetUse,
        command: list[str],
        generation: str | None = None,
        by: Witness = NOBODY,
    ) -> CommandResult:
        self.change_login(by, "make", use.database_container, login_of(container))
        with self.command(by, "make", container), self.command(by, "start", container):
            self.place(target, container, use=use, generation=generation)
        self.record(f"update {container} with {' '.join(command)}")
        if target.build in self.updates_unanswered:
            raise ContainerHostError(f"Running {command[0]} in {container} on test failed: the reply was lost")
        with self.command(by, "stop", container, already=True), self.command(by, "remove", container):
            del self.instances[container]
        failed = target.build in self.updates_that_fail
        return CommandResult(exit_code=1, output="module not found") if failed else CommandResult(0, "")

    def read_log_tail(self, container: str) -> str:
        part = self.background_services.get(container) or self.instances[container]
        return f"{part.build} listening on :5678"


# What the gate keeps for one generation, as tests/test_gate.py shows the real one keeps it.
@dataclass
class Kept:
    armed: tuple[int, str] | None = None
    committed: str | None = None
    abandoned: bool = False
    fenced: int = -1
    revoked: set[str] = field(default_factory=set[str])

    def answer(self) -> GateAnswer:
        if self.abandoned:
            return GateAnswer(Verdict.ABANDONED)
        return GateAnswer(Verdict.NONE) if self.committed is None else GateAnswer(Verdict.COMMITTED, self.committed)

    def revoke(self, candidate: str) -> None:
        self.revoked.add(candidate)
        if self.armed is not None and self.armed[1] == candidate:
            self.armed = None


class PretendGate:
    def __init__(self, proxy: PretendProxy) -> None:
        self.proxy = proxy
        self.kept: dict[str, Kept] = {}
        self.unreadable: str | None = None

    def of(self, generation: str) -> Kept:
        if self.unreadable is not None:
            raise GateError(self.unreadable)
        if not self.proxy.running:
            raise GateError("The gate is a command in the proxy's container, which is not running")
        return self.kept.setdefault(generation, Kept())

    def verdict(self, web_address: str, generation: str) -> GateAnswer:
        assert web_address
        return self.of(generation).answer()

    def switch(self, web_address: str, generation: str, attempt: int, candidate: str) -> GateAnswer:
        kept = self.of(generation)
        later = kept.armed is None or kept.armed[1] == candidate or attempt > kept.armed[0]
        if kept.answer().verdict is Verdict.NONE and later and candidate not in kept.revoked and attempt > kept.fenced:
            if kept.armed is not None and kept.armed[1] != candidate:
                kept.revoke(kept.armed[1])
            kept.armed, kept.committed = (attempt, candidate), candidate
            self.proxy.written[web_address] = candidate
            self.proxy.edited[web_address] = (candidate, f"{candidate} {generation} {attempt}")
            self.proxy.host.record(f"route {candidate}")
        return kept.answer()

    def abandon(self, web_address: str, generation: str) -> GateAnswer:
        assert web_address
        kept = self.of(generation)
        kept.abandoned = kept.committed is None
        return kept.answer()

    def discard(self, web_address: str, generation: str, attempt: int, candidate: str) -> GateAnswer:
        assert web_address and attempt >= 0
        kept = self.of(generation)
        if kept.committed != candidate:
            kept.revoke(candidate)
        return kept.answer()

    def fence(self, web_address: str, generation: str, attempt: int) -> GateAnswer:
        assert web_address
        kept = self.of(generation)
        if kept.answer().verdict is Verdict.NONE:
            kept.fenced = max(kept.fenced, attempt)
            if kept.armed is not None and kept.armed[0] <= attempt:
                kept.revoke(kept.armed[1])
        return kept.answer()


# The gate as a test of calls that reach no move being worked is given it: nothing was ever asked of it.
class GateNeverAsked:
    answers = GateAnswer(Verdict.NONE)

    def abandon(self, product: str, lane: str, generation: str, /) -> str | None:
        raise AssertionError(f"the gate was asked to abandon {generation} of {lane} of {product}, where it answers {self.answers}")

    def fence(self, product: str, lane: str, generation: str, attempt: int, by: Witness = NOBODY, /) -> GateAnswer:
        assert product and lane and generation and attempt and by
        return self.answers

    def clean_up(self, product: str, lane: str, generation: str, attempt: int, witness: Witness, /, *, logins: bool) -> list[str]:
        assert self.answers.verdict is Verdict.NONE and product and lane and generation and attempt and witness and logins
        return []

    def copy(self, product: str, lane: str, generation: str, witness: Witness, /) -> CopyResult:
        raise AssertionError(f"a copy of {lane} of {product} was asked for as {generation} by {witness}")


# What the calls are given where no request is worked: no lane is ever asked to do anything.
class LanesNeverAsked(GateNeverAsked):
    def move(self, target: LaneBuild, generation: str, attempt: int, witness: Witness, /) -> MoveResult:
        raise AssertionError(f"a move to {target} was asked for as {generation}, attempt {attempt}, by {witness}")

    def retire(self, product: str, lane: str, delete_data_set: str | None, witness: Witness, /) -> RetireResult:
        raise AssertionError(f"a retire of {lane} of {product} was asked for, deleting {delete_data_set}, by {witness}")

    def begin_maintenance(self, product: str, lane: str, witness: Witness, /) -> MaintenanceResult:
        raise AssertionError(f"maintenance of {lane} of {product} was begun by {witness}")

    def end_maintenance(self, product: str, lane: str, witness: Witness, /) -> MaintenanceResult:
        raise AssertionError(f"maintenance of {lane} of {product} was ended by {witness}")


class PretendProxy:
    def __init__(self, host: PretendHost, running: bool = True, starts: bool = True) -> None:
        self.host = host
        self.running = running
        self.starts = starts
        self.back_soon_runs = True
        self.loaded: dict[str, LoadedRoute | Conflicting[LoadedRoute]] = {}
        self.written: dict[str, str] = {}
        self.edited: dict[str, tuple[str, str]] = {}
        self.gate = PretendGate(self)
        self.unread: dict[str, Unread] = {}
        # While held, a written route stays unloaded and the proxy goes on sending visitors where it did.
        self.holds_delivery = False
        self.failing: set[str] = set()
        self.failing_its_kind: set[str] = set()
        self.not_answering: dict[str, str] = {}
        self.asked: list[str] = []
        self.while_awaiting: Callable[[], None] = lambda: None
        self.while_checking: Callable[[str], None] = lambda _container: None

    def is_running(self) -> bool:
        return self.running

    def start(self) -> None:
        self.running = self.starts
        self.host.record("start proxy")

    def read(self) -> ProxySeen:
        return ProxySeen(
            runs=self.running,
            back_soon_runs=self.back_soon_runs,
            written={
                web_address: WrittenRoute(to, SINCE, text := self.installed(web_address), text.removeprefix(to).strip())
                for web_address, to in self.written.items()
            }
            | self.unread,
            loaded=dict(self.loaded) if self.running else {},
        )

    # A second router for the web address, as a routes file put beside Launchplane's makes the proxy load one.
    def route_twice(self, web_address: str, *containers: str) -> None:
        seen = tuple(LoadedRoute(each, RouteStatus.ENABLED, f"router-{number}@file") for number, each in enumerate(containers))
        self.loaded[web_address] = Conflicting(f"Where the proxy sends {web_address}", seen)

    def loaded_to(self, web_address: str) -> str:
        route = self.loaded[web_address]
        assert isinstance(route, LoadedRoute), route
        return route.container

    def route(self, web_address: str, container: str) -> None:
        self.written[web_address] = container
        self.loaded[web_address] = LoadedRoute(container, RouteStatus.ENABLED)

    def await_route(self, web_address: str, container: str | None, *, enabled: bool = False) -> None:
        assert enabled in (True, False)
        written = self.written.get(web_address)
        assert written == container, f"awaited {web_address} at {container}, written to {written}"
        if self.holds_delivery:
            raise ProxyError(f"The proxy did not load the routes file of {web_address} within 10 s.")
        twice = {each: route for each, route in self.loaded.items() if isinstance(route, Conflicting)}
        self.loaded = {each: LoadedRoute(to, RouteStatus.ENABLED) for each, to in self.written.items()} | twice
        self.while_awaiting()

    # What stands for the routes file: the container it sends visitors to and, once an edit put it in place, which edit.
    def installed(self, web_address: str) -> str:
        to = self.written.get(web_address, "")
        container, text = self.edited.get(web_address, ("", ""))
        return text if to and to == container else to

    # As the gate does it.
    def edit(self, web_address: str, generation: str, attempt: int, read: str, container: str | None) -> str:
        if container == BACK_SOON and self.host.refuses_helpers:
            raise ContainerHostRefused(f"Starting Launchplane's own container {BACK_SOON} was refused: pull access denied")
        text = "" if container is None else f"{container} {generation} {attempt}"
        installed = self.installed(web_address)
        if installed == text:
            return ALREADY
        if attempt <= self.gate.of(generation).fenced or installed != read:
            return LOST
        if container is None:
            del self.written[web_address]
        else:
            self.written[web_address], self.edited[web_address] = container, (container, text)
        self.host.record({None: "unroute", BACK_SOON: "show back soon"}.get(container, f"route {container}"))
        return DONE

    # As on the host, the two that pass the gate exclude each other: one finds what the other did.
    def switch(self, web_address: str, generation: str, attempt: int, candidate: str) -> GateAnswer:
        return self.gate.switch(web_address, generation, attempt, candidate)

    def check_instance(self, container: str, health_path: str, command: Sequence[str] = ()) -> None:
        self.host.record(f"check {container}{health_path}")
        self.while_checking(container)
        if container in self.failing:
            raise CheckFailed("Connection refused")
        if command and container in self.failing_its_kind:
            raise CheckFailed("The check of its kind failed")

    def why_web_address_fails(self, web_address: str) -> str | None:
        self.asked.append(web_address)
        return self.not_answering.get(web_address)


def numbered_names() -> Callable[[LaneBuild], FreshNames]:
    numbers = iter(range(1000))
    return lambda target: FreshNames(f"{target.product}-{target.lane}-{next(numbers)}", f"data-{next(numbers)}")


def lanes_on(proxy: PretendProxy, product: Product = WITH_DATA) -> Lanes:
    return Lanes(proxy.host, proxy, read_product=lambda _product: product, name_fresh=numbered_names())


def lane_instance(container: str, build: str | None, state: InstanceState = InstanceState.RUNNING) -> RunningInstance:
    return RunningInstance(product="site", lane="testing", build=build, container=container, state=state, networks=(LANE_NETWORK,))


def on_data(instance: RunningInstance, name: str, *extra_networks: str) -> RunningInstance:
    return instance.model_copy(update={"data_set": name, "networks": (LANE_NETWORK, f"network-{name}", *extra_networks)})


def data_set_named(
    name: str,
    state: InstanceState | None = InstanceState.RUNNING,
    *,
    quiet: bool | None = True,
    volume: bool = True,
    databases: tuple[str, ...] | None = None,
    accepts: bool = True,
    unheld: Sequence[str] = (),
    exit_code: int = 0,
) -> DataSet:
    servers = databases if databases is not None else (() if state is None else (f"database-{name}",))
    running = state == InstanceState.RUNNING
    return DataSet(
        product="site",
        lane="testing",
        data_set=name,
        networks=() if quiet is None else (HostNetwork(name=f"network-{name}", is_quiet=quiet),),
        volumes=(f"volume-{name}",) if volume else (),
        databases=tuple(
            DataSetDatabase(
                container=server,
                state=state or InstanceState.RUNNING,
                networks=(f"network-{name}",),
                accepts_connections=accepts if running else None,
                unheld_logins=tuple(unheld) if accepts and running else None,
                mounts=(Mount(source=f"volume-{name}", path="/var/lib/postgresql"),),
                exit_code=exit_code if state == InstanceState.STOPPED else None,
            )
            for server in servers
        ),
    )


def background_service_of(
    instance: RunningInstance,
    running_for: float | None = 10.0,
    container: str | None = None,
    networks: tuple[str, ...] | None = None,
) -> RunningBackgroundService:
    return RunningBackgroundService(
        product="site",
        lane="testing",
        build=instance.build,
        container=container or f"{instance.container}-background-service",
        state=InstanceState.RUNNING if running_for is not None else InstanceState.STOPPED,
        networks=networks or (f"network-{instance.data_set}",),
        running_for_seconds=running_for,
        instance=instance.container,
        data_set=instance.data_set,
    )


def changed[Part: (RunningInstance, RunningBackgroundService, DataSetDatabase)](part: Part, **change: object) -> Part:
    return part.model_copy(update=change)


def with_database(found: DataSet, **change: object) -> DataSet:
    return found.model_copy(update={"databases": (changed(found.databases[0], **change),)})


OLD, NEW, STRAY = lane_instance("old", "a"), lane_instance("new", "b"), lane_instance("stray", "c")
OLD_ON_D1, NEW_ON_D1 = on_data(OLD, "d1"), on_data(NEW, "d1")
D1 = data_set_named("d1")
OLD_BACKGROUND_SERVICE, NEW_BACKGROUND_SERVICE = background_service_of(OLD_ON_D1), background_service_of(NEW_ON_D1)


# The lane testing of site as one reading finds it: what every test of a decision or of the judge is given.
@dataclass(frozen=True)
class Given:
    instances: tuple[RunningInstance, ...] = ()
    route: tuple[str | LoadedRoute | None, str | None] = (None, None)
    background_services: tuple[RunningBackgroundService, ...] = ()
    data_sets: tuple[DataSet, ...] = ()
    proxy_runs: bool = True
    back_soon_runs: bool = True
    web_address_fails: str | None = None

    def proxy(self) -> PretendProxy:
        host = PretendHost()
        host.instances = {each.container: each for each in self.instances}
        host.background_services = {each.container: each for each in self.background_services}
        host.data_sets = {each.data_set: each for each in self.data_sets}
        host.networks = dict(QUIET)
        proxy = PretendProxy(host, running=self.proxy_runs)
        proxy.back_soon_runs = self.back_soon_runs
        loaded, written = self.route
        if loaded is not None:
            proxy.loaded[WEB_ADDRESS] = LoadedRoute(loaded, RouteStatus.ENABLED) if isinstance(loaded, str) else loaded
        if written is not None:
            proxy.written[WEB_ADDRESS] = written
        return proxy

    # Read as the status call reads: every lane, and the web address asked where the proxy sends it to a product.
    def reading(self) -> LaneReading:
        proxy = self.proxy()
        seen = proxy.read()
        asked = {each: self.web_address_fails for each, to in seen.sends_visitors_to().items() if to != BACK_SOON}
        return Reading(SINCE, seen, proxy.host.read(), asked).lane("site", "testing")

    def judged(self, work: Work | None, product: Product | ProductError = WITH_DATA) -> Judgement:
        return judge(self.reading(), product, work)
