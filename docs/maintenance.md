# A lane in maintenance

A lane is in maintenance when its web address shows the back soon page and nothing can write to its data set. Two calls put a lane in and take it out. Nothing else does yet: no release calls them.

```
curl -X POST http://launchplane.shiny:8700/lanes/<product>/<lane>/maintenance/begin
curl -X POST http://launchplane.shiny:8700/lanes/<product>/<lane>/maintenance/end
```

Neither takes a body. Each is kept as a [request](requests.md) and answered `202` at once; with `?wait=<seconds>` it answers as below when it ends. Each reads the host and the proxy again before every step, so one interrupted at any point is finished when its request is taken up again, or by making the same call again.

## The mark

A lane is in maintenance when its own routes file sends its web address to the back soon page. Nothing else is stored. The file is read from the proxy's container whether or not the proxy runs, so a lane in maintenance reads as in maintenance while the proxy is down. `since` in an answer and on the page is the time that file was written, by the control container's clock.

While the routes file points at the back soon page:

- `move` answers `503` and says to end maintenance first. It starts, replaces and repairs nothing, the proxy included.
- `retire` works as on any lane: it removes the route, then the stopped background service and web part, and keeps the data set.

## Into maintenance

`POST /lanes/{product}/{lane}/maintenance/begin` takes the first of these that applies:

1. If the lane's routes file is not a route Launchplane writes, stop with `503`, changing nothing. Ending maintenance does the same, also on a lane that is not in maintenance.
2. If the proxy is not running, start it.
3. If the lane's routes file and the proxy disagree, wait until the proxy has loaded the file.
4. If the routes file does not send the web address to the back soon page, start the back soon page if it is not running and ask [the gate](the-move.md#the-gate) to put the route to it in place of the routes file exactly as it was just read. If the gate answers that the file is no longer that one, nothing was changed and the call starts again from step 1.
5. Stop a background service of the lane that is not stopped.
6. Stop a web part of the lane that is not stopped.
7. Stop a database server of the lane that is not stopped.
8. Answer.

Containers are stopped and never removed, and no data is touched. A lane with no data set or no background service goes in with the parts it has. A lane with nothing on the host, no route included, is refused with `503`.

It stops with `503` when everything is stopped and [the judgement on the lane](data-sets.md#the-judgement-on-a-lane) still finds something: the back soon page's container is not running, a part is attached or mounted where it must not be, or the data set has a part twice or a network that is not quiet.

Docker sends each container its image's stop signal and kills it after 30 s, or after 60 s for a database server. For PostgreSQL the signal is a fast shutdown, which ends with a checkpoint.

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `in maintenance` | 200 | The proxy has loaded the route to the back soon page, every part of the lane was read as stopped, each database server with exit code 0, and the judgement the status page draws found nothing in the lane. A second call changes nothing and answers the same. |
| `database killed` | 422 | Everything is stopped, and a database server exited with another code than 0: Docker killed it, or it had crashed. `error` names it. Asking again answers the same until maintenance is ended and begun again. |

## Out of maintenance

`POST /lanes/{product}/{lane}/maintenance/end` on a lane in maintenance takes the first of these that applies:

1. Start the proxy, or wait for it to load the routes file, as above.
2. Start the database server and wait up to 60 s until it accepts connections. A missing part of the data set is made again as a [move](data-sets.md#a-move-with-data) makes it.
3. Start the web part.
4. Check the web part as a move checks a new instance, while the web address still shows the back soon page.
5. Start the background service and watch it stay up 3 s. One read as running for less is watched too, whoever started it.
6. Remove every [login](data-sets.md#logins) no container holds.
7. Ask the gate to put the route to the web part in place of the route to the back soon page exactly as it was just read, and wait until the proxy has loaded it.
8. Answer.

The web address changes first going in and last coming out, so a call killed anywhere before step 7 leaves the lane in maintenance. Whatever the judgement still finds before step 7 stops the call there, with the back soon page still showing, so a lane never comes back with something left unsaid, and asking again says the same.

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `ended` | 200 | This call switched the web address back to the web part, and the judgement the status page draws then found nothing in the lane. |
| `not in maintenance` | 200 | The lane's routes file did not point at the back soon page. The call changed nothing, started nothing and read no part of the lane, whatever state the lane is in. If the proxy runs and has not yet loaded that file, the call waits until it has, as a move does, and answers `503` if it does not within 10 s. With the proxy down it answers at once. |
| `web part failed` | 422 | Docker refused to start the web part, or it failed its check. |
| `background service failed` | 422 | Docker refused to start the background service, or it did not stay up 3 s. |

After either failure everything is stopped again and the back soon page stays: the lane is still in maintenance. The parts are stopped first and the failed part's log is read after. `error` has why and its last log lines, or says that the log could not be read.

Every answer also has `in_maintenance`, and `since` while it is true.

It stops with `503`, leaving the back soon page, when:

- the lane has no instance or more than one, so there is no telling which received visitors. Retire the lane and move it to a build again; its data set is kept;
- the data set has a fault a move would also stop for;
- the web part passed its check and is attached or mounted where it must not be;
- the background service is missing, doubled, of another build, or attached or mounted where it must not be;
- anything else is found about the instance or its data set once everything runs.

Logins that could not be read do not keep the back soon page: the lane comes back, and the answer lists the server in `logins_not_read`.

A back soon page that is not running does not stop it: switching back is what takes visitors off it.

## The back soon page

One container on the host, `launchplane-back-soon`, serves every lane in maintenance. It is Caddy 2.11.9 run as `caddy respond`, attached only to `launchplane-lanes`, with restart policy `always`. Launchplane starts it the first time a lane goes into maintenance and never removes it.

Every path and every method answers `503` with `Retry-After: 60`, `Cache-Control: no-store` and one short page that says the site is being updated. The page is `src/launchplane/back_soon.html`; a change to it reaches the host only when the container is removed and made again.

## What stays stopped

Every web part, background service and database server is made with Docker's restart policy `unless-stopped`. Docker starts one again when it crashes or when Docker or the host restarts, unless it was stopped through Docker. So a lane in maintenance stays in maintenance across a restart of Docker or of the host. The proxy and the back soon page have the policy `always`.

## On the page

- The lane carries the pill `in maintenance`, and the time since when.
- Its web address row says `shows the back soon page`.
- A stopped part is as intended. A part that is not stopped is wrong, unless a call is at work on the lane just then.
- A database server that did not stop cleanly is wrong.
- With the proxy not running the lane is unfinished, and says the proxy is not running, as its web address row does. Beginning maintenance again starts the proxy.

`/running` says the same in [the judgement on the lane](data-sets.md#the-judgement-on-a-lane).

## Known and not fixed

- **A request the web part already holds when it is stopped** gets its answer only if the product finishes it within 30 s of the stop signal.
- **A container made before restart policies changed has the policy `always`**, and Docker starts it again after a restart even in maintenance. Nothing is converted. A move makes a web part and a background service again; a database server keeps its policy until it is removed and made again.
- **A database server that does not accept connections within 60 s while maintenance ends** stops the call with `503` and is left running behind the back soon page. Beginning maintenance again stops it.
- **If the container host refuses to run the web part's check while maintenance is ending**, a host error and not a failed check, the call answers `503` and the web part and the database server are left running behind the back soon page. The page shows them as wrong. Beginning or ending maintenance again puts it right.
- **A back soon page container that is gone while a lane is in maintenance is not started by beginning that lane's maintenance again.** That call answers `503` and says the container is not running, the web address answers the proxy's own error, and the page shows it as wrong. Docker brings the container back after a crash or a restart. Comes back if anything ever removes it.
- **A lane with a second data set that no instance uses, put in maintenance and ended,** comes back, and the call then answers `503` naming that data set's stopped database server. Asked again it answers `not in maintenance`. Ending maintenance starts only the server its instance uses. What a lane with two data sets may be is settled by the release that copies data.
- **A change of the route that the container host accepted and did not answer does not end the call.** Its request stays working, and its next attempt fences the change, reads the route and finishes: a lane is not left showing the back soon page with a part still running.
- **A call ending maintenance that is killed after it wrote the route back** has finished its work. Asked again it waits for the proxy to load the file and answers `not in maintenance`, not `ended`.
- **A proxy container removed by hand takes the routes files out of reach** until a call starts the proxy again on the same volume. Until then no lane reads as in maintenance.
- **The test-bed kill tests cannot say at which action the kill landed.** They show that the same call asked again reaches the same end. The unit tests cover each action.
- **Untried:** a paused part, and a lane whose route the proxy reports as disabled.

## Measured

On the test bed, November 16, with the pretend ledger product (a web part, a background service writing a row every 0.2 s, PostgreSQL 18):

- Into maintenance: 2.8 to 3.5 s. Asking again: 0.07 to 0.09 s.
- Out of maintenance: 10.0 to 10.2 s, of which the check is 3 s and the watch on the background service 3 s. Asking again: 0.09 to 0.11 s.
- A plain nginx lane: 2.75 s in and 5.80 s out.
- In maintenance, a `GET` and a `POST` at the web address answered `503` with `Retry-After: 60` and `Cache-Control: no-store`, the row was not stored, and `pg_isready` from a container on the data set's network got no answer.
- After `systemctl restart docker` on the scratch container the lane was still in maintenance: every part stopped, no container of the lane running, the back soon page answering.
- Out again: every row from before was there, no row had been written between the two calls, and a write through the web part and one by the background service arrived.
- PostgreSQL stopped with exit code 0 each time.
- A web part killed with `SIGKILL` on the host was started again by Docker.
- Killed at three points going in and three coming out, then asked again: each reached the same end with its rows.
- busybox `wget` in the proxy's image shows only the status line of a `503`, not its headers, so the back soon page is judged by whether its container runs. The test-bed tests read the headers from outside.

## On the test bed

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_maintenance.py -v
```

It uses the lanes `test-bed-ledger/testing` and `test-bed-site/maintenance`, restarts Docker on the scratch container once, and removes what it made. The back soon page container stays.
