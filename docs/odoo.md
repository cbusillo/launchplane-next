# An Odoo product

A product whose description has an `odoo` part is of the Odoo kind. Its lane runs a build Launchplane has verified, on a data set of its own: a database server and a files volume. The first product of the kind is TK, `cbusillo/odoo-tenant-tk`, on Odoo 20.

[A lane's data is copied](copying.md) by a call of its own. A module update inside a release, a release that uses a copy, the background service that runs Odoo's scheduled jobs, previews, backups and acceptance are not built.

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
    "sha256": "5b8e2378c01c9392fc415eb0d02888d76f0a04ea8dec78a3860b07181260ffb4"
  }
}
```

| Field | Meaning |
| --- | --- |
| `repository`, `workflow`, `image` | Where a build comes from: a run of this workflow in this repository, which publishes to this image repository. |
| `addons_path` | Where the build keeps its modules. The path in TK's image leaves out its shared modules, so it is declared. |
| `modules` | The modules the check requires installed. TK's are those its build manifest lists as `odoo_install_modules`. |
| `attachment`, `sha256` | The name of one attachment in the lane's data, and the SHA-256 of its bytes. The check reads it. |

`database` is as for [any lane with a database](data-sets.md) and must be there. Every other field of `odoo` is required, and a field Launchplane does not know makes the file unreadable.

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
| A copy of the lane's two volumes, from the first back soon answer at the web address to both copies confirmed | 4.2 s, for a database volume of 3,242 files and 141 MB, the database `app` being 47 MB, and a files volume of 110 files and 4.6 MB. The rest is in [a copy of a lane's data](copying.md#measured) |
