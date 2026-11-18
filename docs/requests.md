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

- **`?wait=<seconds>`**, up to 3600. The call answers when the request ends, with the status and body the call had before requests were kept: `200` with the result, `422` with the result for an outcome that is not a success, or `503` with `error` when it stopped. A request that was cancelled answers `409` with `error`, saying by what and what was put back. If the request has not ended in that time, or needs a person, the answer is `202` with the request. The `Location` header is there either way.
- **Asking the same thing of a lane while a request for it is waiting or working** cancels nothing, with a key or without. The same thing is the same call, lane, build and data set named for deleting. Without a key the call writes nothing and answers with that request; if that request is being cancelled, it writes a new one. With a key it is answered with a request of its own, worked after the first.
- **Asking anything else of a lane** writes a new request, and that cancels every request for the lane that was waiting or working before it and asks something else: see [cancelling](#cancelling). The new request is worked in its turn, once those before it have ended.
- **`Idempotency-Key: <text>`**. The same key with the same call and body answers with the request already written, whether or not it has ended. The same key with anything else answers `409` and writes nothing. A key is never forgotten and never shown: the record keeps only its SHA-256 digest. A call with a key always writes its own request, even when the same thing is already waiting.
- **`409`** also answers a call for a lane that has a request needing a person, and says which. That request is not cancelled.
- **`422`** with `error` also answers a call that carries a secret a container holds, in any part of it. Nothing is written.
- **`503`** with `error` means the record could not be reached. The answer says the request was not written and nothing was changed only when the record had answered that it holds nothing like it. When the record could not be asked, the call may repeat a request already written, with a key or without one; and when the record was lost after the request was sent, it may be written. Either way the answer says that whether it was written is not known: if it was, it will be worked. Asking again with the same key answers with it if it was, and `GET /requests` lists it.

## Reading

`GET /requests/<id>` answers the request:

| Field | Meaning |
| --- | --- |
| `id`, `product`, `lane` | Which request, and its lane. |
| `work` | `moving`, `retiring`, `beginning maintenance` or `ending maintenance`. |
| `build`, `delete_data_set` | Exactly what was asked: the build of a move, the data set a retire named for deleting. |
| `asked_by`, `asked_at` | The address the call came from, and when. Nobody is known by name yet. |
| `stands` | `waiting`, `working`, `done`, `failed`, `cancelled` or `needs a person`. |
| `ended_at`, `answer`, `error` | When it ended; the result the call answers with `?wait=`, or for a cancelled request what [cancelling](#cancelling) left; the real error of a request that failed or needs a person. |
| `cancel_asked_at`, `cancel_asked_by` | When a cancel was asked, and by what: `request <id>`, a newer request for the lane, or `a call from the address <address>`. `null` when none was asked. |
| `cancel_refused` | Why a cancel that was asked was not carried out, written once the worker has read the host: see [cancelling](#cancelling). |
| `attempts` | Each time a worker took the request up: `number`, `began_at`, `ended_at`, and `ended` as `done`, `failed`, `cancelled` or `interrupted`. |
| `events` | One line for each thing Launchplane did or was refused, in order: `at`, the `attempt` it belongs to, `did`, and `error` with the real error where it was refused. |

A request is `done` when its call would have answered `200`, `failed` when it would have answered `422` or `503`, and `cancelled` when it was cancelled in time. `answer` is that of the attempt that ended the request, so after an interruption its `outcome` and `did` speak for the last attempt only; `events` has every attempt.

`GET /requests` answers `unfinished`, every request waiting, working or needing a person, oldest first and with its attempts and events, and `ended`, the 20 that ended last, newest first.

## Cancelling

`POST /requests/<id>/cancel` cancels one request, and a newer request for a lane cancels the older ones the same way. What it means depends only on where the request is:

- **Waiting:** it ends as `cancelled`. Nothing was done.
- **Working, before its point of no return:** the request is marked in the record. Its worker is never killed: it reads the mark before its next action and puts the lane back, by the same judged and checked steps as the call itself. The request ends as `cancelled` when nothing it made remains on the host and each thing done to put the lane back left the part it touched as it must be. If one of them fails, or leaves that part wrong, such as a database server that had to be killed to stop it, the request ends as `failed` and `error` says which.
- **Working, past that point:** it is too late. The request is finished as asked and ends as it would have. `cancel_refused` says why.
- **Ended, or needing a person:** nothing changes.

The answer of a cancelled request has `put_back`, each thing done to put the lane back; `data_sets_kept`, the data sets the request made, which stay; and `findings`, every sentence the judgement had for the lane when the cancel ended. What else is wrong or unfinished on the lane is not the cancel's verdict: it is in `findings` and on the page. `put_back` is told from the events of every attempt, each written before the change it names, so it survives a record lost in between. It is history and decides nothing.

Where the point is, and what the request made, are read from the host each time, never from the events. An instance, a background service and a data set's network carry the label `launchplane.request` with the id of the request that made them, so nothing an earlier request left is touched. A routes file the proxy has not loaded yet is waited for first, so the instance the proxy sends visitors to is known before anything is removed.

| Request | Too late once | Put back before that |
| --- | --- | --- |
| A move | The proxy sends the web address to an instance this request made, or to one this call switched to: visitors have been switched. A move to the build already serving that is cancelled before it changed anything is not too late, and changes nothing. | Each background service and each instance this request made is removed, as after a failed check, and then the logins no container holds. In a lane in maintenance a move makes nothing, so nothing is removed. |
| A retire | The lane's routes file is gone. Removing it is the first thing a retire changes, and nothing after it can be put back: the containers are removed, and Launchplane does not make a removed instance again by itself. The rest is finished as asked, except that a data set named for deleting is kept when the cancel came before it was deleted: `cancel_refused` says so, and the answer lists it in `kept_data_sets`. | Nothing: nothing was changed. |
| Beginning maintenance | The routes file points at the back soon page. That is its first change too. Ending maintenance brings the lane back. | Nothing: nothing was changed. |
| Ending maintenance | The routes file points at the instance again. | What it started is stopped again, the database server last, as after a failed check. |

Putting a lane back never deletes data: a data set the request made, and a database server it started, stay, as retiring leaves them. A cancelled first move of a product with data is still `cancelled`, and names the data set in `data_sets_kept`.

| Status | Body | Meaning |
| --- | --- | --- |
| `200` | the request | It is cancelled, now or before. |
| `202` | the request | It is being worked and is marked. `cancel_asked_by` says by what. |
| `409` | `error` | It is not cancelled and will not be: the worker found it too late and says why, or it had ended as `done` or `failed`, or it needs a person. Nothing was changed by this call. |
| `404` | `error` | The record holds no such request. |

With `?wait=<seconds>` the call answers as soon as the answer is known: the request is cancelled, its cancel was refused, it has ended, or it needs a person. The mark is kept in the record, so after a restart the worker that takes the request up still puts its lane back.

## The workers

Four workers run in the Launchplane process. Each takes the oldest request that is waiting or working on a lane no other worker is on and that has no request needing a person, and carries it through with the same judgement and the same steps as before. So requests for different lanes are worked at the same time, four lanes at most, and the requests for one lane one at a time, in the order they came. Four is the lanes of the finish-line test: two products, each with testing and production.

- **A lane's turn** is a PostgreSQL advisory lock held by a connection of its own for as long as the request is worked. When the work ends or Launchplane dies, the connection closes and the turn is free. No transaction is open and no row is locked while Launchplane talks to the container host. Before each action on the host the worker asks that connection whether it still holds the lock, and when it does not it stops without acting and writes nothing more; the request is then taken up again as interrupted.
- **After a restart, with nobody calling,** a request found working with nobody holding its lane's turn was interrupted. The worker ends that attempt as `interrupted`, begins a new one, reads the host and decides from what is there, as asking again always did. It does not go on from where the last attempt was.
- **A request taken up three times without ending** is not taken up a fourth time. It stands as `needs a person` with the reason in `error`, and its lane takes no other request. `POST /requests/<id>/retry` ends it as `failed` and writes a new request for the same thing, answering `202` with it. `POST /requests/<id>/give-up` ends it as `failed` and changes nothing on the host. Either answers `409` for a request that does not need a person.
- **Nothing is run again by itself** except this taking-up after an interruption. A request that fails stays failed until someone asks again.
- **Losing the record while working** stops the work at the next thing to write. The request stays working, and when the record is back it is taken up as interrupted, which counts.
- **A restart of Launchplane while a request is working**, a deploy included, is an interruption like any other.

## What lanes share on the host

A worker acts only on its own lane's containers, data sets and routes file. What two lanes share is protected for as long as it is touched, and no lock is held across the rest of a lane's work:

| Shared | What protects it |
| --- | --- |
| The proxy and back soon page containers, the `launchplane-lanes` network and the proxy's outside network | One lock in the Launchplane process, held while one of them is looked for and made. Two lanes that find the proxy missing make it once. |
| The folder of routes files | Each lane has its own file, put in place by a rename. One lock in the process is held while the folder is read whole, and while one file is placed and renamed or removed. |
| What the proxy has loaded | Only read. Traefik loads every lane's file itself. |
| Images | Docker, which pulls one image once for two callers. |
| The secrets Launchplane cleans from what it says | One set for every worker and call. A password is added to it as it is made, before Docker or a database server is given it, so no worker can write another's secret to the record or a log. |
| The record | PostgreSQL: up to 10 connections for every call and worker, and one more for the turn of each lane being worked. |

## What the record holds

Three tables in a PostgreSQL database of Launchplane's own: a request, an attempt, and an event. It holds what was asked and what Launchplane did. It never holds which build a lane runs, which instance receives visitors, whether a part is healthy, or how far a request has got: those are read from the host every time. With every event deleted, Launchplane decides exactly as before.

No secret is written to the record, because the record outlives the container that holds a secret and nothing could then know to remove it on the way out.

A value is judged as an answer is: on the value itself, whatever characters the secret has, against every secret a container holds.

- **What a caller sent** is refused with `422` when it carries one, and nothing is written: the product, lane, build and data set name of a call, and who asked, also when a request is asked for again.
- **What Launchplane writes itself** is cleaned of it: every event, error and answer. The worker reads every container's secrets when it takes a request up, and a secret it makes while working is known from the moment it is made.
- The caller's key is kept as a digest only.
- `asked_by` is the address the connection came from. Launchplane does not read a forwarding header, which is text a caller chooses.

## On the page

- A lane's card has a row for each of its requests that has not ended: what was asked, how it stands, and for the one being worked what it has done so far and how many times it was taken up. One that is marked to be cancelled says by what, or why the cancel was refused.
- A request that was cancelled stays on its lane's card for 10 minutes after it ended, as `cancelled`, with by what and what was put back. `/running` has these in `cancelled`.
- A request that needs a person is red, with the reason and the two calls under it, and its card has a red `needs a person` pill.
- A request for a lane with nothing on the host yet has a card of its own.
- A lane is `work under way` while a request for it stands as working in the record. `work` and `a_move_is_at_work` in `/running` come from there too.
- When the record cannot be read, or does not answer a reading within 6 s (3 s to reach the database and 3 s more to answer), the first card says so with the error. The host is still shown, and no request is. Whether a request is at work on a lane is then not known, on either of the two readings of the record a status makes: each lane says so, its condition is `could not be read` unless something in it is wrong whatever a request is doing, and a finding that would be under way or left unfinished shows as `could not be read`. `/running` has the error in `record_error`, an empty `requests`, and `a_move_is_at_work` as `null` for every lane.

## The database

PostgreSQL 17 from Debian's packages runs on the control container as the systemd service `postgresql`. `./scripts/deploy-control` installs it when it is missing, makes the database `launchplane` and the login `launchplane`, and runs the migrations with Alembic before it restarts Launchplane.

- The login has no password and is not a superuser. It is used through the local socket by the system user `launchplane` only. It owns the database, and no other login but the superuser `postgres` may connect to it.
- PostgreSQL listens on `127.0.0.1` and `::1` only.
- `LAUNCHPLANE_DATABASE` names the database, by default `postgresql:///launchplane?host=/var/run/postgresql`.
- A migration is a file in `migrations/versions`, written as SQL. The one that turned keys into digests cannot be undone, and going down past it refuses and says why. Undoing the one that added `cancelled` turns each cancelled request into a failed one.

## Known and not fixed

- **The database is not backed up.** Losing it loses the requests and their history, and no lane: what runs is on the host.
- **`asked_by` is an address, not a person.** Who may ask is not settled yet.
- **Who may cancel is not settled.** Any caller can, as any caller can ask.
- **A cancel waits for the action under way.** The mark is read between actions, and one action can take as long as a check, 20 s, or a stop, 30 s.
- **A cancel asked of a retire keeps the data set it named, also when the rest was too late to cancel.** The direction session chose this as the safe side; Chris may reverse it.
- **A move that switched to an instance an earlier request left, was killed, and is cancelled before it is taken up again ends as `cancelled`,** though visitors were switched: that instance carries the earlier request's stamp, and the call that switched is gone. Within one attempt the call knows it switched, and it is too late.
- **A data set is stamped on its network only,** the first part of it that is made.
- **A part made before stamps carries none,** so a cancel leaves it.
- **An instance left by an earlier request that a move retired before it was cancelled is not brought back.** It received no visitors.
- **A request that ends between the cancel and its worker's next reading ends as it would have.** The cancel call then answers `409` and says how it ended.
- **The locks on what lanes share are in the process.** A second Launchplane on the same host is held off each lane by the lane's turn, and not off the proxy or the routes folder.
- **A reading given up on keeps its connection until the record answers.** At most 4 readings wait at once, so the workers keep connections.
- **The fresh names of a move are made anew in each attempt.** An instance started by an interrupted attempt is found on the host by its build and taken as the one to check.
- **A request that needs a person is not told to anyone.** It shows on the page and in `GET /requests`.
- **A worker can lose its lane's turn while one action on the host is under way.** The turn is asked before each action, not during it. An action can take as long as a check, 20 s, or a stop, 30 s. If the lock's connection ends in that time and another Launchplane takes the lane, that one action still finishes. With one worker in one process this needs a lost connection and a second Launchplane.
- **A secret no container holds any more is not known to be one.** A caller who sends the password of a container already removed has it stored as sent, in a build or a data set name.
- **Untried:** two Launchplane processes on one record. The lane's turn and the count of attempts are built for it and tested against PostgreSQL, not on the test bed.

## Measured

On the test bed on November 17, PostgreSQL 17, one nginx lane.

- A call answered `202` in 20 to 25 ms. The worker took the request up 4 to 6 ms after it was asked.
- A move from one build to another, with `?wait=`: 6.2 to 6.4 s, as before requests were kept. Asking again for a finished move: 0.12 s, where it was 0.07 s; the wait reads the record every 0.1 s.
- Killed with `kill -9` while the new instance was being checked: systemd started Launchplane again, the request was taken up 1.2 s after the kill with nobody calling, and it ended `done` and `switched` 7.5 s after it was asked, with two attempts, the first `interrupted`.
- The 58 test-bed tests passed in 21 minutes.

With each lane its own turn, on November 17, nginx lanes:

- One lane moved from one build to another in 6.3 s. Two at once took 6.3 s, four at once 6.8 s, and six asked at once 12.2 s: four, then two.
- A call answered `202` in 50 ms from another machine.
- A move cancelled by the call while its new instance was being checked ended as `cancelled` 2.4 s later, with the instance removed. The web address answered from the old build throughout, and no request failed.
- A newer request asked 2 s into a move: the first ended `cancelled`, and the lane ended on the newer build.
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

`tests/test_bed_requests.py` stops PostgreSQL on the control container for one test and starts it again, and freezes it for another. `tests/test_bed_lanes.py` moves two lanes at once and cancels a move three ways. Every test-bed test waits for each request with `?wait=`. A kill test either lets Launchplane take the request up again by itself, or ends the request in the record while Launchplane is down, to leave the lane as a request that failed halfway would.
