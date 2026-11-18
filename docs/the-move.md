# The move

`POST /lanes/{product}/{lane}/move` with `{"build": "<image>"}` moves one lane to one build. The call is kept as a [request](requests.md) and answered `202` at once; with `?wait=<seconds>` it answers as below when the move ends. The lane answers at the web address `{lane}.{product}.test`. Product and lane are lowercase letters, digits and hyphens.

For now a build is a container image reference, and the instance must answer HTTP on port 80. A product with a database, a background service or a health path of its own says so in its description: see [a lane with a database](data-sets.md). Each instance gets a new container name, `{product}-{lane}-` and 12 random hex digits. Which product, lane and build an instance is comes from its labels, never from its name.

## What it does

A move is one **release**, and every release is a **generation**: a number given to the request as it is written and to no other request. It is made from the time and from chance, never counted up, so it does not repeat when Launchplane's record is lost and made again. `GET /requests/<id>` shows it as `generation`. Everything a release makes says which generation it belongs to: the label `launchplane.generation` on its instance, its background service and the network of a data set it makes, and the first line of the routes file renamed into place for it.

A move takes one step at a time. Before every step it reads the host and the proxy again and judges the lane again, with [the judgement the status page draws](data-sets.md#the-judgement-on-a-lane), so nothing read before a step is used after it. A move that has taken 50 steps and is not finished answers `503`; what was done stays done.

Where a move stands is one row of this table, picked from what the host shows and what this attempt has done. Each row has what must be true first, one action, and what a cancel does there.

| The release has | Must be true first | The one action | A cancel |
| --- | --- | --- | --- |
| nothing made | The lane is not in [maintenance](maintenance.md). The proxy runs, has loaded the lane's routes file, and reports the route enabled. | Retire whatever an earlier request or attempt left that receives no visitors. Then, if the instance receiving visitors already is this build with nothing wrong, finish as in the last row. Otherwise make the data set ready and start a new instance. | wins |
| a candidate, the instance this attempt started | the same | Check it. | wins |
| a candidate that failed its check | | Remove it, then answer `check failed`. | wins |
| a candidate that passed its check in this attempt | the same, and nothing wrong is found with the candidate or its data set | Rename the routes file to it, through [the gate](#the-gate). | wins |
| the routes file renamed for it, and the proxy not yet on it | | Start the proxy if it is not running. Wait for it to load the file. | too late |
| the proxy on it, and something else of the lane still there | The instance the file names is on the host and its route is enabled. | Retire the background service of any other instance. Start the new instance's, where the product has one, and watch it. Then retire the other instances. | too late |
| nothing else left | the same | Remove the logins no container holds, and answer. | too late |

- **A release always makes its own candidate.** An instance left by an earlier request, or by an earlier attempt of this request, is retired and never switched to.
- **The rename is the one moment a release becomes real.** Whether a release has switched is read from the first line of the routes file, whoever asks and whichever attempt renamed it. It is never read from the events or from what one attempt did.
- **After an interruption nothing is resumed.** The request is taken up again and finds its row. Before the rename it removes what the earlier attempt started and makes a candidate again. From the rename on it finishes the release.
- **A proxy that has not loaded a renamed file is waited for again,** 10 s at a time, for as long as the move has steps. The release is real by then, so a slow proxy is not its failure.
- **A cancel that wins** removes what the generation made, its background service and then its instance, and the request ends as `cancelled`. Data is never removed. A cancel that is too late changes nothing, and the release is finished as asked. See [cancelling](requests.md#cancelling).

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `switched` | 200 | The routes file was renamed for this request's generation, and the web address points at a running instance of this build. A request taken up again after a kill answers this too, though another attempt did the rename. |
| `unchanged` | 200 | It already did, and nothing was done. |
| `repaired` | 200 | It already did, and the move put something right on the lane. `did` lists what, such as an instance left by a move that failed, retired. A request that renamed the routes file never answers this. |
| `check failed` | 422 | Nothing switched. `error` has the check's error and the instance's last log lines. The instance is gone. |
| `background service failed` | 422 | Only for a product with a background service: see [a move with data](data-sets.md#a-move-with-data). |

A move does not answer success while the lane's web address does not answer a request for `/` with success: it answers `503` with the sentence the status call has for it.

`did` lists every change the move made, whatever the outcome.

A `503` means the host or proxy could not be read or changed. The move stopped where it was, its request has failed, and it is safe to ask again. A lane's requests are worked one at a time, and a newer one that asks for something else [cancels](requests.md#cancelling) a move whose routes file is not renamed yet: the instance it started is removed and the web address never moves.

**The check.** The proxy requests `http://<instance>:80<health path>`, the path real traffic takes. The instance passes once it has answered with a 2xx for 3 s without a failure, and fails if it has not passed after 20 s, with the last error.

**The old instance** is retired once the switch is loaded: Docker sends the image's stop signal, waits up to 30 s, then kills and removes it. A product must finish its requests in flight and exit on its stop signal. A request that needs longer than 30 s after the switch is cut off.

## Which instance receives visitors

The proxy's loaded routes are the only mark. A route sends one web address to one instance by its container name. `GET /running` gives each instance `receives_visitors_for`, and a move answers `container_receiving_visitors`.

A route is written by renaming a new routes file for the lane into place, and Traefik loads it up to 2 s later. Its first line, which the proxy reads as a comment, is `# launchplane.generation=<generation>`, the release it was renamed for. A routes file written to end [maintenance](maintenance.md) has no such line. A move that finds the file and the loaded route disagreeing before it has renamed anything waits for the proxy, or answers `503` with where each sends the web address and the proxy's log. A proxy that cannot be asked, or a route Launchplane did not write, stops the call with `503`.

No action of any call stops or removes a container the proxy's loaded routes send visitors to. That is checked on its own at the moment of the action, from the loaded routes and from no stamp or mark, and the call stops with `503` and says so. A retire has removed the route by then, and a move retires the old instance only once the proxy has loaded the switch.

An instance that exists and does not receive visitors is left over from a move cut short. The next move of the lane, to any build, retires it. The web address points at exactly one instance the whole time.

## The gate

The rename of a release and a cancel of it pass one gate on the container host, so exactly one of them wins. Each is one command, run in the proxy's container while it holds a lock on the file `.gate` in the routes folder:

- **The switch** renames the staged routes file into place, unless the mark `<web address>.<generation>.abandoned` is in the folder. Then it renames nothing, and the lane is put back.
- **A cancel** writes that mark, unless the first line of the routes file already names the generation. Then it marks nothing and is told it is too late.

The second of the two always finds what the first did, because the look and the change are one command under one lock on the host. Nothing is decided inside Launchplane, so a second Launchplane process, and the call that cancels while a worker is at work, are held off by the same lock as the worker. A command that was sent runs to its end on the host also when Launchplane is killed just after sending it, and what it did is read from the folder afterwards.

The lock is held for that one command only. Making and checking a candidate never holds it. A mark stays until the lane's next cancel replaces it or a retire removes it.

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
- **Only the move passes the gate.** A retire and beginning or ending maintenance are cancelled as before, by a mark in the record their worker reads between actions.
- **The gate does not refuse a command from a worker that lost its lane's turn.** It refuses a switch only after a cancel of that generation. Two Launchplane processes are held off each lane by the lane's turn, and a command already sent when the turn is lost still runs.
- **A gate command waits for the lock for as long as something holds it.** Docker's client gives up after 5 s, the request then fails, and the command it sent may still run once the lock is free. Only a test holds the lock for long.
- **A release whose rename the proxy never loads fails after 50 waits,** about 8 minutes, with the routes file naming it. Asking again goes on waiting.
- **Untried:** HTTPS, long-lived connections, two Launchplane processes at once.

## Measured

- A move from one build to another: 6.2 to 6.4 s. Asking again for a finished move: 0.07 s.
- Five test-bed tests, run three times under a request loop: no request failed.
- Changing one lane's routes file while a second lane's stayed: 120 changes, about 9,300 requests, none failed at the proxy. Each change loaded in 0.64 to 2.27 s. Two requests failed in the Mac's lookup of `docker-lp-scratch.shiny`, before reaching the proxy.
- After `docker restart launchplane-proxy` the lane's web address answered again within 115 ms with the same route.
- Caddy 2.11.9 was tried first. Reloading its routes dropped 3 of 3,407 requests over 30 switches; Traefik dropped 0 of 3,426.
- The Docker SDK over the certificate connection gave everything the move needed. Nothing was missing that a container manager such as Dokploy would have given.

## On the test bed

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_move.py -v
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_interruptions.py -v
uv run pytest tests/test_interruptions.py -v
```

Builds A, B and C of `tests/test_bed_move.py` are `nginx:1.30.6-alpine`, `nginx:1.31.8-alpine` and `hashicorp/http-echo:1.0`. C listens on 5678, not 80, so it fails its check. The kill tests run `systemctl kill --signal=KILL launchplane` on the control container, and systemd starts it again. One lets Launchplane take the move up again by itself. The others end the request in the record while Launchplane is down, to leave the lane as a move that failed halfway does.

### Holding a move and interrupting it

`tests/test_bed_interruptions.py` holds the real Launchplane at a named point of a move from A to B, interrupts it there, and reads what a visitor gets from outside the host for the whole of each case. Any error, reset or build that was not expected fails the case, and so does any change to a second lane beside it. The interruptions are a cancel by the call, a newer request for build C, a kill, a kill and then a cancel before any worker recovers, and a cancel and then a kill before the cancel is answered.

| Row | Held at |
| --- | --- |
| M0 | before the lane's turn is taken; before the first action |
| M1 | the candidate created and not started; started and not checked; and on a lane with data, after the network, the volume and the database server of a new data set are each made |
| M2 | a request of the check not yet answered; the check passed |
| M3 | before the gate; the routes file staged and the rename not yet sent |
| M4 | the rename done and its reply not yet had, with the proxy held on A; the worker waiting for a proxy held on A |
| M5 | the rename done and its reply not yet had, with the proxy on B; before the old instance is retired; and with data, before the old background service is retired, after the new one is started, and while it is watched |
| M6 | the old instance gone; and with data, before the logins no container holds are removed |
| Z | before the result is saved; after it is saved; and a move to the build already active, cancelled before its result is saved |

While those tests run, the service `launchplane` on the control container is stopped and `launchplane-held` runs in its place: `tests/held_launchplane.py`, which starts the same Launchplane with nothing changed in what it does. Every call it makes on the container host, the proxy and its record can be held, before the call is forwarded or after it has taken effect and before its caller has the reply. The holds answer on port 8701 and are files, so they outlast a kill:

```
curl -X PUT    http://launchplane.shiny:8701/holds/before.proxy.switch
curl -X PUT   "http://launchplane.shiny:8701/holds/after.proxy.pass_gate?only=mv%20"
curl -X PUT    http://launchplane.shiny:8701/holds/proxy-delivery
curl           http://launchplane.shiny:8701/holds
curl -X DELETE http://launchplane.shiny:8701/holds
curl -X POST   http://launchplane.shiny:8701/kill
```

- A hold is `before.` or `after.`, then `host`, `proxy` or `record`, then the name of the method. `?only=<text>` holds only a call whose arguments have that text: `mv ` picks the switch out of the two commands that pass the gate.
- `GET /holds` answers `held`, and `stopped_at`: the holds a call is stopped at now.
- `proxy-delivery` puts a file Traefik cannot read into the routes folder. Traefik then loads no change to any routes file and goes on serving the routes it has, and its API goes on answering. Letting go removes the file, and Traefik loads what is there. Measured on Traefik v3.7.18.
- `POST /kill` kills Launchplane as `kill -9` does, and systemd starts it again.
- The tests start the service `launchplane` again when they end. `systemctl start launchplane` on the control container does the same by hand, and so does a deploy.

A hold on a call inside the lock Launchplane keeps on the routes folder stops every reading of that folder, the status page with it. The switch is held at `proxy.pass_gate`, just outside that lock.

`tests/test_interruptions.py` runs the same rows against the pretend host, whose proxy can hold a written route unloaded, and against the pretend record and PostgreSQL. It needs no test bed and takes seconds.
