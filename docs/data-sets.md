# A lane with a database

A product that says it has a database runs each lane against a data set: a PostgreSQL server on its own quiet network, with its data on a Docker volume. A release that does not change the data's structure starts the new instance on the same data set. Cloning a data set is not built yet, and neither are files (README, open question 13).

## The product description

One JSON file per product, `/var/lib/launchplane/products/<product>.json` on the control container (`LAUNCHPLANE_PRODUCTS` names another folder), read at the start of each move. A product with no file is a web product with no data that answers its check at `/`. A file Launchplane cannot read stops the move with `503`, naming the file and what is wrong.

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

The web part and the job reach their database through libpq's own variables: `PGHOST=database`, `PGPORT=5432`, `PGUSER=postgres`, `PGDATABASE=postgres` and `PGPASSWORD`.

## What is on the host

Everything is tied by labels and by networks, never by container names. A data set's name is 12 random hex digits, and an instance's container takes 12 more.

| Thing | Name when made | Labels |
| --- | --- | --- |
| Data set network | `launchplane-data-<data set>` | product, lane, `launchplane.data-set` |
| Database volume | `launchplane-data-<data set>-database` | the same |
| Database server | `<product>-<lane>-database-<data set>` | the same, `launchplane.part=database`, `launchplane.password-file` |
| Web part of an instance | `<product>-<lane>-<12 hex>` | product, lane, build, `launchplane.part=web`, `launchplane.data-set`, `launchplane.secret-variables` |
| Job part of an instance | `<web part's container>-job` | the same with `launchplane.part=job`, and `launchplane.instance=<web part's container>` |

A thing is found again by its labels, under whatever name it then has. A database server renamed by hand is started under its new name.

The database answers at the address `database`, which exists only on its own data set's network. The web part is attached to the lanes network, where the proxy reaches it, and to its data set's network. The job is attached only to its data set's network.

Every network an instance is attached to is quiet: Docker's internal mode with the bridge option `com.docker.network.bridge.gateway_mode_ipv4=isolated`. Internal mode alone still reaches the Docker host. An instance reaches the containers beside it and nothing else. The proxy is the one way in, through `launchplane-proxy-outside`. No instance is given a way out yet (README, open question 3).

## The password

A data set's password lives with its data: the file `launchplane-password` at the top of the data set's volume, readable only by its owner, written once before the database first starts. PostgreSQL is told where it is with `POSTGRES_PASSWORD_FILE`. Launchplane stores nothing and reads the file each time it starts a web part or a job, which get it as `PGPASSWORD`.

- A database server removed by hand is made again on the kept volume with the same password.
- A password is made only for a new data set, one whose data PostgreSQL has not made yet.
- Data that exists without its password file stops the start with `503` naming the data set, and the server is left stopped. Put the password back in the file, or delete the data set by name.

Anyone who can read the volume, or run `docker inspect` on a web part or job, can read the password.

## No secret leaves Launchplane

Every answer of the API, headers and body, and every line of the service's log has each secret replaced with `[a secret Launchplane gave this lane, removed]`. Only the secret is replaced; the explanation around it stays.

- The secrets are read from the host before each answer is made: each data set's password file, and the environment of every web part and job.
- Launchplane also keeps, in memory only, every secret it has read or given out since it started. A log line is cleaned with those.
- When the secrets cannot be read, no answer is made and nothing is moved: the call answers `503` with the reason. The page itself still opens.
- A secret a product has changed before printing it, such as encoding it, is not recognised.

## A move with data

`POST /lanes/{product}/{lane}/move` works as in [the move](the-move.md), with these additions.

A move takes these steps, each only when needed, reading the host again before each one:

1. Make the data set: its network, its volume, its database server. Start a stopped server. Wait up to 60 s for the server to accept connections.
2. Start the new instance on the data set of the instance receiving visitors, or on the lane's only data set, or on a new one.
3. Check the new instance at the product's health path, then point the web address at it.
4. Stop the old instance's job, start the new instance's job, and watch it stay up 3 s.
5. Retire the old instance.

Only one instance's job runs at a time. Between the old job and the new one no job runs: measured at 0.50 and 0.55 s.

A move stops with `503`, saying what it found and what it had changed, when:

- a data set has two networks, two volumes or two database servers carrying its labels;
- a data set's network is not quiet;
- a database server is restarting or paused, is attached to any network but its data set's, or has anything mounted but its data set's volume, once, where PostgreSQL keeps its data;
- the lane has two or more data sets and no instance receiving visitors;
- the new instance, after its check, is attached to a network it must not be on, has anything mounted, or runs against another data set than the instance receiving visitors.

A web part or a job with anything mounted is replaced. That rule stands only until files are decided (README, open question 13). Until then a build whose image declares a volume of its own fails it.

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `job failed` | 422 | The web address points at the new instance, and its job did not stay up 3 s. `error` has why and the job's last log lines. Asking again retires the job and starts it again. |

The other outcomes are those of [the move](the-move.md). `data_set` in the answer is the data set of the instance receiving visitors. After a crash nothing is resumed: asking again reads the host and goes on from there.

## Retiring a lane with data

Retiring removes the route, then the jobs, then the instances. It keeps the lane's data sets and lists them in `kept_data_sets`. To delete one, name it:

```
curl -X POST http://launchplane.shiny:8700/lanes/<product>/<lane>/retire \
  -H 'Content-Type: application/json' -d '{"delete_data_set": "<data set>"}'
```

The data set is deleted once the lane has no job and no instance left: its database servers, then its volumes, then its networks. The answer's `deleted_data_set` names it. A name that is not one of this lane's data sets deletes nothing.

## What the page shows for a lane

`/running` answers a verdict for every lane in `lanes`, and the page shows it as given.

| Field | Meaning | On the page |
| --- | --- | --- |
| `a_move_is_at_work` | This Launchplane is moving or retiring the lane right now. Nothing is stored for it; it is gone when the process is. | |
| `problems` | What is wrong. | red |
| `release_under_way` | While a move is at work: what it has not finished yet. | ordinary colour, under `Launchplane is moving this lane now. A release is under way:` |
| `left_unfinished` | With no move at work: what a move would finish. | red, under `No move is at work on this lane, and it was left unfinished. Asking for a build again finishes it:` |

While a move is at work, only a part attached or mounted where it must not be is a problem. After Launchplane is killed mid-release and starts again, the lane shows as left unfinished; asking for the build of the instance beside the active one finishes the release, and asking for the active build removes what was left. A lane retired with its data set kept shows as left unfinished too.

Each lane also lists its data sets and each instance's data set and job.

## Known and not fixed

- **A job whose instance is gone**, attached to an outside network or with something mounted, shows as release progress while a move is at work.
- **With a lane's route removed by hand**, the verdict says only that no instance receives visitors.
- **Two running jobs** show as release progress while a move is at work.
- **A release that finishes between two readings of the move flag** can show for one reading as left unfinished.

## On the test bed

`tests/test_bed_data_set.py` builds the pretend product in `tests/pretend_product` on the scratch container as `launchplane-test-ledger:a` and `:b`: a small Python web app with PostgreSQL whose job writes a beat to the database every 0.2 s and calls a listener every second.

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_data_set.py -v
```

The tests use the product `test-bed-ledger`, lanes `testing` and `other`, and remove what they made, data sets included. The pretend product is built on `python:3.15-slim` with Debian's `libpq5`; on `python:3.15-alpine` psycopg could not find libpq.

## Measured

On the test bed, Docker 29.9.3 on the scratch container, November 15:

- Docker accepts the isolated gateway mode. `launchplane-lanes` and each data set network read back as internal with no gateway address.
- From an instance, TCP to 1.1.1.1:80, to the control container's port 8700, to the scratch container's Docker port 2376 and to a listener's published port all failed. From a plain container on Docker's default bridge all four succeeded. The job's calls reached the listener only after `docker network connect bridge` gave the job a way out.
- From an instance, `database:5432` answered; another data set's database was refused by its address and not found by its name.
- Bringing a lane up with a fresh data set: 12.1 to 12.6 s. Asking again: 0.12 to 0.15 s.
- A release that keeps the data set: 10.4 to 10.8 s, with no request failing.
- Retiring the lane: 0.97 to 1.43 s. Deleting its data set: 0.38 s.
- Launchplane killed at four points of a bring-up and four of a release, then asked again: each reached one instance of the build with its job running on the one data set, rows kept, and no two jobs running at once.
- `/running` with one plain lane on the host: 0.06 to 0.07 s. With a database lane too: 0.12 to 0.13 s. With the scratch container unreachable, a call answers `503` after 5 s.
- A job that printed its password and exited was answered `job failed` with that line cleaned, and a build whose health answer carried its password was answered `check failed` with it cleaned. No password appeared in any answer or in `journalctl --unit launchplane`.
- A container being removed while its password file was read made Docker answer `RWLayer of container … is unexpectedly nil`. Launchplane reads the host again, up to three times, when Docker refuses a question about a container it has just listed.
