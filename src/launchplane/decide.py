from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from launchplane.instance import DataSet, InstanceState, LaneBuild, RunningInstance, RunningJob
from launchplane.product import Database, Product

LANE_DOMAIN = "test"


class Outcome(StrEnum):
    SWITCHED = "switched"
    UNCHANGED = "unchanged"
    CLEANED_UP = "cleaned up"
    CHECK_FAILED = "check failed"


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
class StartProxy:
    pass


@dataclass(frozen=True)
class AwaitRoute:
    web_address: str
    container: str | None


@dataclass(frozen=True)
class EnsureDataSet:
    product: str
    lane: str
    data_set: str
    database: Database


@dataclass(frozen=True)
class AwaitDatabase:
    container: str
    database: Database


@dataclass(frozen=True)
class StartInstance:
    target: LaneBuild
    container: str
    data_set: str | None = None


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
    data_set: str | None
    command: tuple[str, ...]


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
    data_set: str


@dataclass(frozen=True)
class CheckRefused:
    container: str
    report: str


@dataclass(frozen=True)
class Done[Answer]:
    answer: Answer


@dataclass(frozen=True)
class Stop:
    reason: str


type ProxyAction = StartProxy | AwaitRoute | WriteRoute | RemoveRoute
type HostAction = EnsureDataSet | AwaitDatabase | StartInstance | StartJob | Retire | RemoveInstance | DeleteDataSet
type Action = ProxyAction | HostAction | Check
type Event = Action | CheckRefused


def web_address_of(product: str, lane: str) -> str:
    return f"{lane}.{product}.{LANE_DOMAIN}"


def is_loaded_as_written(route: LaneRoute) -> bool:
    return (None if route.loaded is None else route.loaded.container) == route.written


def change_made_by(event: Event) -> str | None:
    match event:
        case StartProxy():
            return "started the proxy"
        case EnsureDataSet(data_set=data_set):
            return f"made the data set {data_set}"
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
    return instance is not None and instance.state == InstanceState.RUNNING and instance.build == build


def is_serving(instance: RunningInstance | None, build: str, product: Product) -> bool:
    return instance is not None and is_running_build(instance, build) and (product.database is None) == (instance.data_set is None)


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
    if any(isinstance(event, Retire | StartJob) for event in done):
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


def data_set_step(
    seen: LaneSeen, data_set: str, fresh: str, target: LaneBuild, database: Database, done: tuple[Event, ...]
) -> Action | Stop | None:
    found = next((each for each in seen.data_sets if each.data_set == data_set), None)
    ensure = EnsureDataSet(target.product, target.lane, data_set, database)
    if found is None:
        if data_set != fresh:
            return Stop(
                f"The instance receiving visitors uses the data set {data_set}, which is not on the host. Nothing was changed."
            )
        return ensure
    if found.network_is_quiet is False:
        return Stop(f"The network of the data set {data_set} is not quiet, so no instance is started on it. {changes_so_far(done)}")
    if found.network_is_quiet is None or not found.has_volume or found.database_container is None:
        return ensure
    if found.database_state == InstanceState.STOPPED:
        return ensure
    if found.database_state != InstanceState.RUNNING:
        return Stop(
            f"The database {found.database_container} of the data set {data_set} is {found.database_state}. {changes_so_far(done)}"
        )
    awaited = AwaitDatabase(found.database_container, database)
    return None if awaited in done else awaited


def settle_serving(
    seen: LaneSeen, serving: RunningInstance, target: LaneBuild, product: Product, answer: MoveResult, done: tuple[Event, ...]
) -> Action | Done[MoveResult]:
    own_job = next((job for job in seen.jobs if job.instance == serving.container), None)
    started_here = any(isinstance(event, StartJob) and event.instance == serving.container for event in done)
    if own_job is not None and (product.job is None or (own_job.state != InstanceState.RUNNING and not started_here)):
        return Retire(own_job.container)
    if own_job is None and product.job is not None:
        return StartJob(target, serving.container, serving.data_set, tuple(product.job.command))
    old = sorted(instance.container for instance in seen.instances if instance.container != serving.container)
    return Retire(old[0]) if old else Done(answer)


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
    if candidate is not None:
        return check_then_route(web_address_of(target.product, target.lane), instances[candidate], product, done)
    if product.database is not None and data_set is not None:
        prepare = data_set_step(seen, data_set, fresh.data_set, target, product.database, done)
        if prepare is not None:
            return prepare
    return StartInstance(target, fresh.container, data_set)


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
    kept = sorted(data_set.data_set for data_set in seen.data_sets)
    if delete_data_set is not None and delete_data_set in kept:
        return DeleteDataSet(delete_data_set)
    route_removed_to = next((event.container for event in done if isinstance(event, RemoveRoute)), None)
    retired = [event.container for event in done if isinstance(event, Retire)]
    deleted = next((event.data_set for event in done if isinstance(event, DeleteDataSet)), None)
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
            kept_data_sets=kept,
        )
    )
