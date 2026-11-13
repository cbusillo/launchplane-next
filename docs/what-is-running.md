# What is running

`GET /running` answers which marked copies exist on the container host, read from the host at the moment of asking. Nothing is stored between calls.

## How a running copy says what it is

A container marks itself with three labels, set when it is created:

| Label | Value |
| --- | --- |
| `launchplane.product` | the product's name |
| `launchplane.lane` | `production`, `testing`, or the preview's name |
| `launchplane.build` | the build it runs |

Only containers carrying `launchplane.product` are listed. Any other container on the host is not Launchplane's and is not shown. Labels cannot change after a container is created, so a copy cannot drift from what it says it is without being replaced.

## The answer

Each copy has `product`, `lane`, `build`, `name`, `state` and `serves`. A missing lane or build label is `null`. `serves` is the name the proxy sends to this copy, read from the proxy's loaded routes, or `null` (see [the move](the-move.md)). `state` is Launchplane's word, not the host's:

| `state` | Docker's states |
| --- | --- |
| `running` | `running` |
| `paused` | `paused` |
| `restarting` | `restarting` |
| `stopped` | `created`, `exited`, `dead`, `removing` |

- `200` with `copies`: the host was read. An empty list means the host answered and nothing marked is on it.
- `503` with `error`: the host or the proxy could not be read, or an answer did not have the expected shape. The error carries the real cause. It never answers "nothing is running".

Both carry `read_at`, the moment the host was asked.

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

On the control container as the systemd service `launchplane`, user `launchplane`, port 8700: `http://launchplane.shiny:8700/running`. `./scripts/deploy-control` ships the committed tree, installs it with uv on Python 3.15 and restarts the service. It is unauthenticated, answers only on the home network, and manages only the scratch container.

`tests/test_bed_what_is_running.py` runs against it from a machine that can reach the test bed: `LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_what_is_running.py`. It places containers on the scratch container with `lp-scratch` (or `LAUNCHPLANE_SCRATCH_SHELL`). For the unreachable case it drops the control container's traffic to port 2376 on the scratch container's firewall for one call.
