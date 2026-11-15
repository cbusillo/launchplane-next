from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from launchplane.instance import DataSet, DataSetDatabase, DataSetNetwork, InstanceState, LaneBuild, RunningInstance, RunningJob
from launchplane.product import Database, Product

LANE_DOMAIN = "test"


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
class CheckJob:
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
type Action = ProxyAction | DataSetAction | InstanceAction | Check | CheckJob
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


def is_running_build(instance: RunningInstance | None, build: str) -> bool:
    return instance is not None and instance.state == InstanceState.RUNNING and instance.is_quiet and instance.build == build


def is_serving(instance: RunningInstance, build: str, product: Product) -> bool:
    return is_running_build(instance, build) and (product.database is None) == (instance.data_set is None)


def candidate_for(
    instances: dict[str, RunningInstance], receiving: str | None, target: LaneBuild, data_set: str | None, done: tuple[Event, ...]
) -> str | None:
    started_here = [event.container for event in done if isinstance(event, StartInstance)]
    own = next((name for name in reversed(started_here) if name in instances and name != receiving), None)
    left_over = (
        name
        for name in sorted(instances)
        if name != receiving and is_running_build(instances[name], target.build) and instances[name].data_set == data_set
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


def check_then_route(web_address: str, candidate: RunningInstance, product: Product, done: tuple[Event, ...]) -> Action:
    check = Check(candidate.container, product.health_path)
    if check in done and candidate.state == InstanceState.RUNNING:
        return WriteRoute(web_address, candidate.container)
    return check


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


def doubled_parts(found: DataSet) -> list[str]:
    parts: dict[str, tuple[str, ...]] = {
        "networks": tuple(network.name for network in found.networks),
        "volumes": found.volumes,
        "database servers": tuple(database.container for database in found.databases),
    }
    return [f"{len(names)} {kind} ({', '.join(names)})" for kind, names in parts.items() if len(names) > 1]


def network_step(found: DataSet, network: DataSetNetwork, done: tuple[Event, ...]) -> Stop | None:
    if network.is_quiet:
        return None
    return Stop(
        f"The network {network.name} of the data set {found.data_set} is not quiet, so nothing is started on it. "
        f"{changes_so_far(done)}"
    )


def database_step(found: DataSet, server: DataSetDatabase, database: Database, done: tuple[Event, ...]) -> Action | Stop | None:
    if server.state == InstanceState.STOPPED:
        return StartDatabase(server.container)
    if server.state != InstanceState.RUNNING:
        return Stop(f"The database {server.container} of the data set {found.data_set} is {server.state}. {changes_so_far(done)}")
    awaited = AwaitDatabase(server.container, database)
    return None if awaited in done else awaited


def data_set_step(found: DataSet, target: LaneBuild, database: Database, done: tuple[Event, ...]) -> Action | Stop | DataSetUse:
    doubled = doubled_parts(found)
    if doubled:
        return Stop(
            f"The data set {found.data_set} has {' and '.join(doubled)} carrying its labels, where it should have one of each. "
            f"Which one is the data set is not Launchplane's to choose. {changes_so_far(done)}"
        )
    product, lane = target.product, target.lane
    if not found.networks:
        return CreateDataSetNetwork(product, lane, found.data_set)
    [network] = found.networks
    if (stop := network_step(found, network, done)) is not None:
        return stop
    if not found.volumes:
        return CreateDataSetVolume(product, lane, found.data_set)
    if not found.databases:
        return CreateDatabase(product, lane, found.data_set, network.name, found.volumes[0], database)
    [server] = found.databases
    if (step := database_step(found, server, database, done)) is not None:
        return step
    return DataSetUse(found.data_set, network.name, server.container, database)


def prepared(
    seen: LaneSeen, data_set: str | None, fresh: str | None, target: LaneBuild, product: Product, done: tuple[Event, ...]
) -> Action | Stop | DataSetUse | None:
    if product.database is None or data_set is None:
        return None
    found = next((each for each in seen.data_sets if each.data_set == data_set), None)
    if found is not None:
        return data_set_step(found, target, product.database, done)
    if data_set == fresh:
        return CreateDataSetNetwork(target.product, target.lane, data_set)
    return Stop(f"The instance receiving visitors uses the data set {data_set}, which is not on the host. {changes_so_far(done)}")


def job_step(
    seen: LaneSeen, serving: RunningInstance, target: LaneBuild, product: Product, use: DataSetUse | None, done: tuple[Event, ...]
) -> Action | None:
    own = next((job for job in seen.jobs if job.instance == serving.container), None)
    if product.job is None:
        return None if own is None else Retire(own.container)
    if any(isinstance(event, JobRefused) for event in done):
        return None
    if own is None:
        return StartJob(target, serving.container, use, tuple(product.job.command))
    started_here = any(isinstance(event, StartJob) and event.instance == serving.container for event in done)
    if started_here and CheckJob(own.container) not in done:
        return CheckJob(own.container)
    if own.state != InstanceState.RUNNING or not own.is_quiet:
        return Retire(own.container)
    return None


def settled_answer(answer: MoveResult, done: tuple[Event, ...]) -> MoveResult:
    refused = next((event for event in done if isinstance(event, JobRefused)), None)
    if refused is None:
        return answer
    return answer.model_copy(update={"outcome": Outcome.JOB_FAILED, "error": refused.report})


def settle_serving(
    seen: LaneSeen, serving: RunningInstance, target: LaneBuild, product: Product, answer: MoveResult, done: tuple[Event, ...]
) -> Action | Done[MoveResult] | Stop:
    use = prepared(seen, serving.data_set, None, target, product, done)
    if not (use is None or isinstance(use, DataSetUse)):
        return use
    if (step := job_step(seen, serving, target, product, use, done)) is not None:
        return step
    old = sorted(instance.container for instance in seen.instances if instance.container != serving.container)
    return Retire(old[0]) if old else Done(settled_answer(answer, done))


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
    if receiving_instance is not None and is_serving(receiving_instance, target.build, product):
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
    candidate = candidate_for(instances, receiving_container, target, data_set, done)
    strays = sorted(instances.keys() - {receiving_container, candidate})
    if strays:
        return Retire(strays[0])
    use = prepared(seen, data_set, fresh.data_set, target, product, done)
    if not (use is None or isinstance(use, DataSetUse)):
        return use
    if candidate is not None:
        return check_then_route(web_address_of(target.product, target.lane), instances[candidate], product, done)
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
