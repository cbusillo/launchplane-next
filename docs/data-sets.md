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

The instance reaches its database at `database:5432`, user `postgres`, database `postgres`, with no password. Only containers on that data set's network can reach the address at all.

## What is on the host

Everything is tied by labels and by networks, never by container names. Identifiers are random: a data set is 12 hex digits, and an instance's container takes 12 more, so the same build released twice never collides with itself.

| Thing | Name | Labels |
| --- | --- | --- |
| Data set network | `launchplane-data-<data set>` | product, lane, `launchplane.data-set` |
| Database volume | `launchplane-data-<data set>-database` | the same |
| Database server | `<product>-<lane>-database-<data set>` | the same, and `launchplane.part=database` |
| Web part of an instance | `<product>-<lane>-<12 hex>` | product, lane, build, `launchplane.part=web`, and `launchplane.data-set` |
| Job part of an instance | `<web part's container>-job` | product, lane, build, data set, `launchplane.part=job`, `launchplane.instance=<web part's container>` |

The database answers at the address `database`, an alias that exists only on its own data set's network. The web part is attached to the lanes network, so the proxy can reach it, and to exactly one data set's network. The job part is attached only to its data set's network. An instance therefore cannot reach another data set's database even by mistake: there is no route to that network, and the name `database` on its own network is its own server.

A container with no `launchplane.part` label is a web part, so the instances of earlier steps read as before.

## The quiet network

Every network an instance is attached to is made with Docker's internal mode and the bridge option `com.docker.network.bridge.gateway_mode_ipv4=isolated`: the lanes network, and each data set's network. Internal mode alone still reaches the Docker host through the bridge's gateway address; the isolated gateway mode leaves the bridge without one. An instance on these networks reaches the containers beside it and nothing else: not the internet, not the local network, not the Docker host.

The proxy is the one way in. It is attached to the lanes network and to `launchplane-proxy-outside`, a plain bridge network through which its port 80 is published.

Launchplane checks a network's mode every time it reads the host. It refuses to attach an instance to a lanes network or data set network that is not quiet, and says so; a data set whose network is not quiet is shown that way on the page.

No instance is given a way out yet. When the instance of a release may reach outside services, and how it avoids sending mail or taking payments twice, is Chris's question (README, open question 3).

## Bringing a lane up and releasing

The move reads, decides one step, takes it and reads again, as in [the move](the-move.md). For a product with a database, the next step is the first that applies, after the proxy and route steps:

1. If an instance failed its check in this call, remove it and answer `check failed`.
2. Retire every job that does not belong to the instance receiving visitors, one per step.
3. If the instance receiving visitors runs this build on a data set, settle it: retire its job if it is not running, start its job if it has none, then retire every other instance of the lane, and answer.
4. Choose the data set: the one the instance receiving visitors uses; else the lane's only data set; else a fresh one. A lane with two or more data sets and no instance receiving visitors stops with `503` and changes nothing, because nothing yet says which one to use.
5. Retire any instance that is neither receiving visitors nor the instance to switch to. An instance on another data set is never switched to.
6. If there is an instance to switch to, check it at the product's health path, then point the web address at it.
7. Otherwise make the data set ready: make what is missing of it (network, volume, server), start a stopped server, stop with `503` on a server that is restarting or paused, then wait until the server accepts connections (`pg_isready`, up to 60 s). Then start the instance on it.

Because of 2 and 3, only one instance's job runs at a time: the old instance's job stops once the web address has moved, then the new instance's job starts, then the old instance retires. Between the two jobs, for a moment, none runs.

After a crash nothing is resumed. The next call reads what is on the host and goes on from there.

## Retiring a lane with data

Retiring removes the route, then the jobs, then the instances. It keeps the lane's data sets and lists them in `kept_data_sets`. To delete one, name it:

```
curl -X POST http://launchplane.shiny:8700/lanes/<product>/<lane>/retire \
  -H 'Content-Type: application/json' -d '{"delete_data_set": "<data set>"}'
```

The data set is deleted only once the lane has no instance left: its server is stopped and removed, then its volume, then its network. The answer's `deleted_data_set` names it. A name that is not one of this lane's data sets deletes nothing and answers `deleted_data_set: null`.

## The page

Each lane shows its data sets under its web address: `Data set <id>  database running`, with a red line if the network is missing or not quiet, or the volume is missing. Each instance shows its data set and, when it has one, its job's state. A job whose instance is gone is listed as `Job with no instance`. A lane with only a data set left still has its section, so a kept data set is never out of sight.

## On the test bed

`tests/test_bed_data_set.py` builds the pretend product in `tests/pretend_product` on the scratch container as `launchplane-test-ledger:a` and `:b`. It is a small Python web app with PostgreSQL: `/health` and `/` answer with its version once the database answers, `POST /rows` writes a row, `GET /rows` reads them back. Its job writes a beat to the database every 0.2 s and calls a listener the tests run on the scratch container every second. `python /app.py reach <host> <port>` tries one TCP connection.

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_data_set.py -v
```

The tests use the product `test-bed-ledger`, lanes `testing` and `other`, and remove what they made, data sets included.
