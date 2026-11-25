# An Odoo product

A product whose description has an `odoo` part is of the Odoo kind. Its lane runs a build Launchplane has verified, on a data set of its own: a database server and a files volume. The first product of the kind is TK, `cbusillo/odoo-tenant-tk`, on Odoo 20.

A release to another build of a product that declares `update` is made on [a copy of the lane's data, updated before the switch](#a-release-that-updates). [A lane's data is also copied](copying.md) by a call of its own. The background service that runs Odoo's scheduled jobs, previews, backups, acceptance, going back after a switch and deleting the data set a release replaced are not built.

## The description

```json
{
  "health_path": "/web/login",
  "database": {"image": "postgres:18-alpine"},
  "odoo": {
    "repository": "cbusillo/odoo-tenant-tk",
    "workflow": ".github/workflows/build.yml",
    "image": "ghcr.io/cbusillo/odoo-tenant-tk",
    "addons_path": ["/opt/project/addons", "/opt/project/addons/shared", "/opt/launchplane/addons", "/odoo/addons", "/odoo/odoo/addons"],
    "modules": ["launchplane_settings", "disable_odoo_online", "crm", "contacts", "website_crm", "website_livechat", "crm_livechat", "base_automation", "auth_passkey", "tk_website", "tk_listings"],
    "attachment": "launchplane-check",
    "sha256": "5b8e2378c01c9392fc415eb0d02888d76f0a04ea8dec78a3860b07181260ffb4",
    "update": ["tk_website"]
  }
}
```

| Field | Meaning |
| --- | --- |
| `repository`, `workflow`, `image` | Where a build comes from: a run of this workflow in this repository, which publishes to this image repository. |
| `addons_path` | Where the build keeps its modules. The path in TK's image leaves out its shared modules, so it is declared. |
| `modules` | The modules the check requires installed. TK's are those its build manifest lists as `odoo_install_modules`. |
| `attachment`, `sha256` | The name of one attachment in the lane's data, and the SHA-256 of its bytes. The check reads it. |
| `update` | The modules a release to another build updates. It may be left out: such a product's releases run on the data the lane has, and nothing is copied or updated. |

`database` is as for [any lane with a database](data-sets.md) and must be there. Every other field of `odoo` but `update` is required, and a field Launchplane does not know makes the file unreadable.

## A build is a publishing run

`POST /lanes/{product}/{lane}/move` with `{"run": <number>}` names a run of the product's workflow. Before the request is kept, Launchplane reads the run and its artifacts from the git host and requires all of:

- the run is of the described repository, and so is its commit: a run for a pull request from a fork is refused
- it is a run of the described workflow, and it ended in `success`
- it has the artifact `artifact-manifest-<its attempt>`, which holds `artifact-manifest.json`. A merge-train run passes without publishing and has none
- the manifest's `source_commit` is the run's commit, its `image.repository` is the described one, and its `image.digest` is a SHA-256

The request is then kept with its `run` and the build `<image>@<digest>`, and the move runs that. A call sent again with its `Idempotency-Key` is answered with its request before anything is read from the git host, whatever the run publishes later. A run that is not such a build is answered `422` with each fact that differs. A git host that cannot be read, or a manifest that cannot be read as one, is answered `503`. Nothing is kept either way. `{"build": "..."}` is answered `422` for a product of this kind, and `{"run": ...}` for any other.

Launchplane reads GitHub with the token in `LAUNCHPLANE_GIT_HOST_TOKEN`. `LAUNCHPLANE_GIT_HOST` names another address in place of `https://api.github.com`.

**What runs is held to the digest.** At every reading, an instance whose build names a digest is compared with the image the host has for that digest. One that runs another image, or whose image the host no longer has, shows as wrong with `runs <image>, which is not its build`. A move to that build replaces it, and no move switches to it.

## How an instance runs

Defined once for the kind, in `src/launchplane/odoo.py`. The product declares only what is in its description.

| | |
| --- | --- |
| Command | `/odoo/odoo-bin`, as the user `ubuntu` |
| Settings | From the environment alone, with `ODOO_RC=/dev/null`: no configuration file is written |
| Database | The instance's own [login](data-sets.md#logins), through `PGHOST`, `PGPORT`, `PGDATABASE=app`, `PGUSER` and `PGPASSWORD`, which Odoo reads itself. `PGDATABASE_SYSTEM=app`, because Odoo looks whether its database exists at every start, and the login may connect to `app` alone |
| Port | 80 on every address, as every instance |
| Files | The data set's files volume at `/volumes/data`. Odoo keeps attachments under `filestore/app` there |
| Scheduled jobs | Off: `ODOO_MAX_CRON_THREADS=0`. They belong to the background service |
| Database manager | Off: `ODOO_LIST_DB=False` |
| Way out | None. Its networks are quiet, and it is given no key to anything |

## The check

A candidate must answer `health_path` with success for 3 s, as any instance. Then Launchplane runs Odoo's own shell once inside it. The shell loads the registry for the lane's database as the server does, and the check fails unless:

- every declared module is installed
- the declared attachment is there exactly once, and its bytes, read through Odoo from the files volume, have the declared SHA-256

A failure answers `check failed` with what the check read, the candidate is removed, and the lane keeps what it served. It fails this way on a database that has only `base` installed, although `/web/login` answers `200` there, and on an attachment whose file is gone, which Odoo reads as no bytes.

## A release that updates

A move of a lane that serves one build to another build, for a product that declares `update`, is made on a copy taken inside the release's own freeze. Nothing written to the lane after the copy can be lost, because nothing can be written: visitors get the back soon page from before the lane is stopped until the release switches or gives up. The first move of a new lane, and a move to the build already serving, copy and update nothing and are [the ordinary move](the-move.md).

It reads the host and the proxy before every step, asks [the gate](the-move.md#the-gate), and takes the first of these that applies.

1. **Freeze and copy,** as [a copy of a lane's data](copying.md#what-it-does) does it, steps 1 to 5: the copy's two volumes are made, the web address is sent to the back soon page, the background service, the web part and the database server are stopped, and each volume is copied and compared. The lane must have exactly one instance, receiving visitors, with nothing found about it.
2. **Give the copy a network and a database server.** Both volumes must be whole copies first: a copy with one volume, or with a copier that did not finish, is never given a server.
3. **Start the candidate** from the new build on the copy: the copy's database server through a login of its own, the copy's files volume, and the copy's network. Its networks have no way out, it runs no scheduled job, and neither volume of the data set that was copied is mounted in it or reachable from it.
4. **Update, once.** `/odoo/odoo-bin --update <modules> --stop-after-init --no-http`, run in the candidate under `timeout 600`. It is sent once by a request and never again.
5. **Check the candidate,** with [the check](#the-check) whole: it answers `health_path` for 3 s, every declared module is installed, and the declared attachment is there once with its bytes.
6. **Switch,** by asking the gate, once. Then the instance the release replaced is removed. Its data set is kept, both volumes and its stopped database server, and shows on the page as it is: the server `exited`, and the lane `good`.

**Giving up.** If the copy is given up, the update ends with an error or runs out of time, the check fails, or a cancel wins at the gate, the candidate is taken through the gate and removed, the copy's database server is stopped, and the lane is brought back as [ending maintenance](maintenance.md#out-of-maintenance) does: the original database server, the original instance, its check, and last the route. Only then does the request end. The copy is kept, with its network and its stopped server, and is never used again.

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `switched` | 200 | The web address sends visitors to the new build on the updated copy. `data_set` names the copy. |
| `not updated` | 422 | Nothing was switched, and the lane serves from its own data again. `error` says why: what the update wrote last, why the copy was given up, or that an earlier attempt did not finish. |
| `check failed` | 422 | The update ran and the candidate failed its check. Nothing was switched, and the lane serves from its own data again. `error` has what the check read. |

**An update whose outcome is not known is never looked into.** A reply that was lost, or a Launchplane killed while the update ran, leaves a request that is taken up again. An attempt that finds the lane behind the back soon page of its own request, and did not put it there itself, gives that copy up whatever is on it, brings the original back, and ends the request `not updated`. Asking again makes a new copy. An attempt that finds the lane already back, with a copy this request began, changes nothing and ends the same way.

**While the original cannot be brought back** the request does not end. It stays `working`, the back soon page stays, `error` says what it waits for, and it is taken up again every 5 s. Docker's refusal of any step behind the back soon page does the same.

**On the page,** a move that put its lane behind the back soon page shows one short paragraph in place of its lines: that the data is being copied, that the copy is being updated and checked, what failed and that the original is coming back, or how it ended. Every line is in `GET /requests/<id>`.

## The data set

The data set of an Odoo lane has a files volume beside the database's: `launchplane-data-<data set>-files`, labelled like the database volume and with `launchplane.part=files`. It is made after the database volume and before the server, shows in `GET /running` as the data set's `files`, and is deleted with the data set. An instance that does not have it mounted shows as wrong.

**A new lane is seeded by hand, once.** Launchplane makes the data set and installs nothing in it. The first move of a new lane therefore answers `check failed` with Odoo's `Database app not initialized`, and leaves the data set. `python -m tests.bed_odoo seed <product> <lane>` then installs the declared modules with Odoo's own `--init` and makes the attachment, with invented data only, from a container on the data set's network that holds a login Launchplane removes at the next move.

## Known and not fixed

- **Launchplane on the test bed holds no key to GitHub.** The bed tests and the watch read the answers GitHub gave for two of TK's runs, served on the control container by `tests/recorded_git_host.py`. From the builder's Mac the same code read both runs from GitHub itself, 1.2 s a run. Granting the bed a key is Chris's.
- **Launchplane cannot pull TK's image.** The image is private, and Launchplane sends Docker no registry login. The scratch host pulls with its own. A build that is not on the host fails the move with Docker's `No such image`. Launchplane also gives Docker 5 s to answer, and this pull took 36 s.
- **The shared Odoo tools cannot start Odoo on a lane's login.** `run_odoo_startup.py` of `odoo-devkit` connects to the database `postgres`, which a lane's login is refused. It waited there until stopped, with 66 refusals in the server's log. So an instance runs Odoo's server directly, and none of that script's work is done: no install of missing modules, no website settings, no admin password. Its module update will meet the same refusal.
- **A new lane cannot pass its first move.** The seed above is a person's work. Installing the modules belongs to the update of a release, which is not built.
- **A build that needs its modules updated fails its check.** TK's run 45359350705, four commits after the first, answered `500` at `/web/login` on data seeded from the first build. The lane kept serving.
- **The admin password of a seeded lane is Odoo's default.** The lane is reachable only on the test bed's own network.
- **A move to the build already serving does not run the check again.** It answers `unchanged`. A file lost from the files volume while the lane serves is found only by the next release.
- **Moves of one lane asked at the same moment are kept in the order the git host answers them,** not the order they were sent. Each is verified before it is written.
- **The modules the check requires come from the description,** not from the manifest of the build being moved to, which lists them too.
- **A background service is not held to its build's digest,** and no Odoo product has one yet.
- **The files volume is on the container host's own disk,** as the database volume is.

## Measured

On November 24, on the scratch host, with TK's build `sha256:0e2ad09f…` of commit `302cb11`, 1.19 GB, on PostgreSQL 18.7:

| | |
| --- | --- |
| A new database with TK's declared modules, 85 modules in all | 37.2 s, then 47 MB of database and 104 files, 286 KB |
| Odoo started until it answers | `/web/health` 3.9 s, `/web/login` 4.5 s, `/` 4.6 s. A second `/web/login` 0.06 s |
| A module update, scheduled jobs off, beside the running server | `tk_website` 6.2 s, every module 31.4 s |
| The check's shell, in the running instance | 1.2 s |
| Memory, idle | 151 MiB for Odoo, 91 MiB for the database server |
| A move of the seeded lane to the build | 17.4 s. Asked again: 1.7 s |
| A move that fails its check | 30 s on a database never installed in |
| `tests/test_bed_odoo.py`, 8 tests | 2 min 19 s |
| A copy of the lane's two volumes, from the first back soon answer at the web address to both copies confirmed | 3.9 to 4.2 s, for a database volume of 3,242 files and 141 MB, the database `app` being 47 MB, and a files volume of 110 files and 4.6 MB. The rest is in [a copy of a lane's data](copying.md#measured) |
