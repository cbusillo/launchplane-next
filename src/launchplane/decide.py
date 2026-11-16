from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel

from launchplane.instance import (
    LANE_NETWORK,
    DataSet,
    DataSetDatabase,
    InstanceState,
    LaneBuild,
    Mount,
    RunningInstance,
    RunningJob,
)
from launchplane.product import Database, Product

LANE_DOMAIN = "test"
JOB_STEADY_SECONDS = 3.0


class Outcome(StrEnum):
    SWITCHED = "switched"
    UNCHANGED = "unchanged"
    CLEANED_UP = "cleaned up"
    CHECK_FAILED = "check failed"
    JOB_FAILED = "job failed"


class MoveResult(BaseModel):
    product: str
    lane: str
    build: str
    web_address: str
    outcome: Outcome
    container_receiving_visitors: str | None
    data_set: str | None = None
    error: str | None = None


class RetireOutcome(StrEnum):
    RETIRED = "retired"
    UNCHANGED = "unchanged"


class RetireResult(BaseModel):
    product: str
    lane: str
    web_address: str
    outcome: RetireOutcome
    route_removed_to_container: str | None
    retired_containers: list[str]
    deleted_data_set: str | None = None
    kept_data_sets: list[str] = []


@dataclass(frozen=True)
class ProxyNotRunning:
    pass


class RouteStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    WARNING = "warning"


@dataclass(frozen=True)
class LoadedRoute:
    container: str
    status: RouteStatus


@dataclass(frozen=True)
class LaneRoute:
    loaded: LoadedRoute | None
    written: str | None


@dataclass(frozen=True)
class LaneSeen:
    instances: tuple[RunningInstance, ...]
    route: ProxyNotRunning | LaneRoute
    jobs: tuple[RunningJob, ...] = ()
    data_sets: tuple[DataSet, ...] = ()
    network_quiet: Mapping[str, bool] = field(default_factory=dict[str, bool])


@dataclass(frozen=True)
class FreshNames:
    container: str
    data_set: str


@dataclass(frozen=True)
class DataSetUse:
    data_set: str
    network: str
    database_container: str
    database: Database


@dataclass(frozen=True)
class DataSetProblem:
    text: str
    a_step_puts_right: bool = False


class JobFix(StrEnum):
    START = "start one"
    REPLACE = "retire it, and start one if the product has a job"
    REPLACE_UNLESS_JUST_STARTED = "retire it, unless this move started it and is about to watch it"
    WATCH = "watch it until it has stayed up"


@dataclass(frozen=True)
class JobProblem:
    text: str
    fix: JobFix
    job: str | None = None


@dataclass(frozen=True)
class StartProxy:
    pass


@dataclass(frozen=True)
class AwaitRoute:
    web_address: str
    container: str | None


@dataclass(frozen=True)
class CreateDataSetNetwork:
    product: str
    lane: str
    data_set: str


@dataclass(frozen=True)
class CreateDataSetVolume:
    product: str
    lane: str
    data_set: str


@dataclass(frozen=True)
class CreateDatabase:
    product: str
    lane: str
    data_set: str
    network: str
    volume: str
    database: Database


@dataclass(frozen=True)
class StartDatabase:
    container: str


@dataclass(frozen=True)
class AwaitDatabase:
    container: str
    database: Database


@dataclass(frozen=True)
class StartInstance:
    target: LaneBuild
    container: str
    use: DataSetUse | None = None


@dataclass(frozen=True)
class Check:
    container: str
    health_path: str = "/"


@dataclass(frozen=True)
class WriteRoute:
    web_address: str
    container: str


@dataclass(frozen=True)
class StartJob:
    target: LaneBuild
    instance: str
    use: DataSetUse | None
    command: tuple[str, ...]


@dataclass(frozen=True)
class AwaitJob:
    container: str


@dataclass(frozen=True)
class RemoveRoute:
    web_address: str
    container: str


@dataclass(frozen=True)
class Retire:
    container: str


@dataclass(frozen=True)
class RemoveInstance:
    container: str


@dataclass(frozen=True)
class DeleteDataSet:
    found: DataSet


@dataclass(frozen=True)
class CheckRefused:
    container: str
    report: str


@dataclass(frozen=True)
class JobRefused:
    container: str
    report: str


@dataclass(frozen=True)
class Done[Answer]:
    answer: Answer


@dataclass(frozen=True)
class Stop:
    reason: str


type ProxyAction = StartProxy | AwaitRoute | WriteRoute | RemoveRoute
type DataSetAction = CreateDataSetNetwork | CreateDataSetVolume | CreateDatabase | StartDatabase | AwaitDatabase | DeleteDataSet
type InstanceAction = StartInstance | StartJob | Retire | RemoveInstance
type Action = ProxyAction | DataSetAction | InstanceAction | Check | AwaitJob
type Event = Action | CheckRefused | JobRefused


def web_address_of(product: str, lane: str) -> str:
    return f"{lane}.{product}.{LANE_DOMAIN}"


def is_loaded_as_written(route: LaneRoute) -> bool:
    return (None if route.loaded is None else route.loaded.container) == route.written


def change_made_by(event: Event) -> str | None:
    match event:
        case StartProxy():
            return "started the proxy"
        case CreateDataSetNetwork(data_set=data_set) | CreateDataSetVolume(data_set=data_set) | CreateDatabase(data_set=data_set):
            return f"made part of the data set {data_set}"
        case StartDatabase(container=container):
            return f"started the database {container}"
        case StartInstance(container=container):
            return f"started the instance {container}"
        case WriteRoute(container=container):
            return f"wrote the route to {container}"
        case StartJob(instance=instance):
            return f"started the job of {instance}"
        case Retire(container=container):
            return f"retired {container}"
        case RemoveInstance(container=container):
            return f"removed {container}"
        case _:
            return None


def changes_so_far(done: tuple[Event, ...]) -> str:
    changes = [change for change in map(change_made_by, done) if change is not None]
    if not changes:
        return "Nothing was changed."
    return f"Before that, this move {', '.join(changes)}, and those changes stay."


def disabled_route_stop(web_address: str, loaded: LoadedRoute, done: tuple[Event, ...]) -> Stop:
    return Stop(
        f"The proxy has loaded the route of {web_address} to {loaded.container} and reports it {loaded.status}, "
        f"so the web address reaches no instance. {changes_so_far(done)} "
        "The proxy's log says why; retiring the lane removes the route."
    )


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


def mount_problems(part: str, mounts: tuple[Mount, ...], expected: set[str]) -> list[str]:
    mounted = {mount.source for mount in mounts}
    extra = sorted(mounted - expected)
    missing = sorted(expected - mounted)
    return [
        *([f"{part} has {', '.join(extra)} mounted, which it must not have"] if extra else []),
        *([f"{part} does not have {', '.join(missing)} mounted"] if missing else []),
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


def instance_problems(instance: RunningInstance, target: LaneBuild, product: Product, seen: LaneSeen) -> list[str]:
    part = f"The instance {instance.container}"
    expected = {LANE_NETWORK} | network_names(data_set_of(seen, instance.data_set))
    return [
        *([f"{part} runs {instance.build}, not {target.build}"] if instance.build != target.build else []),
        *([f"{part} is {instance.state}"] if instance.state != InstanceState.RUNNING else []),
        *data_set_label_problems(instance, product, seen),
        *attachment_problems(part, instance.networks, expected, seen),
        *mount_problems(part, instance.mounts, set()),
    ]


def doubled_parts(found: DataSet) -> list[str]:
    parts: dict[str, tuple[str, ...]] = {
        "networks": tuple(network.name for network in found.networks),
        "volumes": found.volumes,
        "database servers": tuple(database.container for database in found.databases),
    }
    return [f"{len(names)} {kind} ({', '.join(names)})" for kind, names in parts.items() if len(names) > 1]


def database_problems(found: DataSet, database: DataSetDatabase, seen: LaneSeen) -> list[DataSetProblem]:
    part = f"The database {database.container}"
    connected = [
        *attachment_problems(part, database.networks, network_names(found), seen),
        *mount_problems(part, database.mounts, set(found.volumes)),
    ]
    if database.state == InstanceState.STOPPED:
        state = [DataSetProblem(f"{part} is {database.state}", a_step_puts_right=True)]
    elif database.state != InstanceState.RUNNING:
        state = [DataSetProblem(f"{part} is {database.state}")]
    elif not database.accepts_connections:
        state = [DataSetProblem(f"{part} does not accept connections", a_step_puts_right=True)]
    else:
        state = []
    return [*state, *[DataSetProblem(problem) for problem in connected]]


def data_set_problems(found: DataSet, seen: LaneSeen) -> list[DataSetProblem]:
    part = f"The data set {found.data_set}"
    missing = [
        kind for kind, parts in (("network", found.networks), ("volume", found.volumes), ("database", found.databases)) if not parts
    ]
    return [
        *[DataSetProblem(f"{part} has {doubled} where it must have one") for doubled in doubled_parts(found)],
        *[DataSetProblem(f"{part} has no {kind}", a_step_puts_right=True) for kind in missing],
        *[DataSetProblem(f"{part}'s network {network.name} is not quiet") for network in found.networks if not network.is_quiet],
        *[problem for database in found.databases for problem in database_problems(found, database, seen)],
    ]


def job_state_problems(job: RunningJob) -> list[JobProblem]:
    part = f"The job {job.container}"
    if job.state != InstanceState.RUNNING or job.running_for_seconds is None:
        return [JobProblem(f"{part} is {job.state}", JobFix.REPLACE_UNLESS_JUST_STARTED, job.container)]
    if job.running_for_seconds < JOB_STEADY_SECONDS:
        text = f"{part} has run {job.running_for_seconds:.1f} s of the {JOB_STEADY_SECONDS:g} s it must stay up"
        return [JobProblem(text, JobFix.WATCH, job.container)]
    return []


def job_connection_problems(job: RunningJob, active: RunningInstance, seen: LaneSeen) -> list[str]:
    part = f"The job {job.container}"
    found = data_set_of(seen, active.data_set)
    expected = network_names(found) if found is not None else {LANE_NETWORK}
    return [*attachment_problems(part, job.networks, expected, seen), *mount_problems(part, job.mounts, set())]


def problems_of_the_one_job(job: RunningJob, active: RunningInstance, seen: LaneSeen) -> list[JobProblem]:
    part = f"The job {job.container}"
    to_replace = [
        *([] if job.instance == active.container else [f"{part} belongs to {job.instance}, not {active.container}"]),
        *([] if job.build == active.build else [f"{part} runs {job.build}, not {active.build}"]),
        *job_connection_problems(job, active, seen),
    ]
    return [*[JobProblem(text, JobFix.REPLACE, job.container) for text in to_replace], *job_state_problems(job)]


def job_problems(seen: LaneSeen, active: RunningInstance, product: Product) -> list[JobProblem]:
    jobs = sorted(seen.jobs, key=lambda job: job.container)
    if product.job is None:
        return [JobProblem(f"The job {job.container} runs; the product has no job", JobFix.REPLACE, job.container) for job in jobs]
    if len(jobs) == 1:
        return problems_of_the_one_job(jobs[0], active, seen)
    names = ", ".join(job.container for job in jobs) or "none"
    text = f"The lane has {len(jobs)} jobs ({names}) where it must have one, belonging to {active.container}"
    return [JobProblem(text, JobFix.REPLACE, jobs[0].container) if jobs else JobProblem(text, JobFix.START)]


def instance_receiving_visitors(seen: LaneSeen) -> RunningInstance | None:
    receiving = None if isinstance(seen.route, ProxyNotRunning) else seen.route.written
    return next((instance for instance in seen.instances if instance.container == receiving), None)


def data_set_change_problems(seen: LaneSeen, switching_to: RunningInstance) -> list[str]:
    receiving = instance_receiving_visitors(seen)
    if receiving is None or receiving.data_set in (None, switching_to.data_set):
        return []
    return [
        f"The instance {switching_to.container} runs against the data set {switching_to.data_set}, "
        f"not {receiving.data_set}, which the instance receiving visitors runs against"
    ]


def lane_problems(seen: LaneSeen, target: LaneBuild, product: Product, switching_to: RunningInstance | None = None) -> list[str]:
    active = switching_to or instance_receiving_visitors(seen)
    if active is None:
        return ["No instance receives visitors"]
    found = data_set_of(seen, active.data_set)
    to_receive_visitors = [
        *instance_problems(active, target, product, seen),
        *([problem.text for problem in data_set_problems(found, seen)] if found is not None else []),
    ]
    if switching_to is not None:
        return [*to_receive_visitors, *data_set_change_problems(seen, switching_to)]
    others = sorted(instance.container for instance in seen.instances if instance.container != active.container)
    return [
        *to_receive_visitors,
        *[problem.text for problem in job_problems(seen, active, product)],
        *[f"The instance {other} runs beside the active one" for other in others],
    ]


def lane_verdict(seen: LaneSeen, product: Product) -> list[str]:
    active = instance_receiving_visitors(seen)
    used = None if active is None else active.data_set
    of_other_data_sets = [
        problem.text for found in seen.data_sets if found.data_set != used for problem in data_set_problems(found, seen)
    ]
    if active is None:
        return ["No instance receives visitors", *of_other_data_sets]
    if active.lane is None or active.build is None:
        return [f"The instance {active.container} does not say which lane and build it is", *of_other_data_sets]
    running = LaneBuild(product=active.product, lane=active.lane, build=active.build)
    return [*lane_problems(seen, running, product), *of_other_data_sets]


def candidate_for(
    seen: LaneSeen, receiving: str | None, target: LaneBuild, product: Product, data_set: str | None, done: tuple[Event, ...]
) -> str | None:
    instances = {instance.container: instance for instance in seen.instances}
    started_here = [event.container for event in done if isinstance(event, StartInstance)]
    own = next((name for name in reversed(started_here) if name in instances and name != receiving), None)
    left_over = (
        name
        for name, instance in sorted(instances.items())
        if name != receiving and instance.data_set == data_set and not instance_problems(instance, target, product, seen)
    )
    return own or next(left_over, None)


def move_outcome(done: tuple[Event, ...]) -> Outcome:
    if any(isinstance(event, WriteRoute) for event in done):
        return Outcome.SWITCHED
    if any(isinstance(event, Retire | StartJob | StartDatabase) for event in done):
        return Outcome.CLEANED_UP
    return Outcome.UNCHANGED


def after_refusal(refused: CheckRefused, instances: dict[str, RunningInstance], answer: MoveResult) -> Action | Done[MoveResult]:
    if refused.container in instances:
        return RemoveInstance(refused.container)
    return Done(answer.model_copy(update={"outcome": Outcome.CHECK_FAILED, "error": refused.report}))


def check_then_route(
    seen: LaneSeen, candidate: RunningInstance, target: LaneBuild, product: Product, done: tuple[Event, ...]
) -> Action | Stop:
    check = Check(candidate.container, product.health_path)
    if check not in done or candidate.state != InstanceState.RUNNING:
        return check
    problems = lane_problems(seen, target, product, switching_to=candidate)
    if problems:
        return Stop(
            f"The instance {candidate.container} passed its check and the lane is not as it must be to switch to it, so the "
            f"web address was not switched: {'; '.join(problems)}. {changes_so_far(done)} Asking again replaces the instance."
        )
    return WriteRoute(web_address_of(target.product, target.lane), candidate.container)


def lane_data_set(seen: LaneSeen, receiving: RunningInstance | None, fresh: str) -> str | Stop:
    if receiving is not None and receiving.data_set is not None:
        return receiving.data_set
    names = sorted(data_set.data_set for data_set in seen.data_sets)
    if len(names) > 1:
        return Stop(
            f"The lane has the data sets {', '.join(names)} and no instance receiving visitors that uses one, "
            "so Launchplane cannot tell which one to start the instance on. Nothing was changed."
        )
    return names[0] if names else fresh


def unfixable_data_set_stop(found: DataSet, problems: list[str], done: tuple[Event, ...]) -> Stop:
    return Stop(
        f"The data set {found.data_set} is not as it must be, and fixing it is not Launchplane's to choose: "
        f"{'; '.join(problems)}. {changes_so_far(done)}"
    )


def step_toward_ready(found: DataSet, target: LaneBuild, database: Database) -> Action:
    product, lane = target.product, target.lane
    if not found.networks:
        return CreateDataSetNetwork(product, lane, found.data_set)
    if not found.volumes:
        return CreateDataSetVolume(product, lane, found.data_set)
    if not found.databases:
        return CreateDatabase(product, lane, found.data_set, found.networks[0].name, found.volumes[0], database)
    server = found.databases[0]
    if server.state == InstanceState.STOPPED:
        return StartDatabase(server.container)
    return AwaitDatabase(server.container, database)


def data_set_step(
    found: DataSet, target: LaneBuild, database: Database, seen: LaneSeen, done: tuple[Event, ...]
) -> Action | Stop | DataSetUse:
    problems = data_set_problems(found, seen)
    not_a_moves_to_put_right = [problem.text for problem in problems if not problem.a_step_puts_right]
    if not_a_moves_to_put_right:
        return unfixable_data_set_stop(found, not_a_moves_to_put_right, done)
    if problems:
        return step_toward_ready(found, target, database)
    return DataSetUse(found.data_set, found.networks[0].name, found.databases[0].container, database)


def prepared(
    seen: LaneSeen, data_set: str | None, fresh: str | None, target: LaneBuild, product: Product, done: tuple[Event, ...]
) -> Action | Stop | DataSetUse | None:
    if data_set is None:
        return None
    if product.database is None:
        return None
    found = data_set_of(seen, data_set)
    if found is not None:
        return data_set_step(found, target, product.database, seen, done)
    if fresh is not None and data_set == fresh:
        return CreateDataSetNetwork(target.product, target.lane, fresh)
    return Stop(f"The instance receiving visitors uses the data set {data_set}, which is not on the host. {changes_so_far(done)}")


def job_step(
    seen: LaneSeen, serving: RunningInstance, target: LaneBuild, product: Product, use: DataSetUse | None, done: tuple[Event, ...]
) -> Action | None:
    problems = job_problems(seen, serving, product)
    if not problems or any(isinstance(event, JobRefused) for event in done):
        return None
    command = () if product.job is None else tuple(product.job.command)
    start = StartJob(target, serving.container, use, command)
    first = problems[0]
    if first.job is None:
        return start
    if first.fix == JobFix.WATCH or (first.fix == JobFix.REPLACE_UNLESS_JUST_STARTED and start in done):
        return AwaitJob(first.job)
    return Retire(first.job)


def finished(
    seen: LaneSeen, target: LaneBuild, product: Product, answer: MoveResult, done: tuple[Event, ...]
) -> Done[MoveResult] | Stop:
    refused = next((event for event in done if isinstance(event, JobRefused)), None)
    if refused is not None:
        return Done(answer.model_copy(update={"outcome": Outcome.JOB_FAILED, "error": refused.report}))
    problems = lane_problems(seen, target, product)
    if problems:
        return Stop(
            f"The lane is not as it must be, so the move does not answer success: {'; '.join(problems)}. {changes_so_far(done)}"
        )
    return Done(answer)


def settle_serving(
    seen: LaneSeen, serving: RunningInstance, target: LaneBuild, product: Product, answer: MoveResult, done: tuple[Event, ...]
) -> Action | Done[MoveResult] | Stop:
    use = prepared(seen, serving.data_set, None, target, product, done)
    if not (use is None or isinstance(use, DataSetUse)):
        return use
    if (step := job_step(seen, serving, target, product, use, done)) is not None:
        return step
    old = sorted(instance.container for instance in seen.instances if instance.container != serving.container)
    return Retire(old[0]) if old else finished(seen, target, product, answer, done)


def decide_move(
    seen: LaneSeen, target: LaneBuild, product: Product, fresh: FreshNames, done: tuple[Event, ...]
) -> Action | Done[MoveResult] | Stop:
    if isinstance(seen.route, ProxyNotRunning):
        return StartProxy()
    web_address = web_address_of(target.product, target.lane)
    if not is_loaded_as_written(seen.route):
        return AwaitRoute(web_address, seen.route.written)
    if seen.route.loaded is not None and seen.route.loaded.status != RouteStatus.ENABLED:
        return disabled_route_stop(web_address, seen.route.loaded, done)
    return decide_move_with_route(seen, seen.route.written, target, product, fresh, done)


def decide_move_with_route(
    seen: LaneSeen, receiving: str | None, target: LaneBuild, product: Product, fresh: FreshNames, done: tuple[Event, ...]
) -> Action | Done[MoveResult] | Stop:
    instances = {instance.container: instance for instance in seen.instances}
    receiving_instance = None if receiving is None else instances.get(receiving)
    answer = MoveResult(
        product=target.product,
        lane=target.lane,
        build=target.build,
        web_address=web_address_of(target.product, target.lane),
        outcome=move_outcome(done),
        container_receiving_visitors=receiving,
        data_set=None if receiving_instance is None else receiving_instance.data_set,
    )
    refused = next((event for event in done if isinstance(event, CheckRefused)), None)
    if refused is not None:
        return after_refusal(refused, instances, answer)
    jobs_of_others = sorted(job.container for job in seen.jobs if job.instance != receiving)
    if jobs_of_others:
        return Retire(jobs_of_others[0])
    if receiving_instance is not None and not instance_problems(receiving_instance, target, product, seen):
        return settle_serving(seen, receiving_instance, target, product, answer, done)
    return replace_receiving(seen, receiving_instance, target, product, fresh, done)


def replace_receiving(
    seen: LaneSeen,
    receiving: RunningInstance | None,
    target: LaneBuild,
    product: Product,
    fresh: FreshNames,
    done: tuple[Event, ...],
) -> Action | Stop:
    data_set = None if product.database is None else lane_data_set(seen, receiving, fresh.data_set)
    if isinstance(data_set, Stop):
        return data_set
    instances = {instance.container: instance for instance in seen.instances}
    receiving_container = None if receiving is None else receiving.container
    candidate = candidate_for(seen, receiving_container, target, product, data_set, done)
    strays = sorted(instances.keys() - {receiving_container, candidate})
    if strays:
        return Retire(strays[0])
    use = prepared(seen, data_set, fresh.data_set, target, product, done)
    if not (use is None or isinstance(use, DataSetUse)):
        return use
    if candidate is not None:
        return check_then_route(seen, instances[candidate], target, product, done)
    return StartInstance(target, fresh.container, use)


def decide_retire(
    seen: LaneSeen, product: str, lane: str, delete_data_set: str | None, done: tuple[Event, ...]
) -> Action | Done[RetireResult] | Stop:
    if isinstance(seen.route, ProxyNotRunning):
        return StartProxy()
    web_address = web_address_of(product, lane)
    if not is_loaded_as_written(seen.route):
        return AwaitRoute(web_address, seen.route.written)
    if seen.route.written is not None:
        return RemoveRoute(web_address, seen.route.written)
    if seen.jobs:
        return Retire(min(job.container for job in seen.jobs))
    if seen.instances:
        return Retire(min(instance.container for instance in seen.instances))
    named = next((found for found in seen.data_sets if found.data_set == delete_data_set), None)
    if named is not None:
        return DeleteDataSet(named)
    route_removed_to = next((event.container for event in done if isinstance(event, RemoveRoute)), None)
    retired = [event.container for event in done if isinstance(event, Retire)]
    deleted = next((event.found.data_set for event in done if isinstance(event, DeleteDataSet)), None)
    changed = route_removed_to is not None or bool(retired) or deleted is not None
    return Done(
        RetireResult(
            product=product,
            lane=lane,
            web_address=web_address,
            outcome=RetireOutcome.RETIRED if changed else RetireOutcome.UNCHANGED,
            route_removed_to_container=route_removed_to,
            retired_containers=retired,
            deleted_data_set=deleted,
            kept_data_sets=sorted(found.data_set for found in seen.data_sets),
        )
    )
