# Retiring a lane

`POST /lanes/{product}/{lane}/retire` with the body `{}` retires one lane: the proxy stops sending its web address `{lane}.{product}.test` anywhere, then every job and every instance of the lane is stopped and removed. Like the move, it keeps no record. Each call reads the lane's route from the proxy and its instances from the host.

A web address always comes from one product and lane. Product and lane hold no dots, so the web address splits only one way. A web address left with no instance and no product is retired the same way, by the product and lane it names. For example, `y-z.test-bed-x.test` is `/lanes/test-bed-x/y-z/retire`.

A lane's data sets are kept unless one is named in the body as `{"delete_data_set": "<data set>"}`; see [a lane with a database](data-sets.md#retiring-a-lane-with-data).

## What it does

Like the move, it reads again before every step and decides the next step from what it read and what this call has done (see [the move](the-move.md)). The next step is the first of these that applies:

1. If the proxy is not running, start it.
2. If the lane's routes file and the proxy disagree about which instance the web address goes to, wait until the proxy has loaded the file, the same wait as the move. A route the proxy reports as disabled or with a warning still counts as loaded, so a lane with a disabled route is retired like any other.
3. If the lane's routes file sends the web address to an instance, delete that file. The next read sees the file and the proxy disagree, so step 2 waits until the proxy reports the route gone. No other lane's file is touched.
4. Retire a job of the lane, the first in order of container name, the same way as an instance below. Jobs go before instances.
5. Retire an instance of the lane, the first in order of container name: Docker sends the stop signal, waits up to 30 s, then removes the instance.
6. If the body names one of the lane's data sets, delete it.
7. Answer with the route removed, the containers retired and the data set deleted in this call, and the data sets kept.

The web address goes first so that no new request reaches an instance that is stopping. Stopping the instances first would leave the web address sending requests to an instance that refuses them.

A request the instance already holds gets its answer only if the product finishes it within 30 s of the stop signal, and only if the product finishes held requests when it gets that signal at all. After 30 s Docker kills the instance and the request is cut off. A product that exits at once on its stop signal drops what it holds. The test bed's builds finish their requests: nginx on `SIGQUIT`, gunicorn on `SIGTERM`.

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `retired` | 200 | Something was removed. `route_removed_to_container` is the container the web address sent to, or `null` if the route was already gone. `retired_containers` lists the containers of the jobs and instances stopped and removed. `deleted_data_set` is the data set deleted, or `null`. `kept_data_sets` lists the lane's data sets still on the host. |
| `unchanged` | 200 | The lane had no route, no job and no instance, and no data set of it was named. Nothing changed. |

A `503` means the host or proxy could not be read or changed. Retiring stopped where it was and is safe to ask again. Retiring and moving share one lock, so a retire waits for a move in the same process to finish, and a move waits for a retire.

## After a crash

The proxy and the host show what is left, so asking again finishes the job. Measured on the test bed: Launchplane was killed after the route was gone and while the instance held a 10 s request. The instance kept running with no web address sending to it for at least 15 s. Nothing stops it until Launchplane is asked again. The request in flight still got its answer. Asking again retired the instance in 0.97 s, with `route_removed_to_container: null`. A third call answered `unchanged`.

## Measured

- Retiring a lane of one idle nginx instance takes 2.06 to 2.14 s, mostly Traefik loading the route change. Asking again takes 0.07 s.
- With a 5 s request in flight, the page's poll sees the web address gone and the instance still there, then the instance gone. The request answers 200 with its whole body.

## Known and not fixed

- **A web address of another lane that points at the retiring lane's instance is left pointing at nothing.** Retiring removes only the lane's own route, then removes the instance. A lane writes only its own file and only ever points its web address at an instance of that lane, but that is not enough to rule it out: move `a-b/c`, remove its container by hand and leave its route, then move `a/b-c` and get the same random suffix. The new instance of `a/b-c` takes the container name `a-b-c-<suffix>` that the route of `c.a-b.test` still names. Not fixed: it needs a container removed by hand and the same 48 random bits drawn again, which is improbable. Comes back if container names get shorter, or if one web address can ever be sent to another lane's instance, for example to share an instance between lanes.
- **Changes the unit tests do not catch.** Each of these leaves every unit test passing: removing the lock that retiring shares with the move; ignoring a failed `rm` or `mv` of a lane's routes file; accepting an instance address on any port in the proxy's loaded routes; removing the wait for the proxy to answer after starting it; and undoing the page's wording for a running instance that does not answer with success. Not fixed: the lock needs two calls at once; an ignored `rm` or `mv` leaves the file as it was, so the call takes the same step again until it stops at 50 steps with `503`, with a less direct error; a route on another port could only come from a file Launchplane did not write, and the lane's own file is checked exactly; without the startup wait, the first read of a proxy just started fails and the call answers `503`, and asking again works; only watching the page catches the wording. Comes back when one of them breaks without a test noticing, when two Launchplane calls can run at once, or when the page gets a test of its own.

## On the test bed

`tests/test_bed_retire.py` uses the lane `test-bed-site/retiring` with `kennethreitz/httpbin`, pinned by digest. It serves on port 80, `/delay/{n}` holds a request for n seconds, and gunicorn finishes held requests when it gets `SIGTERM`. That image has not been updated since 2018. It is only a pretend build here.

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_retire.py -v
```

Every test-bed test leaves the test bed as it found it: the lanes it made are retired through Launchplane, and `test-bed-site/testing` is moved back to `nginx:1.30.6-alpine`.
