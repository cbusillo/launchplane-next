# Retiring a lane

`POST /lanes/{product}/{lane}/retire` with no body retires one lane: the proxy stops sending its name `{lane}.{product}.test` anywhere, then every copy of the lane is stopped and removed. Like the move, it keeps no record. Each call reads the lane's route from the proxy and its copies from the host.

A name always comes from one product and lane. Product and lane hold no dots, so the name splits only one way. A name left with no copy and no product is retired the same way, by the product and lane it names. For example, `y-z.test-bed-x.test` is `/lanes/test-bed-x/y-z/retire`.

Retiring a lane that holds data is not decided. Chris decides what happens to that data before a lane with data can be retired.

## What it does

Like the move, it reads, decides, then acts (see [the move](the-move.md)).

1. If the lane's routes file and the proxy disagree, it waits until the proxy has loaded the file, the same wait as the move.
2. If the lane's routes file sends the name to a copy, it deletes that file and waits until the proxy reports the route gone. No other lane's file is touched.
3. It retires every copy of the lane, in name order: Docker sends the stop signal, waits up to 30 s, then removes the copy.

The name goes first so that no new request reaches a copy that is stopping. Stopping the copies first would leave the name sending requests to a copy that refuses them.

A request the copy already holds gets its answer only if the product finishes it within 30 s of the stop signal, and only if the product finishes held requests when it gets that signal at all. After 30 s Docker kills the copy and the request is cut off. A product that exits at once on its stop signal drops what it holds. The test bed's builds finish their requests: nginx on `SIGQUIT`, gunicorn on `SIGTERM`.

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `retired` | 200 | Something was removed. `route_removed_to` is the copy the name sent to, or `null` if the name was already gone. `retired_copies` lists the copies stopped and removed. |
| `unchanged` | 200 | The lane had no route and no copy. Nothing changed. |

A `503` means the host or proxy could not be read or changed. Retiring stopped where it was and is safe to ask again. Retiring and moving share one lock, so a retire waits for a move in the same process to finish, and a move waits for a retire.

## After a crash

The proxy and the host show what is left, so asking again finishes the job. Measured on the test bed: Launchplane was killed after the route was gone and while the copy held a 10 s request. The copy kept running with no name for at least 15 s. Nothing stops it until Launchplane is asked again. The request in flight still got its answer. Asking again retired the copy in 0.97 s, with `route_removed_to: null`. A third call answered `unchanged`.

## Measured

- Retiring a lane of one idle nginx copy takes 2.47 to 2.52 s, mostly Traefik loading the route change. Asking again takes 0.10 s.
- With a 5 s request in flight, the page's poll sees the name gone and the copy still there, then the copy gone. The request answers 200 with its whole body.

## Known and not fixed

- **A name of another lane that points at the retiring lane's copy is left pointing at nothing.** Retiring removes only the lane's own name, then removes the copy. Not fixed: a lane writes only its own file and only ever points its name at a copy of that lane, so this needs a route written outside Launchplane. Comes back if one name can ever be sent to another lane's copy, for example to share a copy between lanes.
- **Changes the unit tests do not catch.** Each of these leaves every unit test passing: removing the lock that retiring shares with the move, and undoing the page's wording for a running copy that does not answer with success. Not fixed: the lock needs two calls at once, and only watching the page catches the wording. Comes back when one of them breaks without a test noticing, or when the page gets a test of its own.

## On the test bed

`tests/test_bed_retire.py` uses the lane `test-bed-site/retiring` with `kennethreitz/httpbin`, pinned by digest. It serves on port 80, `/delay/{n}` holds a request for n seconds, and gunicorn finishes held requests when it gets `SIGTERM`. That image has not been updated since 2018. It is only a pretend build here.

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_retire.py -v
```

Every test-bed test leaves the test bed as it found it: the lanes it made are retired through Launchplane, and `test-bed-site/testing` is moved back to `nginx:1.30.6-alpine`.
