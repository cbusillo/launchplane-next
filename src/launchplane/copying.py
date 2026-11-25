from dataclasses import dataclass

from launchplane import maintenance, plan, steps
from launchplane.instance import BACK_SOON, DataSet, LaneReading
from launchplane.judge import LOGIN_KINDS, Judgement, Work, judge
from launchplane.product import Product
from launchplane.steps import CopyOutcome, CopyResult, CopyVolume, Done, GiveUp, Stop, Wait


# What an attempt after a kill must know is on the host: the copy is stamped with the request, and names what it copies.
@dataclass(frozen=True)
class Copying:
    judged: Judgement
    product: Product
    generation: str
    fresh: str
    done: tuple[steps.Event, ...]

    @property
    def lane(self) -> LaneReading:
        return self.judged.lane

    @property
    def copy(self) -> DataSet | None:
        return next((each for each in self.lane.data_sets if each.generation == self.generation), None)

    @property
    def is_whole(self) -> bool:
        copy = self.copy
        return copy is not None and copy.is_whole_copy

    @property
    def given_up(self) -> str | None:
        refused = (GiveUp, steps.CopyRefused)
        return next((each.why if isinstance(each, GiveUp) else each.report for each in self.done if isinstance(each, refused)), None)

    def answer(self) -> Done[CopyResult]:
        lane, copy, active = self.lane, self.copy, self.lane.active
        kept = "What was copied is kept, and no instance is started on it. Asking again makes a new copy."
        left = "A copy was begun by an attempt of this request that did not finish it, and is never gone on with."
        result = CopyResult(
            outcome=CopyOutcome.COPIED if self.is_whole else CopyOutcome.NOT_COPIED,
            container_receiving_visitors=lane.route.sends_visitors_to,
            data_set=None if active is None else active.data_set,
            copied_to=None if copy is None else copy.data_set,
            error=None if self.is_whole else f"{self.given_up or left} {kept}",
        )
        return Done(result)


# Which instance comes back can be read only while the proxy still sends visitors to it.
def begin_step(copying: Copying) -> steps.Action | Stop:
    lane, copy, active = copying.lane, copying.copy, copying.lane.active
    source = None if active is None else lane.data_set(active.data_set)
    found = [] if active is None else plan.left_for_a_move(copying.judged, active)
    if active is None or source is None or len(lane.instances) != 1 or found:
        why = (
            plan.sentences(found) or f"it has {len(lane.instances)} instances, and must have one, receiving visitors, on a data set"
        )
        return Stop(f"{lane.lane} of {lane.product} is not as it must be to be copied: {why}. {plan.changes_so_far(copying.done)}")
    name = copying.fresh if copy is None else copy.data_set
    for files in (False, True) if source.files else (False,):
        if copy is None or not (copy.files if files else copy.volumes):
            return steps.CreateDataSetVolume(lane.product, lane.lane, name, files, source.data_set, copying.generation)
    return steps.EditRoute(lane.web_address, BACK_SOON, lane.route.installed)


def copy_step(copying: Copying) -> steps.Action:
    lane, copy, database = copying.lane, copying.copy, copying.product.database
    source = None if copy is None else lane.data_set(copy.copy_of)
    if copy is None or source is None or database is None:
        return GiveUp("The copy this request began, or the data set it is a copy of, is not on the host.")
    copied_now = {each.destination for each in copying.done if isinstance(each, CopyVolume)}
    left = sorted(each.container for each in copy.copies if each.volume not in copied_now)
    if left:
        return GiveUp(f"An earlier attempt began the copy {copy.data_set} and did not finish it: {', '.join(left)} is of it.")
    if (stop := maintenance.next_to_stop(lane)) is not None:
        return stop
    found = [finding for finding in copying.judged.findings if finding.kind not in LOGIN_KINDS]
    if found:
        return GiveUp(f"The lane is not stopped as it must be for its data to be copied: {plan.sentences(found)}.")
    sources, destinations = (*source.volumes, *source.files), (*copy.volumes, *copy.files)
    to_copy = [pair for pair in zip(sources, destinations, strict=False) if pair[1] not in copied_now]
    if len(sources) != len(destinations) or set(sources) & set(destinations) or not to_copy:
        return GiveUp(
            f"The volumes of the copy {copy.data_set} ({', '.join(destinations)}) are not one fresh volume, still to be "
            f"written, for each volume of {source.data_set} ({', '.join(sources)})."
        )
    return CopyVolume(lane.product, lane.lane, copy.data_set, *to_copy[0], database.image)


def bring_back(copying: Copying) -> steps.Action | Wait:
    lane, copy = copying.lane, copying.copy
    # A copier still reading would go on reading a volume that is being written again.
    running = next((each.container for each in (() if copy is None else copy.copies) if not each.stopped), None)
    if running is not None:
        return steps.StopPart(running)
    judged = judge(lane, copying.product, Work.END_MAINTENANCE)
    answer = maintenance.answer_for(lane.product, lane.lane, lane.route, steps.MaintenanceOutcome.ENDED)
    step = maintenance.bring_back(judged, copying.product, answer, copying.done)
    if isinstance(step, Done):
        return Wait(f"The lane could not be brought back yet, and it is tried again: {step.answer.error}")
    return Wait(step.reason) if isinstance(step, Stop) else step


def open_step(copying: Copying, cancel: steps.Cancel | None) -> plan.Step[CopyResult] | plan.StepBack:
    lane, copy = copying.lane, copying.copy
    if plan.is_turned_back(cancel) and copy is not None and copying.is_whole:
        return steps.TooLate(
            f"The copy {copy.data_set} of {copy.copy_of} is whole and {lane.web_address} is out of maintenance again, so "
            "copying could not be cancelled and is finished as asked. The copy is kept."
        )
    if plan.is_turned_back(cancel):
        return plan.put_back_or_what_is_left(copying.judged, copying.generation)
    began = copy is not None and bool(copy.copies)
    return copying.answer() if began or copying.given_up else begin_step(copying)


def decide(copying: Copying, cancel: steps.Cancel | None = None) -> plan.Step[CopyResult] | plan.StepBack:
    lane, route = copying.lane, copying.lane.route
    step = plan.unread_route(lane, "copied", copying.done) or plan.route_step(copying.judged, copying.done)
    if step is not None:
        return step
    if not route.in_maintenance:
        return open_step(copying, cancel)
    if route.installed_by.split(" ")[0] != copying.generation:
        return Stop(
            f"{lane.lane} of {lane.product} is in maintenance, and this request did not put it there, so it is left as "
            f"it is and its data was not copied. {plan.changes_so_far(copying.done)}"
        )
    if plan.is_turned_back(cancel) or copying.given_up or copying.is_whole:
        return bring_back(copying)
    return copy_step(copying)
