# What is running

`GET /running` answers which marked lane instances exist on the container host and which web addresses the proxy sends where, read from the host at the moment of asking. Nothing is stored between calls. `GET /` is the same answer drawn for a person.

## How a running instance says what it is

A container marks itself with three labels, set when it is created:

| Label | Value |
| --- | --- |
| `launchplane.product` | the product's name |
| `launchplane.lane` | `production`, `testing`, or the preview's name |
| `launchplane.build` | the build it runs |

Only containers carrying `launchplane.product` are listed. Any other container on the host is not Launchplane's and is not shown. Labels cannot change after a container is created, so an instance cannot drift from what it says it is without being replaced.

## The answer

Each instance has `product`, `lane`, `build`, `container`, `state` and `receives_visitors_for`. A missing lane or build label is `null`. `container` is the container's own name on the host. `receives_visitors_for` is the web address whose visitors the proxy sends to this instance, read from the proxy's loaded routes, or `null` (see [the move](the-move.md)). `state` is Launchplane's word, not the host's:

| `state` | Docker's states |
| --- | --- |
| `running` | `running` |
| `paused` | `paused` |
| `restarting` | `restarting` |
| `stopped` | `created`, `exited`, `dead`, `removing` |

Each web address has `web_address`, `product`, `lane`, `sends_to_container`, `answers` and `error`. The web addresses are every one the proxy routes and the one of every lane that has an instance on the host, so a route to an instance that is gone is shown, not dropped. `product` and `lane` come from an instance of that lane, or from the instance the route sends to, and are `null` when neither is on the host. `sends_to_container` is the container the proxy's loaded route names, or `null`. `answers` is whether a request for `/` at that web address, made inside the proxy, succeeded just now, the same request real visitors make; `error` is why not, for example `wget: server returned error: HTTP/1.1 502 Bad Gateway` for a route to a stopped instance, or `The proxy sends … to no instance.` A web address the proxy routes nowhere is not requested.

- `200` with `instances` and `web_addresses`: the host was read. An empty list means the host answered and nothing marked is on it.
- `503` with `error`: the host or the proxy could not be read, or an answer did not have the expected shape. The error carries the real cause. It never answers "nothing is running".

Both carry `read_at`, the moment the host was asked.

## The page

`GET /` is one HTML file, `src/launchplane/running.html`, with its style and script inline. It loads nothing else, needs no build step and no framework. FastAPI serves it as it is and it reads no host itself, so it opens even when the host cannot be read.

- The script asks `/running` once a second, each time after the last answer arrived, so a slow host never piles up requests. It gives up on one answer after 15 s.
- Each lane reads like this:

  ```
  test-bed-site · testing
  Web address   testing.test-bed-site.test     answering

    Instance 1   running   visitors are sent here
    Build        nginx:1.30.6-alpine
  ```

  A web address that does not answer shows `not answering` and the real error under it. One whose instance is running but does not answer with success says so, and does not say nothing is there. One the proxy sends nowhere says `The proxy sends … to no instance.`, and one with no instance of its lane on the host is titled `A web address with no instance on the host`. The instance visitors are sent to is listed first. Every other instance of the lane shows its state and `no visitors are sent here`: the page sees only what the host and the proxy report, not whether an instance is being checked or stopping. A route to another lane's instance names that lane and the container, and a route to a container that is not on the host says so. Instances with no lane label are listed apart, each with its container's name.
- When Launchplane answers `503`, or cannot be reached, the page shows the error and when it last read the host, and hides the lanes. It never shows an empty list for an answer it did not get. The time of the last good read is kept only by the open page, not by Launchplane.
- Text is built with DOM text nodes, never as HTML, so a label cannot inject markup.

Measured on the test bed from the control container: listing the instances takes about 1 ms, reading the proxy's routes about 30 ms, and asking one web address through the proxy about 30 ms. A whole `/running` with one lane and two dangling routes answered the Mac in 0.14 to 0.16 s, against 0.03 s before web addresses were asked. During a move from one nginx build to another, a poll every 0.2 s saw the second instance at 0.5 s, the web address pass to it at 5.8 s, and the old instance gone when the move answered, so a poll a second shows each stage.

`/running` asks every routed web address at the same moment, one thread each, so web addresses that hang cost one request's time limit of 2 s, not 2 s each. Measured with web addresses routed to a container address that never answers: 8 such web addresses took 16.3 s when asked in turn, longer than the page's 15 s limit, and 2.1 s asked at once; 16 took 2.2 s. The Docker client keeps 10 connections; beyond that each extra request opens its own and closes it.

### Known and not fixed

- **Like `/running`, the page reads containers and routes at two moments**, so during a switch it can show the retired instance and no instance receiving visitors for one read.
- **No test loads the page or runs its script.** A blank page, polling that stops, old lanes kept after an error, or colours swapped would each pass every test. Chris watching the page is the check for now. Comes back when the page breaks without anyone noticing, or when there is a second page.

## How Launchplane reaches the host

Only `src/launchplane/container_host.py` knows the host is Docker. It uses the Docker SDK for Python over TCP with mutual TLS, and checks Docker's answer with Pydantic before reading it.

Settings: `LAUNCHPLANE_CONTAINER_HOST` (`tcp://docker-lp-scratch.shiny:2376`) and `LAUNCHPLANE_CONTAINER_HOST_CREDENTIALS`, the folder holding `ca.pem`, `cert.pem` and `key.pem`.

Measured on the test bed with SDK 7.2.0, the control container's traffic to the host dropped by its firewall:

| Transport | Unreachable host | Streaming a command's output |
| --- | --- | --- |
| TLS, `timeout=5` | 5.0 s, `Connection to docker-lp-scratch.shiny timed out. (connect timeout=5)` | line by line, and survives 8 s of silence |
| TLS, Docker stopped | at once, `[Errno 111] Connection refused` | |
| SSH through the `ssh` program | 5.0 s, but the error is `Connection aborted ... Broken pipe`. The SDK does not capture ssh's error output, so the real error only reaches the log. | line by line |
| SSH through paramiko | no answer within 120 s. The SDK gives paramiko no connect timeout. | line by line |
| Docker CLI with `--host ssh://` | 92 s. It forces `-o ConnectTimeout=30` and tries three times. | |

The low-level `containers()` call is used because the high-level list turns Docker's items into objects before they can be checked, and raises a bare `Exception` when the answer is not a list. Its typeshed stub leaves `filters` and the result unannotated, so that one line silences Pyright's two reports of an unknown type.

## Certificates

`./scripts/issue-certificates`, run from a machine with `lp-control` and `lp-scratch` (or `LAUNCHPLANE_CONTROL_SHELL` and `LAUNCHPLANE_SCRATCH_SHELL`), issues what is missing for this test bed and keeps what exists. A second run changes nothing.

- The certificate authority is in `/root/launchplane-ca` on the control container, readable only by root. The `launchplane` user cannot read its key.
- Every key is a P-256 EC key made on the host that uses it. Only certificate requests and certificates travel.
- The scratch container's server certificate and key are in `/etc/docker/tls`, named for `docker-lp-scratch.shiny` and the address it had when issued. The client certificate (`CN=launchplane`) and key are in `/var/lib/launchplane/tls` on the control container. The service reads them on each connection, so a new client certificate needs no restart.
- Certificates are valid for 3650 days. The authority and server certificate were issued on 2026-11-13 and expire on 2036-11-10.
- To reissue one, delete its `cert.pem`, or its `key.pem` too for a new key, and run the command again. Docker keeps using the old server certificate until it restarts.
- The command restarts Docker on the scratch container only when it changed Docker's setting, which stops the containers there.

Docker on the scratch container listens on `192.168.1.56:2376` only, through the systemd override `/etc/systemd/system/docker.service.d/launchplane-tls.conf`, and refuses any caller without a certificate from the authority. That address comes from DHCP. If it changes, Docker cannot bind it and does not start.

## Where it runs for now

On the control container as the systemd service `launchplane`, user `launchplane`, port 8700: the page at `http://launchplane.shiny:8700/` and the call at `http://launchplane.shiny:8700/running`. `./scripts/deploy-control` ships the committed tree, installs it with uv on Python 3.15 and restarts the service. It is unauthenticated, answers only on the home network, and manages only the scratch container.

`tests/test_bed_what_is_running.py` runs against it from a machine that can reach the test bed: `LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_what_is_running.py`. It places instances of the product `test-bed-placed`, which no route names, on the scratch container with `lp-scratch` (or `LAUNCHPLANE_SCRATCH_SHELL`). For the unreachable case it drops the control container's traffic to port 2376 on the scratch container's firewall for one call.
