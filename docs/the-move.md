# The move

`POST /lanes/{product}/{lane}/move` with `{"build": "<image>"}` moves one lane to one build. The lane answers at the web address `{lane}.{product}.test`. Product and lane are lowercase letters, digits and hyphens.

For now a build is a container image reference, and the instance must answer HTTP on port 80. A product with a database, a background service or a health path of its own says so in its description: see [a lane with a database](data-sets.md). Each instance gets a new container name, `{product}-{lane}-` and 12 random hex digits. Which product, lane and build an instance is comes from its labels, never from its name.

## What it does

Each call takes one step at a time. Before every step it reads the host and the proxy again and judges the lane again, with [the judgement the status page draws](data-sets.md#the-judgement-on-a-lane), so nothing read before a step is used after it. After a crash nothing is resumed: asking again reads and goes on from there. A call that has taken 50 steps and is not finished answers `503`; what was done stays done.

The next step is the first of these that applies:

1. If the lane is in [maintenance](maintenance.md), stop with `503` and say to end maintenance first.
2. If the proxy is not running, start it.
3. If the lane's routes file and the proxy disagree about where the web address goes, wait until the proxy has loaded the file.
4. If the proxy reports the lane's route disabled or with a warning, stop with `503`, that status and what this call had changed. Retiring the lane removes the route.
5. If an instance failed its check in this call, remove it and answer `check failed`. The web address never moved.
6. If the web address goes to a running instance of this build, retire any other instance of the lane, then answer.
7. Retire any instance that is neither receiving visitors nor the one to switch to: the newest this call started, or a running instance of the build left by an interrupted move.
8. If there is no instance to switch to, start one. An instance receiving visitors that has stopped is replaced, not restarted.
9. Check the instance, unless it passed in this call and is still running.
10. Point the web address at it. Step 3 then waits for the proxy, and step 6 retires the old instance.

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `switched` | 200 | The web address now points at a running instance of this build. |
| `unchanged` | 200 | It already did. Nothing changed. |
| `cleaned up` | 200 | It already did, and what an interrupted move left was removed. |
| `check failed` | 422 | Nothing switched. `error` has the check's error and the instance's last log lines. The instance is gone. |
| `background service failed` | 422 | Only for a product with a background service: see [a move with data](data-sets.md#a-move-with-data). |

A move does not answer success while the lane's web address does not answer a request for `/` with success: it answers `503` with the sentence the status call has for it.

A `503` means the host or proxy could not be read or changed. The move stopped where it was and is safe to ask again. One move, retirement or maintenance call runs at a time in a Launchplane process; a second call waits.

**The check.** The proxy requests `http://<instance>:80<health path>`, the path real traffic takes. The instance passes once it has answered with a 2xx for 3 s without a failure, and fails if it has not passed after 20 s, with the last error.

**The old instance** is retired once the switch is loaded: Docker sends the image's stop signal, waits up to 30 s, then kills and removes it. A product must finish its requests in flight and exit on its stop signal. A request that needs longer than 30 s after the switch is cut off.

## Which instance receives visitors

The proxy's loaded routes are the only mark. A route sends one web address to one instance by its container name. `GET /running` gives each instance `receives_visitors_for`, and a move answers `container_receiving_visitors`.

A route is written by renaming a new routes file for the lane into place, and Traefik loads it up to 2 s later. A move that finds the file and the loaded route disagreeing waits for the proxy, or answers `503` with where each sends the web address and the proxy's log. A proxy that cannot be asked, or a route Launchplane did not write, stops the call with `503`.

An instance that exists and does not receive visitors is left over from a move cut short. Asking for its build finishes the move; asking for the active build removes it. The web address points at exactly one instance the whole time.

## The proxy

Traefik v3.7.18 runs on the container host as `launchplane-proxy`, on port 80, attached to `launchplane-lanes` with the instances and to `launchplane-proxy-outside`, through which port 80 is published. Launchplane refuses to start an instance while a `launchplane-lanes` that is not quiet exists; it must be removed and made again, which means retiring every lane and removing the proxy first.

- Routes are one file per lane, such as `testing.test-bed-site.test.yml`, in the volume `launchplane-proxy-routes`. Retiring a lane deletes its file.
- Traefik's API listens on `127.0.0.1:8080` inside its container only. Launchplane asks it by running `wget` inside the proxy container. No new port is opened.
- Its restart policy is `always`.

## Known and not fixed

- **A slow reader can lose the end of a large download when the old instance stops.** Measured: a 24 MiB download read at 1.8 MB/s across a move lost its last 2.4 to 2.9 MB in 4 of 4 runs. Comes back if a real product kind shows it.
- **Asking for the old build while its instance is still stopping answers `check failed` for a good build.** Confirmed once, after a kill during retirement. Asking again works, and the web address never moves.
- **Container names are 48 random bits and are not checked against names a route already points at.** A clash with a route to an instance that no longer exists would send that web address's visitors to the new instance before its check. Comes back if names get shorter.
- **A move does not repair a route the proxy reports as disabled or with a warning.** Retiring the lane and moving it again rewrites the route.
- **Two routers for one web address, one of them not written by Launchplane, can make a move answer `unchanged` for the wrong instance.** Found by the checker; not reproduced on the test bed. Comes back if anything else writes to the routes folder.
- **Untried:** HTTPS, long-lived connections, two Launchplane processes at once.

## Measured

- A move from one build to another: 6.2 to 6.4 s. Asking again for a finished move: 0.07 s.
- Five test-bed tests, run three times under a request loop: no request failed.
- Changing one lane's routes file while a second lane's stayed: 120 changes, about 9,300 requests, none failed at the proxy. Each change loaded in 0.64 to 2.27 s. Two requests failed in the Mac's lookup of `docker-lp-scratch.shiny`, before reaching the proxy.
- After `docker restart launchplane-proxy` the lane's web address answered again within 115 ms with the same route.
- Killed between starting the new instance and switching, then asked again: the leftover instance was switched to, or removed, with no request failing.
- Caddy 2.11.9 was tried first. Reloading its routes dropped 3 of 3,407 requests over 30 switches; Traefik dropped 0 of 3,426.
- The Docker SDK over the certificate connection gave everything the move needed. Nothing was missing that a container manager such as Dokploy would have given.

## On the test bed

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_move.py -v
```

Builds A, B and C are `nginx:1.30.6-alpine`, `nginx:1.31.8-alpine` and `hashicorp/http-echo:1.0`. C listens on 5678, not 80, so it fails its check. The kill tests run `systemctl kill --signal=KILL launchplane` on the control container, and systemd starts it again.
