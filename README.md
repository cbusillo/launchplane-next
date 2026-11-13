# Launchplane

Launchplane is the control layer that puts a product's builds onto its sites, keeps their data safe, and records a Client's decision to release. Agents use it through an API.

A product can be maintained without anyone touching Launchplane. That is the test of every change here.

To start, people see two pages: the Client's accept page and one status page. The full interface comes after the product works.

## Words

- **Director:** Chris. Decides design, and holds admin.
- **Client:** the person whose business a product serves. A product records its Client. The Client may be the Director.
- **Product:** one thing a Client uses, such as a website or an Odoo system, with its own repository.
- **Kind:** the type of a product. What a kind needs is defined once, in that kind's shared base. For Odoo that is `odoo-devkit`.
- **Build:** what a product's own repository produces from one commit, today a container image. Launchplane never makes a build and never supplies its inputs. It checks which repository and commit a build came from.
- **Lane:** one running copy of a product. There are three kinds.
  - **Production:** what the Client's business runs on.
  - **Testing:** always up, so a Client who goes to test never finds it down. It is built from production's data, with private details removed and keys for outside services removed or replaced with testing keys.
  - **Preview:** a short-lived copy for one pull request. It is built from testing, as a copy that shares testing's data until something changes, and is paused when not in use.
- **Live:** a product is Live when its Client uses production for real business. A Live site is the production lane of a Live product.
- **Prelaunch:** a product whose production lane exists but whose Client does not yet run their business on it. Its production is not Live.
- **Acceptance:** a Client's recorded yes to one exact build. It stays good until that build is live.
- **Release:** changes which build runs on production. Needs acceptance.
- **Setting:** something that differs between lanes of the same build: addresses, secrets, sizes. A settings change keeps the same build, goes through the same move, and needs no acceptance. Anything that changes what the product does belongs in the build.

Launchplane manages only products described to it. A system outside it is not Launchplane's to touch.

## What cannot be undone, and what guards it

These protections are never removed. How they are built may change.

**Lost data**

1. A verified backup before production. No release starts without one.
2. A failed release never reaches production. The old copy runs until the new one passes its checks, and the backup taken first can put back both the build and the data.

**A leaked secret**

3. Isolation of secrets. A secret is never shown, logged or shared between products. A testing or preview lane never holds a key that can change a Live system.

**A Live site changed without acceptance**

4. Client acceptance on Client products. Which build a Live site runs changes only from the Client's acceptance of that exact build. For a product whose Client is the Director, the Director's standing direction is the acceptance.
5. No machine promotion. No automated identity gains a right to release. Only the acceptance starts one.

On a Live site the Director may ship a temporary hotfix for breakage before the Client accepts it. The Client then reviews it like any release. A rejection puts back their last accepted build through the same move, on the data as it is now, so nothing written since is lost. If that build cannot run on the current data, nothing switches, and the Director chooses between another fix and restoring the backup.

Restoring a backup onto a Live site loses what was written since it was taken. It is always the Director's decision and never automatic.

## Rules

1. Guard only what cannot be undone. Everything else may fail, says why, and is safe to run again.
2. Look, don't remember. Each running copy reports which product, lane and build it is. Launchplane reads that from the host.
3. Every part points to a step of the finish-line test or to a named protection.
4. One move serves every lane. A check that can stop production has already run on the same build before anyone is asked to accept.

## The move

Copy, update the copy, check it, then switch. The old one is not touched until the new one has passed its checks, so not switching is how a failed release is stopped. Going back after a switch is the same move again with the earlier build, on the data as it is now. The backup is the last resort underneath.

## What Launchplane stores

1. **What people decided:** product descriptions, Clients, settings and secrets, acceptances, and answers to questions.
2. **What Launchplane did:** a record of every move, with when, which build, the result, the real error, and which backup belongs to it.
3. **What is waiting on a person,** and on whom.

It never stores what is running now. That is always read from the host.

It does not store what should be running either. That is worked out each time from what people decided and from the product's repository: production should run the build of the latest acceptance or of a hotfix the Director started after it, testing the latest build of the main branch, and a preview the latest build of its pull request. The status page compares that with what the host reports.

The record of what Launchplane did is history. It never answers what is running.

## What Launchplane asks of a product

Four commands, defined once per kind: check health, back up, restore and scrub, update.

## Who uses it

- **The Director:** everything.
- **Agents:** everything except change which build a Live site runs outside an accepted release, delete Live data, or grant access.
- **Clients:** see their own products and accept or reject a release. A Client can stop a change and can never merge one.

## Also required

These are requirements even though the finish-line test does not show them.

- Search engines are told not to index any lane except real production. A prelaunch production site is not indexed either.
- Sign-in through Authentik lives in shared code, so any product can use it.
- A site that is not yet released often needs a seeded test login, including sites that are not Odoo.
- Image scanning belongs to the product's own build. Launchplane does not scan.
- The container host and the git host each sit behind one small piece, so either can be replaced.

## Finish line

One command, with nobody touching anything:

- sets up a new website product and a new Odoo product from their descriptions
- sends three changes each from pull request to production
- gives the Odoo product fresh production data on testing before each acceptance request
- fails a fourth change on purpose, and production stays on the last good build with its data
- leaves the status page green before, red during the failure and green after, with nothing cleared by hand
- keeps an acceptance good across a retry

The first step is the one exception to the finish line: it proves the tools. Ruff, the type checker, the tests, CI and inspection each catch a deliberate mistake and pass its fix.

Not part of this finish line, and not built until Chris says: scheduled backups and disaster restore, Launchplane releasing itself, telling the Director when a lane goes red, setting up hosts, the full interface, and Client agents.

## Open, and who settles it

Nothing below is decided. Do not build past one of Chris's questions. Each names the first step it blocks.

**Chris decides**

1. Who accepts during the finish-line test. The test runs with nobody touching anything, and no automated identity may release. Blocks the first release to production.
2. What happens to writes that reach production while its copy is being updated: a pause, a catch-up, or something else, and how long an interruption is acceptable. Blocks the move for any product with a database.
3. What the copy of production may do before the switch. It holds production's keys and scheduled jobs, and must not send mail, take payments or push to outside services twice. Blocks the same step.
4. What "verified" means for a backup, and how long backups are kept. Blocks the first production move.
5. What scrub must remove, for each kind, and what proves it ran. Blocks any real data reaching testing.
6. What triggers an acceptance request, what a rejection records, and what happens to a waiting request when a newer build arrives. Blocks the accept page.
7. How a Client proves who they are, how an agent is told apart from the Director, and who may mark a product Live. Blocks the accept page and the API's first protected call.
8. Whether a setting that can change what a Live site does, such as a database address or a payment key, needs acceptance or a backup first. Blocks settings changes on production.
9. What "fresh" production data means before an acceptance request: a new copy each time, a maximum age, or a recheck. Blocks the testing refresh.
10. What happens to a Client's test session and to previews when testing is rebuilt. Blocks the testing refresh.

**The builder measures, then Chris decides**

11. How long a database copy and update take at real size, and whether PostgreSQL 18 can clone a database without copying it on this file system.
12. Whether the file store is shared or copied in a move, and how the database and file store are kept consistent with each other.
13. Whether Dokploy's start-first deploy can do the switch, or Launchplane must do it itself.
14. How much disk and memory several previews use at once, and what pausing one actually frees.

**The builder proposes in the step that needs it**

15. What a product description contains and where it lives.
16. How the four commands are called, and where they live for a kind other than Odoo.
17. How a running copy reports which product, lane and build it is.
18. How a build proves which repository and commit it came from.
19. What marks which copy is serving after a crash during the switch, and what Launchplane must record to finish or abandon a half-done move.
20. How the status page shows a failed move separately from a lane's health, and what clears it.

## What exists today

- The test bed is two containers. The control container runs a fresh Dokploy and will run Launchplane. The scratch container is a plain Docker host for the pretend products. Neither is connected to anything real.
- The pretend website and pretend Odoo product do not exist yet. Creating their repositories needs Chris.
- An earlier build of an Odoo system usually cannot run on a database a later build has updated. Going back after a switch will often find it cannot, and then the choice is Chris's.
