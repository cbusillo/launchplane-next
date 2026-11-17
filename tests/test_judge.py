import json
from dataclasses import replace
from pathlib import Path

import pytest

from launchplane.api import lane_answer
from launchplane.instance import (
    BACK_SOON,
    LANE_NETWORK,
    DataSet,
    InstanceState,
    LoadedRoute,
    Mount,
    RouteStatus,
    RunningBackgroundService,
    RunningInstance,
    Unread,
)
from launchplane.judge import Work
from launchplane.product import Product, ProductError
from tests.pretend_host import (
    D1,
    NEW,
    NEW_BACKGROUND_SERVICE,
    NEW_ON_D1,
    OLD,
    OLD_BACKGROUND_SERVICE,
    OLD_ON_D1,
    STRAY,
    WITH_DATA,
    Given,
    background_service_of,
    changed,
    data_set_named,
    lane_instance,
    on_data,
    with_database,
)

PINNED = Path(__file__).parent / "answers" / "judgements.json"
RUNNING, STOPPED, RESTARTING = InstanceState.RUNNING, InstanceState.STOPPED, InstanceState.RESTARTING
PLAIN = Product()
UNREADABLE = ProductError("The description of site is not one Launchplane reads.")
GONE_BACKGROUND_SERVICE = background_service_of(on_data(lane_instance("gone", "a"), "d1"))
DATA_MOUNT = "/var/lib/postgresql"
KILLED = 137


def serving(
    *instances: RunningInstance,
    data_sets: tuple[DataSet, ...] = (D1,),
    background_services: tuple[RunningBackgroundService, ...] = (NEW_BACKGROUND_SERVICE,),
) -> Given:
    return Given(instances, (instances[0].container, instances[0].container), background_services, data_sets)


def mounted(*sources: str) -> tuple[Mount, ...]:
    return tuple(Mount(source=source, path=DATA_MOUNT) for source in sources)


WEB = on_data(lane_instance("web", "b"), "d1")
UP = Given((WEB,), ("web", "web"), (background_service_of(WEB),), (D1,))
IN_MAINTENANCE = Given(
    (changed(WEB, state=STOPPED),), (BACK_SOON, BACK_SOON), (background_service_of(WEB, None),), (data_set_named("d1", STOPPED),)
)
ATTACHED_OUTSIDE = on_data(lane_instance("web", "b"), "d1", "bridge")

WITH_A_DATABASE: dict[str, Given] = {
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
    "a lane being brought up, its database not yet accepting connections": Given(
        (NEW_ON_D1,), data_sets=(data_set_named("d1", accepts=False),)
    ),
    "an instance being prepared that can reach out": serving(
        OLD_ON_D1, on_data(NEW, "d1", "bridge"), background_services=(OLD_BACKGROUND_SERVICE,)
    ),
    "an instance being prepared that is attached to another data set": serving(
        OLD_ON_D1,
        on_data(NEW, "d1", "network-d2"),
        data_sets=(D1, data_set_named("d2")),
        background_services=(OLD_BACKGROUND_SERVICE,),
    ),
    "the old instance's background service can reach out": serving(
        NEW_ON_D1, OLD_ON_D1, background_services=(background_service_of(OLD_ON_D1, networks=("network-d1", "bridge")),)
    ),
    "a database attached to an outside network": serving(
        NEW_ON_D1, data_sets=(with_database(D1, networks=("network-d1", "bridge")),)
    ),
    "a database with another data set's volume mounted too": serving(
        NEW_ON_D1, data_sets=(with_database(D1, mounts=mounted("volume-d1", "volume-d2")),)
    ),
    "a database with nothing mounted": serving(NEW_ON_D1, data_sets=(with_database(D1, mounts=()),)),
    "a database with its volume mounted where its data is not kept": serving(
        NEW_ON_D1, data_sets=(with_database(D1, mounts=(Mount(source="volume-d1", path="/somewhere-else"),)),)
    ),
    "a database with its volume mounted twice": serving(
        NEW_ON_D1, data_sets=(with_database(D1, mounts=(*mounted("volume-d1"), Mount(source="volume-d1", path="/copy"))),)
    ),
    "a database on a network that is not its data set's": serving(
        NEW_ON_D1, data_sets=(with_database(D1, networks=("network-d2",)),)
    ),
    "a background service that keeps restarting": serving(
        NEW_ON_D1, background_services=(changed(NEW_BACKGROUND_SERVICE, state=RESTARTING, running_for_seconds=None),)
    ),
    "an active instance that can reach out": serving(on_data(NEW, "d1", "bridge")),
    "an active instance on another data set's network": serving(
        on_data(NEW, "d1", "network-d2"), data_sets=(D1, data_set_named("d2"))
    ),
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
    "a background service whose instance is gone and that can reach out": serving(
        NEW_ON_D1, background_services=(changed(GONE_BACKGROUND_SERVICE, networks=("network-d1", "bridge")),)
    ),
    "a stopped instance left beside the active one": serving(
        OLD_ON_D1, changed(NEW_ON_D1, state=STOPPED), background_services=(OLD_BACKGROUND_SERVICE,)
    ),
    "a database that does not accept connections": serving(NEW_ON_D1, data_sets=(data_set_named("d1", accepts=False),)),
    "a database that was not asked whether it accepts connections": serving(
        NEW_ON_D1, data_sets=(with_database(D1, accepts_connections=None, unheld_logins=None),)
    ),
    "a database that is stopped": serving(NEW_ON_D1, data_sets=(data_set_named("d1", STOPPED),)),
    "a database that keeps restarting": serving(NEW_ON_D1, data_sets=(data_set_named("d1", RESTARTING),)),
    "a data set whose network is not quiet": serving(NEW_ON_D1, data_sets=(data_set_named("d1", quiet=False),)),
    "a data set with two database servers": serving(
        NEW_ON_D1, data_sets=(data_set_named("d1", databases=("database-a", "database-b")),)
    ),
    "a data set with no volume and no database": serving(NEW_ON_D1, data_sets=(data_set_named("d1", None, volume=False),)),
    "a second data set whose database is stopped": serving(NEW_ON_D1, data_sets=(D1, data_set_named("d2", STOPPED))),
    "a login no container holds": serving(NEW_ON_D1, data_sets=(data_set_named("d1", unheld=("i_left",)),)),
    "logins that could not be read": serving(
        NEW_ON_D1, data_sets=(with_database(D1, unheld_logins=Unread(why="psql: no such database")),)
    ),
    "a lane retired with its data set kept": Given(data_sets=(D1,)),
    "a route removed by hand": Given((NEW_ON_D1,), background_services=(NEW_BACKGROUND_SERVICE,), data_sets=(D1,)),
    "a route removed by hand from an instance that can reach out": Given(
        (on_data(NEW, "d1", "bridge"),), background_services=(NEW_BACKGROUND_SERVICE,), data_sets=(D1,)
    ),
    "a route written and not yet loaded": Given((OLD_ON_D1, NEW_ON_D1), ("old", "new"), (OLD_BACKGROUND_SERVICE,), (D1,)),
    "a route the proxy reports as disabled": replace(serving(NEW_ON_D1), route=(LoadedRoute("new", RouteStatus.DISABLED), "new")),
    "a web address that answers with an error though its instance runs": replace(
        serving(NEW_ON_D1), web_address_fails="HTTP/1.1 500 Internal Server Error"
    ),
    "the proxy down": replace(UP, route=(None, "web"), proxy_runs=False),
    "the proxy down and a part attached where it must not be": replace(
        UP, instances=(ATTACHED_OUTSIDE,), route=(None, "web"), proxy_runs=False
    ),
    "in maintenance with every part stopped": IN_MAINTENANCE,
    "in maintenance with the web part and the database still running": replace(IN_MAINTENANCE, instances=(WEB,), data_sets=(D1,)),
    "in maintenance with a database that was killed": replace(
        IN_MAINTENANCE, data_sets=(data_set_named("d1", STOPPED, exit_code=KILLED),)
    ),
    "in maintenance with the back soon page not running": replace(IN_MAINTENANCE, back_soon_runs=False),
    "in maintenance with the route to the back soon page not yet loaded": replace(IN_MAINTENANCE, route=("web", BACK_SOON)),
    "in maintenance with the proxy down": replace(IN_MAINTENANCE, route=(None, BACK_SOON), proxy_runs=False),
    "in maintenance with a part attached where it must not be": replace(
        IN_MAINTENANCE, instances=(changed(ATTACHED_OUTSIDE, state=STOPPED),)
    ),
    "in maintenance with a login no container holds on a server still running": replace(
        IN_MAINTENANCE, data_sets=(data_set_named("d1", unheld=("i_left",)),)
    ),
    "out of maintenance with the background service stopped": replace(UP, background_services=(background_service_of(WEB, None),)),
}
PLAIN_LANES: dict[str, Given] = {
    "a plain lane as it must be": serving(NEW, data_sets=(), background_services=()),
    "a plain lane with an instance beside the active one": serving(NEW, STRAY, data_sets=(), background_services=()),
    "a plain lane whose active instance is stopped": serving(changed(NEW, state=STOPPED), data_sets=(), background_services=()),
    "a plain lane with a background service the product has none of": serving(
        NEW, data_sets=(), background_services=(changed(NEW_BACKGROUND_SERVICE, data_set=None, networks=(LANE_NETWORK,)),)
    ),
    "a plain lane whose instance says it runs against a data set": serving(
        changed(OLD, data_set="d1"), data_sets=(), background_services=()
    ),
}
CASES: dict[str, tuple[Given, Product | ProductError]] = {
    **{name: (given, WITH_DATA) for name, given in WITH_A_DATABASE.items()},
    **{name: (given, PLAIN) for name, given in PLAIN_LANES.items()},
    "a lane whose description cannot be read": (serving(NEW_ON_D1), UNREADABLE),
}


def work_done_on(name: str) -> dict[str, Work | None]:
    if name.startswith("in maintenance"):
        return {"at rest": None, "at work": Work.BEGIN_MAINTENANCE, "ending maintenance": Work.END_MAINTENANCE}
    return {"at rest": None, "at work": Work.MOVE}


# What the status call answers for the lane, a line each: its condition, each finding as shown, kind and sentence, its parts.
def answered(name: str) -> dict[str, list[str]]:
    given, product = CASES[name]
    return {
        mode: [
            answer.condition,
            *[f"{finding.shown} | {finding.kind} | {finding.sentence}" for finding in answer.findings],
            "parts: " + ", ".join(f"{container} {standing}" for container, standing in answer.parts.items()),
        ]
        for mode, work in work_done_on(name).items()
        for answer in [lane_answer(given.judged(work, product))]
    }


@pytest.mark.parametrize("name", CASES)
def test_the_status_call_answers_this_for_the_lane_as_read(name: str) -> None:
    assert answered(name) == json.loads(PINNED.read_text())[name]


def test_the_three_lists_of_sentences_are_the_findings_by_how_each_is_shown() -> None:
    given = serving(NEW_ON_D1, OLD_ON_D1, data_sets=(with_database(D1, unheld_logins=Unread(why="no answer")),))
    at_rest, moving = lane_answer(given.judged(None)), lane_answer(given.judged(Work.MOVE))
    beside, unread = (finding.sentence for finding in at_rest.findings)
    assert (at_rest.problems, at_rest.left_unfinished, at_rest.release_under_way) == ([unread], [beside], [])
    assert (moving.problems, moving.left_unfinished, moving.release_under_way) == ([unread], [], [beside])
