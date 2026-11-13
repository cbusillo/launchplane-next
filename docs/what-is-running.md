# What is running

`GET /running` answers which marked containers exist on the container host, read from Docker at the moment of asking. Nothing is stored between calls.

## How a running copy says what it is

A container marks itself with three Docker labels:

| Label | Value |
| --- | --- |
| `launchplane.product` | the product's name |
| `launchplane.lane` | `production`, `testing`, or the preview's name |
| `launchplane.build` | the build it runs |

Only containers carrying `launchplane.product` are listed. Any other container on the host is not Launchplane's and is not shown. A missing lane or build label is reported as an empty string.

Labels are fixed when a container is created, so a copy cannot drift from what it says it is without being replaced.

Each copy also reports Docker's `state` (`running`, `exited`, ...), its human `status`, its container name and the image it was started from.

## Answers

- `200` with `copies`: the host was read. An empty list means the host answered and nothing marked is on it.
- `503` with `error`: the host could not be read. The error carries ssh's or Docker's own message. It never answers "nothing is running".

Both carry `read_at`, the moment Docker was asked.

## How Launchplane reaches the host

Launchplane runs `ssh <host> docker system dial-stdio` itself and speaks Docker's HTTP API over it. On the scratch container the control container's key is forced to `docker system dial-stdio` and `restrict`, so it reaches the Docker API and cannot open a shell. Docker API access is still root on that host.

Measured on the test bed: Docker's own CLI with `--host ssh://...` passes `-o ConnectTimeout=30`, which overrides any ssh config, and tries three times, so an unreachable host took 92 seconds to report. Running ssh directly with `ConnectTimeout=5` reports `Connection timed out` in 5 seconds. A stopped Docker daemon answers in under a second with `dial unix /var/run/docker.sock: connect: connection refused`.

## Where it runs for now

On the control container as the systemd service `launchplane`, user `launchplane`, port 8700: `http://launchplane.shiny:8700/running`. `./scripts/deploy-control` ships the committed tree, installs it with uv on Python 3.15 and restarts the service. It is unauthenticated and answers only reads.

`tests/test_bed_what_is_running.py` runs against it from a machine that can reach the test bed: `LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_what_is_running.py`. It places containers on the scratch container with `lp-scratch` (or `LAUNCHPLANE_SCRATCH_SHELL`), and for the unreachable case drops the control container's ssh traffic on the scratch container's firewall for one call.
