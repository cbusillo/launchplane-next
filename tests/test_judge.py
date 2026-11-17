import json
from pathlib import Path

import pytest

from launchplane.instance import BACK_SOON, InstanceState, LaneRoute, LaneSeen, Mount
from launchplane.judge import lane_verdict
from launchplane.product import Product, ProductError
from tests.test_maintenance import BACKGROUND_SERVICE, IN_MAINTENANCE, KILLED, SERVING, SINCE, UP, WEB, Lane, database
from tests.test_plan import (
    D1,
    LEFT_ON_D1,
    NEW,
    NEW_BACKGROUND_SERVICE,
    NEW_ON_D1,
    OLD,
    OLD_BACKGROUND_SERVICE,
    OLD_ON_D1,
    QUIET,
    STOPPED_NEW,
    STRAY,
    WITH_DATA,
    DataCase,
    background_service_of,
    data_set,
    lane_instance,
    lane_seen_of,
    on_data,
    with_database_mounting,
    with_database_on,
)

PINNED = Path(__file__).parent / "answers" / "judgements.json"
RUNNING, STOPPED, RESTARTING = InstanceState.RUNNING, InstanceState.STOPPED, InstanceState.RESTARTING
PLAIN = Product()
UNREADABLE = ProductError("The description of site is not one Launchplane reads.")
GONE_BACKGROUND_SERVICE = background_service_of(on_data(lane_instance("gone", "a"), "d1"))
UNREAD_LOGINS = D1.model_copy(
    update={"databases": (D1.databases[0].model_copy(update={"unheld_logins": None, "logins_unread": "psql: no such database"}),)}
)


def serving(*instances: object, data_sets: tuple[object, ...] = (D1,), background_services: tuple[object, ...] | None = None) -> LaneSeen:
    first = instances[0]
    assert hasattr(first, "container")
    return lane_seen_of(
        DataCase(
            instances,  # type: ignore[arg-type]
            first.container,  # type: ignore[attr-defined]
            data_sets,  # type: ignore[arg-type]
            (),
            None,
            (NEW_BACKGROUND_SERVICE,) if background_services is None else background_services,  # type: ignore[arg-type]
        )
    )


def unrouted(*instances: object, data_sets: tuple[object, ...] = (D1,), background_services: tuple[object, ...] = ()) -> LaneSeen:
    return lane_seen_of(DataCase(instances, None, data_sets, (), None, background_services))  # type: ignore[arg-type]


def with_route(seen: LaneSeen, route: LaneRoute) -> LaneSeen:
    return LaneSeen(seen.instances, route, seen.background_services, seen.data_sets, QUIET)


def changed(part: object, **change: object) -> object:
    return part.model_copy(update=change)  # type: ignore[attr-defined]


IN_MAINTENANCE_SEEN = Lane(IN_MAINTENANCE).seen()
ATTACHED_OUTSIDE = changed(UP.seen().instances[0], networks=(*UP.seen().instances[0].networks, "bridge"))

WITH_A_DATABASE: dict[str, LaneSeen] = {
    "at rest and as it must be": serving(NEW_ON_D1),
    "a new instance being prepared beside the active one": serving(
        OLD_ON_D1, NEW_ON_D1, background_services=(OLD_BACKGROUND_SERVICE,)
    ),
    "the web address moved and the old instance and its background service still there": serving(
        NEW_ON_D1, OLD_ON_D1, background_services=(OLD_BACKGROUND_SERVICE,)
    ),
    "the old background service stopped and the new one not yet started": serving(NEW_ON_D1, OLD_ON_D1, background_services=()),
    "the new background service not yet up as long as it must stay up": serving(
        NEW_ON_D1, background_services=(background_service_of(NEW_ON_D1, 1.0),)
    ),
    "a background service that is stopped": serving(NEW_ON_D1, background_services=(background_service_of(NEW_ON_D1, None),)),
    "a lane being brought up, its database not yet accepting connections": unrouted(
        NEW_ON_D1, data_sets=(data_set("d1", accepts=False),)
    ),
    "an instance being prepared that can reach out": serving(
        OLD_ON_D1, on_data(NEW, "d1", "bridge"), background_services=(OLD_BACKGROUND_SERVICE,)
    ),
    "an instance being prepared that is attached to another data set": serving(
        OLD_ON_D1, on_data(NEW, "d1", "network-d2"), data_sets=(D1, data_set("d2")), background_services=(OLD_BACKGROUND_SERVICE,)
    ),
    "the old instance's background service can reach out": serving(
        NEW_ON_D1, OLD_ON_D1, background_services=(background_service_of(OLD_ON_D1, networks=("network-d1", "bridge")),)
    ),
    "a database attached to an outside network": serving(NEW_ON_D1, data_sets=(with_database_on(D1, "network-d1", "bridge"),)),
    "a database with another data set's volume mounted too": serving(
        NEW_ON_D1, data_sets=(with_database_mounting(D1, "volume-d1", "volume-d2"),)
    ),
    "a database with nothing mounted": serving(NEW_ON_D1, data_sets=(with_database_mounting(D1),)),
    "a background service that keeps restarting": serving(
        NEW_ON_D1, background_services=(changed(NEW_BACKGROUND_SERVICE, state=RESTARTING, running_for_seconds=None),)
    ),
    "an active instance that can reach out": serving(on_data(NEW, "d1", "bridge")),
    "an active instance on another data set's network": serving(on_data(NEW, "d1", "network-d2"), data_sets=(D1, data_set("d2"))),
    "an active instance with a volume mounted": serving(changed(NEW_ON_D1, mounts=(Mount(source="volume-d2", path="/data"),))),
    "an active instance that is stopped": serving(changed(NEW_ON_D1, state=STOPPED)),
    "an active instance with no build label": serving(changed(NEW_ON_D1, build=None)),
    "an active instance whose data set is not on the host": serving(NEW_ON_D1, data_sets=()),
    "two background services": serving(
        NEW_ON_D1, background_services=(NEW_BACKGROUND_SERVICE, background_service_of(NEW_ON_D1, container="second"))
    ),
    "a background service with a volume mounted": serving(
        NEW_ON_D1, background_services=(changed(NEW_BACKGROUND_SERVICE, mounts=(Mount(source="v", path="/data"),)),)
    ),
    "a background service labelled for another data set": serving(
        NEW_ON_D1, background_services=(changed(NEW_BACKGROUND_SERVICE, data_set="d2"),)
    ),
    "a background service of another build": serving(NEW_ON_D1, background_services=(changed(NEW_BACKGROUND_SERVICE, build="a"),)),
    "a background service whose instance is gone": serving(NEW_ON_D1, background_services=(GONE_BACKGROUND_SERVICE,)),
    "a stopped instance left beside the active one": serving(
        OLD_ON_D1, on_data(STOPPED_NEW, "d1"), background_services=(OLD_BACKGROUND_SERVICE,)
    ),
    "a database that does not accept connections": serving(NEW_ON_D1, data_sets=(data_set("d1", accepts=False),)),
    "a database that is stopped": serving(NEW_ON_D1, data_sets=(data_set("d1", STOPPED),)),
    "a database that keeps restarting": serving(NEW_ON_D1, data_sets=(data_set("d1", RESTARTING),)),
    "a data set whose network is not quiet": serving(NEW_ON_D1, data_sets=(data_set("d1", quiet=False),)),
    "a data set with two database servers": serving(NEW_ON_D1, data_sets=(data_set("d1", databases=("database-a", "database-b")),)),
    "a data set with no volume and no database": serving(NEW_ON_D1, data_sets=(data_set("d1", None, volume=False),)),
    "a second data set whose database is stopped": serving(NEW_ON_D1, data_sets=(D1, data_set("d2", STOPPED))),
    "a login no container holds": serving(NEW_ON_D1, data_sets=(LEFT_ON_D1,)),
    "logins that could not be read": serving(NEW_ON_D1, data_sets=(UNREAD_LOGINS,)),
    "a lane retired with its data set kept": unrouted(),
    "a route removed by hand": unrouted(NEW_ON_D1, background_services=(NEW_BACKGROUND_SERVICE,)),
    "a route removed by hand from an instance that can reach out": unrouted(
        on_data(NEW, "d1", "bridge"), background_services=(NEW_BACKGROUND_SERVICE,)
    ),
    "the proxy down": with_route(UP.seen(), LaneRoute(None, WEB, proxy_runs=False)),
    "the proxy down and a part attached where it must not be": with_route(
        LaneSeen((ATTACHED_OUTSIDE,), UP.seen().route, UP.seen().background_services, UP.seen().data_sets),  # type: ignore[arg-type]
        LaneRoute(None, WEB, proxy_runs=False),
    ),
    "in maintenance with every part stopped": IN_MAINTENANCE_SEEN,
    "in maintenance with the web part and the database still running": Lane(IN_MAINTENANCE, RUNNING, STOPPED, database(RUNNING)).seen(),
    "in maintenance with a database that was killed": Lane(IN_MAINTENANCE, database=database(STOPPED, exit_code=KILLED)).seen(),
    "in maintenance with the back soon page not running": with_route(
        IN_MAINTENANCE_SEEN, LaneRoute(IN_MAINTENANCE_SEEN.route.loaded, BACK_SOON, SINCE, back_soon_runs=False)
    ),
    "in maintenance with the proxy down": with_route(IN_MAINTENANCE_SEEN, LaneRoute(None, BACK_SOON, SINCE, proxy_runs=False)),
    "in maintenance with a part attached where it must not be": Lane(IN_MAINTENANCE).seen().__class__(
        (changed(IN_MAINTENANCE_SEEN.instances[0], networks=(*IN_MAINTENANCE_SEEN.instances[0].networks, "bridge")),),  # type: ignore[arg-type]
        IN_MAINTENANCE_SEEN.route,
        IN_MAINTENANCE_SEEN.background_services,
        IN_MAINTENANCE_SEEN.data_sets,
        QUIET,
    ),
    "out of maintenance with the background service stopped": Lane(SERVING, RUNNING, STOPPED, database(RUNNING)).seen(),
}
assert BACKGROUND_SERVICE
PLAIN_LANES: dict[str, LaneSeen] = {
    "a plain lane as it must be": serving(NEW, data_sets=(), background_services=()),
    "a plain lane with an instance beside the active one": serving(NEW, STRAY, data_sets=(), background_services=()),
    "a plain lane whose active instance is stopped": serving(STOPPED_NEW, data_sets=(), background_services=()),
    "a plain lane with a background service the product has none of": serving(
        NEW, data_sets=(), background_services=(changed(NEW_BACKGROUND_SERVICE, data_set=None, networks=("launchplane-lanes",)),)
    ),
    "a plain lane whose instance says it runs against a data set": serving(changed(OLD, data_set="d1"), data_sets=(), background_services=()),
}
CASES = (
    {name: (seen, WITH_DATA) for name, seen in WITH_A_DATABASE.items()}
    | {name: (seen, PLAIN) for name, seen in PLAIN_LANES.items()}
    | {"a lane whose description cannot be read": (serving(NEW_ON_D1), UNREADABLE)}
)


def answered(name: str) -> dict[str, object]:
    seen, product = CASES[name]
    return {
        mode: lane_verdict(seen, product, "site", "testing", a_move_is_at_work=at_work).model_dump(mode="json")
        for mode, at_work in (("at rest", False), ("at work", True))
    }


@pytest.mark.parametrize("name", CASES)
def test_the_status_call_answers_this_for_the_lane_as_read(name: str) -> None:
    assert answered(name) == json.loads(PINNED.read_text())[name]
