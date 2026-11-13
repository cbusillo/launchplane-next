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

Copy, update the copy, check it, then switch. The old one is never touched until the new one has passed, so not switching is how a failed release is stopped. Going back after a switch is the same move again with the earlier build, on the data as it is now. The backup is the last resort underneath.

## What Launchplane stores

1. **What people decided:** product descriptions, Clients, settings and secrets, acceptances, and answers to questions.
2. **What Launchplane did:** a record of every move, with when, which build, the result, the real error, and which backup belongs to it.
3. **What is waiting on a person,** and on whom.

It never stores what is running now. That is always read from the host.

It does not store what should be running either. That is worked out each time from what people decided and from the product's repository: production should run the build of the latest acceptance, testing the latest build of the main branch, and a preview the latest build of its pull request. The status page compares that with what the host reports.

## What Launchplane asks of a product

Four commands, defined once per kind: check health, back up, restore and scrub, update.

## Who uses it

- **The Director:** everything.
- **Agents:** everything except change which build a Live site runs outside an accepted release, delete Live data, or grant access.
- **Clients:** see their own products and accept or reject a release. A Client can stop a change and can never merge one.

## Other facts

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
