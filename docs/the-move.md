# The move

`POST /lanes/{product}/{lane}/move` with `{"build": "<image>"}` moves one lane to one build. The call is kept as a [request](requests.md) and answered `202` at once; with `?wait=<seconds>` it answers as below when the move ends. The lane answers at the web address `{lane}.{product}.test`. Product and lane are lowercase letters, digits and hyphens.

For now a build is a container image reference, and the instance must answer HTTP on port 80. A product with a database, a background service or a health path of its own says so in its description: see [a lane with a database](data-sets.md). Each instance gets a new container name, `{product}-{lane}-` and 12 random hex digits. Which product, lane and build an instance is comes from its labels, never from its name.

## What it does

A move is one **release**, and every release is a **generation**: a number given to the request as it is written and to no other request. It is made from the time and from chance, never counted up, so it does not repeat when Launchplane's record is lost and made again. `GET /requests/<id>` shows it as `generation`. The instance, the background service and the network of a data set a release makes carry it as the label `launchplane.generation`.

Whether a release is real is answered in one place, [the gate](#the-gate) on the container host: for a generation it says `none`, `committed` to one instance, or `abandoned`, and that answer is for good. A move takes one step at a time. Before every step it reads the host and the proxy, and then asks the gate, so the gate's answer is never older than the reading beside it. The gate's answer alone picks what the move does next. The routes file and the proxy's loaded route say where visitors go, and never whether the release happened. A move that has taken 50 steps and is not finished ends nothing: its request stays working and is taken up again.

| The gate answers | The lane as read | The one next action | A cancel |
| --- | --- | --- | --- |
| `none` | the routes file or the proxy names an instance of this release | Wait: that contradicts the gate, and nothing is touched on a contradiction. | is carried to the gate |
| `none` | an instance this attempt started | Check it. One that failed its check goes through the gate and is removed, and the move answers `check failed`. One that passed is switched to, by asking the gate. | is carried to the gate |
| `none` | no such instance | The lane must not be in [maintenance](maintenance.md), its routes file must be one Launchplane writes, and the proxy must run and have loaded it. If the instance receiving visitors already is this build with nothing wrong, the move finishes around it. Otherwise whatever an earlier request or attempt left goes through the gate and is removed, the data set is made ready, and a new instance is started. | is carried to the gate |
| `committed` | the instance the gate names is not on the host | Stop, and touch nothing else. The request stays working. | too late |
| `committed` | the proxy does not send visitors to it yet | Start the proxy if it is not running. Wait for it to send visitors there. | too late |
| `committed` | the proxy sends visitors to it | Retire the background service of any other instance. Start the new instance's, where the product has one, and watch it. Only once it has stayed up, remove the other instances. Remove the logins no container holds, and answer `switched`. While the logins of its data set cannot be read, wait. | too late |
| `abandoned` | the routes file or the proxy names an instance of this release, or a part's release cannot be read | Wait, and touch nothing. | won |
| `abandoned` | anything of this generation is left | Remove its background service, then take its instance through the gate and remove it. Data stays. | won |
| `abandoned` | nothing of it is left | Remove the logins no container holds, where they can be read, and end as `cancelled`. | won |

- **A release always makes its own candidate.** An instance left by an earlier request, or by an earlier attempt of this request, is never switched to.
- **An instance of a release is removed in one way,** through the gate. The gate is asked about the generation that made it. If it answers that the routes file was renamed to that very instance, the instance stays, and where it is of the release being worked that release is finished as the committed one. Otherwise the gate has taken away every permission to switch to it, and it is removed. An instance an earlier release was renamed to is retired only once this release is committed to another instance and the proxy sends visitors there with the route enabled. An instance whose release cannot be read from its label, or that the routes file or the proxy names, is not removed: the move waits.
- **The proxy's loaded route is used for three things:** to say where visitors go, to wait until the proxy sends them to the committed instance before the old one is retired, and as the guard at the moment of every stop or removal.
- **After an interruption nothing is resumed.** The request is taken up again, reads the host, asks the gate, and goes on from the answer.

**How a request may end** follows the gate too:

| The gate answers | The request may end as |
| --- | --- |
| `committed` | `done` and `switched` only, once the instance serves and the rest is finished. Until then it stays `working`, with why in `error`, and is taken up again by itself, ahead of newer work on its lane, however often. Neither a retry nor a give-up ends it. One thing ends it otherwise: a database server that refuses to remove a login no container holds. The request then ends `failed` with the server's error, and the release stays switched. |
| `abandoned` | `cancelled`, once the lane is put back. It stays `working` while the host cannot be read. It ends as `failed` only when Docker refused to remove a part of the release, its instance or its background service, and the part is still there, and `error` then says that the cancel won and what could not be removed. |
| `none` | `failed`, `unchanged` or `repaired`, and only after the gate has confirmed `none` and refused every switch of this attempt and earlier ones, also one still on its way. Before `failed` is written, the instance the release started is removed, through the gate and the guard, and a fresh reading shows it gone. A background service the move started for the instance that already serves is left: it is live work, and only a cancel takes it back. Where a server's logins could not be read then, `error` says that whether a login was left is not known. A call the host did not answer, a wait, a login list a finished move still needs, two routers for the web address, a guard that refused, or 50 steps is no failure: nothing is ended, and the request is taken up again after 5 s. |
| no answer | nothing. No ending is written and the attempt is not counted. The request is taken up again after 5 s. |

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `switched` | 200 | The gate says the routes file was renamed for this request's generation, and the web address points at a running instance of this build. A request taken up again after a kill answers this too, though another attempt did the rename. |
| `unchanged` | 200 | It already did, and nothing was done. |
| `repaired` | 200 | It already did, and the move put something right on the lane. `did` lists what, such as an instance left by a move that failed, retired. A request that renamed the routes file never answers this. A request taken up more than once answers for all its attempts: `did` then lists what each changed, in the words of the request's events, and a request whose last attempt changed nothing still answers `repaired`. |
| `check failed` | 422 | Nothing switched. `error` has the check's error and the instance's last log lines. The instance is gone. |
| `background service failed` | 422 | Only for a product with a background service, and only where nothing was switched: see [a move with data](data-sets.md#a-move-with-data). Under a release that is switched, the request stays working. |

A move does not answer success while the lane's web address does not answer a request for `/` with success: it answers `503` with the sentence the status call has for it.

`did` lists every change the move made, whatever the outcome.

A `503` means the move stopped where it was with nothing switched, the gate has confirmed that, and what the move made is gone. Its request has failed, and it is safe to ask again. A build Docker answers it does not have ends this way; a host that does not answer leaves the request working. A lane's requests are worked one at a time, and a newer one that asks for something else [cancels](requests.md#cancelling) a move whose routes file is not renamed yet: the instance it started is removed and the web address never moves.

**The check.** The proxy requests `http://<instance>:80<health path>`, the path real traffic takes. The instance passes once it has answered with a 2xx for 3 s without a failure, and fails if it has not passed after 20 s, with the last error.

**The old instance** is retired once the switch is loaded: Docker sends the image's stop signal, waits up to 30 s, then kills and removes it. A product must finish its requests in flight and exit on its stop signal. A request that needs longer than 30 s after the switch is cut off.

## Which instance receives visitors

The proxy's loaded routes are the only mark. A route sends one web address to one instance by its container name. `GET /running` gives each instance `receives_visitors_for`, and a move answers `container_receiving_visitors`.

A route is written by renaming a new routes file for the lane into place, and Traefik loads it up to 2 s later. The file holds the route and nothing about any release. A first line the proxy reads as a comment, such as the code before the gate wrote, is not part of the route.

A routes file that is otherwise not exactly the route Launchplane writes for that web address is that lane's fault and no other lane's: the lane shows it as wrong, and a move of the lane stops and says so. Every other lane is read as before.

A move that finds the file and the loaded route disagreeing before it has switched anything waits for the proxy, or stops with where each sends the web address and the proxy's log. A proxy that cannot be asked, or a loaded route Launchplane did not write, stops the call.

No action of any call stops or removes a container the proxy's loaded routes send visitors to. That is checked on its own at the moment of the action, from the loaded routes and from no stamp or mark, and the call stops with `503` and says so. A database server about to be stopped or deleted is checked the same way. While the proxy has two routers for one web address, of any lane, where those visitors go is not known, and nothing is stopped or removed. A retire has removed the route by then, and a move retires the old instance only once the proxy has loaded the switch.

### Two routers for one web address

The proxy must have one router for a web address. When it has loaded two, from a second file in the routes folder, that is a conflict, also when both send the web address to the same instance, and in whatever order the proxy lists them. Neither is taken for where visitors go.

- The lane's `condition` is `conflicting`, with one finding that names each router, the container it sends to and its status. The web address says `conflicting` with the same sentence, and has no `sends_to_container`.
- No call changes anything on that lane. A move stays `working` with the same sentence in `error`, starts, switches and removes nothing, and is taken up again every 5 s. Once the proxy has one router it goes on by itself. A retire and the maintenance calls answer `503` with the sentence, having changed nothing.
- Nothing puts it right by itself: remove the file Launchplane did not write.

An instance that exists and does not receive visitors is left over from a move cut short. The next move of the lane, to any build, retires it. The web address points at exactly one instance the whole time.

## The gate

The gate is the one place that answers, for a generation, `none`, `committed` or `abandoned`, and the answer is for good. Everything that settles a release is one command, run in the proxy's container while it holds a lock on the folder the routes folder is in. What it decides is kept in the folder `gate` beside the routes folder, as files named for the web address and the generation:

- **The switch proves itself.** Under the lock the staged file is finished and made durable, then the `armed` mark, and then the routes file is put in place by renaming the staged file. While a generation is armed, only that rename takes its staged file away. Armed with the staged file there is `none`. Armed with the staged file gone is `committed` to that candidate. Nothing is written after the rename, and no reply has to arrive: a command killed just after the rename, and one whose reply is lost, are answered `committed` when asked again.
- **A cancel** makes the generation `abandoned` if it has no verdict, then takes its arming away, then removes every staged file of the generation and looks that none is left. Sent again it does the same, also where the arming is already gone.
- **A discard** names the generation, the attempt and the exact candidate. In the one command it either learns that the candidate was renamed to, and the answer is `committed` to it, or it takes away the permission to switch to it, and then the candidate may be removed.
- **A fence** confirms `none` and refuses every switch of an attempt up to a number, also one still on its way.
- **Asking** for a generation's verdict changes nothing on the host, also where nothing was ever kept.
- **A route that is not a file,** such as a folder of that name, is refused before anything is staged: the command fails, and the generation keeps no verdict.

Every command answers with the verdict as it then holds. Whatever takes permission away keeps the revocation first, then removes the arming, then the staged file, and a command cut short and sent again goes in the same order. A command sent again returns the first verdict and stages nothing. A generation with a verdict is never renamed for again, a discarded candidate is never armed again, and another candidate is armed only by a later attempt.

A file that cannot be looked for is never taken as absent: the command then fails, and its sender has no answer, which is not `none`. A path named `gate` that is there and is no folder gives no answer either.

The gate is a command in the proxy's container, so a move starts a proxy that is absent or stopped before it asks the gate anything.

Nothing is decided inside Launchplane, so a second Launchplane process, and the call that cancels while a worker is at work, are held off by the same lock as the worker. The lock is held for the one command only. Making and checking a candidate never holds it.

**Where the evidence is kept, and for how long.** The folder `gate` is not removed when a lane is retired, and a later release of the lane changes nothing in an earlier generation's files. Nothing removes them yet, and a reading of the routes takes none of them off the host. A generation's files are safe to remove once its request has ended in the record and no command for it can still arrive: no Launchplane process is working it, and no command for it is left running in the proxy's container, which a restart of that container ends.

## The proxy

Traefik v3.7.18 runs on the container host as `launchplane-proxy`, on port 80, attached to `launchplane-lanes` with the instances and to `launchplane-proxy-outside`, through which port 80 is published. Launchplane refuses to start an instance while a `launchplane-lanes` that is not quiet exists; it must be removed and made again, which means retiring every lane and removing the proxy first.

- Routes are one file per lane, such as `testing.test-bed-site.test.yml`, in the folder `routes` of the volume `launchplane-proxy-routes`. Retiring a lane deletes its file.
- The gate's folder `gate` is beside `routes` in the same volume, so the gate's rename stays inside one file system. Reading where visitors go fetches the folder `routes` and nothing else, also while the proxy is stopped.
- The proxy's container makes the folder `routes` before it starts Traefik, which watches no folder that is missing when it starts. Its log then begins with `'sh' is not a Traefik command: assuming shell execution`.
- Traefik's API listens on `127.0.0.1:8080` inside its container only. Launchplane asks it by running `wget` inside the proxy container. No new port is opened.
- Its restart policy is `always`.

### A host whose routes are still beside the folder `gate`

Before November 20 the routes files lay in the volume itself, with the folder `gate` among them. Such a host is moved once, with Launchplane stopped. Visitors get no answer from the moment the proxy is stopped until it runs again.

```
lp-control 'systemctl stop launchplane launchplane-held'
lp-scratch 'set -eu
volume=$(docker volume inspect launchplane-proxy-routes --format "{{.Mountpoint}}")
docker stop launchplane-proxy
mkdir "$volume/routes"
find "$volume" -mindepth 1 -maxdepth 1 ! -name routes ! -name gate -exec mv {} "$volume/routes/" \;
docker rm launchplane-proxy'
./scripts/deploy-control
curl -X POST -H 'Content-Type: application/json' -d '{"build": "<the build it runs>"}' 'http://launchplane.shiny:8700/lanes/<product>/<lane>/move?wait=60'
```

- The deploy stops unless `/etc/launchplane/bed.env` [names the container host](what-is-running.md#choosing-the-test-bed).
- Every file that is not the gate's goes into `routes` with its content and time kept, also one Launchplane did not write. Nothing in `gate` is touched.
- The proxy is removed because a restarted container keeps the command it was made with, and that names the old folder.
- The last line asks any one lane for the build it already runs. The move finds no proxy, makes it with the new folder and answers `repaired`. Every lane then reads `good` again.

Done on the test bed on November 20: 6 routes files and a stray empty file moved, 3,174 gate files kept, and all 6 lanes answered and read `good` after.

## Known and not fixed

- **A slow reader can lose the end of a large download when the old instance stops.** Measured: a 24 MiB download read at 1.8 MB/s across a move lost its last 2.4 to 2.9 MB in 4 of 4 runs. Comes back if a real product kind shows it.
- **Asking for the old build while its instance is still stopping answers `check failed` for a good build.** Confirmed once, after a kill during retirement. Asking again works, and the web address never moves.
- **Container names are 48 random bits and are not checked against names a route already points at.** A clash with a route to an instance that no longer exists would send that web address's visitors to the new instance before its check. Comes back if names get shorter.
- **A move does not repair a route the proxy reports as disabled or with a warning.** Retiring the lane and moving it again rewrites the route.
- **What a request did across attempts is sorted by how each told line begins.** A request taken up more than once lists as changes the lines of its events that do not begin as a wait, a check, a watch, a log read or a mark at the gate. A removal of logins that found none left to remove is listed as one.
- **Two routers on another lane's web address can stop a retire after its route is removed.** The retire removes its lane's route first, and the guard then refuses to remove any container while any web address has two routers. That lane has lost its visitors, and its retire has ended as `failed`. Ask again once the other lane has one router.
- **A second router that appears between a move's last reading and its rename does not stop that rename.** The move finds the conflict at its next reading and holds from there, with the routes file already renamed.
- **A router Launchplane cannot make sense of hides a conflict.** A second router for a web address whose target is on another port, or is not an instance, makes the whole reading of the proxy fail: `/running` answers `503` with why, and does not name the two routers.
- **A retire or a maintenance call refused for two routers is not taken up again when there is one.** It has ended as `failed`. Ask again.
- **Only the move passes the gate.** A retire and beginning or ending maintenance are cancelled as before, by a mark in the record their worker reads between actions.
- **The gate does not know whose turn a lane is.** It refuses a switch after a cancel of its generation, after its candidate was discarded, and after its attempt was fenced. A worker that lost its lane's turn, and whose attempt nobody has fenced, can still switch.
- **A gate command waits for the lock for as long as something holds it.** Docker's client gives up after 5 s, and the caller then has no answer: the request stays working and is taken up again. Only a test holds the lock for long.
- **A release whose instance never serves keeps its lane.** It stays `working` with the reason, is taken up again every 5 s, a cancel is told too late, and a newer request waits behind it. Nobody is told. Each time it is taken up is an attempt in the record.
- **What the gate keeps is never cleaned up.** Each release leaves one or two small files in the folder `gate`, also after its lane is retired. A gate command looks through the whole folder: with 3,188 files a verdict took 0.15 s on the host, 0.1 s more than a command that does nothing.
- **Two gate commands of one Launchplane go one after the other,** by a lock in the process that also keeps a reading of the routes folder from finding a file half put in place. A cancel so waits behind a switch that is waiting for the host's lock.
- **A move the host does not answer for never fails by itself.** It stays `working` and is taken up again every 5 s until the host answers. A cancel ends it.
- **A routes file or a proxy that names an instance of a release the gate has not committed is waited on for good.** Nothing puts that right by itself.
- **Untried:** HTTPS, long-lived connections, two Launchplane processes at once.

## Measured

- A move from one build to another: 6.2 to 6.4 s. Asking again for a finished move: 0.07 s.
- Five test-bed tests, run three times under a request loop: no request failed.
- Changing one lane's routes file while a second lane's stayed: 120 changes, about 9,300 requests, none failed at the proxy. Each change loaded in 0.64 to 2.27 s. Two requests failed in the Mac's lookup of `docker-lp-scratch.shiny`, before reaching the proxy.
- After `docker restart launchplane-proxy` the lane's web address answered again within 115 ms with the same route.
- Caddy 2.11.9 was tried first. Reloading its routes dropped 3 of 3,407 requests over 30 switches; Traefik dropped 0 of 3,426.
- On November 18, the 111 cases of `tests/test_bed_interruptions.py`, each a move held at one point and interrupted one way, passed in 35 minutes. No probe of a web address failed in any of them, and the lane beside was never touched.
- A cancel sent while a release was held just after its rename was answered `409` while the old build still answered visitors.
- On November 19, all 139 test-bed tests passed in 50 min 7 s, the four of `tests/test_bed_host_answers.py` in 2 min 47 s.
- A repaired background service whose move waited on a refused login read kept one container and wrote 41 beats in 8 s. The request was taken up again every 5 s and ended `done` within 8 s of the read answering.
- A second routes file made the lane `conflicting` within 5 s. A move asked then stayed `working`, and ended `switched` within 25 s of the file being removed.
- On November 20, with 3,174 files in the folder `gate` and none removed: a reading of the routes transferred 8 KiB where it had transferred 3.1 MB, and `/running` answered in 0.37 to 0.49 s where it had taken 0.78 s. Ten releases of one lane took the folder from 3,175 to 3,186 files. A switch and a cancel sent twice more for the first of those releases, and for a release cancelled after them, each got the first answer again, `committed` to the same instance and `abandoned`, and changed no file.
- The Docker SDK over the certificate connection gave everything the move needed. Nothing was missing that a container manager such as Dokploy would have given.

## On the test bed

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_move.py -v
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_interruptions.py -v
uv run pytest tests/test_interruptions.py -v
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_host_answers.py -v
```

Which bed the tests run against is [chosen by settings](what-is-running.md#choosing-the-test-bed), and these lines name the first one. Builds A, B and C of `tests/test_bed_move.py` are `nginx:1.30.6-alpine`, `nginx:1.31.8-alpine` and `hashicorp/http-echo:1.0`. C listens on 5678, not 80, so it fails its check. The kill tests run `systemctl kill --signal=KILL launchplane` on the control container, and systemd starts it again. One lets Launchplane take the move up again by itself. The others end the request in the record while Launchplane is down, to leave the lane as a move that failed halfway does.

`tests/test_bed_host_answers.py` runs under the same harness as the interruptions below, with the product `test-bed-answers`. It refuses the reading of a database server's logins after a repaired background service has passed its watch, and reads that service's container and the rows it writes from the scratch container while the request stays `working`. It puts a database server on another volume in a data set's place, asks for a start and then a retire. And it puts a second routes file into the proxy's folder, once with a router listed before the lane's own and once after.

### Holding a move and interrupting it

`tests/test_bed_interruptions.py` holds the real Launchplane at a named point of a move from A to B, interrupts it there, and reads what a visitor gets from outside the host for the whole of each case. Any error, reset or build that was not expected fails the case, and so does any change to a second lane beside it. The interruptions are a cancel by the call, a newer request for build C, a kill, a kill and then a cancel before any worker recovers, and a cancel and then a kill before the cancel is answered. One pause point of each row is run with all five, and every other pause point with one, 59 cases. Two more hold a rename that Docker has accepted inside the gate, by holding the gate's lock from outside, and kill Launchplane there. The whole table, every pause point with every interruption, runs against the pretend host.

| Row | Held at |
| --- | --- |
| M0 | before the lane's turn is taken; before the first action |
| M1 | the candidate created and not started; started and not checked; and on a lane with data, after the network, the volume and the database server of a new data set are each made |
| M2 | a request of the check not yet answered; the check passed |
| M3 | before the gate; and with the candidate's routes file staged and armed as the gate keeps them just before its rename |
| M4 | the rename done and its reply not yet had, with the proxy held on A; the worker waiting for a proxy held on A |
| M5 | the rename done and its reply not yet had, with the proxy on B; before the old instance is retired; and with data, before the old background service is retired, after the new one is started, and while the new one is watched |
| M6 | the old instance gone; and with data, before the logins no container holds are removed |
| Z | before the result is saved; after it is saved; and a move to the build already active, cancelled before its result is saved |

While those tests run, the service `launchplane` on the control container is stopped and `launchplane-held` runs in its place: `tests/held_launchplane.py`, which starts the same Launchplane with nothing changed in what it does. Every call it makes on the container host, the proxy and its record can be held, before the call is forwarded or after it has taken effect and before its caller has the reply. The holds answer on port 8701 and are files, so they outlast a kill:

```
curl -X PUT    http://launchplane.shiny:8701/holds/before.proxy.switch
curl -X PUT    http://launchplane.shiny:8701/holds/after.proxy.switch
curl -X PUT    http://launchplane.shiny:8701/holds/proxy-delivery
curl           http://launchplane.shiny:8701/holds
curl -X DELETE http://launchplane.shiny:8701/holds
curl -X POST   http://launchplane.shiny:8701/kill
```

- A hold is `before.` or `after.`, then `host`, `logins`, `proxy`, `record` or `lanes`, then the name of the method. `logins` are the questions put to a database server, so `fail.logins.read_logins` is a server that cannot tell its logins. `?only=<text>` holds only a call whose arguments have that text.
- `fail.` in place of `before.` makes the call fail as a host that cannot be reached, and `refuse.` as a host that answers no, for as long as the hold stands. `lose.` lets the call take effect once and, where it succeeded, gives its caller the error of a lost reply.
- `GET /holds` answers `held`, and `stopped_at`: the holds a call is stopped at now.
- `proxy-delivery` puts a file Traefik cannot read into the routes folder. Traefik then loads no change to any routes file and goes on serving the routes it has, and its API goes on answering. Letting go removes the file, and Traefik loads what is there. Measured on Traefik v3.7.18.
- `POST /kill` kills Launchplane as `kill -9` does, and systemd starts it again.
- The tests start the service `launchplane` again when they end, and they pass from a bed on the plain service with nothing done by hand. By hand, on the control container:

```
lp-control 'install -m 644 /opt/launchplane/tests/launchplane-held.service /etc/systemd/system/ && systemctl daemon-reload && systemctl stop launchplane && systemctl start launchplane-held'
lp-control 'systemctl stop launchplane-held && systemctl start launchplane'
```

  The first puts the bed into holding mode and the second puts it back. From a plain shell, `ssh root@launchplane.shiny` takes the place of `lp-control`. A deploy puts it back too. The older test-bed tests need the plain service: some of them stop and start `launchplane` by name.

A hold on a call inside the lock Launchplane keeps on the routes folder stops every reading of that folder, the status page with it. The switch is held at `proxy.switch`, outside that lock: `before.` it nothing is staged, and `after.` it the rename is done and its reply not yet had.

The same file has the eight held cases of the rule the move is built to. Each probes the lane and its neighbour for the whole of the case and asserts that every probe answered and that the builds came in the order expected. Renames are counted from the kernel's report of every staged routes file of the generation that left the gate's folder.

| Case | Held, and what must come of it |
| --- | --- |
| 1 | B's switch accepted and held before the lock, Launchplane killed, and recovery held after it has read the routes folder and before it has read the proxy; the rename lands and loads in between: `done` and `switched`, B alone, one rename. |
| 2 | The switch's own command on the host succeeds and only its reply is lost: `done` and `switched` on the same B, one rename. |
| 3 | Renamed, the proxy held on A, Launchplane killed four times: a cancel is told too late, the request stays `working`, and it is finished once the proxy loads. |
| 4 | As case 1, and recovery discards B before the stale switch runs: it renames nothing, and a fresh candidate of the same generation switches once. |
| 5 | A cancel wins during B's check, then the host cannot be read: `working`, and `cancelled` once it can be. |
| 6 | A failed check about to be saved, and a cancel wins: `cancelled`, no rename. |
| 7 | B's cancel handler held after it recorded the cancel, the worker abandons B, C's cancel wins, the handler resumes and both switches are sent again: both cancels stand, no rename. |
| 8 | A cancel wins and Docker refuses to remove B: `failed`, saying the cancel won and what is left, and a repeated cancel says the same. |

Case 3 holds the release after the rename and kills Launchplane. It does not kill the host command itself: `tests/test_bed_gate.py` does that, at the gate.

`tests/test_bed_gate.py` proves the gate alone on the real host, with no worker: it sends the gate's own commands to the proxy's container and counts the renames by the inode of the routes file. Its five cases are a command killed just after the rename, a reply lost after the rename, a switch held before the lock whose candidate is discarded meanwhile, a cancel and a switch each sent again after the cancel won, and two generations on one lane. They take 13 s. `tests/test_gate.py` runs the gate's command against a folder on the machine that tests.

`tests/test_interruptions.py` runs the same rows against the pretend host, whose proxy can hold a written route unloaded, and against the pretend record and PostgreSQL. It needs no test bed and takes seconds.
