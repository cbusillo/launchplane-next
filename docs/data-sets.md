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

No password is in a step Launchplane decides, an answer of the API, the page or Launchplane's log. Measured on the test bed: two data sets had different passwords, the web part and the job of an instance had their data set's, a connection with a wrong password was refused, and neither password appeared in `/running` or in the last hour of `journalctl --unit launchplane`.

Anyone who can run `docker inspect` on the scratch container can read it, as they can read the data itself. Inside the database server's own container, connections over its local socket need no password, as the PostgreSQL image sets up; only `docker exec` reaches that socket, and the tests read the job's beats that way.

## What is on the host

Everything is tied by labels and by networks, never by container names. Identifiers are random: a data set is 12 hex digits, and an instance's container takes 12 more, so the same build released twice never collides with itself.

| Thing | Name when made | Labels |
| --- | --- | --- |
| Data set network | `launchplane-data-<data set>` | product, lane, `launchplane.data-set` |
| Database volume | `launchplane-data-<data set>-database` | the same |
| Database server | `<product>-<lane>-database-<data set>` | the same, and `launchplane.part=database` |
| Web part of an instance | `<product>-<lane>-<12 hex>` | product, lane, build, `launchplane.part=web`, and `launchplane.data-set` |
| Job part of an instance | `<web part's container>-job` | product, lane, build, data set, `launchplane.part=job`, `launchplane.instance=<web part's container>` |

Names are given when a thing is made and never used to find it again. A data set is read as every network, volume and database server carrying its labels, each with the name it has on the host, and every later step uses that name. A database server renamed by hand is started under its new name; no second one is made beside it. Two networks, two volumes or two database servers carrying one data set's labels are read as two, shown on the page, and stop a move with `503` naming both: which one is the data set is not Launchplane's to choose.

The database answers at the address `database`, an alias that exists only on its own data set's network. The web part is attached to the lanes network, so the proxy can reach it, and to exactly one data set's network. The job part is attached only to its data set's network. An instance therefore cannot reach another data set's database even by mistake: there is no route to that network, and the name `database` on its own network is its own server.

A container with no `launchplane.part` label is a web part, so the instances of earlier steps read as before.

## The quiet network

Every network an instance is attached to is made with Docker's internal mode and the bridge option `com.docker.network.bridge.gateway_mode_ipv4=isolated`: the lanes network, and each data set's network. Internal mode alone still reaches the Docker host through the bridge's gateway address; the isolated gateway mode leaves the bridge without one. An instance on these networks reaches the containers beside it and nothing else: not the internet, not the local network, not the Docker host.

The proxy is the one way in. It is attached to the lanes network and to `launchplane-proxy-outside`, a plain bridge network through which its port 80 is published.

Launchplane reads every network's mode each time it reads the host. A web part or job attached to any network that is not quiet reads as `is_quiet: false` and is shown as `can reach out`. Such a web part is never switched to and never counts as serving: a move replaces it through the check and the switch like a stopped one. Such a job is retired and started again. A data set whose network is not quiet stops a move with `503` before anything is started on it or switched to, including an instance an interrupted release left. Launchplane also refuses to attach anything to a `launchplane-lanes` that is not quiet.

No instance is given a way out yet. When the instance of a release may reach outside services, and how it avoids sending mail or taking payments twice, is Chris's question (README, open question 3).

## Bringing a lane up and releasing

The move reads, decides one step, takes it and reads again, as in [the move](the-move.md). Nothing is answered as done, and nothing is switched to, from what an earlier read or an earlier step said: each of those depends on what the latest read shows. For a product with a database, the next step is the first that applies, after the proxy and route steps:

1. If an instance failed its check in this call, remove it and answer `check failed`.
2. Retire every job that does not belong to the instance receiving visitors, one per step.
3. If the instance receiving visitors is running this build, is quiet, and is on a data set, settle it:
   1. Make its data set ready (see 7). A stopped database is started; one that does not accept connections stops the move with `503`. A serving lane is not answered `unchanged` over a database that is down.
   2. Retire its job if it is not running or not quiet, or start it if it has none. A job started in this call is watched for 3 s; if it stops, the move answers `job failed` with `422` and the job's last log lines. The web address has already moved by then, because the new job may start only once the old one has stopped, and that is after the switch.
   3. Retire every other instance of the lane, then answer.
4. Choose the data set: the one the instance receiving visitors uses; else the lane's only data set; else a fresh one. A lane with two or more data sets and no instance receiving visitors stops with `503` and changes nothing, because nothing yet says which one to use.
5. Retire any instance that is neither receiving visitors nor the instance to switch to. An instance on another data set, or one that is not quiet, is never switched to.
6. Make the data set ready (see 7), whether or not an instance to switch to already exists.
7. To make a data set ready, take the first that applies: stop with `503` if two of any part carry its labels; make its network; stop with `503` if its network is not quiet; make its volume; make its database server on that network and volume; start a stopped server by the name it was read under; stop with `503` on a server that is restarting or paused; wait until the server accepts connections (`pg_isready`, up to 60 s), once per call.
8. If there is an instance to switch to, check it at the product's health path, then point the web address at it. Otherwise start the instance on the data set.

Because of 2 and 3, only one instance's job runs at a time: the old instance's job stops once the web address has moved, then the new instance's job starts, then the old instance retires. Between the two jobs no job runs: measured at 0.50 and 0.55 s, from the old job's last beat to the new job's first.

After a crash nothing is resumed. The next call reads what is on the host and goes on from there.

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `job failed` | 422 | The web address points at the new instance, and its job stopped within 3 s of starting. `error` has why and the job's last log lines. Asking again retires the job and starts it again. |

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
- Bringing a lane up with a fresh data set: 12.1 s, twice. Mostly the database's first start, the 3 s check and the 3 s job watch. Asking again: 0.14 to 0.15 s.
- A release that keeps the data set: 10.4 s, twice, with no request failing under a request loop.
- Retiring the lane: 0.97 to 1.43 s. Deleting its data set: 0.38 s.
- Launchplane killed at four points of a bring-up and four of a release, then asked again: each reached one instance of the build with its job running on the one data set, rows kept, and no two jobs' beats interleaved.
- `postgres:18-alpine` and `python:3.15-slim` pull and run.

