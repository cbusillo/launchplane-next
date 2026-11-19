# A lane with a database

A product that says it has a database runs each lane against a data set: a PostgreSQL server on its own quiet network, with its data on a Docker volume. A release that does not change the data's structure starts the new instance on the same data set. Cloning a data set is not built yet, and neither are files (README, open question 14).

## The product description

One JSON file per product, `/var/lib/launchplane/products/<product>.json` on the control container (`LAUNCHPLANE_PRODUCTS` names another folder), read at the start of each move. A product with no file is a web product with no data that answers its check at `/`. A file Launchplane cannot read stops the move with `503`, naming the file and what is wrong.

```json
{
  "health_path": "/health",
  "database": {"image": "postgres:18-alpine"},
  "background_service": {"command": ["python", "/app.py", "background-service"]}
}
```

| Field | Meaning |
| --- | --- |
| `health_path` | The path the check requests on port 80. Default `/`. |
| `database` | The lane runs against a data set whose server is this PostgreSQL image. Leave it out for a product with no data. |
| `background_service` | The command that runs the product's background service, from the same build as its web part. Leave it out for a product with none. |

The web part and the background service reach their database through libpq's own variables: `PGHOST=database`, `PGPORT=5432`, `PGDATABASE=app`, `PGUSER` and `PGPASSWORD`. The login is the instance's own: see [logins](#logins).

## What is on the host

Everything is tied by labels and by networks, never by container names. A data set's name is 12 random hex digits, and an instance's container takes 12 more.

| Thing | Name when made | Labels |
| --- | --- | --- |
| Data set network | `launchplane-data-<data set>` | product, lane, `launchplane.data-set` |
| Database volume | `launchplane-data-<data set>-database` | the same |
| Database server | `<product>-<lane>-database-<data set>` | the same, `launchplane.part=database`, `launchplane.secret-variables` |
| Web part of an instance | `<product>-<lane>-<12 hex>` | product, lane, build, `launchplane.part=web`, `launchplane.data-set`, `launchplane.secret-variables` |
| Background service of an instance | `<web part's container>-background-service` | the same with `launchplane.part=background-service`, and `launchplane.instance=<web part's container>` |

A thing is found again by its labels, under whatever name it then has. A database server renamed by hand is started under its new name.

The database answers at the address `database`, which exists only on its own data set's network. The web part is attached to the lanes network, where the proxy reaches it, and to its data set's network. The background service is attached only to its data set's network.

Every network an instance is attached to is quiet: Docker's internal mode with the bridge option `com.docker.network.bridge.gateway_mode_ipv4=isolated`. Internal mode alone still reaches the Docker host. An instance reaches the containers beside it and nothing else. The proxy is the one way in, through `launchplane-proxy-outside`. No instance is given a way out yet (README, open question 3).

## Logins

No password is kept with the data, and Launchplane keeps none. Each instance has a database login of its own, which Launchplane makes just before it makes the instance's web part and removes when the instance is retired.

**In the server.** The role `app` cannot log in and owns the database `app` and everything in it. An instance's login is `i_` and the 12 hex digits its container name ends with. It is a member of `app` and acts as `app`, so whatever one instance makes, the next can change. `postgres` has no password and is reached only from inside the database server's container; Launchplane does all its work there, with `psql`.

**The access rules** are these, and no others:

```
local all postgres trust
host all postgres all reject
host app +app all scram-sha-256
host all all all reject
```

So a password is asked for from every address, and only a login of an instance opens anything, and only the database `app`. The quiet network is the second lock.

**A new data set.** The server first starts with a random password that is thrown away, refusing every address. A set-up script Launchplane puts in the container makes `app`, gives it the database, takes the password off `postgres` and writes the rules.

**An instance.** Before each login is made Launchplane reads the rules back from the server. If they are not exactly the rules above, the call answers `503` saying what the server has, starts no instance on that data set and deletes nothing. Launchplane sends the server the scrambled form of the new password, never the password. The web part gets the password as `PGPASSWORD`. The background service is given the login its instance's web part holds, read from that container on the host.

**When it goes.** A move, a retire or the end of maintenance is not finished while a running database server of the lane has a login that no container on the host holds. Before it answers, it reads the logins from the server and the containers from the host, and removes each such login: closed to new sessions, its sessions ended, anything it owns given to `app`, then dropped. The same is done before each new login is made. So a retired instance's login goes in the call that retired it, one left by a call that was killed goes in the next move, retire or end of maintenance, and one left by a start that failed goes before its move ends as `failed`, where the server's logins can be read then.

- A release that keeps the data set has two instances for a short time, each with its own login.
- A database server removed by hand is made again on the kept volume. Its logins are in the data, so the instance still logs in.
- A stopped server is the one exception: its logins are not read, and wait in the data until a move, a retire or the end of maintenance finds the server running.
- When a running server's logins cannot be read, the lane's judgement has it as `could not be read` with the reason, whether or not a call is at work on the lane, and a move or a retire that has done everything else answers `503` with the same words. Ask again once the server answers, or delete the data set by name.
- A data set made before logins were per instance does not have these rules, so no instance is started on it. Nothing is converted: delete it by name.

Anyone who can run `docker inspect` on a web part or background service can read that instance's password. It opens nothing once the instance is retired.

## No secret leaves Launchplane

Every answer of the API, headers and body, and every line of Launchplane's own log has each secret replaced with `[a secret Launchplane gave this lane, removed]`. Only the secret is replaced; the explanation around it stays.

- Before every answer is made, Launchplane lists every marked container and describes it, and notes the secrets in its environment: every web part, background service and database server. So an answer that reads nothing else, such as a refusal of what was sent, is cleaned too, also just after Launchplane starts.
- A container's secrets are noted as it is described, before the next one is and before anything else is made of the list. So an error about the read itself, from that read or a later one, is cleaned of what was already found.
- Launchplane also keeps, in memory only, every secret it has read or given out since it started. A log line is cleaned with those.
- When the secrets cannot be read, no answer is made and nothing is moved: the call answers `503` with the reason. The page itself still opens. A container Launchplane cannot make sense of does not stop this read; only Docker not answering does.
- A secret a product has changed before printing it, such as encoding it, is not recognised.

## A move with data

`POST /lanes/{product}/{lane}/move` works as in [the move](the-move.md), with these additions.

A move takes these steps, each only when needed, reading the host again before each one:

1. Make the data set: its network, its volume, its database server with its set-up. Start a stopped server. Wait up to 60 s for the server to accept connections.
2. Make a login and start the new instance with it, on the data set of the instance receiving visitors, or on the lane's only data set, or on a new one.
3. Check the new instance at the product's health path, then point the web address at it.
4. Stop the old instance's background service, start the new instance's, and watch it stay up 3 s.
5. Retire the old instance.
6. Remove every login no container holds.

Only one background service of a lane runs at a time. Between the old one and the new one none runs: measured at 0.50 and 0.55 s.

A move stops with `503`, saying what it found and what it had changed, when:

- a data set has two networks, two volumes or two database servers carrying its labels;
- a data set's network is not quiet;
- a database server does not have Launchplane's access rules;
- a database server is restarting or paused, is attached to any network but its data set's, or has anything mounted but its data set's volume, once, where PostgreSQL keeps its data;
- the lane has two or more data sets and no instance receiving visitors;
- the new instance, after its check, is attached to a network it must not be on, has anything mounted, or runs against another data set than the instance receiving visitors.

A web part or a background service with anything mounted is replaced. That rule stands only until files are decided (README, open question 14). Until then a build whose image declares a volume of its own fails it.

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `background service failed` | 422 | Only where the move switched nothing: the lane already served the build, and its background service did not stay up 3 s. `error` has why and its last log lines. Asking again retires it and starts it again. |

Under a release that has switched, a background service that does not stay up ends nothing: the request stays `working`, its `error` has why and the last log lines, and it is taken up again by itself, ahead of any newer request for the lane ([the move](the-move.md)). The old instance stays until the new background service has stayed up. Known and not fixed: while the build's background service cannot stay up, nothing ends that request, so no newer move and no retire of the lane gets its turn.

The other outcomes are those of [the move](the-move.md). `data_set` in the answer is the data set of the instance receiving visitors. After an interruption the request is taken up again, reads the host and goes on from what is there.

## Retiring a lane with data

Retiring removes the route, then the background services, then the instances, then the logins no container holds. It keeps the lane's data sets and lists them in `kept_data_sets`. To delete one, name it:

```
curl -X POST http://launchplane.shiny:8700/lanes/<product>/<lane>/retire \
  -H 'Content-Type: application/json' -d '{"delete_data_set": "<data set>"}'
```

The data set is deleted once the lane has no background service and no instance left: its database servers, then its volumes, then its networks. The answer's `outcome` is `deleted` and its `deleted_data_set` names it. A name that is not one of this lane's data sets deletes nothing.

## The judgement on a lane

One judgement is made of a lane from one reading of it, what the product's description says, and which call, if any, is at work on it. `/running` answers it for every lane in `lanes`, the page draws it, and a move, a retire and the maintenance calls choose their next step from the same one. So what the page shows as wrong is what stops a call, in the same words.

| Field | Meaning |
| --- | --- |
| `condition` | `good`, `in maintenance`, `work under way`, `unfinished`, `wrong` or `could not be read`. The worst that applies: wrong, then could not be read, then work under way, then unfinished. |
| `work` | What the [request](requests.md) being worked on the lane is doing: `moving`, `retiring`, `beginning maintenance`, `ending maintenance`, or `null`. It is read from the record with the host. When the record cannot be read it is `null`, `a_move_is_at_work` is `null` too, the lane has a finding saying so, and what would be under way or left unfinished is shown as `could not be read`. `a_move_is_at_work` is whether it is not `null`. |
| `findings` | Everything found, each with `subject` (a container, a data set or the web address), `kind`, `sentence` and `shown`. |
| `parts` | For each container of the lane, its standing: `good`, `wrong`, `on purpose` or `could not be read`. It is the worst of what was found about that container. With nothing found it is good, or on purpose when it is stopped in a lane in maintenance. |
| `in_maintenance` | The lane's route points at the back soon page: see [a lane in maintenance](maintenance.md). |
| `in_maintenance_since` | When that route was written, or `null`. |
| `problems`, `release_under_way`, `left_unfinished` | The sentences of the findings shown as `wrong` or `could not be read`, as `under way`, and as `left unfinished`. |

A finding is `shown` one of four ways:

| `shown` | When |
| --- | --- |
| `wrong` | Never acceptable, whatever a call is doing: a part attached or mounted where it must not be, a database server in maintenance that did not stop cleanly, the back soon page not running for a lane in maintenance. Or wrong with no call at work: a part that keeps restarting, a data set with a part twice or a network that is not quiet, a part not stopped in a lane in maintenance, a route the proxy reports as not enabled, a web address that does not answer with success. |
| `under way` | A call is at work on the lane, and the finding is one of those wrong only with no call at work, or one a call passes through. |
| `left unfinished` | No call is at work, and the finding is a state a call passes through: no instance receives visitors, a routes file the proxy has not loaded yet, an instance beside the active one, a stopped or missing part, a background service not yet up 3 s, a login no container holds, the proxy not running. Asking again finishes it. |
| `could not be read` | Something asked has no answer: Launchplane's record of requests, the product's description, a server's logins, a server not asked whether it accepts connections, a network a part is attached to that was not among the networks read. Never shown as under way. |

Whether the web address answers is asked by the status call and by a move, and is a finding about the instance the web address is sent to. So a move to the build a lane already serves answers `503`, in the status call's words, while that web address does not answer with success. It starts no other instance for it. A retire and the maintenance calls do not ask.

While a call is ending maintenance the lane is judged as it must be once it is back: a part still stopped is under way, and a running one is good.

Every background service is judged for itself. Where a lane has more than the one it must have, each of them is wrong. One whose instance is not on the host is wrong, and one whose instance does not receive visitors says so.

After Launchplane is killed mid-release and starts again, its [request](requests.md) still stands as working, so the lane shows as work under way, and the worker takes the request up and finishes the release with nobody asking. A lane shows as left unfinished when a request failed halfway or was given up: then asking for the build of the instance beside the active one finishes the release, and asking for the active build removes what was left. A lane retired with its data set kept shows as left unfinished too.

A move and the end of maintenance answer for the instance they serve from and that instance's data set. A fault in another data set of the lane is in the judgement and on the page, and does not stop them.

How the page lays a lane out is in [the page](what-is-running.md#the-page).

A web part, a background service and a database server are each made with the restart policy `unless-stopped`: see [what stays stopped](maintenance.md#what-stays-stopped).

## Known and not fixed

- **With a lane's route removed by hand**, no instance is the one the lane serves from. A background service is then shown as belonging to an instance that receives no visitors, and the judgement does not say whether it is of the right build or has stayed up.
- **Two running background services, a background service whose instance is gone, and one of another build** show as under way while a call is at work.
- **A release that finishes between two readings of the move flag** can show for one reading as left unfinished.
- **A database server keeps the password it was first started with in its environment.** The set-up takes it off `postgres`, so it opens nothing. If the set-up did not finish it is still the password of `postgres`, which the rules then left in place refuse from every address but the server's own container. Launchplane cleans it from its answers like any secret.
- **The scrambled form of a new password can be read in Docker's events on the container host,** because it is part of the command Launchplane runs in the database server. It does not open the database.
- **Access rules changed by hand on a running server, with the file then put back and the server not told to load it, pass the read-back,** because the read-back reads the file and not what the server has loaded.
- **After a start that fails, a login no container holds can stay in the database until the next start on that data set.** It happens where the server's logins cannot be read when the failed move ends: the move ends as `failed` all the same. Until the login goes the lane's judgement shows it.
- **A password handed to the product as a file, a login that expires, and an encrypted connection to the database are not built.**
- **Where a call decides from what it has itself done, because the host cannot show it:** which instance it started, whether a check passed, whether a check or a watch on a background service refused, and whether it has already started a part that reads stopped again. A second call cannot know these, so it checks again and starts again.
- **A container made before the background service was named, marked `launchplane.part=job`, makes the host unreadable.** No version has been released, so nothing is converted: the test bed's lanes are made again.
- **Rules no test catches:** reading the host again when Docker refuses a question about a container it has just listed, leaving out a superuser when the logins are listed, and finishing a delete whose parts have already gone. Comes back when one of them breaks without a test noticing.

## On the test bed

`tests/test_bed_data_set.py` builds the pretend product in `tests/pretend_product` on the scratch container as `launchplane-test-ledger:a` and `:b`: a small Python web app with PostgreSQL whose background service writes a beat to the database every 0.2 s and calls a listener every second.

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_data_set.py -v
```

The tests use the product `test-bed-ledger`, lanes `testing` and `other`, and remove what they made, data sets included. The pretend product is built on `python:3.15-slim` with Debian's `libpq5`; on `python:3.15-alpine` psycopg could not find libpq.

## Measured

On the test bed, Docker 29.9.3 on the scratch container, November 15:

- Docker accepts the isolated gateway mode. `launchplane-lanes` and each data set network read back as internal with no gateway address.
- From an instance, TCP to 1.1.1.1:80, to the control container's port 8700, to the scratch container's Docker port 2376 and to a listener's published port all failed. From a plain container on Docker's default bridge all four succeeded. The background service's calls reached the listener only after `docker network connect bridge` gave the background service a way out.
- From an instance, `database:5432` answered; another data set's database was refused by its address and not found by its name.
- Bringing a lane up with a fresh data set: 12.1 to 12.6 s. Asking again: 0.12 to 0.15 s.
- A release that keeps the data set: 10.4 to 10.8 s, with no request failing.
- Retiring the lane: 0.97 to 1.43 s. Deleting its data set: 0.38 s.
- Launchplane killed at four points of a bring-up and four of a release, then asked again: each reached one instance of the build with its background service running on the one data set, rows kept, and no two background services running at once.
- `/running` with one plain lane on the host: 0.06 to 0.07 s. With a database lane too: 0.12 to 0.13 s. With the scratch container unreachable, a call answers `503` after 5 s.
- With a login per instance, November 16, one run each, before logins were read on every reading of the host: a lane brought up with a fresh data set 13.3 s, on a kept data set 10.3 s, asking again 0.11 s, a release that keeps the data set 11.3 s, retiring the lane 1.05 s, deleting its data set 0.39 s, and `/running` with a database lane 0.14 to 0.15 s.
- `/running` with one database lane, its logins read each time: 0.18 s.
- Making a login takes 44 to 50 ms and removing one 39 to 45 ms. A stored password looks like `SCRAM-SHA-256$4096:<salt>$<StoredKey>:<ServerKey>`.
- From a container on the data set's network: the instance's login with no password was answered `no password supplied`, with a wrong password `password authentication failed`, and `postgres`, the instance's login on the `postgres` database, and a retired instance's login and password `pg_hba.conf rejects connection`. From a container outside that network the right login and password timed out.
- Two instances at once each altered a table the other had made and wrote to it together. Every table, sequence and index, and the database, belonged to `app`.
- Neither the instance's password nor the one the server was first started with was in any file of the data set's volume.
- A background service that printed its password and exited under a switched release left its request `working` with that line cleaned in `error`, and a build whose health answer carried its password was answered `check failed` with it cleaned. No password appeared in any answer or in `journalctl --unit launchplane`.
