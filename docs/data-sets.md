# A lane with a database

A product that says it has a database runs each lane against a data set: a PostgreSQL server on its own quiet network, with its data on a Docker volume. A lane instance is a build running against one data set. A release that does not change the data's structure starts the new instance on the same data set. Cloning a data set is not built yet, and neither are files: whether files are shared or cloned in a move is open (README, open question 13).

## How a product says it has a database

A product description is one JSON file per product, `/var/lib/launchplane/products/<product>.json` on the control container (`LAUNCHPLANE_PRODUCTS` names another folder). Launchplane reads it at the start of each move. A product with no file is a web product with no data that answers its check at `/`. A file Launchplane cannot read stops the move with `503`, naming the file and what is wrong.

```json
{
  "health_path": "/health",
  "database": {"image": "postgres:18-alpine"},
  "job": {"command": ["python", "/app.py", "job"]}
}
```

| Field | Meaning |
| --- | --- |
| `health_path` | The path the check requests on port 80. Default `/`. |
| `database` | The lane runs against a data set whose server is this PostgreSQL image. Leave it out for a product with no data. |
| `job` | The command that runs the product's background work, from the same build as its web part. Leave it out for a product with none. |

The web part and the job are given what they need to reach their database in libpq's own variables: `PGHOST=database`, `PGPORT=5432`, `PGUSER=postgres`, `PGDATABASE=postgres` and `PGPASSWORD`. A PostgreSQL client that reads them needs no other setting. Only containers on that data set's network can reach the address at all.

## The password

Each data set's database is made with its own password, 32 random bytes, drawn by Launchplane at the moment it makes the database server. Launchplane stores nothing. The password lives on the host with the data set: in the database server container's environment, as `POSTGRES_PASSWORD`, which the PostgreSQL image reads when it first makes the database. When Launchplane starts a web part or a job on that data set, it reads the password from the database server container it read for that data set, and gives it to the new container as `PGPASSWORD`. A database with no password stops the start with `503` saying so, and names no password.

Anyone who can run `docker inspect` on the scratch container can read it, as they can read the data itself. Inside the database server's own container, connections over its local socket need no password, as the PostgreSQL image sets up; only `docker exec` reaches that socket, and the tests read the job's beats that way.

## No secret leaves Launchplane

A secret is anything Launchplane has given to a container on the host that must not be shown: today, each data set's password. Launchplane puts none in a step it decides or an answer it builds. But much of what it answers is text it read from the host and did not write: a product's log lines after a failed check or job, the output of a command that failed inside a container, Docker's own errors, names and labels. Any of it can carry a secret.

So everything that leaves passes through one boundary, in `src/launchplane/secret_boundary.py`, and nothing is cleaned anywhere else:

- **Every answer of the API**: `/running`, a move, a retirement, each with any status, the page, and the refusals FastAPI writes itself. The boundary is around the whole application, not inside the calls, so a new call is behind it without being told. Each secret is replaced with `[a secret Launchplane gave this lane, removed]`. A JSON answer is cleaned value by value and written again, so a secret that JSON would write differently, such as one holding a quote, is still found.
- **Every line of the service's log**, whichever library wrote it: uvicorn's lines, a traceback, a warning from the Docker client. Each handler cleans the whole line it is about to write, message, arguments and traceback together. A line that cannot be written is not repeated by the logging library, which would print it uncleaned.

What the secrets are is read from the host, before each answer is made. Every container Launchplane gives a secret to carries the label `launchplane.secret-variables`, naming the environment variables that hold one: `POSTGRES_PASSWORD` on a database server, `PGPASSWORD` on a web part and a job. Launchplane reads those variables from every container with the label, running or stopped. A password is therefore known for as long as anything holds it: removing the database server hides nothing while a web part or job still has the password. Launchplane also keeps, in memory only, every secret it has read or given out since it started, so text read from a container stays cleaned after that container is gone. A log line is cleaned with these and does not read the host.

When the secrets cannot be read, no answer is made: the call answers `503` saying so, with the reason, before anything is read or moved. The one exception is the page itself, a file of Launchplane's own that is made without reading anything, so it still opens and can say the host cannot be read. An answer of Docker's that does not have the expected shape is reported by where it is wrong, never by repeating what Docker answered, because that may be a container's environment.

What the boundary does not do: a secret a product has changed before printing it, such as encoding it, is not recognised. And after Launchplane restarts, a secret nothing on the host holds any more is not known, though nothing on the host can print it then.

Measured on the test bed: two data sets had different passwords, the web part and the job of an instance had their data set's, and a connection with a wrong password was refused. A job that printed its password and exited was answered `job failed` with that line cleaned, and a build whose health answer carried its password in the status line was answered `check failed` with it cleaned. No password appeared in any answer, in `/running` or in `journalctl --unit launchplane`.

## What is on the host

Everything is tied by labels and by networks, never by container names. Identifiers are random: a data set is 12 hex digits, and an instance's container takes 12 more, so the same build released twice never collides with itself.

| Thing | Name when made | Labels |
| --- | --- | --- |
| Data set network | `launchplane-data-<data set>` | product, lane, `launchplane.data-set` |
| Database volume | `launchplane-data-<data set>-database` | the same |
| Database server | `<product>-<lane>-database-<data set>` | the same, `launchplane.part=database`, and `launchplane.secret-variables` |
| Web part of an instance | `<product>-<lane>-<12 hex>` | product, lane, build, `launchplane.part=web`, `launchplane.data-set`, and `launchplane.secret-variables` |
| Job part of an instance | `<web part's container>-job` | product, lane, build, data set, `launchplane.part=job`, `launchplane.instance=<web part's container>`, and `launchplane.secret-variables` |

Names are given when a thing is made and never used to find it again. A data set is read as every network, volume and database server carrying its labels, each with the name it has on the host, and every later step uses that name. A database server renamed by hand is started under its new name; no second one is made beside it. Two networks, two volumes or two database servers carrying one data set's labels are read as two, shown on the page, and stop a move with `503` naming both: which one is the data set is not Launchplane's to choose.

The database answers at the address `database`, an alias that exists only on its own data set's network. The web part is attached to the lanes network, so the proxy can reach it, and to exactly one data set's network. The job part is attached only to its data set's network. An instance therefore cannot reach another data set's database even by mistake: there is no route to that network, and the name `database` on its own network is its own server.

A container with no `launchplane.part` label is a web part, so the instances of earlier steps read as before.

## The quiet network

Every network an instance is attached to is made with Docker's internal mode and the bridge option `com.docker.network.bridge.gateway_mode_ipv4=isolated`: the lanes network, and each data set's network. Internal mode alone still reaches the Docker host through the bridge's gateway address; the isolated gateway mode leaves the bridge without one. An instance on these networks reaches the containers beside it and nothing else: not the internet, not the local network, not the Docker host.

The proxy is the one way in. It is attached to the lanes network and to `launchplane-proxy-outside`, a plain bridge network through which its port 80 is published.

Launchplane reads every network's mode, and every network each container is attached to, each time it reads the host. A web part, job or database attached to any network that is not quiet, or to any network other than its own, is not as it must be (see below) and is shown as `can reach out` when it can. Launchplane also refuses to attach anything to a `launchplane-lanes` that is not quiet.

No instance is given a way out yet. When the instance of a release may reach outside services, and how it avoids sending mail or taking payments twice, is Chris's question (README, open question 3).

## Bringing a lane up and releasing

The move reads, decides one step, takes it and reads again, as in [the move](the-move.md).

### What is read

Before every step, the move reads the whole lane from the host: each web part and job with every network it is attached to and its state; each job with how long it has been running by Docker's own clock, or no time at all if it is not running, is restarting or is paused; every network, volume and database server carrying the lane's data set labels, two being two; each database's networks and state, and whether it accepts connections, asked with `pg_isready` at that moment; and whether each network on the host is quiet. Nothing is reduced to a yes or no before the move decides.

A read is several questions to Docker, and the host can change between them. A job that was listed and has gone before it is described, or a database listed as running that is gone, stopped, paused or restarting when it is asked, is the host changing during the read, not a host that cannot be read. The whole host is then read again, three times at most, and what is there the last time is judged. A host that changes during all three answers `503` saying so. A host that cannot be reached, or any other refusal, is an error at once. `/running` reads the same way.

Docker reports a container waiting out its restart back-off as `Running`. Launchplane reads `Restarting` and `Paused` too, so a job that exits at once and is started again by Docker never reads as running.

### What the lane must be

One function, `lane_problems` in `src/launchplane/decide.py`, says from that reading whether the lane is as it must be for the build asked for:

- the instance the web address sends visitors to runs that build, is running, carries the data set it runs against, and is attached to exactly the lanes network and its data set's network, both quiet;
- the data set has exactly one network, one volume and one database server; the network is quiet; the server is running, accepts connections, and is attached to exactly the data set's network;
- the lane has exactly one job, belonging to that instance, running the same build, running for at least 3 s, attached to exactly the data set's network;
- no other instance runs in the lane.

The judge is asked before anything is started or switched, not only before success is answered:

- **Before anything in a data set is made or started.** The rules for a data set mark which problems a step of the move puts right: a missing network, volume or server, a server that is stopped, a server that does not yet accept connections. Every other problem stops the move with `503` before any step is taken on that data set. A stopped server attached to an outside network is therefore refused while still stopped. The step taken is chosen only once every problem found is one a step puts right, and an instance or job is started on a data set only when the judge finds nothing wrong with it.
- **Before the web address is pointed at a new instance.** After the check, the lane is read again and the same function is asked about the lane as it will be: the rules for the instance and for the data set it runs against, and one more, that it runs against the same data set as the instance receiving visitors now. The rules for the job and for other instances are left out there, because the old instance and its job are still present until the switch has loaded, and the steps after it replace them. An instance given an outside network during its check is not switched to, nor one on another data set.
- **Before success is answered.** A move answers `switched`, `unchanged` or `cleaned up` only when the function, asked after the move's last step, finds nothing. If it finds something the steps below cannot put right, the move answers `503` with what it found and what it changed.

### The steps

For a product with a database, the next step is the first that applies, after the proxy and route steps:

1. If an instance failed its check in this call, remove it and answer `check failed`.
2. Retire every job that does not belong to the instance receiving visitors, one per step.
3. If the instance receiving visitors is as an instance must be, settle the lane:
   1. Make its data set ready (see 7). A serving lane is not answered `unchanged` over a database that is stopped or does not accept connections.
   2. Retire its job if it has two, if one is attached to anything but its data set's network, or if it is not running and was not started in this call. Start a job if it has none. Wait on a job that has run less than 3 s, whoever started it, so a move retried after a crash between starting the job and watching it watches it. A job that does not stay up 3 s answers `job failed` with `422` and its last log lines; by then the web address has moved, because the new job may start only once the old one has stopped.
   3. Retire every other instance of the lane, then ask whether the lane is as it must be.
4. Choose the data set: the one the instance receiving visitors uses; else the lane's only data set; else a fresh one. A lane with two or more data sets and no instance receiving visitors stops with `503` and changes nothing.
5. Retire any instance that is neither receiving visitors nor the instance to switch to. A left-over instance is switched to only if it is as an instance must be and on the chosen data set.
6. Make the data set ready (see 7), whether or not an instance to switch to already exists.
7. To make a data set ready, first stop with `503` if it has any problem no step puts right: two of any part carrying its labels, a network that is not quiet, a server that is restarting or paused, or a server attached to any network but its data set's. Then take the first that applies: make its network; make its volume; make its database server on that network and volume; start a stopped server by the name it was read under; wait until a server read as not accepting connections does (up to 60 s).
8. If there is an instance to switch to, check it at the product's health path, read again, and point the web address at it only if the judge finds the lane fit to switch to it. Otherwise start the instance on the data set.

Only one instance's job runs at a time: the old instance's job stops once the web address has moved, then the new instance's job starts, then the old instance retires. Between the two jobs no job runs: measured at 0.50 and 0.55 s, from the old job's last beat to the new job's first.

After a crash nothing is resumed. The next call reads what is on the host and goes on from there.

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `job failed` | 422 | The web address points at the new instance, and its job did not stay up 3 s. `error` has why and the job's last log lines, cleaned of secrets. Asking again retires the job and starts it again. |

The other outcomes are those of [the move](the-move.md). `data_set` in the answer is the data set of the instance receiving visitors.

## Retiring a lane with data

Retiring removes the route, then the jobs, then the instances. It keeps the lane's data sets and lists them in `kept_data_sets`. To delete one, name it:

```
curl -X POST http://launchplane.shiny:8700/lanes/<product>/<lane>/retire \
  -H 'Content-Type: application/json' -d '{"delete_data_set": "<data set>"}'
```

The data set is deleted only once the lane has no job and no instance left: every database server read for it is stopped and removed, then every volume, then every network, each by the name it was read under. The answer's `deleted_data_set` names it. A name that is not one of this lane's data sets deletes nothing and answers `deleted_data_set: null`.

## The page

Each lane shows its data sets under its web address: `Data set <id>  database running`, with a red line for a missing network, volume or server, two of any of them, or a network that is not quiet. Each instance shows its data set and, when it has one, its job's state. An instance or job attached to a network that is not quiet is tagged `can reach out`. A job whose instance is gone is listed as `Job with no instance`. A lane with only a data set left still has its section, so a kept data set is never out of sight.

## On the test bed

`tests/test_bed_data_set.py` builds the pretend product in `tests/pretend_product` on the scratch container as `launchplane-test-ledger:a` and `:b`. It is a small Python web app with PostgreSQL: `/health` and `/` answer with its version once the database answers, `POST /rows` writes a row, `GET /rows` reads them back. Its job writes a beat to the database every 0.2 s and calls a listener the tests run on the scratch container every second. `python /app.py reach <host> <port>` tries one TCP connection.

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_data_set.py -v
```

The tests use the product `test-bed-ledger`, lanes `testing` and `other`, and remove what they made, data sets included. The pretend product is built on `python:3.15-slim` with Debian's `libpq5`. On `python:3.15-alpine`, psycopg's pure-Python driver could not find libpq and the instance failed its check.

## Measured

On the test bed, Docker 29.9.3 on the scratch container, November 15:

- Docker accepts `com.docker.network.bridge.gateway_mode_ipv4=isolated`. `launchplane-lanes` and each data set network read back as internal, with that option and no gateway address.
- The proxy publishes port 80 with two networks attached, `launchplane-proxy-outside` first: a request from the Mac through 192.168.1.56 answered 200, and the move test's request loops failed nothing.
- From an instance, TCP to 1.1.1.1:80, to the control container's port 8700, to the scratch container's Docker port 2376 and to the listener's published port all failed. From the listener, a plain container on Docker's default bridge, all four succeeded. The job's calls reached the listener only after `docker network connect bridge` attached a way out to the job.
- From an instance, `database:5432` answered; the other data set's database refused by its address and was not found by its name.
- Bringing a lane up with a fresh data set: 12.1 to 12.6 s. Mostly the database's first start, the 3 s check and the 3 s job watch. Asking again: 0.12 to 0.15 s, including the database probe and reading the job.
- A release that keeps the data set: 10.4 to 10.8 s, with no request failing under a request loop.
- A job that exits at once: Docker reported it `Running: true, Restarting: true` while it waited to restart. The move answered `job failed` with its log.
- Retiring the lane: 0.97 to 1.43 s. Deleting its data set: 0.38 s.
- Launchplane killed at four points of a bring-up and four of a release, then asked again: each reached one instance of the build with its job running on the one data set, rows kept, and no two jobs' beats interleaved.
- `postgres:18-alpine` and `python:3.15-slim` pull and run.
- Reading the secrets before each answer: `/running` with one lane on the host answered the Mac in 0.06 to 0.07 s. With the scratch container unreachable, a call answers `503` after the Docker client's 5 s limit, once, because the secrets are read first and nothing else is tried.
- Launchplane killed once the old job had gone, with the page's poll reading the host throughout: no read answered `503`. Before a torn read was read again, a poll that listed the job and then found it gone did.

