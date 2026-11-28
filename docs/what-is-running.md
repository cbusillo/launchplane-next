# What is running

`GET /running` answers what is on the container host, where the proxy sends each web address, and what Launchplane makes of each lane, read at the moment of asking. Nothing is stored between calls. `GET /` is the same answer drawn for a person.

Every call reads the host the same way and judges a lane the same way, so the status call, the page and a call that would act on a lane say the same of it: see [one reading](#one-reading) and [the judgement on a lane](data-sets.md#the-judgement-on-a-lane).

## Labels

Only containers carrying `launchplane.product` are Launchplane's. Any other container on the host is not shown. Labels are set when a container is made and cannot change.

| Label | Value |
| --- | --- |
| `launchplane.product` | the product's name |
| `launchplane.lane` | `production`, `testing`, or the preview's name |
| `launchplane.build` | the build it runs |
| `launchplane.part` | `web`, `update`, `first-start`, `background-service` or `database`; a container without it is `web`. `update` is [the build run once to update a copy](odoo.md#a-release-that-updates), and `first-start` [the build run once on a data set before its first web start](odoo.md#the-first-start), which is kept once it has ended |
| `launchplane.data-set` | the data set it runs against or belongs to |
| `launchplane.instance` | on a background service, the container of its instance's web part |
| `launchplane.generation` | on a web part, a background service and a data set's network, the generation of the [release](the-move.md#what-it-does) that made it. One that is not a generation makes that part wrong, and nothing else. |
| `launchplane.secret-variables` | on a web part, background service or database server given a secret, the environment variables that hold one, separated by commas |

Data set networks and volumes carry the product, lane and data set labels too.

## The answer

`200` means the host was read. An empty list means nothing marked is on it.

| Field | Each entry has |
| --- | --- |
| `instances` | `product`, `lane`, `build`, `container`, `state`, `networks`, `mounts`, `data_set`, `generation`, `other_image`, `is_update`, `receives_visitors_for`. An instance is its web part, or with `is_update` the build run once to update a copy, which is no web part: it is on its data set's network alone and never receives visitors. `other_image` is `null` unless [it runs another image than its build's digest](odoo.md#a-build-is-a-publishing-run). A missing lane or build label is `null`. |
| `background_services` | `product`, `lane`, `build`, `container`, `state`, `networks`, `mounts`, `data_set`, `instance`, and `running_for_seconds` by Docker's clock, `null` unless it is running and not restarting or paused. |
| `data_sets` | `product`, `lane`, `data_set`, `networks` (each `name` and `is_quiet`), `volumes` (names, of the database), `files` (names, of [the files volume](odoo.md#the-data-set)) and `databases` (each `container`, `state`, `networks`, `mounts`, `accepts_connections`, `exit_code`, `image`, `unheld_logins`), and `first_starts` (each `container` and `seeded`: see [the first start](odoo.md#the-first-start)). An empty list is a missing part and two are two. For [a copy](copying.md#a-copy-on-the-host) also `copy_of`, `generation` and `copies`. |
| `networks` | every network on the host, with `name` and `is_quiet`. |
| `web_addresses` | `web_address`, `product`, `lane`, `sends_to_container`, `answers`, `shows_back_soon_page`, `error`, and what the page shows for it: `says` and `standing`. |
| `lanes` | the judgement on each lane: see [the judgement on a lane](data-sets.md#the-judgement-on-a-lane). |
| `requests` | every [request](requests.md) that has not ended, as `GET /requests` gives it. |
| `cancels` | every request that ended in the last 10 minutes and had a cancel asked of it, however it ended, or was replaced by a newer request. |
| `record_error` | why Launchplane's record could not be read or did not answer in time, or `null`. The rest of the answer is still what is on the host. |
| `cards` | the page, ready to draw: see [the page](#the-page). |
| `read_at` | the moment the host was asked. |

- `mounts` lists everything a container has mounted, each with `source` (a volume's name, or a folder of the host) and `path`.
- `receives_visitors_for` is the web address whose visitors the proxy sends to the instance, or `null`.
- `accepts_connections` is asked at that moment of every running database server, and is `null` for one that is not running.
- `unheld_logins` lists the [logins](data-sets.md#logins) on a server that accepts connections which no container on the host holds. It is `{"asked": "…", "why": "…"}` when the server was asked and could not tell, and `null` for a server that does not accept connections, which was not asked. An empty list is a server that was read and has none.
- `exit_code` is the code a stopped database server exited with, and `null` while it is not stopped. `0` is a clean stop. `image` is the image the server runs.
- `web_addresses` are every one the proxy routes, and the one of every lane on the host, so a route to an instance that is gone is shown. `answers` is whether a request for `/` at that web address, made inside the proxy, succeeded just now, and `error` is why not. The lane's judgement has the same sentence, about the instance the web address is sent to. A web address the proxy routes nowhere is not requested.
- A web address of a lane in [maintenance](maintenance.md) has `sends_to_container` `launchplane-back-soon` and `answers` `false`. `shows_back_soon_page` is `true` while the back soon page's container runs.
- While the proxy is not running every web address has `sends_to_container` `null`, and `error` says the proxy is not running. Each lane's judgement agrees: no instance receives visitors, because the proxy is not running. A lane in maintenance still reads as in maintenance, from its routes file, and its judgement says the proxy is not running too.
- `state` is Launchplane's word: `running`, `paused`, `restarting`, or `stopped` for Docker's `created`, `exited`, `dead` and `removing`.

`503` with `error` means the host or the proxy could not be read, an answer did not have the expected shape, or Docker refused a question about a container it had just listed during three reads in a row. The error carries the real cause. It never answers "nothing is running".

## One reading

Every call gets what it knows of the host from one pass, made once for each step it takes, and takes only what it needs of it. The status call makes one pass for its answer. Before every answer but the page, the containers are also listed and described once more for the secrets they hold: see below.

| What is read | Who asks for it |
| --- | --- |
| The routes files, the routes the proxy has loaded, and whether the proxy and the back soon page run. | every call |
| The marked containers, each described once, the marked volumes and every network. The secrets and the database login each container holds come from that same list. | a move, a retire and a maintenance call for their own lane only; the status call for every lane. Ending maintenance on a lane that is not in maintenance reads no part of the lane. |
| Whether each running database server that was listed accepts connections, and its logins. | the same calls, for the same lanes |
| Whether each web address the proxy sends to a product answers. | the status call for every one, and a move for its own lane's |

A call for one lane lists only that lane's containers, so a container of another lane that Launchplane cannot read does not stop it. The status call reads every lane and answers `503` over it.

No answer and no log line repeats a secret a container holds: see [no secret leaves Launchplane](data-sets.md#no-secret-leaves-launchplane). A `503` without `read_at` means Launchplane could not read the secrets and made no answer.

Anything asked of the host has one of four answers: what is there, nothing there, could not be read, or conflicting. The four stay apart from the reading to what a call decides. What could not be read is never taken for no, for nothing or for fine: it is shown as `could not be read` with what was asked and the reason, on the lane, on the part and on the page. Two things where there must be one are shown as `conflicting`, each of them named, and neither is chosen: see [two routers](the-move.md#two-routers-for-one-web-address). What a call does without an answer depends on whether it needs it: see [logins](data-sets.md#logins).

## The page

`GET /` is one HTML file with its style and script inline. It loads nothing else and reads no host itself, so it opens even when the host cannot be read.

- It asks `/running` once a second, each time after the last answer arrived, and gives up on one answer after 15 s.
- It draws `cards` as given and decides nothing: every word and every colour is in the answer. A card has a `title`, `pills`, `in_maintenance_since` and `rows`. A row has a `name`, a `value` in pieces drawn as text, as code or as a build, `pills`, and a `note` shown small under it. A pill has `text` and a `tone`: `good` is green, `wrong` and `conflicting` red, `on purpose` blue and `could not be read` amber.
- A lane's card has its condition as a pill, and `in maintenance` beside it when the lane is in maintenance and its condition is something else. Then its web address, then what the judgement found, one row each, then its background service, its instances with the active one first, and its data sets. A data set's row has the image of its database server, written as a build is, with that server's state, and the server's container name in the small line. While an instance of the lane receives visitors, the small line also says in one sentence [why the data set is kept](copying.md#old-copies-go), or that the next release that switches deletes it.
- The colour of a part's state is its standing in the judgement, so a running part with something wrong about it is red.
- A lane's requests that have not ended are rows on its card, and a record that cannot be read is a card of its own: see [requests on the page](requests.md#on-the-page).
- A container or data set marked for Launchplane with no lane label is shown in a card of its own, as wrong.
- A build named by digest is shown as its image name and the first 12 characters of the digest. The full value is the tooltip.
- When Launchplane answers `503` or cannot be reached, the page shows the error. The last reading stays below it, faded, under `Not current: below is what was read at <time>.` Once a read succeeds again, one line with the last failure stays until dismissed.

## Known and not fixed

- **The page reads containers and routes at two moments**, so during a switch it can show the retired instance and no instance receiving visitors for one read.
- **The page does not show where a route to another lane's instance goes.** `/running` answers it.
- **No test runs the page's script.** A unit test covers the cards it is given. Chris watching the page is the check on the drawing. Comes back when the page breaks without anyone noticing, or when there is a second page.
- **While a start is between making a login and making its container, a reading can find that login held by no container.** A call is at work then, so the page shows it as under way. The removal of logins reads the server first and the containers after, so it never takes that login.
- **A container that Docker keeps refusing to describe stops every call.** Every answer is cleaned of the secrets containers hold, so each call first describes every container, and answers `503` when one cannot be described.

## Measured

- Listing the instances takes about 1 ms, reading the proxy's routes about 30 ms, and asking one web address through the proxy about 30 ms.
- `/running` asks every routed web address at once, so ones that hang cost one 2 s limit, not 2 s each: 8 hanging web addresses took 16.3 s asked in turn and 2.1 s asked at once.
- During a move, a poll every 0.2 s saw the second instance at 0.5 s and the web address pass to it at 5.8 s, so a poll a second shows each stage.
- Reaching Docker over TLS with a 5 s limit: an unreachable host answers in 5.0 s with `Connection to docker-lp-scratch.shiny timed out`, and a stopped Docker at once with `Connection refused`. Over SSH the real error was lost or no answer came within 120 s, so TLS is used.

## How Launchplane reaches the host

The Docker SDK for Python over TCP with mutual TLS. Settings: `LAUNCHPLANE_CONTAINER_HOST` (`tcp://docker-lp-scratch.shiny:2376`) and `LAUNCHPLANE_CONTAINER_HOST_CREDENTIALS`, the folder holding `ca.pem`, `cert.pem` and `key.pem`. The service reads the first from `/etc/launchplane/bed.env` on the control container, which a deploy does not replace.

`./scripts/issue-certificates`, run from a machine with `lp-control` and `lp-scratch`, issues what is missing for the [chosen test bed](#choosing-the-test-bed) and keeps what exists.

- The certificate authority is in `/root/launchplane-ca` on the control container, readable only by root.
- Every key is made on the host that uses it. Only certificate requests and certificates travel.
- The scratch container's server certificate and key are in `/etc/docker/tls`. The client certificate and key are in `/var/lib/launchplane/tls` on the control container, read on each connection.
- Certificates are valid for 3650 days. The authority and server certificate expire on 2036-11-10.
- To reissue one, delete its `cert.pem`, or its `key.pem` too for a new key, and run the command again. Docker keeps the old server certificate until it restarts.
- The command restarts Docker on the scratch container only when it changed Docker's setting, which stops the containers there.

Docker on the scratch container listens on `192.168.1.56:2376` only and refuses any caller without a certificate from the authority. That address comes from DHCP. If it changes, Docker cannot bind it and does not start.

## Where it runs for now

On the control container as the systemd service `launchplane`, user `launchplane`, port 8700: `http://launchplane.shiny:8700/`. `./scripts/deploy-control` ships the committed tree, installs it with uv on Python 3.15, sets up [its database](requests.md#the-database) and restarts the service. It is unauthenticated, answers only on the home network, and manages only the scratch container.

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_what_is_running.py
```

### Choosing the test bed

A test bed is one control container and one scratch container. Nothing in the repository says which pair: these settings do, and what each is when not set is the first bed.

| Setting | Read by | Not set |
| --- | --- | --- |
| `LAUNCHPLANE_URL` | the tests, which also take the control container's address and port from it | the test-bed tests are skipped |
| `LAUNCHPLANE_CONTROL_SHELL` | the tests, `./scripts/deploy-control`, `./scripts/issue-certificates` | `lp-control` |
| `LAUNCHPLANE_SCRATCH_SHELL` | the tests, `./scripts/issue-certificates` | `lp-scratch` |
| `LAUNCHPLANE_SCRATCH_ADDRESS` | the tests, `./scripts/issue-certificates` | `docker-lp-scratch.shiny` |
| `LAUNCHPLANE_HOLDS_URL` | the tests that hold Launchplane | port 8701 at the address of `LAUNCHPLANE_URL` |
| `/etc/launchplane/bed.env` on the control container | the services `launchplane` and `launchplane-held` | a deploy stops and says what to write |

`/etc/launchplane/bed.env` is written once by hand and holds one line, `LAUNCHPLANE_CONTAINER_HOST=tcp://<scratch address>:2376`. A second bed has not been made. `tests/test_bed_interruptions.py` builds every image it uses, so it runs by itself on a bed where no other test has run.
