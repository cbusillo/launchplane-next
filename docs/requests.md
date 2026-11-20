# Requests

A call that changes a lane is written down as a request, answered at once, and carried through by a worker inside Launchplane. Each lane has its own turn, so different lanes are worked at the same time, and a request can be cancelled. The four calls are the [move](the-move.md), [retire](retiring-a-lane.md), and beginning and ending [maintenance](maintenance.md). What each does to a lane is in its own document and is unchanged.

```
curl -i -X POST http://launchplane.shiny:8700/lanes/<product>/<lane>/move \
  -H 'Content-Type: application/json' -d '{"build": "<image>"}'
curl http://launchplane.shiny:8700/requests/<id>
curl http://launchplane.shiny:8700/requests
curl -X POST http://launchplane.shiny:8700/requests/<id>/retry
curl -X POST http://launchplane.shiny:8700/requests/<id>/give-up
curl -X POST http://launchplane.shiny:8700/requests/<id>/cancel
```

## Asking

Each of the four calls writes a request and answers `202` with the request and a `Location` header, `/requests/<id>`. Nothing on the host has changed yet.

- **`?wait=<seconds>`**, up to 3600. The call answers when the request ends, with the status and body the call had before requests were kept: `200` with the result, `422` with the result for an outcome that is not a success, or `503` with `error` when it stopped. A request that was cancelled answers `409` with `error`, saying by what and what was put back, and one that was replaced answers `409` saying by which request. If the request has not ended in that time, or needs a person, the answer is `202` with the request. The `Location` header is there either way.
- **Asking the same thing of a lane while a request for it is waiting or working** writes nothing, replaces nothing and answers with that request, without a key or with one not used before. The same thing is the same call, lane, build and data set named for deleting. If that request is being cancelled, the call writes a new one.
- **Asking anything else of a lane** writes a new request, which waits. A lane has at most one request at work and one waiting, and the new request takes the waiting place. The request at work is not touched by it: it ends only by finishing, by failing, or by a [cancel](#cancelling) a person asks for. The request that was waiting ends as `replaced`, with nothing done, and its `replaced_by` names the new request. The new request is worked once the one at work has ended. The record itself refuses a lane a second request that waits.
- **`Idempotency-Key: <text>`**. The same key with the same call and body answers with the request already written, whether or not it has ended. The same key with anything else answers `409` and writes nothing. A key is never forgotten and never shown: the record keeps only its SHA-256 digest. A key says only that a call is one sent before: a call with a key not used before is held to its lane's rule like any other. When it is answered with a request already waiting or working, the key is kept against that request, so the same key sent again at any later time answers with that request, however it has ended.
- **`409`** also answers a call for a lane that has a request needing a person, and says which. That request is not cancelled.
- **`422`** with `error` also answers a call that carries a secret a container holds, in any part of it. Nothing is written.
- **`503`** with `error` means the record could not be reached. The answer says the request was not written and nothing was changed only when the record had answered that it holds nothing like it. When the record could not be asked, the call may repeat a request already written, with a key or without one; and when the record was lost after the request was sent, it may be written. Either way the answer says that whether it was written is not known: if it was, it will be worked. Asking again with the same key answers with it if it was, and `GET /requests` lists it.

## Reading

`GET /requests/<id>` answers the request:

| Field | Meaning |
| --- | --- |
| `id`, `product`, `lane` | Which request, and its lane. |
| `generation` | The number of the release this request is, stamped on what it makes: see [the move](the-move.md#what-it-does). No other request has it, also after the record is lost and made again. |
| `work` | `moving`, `retiring`, `beginning maintenance` or `ending maintenance`. |
| `build`, `delete_data_set` | Exactly what was asked: the build of a move, the data set a retire named for deleting. |
| `asked_by`, `asked_at` | The address the call came from, and when. Nobody is known by name yet. |
| `stands` | `waiting`, `working`, `done`, `failed`, `cancelled`, `replaced` or `needs a person`. |
| `ended_at`, `answer`, `error` | When it ended; the result the call answers with `?wait=`, or for a cancelled request what [cancelling](#cancelling) left; the real error of a request that failed or needs a person, or why a move that is still working is not finished yet. |
| `cancel_asked_at`, `cancel_asked_by` | When a cancel was asked, and by what: `a call from the address <address>`. `null` when none was asked. A request written before November 20, 2026 may say `request <id>`: a newer request then cancelled the older. |
| `replaced_by` | The newer request for the lane that took the waiting place from this one. `null` unless it stands as `replaced`. |
| `cancel_refused` | Why a cancel that was asked was not carried out: see [cancelling](#cancelling). For a move it is written by whoever learns it at the gate, the call that cancels or the worker. |
| `attempts` | Each time a worker took the request up: `number`, `began_at`, `ended_at`, and `ended` as `done`, `failed`, `cancelled`, `interrupted`, or `unfinished` for a request that could not be ended yet. |
| `events` | One line for each thing Launchplane did or was refused, in order: `at`, the `attempt` it belongs to, `did`, and `error` with the real error where it was refused. |

A request is `done` when its call would have answered `200`, `failed` when it would have answered `422` or `503`, `cancelled` when it was cancelled in time, and `replaced` when a newer request took its waiting place before it was taken up. `answer` is that of the attempt that ended the request, so after an interruption its `outcome` and `did` speak for the last attempt only; `events` has every attempt.

`GET /requests` answers `unfinished`, every request waiting, working or needing a person, oldest first and with its attempts and events, and `ended`, the 20 that ended last, newest first.

## Cancelling

`POST /requests/<id>/cancel` cancels one request, and nothing else cancels one: a newer request for the lane waits behind the request at work, and [replaces](#asking) only one that waits. What a cancel means depends only on where the request is:

- **Waiting:** it ends as `cancelled`. Nothing was done.
- **A move being worked:** the call itself carries the cancel to [the gate](the-move.md#the-gate) on the container host before it answers, and there either the cancel or the rename of the routes file wins, never both. A cancel counts from the moment it passes the gate, not from the moment it is written to the record: one that is on record and has not reached the gate when the switch passes it is told too late.
  - **The cancel wins** when the gate has no verdict for the request's generation. The gate then says `abandoned` for good, no switch of it can rename, and the worker puts the lane back: it removes what the generation made and nothing else. The request ends as `cancelled`. A Launchplane killed just after the cancel won, also before the call was answered, puts the lane back when it takes the request up again.
  - **The cancel is too late** once the gate says `committed`, also while the proxy has not loaded the renamed file and the old build still answers. The call answers `409` at once, `cancel_refused` says the rename was the boundary, and the release is finished as asked, also after a kill.
  - **The gate gives no answer,** since the host cannot be reached or the gate's command fails: the call answers `503` with the reason. The cancel stays asked in the record and is carried to the gate again by the worker of the request, where it wins or is too late like any other.
- **A retire, or beginning or ending maintenance, being worked:** the request is marked in the record. Its worker reads the mark before its next action and puts the lane back, or finds it too late and says why in `cancel_refused`.
- **Ended, or needing a person:** nothing changes.

A move never ends over a cancel that has no answer from the gate. When a cancel is recorded while the worker is making its answer, the record refuses that ending, the worker carries the cancel to the gate, and the move goes on from what the gate then says. So a move whose check failed, or one to the build already active, that is cancelled before its result is saved ends as `cancelled`, and one that had been switched ends as `done` with `cancel_refused`.

A move whose cancel won ends as `failed` only when Docker refused to remove an instance of the release and it is still there. `error` then begins `The cancel won` and names what could not be removed, and a cancel asked again answers `409` with the same words, never that the cancel lost. While the host cannot be read, such a move stays `working`. For a retire or maintenance, putting back that fails or leaves a part wrong, such as a database server that had to be killed to stop it, ends the request as `failed` and `error` says which.

The answer of a cancelled request has `put_back`, each thing done to put the lane back; `data_sets_kept`, the data sets the request made, which stay; and `findings`, every sentence the judgement had for the lane when the cancel ended. What else is wrong or unfinished on the lane is not the cancel's verdict: it is in `findings` and on the page. `put_back` is told from the events of every attempt, each written once the host had made the change it names, so nothing is listed that did not happen. A change made just before Launchplane was killed, with its event not yet written, is not listed. It is history and decides nothing. `logins_not_read` lists each database server whose logins, or their holders, could not be read while the lane was put back.

Where the point is, and what the request made, are read from the host each time, never from the events. What a release made carries its generation, so nothing an earlier request left is taken for it.

| Request | Too late once | Put back before that |
| --- | --- | --- |
| A move | The routes file is renamed for this request's generation. Whether the proxy has loaded it yet makes no difference. A move to the build already active is never too late, and a cancel of it changes nothing. | Each background service and each instance of this generation is removed, as after a failed check, and then the logins no container holds. In a lane in maintenance a move makes nothing, so nothing is removed. |
| A retire | The lane's routes file is gone. Removing it is the first thing a retire changes, and nothing after it can be put back: the containers are removed, and Launchplane does not make a removed instance again by itself. The rest is finished as asked, except that a data set named for deleting is kept when the cancel came before it was deleted: `cancel_refused` says so, and the answer lists it in `kept_data_sets`. | Nothing: nothing was changed. |
| Beginning maintenance | The routes file points at the back soon page. That is its first change too. Ending maintenance brings the lane back. | Nothing: nothing was changed. |
| Ending maintenance | The routes file points at the instance again. | What it started is stopped again, the database server last, as after a failed check. Every part of the lane is then stopped, and the cancel answers for each of them. |

Putting a lane back never deletes data: a data set the request made, and a database server it started, stay, as retiring leaves them. A cancelled first move of a product with data is still `cancelled`, and names the data set in `data_sets_kept`.

| Status | Body | Meaning |
| --- | --- | --- |
| `200` | the request | It is cancelled, now or before. |
| `202` | the request | It is being worked. For a move the cancel has won at the gate and the lane is being put back. For another call it is marked, and may still be found too late. `cancel_asked_by` says by what. |
| `409` | `error` | It is not cancelled and will not be: it was too late and the answer says why, or it had ended as `done`, `failed` or `replaced`, or it needs a person. Nothing was changed by this call. |
| `404` | `error` | The record holds no such request. |
| `503` | `error` | The record could not be reached, or the cancel is recorded and the gate gave no answer. The answer says which. |

With `?wait=<seconds>` the call answers as soon as the answer is known: the request is cancelled and its lane put back, its cancel was refused, it has ended, or it needs a person.

## The workers

Four workers run in the Launchplane process. Each takes the oldest request that is waiting or working on a lane no other worker is on and that has no request needing a person, and carries it through with the same judgement and the same steps as before. So requests for different lanes are worked at the same time, four lanes at most, and the requests for one lane one at a time, in the order they came. Four is the lanes of the finish-line test: two products, each with testing and production.

- **A lane's turn** is a PostgreSQL advisory lock held by a connection of its own for as long as the request is worked. When the work ends or Launchplane dies, the connection closes and the turn is free. No transaction is open and no row is locked while Launchplane talks to the container host. Before each action on the host the worker asks that connection whether it still holds the lock, and when it does not it stops without acting and writes nothing more; the request is then taken up again as interrupted.
- **After a restart, with nobody calling,** a request found working with nobody holding its lane's turn was interrupted. The worker ends that attempt as `interrupted`, begins a new one, reads the host and decides from what is there, as asking again always did. It does not go on from where the last attempt was.
- **A request interrupted three times without ending** is not taken up a fourth time. It stands as `needs a person` with the reason in `error`, and its lane takes no other request. `POST /requests/<id>/retry` ends it as `failed` and writes a new request for the same thing, answering `202` with it. `POST /requests/<id>/give-up` ends it as `failed` and changes nothing on the host. Either answers `409` for a request that does not need a person.
- **A request is left to a person only by the gate's word.** At the third interruption the gate is asked for the request's generation, in a way that also refuses any switch or edit of its last attempt from then on. Only if it answers `none`, and no cancel is on record for the request, does it stand as `needs a person`. One with a cancel on record is taken up again and the cancel carried out. A move the gate says is committed is finished, one whose cancel won is put back, and one the gate cannot be asked about is taken up again, however often they were interrupted. A retry or a give-up of a move that needs a person asks the gate first, the same way: if the gate says committed or abandoned, the call answers `409` and the request is worked again.
- **A move that cannot be ended yet is left open.** Its attempt ends as `unfinished`, which is not counted, `error` says why, and it is taken up again after 5 s, ahead of newer work on its lane. See [how a request may end](the-move.md#what-it-does).
- **A change of a lane's route that was sent and has not run cannot arrive under a later request.** A retire and beginning or ending maintenance change the route by an [edit at the gate](the-move.md#the-gate). When a request is taken up, its attempts are read from the record under its lane's turn. One that has an earlier attempt is fenced through it before it reads its lane, since a Launchplane that was killed may have sent an edit. So a command Docker accepted and that runs late has either been made by then, and is read with the lane, or changes nothing. An attempt that sent an edit and got no answer never ends its request: the attempt ends as `unfinished`, and the next one fences it, reads the route and goes on from what is there, to the end of the maintenance or the retire. A request whose edits were all answered, or that sent none, asks the gate for no fence. While the gate gives no answer to a fence nothing is done on the host, the attempt ends as `unfinished`, and the lane takes no other request. The gate is a command in the proxy's container, so a proxy that is not running is started to ask it. A request at the limit of interruptions is fenced through its last attempt before it stands as `needs a person`, and giving it up or retrying it fences every attempt of it first and answers `503` while the gate cannot be asked.
- **Nothing else is run again by itself.** A request that fails stays failed until someone asks again.
- **Losing the record while working** stops the work at the next thing to write. The request stays working, and when the record is back it is taken up as interrupted, which counts.
- **A restart of Launchplane while a request is working**, a deploy included, is an interruption like any other.

## What lanes share on the host

A worker acts only on its own lane's containers, data sets and routes file. What two lanes share is protected for as long as it is touched, and no lock is held across the rest of a lane's work:

| Shared | What protects it |
| --- | --- |
| The proxy and back soon page containers, the `launchplane-lanes` network and the proxy's outside network | One lock in the Launchplane process, held while one of them is looked for and made. Two lanes that find the proxy missing make it once. |
| The folder of routes files | Each lane has its own file, put in place by a rename. Each change is one command of [the gate](the-move.md#the-gate), which holds its lock on the host for that command. One lock in the process is held while the folder is read whole and while a gate command runs, so no reading finds the folder mid-change. |
| What the proxy has loaded | Only read. Traefik loads every lane's file itself. |
| Images | Docker, which pulls one image once for two callers. |
| The secrets Launchplane cleans from what it says | One set for every worker and call. A password is added to it as it is made, before Docker or a database server is given it, so no worker can write another's secret to the record or a log. |
| The record | PostgreSQL: up to 10 connections for every call and worker, and one more for the turn of each lane being worked. |

## What the record holds

Four tables in a PostgreSQL database of Launchplane's own: a request, an attempt, an event, and the digest of each key a request was answered for. It holds what was asked and what Launchplane did. It never holds which build a lane runs, which instance receives visitors, whether a part is healthy, or how far a request has got: those are read from the host every time. With every event deleted, Launchplane decides exactly as before.

No secret is written to the record, because the record outlives the container that holds a secret and nothing could then know to remove it on the way out.

A value is judged as an answer is: on the value itself, whatever characters the secret has, against every secret a container holds.

- **What a caller sent** is refused with `422` when it carries one, and nothing is written: the product, lane, build and data set name of a call, and who asked, also when a request is asked for again.
- **What Launchplane writes itself** is cleaned of it: every event, error and answer. The worker reads every container's secrets when it takes a request up, and a secret it makes while working is known from the moment it is made.
- The caller's key is kept as a digest only.
- `asked_by` is the address the connection came from. Launchplane does not read a forwarding header, which is text a caller chooses.

## On the page

- A lane's card has a row for each of its requests that has not ended: what was asked, how it stands, and for the one being worked what it has done so far and how many times it was taken up. One a cancel was asked of says by what, and for a move what the gate answered for it when the page was read: that the cancel won, that it came too late, that the gate has not been asked yet, or that the gate could not be asked.
- A request a cancel was asked of stays on its lane's card for 10 minutes after it ended, however it ended: as `cancelled` with by what and what was put back, or as `done` or `failed` with why the cancel was not carried out. A cancel that won and whose lane could not be put back shows the error that was saved: that it won, and what could not be removed. A request that was replaced stays for the same 10 minutes, as `replaced` and saying by which request; it is not shown as something wrong. `/running` has these in `cancels`.
- A request that needs a person is red, with the reason and the two calls under it, and its card has a red `needs a person` pill.
- A request for a lane with nothing on the host yet has a card of its own.
- A lane is `work under way` while a request for it stands as working in the record. `work` and `a_move_is_at_work` in `/running` come from there too.
- When the record cannot be read, or does not answer a reading within 6 s (3 s to reach the database and 3 s more to answer), the first card says so with the error. The host is still shown, and no request is. Whether a request is at work on a lane is then not known, on either of the two readings of the record a status makes: each lane says so, its condition is `could not be read` unless something in it is wrong whatever a request is doing, and a finding that would be under way or left unfinished shows as `could not be read`. `/running` has the error in `record_error`, an empty `requests`, and `a_move_is_at_work` as `null` for every lane.

## The database

PostgreSQL 17 from Debian's packages runs on the control container as the systemd service `postgresql`. `./scripts/deploy-control` installs it when it is missing, makes the database `launchplane` and the login `launchplane`, and runs the migrations with Alembic before it restarts Launchplane.

- The login has no password and is not a superuser. It is used through the local socket by the system user `launchplane` only. It owns the database, and no other login but the superuser `postgres` may connect to it.
- PostgreSQL listens on `127.0.0.1` and `::1` only.
- `LAUNCHPLANE_DATABASE` names the database, by default `postgresql:///launchplane?host=/var/run/postgresql`.
- A migration is a file in `migrations/versions`, written as SQL. The one that turned keys into digests cannot be undone, and going down past it refuses and says why. Undoing the one that added `cancelled` turns each cancelled request into a failed one, and undoing the one that added `replaced` turns each replaced request into one cancelled by the request that replaced it. The one that lets a lane have one waiting request ends every waiting request of a lane but the newest as replaced by the newest. Undoing the one that keeps every key a request was answered for leaves each request one of its keys. The one that added `generation` gives each request written before it a generation made from chance. It stops, with nothing changed, while a move begun before it is working or needs a person: the Launchplane that began it settles it first.

## Known and not fixed

- **The database is not backed up.** Losing it loses the requests and their history, and no lane: what runs is on the host.
- **`asked_by` is an address, not a person.** Who may ask is not settled yet.
- **Who may cancel is not settled.** Any caller can, as any caller can ask.
- **A routes file that is not Launchplane's, on a lane with nothing else on the host, does not show on the status page.** A lane is shown from its instances, background services and data sets, and from the routes the proxy has loaded.
- **A loaded route whose target is not a kind Launchplane knows makes the whole reading of the proxy fail.** `/running` and every call then answer `503` for every lane, until that route is removed from the proxy by hand.
- **A lane is put back after the action under way.** Who wins is decided at once at the gate. The worker asks the gate between actions, and one action can take as long as a check, 20 s, or a stop, 30 s.
- **A cancel asked of a retire keeps the data set it named, also when the rest was too late to cancel.** The direction session chose this as the safe side; Chris may reverse it.
- **Ending maintenance that is cancelled answers for every part of the lane,** also one that was wrong before it began: which of them it started, the host cannot show, since a start changes no label.
- **A data set carries its generation on its network only,** the first part of it that is made.
- **A part made before generations carries none,** so a cancel leaves it.
- **An instance left by an earlier request that a move retired before it was cancelled is not brought back.** It received no visitors.
- **A retire or a maintenance call that ends between the cancel and its worker's next reading ends as it would have.** The cancel call then answers `409` and says how it ended.
- **Only a move's cancel is decided at the gate.** A second Launchplane on the same host is held off each lane by the lane's turn. Every change of a routes file, and a release's cancel, are held off each other on the host. The proxy, the back soon page and readings of the routes folder are protected in the process only.
- **After a lost reply, a request answers from its last attempt alone.** An ended maintenance can say `not in maintenance`, and a retire can leave out what it removed. The host is as asked, and what each attempt did is in its events.
- **A host read that fails after a maintenance or retire edit was answered ends that request `failed` with the lane part way:** back soon showing and a part still running. A move in the same position stays working and tries again. Asking again finishes it.
- **Giving up a request at the interruption limit lets the lane go as it stands.** Its attempts are fenced, and the route is not read: a lane left showing back soon with a part running stays so until the next request.
- **Only a refusal Docker answers ends a request whose edit was not answered.** An attempt refused before its edit was sent, such as a back soon image Docker will not pull, fails with the real error and its lane goes on. A host that cannot be reached at that point leaves the request working instead.
- **A request whose edit the gate keeps failing to answer stays working.** It is taken up again every 5 s and its lane takes no other request until the gate answers or the request is cancelled. Nobody is told.
- **A reading given up on keeps its connection until the record answers.** At most 4 readings wait at once, so the workers keep connections.
- **A request that needs a person is not told to anyone.** It shows on the page and in `GET /requests`.
- **A worker can lose its lane's turn while one action on the host is under way.** The turn is asked before each action, not during it. An action can take as long as a check, 20 s, or a stop, 30 s. If the lock's connection ends in that time and another Launchplane takes the lane, that one action still finishes. With one worker in one process this needs a lost connection and a second Launchplane.
- **A secret no container holds any more is not known to be one.** A caller who sends the password of a container already removed has it stored as sent, in a build or a data set name.
- **A request that arrives at the moment a worker takes up the request waiting on its lane is held for 1 s.** PostgreSQL finds the two waiting on each other and ends one. Either the waiting request is replaced and never begins, or it is worked and the call answers `503` saying that whether its request was written is not known; it was not. The same was true of a newer request before the waiting place.
- **Asking again for a waiting request while the request ahead of it needs a person is answered with the waiting request, not `409`.** The call is found to be a repeat before the lane is found to be stopped.
- **The pretend record of the tests lists ended requests newest id first.** PostgreSQL lists them by when they ended, so a request that was replaced comes after the older request that finished later; the pretend record puts it before.
- **Untried:** two Launchplane processes on one record. The lane's turn and the count of attempts are built for it and tested against PostgreSQL, not on the test bed.

## Measured

On the test bed on November 17, PostgreSQL 17, one nginx lane.

- A call answered `202` in 20 to 25 ms. The worker took the request up 4 to 6 ms after it was asked.
- A move from one build to another, with `?wait=`: 6.2 to 6.4 s, as before requests were kept. Asking again for a finished move: 0.12 s, where it was 0.07 s; the wait reads the record every 0.1 s.
- Killed with `kill -9` while the new instance was being checked: systemd started Launchplane again, the request was taken up 1.2 s after the kill with nobody calling, and it ended `done` and `switched` 7.5 s after it was asked, with two attempts, the first `interrupted`.
- The 58 test-bed tests passed in 21 minutes.

With each lane its own turn, on November 17, nginx lanes:

- One lane moved from one build to another in 6.3 s. Two at once took 6.3 s, four at once 6.8 s, and six asked at once 12.2 s: four, then two.
- On November 20, with the gate and 3,188 files in its folder: one lane moved in 5.9 to 7.0 s over ten moves, and two at once in 9.6, 10.8 and 9.3 s. Before the routes were read without the gate's files, two at once took 10.5 s. `tests/test_bed_lanes.py` reports that time and does not judge by it.
- A call answered `202` in 50 ms from another machine.
- A move cancelled by the call while its new instance was being checked ended as `cancelled` 2.4 s later, with the instance removed. The web address answered from the old build throughout, and no request failed.
- On November 20, a move held before its switch while a second and a third request arrived for its lane: the second ended `replaced` at once with no attempt, the first ended `switched` 2 s after the hold was let go, and the third `switched` 5 s after that. A probe every 0.5 s was answered by the old build, then the first build, then the third, and never failed.
- Cancelled, then killed with `kill -9` before its worker read the mark: after the restart the request was taken up and ended `cancelled`, with two attempts, `interrupted` and `cancelled`.
- With every PostgreSQL process stopped with `SIGSTOP`, `/running` answered `200` in 6.2 s with the host and `record_error`. Restarting Launchplane then took 3 s.
- Docker's client warned once in 22 minutes of tests that its pool of 10 connections was full. It opens another connection then, and nothing failed.
- The 63 test-bed tests passed in 22 minutes 30 s, one after another. None ran side by side: tests in five of the seven files kill Launchplane, stop PostgreSQL, restart Docker or stop the proxy, which reaches every lane. Lanes are worked side by side inside the tests of `tests/test_bed_lanes.py`.

## On the test bed

```
LAUNCHPLANE_TEST_DATABASE=postgresql://postgres:test@127.0.0.1:55432/postgres uv run pytest
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_requests.py tests/test_bed_lanes.py -v
```

The tests of the record, the worker and the calls run against a pretend record in memory, and again against PostgreSQL when `LAUNCHPLANE_TEST_DATABASE` names one a test may empty. `docker run -d -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:17-alpine` gives one. CI runs both.

`tests/test_bed_requests.py` stops PostgreSQL on the control container for one test and starts it again, and freezes it for another. `tests/test_bed_lanes.py` moves two lanes at once and cancels a move two ways. Every test-bed test waits for each request with `?wait=`. A kill test either lets Launchplane take the request up again by itself, or ends the request in the record while Launchplane is down, to leave the lane as a request that failed halfway would.
