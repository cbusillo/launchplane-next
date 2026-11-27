from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum, auto

from pydantic import BaseModel

from launchplane.data_set import DATA_PATH
from launchplane.instance import (
    BACK_SOON,
    LANE_NETWORK,
    Conflicting,
    DataSet,
    DataSetDatabase,
    InstanceState,
    LaneReading,
    LaneRoute,
    LoadedRoute,
    Mount,
    Reading,
    RouteStatus,
    RunningBackgroundService,
    RunningInstance,
    Stamp,
    Unread,
    web_address_of,
)
from launchplane.product import Product, ProductError

BACKGROUND_SERVICE_STEADY_SECONDS = 3.0


class Work(StrEnum):
    MOVE = "moving"
    RETIRE = "retiring"
    BEGIN_MAINTENANCE = "beginning maintenance"
    END_MAINTENANCE = "ending maintenance"
    COPY = "copying"


# Launchplane's record could not be read, so whether a request is at work on a lane is not known.
@dataclass(frozen=True)
class WorkUnread:
    why: str


class Kind(StrEnum):
    UNREAD = "could not be read"
    STAMP_UNREAD = "says a generation made it that is none"
    NO_VISITORS = "no instance receives visitors"
    PROXY_STOPPED = "the proxy is not running"
    ROUTE_NOT_LOADED = "the proxy has not loaded the routes file"
    ROUTE_DISABLED = "the proxy reports the route not enabled"
    ROUTE_UNREAD = "the routes file is not one Launchplane writes"
    ROUTES_CONFLICT = "the proxy has more than one route for the web address"
    NOT_ANSWERING = "the web address does not answer"
    BACK_SOON_STOPPED = "the back soon page is not running"
    NO_BUILD = "no build label"
    STOPPED = "stopped"
    NOT_RUNNING = "neither running nor stopped"
    STILL_UP = "not stopped in maintenance"
    KILLED = "did not stop cleanly"
    WRONG_DATA_SET = "labelled for a data set it cannot have"
    MISPLACED = "attached or mounted where it must not be"
    BESIDE = "beside the active instance"
    UNWANTED = "a background service the product has none of"
    NO_BACKGROUND_SERVICE = "no background service"
    DOUBLED = "more than one"
    OF_ANOTHER_INSTANCE = "of another instance"
    OF_NO_ACTIVE_INSTANCE = "of an instance that receives no visitors"
    OF_ANOTHER_BUILD = "of another build"
    UNSTEADY = "not yet up as long as it must stay up"
    NO_NETWORK = "no network"
    NO_VOLUME = "no volume"
    NO_FILES = "no files volume"
    NOT_ITS_BUILD = "runs an image that is not its build"
    NO_DATABASE = "no database server"
    NOT_QUIET = "network not quiet"
    NOT_ACCEPTING = "does not accept connections"
    NOT_ASKED = "not asked whether it accepts connections"
    LOGINS_UNREAD = "logins could not be read"
    LOGIN_UNHELD = "a login no container holds"


LOGIN_KINDS = {Kind.LOGINS_UNREAD, Kind.LOGIN_UNHELD}


class Weight(Enum):
    # Wrong whatever a call is doing.
    FAULT = auto()
    # Wrong, and under way while a call is at work on the lane.
    WRONG = auto()
    # A state a call passes through: under way while one is at work, left unfinished when none is.
    UNFINISHED = auto()
    UNREAD = auto()
    CONFLICT = auto()


# An unread answer and a conflicting one are each shown in their own word, whatever a call is doing.
class Shown(StrEnum):
    WRONG = "wrong"
    UNDER_WAY = "under way"
    LEFT_UNFINISHED = "left unfinished"
    UNREAD = "could not be read"
    CONFLICTING = "conflicting"


class Standing(StrEnum):
    GOOD = "good"
    WRONG = "wrong"
    ON_PURPOSE = "on purpose"
    UNREAD = "could not be read"
    CONFLICTING = "conflicting"


class Condition(StrEnum):
    GOOD = "good"
    IN_MAINTENANCE = "in maintenance"
    UNDER_WAY = "work under way"
    UNFINISHED = "unfinished"
    WRONG = "wrong"
    UNREAD = "could not be read"
    CONFLICTING = "conflicting"


@dataclass(frozen=True)
class Found:
    weight: Weight
    subject: str
    kind: Kind
    sentence: str


class Finding(BaseModel, frozen=True):
    subject: str
    kind: Kind
    sentence: str
    shown: Shown


@dataclass(frozen=True)
class Judgement:
    lane: LaneReading
    work: Work | None
    condition: Condition
    findings: tuple[Finding, ...]
    parts: Mapping[str, Standing]
    work_unread: bool = False

    def about(self, subjects: Collection[str]) -> list[Finding]:
        return [finding for finding in self.findings if finding.subject in subjects]

    def of_kind(self, *kinds: Kind) -> list[Finding]:
        return [finding for finding in self.findings if finding.kind in kinds]

    # Why the lane is not as it was before a release: a stamp that is unread or its own, or logins untold or without a holder.
    def left_by(self, generation: str) -> list[str]:
        made = [part.generation for part in (*self.lane.instances, *self.lane.background_services, *self.lane.data_sets)]
        unread = [str(stamp) for stamp in made if not isinstance(stamp, str | None)]
        logins = [finding.sentence for finding in self.of_kind(*LOGIN_KINDS, Kind.NOT_ACCEPTING, Kind.NOT_ASKED)]
        return [*unread, *logins, *(["A part its release made is on the lane"] * (generation in made))]

    # What is wrong with the instance itself. Starting another in its place does not make a web address answer.
    def about_instance(self, instance: RunningInstance) -> list[Finding]:
        return [finding for finding in self.about({instance.container}) if finding.kind not in {Kind.BESIDE, Kind.NOT_ANSWERING}]

    # Its logins are settled last, so they do not stand in the way of using it.
    def about_data_set(self, found: DataSet | None) -> list[Finding]:
        return [finding for finding in self.about(subjects_of(found)) if finding.kind not in LOGIN_KINDS]

    def about_background_services(self) -> list[Finding]:
        containers = {running.container for running in self.lane.background_services}
        return [finding for finding in self.findings if finding.subject in containers or finding.kind == Kind.NO_BACKGROUND_SERVICE]

    def shown_as(self, *shown: Shown) -> list[str]:
        return [finding.sentence for finding in self.findings if finding.shown in shown]


def subjects_of(found: DataSet | None) -> set[str]:
    return set() if found is None else {found.data_set, *(database.container for database in found.databases)}


@dataclass(frozen=True)
class Judged:
    lane: LaneReading
    product: Product | None
    stopped_is_meant: bool
    serving: RunningInstance | None

    def network_names(self, data_set: str | None) -> set[str]:
        found = self.lane.data_set(data_set)
        return set() if found is None else {network.name for network in found.networks}

    # Beside the data set the lane serves from, one no instance runs against is kept, and its server is meant to be stopped.
    def is_kept_offline(self, data_set: str) -> bool:
        used = {instance.data_set for instance in self.lane.instances}
        return self.serving is not None and data_set not in used

    # Where the product keeps files: its data set's files volume, mounted in the instance.
    def files_mounts(self, data_set: str | None) -> tuple[Mount, ...]:
        found, path = self.lane.data_set(data_set), None if self.product is None else self.product.run.files
        return () if found is None or path is None else tuple(Mount(source=volume, path=path) for volume in found.files)


type Part = RunningInstance | RunningBackgroundService | DataSetDatabase


def attachment_findings(part: str, subject: str, attached: tuple[str, ...], expected: set[str], lane: LaneReading) -> list[Found]:
    extra = sorted(set(attached) - expected)
    missing = sorted(expected - set(attached))
    reaching_out = sorted(name for name in attached if lane.network_quiet.get(name) is False)
    unread = sorted(name for name in attached if name not in lane.network_quiet)
    sentences = [
        *([f"{part} is attached to {', '.join(extra)}, which it must not be"] if extra else []),
        *([f"{part} is not attached to {', '.join(missing)}"] if missing else []),
        *([f"{part} can reach out through {', '.join(reaching_out)}"] if reaching_out else []),
    ]
    why = f"{part} is attached to {', '.join(unread)}, which was not among the networks read, so whether it can reach out is unknown"
    return [
        *[Found(Weight.FAULT, subject, Kind.MISPLACED, sentence) for sentence in sentences],
        *([Found(Weight.UNREAD, subject, Kind.UNREAD, why)] if unread else []),
    ]


def described(mounts: Counter[Mount]) -> str:
    return ", ".join(
        f"{mount.source} mounted at {mount.path}" + (f" {times} times" if times > 1 else "")
        for mount, times in sorted(mounts.items(), key=lambda counted: (counted[0].path, counted[0].source))
    )


def mount_findings(part: str, subject: str, mounts: tuple[Mount, ...], expected: tuple[Mount, ...]) -> list[Found]:
    extra = Counter(mounts) - Counter(expected)
    missing = Counter(expected) - Counter(mounts)
    sentences = [
        *([f"{part} has {described(extra)}, which it must not have"] if extra else []),
        *([f"{part} does not have {described(missing)}"] if missing else []),
    ]
    return [Found(Weight.FAULT, subject, Kind.MISPLACED, sentence) for sentence in sentences]


def state_findings(part: str, each: Part, of: Judged, when_stopped: Weight) -> list[Found]:
    if of.stopped_is_meant:
        if each.state == InstanceState.STOPPED:
            return []
        return [Found(Weight.WRONG, each.container, Kind.STILL_UP, f"{part} is {each.state}, in a lane in maintenance")]
    if each.state == InstanceState.RUNNING:
        return []
    kind, weight = (Kind.STOPPED, when_stopped) if each.state == InstanceState.STOPPED else (Kind.NOT_RUNNING, Weight.WRONG)
    return [Found(weight, each.container, kind, f"{part} is {each.state}")]


def data_set_label_findings(part: str, instance: RunningInstance, of: Judged) -> list[Found]:
    if of.product is None:
        return []
    if of.product.database is None:
        sentence = (
            None if instance.data_set is None else f"{part} runs against the data set {instance.data_set}; the product has none"
        )
    elif instance.data_set is None:
        sentence = f"{part} runs against no data set"
    elif of.lane.data_set(instance.data_set) is None:
        sentence = f"{part} runs against the data set {instance.data_set}, which is not on the host"
    else:
        sentence = None
    return [] if sentence is None else [Found(Weight.WRONG, instance.container, Kind.WRONG_DATA_SET, sentence)]


# A stamp that is not a generation is a fault of the part that carries it, and of nothing else on the host.
def stamp_findings(subject: str, stamp: Stamp) -> list[Found]:
    return [Found(Weight.FAULT, subject, Kind.STAMP_UNREAD, str(stamp))] if isinstance(stamp, Unread | Conflicting) else []


def instance_findings(instance: RunningInstance, of: Judged) -> list[Found]:
    part, subject = f"The {instance.called} {instance.container}", instance.container
    serves = of.serving is not None and of.serving.container == subject
    active, other = of.lane.active, instance.other_image
    beside = active is not None and active.container != subject
    # What runs once is on its data set's network alone: nobody is sent to it.
    of_the_lane: set[str] = set() if instance.runs_once else {LANE_NETWORK}
    networks = of_the_lane | of.network_names(instance.data_set)
    return [
        *([Found(Weight.WRONG, subject, Kind.NO_BUILD, f"{part} does not say which build it is")] if instance.build is None else []),
        *stamp_findings(subject, instance.generation),
        *state_findings(part, instance, of, Weight.WRONG if serves else Weight.UNFINISHED),
        *data_set_label_findings(part, instance, of),
        *attachment_findings(part, subject, instance.networks, networks, of.lane),
        *mount_findings(part, subject, instance.mounts, of.files_mounts(instance.data_set)),
        *([Found(Weight.UNFINISHED, subject, Kind.BESIDE, f"{part} runs beside the active one")] if beside else []),
        *([Found(Weight.FAULT, subject, Kind.NOT_ITS_BUILD, f"{part} runs {other}, which is not its build")] if other else []),
    ]


def doubled_parts(found: DataSet) -> list[str]:
    parts: dict[str, tuple[str, ...]] = {
        "networks": tuple(network.name for network in found.networks),
        "volumes": found.volumes,
        "files volumes": found.files,
        "database servers": tuple(database.container for database in found.databases),
    }
    return [f"{len(names)} {kind} ({', '.join(names)})" for kind, names in parts.items() if len(names) > 1]


def database_state_findings(part: str, database: DataSetDatabase, of: Judged, kept_offline: bool) -> list[Found]:
    subject = database.container
    if of.stopped_is_meant and database.state == InstanceState.STOPPED and database.exit_code != 0:
        sentence = f"{part} did not stop cleanly: it exited with code {database.exit_code}, so it was killed or it crashed"
        return [Found(Weight.FAULT, subject, Kind.KILLED, sentence)]
    if kept_offline and (database.state, database.exit_code) == (InstanceState.STOPPED, 0):
        return []
    if of.stopped_is_meant or database.state != InstanceState.RUNNING:
        return state_findings(part, database, of, Weight.UNFINISHED)
    if database.accepts_connections is None:
        return [Found(Weight.UNREAD, subject, Kind.NOT_ASKED, f"{part} was not asked whether it accepts connections")]
    if not database.accepts_connections:
        return [Found(Weight.UNFINISHED, subject, Kind.NOT_ACCEPTING, f"{part} does not accept connections")]
    return []


def database_findings(found: DataSet, database: DataSetDatabase, of: Judged) -> list[Found]:
    part, subject = f"The database {database.container}", database.container
    names = {network.name for network in found.networks}
    if of.product is None:
        mounted: list[Found] = []
    else:
        own = tuple(Mount(source=volume, path=DATA_PATH) for volume in found.volumes) if of.product.database is not None else ()
        mounted = mount_findings(part, subject, database.mounts, own)
    return [
        *database_state_findings(part, database, of, of.is_kept_offline(found.data_set)),
        *attachment_findings(part, subject, database.networks, names, of.lane),
        *mounted,
    ]


def data_set_findings(found: DataSet, of: Judged) -> list[Found]:
    part, subject = f"The data set {found.data_set}", found.data_set
    missing = [
        (kind, word)
        for kind, word, parts in (
            (Kind.NO_NETWORK, "network", found.networks),
            (Kind.NO_VOLUME, "volume", found.volumes),
            (Kind.NO_DATABASE, "database", found.databases),
        )
        # A copy is kept offline: it has a network and a server only once something is to run on it.
        if not parts and (found.copy_of is None or found.networks or found.databases)
    ]
    if of.product is not None and of.product.run.files and not found.files and found.copy_of is None:
        missing.append((Kind.NO_FILES, "files volume"))
    return [
        *stamp_findings(subject, found.generation),
        *[
            Found(Weight.WRONG, subject, Kind.DOUBLED, f"{part} has {doubled} where it must have one")
            for doubled in doubled_parts(found)
        ],
        *[Found(Weight.UNFINISHED, subject, kind, f"{part} has no {word}") for kind, word in missing],
        *[
            Found(Weight.WRONG, subject, Kind.NOT_QUIET, f"{part}'s network {network.name} is not quiet")
            for network in found.networks
            if not network.is_quiet
        ],
        *[finding for database in found.databases for finding in database_findings(found, database, of)],
    ]


def connection_findings(running: RunningBackgroundService, beside: RunningInstance, of: Judged) -> list[Found]:
    part, subject = f"The background service {running.container}", running.container
    expected = of.network_names(beside.data_set) if of.lane.data_set(beside.data_set) is not None else {LANE_NETWORK}
    labelled = f"{part} says it runs against the data set {running.data_set}, not {beside.data_set}"
    return [
        *([] if running.data_set == beside.data_set else [Found(Weight.FAULT, subject, Kind.MISPLACED, labelled)]),
        *attachment_findings(part, subject, running.networks, expected, of.lane),
        *mount_findings(part, subject, running.mounts, ()),
    ]


def steadiness_findings(part: str, running: RunningBackgroundService, of: Judged) -> list[Found]:
    if of.stopped_is_meant:
        return []
    if running.state != InstanceState.RUNNING or running.running_for_seconds is None:
        stopped = running.state == InstanceState.STOPPED
        kind, weight = (Kind.STOPPED, Weight.UNFINISHED) if stopped else (Kind.NOT_RUNNING, Weight.WRONG)
        return [Found(weight, running.container, kind, f"{part} is {running.state}")]
    if running.running_for_seconds < BACKGROUND_SERVICE_STEADY_SECONDS:
        sentence = (
            f"{part} has run {running.running_for_seconds:.1f} s of the {BACKGROUND_SERVICE_STEADY_SECONDS:g} s it must stay up"
        )
        return [Found(Weight.UNFINISHED, running.container, Kind.UNSTEADY, sentence)]
    return []


# Which instance a background service must belong to, and whether it does.
def belonging_findings(part: str, running: RunningBackgroundService, owner: RunningInstance | None, of: Judged) -> list[Found]:
    subject = running.container
    if owner is None:
        sentence = f"{part} belongs to {running.instance or 'no instance'}, which is not on the host"
        return [Found(Weight.WRONG, subject, Kind.OF_ANOTHER_INSTANCE, sentence)]
    if of.serving is None:
        # In maintenance no instance receiving visitors is meant, and which instance will again is not known of several.
        # Under two routers which instance receives them is not known, and is not said to be none.
        if of.lane.route.in_maintenance or isinstance(of.lane.route.loaded, Conflicting):
            return []
        sentence = f"{part} belongs to {owner.container}, which does not receive visitors: no instance does"
        return [Found(Weight.UNFINISHED, subject, Kind.OF_NO_ACTIVE_INSTANCE, sentence)]
    if owner.container != of.serving.container:
        sentence = f"{part} belongs to {owner.container}, not {of.serving.container}"
        return [Found(Weight.UNFINISHED, subject, Kind.OF_ANOTHER_INSTANCE, sentence)]
    if of.serving.build in (None, running.build):
        return []
    return [Found(Weight.WRONG, subject, Kind.OF_ANOTHER_BUILD, f"{part} runs {running.build}, not {of.serving.build}")]


# Each is judged for itself, however many the lane has: one more than it must have makes none of them good.
def one_background_service_findings(running: RunningBackgroundService, of: Judged) -> list[Found]:
    part = f"The background service {running.container}"
    owner = next((instance for instance in of.lane.instances if instance.container == running.instance), None)
    still_up = state_findings(part, running, of, Weight.UNFINISHED) if of.stopped_is_meant else []
    beside = owner or of.serving
    connected = [] if beside is None else connection_findings(running, beside, of)
    belonging = belonging_findings(part, running, owner, of)
    of_the_serving_instance = of.serving is not None and not belonging
    return [
        *stamp_findings(running.container, running.generation),
        *belonging,
        *still_up,
        *connected,
        *(steadiness_findings(part, running, of) if of_the_serving_instance else []),
    ]


def background_service_count_findings(containers: list[str], of: Judged) -> list[Found]:
    if of.serving is None or of.product is None:
        return []
    if of.product.background_service is None:
        sentences = [(container, f"The background service {container} runs; the product has none") for container in containers]
        return [Found(Weight.WRONG, container, Kind.UNWANTED, sentence) for container, sentence in sentences]
    if len(containers) == 1:
        return []
    must = f"where it must have one, belonging to {of.serving.container}"
    if not containers:
        return [
            Found(
                Weight.UNFINISHED,
                of.lane.web_address,
                Kind.NO_BACKGROUND_SERVICE,
                f"The lane has 0 background services (none) {must}",
            )
        ]
    names = ", ".join(containers)
    return [
        Found(
            Weight.WRONG, container, Kind.DOUBLED, f"The background service {container} is one of {len(containers)} ({names}) {must}"
        )
        for container in containers
    ]


def background_service_findings(of: Judged) -> list[Found]:
    running = sorted(of.lane.background_services, key=lambda each: each.container)
    return [
        *background_service_count_findings([each.container for each in running], of),
        *[finding for each in running for finding in one_background_service_findings(each, of)],
    ]


def login_findings(lane: LaneReading) -> list[Found]:
    findings: list[Found] = []
    for database in lane.databases:
        subject, unheld = database.container, database.unheld_logins
        if database.state != InstanceState.RUNNING or not database.accepts_connections:
            continue
        if isinstance(unheld, tuple):
            held_by_none = [f"The database {subject} has the login {login}, which no container holds" for login in unheld]
            findings += [Found(Weight.UNFINISHED, subject, Kind.LOGIN_UNHELD, sentence) for sentence in held_by_none]
        else:
            sentence = str(unheld or Unread(asked=f"The logins of the database {subject}", why="they were not asked for"))
            findings.append(Found(Weight.UNREAD, subject, Kind.LOGINS_UNREAD, sentence))
    return findings


def is_loaded_as_written(route: LaneRoute) -> bool:
    return (route.loaded.container if isinstance(route.loaded, LoadedRoute) else route.loaded) == route.written


def does_not_answer(web_address: str, failure: str) -> str:
    return f"{web_address} does not answer with success: {failure}"


def proxy_findings(lane: LaneReading) -> list[Found]:
    route, web_address = lane.route, lane.web_address
    if not route.proxy_runs:
        down = (
            f"The proxy is not running, so {web_address} answers nothing, not the back soon page"
            if route.in_maintenance
            else "No instance receives visitors: the proxy is not running"
        )
        return [Found(Weight.UNFINISHED, web_address, Kind.PROXY_STOPPED, down)]
    loaded = route.loaded
    if isinstance(loaded, Conflicting):
        return [Found(Weight.CONFLICT, web_address, Kind.ROUTES_CONFLICT, str(loaded))]
    unloaded = (
        f"The proxy has not loaded the routes file of {web_address}: the file sends it to {route.written or 'no instance'} "
        f"and the proxy to {'no instance' if loaded is None else loaded.container}"
    )
    return [
        *(
            []
            if route.in_maintenance or lane.active is not None
            else [Found(Weight.UNFINISHED, web_address, Kind.NO_VISITORS, "No instance receives visitors")]
        ),
        *([] if is_loaded_as_written(route) else [Found(Weight.UNFINISHED, web_address, Kind.ROUTE_NOT_LOADED, unloaded)]),
        *(
            []
            if loaded is None or loaded.status == RouteStatus.ENABLED
            else [
                Found(
                    Weight.WRONG,
                    web_address,
                    Kind.ROUTE_DISABLED,
                    f"The proxy has loaded the route of {web_address} to {loaded.container} and reports it {loaded.status}, "
                    "so the web address reaches no instance",
                )
            ]
        ),
    ]


def route_findings(lane: LaneReading) -> list[Found]:
    page = f"The back soon page's container {BACK_SOON} is not running, so the web address answers the proxy's own error"
    page_down = lane.route.in_maintenance and not lane.route.back_soon_runs
    active, failure = lane.active, lane.web_address_failure
    file = lane.route.file
    return [
        *([Found(Weight.FAULT, lane.web_address, Kind.ROUTE_UNREAD, str(file))] if isinstance(file, Unread) else []),
        *proxy_findings(lane),
        *([Found(Weight.FAULT, BACK_SOON, Kind.BACK_SOON_STOPPED, page)] if page_down else []),
        *(
            []
            if active is None or failure is None
            else [Found(Weight.WRONG, active.container, Kind.NOT_ANSWERING, does_not_answer(lane.web_address, failure))]
        ),
    ]


# In maintenance no instance receives visitors, and a lane's one instance is the one that will again.
def serving_instance(lane: LaneReading) -> RunningInstance | None:
    web = [each for each in lane.instances if not each.runs_once]
    if lane.active is not None or not lane.route.in_maintenance or len(web) != 1:
        return lane.active
    return web[0]


def everything_found(lane: LaneReading, product: Product | ProductError, work: Work | WorkUnread | None) -> list[Found]:
    described_product = None if isinstance(product, ProductError) else product
    stopped_is_meant = lane.route.in_maintenance and work is not Work.END_MAINTENANCE
    of = Judged(lane, described_product, stopped_is_meant, serving_instance(lane))
    served = None if of.serving is None else of.serving.container
    used = None if of.serving is None else of.serving.data_set
    others = sorted((instance for instance in lane.instances if instance.container != served), key=lambda each: each.container)
    return [
        *([Found(Weight.UNREAD, lane.product, Kind.UNREAD, str(product))] if described_product is None else []),
        *([Found(Weight.UNREAD, lane.web_address, Kind.UNREAD, work_unread(work))] if isinstance(work, WorkUnread) else []),
        *route_findings(lane),
        *([] if of.serving is None else instance_findings(of.serving, of)),
        *[finding for found in lane.data_sets if found.data_set == used for finding in data_set_findings(found, of)],
        *background_service_findings(of),
        *[finding for instance in others for finding in instance_findings(instance, of)],
        *[finding for found in lane.data_sets if found.data_set != used for finding in data_set_findings(found, of)],
        *login_findings(lane),
    ]


def work_unread(work: WorkUnread) -> str:
    return f"Whether a request is at work on this lane is not known, so what it would explain shows as could not be read: {work.why}"


# What is wrong only with no request at work is neither wrong nor under way while that is not known.
def shown_for(weight: Weight, work: Work | WorkUnread | None) -> Shown:
    if weight == Weight.CONFLICT:
        return Shown.CONFLICTING
    if weight == Weight.FAULT:
        return Shown.WRONG
    if weight == Weight.UNREAD or isinstance(work, WorkUnread):
        return Shown.UNREAD
    if work is not None:
        return Shown.UNDER_WAY
    return Shown.WRONG if weight == Weight.WRONG else Shown.LEFT_UNFINISHED


def standing_of(shown_about: set[Shown], unremarked: Standing) -> Standing:
    if shown_about & {Shown.WRONG, Shown.LEFT_UNFINISHED}:
        return Standing.WRONG
    if Shown.UNREAD in shown_about:
        return Standing.UNREAD
    return Standing.ON_PURPOSE if Shown.UNDER_WAY in shown_about else unremarked


def condition_of(lane: LaneReading, work: Work | WorkUnread | None, shown_in_lane: set[Shown]) -> Condition:
    if Shown.CONFLICTING in shown_in_lane:
        return Condition.CONFLICTING
    if Shown.WRONG in shown_in_lane:
        return Condition.WRONG
    if Shown.UNREAD in shown_in_lane:
        return Condition.UNREAD
    if work is not None:
        return Condition.UNDER_WAY
    if Shown.LEFT_UNFINISHED in shown_in_lane:
        return Condition.UNFINISHED
    return Condition.IN_MAINTENANCE if lane.route.in_maintenance else Condition.GOOD


def judge(lane: LaneReading, product: Product | ProductError, work: Work | WorkUnread | None) -> Judgement:
    findings = tuple(
        Finding(subject=each.subject, kind=each.kind, sentence=each.sentence, shown=shown_for(each.weight, work))
        for each in everything_found(lane, product, work)
    )
    if isinstance(product, ProductError):
        unremarked = Standing.UNREAD
    else:
        unremarked = Standing.ON_PURPOSE if lane.route.in_maintenance and work is not Work.END_MAINTENANCE else Standing.GOOD
    parts = [*lane.instances, *lane.background_services, *lane.databases]
    return Judgement(
        lane,
        work if isinstance(work, Work) else None,
        condition_of(lane, work, {finding.shown for finding in findings}),
        findings,
        {
            part.container: standing_of({finding.shown for finding in findings if finding.subject == part.container}, unremarked)
            for part in parts
        },
        isinstance(work, WorkUnread),
    )


class WebAddressJudged(BaseModel):
    web_address: str
    product: str | None
    lane: str | None
    sends_to_container: str | None
    answers: bool
    shows_back_soon_page: bool
    error: str | None
    says: str
    standing: Standing


ANSWERING, BACK_SOON_SHOWN, UNASKED = "answering", "shows the back soon page", "was not asked"


def why_not_answering(web_address: str, reading: Reading, sends_to: str | None) -> str | None:
    if not reading.proxy.runs:
        return f"The proxy is not running, so {web_address} answers nothing."
    if sends_to is None:
        return f"The proxy sends {web_address} to no instance."
    if sends_to == BACK_SOON:
        return None if reading.proxy.back_soon_runs else f"The back soon page's container {BACK_SOON} is not running."
    failure = reading.web_address_failures[web_address]
    return None if failure is None else does_not_answer(web_address, failure)


def web_address_judged(web_address: str, owner: tuple[str, str | None] | None, reading: Reading) -> WebAddressJudged:
    sends_to = reading.proxy.sends_visitors_to().get(web_address) if reading.proxy.runs else None
    loaded = reading.proxy.loaded.get(web_address)
    if isinstance(loaded, Conflicting):
        error, says, standing = str(loaded), Standing.CONFLICTING, Standing.CONFLICTING
    elif sends_to not in (None, BACK_SOON) and web_address not in reading.web_address_failures:
        error, says, standing = f"{web_address} was not asked, so whether it answers is unknown.", UNASKED, Standing.UNREAD
    else:
        error = why_not_answering(web_address, reading, sends_to)
        answering = (BACK_SOON_SHOWN, Standing.ON_PURPOSE) if sends_to == BACK_SOON else (ANSWERING, Standing.GOOD)
        says, standing = answering if error is None else ("not answering", Standing.WRONG)
    return WebAddressJudged(
        web_address=web_address,
        product=None if owner is None else owner[0],
        lane=None if owner is None else owner[1],
        sends_to_container=sends_to,
        answers=says == ANSWERING,
        shows_back_soon_page=says == BACK_SOON_SHOWN,
        error=error,
        says=says,
        standing=standing,
    )


def web_addresses_judged(reading: Reading) -> list[WebAddressJudged]:
    by_container = {instance.container: instance for instance in reading.parts.instances}
    owners: dict[str, tuple[str, str | None] | None] = {
        web_address: None if (instance := by_container.get(container)) is None else (instance.product, instance.lane)
        for web_address, container in reading.proxy.sends_visitors_to().items()
    }
    owners |= {web_address: None for web_address, route in reading.proxy.loaded.items() if isinstance(route, Conflicting)}
    for product, lane in reading.lanes():
        owners[web_address_of(product, lane)] = (product, lane)
    return [web_address_judged(web_address, owners[web_address], reading) for web_address in sorted(owners)]
