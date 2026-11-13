# The move

`POST /lanes/{product}/{lane}/move` with `{"build": "<image>"}` moves one lane to one build. The lane answers at the name `{lane}.{product}.test`. Product and lane are lowercase letters, digits and hyphens.

For now a build is a container image reference, and the copy must answer HTTP on port 80.

## What it does

Every call reads the lane's copies from the container host and the proxy's loaded routes, then:

1. If the proxy already sends the name to a running copy of this build, it removes any other copy of the lane and stops. Nothing else changes.
2. Otherwise it removes every copy of the lane except the serving one and one of the target build. Those are left over from a move that was interrupted or abandoned.
3. It starts a copy of the build beside the serving one, unless one is already running.
4. It checks the copy (see below). If the check fails, it removes the copy and answers why. The name never moved.
5. It points the name at the new copy and waits until the proxy reports that route loaded.
6. It stops the old copy, giving it 10 s to finish what it is doing, and removes it.

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `switched` | 200 | The name now points at a copy of this build. |
| `unchanged` | 200 | It already did. Nothing changed. |
| `cleaned up` | 200 | It already did, and copies left by an interrupted move were removed. |
| `check failed` | 422 | Nothing switched. `error` has the check's error and the copy's last log lines. The copy is gone. |

A `503` means the host or proxy could not be read or changed. The move stopped where it was and is safe to ask again.

One move runs at a time in a Launchplane process. A second call waits for the first.

### The check

The proxy requests `http://<copy>:80/` on the lane network, the path real traffic takes. The copy passes once it has answered with a 2xx for 3 s without a failure. It fails if it has not passed after 20 s, with the last error, for example `wget: can't connect to remote host (172.16.0.3): Connection refused`. A copy that answers once and then crashes does not pass.

### The old copy

It is stopped and removed once the switch is loaded. There is no data, so keeping it gives nothing that the move back cannot: going back is the same move with the earlier build, and the image stays on the host so it starts at once. A kept copy would also be a second copy of the lane that every reader has to explain.

## Which copy is serving, after a crash too

The proxy's loaded routes are the only mark. Launchplane writes nothing else. A route sends one name to one copy by its container name, and the copy's labels say which product, lane and build it is. `GET /running` gives each copy a `serves` field: the name the proxy sends to it, or `null`.

To finish or abandon a half-done move, Launchplane needs no record of its own. The host shows every copy of the lane and the proxy shows the serving one, so a copy that exists and is not serving is left over. Asking for the same build finishes the move. Asking for the serving build removes the leftover. Either way the name pointed at exactly one copy the whole time.

Killed between starting the new copy and switching, then asked again, measured on the test bed: the leftover copy was checked and switched to, or removed, with no request failing.

## The proxy

Traefik v3.7.18 runs on the container host as `launchplane-proxy`, on port 80, on the Docker network `launchplane-lanes` with the copies. Only `src/launchplane/name_switch.py` knows it is Traefik.

- Routes are one file, `lanes.yml`, in the volume `launchplane-proxy-routes`. Traefik watches the folder. Launchplane writes the new file beside it as `lanes.next`, which Traefik ignores, then renames it over `lanes.yml`, so Traefik never reads half a file. The file is JSON, which YAML accepts.
- Traefik's API listens on `127.0.0.1:8080` inside its container only. Launchplane reads `/api/rawdata` and makes its requests by running `wget` inside the proxy container over the existing certificate connection to Docker. No new port is opened and no new key exists.
- Its restart policy is `always`. After `docker restart launchplane-proxy` it answered the lane's name again within 115 ms with the same route, read from the volume.
- A route Launchplane did not write is an error, and the proxy is left as it is.

### Why not Caddy

Caddy was tried first, as asked. Pointing a name at another copy is one request to its admin API and the answer reads back. But every change reloads the whole configuration, including its listeners, and that drops requests. With a loop from the Mac at about 40 requests a second:

| Proxy | Switches | Requests | Failed |
| --- | --- | --- | --- |
| Caddy 2.11.9 (`caddy reload --force` of the same routes) | 30 | 3407 | 3 (`Connection reset by peer`, `Remote end closed connection without response`) |
| Caddy 2.11.9, unpaced loop | 60 | 11470 | 14 |
| Traefik v3.7.18 (file renamed) | 30, each one changing the answer | 3426 | 0 |

The same Caddy reloads from a loop on the scratch container itself failed nothing in 40 reloads, so testing from another machine is what showed it.

Against the five points:

| | Caddy | Traefik |
| --- | --- | --- |
| 1. One name to one copy | yes | yes |
| 2. Switch with no request failing | no, as measured | yes, as measured |
| 3. Read which copy serves, after a crash | yes, admin API | yes, `/api/rawdata` |
| 4. Keeps working after a restart | yes, with `--resume` | yes, routes file on a volume |
| 5. Certificates and long-lived connections | built in; not yet tried | built in; not yet tried |

Traefik loads a change asynchronously: it waits up to 2 s to group file events. The switch is therefore confirmed by reading the loaded route back, not assumed.

### Measured

- A move from one build to another takes 3.5 to 5.8 s: 3 s of check, up to 2 s for Traefik to load the route, and the old copy's stop.
- Asking again for a finished move takes 0.08 s.
- Five test-bed tests, run three times with the request loop running throughout: no request failed.

## Without a container manager

The move needed from the container host: list containers by label, pull an image, start a container with labels on a network, run a command inside a container, place a file in a container, read a container's log, stop and remove a container, and one network. The Docker SDK over the existing certificate connection gives all of these. Nothing was missing that a manager such as Dokploy would have given for this move.

## On the test bed

`tests/test_bed_move.py` runs the move against the deployed service with a request loop from the Mac to `docker-lp-scratch.shiny` with the lane's name in the `Host` header:

```
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_move.py -v
```

Builds A, B and C are `nginx:1.30.6-alpine`, `nginx:1.31.8-alpine` and `hashicorp/http-echo:1.0`. The two nginx builds tell themselves apart in the `Server` header. C listens on 5678, not 80, so it fails its check. The kill tests run `systemctl kill --signal=KILL launchplane` on the control container with `lp-control` once copy B exists on the scratch container. systemd starts it again.
