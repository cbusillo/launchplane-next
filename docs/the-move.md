# The move

`POST /lanes/{product}/{lane}/move` with `{"build": "<image>"}` moves one lane to one build. The call is kept as a [request](requests.md) and answered `202` at once; with `?wait=<seconds>` it answers as below when the move ends. The lane answers at the web address `{lane}.{product}.test`. Product and lane are lowercase letters, digits and hyphens.

A move of a product that declares an update, to another build than the one serving, is [a release that updates](odoo.md#a-release-that-updates): it copies the lane's data behind the back soon page, updates the copy, and switches to it. What is below is every other move.

A build is a container image reference, and the instance must answer HTTP on port 80. A product with a database, a background service or a health path of its own says so in its description: see [a lane with a database](data-sets.md). For [a product of the Odoo kind](odoo.md) a build is given as `{"run": <number>}`, a publishing run of its own workflow, and runs as the image that run published, by digest. Each instance gets a new container name, `{product}-{lane}-` and 12 random hex digits. Which product, lane and build an instance is comes from its labels, never from its name.

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
| `committed` | `done` and `switched` only, once the instance serves and the rest is finished. Until then it stays `working`, with why in `error`, and is taken up again by itself, ahead of newer work on its lane, however often. A [give-up](requests.md#giving-up) does not end it. One thing ends it otherwise: a database server that refuses to remove a login no container holds. The request then ends `failed` with the server's error, and the release stays switched. |
| `abandoned` | `cancelled`, once the lane is put back. It stays `working` while the host cannot be read. It ends as `failed` only when Docker refused to remove a part of the release, its instance or its background service, and the part is still there, and `error` then says that the cancel won and what could not be removed. |
| `none` | `failed`, `unchanged` or `repaired`, and only after the gate has confirmed `none` and refused every switch of this attempt and earlier ones, also one still on its way. Before `failed` is written, the instance the release started is removed, through the gate and the guard, and a fresh reading shows it gone. A background service the move started for the instance that already serves is left: it is live work, and only a cancel takes it back. Where a server's logins could not be read then, `error` says that whether a login was left is not known. A call the host did not answer, a wait, a login list a finished move still needs, two routers for the web address, a guard that refused, or 50 steps is no failure: nothing is ended, and the request is taken up again after 5 s. |
| no answer | nothing. No ending is written. The request is taken up again after 5 s. |

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `switched` | 200 | The gate says the routes file was renamed for this request's generation, and the web address points at a running instance of this build. A request taken up again after a kill answers this too, though another attempt did the rename. |
| `unchanged` | 200 | It already did, and nothing was done. |
| `repaired` | 200 | It already did, and the move put something right on the lane. `did` lists what, such as an instance left by a move that failed, stopped and removed. A request that renamed the routes file never answers this. A request taken up more than once answers for all its attempts: `did` then lists what each changed, in the words of the request's events, and a request whose last attempt changed nothing still answers `repaired`. |
| `check failed` | 422 | Nothing switched. `error` has the check's error and the instance's last log lines. The instance is gone. |
| `not updated` | 422 | Only for [a release that updates](odoo.md#a-release-that-updates): nothing switched, and the lane serves from its own data again. |
| `not initialized` | 422 | Only for a product that declares [a first start](odoo.md#the-first-start): it failed, so no instance was started and nothing switched. |
| `background service failed` | 422 | Only for a product with a background service, and only where nothing was switched: see [a move with data](data-sets.md#a-move-with-data). Under a release that is switched, the request stays working. |

A move does not answer success while the lane's web address does not answer a request for `/` with success: it answers `503` with the sentence the status call has for it.

`did` lists every change the move made, whatever the outcome. What it did to an instance or a background service is listed command by command, each one Docker answered as done, in the words of its line: `make`, `start`, `stop` or `remove` and the container. They come from the request's saved lines, however many attempts it took. [A command found already as asked, refused or not answered is left out](requests.md#a-command-for-a-part). A switch an earlier attempt sent with no reply, and that the routes file then showed to be that attempt's, [is listed too](requests.md#a-change-of-the-route).

`not_answered` lists each command for an instance or a background service that may have been sent and that Docker did not answer, as a sentence: `remove site-testing-4f2a may have been sent, and Docker did not answer`. A login whose making or removing got no answer is listed the same way, ending `and the database server did not answer`. Neither is in `did`. So a move that answers `unchanged` with an empty `did` and an entry here did not find that nothing changed: a removal whose reply was lost may be what put the lane right. The card shows the same line as `may have been sent, no answer`.

A `503` means the move stopped where it was with nothing switched, the gate has confirmed that, and what the move made is gone. Its request has failed, and it is safe to ask again. A build Docker answers it does not have ends this way; a host that does not answer leaves the request working. A lane's requests are worked one at a time, and a newer one that asks for something else [cancels](requests.md#cancelling) a move whose routes file is not renamed yet: the instance it started is removed and the web address never moves.

**The check.** The proxy requests `http://<instance>:80<health path>`, the path real traffic takes. The instance passes once it has answered with a 2xx for 3 s without a failure, and fails if it has not passed after 20 s, with the last error. An instance of the Odoo kind must then pass [the check of its kind](odoo.md#the-check), run once inside it. The candidate of [a release that updates](odoo.md#a-release-that-updates), and no other instance, must also be the process that was started: one that has passed all of it still fails if Docker has started it again since, and `error` says how often.

**The old instance** is retired once the switch is loaded: Docker sends the image's stop signal, waits up to 30 s, then kills and removes it. A product must finish its requests in flight and exit on its stop signal. A request that needs longer than 30 s after the switch is cut off.

## Which instance receives visitors

The proxy's loaded routes are the only mark. A route sends one web address to one instance by its container name. `GET /running` gives each instance `receives_visitors_for`, and a move answers `container_receiving_visitors`.

A route is written by renaming a new routes file for the lane into place, and Traefik loads it up to 2 s later. Every change of a routes file is made by [the gate](#the-gate): no other command writes or removes one. The first line of the file, which the proxy reads as a comment, says which installation the route is: the generation and attempt that put it in place. So a route put back to the same instance is not the file that was there before. A file with no such line, or with another comment there, is a route all the same.

A routes file that is otherwise not exactly the route Launchplane writes for that web address is that lane's fault and no other lane's: the lane shows it as wrong, and a move of the lane stops and says so. Every other lane is read as before.

A move that finds the file and the loaded route disagreeing before it has switched anything waits for the proxy, or stops with where each sends the web address and the proxy's log. A proxy that cannot be asked, or a loaded route Launchplane did not write, stops the call.

No action of any call stops or removes a container the proxy's loaded routes send visitors to. That is checked on its own at the moment of the action, from the loaded routes and from no stamp or mark, and the request stays `working` and says so. A database server about to be stopped or deleted is checked the same way. While the proxy has two routers for one web address, of any lane, where those visitors go is not known, and nothing is stopped or removed. A retire has removed the route by then, and a move retires the old instance only once the proxy has loaded the switch.

### Two routers for one web address

The proxy must have one router for a web address. When it has loaded two, from a second file in the routes folder, that is a conflict, also when both send the web address to the same instance, and in whatever order the proxy lists them. Neither is taken for where visitors go.

- The lane's `condition` is `conflicting`, with one finding that names each router, the container it sends to and its status. The web address says `conflicting` with the same sentence, and has no `sends_to_container`.
- No call changes anything on that lane. A move stays `working` with the same sentence in `error`, starts, switches and removes nothing, and is taken up again every 5 s. Once the proxy has one router it goes on by itself. A retire and the maintenance calls do the same.
- Nothing puts it right by itself: remove the file Launchplane did not write.

An instance that exists and does not receive visitors is left over from a move cut short. The next move of the lane, to any build, retires it. The web address points at exactly one instance the whole time.

## The gate

The gate is the one place that answers, for a generation, `none`, `committed` or `abandoned`, and the answer is for good. Everything that settles a release is one command, run in the proxy's container while it holds a lock on the folder the routes folder is in. What it decides is kept in the folder `gate` beside the routes folder, as files named for the web address and the generation:

- **The switch proves itself.** Under the lock the staged file is finished and made durable, then the `armed` mark, and then the routes file is put in place by renaming the staged file. While a generation is armed, only that rename takes its staged file away. Armed with the staged file there is `none`. Armed with the staged file gone is `committed` to that candidate. Nothing is written after the rename, and no reply has to arrive: a command killed just after the rename, and one whose reply is lost, are answered `committed` when asked again.
- **A cancel** makes the generation `abandoned` if it has no verdict, then takes its arming away, then removes every staged file of the generation and looks that none is left. Sent again it does the same, also where the arming is already gone.
- **A discard** names the generation, the attempt and the exact candidate. In the one command it either learns that the candidate was renamed to, and the answer is `committed` to it, or it takes away the permission to switch to it, and then the candidate may be removed.
- **A fence** confirms `none` and refuses every switch and every edit of an attempt up to a number, also one still on its way.
- **An edit** is every change of a route that is no release: into [maintenance](maintenance.md), out of it, and the removal by a [retire](retiring-a-lane.md). It names the generation and attempt of its request and the routes file exactly as that request read it. Under the lock it is made only if no fence has reached that attempt and the file is still exactly that one. The new file is staged in the gate's folder, apart from every candidate of a release, and renamed into place; a removal removes the file. It answers `done`, `already` when the file is the one wanted, or `lost` when it changed nothing. On `lost` the request reads its lane again and decides again. An edit gives its generation no verdict and takes nothing from a release.
- **Asking** for a generation's verdict changes nothing on the host, also where nothing was ever kept.
- **A route that is not a file,** such as a folder of that name, is refused before anything is staged: the command fails, and the generation keeps no verdict.

Every command but an edit answers with the verdict as it then holds. Whatever takes permission away keeps the revocation first, then removes the arming, then the staged file, and a command cut short and sent again goes in the same order. A command sent again returns the first verdict and stages nothing. A generation with a verdict is never renamed for again, a discarded candidate is never armed again, and another candidate is armed only by a later attempt.

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

## Known and not fixed

- **A slow reader can lose the end of a large download when the old instance stops.** Measured: a 24 MiB download read at 1.8 MB/s across a move lost its last 2.4 to 2.9 MB in 4 of 4 runs. Comes back if a real product kind shows it.
- **Asking for the old build while its instance is still stopping answers `check failed` for a good build.** Confirmed once, after a kill during retirement. Asking again works, and the web address never moves.
- **Container names are 48 random bits and are not checked against names a route already points at.** A clash with a route to an instance that no longer exists would send that web address's visitors to the new instance before its check. Comes back if names get shorter.
- **A move does not repair a route the proxy reports as disabled or with a warning.** Retiring the lane and moving it again rewrites the route.
- **What a request did across attempts is sorted by how each told line begins.** A request taken up more than once lists as changes the lines of its events that do not begin as a wait, a check, a watch, a log read or a mark at the gate. A change of the route, a command for an instance or a background service, and a login made or removed are the exceptions: each has its own line, which says whether the request made it.
- **Two routers on another lane's web address can stop a retire after its route is removed.** The retire removes its lane's route first, and the guard then refuses to remove any container while any web address has two routers. That lane has lost its visitors, and its retire stays `working` until the other lane has one router.
- **A second router that appears between a move's last reading and its rename does not stop that rename.** The move finds the conflict at its next reading and holds from there, with the routes file already renamed.
- **A router Launchplane cannot make sense of hides a conflict.** A second router for a web address whose target is on another port, or is not an instance, makes the whole reading of the proxy fail: `/running` answers `503` with why, and does not name the two routers.
- **Only a move's cancel is decided at the gate.** A retire and beginning or ending maintenance change the route through the gate, and are cancelled as before, by a mark in the record their worker reads between actions.
- **The gate does not know whose turn a lane is.** It refuses a switch after a cancel of its generation, after its candidate was discarded, and after its attempt was fenced. A worker that lost its lane's turn, and whose attempt nobody has fenced, can still switch, or edit a route that is still exactly as it read it.
- **A routes file changed by hand between a move's planning and its switch is overwritten.** The switch does not compare the route the move read, as an edit does.
- **A change of only the newlines at the end of a routes file is not seen by an edit.** The gate compares the file without them.
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
- On November 20, with 3,174 files in the folder `gate` and none removed: a reading of the routes transferred 8 KiB where it had transferred 3.1 MB, and `/running` answered in 0.37 to 0.49 s where it had taken 0.78 s. Every lane answered after the routes files were moved into `routes`. Ten releases of one lane took the folder from 3,175 to 3,186 files. A switch and a cancel sent twice more for the first of those releases, and for a release cancelled after them, each got the first answer again, `committed` to the same instance and `abandoned`, and changed no file.
- The Docker SDK over the certificate connection gave everything the move needed. Nothing was missing that a container manager such as Dokploy would have given.

## On the test bed

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_move.py -v
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_interruptions.py -v
uv run pytest tests/test_interruptions.py -v
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_host_answers.py -v
```

`LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_odoo.py -v` runs TK's real build as the lane `testing` of `test-bed-tk`: see [the TK watch](#the-tk-watch). Its last test, selected with `-k first_start_from_descriptions -s`, is [the first-start watch](#the-first-start-watch). `tests/test_bed_copy.py` copies that lane's data, whole and killed between its volumes: see [the copy watch](#the-copy-watch).

Which bed the tests run against is [chosen by settings](what-is-running.md#choosing-the-test-bed), and these lines name the first one. Builds A, B and C of `tests/test_bed_move.py` are `nginx:1.30.6-alpine`, `nginx:1.31.8-alpine` and `hashicorp/http-echo:1.0`. C listens on 5678, not 80, so it fails its check. The kill tests run `systemctl kill --signal=KILL launchplane` on the control container, and systemd starts it again. One lets Launchplane take the move up again by itself. The others end the request in the record while Launchplane is down, to leave the lane as a move that failed halfway does.

`tests/test_bed_host_answers.py` runs under the same harness as the interruptions below, with the product `test-bed-answers`. It refuses the reading of a database server's logins after a repaired background service has passed its watch, and reads that service's container and the rows it writes from the scratch container while the request stays `working`. It puts a database server on another volume in a data set's place, asks for a start and then a retire. And it puts a second routes file into the proxy's folder, once with a router listed before the lane's own and once after. It begins maintenance and fails every reading of the lane from the moment the back soon page is up, with `after.proxy.edit` and `fail.host.read?only=test-bed-answers`: the request stays `working` with a part still running, a visitor from outside gets the back soon page throughout, and once the reading answers the request ends `done` with every part stopped.

### Holding a move and interrupting it

`tests/test_bed_interruptions.py` holds the real Launchplane at a named point of a move from A to B, interrupts it there, and reads what a visitor gets from outside the host for the whole of each case. Any error, reset or build that was not expected fails the case, and so does any change to a second lane beside it. The interruptions are a cancel by the call, a kill, a kill and then a cancel before any worker recovers, and a cancel and then a kill before the cancel is answered. One pause point of each row is run with all four, and every other pause point with one, 50 cases. One more holds a move before its switch while a second and then a third request arrive for its lane: the first is finished as asked, the third runs next, and the second ends as `replaced` with no attempt and no visitor ever answered by its build. One holds a move before its switch and kills Launchplane four times: the request is still `working` after the fourth, and is finished as asked when the hold is let go. Two more hold a rename that Docker has accepted inside the gate, by holding the gate's lock from outside, and kill Launchplane there. The whole table, every pause point with every interruption, runs against the pretend host.

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

One more case is a retire's. Its removal of the route is accepted by Docker and frozen before it has the gate's lock, and Launchplane is killed. A cancel is asked, recovery ends the retire as `cancelled` and fences it, and the lane is put in maintenance. The frozen command is then let go: the routes file keeps its bytes and its inode, the instance stays, and every probe from outside is answered by the back soon page.

Another is ending maintenance's, on the lane with a database and a background service: [the maintenance watch](#the-maintenance-watch) by hand. The end is held before its change of the route, where the request's line reads `about to` and a visitor still gets the back soon page. It is then held after the change has taken effect on the host and before the worker has the reply, and Launchplane is killed there. The same request ends `done` and `ended` in a second attempt, its line with no answer and read back; the instance serves, the lane beside it answers every probe, and an end asked after it answers `not in maintenance`. Case 1 also asserts that its switch, which landed after recovery's first reading, is read back and is in the move's `did`.

The last is a move's, on the same lane: [the start watch](#the-start-watch) by hand. The move is held before it starts the new background service, then held again once the line of that start is in the record and before the command is sent, and Launchplane is killed there. The same request ends `done` and `switched` in a second attempt. Its first line of the start has no answer and stays so, its second is done, `did` has that start once, and the lane beside it answers every probe.

One more move, on the same lane, is [the login watch](#the-login-watch) by hand. The move is held once the login of its new instance is made and before the instance is, and Launchplane is killed there. The same request ends `done` and `switched` in a second attempt. It has four lines for logins, each answered as done and each in `did`: the first attempt's making, then the second attempt's removal of that login, which no container ever held, its making of the login of the instance it does start, and after the switch its removal of the old instance's login.

`tests/test_bed_gate.py` proves the gate alone on the real host, with no worker: it sends the gate's own commands to the proxy's container and counts the renames by the inode of the routes file. Its five cases are a command killed just after the rename, a reply lost after the rename, a switch held before the lock whose candidate is discarded meanwhile, a cancel and a switch each sent again after the cancel won, and two generations on one lane. They take 13 s. `tests/test_gate.py` runs the gate's command against a folder on the machine that tests.

`tests/test_interruptions.py` runs the same rows against the pretend host, whose proxy can hold a written route unloaded, and against the pretend record and PostgreSQL. It needs no test bed and takes seconds.

### The held watches

The maintenance, start and login watches each begin with the first block here and end with the second. Every block pastes into zsh as it stands. The lane is the one `tests/test_bed_interruptions.py` uses, whose images and description are on the bed once that file has run. The page is `http://launchplane.shiny:8700/`.

The setup starts Launchplane with its calls able to be held, and brings up the lane `held` and the lane `untouched` beside it on build a:

```
L=http://launchplane.shiny:8700; H=http://launchplane.shiny:8701; J='Content-Type: application/json'
P=/lanes/test-bed-gate-ledger; V=http://docker-lp-scratch.shiny/
ssh root@launchplane.shiny 'install -m 644 /opt/launchplane/tests/launchplane-held.service /etc/systemd/system/ && systemctl daemon-reload && systemctl stop launchplane && systemctl start launchplane-held'
until curl -sf "$H/holds" >/dev/null && curl -sf "$L/requests" >/dev/null; do sleep 1; done
curl -s -X POST "$L$P/held/move?wait=110" -H "$J" -d '{"build":"launchplane-test-ledger:a"}'
curl -s -X POST "$L$P/untouched/move?wait=110" -H "$J" -d '{"build":"launchplane-test-ledger:a"}'
```

Putting the bed back retires each lane with its data set and starts the plain service:

```
for lane in held untouched; do D=$(curl -s "$L$P/$lane/retire?wait=110" -H "$J" -d '{}' | python3 -c 'import sys, json; print(json.load(sys.stdin)["kept_data_sets"][0])'); curl -s -X POST "$L$P/$lane/retire?wait=110" -H "$J" -d "{\"delete_data_set\": \"$D\"}"; done
ssh root@launchplane.shiny 'systemctl stop launchplane-held && systemctl start launchplane'
```

### The maintenance watch

Ending maintenance, killed after its change of the route is on the host and before Launchplane has the reply. Paste [the setup](#the-held-watches) first, then:

```
curl -s -X POST "$L$P/held/maintenance/begin?wait=110"
curl -s -X PUT "$H/holds/before.proxy.edit"
R=$(curl -s -X POST "$L$P/held/maintenance/end" | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])'); echo "request $R"
until curl -s "$H/holds" | grep -q '"stopped_at": \["before.proxy.edit"\]'; do sleep 1; done
```

1. **Held before the edit.** The block ends once the end is stopped at its hold. On the page, the card of `test-bed-gate-ledger · held` has the request as `working`, and its note ends `about to send held.test-bed-gate-ledger.test to` and the instance's name. A visitor still gets the back soon page, `503`, and the lane beside it answers `pretend product a`:

```
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: held.test-bed-gate-ledger.test' "$V"
curl -s -H 'Host: untouched.test-bed-gate-ledger.test' "$V"
```

2. **Held after the edit has taken effect.** The second hold is set before the first is let go, and the block ends once the end is stopped at it:

```
curl -s -X PUT "$H/holds/after.proxy.edit"; curl -s -X DELETE "$H/holds/before.proxy.edit"
until curl -s "$H/holds" | grep -q '"stopped_at": \["after.proxy.edit"\]'; do sleep 1; done
ssh root@docker-lp-scratch.shiny 'docker exec launchplane-proxy head -1 /etc/traefik/launchplane/routes/held.test-bed-gate-ledger.test.yml'
curl -s "$L/requests/$R"
```

   The second `ssh` is to the scratch container. The routes file's first line is the request's `generation`, as the last command shows it, and the number of the attempt that sent the edit. The page still says `about to`: Launchplane has not had the reply. The proxy loads the file within a few seconds, and a visitor then gets `pretend product a`.

3. **Killed, and let go.**

```
curl -s -X POST "$H/kill"; until curl -sf -X DELETE "$H/holds" >/dev/null; do sleep 1; done
sleep 10; curl -s "$L/requests/$R"
curl -s -H 'Host: held.test-bed-gate-ledger.test' "$V"
```

   The first command answers the request as `done`, with `outcome` `ended` and attempts `interrupted` then `done`; the second answers `pretend product a`. Look at the page now: the request's row on the card is `done` and stays for 10 minutes. Its note says it ended as done, and then its lines, the last of them `send held.test-bed-gate-ledger.test to` the instance, `: may have been sent, no answer, then read as made by this request`.

4. **A new end, once the page has been read.** It answers `not in maintenance`, and its row takes the place of the first on the card, with no lines:

```
curl -s -X POST "$L$P/held/maintenance/end?wait=60"
```

5. **Put the bed back,** with [the block that does](#the-held-watches).

### The start watch

A move, killed once the line for the start of its new background service is written and before the command is sent. Paste [the setup](#the-held-watches) first, then:

```
curl -s -X PUT "$H/holds/before.host.start_background_service"
R=$(curl -s -X POST "$L$P/held/move" -H "$J" -d '{"build":"launchplane-test-ledger:b"}' | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])'); echo "request $R"
until curl -s "$H/holds" | grep -q '"stopped_at": \["before.host.start_background_service"\]'; do sleep 1; done
```

1. **Held before the new background service is made.** The block ends once the move is stopped at its hold. It has switched: a visitor gets `pretend product b`. On the page, the card of `test-bed-gate-ledger · held` has the request as `working`. Its note has one line for each command so far, each ending `: done`: `make` and `start` of the new instance, and, after the switch, `stop` and `remove` of the old background service.

```
curl -s -H 'Host: held.test-bed-gate-ledger.test' "$V"
```

2. **Held with the line of the start written, and nothing sent.** The second hold stops the record once it has written the line of a start. It is set before the first is let go, and the block ends once the move is stopped at it:

```
curl -s -X PUT "$H/holds/after.record.about_to?only=verb=%27start%27"; curl -s -X DELETE "$H/holds/before.host.start_background_service"
until curl -s "$H/holds" | grep -q '"stopped_at": \["after.record.about_to"\]'; do sleep 1; done
ssh root@docker-lp-scratch.shiny 'docker ps --all --filter label=launchplane.lane=held --filter label=launchplane.part=background-service --format "{{.Names}} {{.Status}}"'
```

   The `ssh` is to the scratch container. It shows the new background service as `Created`: Docker made it and nothing started it. The page's note now ends with its `make`, `: done`, and then `about to start` and its name.

3. **Killed, and let go.**

```
curl -s -X POST "$H/kill"; until curl -sf -X DELETE "$H/holds" >/dev/null; do sleep 1; done
until curl -s "$L/requests/$R" | grep -q '"stands": "done"'; do sleep 1; done
curl -s "$L/requests/$R" | python3 -c 'import sys, json; r = json.load(sys.stdin); print([a["ended"] for a in r["attempts"]], r["answer"]["outcome"]); print(*r["answer"]["did"], sep="\n")'
curl -s -H 'Host: held.test-bed-gate-ledger.test' "$V"; curl -s -H 'Host: untouched.test-bed-gate-ledger.test' "$V"
```

   The third command prints the attempts, `interrupted` then `done`, the outcome `switched`, and the request's account. In it `start` and the background service's name is there once. The last answers `pretend product b` and `pretend product a`. Look at the page now: the request's row is `done` and stays for 10 minutes, and among its lines are, in this order, the start as `: may have been sent, no answer`; a `stop` of the same container, `: found already as asked`, and its `remove`, `: done`, which is the second attempt clearing away the container that was made and never started; then its `make` and its `start` again, each `: done`. After them `retire` and the old instance's name, `, refused`, is the gate answering that an earlier release was switched to that instance, so it is not removed as something a failed move left. It is removed next as the instance this release replaced, visitors being on the new one by then: its `stop` and `remove` follow, each `: done`.

4. **Put the bed back,** with [the block that does](#the-held-watches).

### The TK watch

TK's real Odoo 20 build as a lane, from a build Launchplane verified, on a data set Launchplane initializes itself. Every block pastes into zsh as it stands, from this repository. The first block pulls two of TK's builds on the scratch container, starts [the recorded git host](odoo.md#known-and-not-fixed) on the control container and describes the product `watch-tk` with [a first start](odoo.md#the-first-start). Open `http://launchplane.shiny:8700/` beside it.

```
L=http://launchplane.shiny:8700; J='Content-Type: application/json'; V=http://docker-lp-scratch.shiny/; P=/lanes/watch-tk/testing
export LAUNCHPLANE_URL=$L LAUNCHPLANE_CONTROL_SHELL='ssh root@launchplane.shiny' LAUNCHPLANE_SCRATCH_SHELL='ssh root@docker-lp-scratch.shiny'
said() { python3 -c 'import sys, json; a = json.load(sys.stdin); e = (a.get("error") or "").splitlines(); print(a.get("outcome"), a.get("build", ""), *a.get("did", []), *e[:1], *e[-3:-1], sep="\n")'; }
uv run python -m tests.bed_odoo set-up-first-start watch-tk
curl -s -X POST "$L$P/move" -H "$J" -d '{"build":"ghcr.io/cbusillo/odoo-tenant-tk:latest"}'
```

1. **A build given as text is refused.** The last line answers that a move of `watch-tk` names its build as `{"run": <number>, of a run of .github/workflows/build.yml in cbusillo/odoo-tenant-tk}`. **The page** has no card for `watch-tk`.

```
curl -s -X POST "$L$P/move?wait=110" -H "$J" -d '{"run":45278647986}' | said
```

2. **A new lane serves from its first move, with nothing done by hand.** After about 55 s: `switched`, the build as `ghcr.io/cbusillo/odoo-tenant-tk@sha256:0e2ad09f…`, and four lines that each name a part: `made the network`, `the database volume`, `the files volume` and `the database server of the data set`. Then the `make`, `start` and `remove` of a container whose name ends `-first-start`, with its login `i_start`, and the `make` and `start` of the instance with its own login, `wrote the route` and `switch testing.watch-tk.test to` it.

   **The card** `watch-tk · testing`, while it works, reads `work under way` and has a row `First start`, `running`, for about 35 s, and no instance. **Afterwards** it reads `good`, and its web address `answering`. The request is `done`: `It ended as done.` and its lines, among them `run the first start on the data set` and its name `in` that container, then `start the instance`, the check and the switch. There is no row `Found` and no row `First start`. `Instance 1` shows the build by its digest, `running` and `active`, and the data set's server is `running`.

```
uv run python -m tests.bed_odoo sign-in watch-tk
curl -s -H 'Host: testing.watch-tk.test' "$V" | grep -o '<title>[^<]*'
ssh root@docker-lp-scratch.shiny 'docker ps --no-trunc --filter label=launchplane.product=watch-tk --format "{{.Names}} {{.Image}} {{.Mounts}}"'
```

3. **The testing login signs in, and Odoo's default one does not.** The first command reads the lane's password on the control container, shows it nowhere, and prints two lines: `signed in as lp-tester, attachment: 5b8e2378…`, the SHA-256 of the declared attachment's bytes read through that session at the web address, and `admin with Odoo's default password: refused`. The site answers `<title>Tonia Kovach | Coastal Towne Realty`. The last command shows the instance on `ghcr.io/cbusillo/odoo-tenant-tk@sha256:0e2ad09f…` with the volume `launchplane-data-…-files`, and the database server with `…-database`. Nothing named `-first-start` is left.

```
curl -s -X POST "$L$P/move?wait=110" -H "$J" -d '{"run":45278647986}' | said
```

4. **The same build again changes nothing.** `unchanged`, and no line: no first start is run on a lane that serves. **The card** is as at point 2, with this request `done` and the note `It ended as done.` alone. The first command of point 3 prints the same two lines.

```
F=/volumes/data/filestore/app/34/34d5bcdaa3ca7286418fae675e3339c20fe1accc
W=$(ssh root@docker-lp-scratch.shiny 'docker ps --quiet --filter label=launchplane.product=watch-tk --filter label=launchplane.part=web')
ssh root@docker-lp-scratch.shiny "docker exec $W mv $F /volumes/data/held"
curl -s -X POST "$L$P/move?wait=110" -H "$J" -d '{"run":45291692474}' | said
curl -s -H 'Host: testing.watch-tk.test' "$V" | grep -o '<title>[^<]*'
```

5. **With the attachment's file gone, the next build fails the check and the lane keeps serving.** The move to TK's next build answers `check failed`: the candidate `answers the proxy, and the check of its kind, run in it, failed`, since the attachment `must be there once with the SHA-256 5b8e2378…` and has `e3b0c442…`, the SHA-256 of no bytes. The two lines after it are from the candidate's log. The site still answers its title, from the first build.

   **The card** still reads `good` and `answering`: the lane serves. The request is `failed`, and its note is that one sentence of the check and then its lines, with `check … at /web/login, refused` among them and no line of a log: the candidate's log speaks of no error. There is no row `Found`, and `Instance 1` is still the first build's, `running` and `active`.

```
ssh root@docker-lp-scratch.shiny "docker exec $W mv /volumes/data/held $F"
curl -s -X POST "$L$P/move?wait=110" -H "$J" -d '{"run":45291692474}' | said
```

6. **With the file back, the same move switches.** `switched`, to `…@sha256:f0b8a27b…`, and after `wrote the route` the `stop` and `remove` of the first build's instance and of its login.

   **The card** reads `good`. The request is `done`, and its lines run from the `make` of the new login through the `switch` to the `stop` and `remove` of the first instance. Among them `retire` and the first instance's name, `, refused`, is the gate answering that an earlier release was switched to it, so it is removed as the instance this release replaced and not as something a failed move left. `Instance 1` is the second build's digest, `running` and `active`.

7. **Put the bed back.** The tear-down retires the lane with its data set, and only once that has ended takes Launchplane off the recorded git host and removes the description and the lane's password. If the retire did not end, it stops and says so, and everything is left as it is. **The card** afterwards has one row, the retire, `done`, whose lines end `delete the data set` and its name; it leaves the page 10 minutes later:

```
uv run python -m tests.bed_odoo tear-down watch-tk
```

### The first-start watch

A website and an Odoo product, each from its description and nothing else on the host, brought up by one move each. It is the last test of `tests/test_bed_odoo.py`, which asks every move through the API as any caller does, prints each point as it reaches it, waits 8 s after each so the page can be read, and puts the bed back. It takes about two and a half minutes.

```
open http://launchplane.shiny:8700/
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest -q -s tests/test_bed_odoo.py -k first_start_from_descriptions
```

1. **Described, and nothing on the host.** The two web addresses, `testing.first-site.test` and `testing.first-odoo.test`, each answering `404`, and for each product no instance, no data set and no container visitors are sent to. **The page** has at most a card with one row for each, the retire that cleared the lane.
2. **The website's first move.** `switched`, and the address answers as `nginx/1.30.6`. **The card** `first-site · testing` reads `good` and `answering`.
3. **Odoo's first move, on its first request.** `switched` after about 53 s, with one line `run the first start on the data set`. `signed in as: lp-tester`, the SHA-256 of the declared attachment read through that session, and `default admin refused: True`. **The card** `first-odoo · testing` shows a row `First start` while it works, then reads `good` and `answering`.
4. **Asked again.** The test first writes one invented contact through the session. Then the website answers `unchanged`; Odoo's move sent again with its `Idempotency-Key` is answered with the first request, and a new request for the same build answers `unchanged` with `did: []`. The same instance, the same data set, and the contact's id under `row`.
5. **Retired with its data kept, and moved again.** `switched`, on the same data set, after a first start that found the data and changed nothing: the same `row`, `password is the same: True`, and the default administrator still refused.
6. **A first start that fails.** For the lane of a third description, `first-odoo-fails`, the test runs Launchplane [able to be held](#holding-a-move-and-interrupting-it), holds it before the first start, and has the empty database refuse every new table. The request is `failed`, `not initialized`, and `error` ends in Odoo's own `psycopg2.errors.RaiseException: the first-start watch lets no table be made in this database`. `visitor: 404`, no instance, and nobody is sent anywhere. **The card** `first-odoo-fails · testing` reads `move failed`, its web address `not answering`, and its row `Found` says `No instance receives visitors`, with the note that the first start failed, so no instance was started.

### The copy watch

[A copy of a lane's data](copying.md), whole and then killed between its two volumes. Every block pastes into zsh as it stands, from this repository. The first block brings up TK's build as the lane `testing` of `watch-copy`, described with no first start and [seeded by hand](odoo.md#the-data-set), and writes one invented lead with an attachment through Odoo at the lane's web address. It takes about three minutes. Open `http://launchplane.shiny:8700/` beside it.

```
L=http://launchplane.shiny:8700; H=http://launchplane.shiny:8701; J='Content-Type: application/json'; V=http://docker-lp-scratch.shiny/; P=/lanes/watch-copy/testing
export LAUNCHPLANE_URL=$L LAUNCHPLANE_CONTROL_SHELL='ssh root@launchplane.shiny' LAUNCHPLANE_SCRATCH_SHELL='ssh root@docker-lp-scratch.shiny'
site() { curl -s -o /dev/null -w '%{http_code}' -H 'Host: testing.watch-copy.test' "$V"; }
stands() { curl -s "$L/requests/$1" | python3 -c 'import sys, json; r = json.load(sys.stdin); print(r["stands"], (r["answer"] or {}).get("outcome", ""), (r["answer"] or {}).get("copied_to", ""))'; }
uv run python -m tests.bed_odoo set-up watch-copy >/dev/null
curl -s -X POST "$L$P/move?wait=110" -H "$J" -d '{"run":45278647986}' >/dev/null
uv run python -m tests.bed_odoo seed watch-copy >/dev/null
curl -s -X POST "$L$P/move?wait=110" -H "$J" -d '{"run":45278647986}' >/dev/null
uv run python -m tests.bed_odoo lead watch-copy
```

1. **The lane serves, with the lead.** Six lines: the `instance` the web address is sent to, its `build`, `ghcr.io/cbusillo/odoo-tenant-tk@sha256:0e2ad09f…`, its two `volumes`, `launchplane-data-…-database` and `…-files`, then what Odoo answers there: `lead: lead`, `attachment: 2663df39…`, the SHA-256 of the attachment's bytes, and `written:` with nothing after it.

   **The card** `watch-copy · testing` reads `good`, its web address `answering`. `Instance 1` is `running` and `active`, and the data set's server is `running`.

```
R=$(curl -s -X POST "$L$P/copy" | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')
for each in $(seq 16); do echo "$(site) $(stands $R)"; sleep 1; done
C=$(stands $R | cut -d' ' -f3); uv run python -m tests.bed_odoo show-copy watch-copy testing $C
```

2. **A whole copy.** The loop prints what a visitor gets beside how the request stands, once a second: `200 working`, then `503 working` for about 11 s, the back soon page, then `200 done copied` and the copy's name. The last command reads the copy from its own two volumes and nothing else, in a container that reaches no network: `data_set` is the copy, its `volumes` are two that step 1 did not show, and `lead`, `attachment` and `written` are as in step 1.

   **The card,** while the loop prints `503`: the pills `in maintenance` and `work under way`, the web address `shows the back soon page`, and the request `working` with what it has done so far, ending in a `stop` or in `copy the volume` and the two volumes' names. A row `Found` says a part still running in a lane in maintenance, `under way`, and the copy's row says `is a copy, not whole, of` the lane's data set. **Afterwards** it reads `good` and `answering` again. The request is `done`: `It ended as done.` and its lines, from `make the database volume of the data set` through the two `copy the volume … to …` to `send testing.watch-copy.test to` the instance, `: done`. There is no `switch` among them. A new row says the copy `is a whole copy of` the lane's data set, `kept offline: no instance is started on it`, and under it `An old copy: the next release that switches deletes it.` The serving data set's row has `Kept: the lane serves from it.` after its server's name.

```
uv run python -m tests.bed_odoo write watch-copy testing 'after the copy'
```

3. **The same instance takes a write.** The six lines of step 1 again, with the same `instance`, `build` and `volumes`, and `written: Written after the copy`: a lead made through Odoo just now and read back.

```
ssh root@launchplane.shiny 'install -m 644 /opt/launchplane/tests/launchplane-held.service /etc/systemd/system/ && systemctl daemon-reload && systemctl stop launchplane && systemctl start launchplane-held'
until curl -sf "$H/holds" >/dev/null && curl -sf "$L/requests" >/dev/null; do sleep 1; done
curl -s -X PUT "$H/holds/before.host.copy_volume?only=-files" >/dev/null
R=$(curl -s -X POST "$L$P/copy" | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])'); echo "request $R"
until curl -s "$H/holds" | grep -q '"stopped_at": \["before.host.copy_volume"\]'; do sleep 1; done
site; echo; ssh root@docker-lp-scratch.shiny 'docker ps --all --filter label=launchplane.product=watch-copy --format "{{.Names}}: {{.Status}}"'
```

4. **Held between the two volumes.** The first `ssh` puts the bed into [holding mode](#holding-a-move-and-interrupting-it). The block ends once the second copy is stopped at its hold, with the database volume copied and the files volume not. A visitor gets `503`. The second `ssh` is to the scratch container: the instance and the database server are `Exited`, the server with `(0)`, and there are three copiers, each `Exited (0)`: the two of step 2 and one `…-database-copier` of the new copy. It has no `…-files-copier`.

   **The card** has `in maintenance` and `work under way`. The request is `working`, and its note ends `copy the volume` and the names of the two database volumes.

```
curl -s -X PUT "$H/holds/after.proxy.edit?only=testing.watch-copy.test" >/dev/null
curl -s -X POST "$H/kill"; until curl -s "$H/holds" | grep -q '"stopped_at": \["after.proxy.edit"\]'; do sleep 1; done
sleep 3; site; echo; uv run python -m tests.bed_odoo write watch-copy testing 'after the kill'
```

5. **Killed, and the lane is brought back by itself.** Launchplane is killed as `kill -9` does, and systemd starts it again. Nothing lets the held copy go on: the request is taken up again, gives the copy up, and brings the lane back. The block ends once that attempt is stopped at a second hold, with its change of the route made on the host and its reply not yet had. A visitor gets `200`. The last command writes a lead through Odoo and prints the lane: the `instance`, `build` and `volumes` of step 1, `lead: lead`, the same `attachment`, and `written` ending `Written after the kill`.

   **The card** reads `work under way`, its web address `answering`. The request is `working`, `Taken up 2 times`, the last attempt ended by `Launchplane stopped, or lost its record, while it was at work on it.` Its lines go on from the copy of the database volume with `give the copy up: An earlier attempt began the copy` and its name, `and did not finish it`, then the `start` of the database server and of the instance, its `check`, and last `about to send testing.watch-copy.test to` the instance.

```
curl -s -X POST "$H/kill"; until curl -sf -X DELETE "$H/holds" >/dev/null; do sleep 1; done
until stands $R | grep -q failed; do sleep 1; done; stands $R
curl -s "$L/requests/$R" | python3 -c 'import sys, json; r = json.load(sys.stdin); print([a["ended"] for a in r["attempts"]]); print(r["error"])'
uv run python -m tests.bed_odoo show watch-copy
C=$(stands $R | cut -d' ' -f3); ssh root@docker-lp-scratch.shiny "docker run --rm --network none --volume launchplane-data-$C-files:/copy:ro busybox:1.37 ls -A /copy | wc -l; docker ps --all --quiet --filter volume=launchplane-data-$C-files | wc -l"
```

6. **Taken up a third time, the same request changes nothing.** Launchplane is killed again, with the lane already back, and the holds are let go. The request ends `failed`, `not copied`, with the name of what it left. Its attempts are `interrupted`, `interrupted` and `failed`, and its error is that a copy was begun by an attempt of this request that did not finish it and is never gone on with, that what was copied is kept with no instance started on it, and that asking again makes a new copy. The lane is as in step 5, the write `Written after the kill` with it. The last line counts the files in the files volume of the copy that was not finished, and the containers that have it mounted: `0` and `0`.

   **The card** reads `good` and `answering`. The request is `failed`, and its note is that error and its lines: the last is `send testing.watch-copy.test to` the instance, `: may have been sent, no answer, then read as made by this request`, and the third attempt added none. Two rows are copies: the one of step 2, `a whole copy`, and this one, `is a copy, not whole, of` the lane's data set, `kept offline: no instance is started on it`. Nothing is left to do by hand.

7. **Put the bed back.** The plain service is started, and the tear-down retires the lane with each copy and then its data set:

```
ssh root@launchplane.shiny 'systemctl stop launchplane-held && systemctl start launchplane'; until curl -sf "$L/requests" >/dev/null; do sleep 1; done
uv run python -m tests.bed_odoo tear-down watch-copy
```

### The update watch

[A release that updates](odoo.md#a-release-that-updates), on the lane `testing` of `watch-update`: three releases that fail and leave the original serving, each with a copy it gave up, then one that switches and [deletes those three copies](copying.md#old-copies-go). Then the same lane is made again from two builds made on the bed, where the new build's start reads a field only its own update adds: one release whose update fails, and one whose update is committed before the first web process of the new build is started. It is `tests/test_bed_tk_update.py`, which asks every move through the API as any caller does, makes its faults on the scratch container alone, and prints each observation as it happens. It takes eight to eleven minutes and puts the bed back. The three blocks paste into zsh as they stand.

```zsh
cd /Users/cbusillo/Developer/launchplane-next
export LAUNCHPLANE_URL=http://launchplane.shiny:8700
export LAUNCHPLANE_CONTROL_SHELL='ssh root@launchplane.shiny'
export LAUNCHPLANE_SCRATCH_SHELL='ssh root@docker-lp-scratch.shiny'
open "$LAUNCHPLANE_URL/"
uv run pytest -q -s tests/test_bed_tk_update.py
```

In a second zsh window, this block samples what a visitor gets at the website route. Stop it with Ctrl-C.

```zsh
while true; do
  curl -sS -o /dev/null -w 'website %{http_code}\n' -H 'Host: testing.watch-update.test' http://docker-lp-scratch.shiny/
  sleep 1
done
```

It prints `website 404`, and for a moment other codes, while the lane is being made: before point 1, and for about a minute between points 6 and 7. Which build and data set receive visitors is on the card and in each block the test prints.

In a third zsh window, this block prints the lane's data sets every two seconds: each `Data set` row of the card with the sentence under it, the data sets `GET /running` answers for the lane, and how many volumes of the lane the scratch container has. Stop it with Ctrl-C.

```zsh
while true; do
  curl -fsS http://launchplane.shiny:8700/running | python3 -c 'import json,sys
r=json.load(sys.stdin)
for card in r["cards"]:
    for row in card["rows"] if card["title"]=="watch-update · testing" else []:
        if row["name"]=="Data set": print("card:", "".join(piece["text"] for piece in row["value"])[:70], "|", row["note"])
print("api: ", sorted(each["data_set"] for each in r["data_sets"] if each["product"]=="watch-update"))'
  ssh root@docker-lp-scratch.shiny 'echo "scratch: $(docker volume ls -q --filter label=launchplane.product=watch-update | wc -l) volumes"'
  echo; sleep 2
done
```

The test prints one block for each point below as it reaches it, and the page shows the card `watch-update · testing` at the same moment. It first brings the lane up on TK's first build, seeded by hand with `python -m tests.bed_odoo seed`, writes one invented lead on a sales team with one attachment through Odoo at the web address, and then describes the product with `"update": ["tk_website"]` and with that attachment as the one [the check](odoo.md#the-check) reads. That takes about two minutes, with nothing printed. Where a point must be read between two of Launchplane's steps, the test runs Launchplane [able to be held](#holding-a-move-and-interrupting-it) and holds it there; points 5, 6, 8 and 10 run the plain service.

1. **Original.** `instance`, `build` and `volumes` are what `/running` has for the web address: `ghcr.io/cbusillo/odoo-tenant-tk@sha256:0e2ad09f…` on `launchplane-data-…-database` and `…-files`. `witness` is the record to follow, read through Odoo at the web address: `lead` and `team`, both IDs, `type: lead`, the `attachment` ID, `attached_to: crm.lead,` and the lead's ID, and `sha256: 2663df39…`, the hash of the attachment's bytes.

   **The card** reads `good`, its web address `answering`, and `Instance 1` is `running` and `active`. The second window prints `website 200`.

2. **Frozen and copied.** The move to run `45291692474` is answered `202`, and its request reads `working`. A visitor gets `503`, the back soon page. Launchplane is held where both volumes are copied and nothing is started on the copy. `the copy` is read from the copy's own two volumes alone, in a container with no network: two volumes that point 1 did not show, `lead: lead`, and the same `attachment` hash.

   **The card** has the pills `in maintenance` and `work under way`, its web address `shows the back soon page`, and `Instance 1` is `stopped`. The request is `working`, and its note is: `A copy of the lane's data is being made. It has no network of its own yet, so nothing runs on it. Visitors get the back soon page, and nothing is switched. Every line is in GET /requests/<id>.`

3. **Update fails.** With the copy's database server up, nothing of the new build started and the update not yet sent, the test adds a trigger to the copy's database alone that refuses any lead becoming an opportunity, and lets Launchplane go. TK's real module update runs, and its migration fails on that trigger. Launchplane is held once more, about to start the original's database server again, and a visitor still gets `503`.

   **The card, recovering:** the request is `working`, and its note names the step, `update the data of` the copy `in` a container whose name ends `-update`, that it `ended with code 255`, `The last error in its log: psycopg2.errors.RaiseException: the update watch lets no lead become an opportunity on this copy`, and then `Nothing is switched: Launchplane is bringing the original instance back on its own data.`

   **Restored:** the request is `failed`, `not updated`, and the gate's answer for it is `none`: it has no line that begins `switch`. `error` is the sentence above, `Nothing was switched, and the lane serves from its own data again. What was copied is kept offline, and asking again makes a new copy.`, and the last 20 lines the update wrote. The card reads `good` and `answering`, and its note is that error in one paragraph. The test then writes a lead through Odoo at the web address and prints the lane: the `instance`, `build` and `volumes` of point 1, `lead: lead`, the same `attachment`, and `written: Written after the update failed`.

4. **Check fails.** The same move again, on a fresh copy, held once the update has run, the candidate has been started, and before the check. The card's note is `The copy is updated, and the new build runs on it. It has not passed its check. Visitors get the back soon page, and nothing is switched.` First the witness is read by the candidate's own Odoo, on the updated copy that no visitor reaches: the same `lead`, `team`, `attachment` and `attached_to` as in point 1, `type: opportunity`, and the same `sha256`. Then the test overwrites that attachment's file in the copy's files volume, and asks the candidate for `/web/login` through the proxy's container: `200`. Let go, the check reads the attachment through Odoo and fails.

   **The card** reads `good` and `answering` again, with the original `running` and `active`. The request is `failed`, `check failed`, with no `switch`, and its note is the check's own sentence: the candidate `answers the proxy, and the check of its kind, run in it, failed: The attachment invented-lead.txt of the database app must be there once with the SHA-256 2663df39…. Read through Odoo from its file, it has:` another hash. At the web address the lead is still a `lead` with its bytes, the write of point 3 is there, and a new one, `Written after the check failed`, reads back.

5. **Killed during the update.** The same move a third time, on the plain service. As soon as the update's process shows on the scratch container, Launchplane is killed as `kill -9` does, and systemd starts it again. The request is taken up, finds its lane behind the back soon page with an update it knows nothing of, removes the container the update still runs in, and brings the original back. No candidate was started.

   **The card** reads `good` and `answering`. The request is `failed`, `not updated`, its attempts `interrupted` and `failed`, with no `switch`, and its note says why in one paragraph: `An earlier attempt of this request put the lane behind the back soon page and did not finish, so whether its update ran is not known and its copy is not used. Nothing was switched, and the lane serves from its own data again. What was copied is kept offline, and asking again makes a new copy.` The lead, its bytes and both earlier writes are at the web address, and `Written after the kill` reads back.

6. **Successful switch and retry.** The same move a fourth time, sent with an `Idempotency-Key` and left alone. The card goes from `in maintenance` and `work under way`, with the note of point 2 and then each note of point 9 in turn, to `good` and `answering` with `…@sha256:f0b8a27b…` `running` and `active`, and the second window goes from `website 503` to `website 200`. The request is `done`, `switched`: one line `update the data of`, one line `switch`, and the gate's answer `committed`. Its note: `Visitors are on the new build and the updated copy. Every line is in GET /requests/<id>.`

   `witness`, now read at the same web address, is the record of point 1 as an `opportunity`: the same `lead`, `team`, `attachment`, `attached_to` and `sha256`. `written` has the three writes of points 3 to 5. The first line of the block is [the measured time](odoo.md#measured) from the first back soon answer to the new instance's first answer. The test also holds that the scheduled job it made due before the move is still due and has not run, that the new instance has only the copy's files volume mounted and only networks with no way out, and that the data set it replaced still has both volumes, with its database server `exited` with code 0.

   **The older copies are gone.** Each of the three releases that failed left its copy with a stopped database server. Just before this move the third window prints four `card:` lines, each the image of a data set's server and then its container: the serving one with `Kept: the lane serves from it.`, and three with `An old copy: the next release that switches deletes it.` Under them `api:` has four names, and the scratch container `8 volumes`. While the move runs there are five and `10`, with no sentence while no instance receives visitors, and a copy that has no server yet reads `is a copy, not whole, of` the serving data set. Once it has switched, for a moment all five have a sentence, the fifth `Kept: the last release replaced it.` Then two `card:` lines are left, `Kept: the lane serves from it.` for the new data set and `Kept: the last release replaced it.` for the one of point 1, `api:` has those two names, and the scratch container `4 volumes`.

   The test prints the same under `6. The older copies are gone`: three lines `delete the data set` and a name, the card's two sentences, and the four volumes left, two for each data set kept. While Launchplane is killed in point 5 the third window prints an error once.

   Then it writes once more, sends the same move with the same key, and asks for the serving build again without a key: `the key answers request` the request that switched, and the new request is `unchanged` with `0 lines`. Neither the update nor the switch ran again, and the write is there. The card stays `good`.

**The fixture.** For the last four points the test retires the lane with its data and makes it again from two builds it makes on the scratch container, in `tests/bed_fixture.py`. Both are TK's later build with one module added, `lp_fixture`, and with a start that does what the shared Odoo tools do for a site: a website bootstrap that reads the `website` table, and the HTTP server only after it. The new build's `lp_fixture` adds the stored field `lp_greeting` to `website`, and its bootstrap reads that field. On data the new build has not updated, the bootstrap ends with PostgreSQL's `UndefinedColumn` and no HTTP server is started. The recorded git host answers for the two builds as runs `1` and `2`. The product is described with `"update": ["lp_fixture"]`, the modules `website` and `lp_fixture`, and `/` as the path its check asks. The new lane is initialized as [a new lane is](odoo.md#the-data-set): a first move that makes its data set, the old build's own `--init`, and the move that starts it.

7. **Original.** `website: 200`, on `launchplane-test-fixture/website-field@sha256:…`, the old build. `field in its database` is read by `psql` in the lane's database server: `no column lp_greeting`.

   **The card** reads `good`, its web address `answering`, and `Instance 1` is `running` and `active`.

8. **Failed update.** The test adds a trigger to the lane's database that refuses every `ALTER TABLE`, which the copy then has too, and asks for run `2` on the plain service, left alone. The second window goes `website 503` and back to `website 200`, and the first line of the block is [the measured time](odoo.md#measured) between the two. The card goes `in maintenance`, `work under way`, then names the update's error and `Nothing is switched: Launchplane is bringing the original instance back on its own data.`

   **Restored:** the move is answered `422`. The request is `failed`, `not updated`, with no `switch`, and `error` ends with the update's last lines: `psycopg2.errors.RaiseException: the update watch lets no table be altered in this database`. `started` has every line of the request that begins `start`: the container that ends `-update`, the original's database server, and the original instance. There is none for a candidate. `left of the release` is `[]`: the scratch container has no container of this release. The card reads `good` and `answering` with the request `failed`. `website: 200`, on the `instance`, `build` and `volumes` of point 7, and a contact the test then writes through Odoo at the web address reads back as `written`. The trigger is removed.

9. **Copy and update, commit, start and switch.** The same move again, held three times.

   **Copied, before the update.** `visitor: 503`. The card reads `in maintenance` and `work under way`, and the request's note is `The lane's data is copied. The copy is not updated, and nothing of the new build is started on it. Visitors get the back soon page, and nothing is switched.` `of the release` is `[]`.

   **While the update runs.** Let go, the test reads the card again and again until Launchplane is held about to remove the update's container, and prints how many cards it read and how many had the update running. Every one reads `in maintenance` and `work under way`, never `wrong`. While the update's container runs the card has a row `Update`, `running`, under the stopped `Instance 1`, and the note is `The lane's data is copied, and the new build is updating the copy. No web part of it is started. Visitors get the back soon page, and nothing is switched.`

   **The update has ended, and its container is not yet removed.** The `Update` row is `stopped`, and the note is `The container of the update is on the host and does not run. How the update ended is not recorded yet, and no web part of the new build is started. Visitors get the back soon page, and nothing is switched.`

   **Updated, before the first web start.** Launchplane is held about to start the candidate. The card has no `Update` row and no second instance, and its note is `The update of the copy ended well. No web part of the new build is started on it yet. Visitors get the back soon page, and nothing is switched.` `field in the copy`, read by `psql` in the copy's own database server, over a connection that is not the update's: `column lp_greeting, character varying`. `of the release` is still `[]`: the update's container is gone and no web process of the new build has existed. `visitor: 503`.

   **Started once, before the check.** `first web process` is the candidate as Docker has it: its name, `running`, `restarts=0`, when it started, and its command `/odoo/odoo-bin`. Asked through the proxy's container, it answers `/` with success, and `answered through the proxy s container` is `200`. A visitor still gets `503`, and the note is that of point 4.

   **The route is written, and the proxy has not loaded it.** The test keeps the proxy from loading its routes and holds Launchplane after the gate's rename, before it has seen the proxy serve the route. The request has its one line `switch`, `visitor: 503`, and the note is `The route is written to the new build on the updated copy. The proxy is not yet seen to send visitors there, so they may still get the back soon page.` It says `The proxy sends visitors to the new build on the updated copy.` only once the request has, after the switch, the line of the proxy loading the route.

   **Switched.** The request is `done`, `switched`, with one `switch`. `still` is the same line as `first web process`, with the same start time and `restarts=0`: the container that answered first is the one that serves, never restarted or replaced. `website: 200`, the card reads `good` and `answering`, and the contact of point 8 is still there.

10. **Restarted, and a fresh database.** The test restarts the serving container with `docker restart`: its bootstrap runs again, on the updated database, and `website: 200` with the card `good`. Then it initializes the lane `fresh` of the same product with the new build alone, as point 7's lane was with the old one: `website: 200` at `fresh.watch-update.test`, the card `watch-update · fresh` reads `good` and `answering`, and that database has the column too.

Afterwards the test retires both lanes with each copy and its data set, removes the two builds, and takes Launchplane off the recorded git host.

### The login watch

A move on a lane with data, killed once the login of its new instance is made and before the instance is. Paste [the setup](#the-held-watches) first, then:

```
curl -s -X PUT "$H/holds/after.logins.make"
R=$(curl -s -X POST "$L$P/held/move" -H "$J" -d '{"build":"launchplane-test-ledger:b"}' | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])'); echo "request $R"
until curl -s "$H/holds" | grep -q '"stopped_at": \["after.logins.make"\]'; do sleep 1; done
```

1. **Held with the login made and no instance for it.** The block ends once the move is stopped at its hold. On the page, the card of `test-bed-gate-ledger · held` has the request as `working`, and its note is one line: `make the login i_` and 12 hex digits, ` on ` the database server, `: done`. The lane still has its one instance, and a visitor gets `pretend product a`. The second command asks the server: it has two logins, the old instance's and the new one, which no container holds.

```
curl -s -H 'Host: held.test-bed-gate-ledger.test' "$V"
ssh root@docker-lp-scratch.shiny 'docker exec $(docker ps --quiet --filter label=launchplane.lane=held --filter label=launchplane.part=database) psql -U postgres -d app -Atc "select rolname from pg_roles where rolname like \$\$i\\_%\$\$ order by 1"; docker ps --filter label=launchplane.lane=held --filter label=launchplane.part=web --format "{{.Names}}"'
```

2. **Killed, and let go.**

```
curl -s -X POST "$H/kill"; until curl -sf -X DELETE "$H/holds" >/dev/null; do sleep 1; done
until curl -s "$L/requests/$R" | grep -q '"stands": "done"'; do sleep 1; done
curl -s "$L/requests/$R" | python3 -c 'import sys, json; r = json.load(sys.stdin); print([a["ended"] for a in r["attempts"]], r["answer"]["outcome"]); print(*r["answer"]["did"], sep="\n")'
curl -s -H 'Host: held.test-bed-gate-ledger.test' "$V"; curl -s -H 'Host: untouched.test-bed-gate-ledger.test' "$V"
```

   The third command prints the attempts, `interrupted` then `done`, the outcome `switched`, and the request's account. Four of its entries are logins, in this order: `make the login` of step 1; `remove the login` of the same name, which is the second attempt clearing away the login no instance was made for; `make the login` of another name, for the instance it then makes and starts; and, after the switch and the removal of the old instance, `remove the login` of the old instance. The last command answers `pretend product b` and `pretend product a`. Look at the page now: the request's row is `done` and stays for 10 minutes, and its note has the same four lines in the same order, each ending `: done`, among the lines of its commands.

3. **What the server has now.** One login, that of the instance that serves:

```
ssh root@docker-lp-scratch.shiny 'docker exec $(docker ps --quiet --filter label=launchplane.lane=held --filter label=launchplane.part=database) psql -U postgres -d app -Atc "select rolname from pg_roles where rolname like \$\$i\\_%\$\$ order by 1"; docker ps --filter label=launchplane.lane=held --filter label=launchplane.part=web --format "{{.Names}}"'
```

4. **Put the bed back,** with [the block that does](#the-held-watches).
