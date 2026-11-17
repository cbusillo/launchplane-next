# Requests

A call that changes a lane is written down as a request, answered at once, and carried through by a worker inside Launchplane. The four calls are the [move](the-move.md), [retire](retiring-a-lane.md), and beginning and ending [maintenance](maintenance.md). What each does to a lane is in its own document and is unchanged.

```
curl -i -X POST http://launchplane.shiny:8700/lanes/<product>/<lane>/move \
  -H 'Content-Type: application/json' -d '{"build": "<image>"}'
curl http://launchplane.shiny:8700/requests/<id>
curl http://launchplane.shiny:8700/requests
curl -X POST http://launchplane.shiny:8700/requests/<id>/retry
curl -X POST http://launchplane.shiny:8700/requests/<id>/give-up
```

## Asking

Each of the four calls writes a request and answers `202` with the request and a `Location` header, `/requests/<id>`. Nothing on the host has changed yet.

- **`?wait=<seconds>`**, up to 3600. The call answers when the request ends, with the status and body the call had before requests were kept: `200` with the result, `422` with the result for an outcome that is not a success, or `503` with `error` when it stopped. If the request has not ended in that time, or needs a person, the answer is `202` with the request. The `Location` header is there either way.
- **Asking the same thing of a lane while a request for it is waiting or working** writes nothing and answers with that request. The same thing is the same call, lane, build and data set named for deleting.
- **`Idempotency-Key: <text>`**. The same key with the same call and body answers with the request already written, whether or not it has ended. The same key with anything else answers `409` and writes nothing. A key is never forgotten and never shown: the record keeps only its SHA-256 digest. A call with a key always writes its own request, even when the same thing is already waiting.
- **`409`** also answers a call for a lane that has a request needing a person, and says which.
- **`422`** with `error` also answers a call whose build or data set name carries a secret a container holds. Nothing is written.
- **`503`** with `error` means the record could not be reached. The answer says the request was not written and nothing was changed only when that is known. When the record was lost after the request was sent to it, the answer says that whether it was written is not known: if it was, it will be worked. Asking again with the same key answers with it if it was, and `GET /requests` lists it.

## Reading

`GET /requests/<id>` answers the request:

| Field | Meaning |
| --- | --- |
| `id`, `product`, `lane` | Which request, and its lane. |
| `work` | `moving`, `retiring`, `beginning maintenance` or `ending maintenance`. |
| `build`, `delete_data_set` | Exactly what was asked: the build of a move, the data set a retire named for deleting. |
| `asked_by`, `asked_at` | The address the call came from, and when. Nobody is known by name yet. |
| `stands` | `waiting`, `working`, `done`, `failed` or `needs a person`. |
| `ended_at`, `answer`, `error` | When it ended; the result the call answers with `?wait=`; the real error of a request that failed or needs a person. |
| `attempts` | Each time a worker took the request up: `number`, `began_at`, `ended_at`, and `ended` as `done`, `failed` or `interrupted`. |
| `events` | One line for each thing Launchplane did or was refused, in order: `at`, the `attempt` it belongs to, `did`, and `error` with the real error where it was refused. |

A request is `done` when its call would have answered `200`, and `failed` when it would have answered `422` or `503`. `answer` is that of the attempt that ended the request, so after an interruption its `outcome` and `did` speak for the last attempt only; `events` has every attempt.

`GET /requests` answers `unfinished`, every request waiting, working or needing a person, oldest first and with its attempts and events, and `ended`, the 20 that ended last, newest first.

## The worker

One worker runs in the Launchplane process. It takes the oldest request that is waiting or working, on a lane with no request needing a person, and carries it through with the same judgement and the same steps as before. One request works at a time, in the order they came.

- **A lane's turn** is a PostgreSQL advisory lock held by a connection of its own for as long as the request is worked. When the work ends or Launchplane dies, the connection closes and the turn is free. No transaction is open and no row is locked while Launchplane talks to the container host. Before each action on the host the worker asks that connection whether it still holds the lock, and when it does not it stops without acting and writes nothing more; the request is then taken up again as interrupted.
- **After a restart, with nobody calling,** a request found working with nobody holding its lane's turn was interrupted. The worker ends that attempt as `interrupted`, begins a new one, reads the host and decides from what is there, as asking again always did. It does not go on from where the last attempt was.
- **A request taken up three times without ending** is not taken up a fourth time. It stands as `needs a person` with the reason in `error`, and its lane takes no other request. `POST /requests/<id>/retry` ends it as `failed` and writes a new request for the same thing, answering `202` with it. `POST /requests/<id>/give-up` ends it as `failed` and changes nothing on the host. Either answers `409` for a request that does not need a person.
- **Nothing is run again by itself** except this taking-up after an interruption. A request that fails stays failed until someone asks again.
- **Losing the record while working** stops the work at the next thing to write. The request stays working, and when the record is back it is taken up as interrupted, which counts.
- **A restart of Launchplane while a request is working**, a deploy included, is an interruption like any other.

## What the record holds

Three tables in a PostgreSQL database of Launchplane's own: a request, an attempt, and an event. It holds what was asked and what Launchplane did. It never holds which build a lane runs, which instance receives visitors, whether a part is healthy, or how far a request has got: those are read from the host every time. With every event deleted, Launchplane decides exactly as before.

No secret is written to the record, because the record outlives the container that holds a secret and nothing could then know to remove it on the way out.

- The caller's key is kept as a digest only.
- A call whose build or data set name carries a secret a container holds is refused. Every secret on the host is read just before, as for every answer. Whether a value carries a secret is judged as it is for an answer: on the value itself, whatever characters the secret has.
- A product or lane name is at most 31 lowercase letters, digits and hyphens. A password Launchplane makes is 43 characters, so a name cannot be one.
- `asked_by` is the address the connection came from, not text the caller chose.
- Every event, error and answer is cleaned of every secret a container holds before it is written. The worker reads every container's secrets when it takes a request up, and a secret it makes while working is known from the moment it is made.

## On the page

- A lane's card has a row for each of its requests that has not ended: what was asked, how it stands, and for the one being worked what it has done so far and how many times it was taken up.
- A request that needs a person is red, with the reason and the two calls under it, and its card has a red `needs a person` pill.
- A request for a lane with nothing on the host yet has a card of its own.
- A lane is `work under way` while a request for it stands as working in the record. `work` and `a_move_is_at_work` in `/running` come from there too.
- When the record cannot be read, the first card says so with the error. The host is still shown. Whether a request is at work is then unknown, so a finding that would be under way shows as left unfinished. `/running` has the same in `record_error`, and the unfinished requests in `requests`.

## The database

PostgreSQL 17 from Debian's packages runs on the control container as the systemd service `postgresql`. `./scripts/deploy-control` installs it when it is missing, makes the database `launchplane` and the login `launchplane`, and runs the migrations with Alembic before it restarts Launchplane.

- The login has no password and is not a superuser. It is used through the local socket by the system user `launchplane` only. It owns the database, and no other login but the superuser `postgres` may connect to it.
- PostgreSQL listens on `127.0.0.1` and `::1` only.
- `LAUNCHPLANE_DATABASE` names the database, by default `postgresql:///launchplane?host=/var/run/postgresql`.
- A migration is a file in `migrations/versions`, written as SQL. The one that turned keys into digests cannot be undone, and going down past it refuses and says why.

## Known and not fixed

- **The database is not backed up.** Losing it loses the requests and their history, and no lane: what runs is on the host.
- **`asked_by` is an address, not a person.** Who may ask is not settled yet.
- **A request cannot be cancelled, and a newer request does not replace an older one.** Requests for one lane run in the order they came.
- **Only one request works at a time**, whatever its lane.
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

## On the test bed

```
LAUNCHPLANE_TEST_DATABASE=postgresql://postgres:test@127.0.0.1:55432/postgres uv run pytest
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_requests.py -v
```

The tests of the record, the worker and the calls run against a pretend record in memory, and again against PostgreSQL when `LAUNCHPLANE_TEST_DATABASE` names one a test may empty. `docker run -d -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:17-alpine` gives one. CI runs both.

`tests/test_bed_requests.py` stops PostgreSQL on the control container for one test and starts it again. Every test-bed test waits for each request with `?wait=`. A kill test either lets Launchplane take the request up again by itself, or ends the request in the record while Launchplane is down, to leave the lane as a request that failed halfway would.
