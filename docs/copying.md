# A copy of a lane's data

`POST /lanes/{product}/{lane}/copy` puts a serving lane behind the back soon page, copies its database volume and its files volume into a new data set while everything of the lane is stopped, and brings the same instance back on its own data. It takes no body. It is kept as a [request](requests.md) and answered `202` at once; with `?wait=<seconds>` it answers as below when it ends.

Nothing uses a copy yet. No module is updated on it, no instance is started on it, visitors are never sent to it, and no release is committed: the copy's request changes the route twice, to the back soon page and back, and both are edits at [the gate](the-move.md#the-gate).

```
curl -X POST http://launchplane.shiny:8700/lanes/<product>/<lane>/copy
```

## What it does

It reads the host and the proxy again before every step and takes the first of these that applies.

1. **While the proxy still sends visitors to it, read what is to come back.** The lane must have exactly one instance, it must receive visitors, and nothing may be found about it, its data set or its background service: its build is the image the host has for its digest, and its mounts and networks are as they must be. Otherwise the call answers `503`, with nothing stopped.
2. **Make the copy's volumes,** `launchplane-data-<copy>-database` and, for a product that keeps files, `launchplane-data-<copy>-files`. Each carries `launchplane.copy-of`, the data set being copied, and `launchplane.generation`, the request. From here on the host says which data set this request copies, so an attempt after a kill reads it there.
3. **Send the web address to the back soon page,** and wait until the proxy has loaded that. Nothing is stopped while the proxy still sends visitors to the instance.
4. **Stop the background service, the web part and then the database server,** as [beginning maintenance](maintenance.md#into-maintenance) does. No login is removed or closed, so the instance comes back with the login it has.
5. **Copy each volume,** the database's and then the files', only once every part is read as stopped, the database server with exit code 0, and nothing else is found on the lane.
6. **Bring the lane back** as [ending maintenance](maintenance.md#out-of-maintenance) does: the database server, the one instance, its check, with [the check of its kind](odoo.md#the-check) for an Odoo product, its background service, and last the route.
7. **Answer.**

**The copier.** Each volume is copied by a container of the data set's database image, named `<the copy's volume>-copier`. It has the volume it copies mounted read-only, the copy's volume as its only other mount, and no network. It runs `cp -a`, then `diff -r` over both, then `sync`, so a file the copy lacks, has too many, or has other bytes for fails it. Before it is made, Docker is asked for every running container that has the volume to be copied mounted, whatever its labels, and one such container refuses the copy. A copier has 300 s. It is not removed: Docker's description of it is the account of what was read, what was written and how it ended.

**Giving up.** A database server that exited with another code than 0, a part that does not read as stopped, a back soon page that is not running, a copier Docker refused or that ended with an error, a copy another attempt began, or a copy's volumes that are not one fresh volume for each volume copied: each gives the copy up. The request writes the line `give the copy up:` with why, stops a copier that still runs, and takes step 6. A copy is never gone on with by another attempt than the one that began it, and never begun twice by one request.

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `copied` | 200 | The copy is whole and the web address sends visitors to the same instance again. `copied_to` names the copy and `data_set` the data set copied. |
| `not copied` | 422 | The copy was given up and the lane is back. `error` says why. `copied_to` names what was made, which is kept. |

It answers `503`, having stopped nothing, when the lane is not as step 1 needs it, and when the lane is in maintenance that this request did not begin: whose route it is, is read from the routes file's first line.

**While the lane cannot be brought back** the request does not end. It stays `working`, the back soon page stays, `error` says what it waits for, such as a check that failed, and it is taken up again every 5 s. Docker's refusal of any step behind the back soon page is the same: a start Docker answers no to is tried again from a fresh reading, and only a refused copy gives the copy up. A refusal before the back soon page shows fails the request, with the lane still serving. A cancel does not end it either: a cancel brings the lane back the same way.

## After a kill, a lost reply or a cancel

Nothing is resumed. The request is taken up again and decides from the host:

| The host shows | What the next attempt does |
| --- | --- |
| No copier yet | Goes on from where the lane is: stops what still runs, and copies. |
| A copier that this attempt did not run, and the copy not whole | Gives the copy up, stops that copier if it runs, and brings the lane back. The request ends `not copied`. |
| The copy whole, the back soon page still showing | Brings the lane back and answers `copied`. |
| The lane back, and a copier of this request's copy | Changes nothing, and answers `copied` or `not copied` as the copy is. |

A **cancel** asked while the back soon page shows brings the lane back and ends the request as `cancelled`. Its answer lists the copy under `data_sets_kept`. Asked before the route changed, it changes nothing. Asked once the copy is whole and the lane is back, it is too late and the request ends `done`.

## A copy on the host

A copy is whole when each of its volumes was written by one copier whose description says it exited with code 0, and no copier of it ended otherwise. The state is read from the description, not from Docker's list, which can still say `running` just after a container has exited.

- `GET /running` gives each data set `copy_of`, `generation` and `copies`: each copier's `container`, the `volume` it could write, `finished` and `stopped`.
- The page shows a copy as `is a whole copy of <data set>, kept offline: no instance is started on it`, or `is a copy, not whole, of <data set>`. Neither is a finding: the lane stays `good`.
- A copy has no network and no database server.
- A [move](data-sets.md#a-move-with-data) never starts an instance on a copy, whole or not. A lane whose only data set is a copy is given a new, empty data set, and the copy is left as it is.
- A copy is kept until [a retire names it](data-sets.md#retiring-a-lane-with-data). A data set that has a copy is not deleted before the copy: the copiers hold its volumes as what they read, and the retire answers `503` naming the copy.

## Known and not fixed

- **Nothing deletes a copy but a retire of its lane that names it.** Each call leaves one, also one that was not finished.
- **The back soon page shows for much longer than the copy takes.** Most of it is Odoo starting again and its check. See the measurements.
- **A copier has 300 s for a volume,** chosen with nothing measured at real size. When it runs out, the request stays `working`, its next attempt stops the copier, gives the copy up and brings the lane back.
- **A clean stop is read from the database server's exit code,** as maintenance reads it, not from PostgreSQL's own control file.
- **`diff -r` compares names and bytes,** not owners, modes or times. `cp -a` keeps them.
- **The copier needs `cp`, `diff` and `sync` in the database image.** `postgres:18-alpine` has them.
- **An unclean database stop followed by a kill just after the route back is written can freeze the lane a second time.** The copy was given up before any copier was made, and once the server has started again the host no longer shows why. The request, taken up again, finds no copier, begins the copy again, and can end `copied`. A copy that got as far as a copier is never begun again.
- **A web part that exits just as the route back loads can be answered `copied`.** The check passes behind the back soon page, and the request's last reading does not ask again whether the instance runs or the web address answers. The lane then shows as wrong on the status page.
- **Pruning unused Docker volumes during an interrupted copy can make a half copy read as whole.** Between the two volumes the copy's files volume is empty and no container holds it, so `docker volume prune --all` would remove it. Not tried. The copy then has one volume and one finished copier, which is what whole means.
- **A request whose lane cannot be brought back keeps its lane for as long as that lasts.** Nobody is told.
- **Volumes are copied whole.** Whether PostgreSQL 18 or the file system can copy without copying every byte is not measured (README, open question 13).
- **Untried:** a product with a background service on the real host, a lane with more than one earlier copy, and a volume too large for 300 s.

## Measured

On November 24, on the scratch host, with TK's build `sha256:0e2ad09f…` seeded as in [an Odoo product](odoo.md#measured) and one lead with an attachment added:

| | |
| --- | --- |
| What was copied | The database volume, 3,242 files and 141 MB, of which the database `app` is 47 MB. The files volume, 110 files and 4.6 MB |
| From the first back soon answer at the web address to both copies confirmed | 3.9 to 4.2 s, three runs |
| Of that, by the request's lines | Stopping Odoo 0.5 s, stopping the database server 0.9 to 2.6 s, copying and comparing the database volume 1.1 to 1.3 s and the files volume 0.2 to 0.3 s |
| The back soon page, from the route to it to the route back | 10.2 to 11.9 s. Odoo starting and its check are 6.6 s of it |
| A kill between the two volumes, to the lane back | 13 to 14 s after the request began |
| `tests/test_bed_copy.py`, 2 tests | 2 min 12 s |

## On the test bed

```
uv run pytest tests/test_copying.py -v
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_copy.py -v -s
```

The bed test uses the lane `testing` of `test-bed-copy`, seeded as `tests/test_bed_odoo.py` seeds its lane, and removes what it made. With `-s` it prints the measured time and what was copied. Its second test runs under [the held Launchplane](the-move.md#holding-a-move-and-interrupting-it). [The copy watch](the-move.md#the-copy-watch) is the same two runs by hand.
