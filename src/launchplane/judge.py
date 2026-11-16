from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from launchplane.data_set import DATA_PATH
from launchplane.instance import (
    BACK_SOON,
    LANE_NETWORK,
    DataSet,
    DataSetDatabase,
    InstanceState,
    LaneBuild,
    LaneSeen,
    Mount,
    RunningBackgroundService,
    RunningInstance,
)
from launchplane.product import Product, ProductError

BACKGROUND_SERVICE_STEADY_SECONDS = 3.0


@dataclass(frozen=True)
class DataSetProblem:
    text: str
    fixable: bool = False


class Fix(StrEnum):
    START = "start one"
    REPLACE = "retire it, and start one if the product has a background service"
    REPLACE_OR_WATCH = "retire it, unless this move started it and is about to watch it"
    WATCH = "watch it until it has stayed up"


@dataclass(frozen=True)
class BackgroundServiceProblem:
    text: str
    fix: Fix
    container: str | None = None
    of_a_release: bool = False


@dataclass(frozen=True)
class Finding:
    text: str
    # A state a release passes through: wrong only when no move is at work to finish it.
    of_a_release: bool = False


class Standing(StrEnum):
    GOOD = "good"
    WRONG = "wrong"
    ON_PURPOSE = "on purpose"


class LaneVerdict(BaseModel):
    product: str
    lane: str
    a_move_is_at_work: bool
    problems: list[str]
    release_under_way: list[str] = []
    left_unfinished: list[str] = []
    in_maintenance: bool = False
    in_maintenance_since: datetime | None = None
    parts: dict[str, Standing] = {}


@dataclass(frozen=True)
class StillUp:
    container: str
    text: str
    is_database: bool = False


def data_set_of(seen: LaneSeen, name: str | None) -> DataSet | None:
    return next((found for found in seen.data_sets if found.data_set == name), None)


def network_names(found: DataSet | None) -> set[str]:
    return set() if found is None else {network.name for network in found.networks}


def attachment_problems(part: str, attached: tuple[str, ...], expected: set[str], seen: LaneSeen) -> list[str]:
    extra = sorted(set(attached) - expected)
    missing = sorted(expected - set(attached))
    reaching_out = sorted(name for name in attached if not seen.network_quiet.get(name, False))
    return [
        *([f"{part} is attached to {', '.join(extra)}, which it must not be"] if extra else []),
        *([f"{part} is not attached to {', '.join(missing)}"] if missing else []),
        *([f"{part} can reach out through {', '.join(reaching_out)}"] if reaching_out else []),
    ]


def described(mounts: Counter[Mount]) -> str:
    return ", ".join(
        f"{mount.source} mounted at {mount.path}" + (f" {times} times" if times > 1 else "")
        for mount, times in sorted(mounts.items(), key=lambda counted: (counted[0].path, counted[0].source))
    )


def mount_problems(part: str, mounts: tuple[Mount, ...], expected: tuple[Mount, ...]) -> list[str]:
    extra = Counter(mounts) - Counter(expected)
    missing = Counter(expected) - Counter(mounts)
    return [
        *([f"{part} has {described(extra)}, which it must not have"] if extra else []),
        *([f"{part} does not have {described(missing)}"] if missing else []),
    ]


def data_set_label_problems(instance: RunningInstance, product: Product, seen: LaneSeen) -> list[str]:
    part = f"The instance {instance.container}"
    if product.database is None:
        return [] if instance.data_set is None else [f"{part} runs against the data set {instance.data_set}; the product has none"]
    if instance.data_set is None:
        return [f"{part} runs against no data set"]
    if data_set_of(seen, instance.data_set) is None:
        return [f"{part} runs against the data set {instance.data_set}, which is not on the host"]
    return []


def instance_connection_problems(instance: RunningInstance, seen: LaneSeen) -> list[str]:
    part = f"The instance {instance.container}"
    expected = {LANE_NETWORK} | network_names(data_set_of(seen, instance.data_set))
    return [*attachment_problems(part, instance.networks, expected, seen), *mount_problems(part, instance.mounts, ())]


def instance_problems(instance: RunningInstance, target: LaneBuild, product: Product, seen: LaneSeen) -> list[str]:
    part = f"The instance {instance.container}"
    return [
        *([f"{part} runs {instance.build}, not {target.build}"] if instance.build != target.build else []),
        *([f"{part} is {instance.state}"] if instance.state != InstanceState.RUNNING else []),
        *data_set_label_problems(instance, product, seen),
        *instance_connection_problems(instance, seen),
    ]


def doubled_parts(found: DataSet) -> list[str]:
    parts: dict[str, tuple[str, ...]] = {
        "networks": tuple(network.name for network in found.networks),
        "volumes": found.volumes,
        "database servers": tuple(database.container for database in found.databases),
    }
    return [f"{len(names)} {kind} ({', '.join(names)})" for kind, names in parts.items() if len(names) > 1]


def database_mounts(found: DataSet, has_database: bool) -> tuple[Mount, ...]:
    return tuple(Mount(source=volume, path=DATA_PATH) for volume in found.volumes) if has_database else ()


def database_connection_problems(found: DataSet, database: DataSetDatabase, seen: LaneSeen, has_database: bool) -> list[str]:
    part = f"The database {database.container}"
    return [
        *attachment_problems(part, database.networks, network_names(found), seen),
        *mount_problems(part, database.mounts, database_mounts(found, has_database)),
    ]


def database_problems(found: DataSet, database: DataSetDatabase, seen: LaneSeen, has_database: bool) -> list[DataSetProblem]:
    part = f"The database {database.container}"
    connected = database_connection_problems(found, database, seen, has_database)
    if database.state == InstanceState.STOPPED:
        state = [DataSetProblem(f"{part} is {database.state}", fixable=True)]
    elif database.state != InstanceState.RUNNING:
        state = [DataSetProblem(f"{part} is {database.state}")]
    elif not database.accepts_connections:
        state = [DataSetProblem(f"{part} does not accept connections", fixable=True)]
    else:
        state = []
    return [*state, *[DataSetProblem(problem) for problem in connected]]


def data_set_problems(found: DataSet, seen: LaneSeen, has_database: bool) -> list[DataSetProblem]:
    part = f"The data set {found.data_set}"
    missing = [
        kind for kind, parts in (("network", found.networks), ("volume", found.volumes), ("database", found.databases)) if not parts
    ]
    return [
        *[DataSetProblem(f"{part} has {doubled} where it must have one") for doubled in doubled_parts(found)],
        *[DataSetProblem(f"{part} has no {kind}", fixable=True) for kind in missing],
        *[DataSetProblem(f"{part}'s network {network.name} is not quiet") for network in found.networks if not network.is_quiet],
        *[problem for database in found.databases for problem in database_problems(found, database, seen, has_database)],
    ]


def background_service_state_problems(running: RunningBackgroundService) -> list[BackgroundServiceProblem]:
    part = f"The background service {running.container}"
    if running.state != InstanceState.RUNNING or running.running_for_seconds is None:
        stopped = running.state == InstanceState.STOPPED
        return [
            BackgroundServiceProblem(f"{part} is {running.state}", Fix.REPLACE_OR_WATCH, running.container, of_a_release=stopped)
        ]
    if running.running_for_seconds < BACKGROUND_SERVICE_STEADY_SECONDS:
        text = f"{part} has run {running.running_for_seconds:.1f} s of the {BACKGROUND_SERVICE_STEADY_SECONDS:g} s it must stay up"
        return [BackgroundServiceProblem(text, Fix.WATCH, running.container, of_a_release=True)]
    return []


def background_service_connection_problems(running: RunningBackgroundService, active: RunningInstance, seen: LaneSeen) -> list[str]:
    part = f"The background service {running.container}"
    found = data_set_of(seen, active.data_set)
    expected = network_names(found) if found is not None else {LANE_NETWORK}
    labelled = (
        []
        if running.data_set == active.data_set
        else [f"{part} says it runs against the data set {running.data_set}, not {active.data_set}"]
    )
    return [*labelled, *attachment_problems(part, running.networks, expected, seen), *mount_problems(part, running.mounts, ())]


def one_background_service_problems(
    running: RunningBackgroundService, active: RunningInstance, seen: LaneSeen
) -> list[BackgroundServiceProblem]:
    part = f"The background service {running.container}"
    connected = [
        BackgroundServiceProblem(text, Fix.REPLACE, running.container)
        for text in background_service_connection_problems(running, active, seen)
    ]
    if running.instance != active.container:
        beside = any(instance.container == running.instance for instance in seen.instances)
        text = f"{part} belongs to {running.instance}, not {active.container}"
        return [BackgroundServiceProblem(text, Fix.REPLACE, running.container, of_a_release=beside), *connected]
    build = (
        []
        if running.build == active.build
        else [BackgroundServiceProblem(f"{part} runs {running.build}, not {active.build}", Fix.REPLACE, running.container)]
    )
    return [*build, *connected, *background_service_state_problems(running)]


def background_service_problems(seen: LaneSeen, active: RunningInstance, product: Product) -> list[BackgroundServiceProblem]:
    containers = sorted(running.container for running in seen.background_services)
    if product.background_service is None:
        return [
            BackgroundServiceProblem(f"The background service {container} runs; the product has none", Fix.REPLACE, container)
            for container in containers
        ]
    if len(containers) == 1:
        return one_background_service_problems(seen.background_services[0], active, seen)
    names = ", ".join(containers) or "none"
    text = f"The lane has {len(containers)} background services ({names}) where it must have one, belonging to {active.container}"
    if not containers:
        return [BackgroundServiceProblem(text, Fix.START, of_a_release=True)]
    return [BackgroundServiceProblem(text, Fix.REPLACE, containers[0])]


def active_instance(seen: LaneSeen) -> RunningInstance | None:
    return next((instance for instance in seen.instances if instance.container == seen.route.sends_visitors_to), None)


def data_set_change_problems(seen: LaneSeen, switching_to: RunningInstance) -> list[str]:
    receiving = active_instance(seen)
    if receiving is None or receiving.data_set in (None, switching_to.data_set):
        return []
    return [
        f"The instance {switching_to.container} runs against the data set {switching_to.data_set}, "
        f"not {receiving.data_set}, which the instance receiving visitors runs against"
    ]


def data_set_findings(found: DataSet | None, seen: LaneSeen, product: Product) -> list[Finding]:
    if found is None:
        return []
    return [Finding(problem.text, problem.fixable) for problem in data_set_problems(found, seen, product.database is not None)]


def lane_findings(seen: LaneSeen, target: LaneBuild, product: Product, switching_to: RunningInstance | None = None) -> list[Finding]:
    active = switching_to or active_instance(seen)
    if active is None:
        return [Finding("No instance receives visitors", of_a_release=True)]
    to_receive_visitors = [
        *[Finding(problem) for problem in instance_problems(active, target, product, seen)],
        *data_set_findings(data_set_of(seen, active.data_set), seen, product),
    ]
    if switching_to is not None:
        return [*to_receive_visitors, *[Finding(problem) for problem in data_set_change_problems(seen, switching_to)]]
    others = sorted(instance.container for instance in seen.instances if instance.container != active.container)
    return [
        *to_receive_visitors,
        *[Finding(problem.text, problem.of_a_release) for problem in background_service_problems(seen, active, product)],
        *[Finding(f"The instance {other} runs beside the active one", of_a_release=True) for other in others],
    ]


def lane_problems(seen: LaneSeen, target: LaneBuild, product: Product, switching_to: RunningInstance | None = None) -> list[str]:
    return [finding.text for finding in lane_findings(seen, target, product, switching_to)]


def findings_at_rest(seen: LaneSeen, product: Product) -> list[Finding]:
    active = active_instance(seen)
    used = None if active is None else active.data_set
    of_other_data_sets = [
        finding for found in seen.data_sets if found.data_set != used for finding in data_set_findings(found, seen, product)
    ]
    if active is None:
        return [Finding("No instance receives visitors", of_a_release=True), *of_other_data_sets]
    if active.lane is None or active.build is None:
        return [Finding(f"The instance {active.container} does not say which lane and build it is"), *of_other_data_sets]
    running = LaneBuild(product=active.product, lane=active.lane, build=active.build)
    return [*lane_findings(seen, running, product), *of_other_data_sets]


def connection_faults(seen: LaneSeen, product: Product) -> list[str]:
    with_owner = [
        (running, owner) for running in seen.background_services for owner in seen.instances if owner.container == running.instance
    ]
    databases = [(found, database) for found in seen.data_sets for database in found.databases]
    return [
        *[fault for instance in seen.instances for fault in instance_connection_problems(instance, seen)],
        *[fault for running, owner in with_owner for fault in background_service_connection_problems(running, owner, seen)],
        *[
            fault
            for found, each in databases
            for fault in database_connection_problems(found, each, seen, product.database is not None)
        ],
    ]


def databases_of(seen: LaneSeen) -> list[DataSetDatabase]:
    return [database for found in seen.data_sets for database in found.databases]


class Stoppable(Protocol):
    @property
    def container(self) -> str: ...
    @property
    def state(self) -> InstanceState: ...


def up_among(part: str, found: Iterable[Stoppable], *, is_database: bool = False) -> list[StillUp]:
    return [
        StillUp(each.container, f"The {part} {each.container} is {each.state}, in a lane in maintenance", is_database)
        for each in sorted(found, key=lambda one: one.container)
        if each.state != InstanceState.STOPPED
    ]


# In the order maintenance stops them: what writes stops before the web part, and the database stops last.
def still_up(seen: LaneSeen) -> list[StillUp]:
    return [
        *up_among("background service", seen.background_services),
        *up_among("instance", seen.instances),
        *up_among("database", databases_of(seen), is_database=True),
    ]


def killed_databases(seen: LaneSeen) -> list[str]:
    return [
        f"The database {database.container} did not stop cleanly: it exited with code {database.exit_code}, "
        "so it was killed or it crashed"
        for database in databases_of(seen)
        if database.state == InstanceState.STOPPED and database.exit_code != 0
    ]


def back_soon_faults(seen: LaneSeen) -> list[str]:
    if seen.route.back_soon_runs:
        return []
    return [f"The back soon page's container {BACK_SOON} is not running, so the web address answers the proxy's own error"]


def findings_in_maintenance(seen: LaneSeen, product: Product) -> list[Finding]:
    return [Finding(text) for text in [*[each.text for each in still_up(seen)], *never_acceptable(seen, product)]]


# What is wrong whatever a call is doing, so it is never shown as a call under way.
def never_acceptable(seen: LaneSeen, product: Product) -> list[str]:
    if not seen.route.in_maintenance:
        return connection_faults(seen, product)
    return [*killed_databases(seen), *back_soon_faults(seen), *connection_faults(seen, product)]


def findings_as_read(seen: LaneSeen, product: Product) -> list[Finding]:
    return findings_in_maintenance(seen, product) if seen.route.in_maintenance else findings_at_rest(seen, product)


def part_standings(seen: LaneSeen, *, a_move_is_at_work: bool) -> dict[str, Standing]:
    meant = InstanceState.STOPPED if seen.route.in_maintenance else InstanceState.RUNNING
    as_meant = Standing.ON_PURPOSE if seen.route.in_maintenance else Standing.GOOD
    otherwise = Standing.ON_PURPOSE if a_move_is_at_work else Standing.WRONG
    parts = [*seen.instances, *seen.background_services, *databases_of(seen)]
    return {part.container: as_meant if part.state == meant else otherwise for part in parts}


def sorted_findings(
    seen: LaneSeen, product: Product | ProductError, a_move_is_at_work: bool
) -> tuple[list[str], list[str], list[str]]:
    if isinstance(product, ProductError):
        return [str(product)], [], []
    findings = findings_as_read(seen, product)
    if a_move_is_at_work:
        wrong = never_acceptable(seen, product)
        return wrong, [finding.text for finding in findings if finding.text not in wrong], []
    return (
        [finding.text for finding in findings if not finding.of_a_release],
        [],
        [finding.text for finding in findings if finding.of_a_release],
    )


def lane_verdict(seen: LaneSeen, product: Product | ProductError, name: str, lane: str, *, a_move_is_at_work: bool) -> LaneVerdict:
    wrong, under_way, unfinished = sorted_findings(seen, product, a_move_is_at_work)
    return LaneVerdict(
        product=name,
        lane=lane,
        a_move_is_at_work=a_move_is_at_work,
        problems=wrong,
        release_under_way=under_way,
        left_unfinished=unfinished,
        in_maintenance=seen.route.in_maintenance,
        in_maintenance_since=seen.route.back_soon_since,
        parts=part_standings(seen, a_move_is_at_work=a_move_is_at_work),
    )
