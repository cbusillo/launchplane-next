# Retiring a lane

`POST /lanes/{product}/{lane}/retire` with the body `{}` retires one lane: the proxy stops sending its web address `{lane}.{product}.test` anywhere, then every background service and every instance of the lane is stopped and removed. It keeps no record.

A web address left with no instance is retired by the product and lane it names. For example, `y-z.test-bed-x.test` is `/lanes/test-bed-x/y-z/retire`.

A lane in [maintenance](maintenance.md) is retired the same way: its route to the back soon page is removed, then its stopped parts.

A lane's data sets are kept unless one is named in the body as `{"delete_data_set": "<data set>"}`: see [retiring a lane with data](data-sets.md#retiring-a-lane-with-data).

## What it does

It reads the host and the proxy again before every step. The next step is the first of these that applies:

1. If the lane's routes file is not a route Launchplane writes, stop with `503`, changing nothing: what it sends visitors to is not known. Remove or repair the file by hand, then ask again.
2. If the proxy is not running, start it.
3. If the lane's routes file and the proxy disagree about where the web address goes, wait until the proxy has loaded the file. A route the proxy reports as disabled still counts as loaded.
4. If the lane has a routes file, ask [the gate](the-move.md#the-gate) to remove it, as the file exactly as it was just read. If the gate answers that the file is no longer that one, nothing was removed and the retire starts again from step 1. No other lane's file is touched, and what the gate keeps of the lane's releases stays.
5. Retire a background service of the lane.
6. Retire an instance of the lane: Docker sends the stop signal, waits up to 30 s, then removes it.
7. If the body names one of the lane's data sets, delete it, unless it has a part twice: then stop with `503`, deleting nothing of it ([retiring a lane with data](data-sets.md#retiring-a-lane-with-data)).
8. If a running database server of the lane has a [login](data-sets.md#logins) no container holds, remove it. A server whose logins, or whose logins' holders, cannot be read does not stop the retire: the answer lists it in `logins_not_read`.
9. Answer.

The web address goes first so that no new request reaches an instance that is stopping. A request the instance already holds gets its answer only if the product finishes it within 30 s of the stop signal.

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `retired` | 200 | Something was removed, and no data set was deleted. `route_removed_to_container` is the container the web address sent to, or `null`. `retired_containers` lists the background services and instances removed. `kept_data_sets` lists the lane's data sets still on the host. |
| `deleted` | 200 | The data set named in the body was deleted by this request, and `deleted_data_set` names it, also when the attempt that sent the deletion never had its reply. Anything of the lane still running was retired first and is listed as for `retired`. |
| `unchanged` | 200 | The lane had no route, no background service and no instance, and no data set of it was named. |

The call is kept as a [request](requests.md) and answered `202` at once; with `?wait=<seconds>` it answers as above when the retire ends.

A `503` means the host answered no, or the lane is not as it must be. It also answers a retire that named a data set the lane does not have and that no attempt of the request deleted: the rest of the retire was done, `error` names the data set, and nothing was deleted. Retiring stopped where it was, its request has failed, and it is safe to ask again. A host or proxy that could not be read, and a change that was sent and not answered, do not fail the request: it stays working, and its [next attempt](requests.md#the-workers) reads the host and finishes the retire. A removal of the route not answered is fenced first. A lane's requests are worked one at a time, in the order they came. A retire can be [cancelled](requests.md#cancelling) only before it has removed the route, its first change.

## Known and not fixed

- **A web address of another lane that points at the retiring lane's instance is left pointing at nothing.** It needs a container removed by hand and the same 48 random bits drawn again. Comes back if container names get shorter, or if one web address can ever be sent to another lane's instance.
- **Changes no unit test catches:** accepting an instance address on any port in the proxy's loaded routes, and removing the wait for the proxy to answer after starting it. Comes back when one of them breaks without a test noticing.

## Measured

- Retiring a lane of one idle nginx instance: 2.06 to 2.14 s, mostly Traefik loading the route change. Asking again: 0.07 s.
- With a 5 s request in flight, the request answered 200 with its whole body.
- On November 20, with the route removed by the gate: the removal was done 0.18 to 0.20 s after the retire was asked, where a plain `rm` took 0.11 to 0.12 s. The whole retire took as long as before.
- Killed after the route was gone, while the instance held a 10 s request: the instance kept running with no web address until Launchplane was asked again, the request got its answer, and asking again retired the instance in 0.97 s.

## On the test bed

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_retire.py -v
```

It uses the lane `test-bed-site/retiring` with `kennethreitz/httpbin`, pinned by digest, whose `/delay/{n}` holds a request for n seconds. Every test-bed test leaves the test bed as it found it.
