# The move

`POST /lanes/{product}/{lane}/move` with `{"build": "<image>"}` moves one lane to one build. The lane answers at the web address `{lane}.{product}.test`. Product and lane are lowercase letters, digits and hyphens.

For now a build is a container image reference, and the instance must answer HTTP on port 80. Each instance Launchplane starts gets a new container name: `{product}-{lane}-` followed by 12 random hex digits. The prefix is for people reading `docker ps`. Which product, lane and build an instance is comes from its labels, never from its container name, so product `a-b` lane `c` and product `a` lane `b-c` stay apart. The container name is random because a route names its instance's container: a replacement that took the container name of a stopped instance receiving visitors would receive them the moment it started, before its check.

## What it does

Each call takes one step at a time. Before every step it reads the lane's instances from the container host, the lane's routes file and the route the proxy has loaded for the lane's web address. From that and what this call has already done, it decides the next step without reading or changing anything more, takes it, and reads again. Nothing read before a step is used after it, so an instance that stops while Launchplane waits is seen at the next read. After a crash nothing is resumed: the next call reads again and decides again. A call that has taken 50 steps and is still not finished stops before the next step and answers `503`; what was done stays done, and asking again goes on from there. It reads once more after its 50th step, so a call whose last step finished the work answers as finished. The limit is there so a lane whose state keeps changing under Launchplane, or a proxy that never stays running, cannot hold the lock that every other lane's move waits for.

The next step is the first of these that applies:

1. If the proxy is not running, start it. The first move ever starts the proxy this way.
2. If the lane's routes file and the proxy disagree about which instance the web address goes to, wait until the proxy has loaded the file (see below). From here the file is taken as where visitors are sent.
3. If the proxy has loaded the lane's route but reports it disabled or with a warning, the web address reaches no instance. Stop and answer `503` with that status and what this call changed before it: nothing, or for example the instance it started and the route it wrote. Retiring the lane removes the route.
4. If an instance failed its check in this call, remove it, then answer `check failed` with why. The web address never moved.
5. If the web address is routed to a running instance of this build, retire any other instance of the lane, one per step, then answer.
6. Retire any instance of the lane that is neither serving nor the instance to switch to. The instance to switch to is the newest instance this call started that the web address is not routed to, or else a running instance of the build that is not serving, left by an interrupted move.
7. If there is no instance to switch to, start one. A serving instance that has stopped is not restarted: a new instance replaces it through the check and the switch like any other. Each start gets a new random container name.
8. Check the instance (see below), unless it passed in this call and is still running. An instance that passed and has since stopped is checked again, and fails.
9. Point the web address at the instance. The next read sees the file and the proxy disagree, so step 2 waits until the proxy has loaded the route, and step 5 then retires the old instance.

To retire an instance is to stop it gracefully and then remove it (see "The old instance"). Only an instance that has never served, such as one that failed its check, is removed at once.

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `switched` | 200 | The web address now points at a running instance of this build. |
| `unchanged` | 200 | It already did. Nothing changed. |
| `cleaned up` | 200 | It already did, and instances left by an interrupted move were removed. |
| `check failed` | 422 | Nothing switched. `error` has the check's error and the instance's last log lines. The instance is gone. |

A `503` means the host or proxy could not be read or changed. The move stopped where it was and is safe to ask again.

One move runs at a time in a Launchplane process. A second call waits for the first. To take a lane away, see [retiring a lane](retiring-a-lane.md).

### The check

The proxy requests `http://<instance>:80/` on the lane network, the path real traffic takes. The instance passes once it has answered with a 2xx for 3 s without a failure. It fails if it has not passed after 20 s, with the last error, for example `wget: can't connect to remote host (172.16.0.3): Connection refused`. An instance that answers once and then crashes does not pass.

### The old instance

It is retired once the switch is loaded: Docker sends it the image's stop signal, waits up to 30 s for it to exit, then kills it and removes it. By then the proxy sends every new request to the new instance, so the old instance only finishes what it already has. A product must finish its requests in flight and exit when it gets its stop signal. The nginx images do: their stop signal is `SIGQUIT`, nginx's graceful shutdown. A request that needs longer than 30 s after the switch is cut off.

A leftover instance found by a later call is retired the same way, because it may be an old instance whose retirement a crash cut short.

There is no data, so keeping it gives nothing that the move back cannot: going back is the same move with the earlier build, and the image stays on the host so it starts at once. A kept instance would also be a second instance of the lane that every reader has to explain.

## Which instance is serving, after a crash too

The proxy's loaded routes are the only mark. Launchplane writes nothing else. A route sends one web address to one instance by its container name, and the instance's labels say which product, lane and build it is. `GET /running` gives each instance a `receives_visitors_for` field: the web address whose visitors the proxy sends to it, or `null`. A move answers the same as `container_receiving_visitors`.

Launchplane writes a route by renaming a new routes file for the lane into place, and Traefik loads it up to 2 s later. A crash in that gap leaves a file that sends the web address to the new instance while the proxy still sends it to the old one. So a move whose lane's file and loaded route disagree first waits until the proxy has loaded the file, or answers `503` with where the file and the proxy send the web address and the proxy's log. Each lane has its own routes file and writes only that one, so one lane whose route never loads cannot stop moves of the others, and no move or retirement rewrites another lane's route.

A failed read is an error, never an empty answer. Each router's status is read with it. A proxy that cannot be asked, or answers in a shape Launchplane does not know, stops the call with `503`. Only an answer with no routes from the files reads as no routes. A proxy that is not running is its own case: no web address reaches an instance, and a move or retirement starts it.

To finish or abandon a half-done move, Launchplane needs no record of its own. The host shows every instance of the lane and the proxy shows the serving one, so an instance that exists and is not serving is left over. Asking for the same build finishes the move. Asking for the serving build removes the leftover. Either way the web address pointed at exactly one instance the whole time.

Killed between starting the new instance and switching, then asked again, measured on the test bed: the leftover instance was checked and switched to, or removed, with no request failing. With a route to the new instance written but not yet loaded when the old build was asked for, the move waited for the route, then switched back to the old build, again with no request failing.

## Known and not fixed

- **A slow reader can lose the end of a large download when the old instance stops.** Measured: a 24 MiB download read at 1.8 MB/s across a move lost its last 2.4 to 2.9 MB in 4 of 4 runs. nginx counts the request done once the bytes are in its kernel send queue, exits, and Docker tears down the instance's network before the queue drains. Not fixed: it needs a large response, a throttled reader and a move at that moment. Comes back if a real product kind shows it.
- **Asking for the old build while its instance is still stopping answers `check failed` for a good build and removes that instance by force.** Confirmed once on the test bed, after a kill during retirement. Not fixed: asking again works, and the web address never moves.
- **`/running` reads the containers and the routes at two moments**, so during a switch it can show the retired instance and no serving instance.
- **Container names are 48 random bits and are not checked against container names a route already points at.** A clash with a container that exists makes Docker refuse the start, and the move answers `503`. A clash with a route to an instance that no longer exists would send that web address's visitors to the new instance before its check. Not fixed: a lane starts a handful of instances, so the chance is far below anything else here. Comes back if container names get shorter or instances are started at a rate where a clash becomes plausible.
- **A move does not repair a route the proxy reports as disabled or with a warning.** It stops with `503`, the status and what it had changed before the proxy reported it. Not fixed: Launchplane writes the same file each time, so a disabled route means something outside the file is wrong, and the proxy's log says what. Retiring the lane and moving it again rewrites the route. Comes back if the proxy can disable a route Launchplane wrote correctly.
- **Two routers for one web address, one of them not written by Launchplane, can make a move answer `unchanged` for the wrong instance.** Launchplane reads one loaded route per web address, so a second router for the same `Host` rule, from a file it did not write, can hide which instance the proxy really sends visitors to. Found by the checker, in memory; not reproduced on the test bed. Not fixed: only Launchplane writes routes files on the test bed. Comes back if anything else writes to the routes folder.
- **Untried:** HTTPS, long-lived connections, two Launchplane processes at once.

## The proxy

Traefik v3.7.18 runs on the container host as `launchplane-proxy`, on port 80, on the Docker network `launchplane-lanes` with the instances. Only `src/launchplane/proxy.py` knows it is Traefik.

- Routes are one file per lane, named for the lane's web address, such as `testing.test-bed-site.test.yml`, in the volume `launchplane-proxy-routes`. Traefik watches the folder and merges the files. Launchplane writes a lane's new file beside it as `testing.test-bed-site.test.next`, which Traefik ignores, then renames it over the lane's file, so Traefik never reads half a file. Retiring a lane deletes its file. The file is JSON, which YAML accepts. A lane's file that is not exactly what Launchplane writes for that lane is an error.
- Traefik's API listens on `127.0.0.1:8080` inside its container only. Launchplane reads `/api/rawdata` and makes its requests by running `wget` inside the proxy container over the existing certificate connection to Docker. No new port is opened and no new key exists.
- Its restart policy is `always`. After `docker restart launchplane-proxy` it answered the lane's web address again within 115 ms with the same route, read from the volume.
- A route Launchplane did not write is an error, and the proxy is left as it is.

### Why not Caddy

Caddy was tried first, as asked. Pointing a web address at another instance is one request to its admin API and the answer reads back. But every change reloads the whole configuration, including its listeners, and that drops requests. With a loop from the Mac at about 40 requests a second:

| Proxy | Switches | Requests | Failed |
| --- | --- | --- | --- |
| Caddy 2.11.9 (`caddy reload --force` of the same routes) | 30 | 3407 | 3 (`Connection reset by peer`, `Remote end closed connection without response`) |
| Caddy 2.11.9, unpaced loop | 60 | 11470 | 14 |
| Traefik v3.7.18 (file renamed) | 30, each one changing the answer | 3426 | 0 |

The same Caddy reloads from a loop on the scratch container itself failed nothing in 40 reloads, so testing from another machine is what showed it.

Against the five points:

| | Caddy | Traefik |
| --- | --- | --- |
| 1. One web address to one instance | yes | yes |
| 2. Switch with no request failing | no, as measured | yes, as measured |
| 3. Read which instance serves, after a crash | yes, admin API | yes, `/api/rawdata` |
| 4. Keeps working after a restart | yes, with `--resume` | yes, routes file on a volume |
| 5. Certificates and long-lived connections | built in; not yet tried | built in; not yet tried |

Traefik loads a change asynchronously: it waits up to 2 s to group file events. The switch is therefore confirmed by reading the loaded route back, not assumed.

### Measured

- A move from one build to another takes 6.2 to 6.4 s in 4 moves: 3 s of check, up to 2 s for Traefik to load the route, polled every 0.5 s, the old instance's stop, and a read of the host and the proxy before each of the five steps.
- Asking again for a finished move takes 0.07 s.
- Five test-bed tests, run three times with the request loop running throughout: no request failed.
- Changing one lane's file while a second lane's file stayed: 120 changes, each confirmed loaded, with a request loop on each web address. About 9,300 requests, none failed at the proxy. Each change loaded in 0.64 to 2.27 s. Two requests to the steady lane failed in the Mac's lookup of `docker-lp-scratch.shiny` (`gaierror`), before reaching the proxy. Against the address, none failed. The test-bed request loops use the host name, so a lookup failure there can fail a test without the proxy dropping anything.

## Without a container manager

The move needed from the container host: list containers by label, pull an image, start a container with labels on a network, run a command inside a container, place a file in a container, read a container's log, stop and remove a container, and one network. The Docker SDK over the existing certificate connection gives all of these. Nothing was missing that a manager such as Dokploy would have given for this move.

## On the test bed

`tests/test_bed_move.py` runs the move against the deployed service with a request loop from the Mac to `docker-lp-scratch.shiny` with the lane's web address in the `Host` header:

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_move.py -v
```

Builds A, B and C are `nginx:1.30.6-alpine`, `nginx:1.31.8-alpine` and `hashicorp/http-echo:1.0`. The two nginx builds tell themselves apart in the `Server` header. C listens on 5678, not 80, so it fails its check. The kill tests run `systemctl kill --signal=KILL launchplane` on the control container with `lp-control` once instance B exists on the scratch container. systemd starts it again.
