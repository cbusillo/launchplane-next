# What is running

`GET /running` answers what is on the container host and where the proxy sends each web address, read at the moment of asking. Nothing is stored between calls. `GET /` is the same answer drawn for a person.

## Labels

Only containers carrying `launchplane.product` are Launchplane's. Any other container on the host is not shown. Labels are set when a container is made and cannot change.

| Label | Value |
| --- | --- |
| `launchplane.product` | the product's name |
| `launchplane.lane` | `production`, `testing`, or the preview's name |
| `launchplane.build` | the build it runs |
| `launchplane.part` | `web`, `background-service` or `database`; a container without it is `web` |
| `launchplane.data-set` | the data set it runs against or belongs to |
| `launchplane.instance` | on a background service, the container of its instance's web part |
| `launchplane.secret-variables` | on a web part or background service given a secret, the environment variables that hold one, separated by commas |
| `launchplane.password-file` | on a database server, where on its volume the data set's password is kept |

Data set networks and volumes carry the product, lane and data set labels too.

## The answer

`200` means the host was read. An empty list means nothing marked is on it.

| Field | Each entry has |
| --- | --- |
| `instances` | `product`, `lane`, `build`, `container`, `state`, `networks`, `mounts`, `data_set`, `receives_visitors_for`. An instance is its web part. A missing lane or build label is `null`. |
| `background_services` | `product`, `lane`, `build`, `container`, `state`, `networks`, `mounts`, `data_set`, `instance`, and `running_for_seconds` by Docker's clock, `null` unless it is running and not restarting or paused. |
| `data_sets` | `product`, `lane`, `data_set`, `networks` (each `name` and `is_quiet`), `volumes` (names) and `databases` (each `container`, `state`, `networks`, `mounts`, `accepts_connections`, `exit_code`, `image`). An empty list is a missing part and two are two. |
| `networks` | every network on the host, with `name` and `is_quiet`. |
| `web_addresses` | `web_address`, `product`, `lane`, `sends_to_container`, `answers`, `shows_back_soon_page`, `error`. |
| `lanes` | the verdict on each lane: see [the verdict on a lane](data-sets.md#the-verdict-on-a-lane). |
| `read_at` | the moment the host was asked. |

- `mounts` lists everything a container has mounted, each with `source` (a volume's name, or a folder of the host) and `path`.
- `receives_visitors_for` is the web address whose visitors the proxy sends to the instance, or `null`.
- `accepts_connections` is asked at that moment of a running server whose product's description names a database, and is `null` otherwise.
- `exit_code` is the code a stopped database server exited with, and `null` while it is not stopped. `0` is a clean stop. `image` is the image the server runs.
- `web_addresses` are every one the proxy routes, and the one of every lane on the host, so a route to an instance that is gone is shown. `answers` is whether a request for `/` at that web address, made inside the proxy, succeeded just now, and `error` is why not. A web address the proxy routes nowhere is not requested.
- A web address of a lane in [maintenance](maintenance.md) has `sends_to_container` `launchplane-back-soon` and `answers` `false`. `shows_back_soon_page` is `true` while the back soon page's container runs.
- While the proxy is not running every web address has `sends_to_container` `null`, and `error` says the proxy is not running. The lanes' verdicts are read as before, from the route files and the parts.
- `state` is Launchplane's word: `running`, `paused`, `restarting`, or `stopped` for Docker's `created`, `exited`, `dead` and `removing`.

`503` with `error` means the host or the proxy could not be read, an answer did not have the expected shape, or Docker refused a question about a container it had just listed during three reads in a row. The error carries the real cause. It never answers "nothing is running". A `503` without `read_at` means Launchplane could not read which secrets it has given out and made no answer (see [no secret leaves Launchplane](data-sets.md#no-secret-leaves-launchplane)).

## The page

`GET /` is one HTML file with its style and script inline. It loads nothing else and reads no host itself, so it opens even when the host cannot be read.

- It asks `/running` once a second, each time after the last answer arrived, and gives up on one answer after 15 s.
- Every row is a name, then a value, then its status pills at the end. A pill is green for good, red for wrong and blue for under way on purpose. A build stands beside the thing it belongs to, and a container's name is a small grey line under its row.
- A lane's card has its web address, then what the verdict found, one row each, then its background service, its instances with the active one first, and its data sets. A data set's row has the image of its database server, written as a build is, with that server's state, and the server's container name in the grey line.
- A lane in maintenance has one blue pill, `in maintenance`, with since when under it. A lane Launchplane is at work on has the pill `Launchplane is at work`.
- A container or data set marked for Launchplane with no lane label is shown in a card of its own, as wrong.
- A build named by digest is shown as its image name and the first 12 characters of the digest. The full value is the tooltip.
- When Launchplane answers `503` or cannot be reached, the page shows the error. The last reading stays below it, faded, under `Not current: below is what was read at <time>.` Once a read succeeds again, one line with the last failure stays until dismissed.

## Known and not fixed

- **A refusal from Docker inside the read of the secrets that comes from the Docker client's own follow-up question is not read again.** It answers `503`, and asking again works.
- **The page reads containers and routes at two moments**, so during a switch it can show the retired instance and no instance receiving visitors for one read.
- **The page does not show where a route to another lane's instance goes.** `/running` answers it.
- **No test loads the page or runs its script.** Chris watching the page is the check. Comes back when the page breaks without anyone noticing, or when there is a second page.

## Measured

- Listing the instances takes about 1 ms, reading the proxy's routes about 30 ms, and asking one web address through the proxy about 30 ms.
- `/running` asks every routed web address at once, so ones that hang cost one 2 s limit, not 2 s each: 8 hanging web addresses took 16.3 s asked in turn and 2.1 s asked at once.
- During a move, a poll every 0.2 s saw the second instance at 0.5 s and the web address pass to it at 5.8 s, so a poll a second shows each stage.
- Reaching Docker over TLS with a 5 s limit: an unreachable host answers in 5.0 s with `Connection to docker-lp-scratch.shiny timed out`, and a stopped Docker at once with `Connection refused`. Over SSH the real error was lost or no answer came within 120 s, so TLS is used.

## How Launchplane reaches the host

The Docker SDK for Python over TCP with mutual TLS. Settings: `LAUNCHPLANE_CONTAINER_HOST` (`tcp://docker-lp-scratch.shiny:2376`) and `LAUNCHPLANE_CONTAINER_HOST_CREDENTIALS`, the folder holding `ca.pem`, `cert.pem` and `key.pem`.

`./scripts/issue-certificates`, run from a machine with `lp-control` and `lp-scratch`, issues what is missing for this test bed and keeps what exists.

- The certificate authority is in `/root/launchplane-ca` on the control container, readable only by root.
- Every key is made on the host that uses it. Only certificate requests and certificates travel.
- The scratch container's server certificate and key are in `/etc/docker/tls`. The client certificate and key are in `/var/lib/launchplane/tls` on the control container, read on each connection.
- Certificates are valid for 3650 days. The authority and server certificate expire on 2036-11-10.
- To reissue one, delete its `cert.pem`, or its `key.pem` too for a new key, and run the command again. Docker keeps the old server certificate until it restarts.
- The command restarts Docker on the scratch container only when it changed Docker's setting, which stops the containers there.

Docker on the scratch container listens on `192.168.1.56:2376` only and refuses any caller without a certificate from the authority. That address comes from DHCP. If it changes, Docker cannot bind it and does not start.

## Where it runs for now

On the control container as the systemd service `launchplane`, user `launchplane`, port 8700: `http://launchplane.shiny:8700/`. `./scripts/deploy-control` ships the committed tree, installs it with uv on Python 3.15 and restarts the service. It is unauthenticated, answers only on the home network, and manages only the scratch container.

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_what_is_running.py
```
