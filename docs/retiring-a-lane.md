# Retiring a lane

`POST /lanes/{product}/{lane}/retire` with the body `{}` retires one lane: the proxy stops sending its web address `{lane}.{product}.test` anywhere, then every job and every instance of the lane is stopped and removed. It keeps no record.

A web address left with no instance is retired by the product and lane it names. For example, `y-z.test-bed-x.test` is `/lanes/test-bed-x/y-z/retire`.

A lane's data sets are kept unless one is named in the body as `{"delete_data_set": "<data set>"}`: see [retiring a lane with data](data-sets.md#retiring-a-lane-with-data).

## What it does

It reads the host and the proxy again before every step. The next step is the first of these that applies:

1. If the proxy is not running, start it.
2. If the lane's routes file and the proxy disagree about where the web address goes, wait until the proxy has loaded the file. A route the proxy reports as disabled still counts as loaded.
3. If the lane's routes file sends the web address to an instance, delete that file. No other lane's file is touched.
4. Retire a job of the lane.
5. Retire an instance of the lane: Docker sends the stop signal, waits up to 30 s, then removes it.
6. If the body names one of the lane's data sets, delete it.
7. Answer.

The web address goes first so that no new request reaches an instance that is stopping. A request the instance already holds gets its answer only if the product finishes it within 30 s of the stop signal.

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `retired` | 200 | Something was removed. `route_removed_to_container` is the container the web address sent to, or `null`. `retired_containers` lists the jobs and instances removed. `deleted_data_set` is the data set deleted, or `null`. `kept_data_sets` lists the lane's data sets still on the host. |
| `unchanged` | 200 | The lane had no route, no job and no instance, and no data set of it was named. |

A `503` means the host or proxy could not be read or changed. Retiring stopped where it was and is safe to ask again. A retire waits for a move in the same process to finish, and a move waits for a retire.

## Known and not fixed

- **A web address of another lane that points at the retiring lane's instance is left pointing at nothing.** It needs a container removed by hand and the same 48 random bits drawn again. Comes back if container names get shorter, or if one web address can ever be sent to another lane's instance.
- **Changes no unit test catches:** accepting an instance address on any port in the proxy's loaded routes, and removing the wait for the proxy to answer after starting it. Comes back when one of them breaks without a test noticing.

## Measured

- Retiring a lane of one idle nginx instance: 2.06 to 2.14 s, mostly Traefik loading the route change. Asking again: 0.07 s.
- With a 5 s request in flight, the request answered 200 with its whole body.
- Killed after the route was gone, while the instance held a 10 s request: the instance kept running with no web address until Launchplane was asked again, the request got its answer, and asking again retired the instance in 0.97 s.

## On the test bed

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_retire.py -v
```

It uses the lane `test-bed-site/retiring` with `kennethreitz/httpbin`, pinned by digest, whose `/delay/{n}` holds a request for n seconds. Every test-bed test leaves the test bed as it found it.
