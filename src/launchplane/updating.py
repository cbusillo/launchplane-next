from collections import Counter
from dataclasses import replace

from launchplane import copying, plan, steps
from launchplane.copying import Copying
from launchplane.instance import BACK_SOON, DataSet, DataSetUse, InstanceState, LaneReading, Verdict
from launchplane.judge import Work, judge
from launchplane.plan import Move
from launchplane.steps import Done, GiveUp, MoveResult, Outcome, Stop, Wait

KEPT = (
    "Nothing was switched, and the lane serves from its own data again. What was copied is kept offline, and asking "
    "again makes a new copy."
)
NOT_FINISHED = (
    "An earlier attempt of this request put the lane behind the back soon page and did not finish, so whether its update "
    "ran is not known and its copy is not used."
)
# How often one release asks for one old copy to be deleted. One still refused then is left to the next that switches.
MOST_DELETIONS = 2
type Decided = plan.Step[MoveResult] | plan.StepBack | steps.CannotPutBack


def given_up(move: Move) -> str | None:
    whys = (
        each.why if isinstance(each, GiveUp) else each.report
        for each in move.done
        if isinstance(each, GiveUp | steps.CopyRefused | steps.UpdateRefused)
    )
    return next(whys, None)


# The copy has a network and a server only from here, where something is to run on it. The update runs once, in the
# candidate and before its check, and nothing of the data set copied is within its reach.
def candidate_step(move: Move, copy: DataSet) -> steps.Action | Stop:
    lane = move.lane
    source = lane.data_set(copy.copy_of)
    if source is None or (len(copy.volumes), len(copy.files)) != (len(source.volumes), len(source.files)):
        return Stop(
            f"The copy {copy.data_set} does not have one volume for each volume of {copy.copy_of}, so nothing is started on it."
        )
    judged = judge(lane, move.product, Work.END_MAINTENANCE)
    if not copy.networks:
        return steps.CreateDataSetNetwork(lane.product, lane.lane, copy.data_set, move.generation)
    use = plan.prepared(judged, copy.data_set, None, move.product, move.done)
    if not isinstance(use, DataSetUse):
        return use or Stop(f"{lane.product} describes no database for the copy {copy.data_set} to be served by.")
    candidate = move.candidate
    if candidate is None:
        return steps.StartInstance(move.target, move.fresh.container, use, move.generation)
    update = steps.Update(candidate.container, move.product.update)
    if update not in move.done:
        return update
    return plan.switch_or_check(replace(move, judged=judged), candidate)


# What the release started on the copy goes first, through the gate, and the copy's server stops. Then the lane comes
# back as it does after a copy, and what the candidate refused is not taken for a refusal of the instance that comes back.
def back_step(move: Move, of: Copying) -> steps.Action | Wait:
    copy = of.copy
    own = sorted((each for each in move.lane.instances if each.generation == move.generation), key=lambda each: each.container)
    if own:
        step = plan.discard(move, own[0])
        return Wait(step.reason) if isinstance(step, Stop | steps.CannotPutBack) else step
    servers = () if copy is None else copy.databases
    running = next((each.container for each in servers if each.state != InstanceState.STOPPED), None)
    if running is not None:
        return steps.StopDatabase(running)
    started = {each.container for each in move.done if isinstance(each, steps.StartInstance)}
    seen = tuple(each for each in move.done if not (isinstance(each, steps.Refused) and each.container in started))
    return copying.bring_back(replace(of, done=seen))


# Behind the back soon page nothing ends the request but the switch: whatever else happens, the lane is brought back
# first. Only the attempt that put the lane there goes on, so no update whose outcome is not known is looked into.
def frozen_step(move: Move, of: Copying, cancel: steps.Cancel | None) -> Decided:
    if plan.is_turned_back(cancel) and move.gate.verdict is Verdict.NONE:
        return steps.Abandon(move.lane.web_address, move.generation)
    refused = any(isinstance(each, steps.CheckRefused) for each in move.done)
    if move.gate.verdict is Verdict.ABANDONED or given_up(move) or refused:
        return back_step(move, of)
    if not any(isinstance(each, steps.EditRoute) and each.to == BACK_SOON for each in move.done):
        return GiveUp(NOT_FINISHED)
    copy = of.copy
    step = candidate_step(move, copy) if copy is not None and of.is_whole else copying.copy_step(of)
    return GiveUp(step.reason) if type(step) is Stop else step


# The one rule of what a lane keeps of its data: why this data set stays, or None for a copy that goes once a release
# has switched. Nothing is known to go while no instance receives visitors on a data set of the lane.
def why_kept(lane: LaneReading, found: DataSet) -> str | None:
    active = lane.active
    serving = None if active is None else lane.data_set(active.data_set)
    if serving is None:
        return "Kept: no instance receives visitors on a data set of the lane."
    if found.data_set == serving.data_set:
        return "Kept: the lane serves from it."
    if found.data_set == serving.copy_of:
        return "Kept: the last release replaced it."
    if found.copy_of is None:
        return "Kept: it is no copy, and a release deletes only copies."
    if any(each.data_set == found.data_set for each in (*lane.instances, *lane.background_services)):
        return "Kept: a part of the lane runs against it."
    return None


# A copier of a copy made from it still holds its volumes, and Docker deletes no volume a container holds, so those
# copiers go with it. What they copied has served since, or goes too.
def old_copy_step(move: Move) -> steps.DeleteDataSet | None:
    lane = move.lane
    refused = Counter(each.container for each in move.done if isinstance(each, steps.DeletionRefused))
    old = next((each for each in lane.data_sets if why_kept(lane, each) is None and refused[each.data_set] < MOST_DELETIONS), None)
    if old is None:
        return None
    readers = tuple(copier for each in lane.data_sets if each.copy_of == old.data_set for copier in each.copies)
    return steps.DeleteDataSet(old.model_copy(update={"copies": (*old.copies, *readers)}))


# Old copies go last, once the release that switched has nothing else left to do, and never keep it from its answer.
def finish_step(move: Move, cancel: steps.Cancel | None) -> Decided:
    step = plan.decide_move(move, cancel)
    return (old_copy_step(move) or step) if isinstance(step, Done) else step


def not_updated(move: Move, why: str) -> Done[MoveResult]:
    what, line_end, log = why.partition("\n")
    return Done(move.answer(outcome=Outcome.NOT_UPDATED, error=f"{what} {KEPT}{line_end}{log}"))


# A release of a product that updates its data, to another build than the one serving, is made on a copy taken inside
# the release's own freeze. Every other move is the ordinary one.
def decide(move: Move, cancel: steps.Cancel | None = None) -> Decided:
    route = move.lane.route
    if move.gate.verdict is Verdict.COMMITTED:
        return finish_step(move, cancel)
    if not move.product.update:
        return plan.decide_move(move, cancel)
    if (step := plan.loading_step(move.judged)) is not None:
        return step
    of = Copying(move.judged, move.product, move.generation, move.fresh.data_set, move.done)
    if route.in_maintenance and route.installed_by.split(" ")[0] == move.generation:
        return frozen_step(move, of, cancel)
    refused = any(isinstance(each, steps.CheckRefused) for each in move.done)
    if move.gate.verdict is Verdict.ABANDONED or plan.is_turned_back(cancel) or refused:
        return plan.decide_move(move, cancel)
    copy = of.copy
    why = given_up(move) or (NOT_FINISHED if copy is not None and copy.copies else None)
    if why is not None:
        return not_updated(move, why)
    receiving = move.instance(route.written)
    if receiving is None or receiving.build == move.target.build:
        return plan.decide_move(move, cancel)
    return plan.in_service(move) or copying.begin_step(of)
