# Requests

A call that changes a lane is written down as a request, answered at once, and carried through by a worker inside Launchplane. Each lane has its own turn, so different lanes are worked at the same time, and a request can be cancelled. The four calls are the [move](the-move.md), [retire](retiring-a-lane.md), and beginning and ending [maintenance](maintenance.md). What each does to a lane is in its own document and is unchanged.

```
curl -i -X POST http://launchplane.shiny:8700/lanes/<product>/<lane>/move \
  -H 'Content-Type: application/json' -d '{"build": "<image>"}'
curl http://launchplane.shiny:8700/requests/<id>
curl http://launchplane.shiny:8700/requests
curl -X POST http://launchplane.shiny:8700/requests/<id>/give-up
curl -X POST http://launchplane.shiny:8700/requests/<id>/cancel
```

## Asking

Each of the four calls writes a request and answers `202` with the request and a `Location` header, `/requests/<id>`. Nothing on the host has changed yet.

- **`?wait=<seconds>`**, up to 3600. The call answers when the request ends, with the status and body the call had before requests were kept: `200` with the result, `422` with the result for an outcome that is not a success, or `503` with `error` when it stopped. A request that was cancelled answers `409` with `error`, saying by what and what was put back, and one that was replaced answers `409` saying by which request. If the request has not ended in that time, the answer is `202` with the request. The `Location` header is there either way.
- **Asking the same thing of a lane while a request for it is waiting or working** writes nothing, replaces nothing and answers with that request, without a key or with one not used before. The same thing is the same call, lane, build and data set named for deleting. If that request is being cancelled, the call writes a new one.
- **Asking anything else of a lane** writes a new request, which waits. A lane has at most one request at work and one waiting, and the new request takes the waiting place. The request at work is not touched by it: it ends only by finishing, by failing, or by a [cancel](#cancelling) or a [give-up](#giving-up) a person asks for. The request that was waiting ends as `replaced`, with nothing done, and its `replaced_by` names the new request. The new request is worked once the one at work has ended. The record itself refuses a lane a second request that waits.
- **`Idempotency-Key: <text>`**. The same key with the same call and body answers with the request already written, whether or not it has ended. The same key with anything else answers `409` and writes nothing. A key is never forgotten and never shown: the record keeps only its SHA-256 digest. A key says only that a call is one sent before: a call with a key not used before is held to its lane's rule like any other. When it is answered with a request already waiting or working, the key is kept against that request, so the same key sent again at any later time answers with that request, however it has ended.
- **`422`** with `error` also answers a call that carries a secret a container holds, in any part of it. Nothing is written.
- **`503`** with `error` means the record could not be reached, and whether the request was written is then not known: the call may repeat a request already written, with a key or without one, and a write may have landed before the record was lost. If it was written, it will be worked. Asking again with the same key answers with it if it was, and `GET /requests` lists it.

## Reading

`GET /requests/<id>` answers the request:

| Field | Meaning |
| --- | --- |
| `id`, `product`, `lane` | Which request, and its lane. |
| `generation` | The number of the release this request is, stamped on what it makes: see [the move](the-move.md#what-it-does). No other request has it, also after the record is lost and made again. |
| `work` | `moving`, `retiring`, `beginning maintenance` or `ending maintenance`. |
| `build`, `delete_data_set` | Exactly what was asked: the build of a move, the data set a retire named for deleting. |
| `asked_by`, `asked_at` | The address the call came from, and when. Nobody is known by name yet. |
| `stands` | `waiting`, `working`, `done`, `failed`, `cancelled` or `replaced`. |
| `ended_at`, `answer`, `error` | When it ended; the result the call answers with `?wait=`, or for a cancelled request what [cancelling](#cancelling) left; the real error of a request that failed, or why a request that is still working is not finished yet. |
| `cancel_asked_at`, `cancel_asked_by` | When a cancel was asked, and by what: `a call from the address <address>`. `null` when none was asked. A request written before November 20, 2026 may say `request <id>`: a newer request then cancelled the older. |
| `replaced_by` | The newer request for the lane that took the waiting place from this one. `null` unless it stands as `replaced`. |
| `cancel_refused` | Why a cancel that was asked was not carried out: see [cancelling](#cancelling). For a move it is written by whoever learns it at the gate, the call that cancels or the worker. |
| `attempts` | Each time a worker took the request up: `number`, `began_at`, `ended_at`, and `ended` as `done`, `failed`, `cancelled`, `interrupted`, or `unfinished` for a request that could not be ended yet. A request at work that has more than one was taken up again that often, and the last one that is over says why: `interrupted` is a Launchplane that stopped, or lost its record, while at work on it, and `unfinished` has its reason in `error`. |
| `events` | One line for each thing Launchplane did or was refused, in order: `at`, the `attempt` it belongs to, `did`, and `error` with the real error where it was refused. The line of [a change of the route](#a-change-of-the-route) also has `action` and `outcome`, and the line of [a command for a part](#a-command-for-a-part) or of [a login](#a-login-made-or-removed) has `command` and `outcome`. |
| `release_verdict` | What [the gate](the-move.md#the-gate) answered for a move's release when the move ended: `none`, `committed` or `abandoned`. `null` for a request that is no move, one that has not ended, one a person gave up, and one that ended before November 21, 2026. |

A request is `done` when its call would have answered `200`, `failed` when it would have answered `422` or `503`, `cancelled` when it was cancelled in time, and `replaced` when a newer request took its waiting place before it was taken up. `answer` is that of the attempt that ended the request, with what [a change of the route](#a-change-of-the-route), [a command for a part](#a-command-for-a-part) and the events of earlier attempts add to it; `events` has every attempt.

### A change of the route

An edit of a lane's routes file and a release's switch are each one command sent to [the gate](the-move.md#the-gate). Each sending has one line, and only these lines, those of [a command for a part](#a-command-for-a-part) and those of [a login](#a-login-made-or-removed) are written before the thing is done:

- **Before it is sent** the line is written with `action`: the `web_address`, and `to`, the container it sends visitors to, or `null` for a removal. `outcome` is `null`. Nothing is sent on a line that was not written.
- **After,** the gate's reply is written on the same line, with `outcome.at`. `outcome.made` is `true` when the gate made the change. It is `false` when the gate refused, with why in `error`, and when the gate found the route already as asked, with `outcome.already` `true`: a route someone else removed while a retire was in flight is not put down to the retire. The retire's answer still names the container the route went to, as before.
- **With no reply,** `outcome.made` stays `null` and `error` has the real error. The change may have been made. A later attempt of the same request looks for it in every reading of the lane it makes anyway: when the routes file sends visitors where the line says and its first line names this request's generation and the attempt that sent it, `outcome.read_back_at` is written and the change is put down to the request. Otherwise the line stays as it is: a route that is as asked and names another sender, or none, is not this request's.

What is put down to a request this way changes two answers and nothing else. Ending maintenance that finds its lane already out answers `ended`, not `not in maintenance`, when a line of the request sent visitors to an instance and was made or read back. A move taken up again lists a switch that was made or read back in `did`. Nothing is decided from a line: with every one deleted, Launchplane makes the same calls on the host in the same order, and answers `not in maintenance` and leaves the switch out of `did`.

The page shows such a line as `about to <did>` while its attempt is open and nothing is known, then `<did>: done`, `<did>: found already as asked`, `<did>: refused` or `<did>: may have been sent, no answer`, with `, then read as made by this request` once it was read back.

### A command for a part

Each command Launchplane sends to Docker for a lane's instance or background service has one line: `make`, `start`, `stop` or `remove`, and the container it names. A step that sends two commands has two lines: starting an instance is a make and a start, and retiring one is a stop and a removal.

- **Before it is sent** the line is written with `command`: the `verb` and the `container`. `did` is the two together, such as `stop site-testing-4f2a`. `outcome` is `null`. The line is written after everything the command waits on has been read: the image, the networks, the login. A command whose line cannot be written is not sent.
- **After,** Docker's answer is written on the same line before the next command is sent. `outcome.made` is `true` when Docker answered that it did it. It is `false` when Docker refused the command itself, with Docker's error in `error`, and when the part was read as already so just before the command went, with `outcome.already` `true`: a stop of a part that had stopped, a start of one that was running. Such a command is sent all the same, as before. A removal of a part that is already gone sends nothing and is written as found so.
- **With no answer,** `outcome.made` stays `null` and `error` has the real error. The command may have been sent. Docker's client reads a container back once Docker has made it, and a failure of that read is no answer to the make, not a refusal: the request stays `working` and is taken up again. Nothing on a container shows which sending started, stopped or removed it, so such a line is never put down to the request later. A `make` is the one exception: when the first reading of a later attempt shows a container of that name stamped with the request's generation, `outcome.read_back_at` is written. One container is put down to one make: of several lines with no answer only the last, none where a make of that container was answered, and none for a container the later attempt makes itself.
- No line holds the command's text, the environment, a password or what Docker said of the container.

A command Docker answered as done, and a make read back, are the request's own. A request's account of its commands is built from these saved lines and from nothing else, the same way whether it took one attempt or several: what a move lists in `did`, what a retire lists in `retired_containers`, what a cancel lists in `put_back`, and whether a move answers `repaired` or a retire `retired` where nothing else changed. `did` and `put_back` list a command in the words of its line, `stop site-testing-4f2a`. A request that fails with no answer says the same after why it failed: `Of its commands for the lane's parts, make site-testing-4f2a was done; remove site-testing-9c1e may have been sent, and Docker did not answer.` Before that, where a call had changed nothing but by such commands, its error says `Nothing else was changed.` A command found already as asked, one refused and one with no answer are in none of them. A step for a part has no line of its own any more, except where it failed before any command was sent or the gate kept the part: `retire site-testing-4f2a` with why. A command Docker refused or did not answer says so on its own line and has no second one. A line for a part written before November 22, 2026 is the step's, and is listed as it was.

Nothing is decided from these lines. With every one deleted, Launchplane makes the same calls on the host in the same order.

What still comes from the step and not from a line of its own: a data set's network, volume and database server, and starting the proxy. They are listed as before, once the step that did them returned, and are next.

The page shows the line of a command as it shows a change of the route.

### A login made or removed

Each [login](data-sets.md#logins) Launchplane makes or removes on a lane's database server has one line. A removal of two logins has two lines, and starting an instance on a data set has a line for its login before the two for its container.

- **Before its statements are sent** the line is written with `command`: the `verb`, `make` or `remove`, the `login`, and as `container` the database server. `did` is `make the login i_4f2a… on <server>` or `remove the login i_4f2a… from <server>`. The line is written after the reads that chose the login: the server's access rules, its logins, and which containers hold them. A login whose line cannot be written is neither made nor removed.
- **After,** the server's answer is written on the same line before anything else is sent. `outcome.made` is `true` when the server did it. It is `false` when the server refused a statement, with the server's own words in `error`.
- **With no answer,** `outcome.made` stays `null` and `error` has the real error. The statements may have run. Such a line is not put down to the request later. A making is the one exception, as for a container: when the first reading of a later attempt shows an instance stamped with the request's generation whose name is the one the login is named after, `outcome.read_back_at` is written.
- A login the planner read as held by nobody and the server no longer has when it is asked just before the removal has no line: nothing is sent and nothing is put down to the request.
- No line holds a statement, a password, its scrambled form or anything else the instance is given. The name of a login is not a secret: it is the end of its instance's name.

A login the server answered as made or removed is the request's own, and is in the same account as its commands: a move's `did`, a cancel's `put_back`, the sentence after why a request failed, and whether a move answers `repaired` or a retire `retired` where nothing else changed. A retire's `retired_containers` lists containers only. Nothing is decided from these lines. With every one deleted, Launchplane sends the same commands and the same statements in the same order.

`GET /requests` answers `unfinished`, every request waiting or working, oldest first and with its attempts and events, and `ended`, the 20 that ended last, newest first.

## Cancelling

`POST /requests/<id>/cancel` cancels one request, and nothing else cancels one: a newer request for the lane waits behind the request at work, and [replaces](#asking) only one that waits. What a cancel means depends only on where the request is:

- **Waiting:** it ends as `cancelled`. Nothing was done.
- **A move being worked:** the call itself carries the cancel to [the gate](the-move.md#the-gate) on the container host before it answers, and there either the cancel or the rename of the routes file wins, never both. A cancel counts from the moment it passes the gate, not from the moment it is written to the record: one that is on record and has not reached the gate when the switch passes it is told too late.
  - **The cancel wins** when the gate has no verdict for the request's generation. The gate then says `abandoned` for good, no switch of it can rename, and the worker puts the lane back: it removes what the generation made and nothing else. The request ends as `cancelled`. A Launchplane killed just after the cancel won, also before the call was answered, puts the lane back when it takes the request up again.
  - **The cancel is too late** once the gate says `committed`, also while the proxy has not loaded the renamed file and the old build still answers. The call answers `409` at once, `cancel_refused` says the rename was the boundary, and the release is finished as asked, also after a kill.
  - **The gate gives no answer,** since the host cannot be reached or the gate's command fails: the call answers `503` with the reason. The cancel stays asked in the record and is carried to the gate again by the worker of the request, where it wins or is too late like any other.
- **A retire, or beginning or ending maintenance, being worked:** the request is marked in the record. Its worker reads the mark before its next action and puts the lane back, or finds it too late and says why in `cancel_refused`.
- **Ended:** nothing changes.

A move never ends over a cancel that has no answer from the gate. When a cancel is recorded while the worker is making its answer, the record refuses that ending, the worker carries the cancel to the gate, and the move goes on from what the gate then says. So a move whose check failed, or one to the build already active, that is cancelled before its result is saved ends as `cancelled`, and one that had been switched ends as `done` with `cancel_refused`.

A move whose cancel won ends as `failed` only when Docker refused to remove an instance of the release and it is still there. `error` then says that the cancel won and names what could not be removed, and a cancel asked again answers `409` with the same words, never that the cancel lost. That the cancel won is read from `release_verdict`, `abandoned`, and not from the words of `error`. Of a move that failed with a cancel asked before the verdict was kept, the page and a repeated cancel give `error` and say the gate's answer was not kept. While the host cannot be read, such a move stays `working`. For a retire or maintenance, putting back that fails or leaves a part wrong, such as a database server that had to be killed to stop it, ends the request as `failed` and `error` says which.

The answer of a cancelled request has `put_back`, each thing done to put the lane back; `data_sets_kept`, the data sets the request made, which stay; and `findings`, every sentence the judgement had for the lane when the cancel ended. What else is wrong or unfinished on the lane is not the cancel's verdict: it is in `findings` and on the page. `put_back` is told from the events of every attempt. A command for an instance or a background service is listed where Docker answered that it did it, so a stop and a removal are two entries, and one whose answer was lost when Launchplane was killed is not listed. Anything else is listed once the host had made the change it names. It is history and decides nothing. `logins_not_read` lists each database server whose logins, or their holders, could not be read while the lane was put back.

Where the point is, and what the request made, are read from the host each time, never from the events. What a release made carries its generation, so nothing an earlier request left is taken for it.

| Request | Too late once | Put back before that |
| --- | --- | --- |
| A move | The routes file is renamed for this request's generation. Whether the proxy has loaded it yet makes no difference. A move to the build already active is never too late, and a cancel of it changes nothing. | Each background service and each instance of this generation is removed, as after a failed check, and then the logins no container holds. In a lane in maintenance a move makes nothing, so nothing is removed. |
| A retire | The lane's routes file is gone. Removing it is the first thing a retire changes, and nothing after it can be put back: the containers are removed, and Launchplane does not make a removed instance again by itself. The rest is finished as asked, except that a data set named for deleting is kept when the cancel came before it was deleted: `cancel_refused` says so, and the answer lists it in `kept_data_sets`. | Nothing: nothing was changed. |
| Beginning maintenance | The routes file points at the back soon page. That is its first change too. Ending maintenance brings the lane back. | Nothing: nothing was changed. |
| Ending maintenance | The routes file points at the instance again. | What it started is stopped again, the database server last, as after a failed check. Every part of the lane is then stopped, and the cancel answers for each of them. |

Putting a lane back never deletes data: a data set the request made, and a database server it started, stay, as retiring leaves them. A cancelled first move of a product with data is still `cancelled`, and names the data set in `data_sets_kept`.

| Status | Body | Meaning |
| --- | --- | --- |
| `200` | the request | It is cancelled, now or before. |
| `202` | the request | It is being worked. For a move the cancel has won at the gate and the lane is being put back. For another call it is marked, and may still be found too late. `cancel_asked_by` says by what. |
| `409` | `error` | It is not cancelled and will not be: it was too late and the answer says why, or it had ended as `done`, `failed` or `replaced`. Nothing was changed by this call. |
| `404` | `error` | The record holds no such request. |
| `503` | `error` | The record could not be reached, or the cancel is recorded and the gate gave no answer. The answer says which. |

With `?wait=<seconds>` the call answers as soon as the answer is known: the request is cancelled and its lane put back, its cancel was refused, or it has ended.

## Giving up

`POST /requests/<id>/give-up` ends a request at work as `failed`. It is no force: it is for a request that has nothing left to do, or has done nothing, and that the host keeps from ending. The rule is the same for a move, a retire, and beginning and ending maintenance. A give-up that is refused leaves the route, the proxy and every part of the lane as they were.

In order:

1. **It looks at this lane only, and starts nothing.** A request with an attempt open is being worked at that moment, and is refused: one the host keeps from ending is between two attempts for 5 s at a time, so ask again. A stopped proxy is not started, and the give-up is refused.
2. **It fences the request's attempts at [the gate](the-move.md#the-gate),** as taking a request up does. A switch or a change of the route that an attempt sent, and that has not run yet, changes nothing when it does. A refused give-up may leave them fenced, which changes nothing for the request: its next attempt is a new one.
3. **It asks the gate for the request's release.** A move the gate answers `committed` for has switched and is finished as asked, and is not given up.
4. **It reads the lane.** Give-up goes on only where the lane is as the request would leave it, or as it was before the request:
   - **As the request would leave it:** the request's own planner, asked what it would do on what the host shows now and stopped before it acts, has nothing left to do.
   - **As it was before the request:** for a retire or a maintenance call the planner, turned back as for a cancel, has nothing to put back and refuses no cancel as too late. For a move the host shows nothing its release made: no instance, background service or data set carries its generation.
   - Anything that was not read refuses the give-up: the host, the proxy, a stamp, or the logins of a database server of the lane. For a move, so does a login no container holds.
5. **It ends the request,** unless a cancel was asked of it or it was taken up again meanwhile. A request a cancel was asked of is the cancel's to end.

| Status | Body | Meaning |
| --- | --- | --- |
| `200` | the request | It ended as `failed`. `error` ends with `A person gave it up`. Nothing on the host was changed. |
| `409` | `error` | It is not given up and will not be by asking again: it waits, and is [cancelled](#cancelling) instead; it has ended; a cancel was asked of it; or the gate has decided its release. `error` says which. |
| `404` | `error` | The record holds no such request. |
| `503` | `error` | It is not given up now, and `error` says why: it is being worked at this moment; the proxy is stopped; the lane is part way; something could not be read, with the real error; or it was taken up again, or a cancel was asked of it, while the lane was read. The request is still at work and is taken up again by itself. |

## The workers

Four workers run in the Launchplane process. Each takes the oldest request that is waiting or working on a lane no other worker is on, and carries it through with the same judgement and the same steps as before. So requests for different lanes are worked at the same time, four lanes at most, and the requests for one lane one at a time, in the order they came. Four is the lanes of the finish-line test: two products, each with testing and production.

- **A lane's turn** is a PostgreSQL advisory lock held by a connection of its own for as long as the request is worked. When the work ends or Launchplane dies, the connection closes and the turn is free. No transaction is open and no row is locked while Launchplane talks to the container host. Before each action on the host the worker asks that connection whether it still holds the lock, and when it does not it stops without acting and writes nothing more; the request is then taken up again as interrupted.
- **After a restart, with nobody calling,** a request found working with nobody holding its lane's turn was interrupted. The worker ends that attempt as `interrupted`, begins a new one, reads the host and decides from what is there, as asking again always did. It does not go on from where the last attempt was.
- **A request is taken up again however often it was interrupted.** No count ends a request or stops a lane. A lane whose request is at work takes a new request, which waits behind it. A request ends by finishing, on a refusal the host answered, by a [cancel](#cancelling), or by a [give-up](#giving-up). `attempts` and the page say how often it was taken up, and why the last attempt did not end it.
- **A Launchplane that keeps dying is stopped by systemd, not by a count of its requests.** `deploy/launchplane.service` sets `StartLimitBurst=10` in `StartLimitIntervalSec=300`: after ten starts in five minutes the next is refused, the service is left `failed`, and Launchplane is down, which is what a person sees. A healthy start answers in 1 s, so a request that makes Launchplane die each time it is taken up reaches the limit in about 15 s. `systemctl reset-failed launchplane && systemctl start launchplane` on the control container starts it again and forgets the starts counted. A deploy does the same, so no deploy is refused for the starts before it.
- **A request that cannot be ended yet is left open,** whichever of the four calls it is. Its attempt ends as `unfinished`, `error` says which read or change got no answer, the page shows that under the request, and it is taken up again after 5 s, ahead of newer work on its lane. The next attempt reads the host and goes on from what is there. That is so when the host or the proxy could not be read, when a change was sent and its reply was not had, when the proxy has two routers for the web address or still sends visitors to what would be stopped, and after 50 steps. A request ends only on a result it read, on a refusal the host answered, or by a person, so one the host keeps from ending is worked for as long as that lasts, and says why on the page: a refusal ends it as `failed` with the host's own error, also before anything was changed. For a move, see [how a request may end](the-move.md#what-it-does).
- **A change of a lane's route that was sent and has not run cannot arrive under a later request.** A retire and beginning or ending maintenance change the route by an [edit at the gate](the-move.md#the-gate). When a request is taken up, its attempts are read from the record under its lane's turn. One that has an earlier attempt is fenced through it before it reads its lane, since a Launchplane that was killed may have sent an edit. So a command Docker accepted and that runs late has either been made by then, and is read with the lane, or changes nothing. An attempt that sent an edit and got no answer never ends its request: the attempt ends as `unfinished`, and the next one fences it, reads the route and goes on from what is there, to the end of the maintenance or the retire. A request on its first attempt asks the gate for no fence. While the gate gives no answer to a fence nothing is done on the host, the attempt ends as `unfinished`, and the lane takes no other request. The gate is a command in the proxy's container, so a proxy that is not running is started to ask it. Giving a request up fences its attempts too, before it reads the lane.
- **Nothing else is run again by itself.** A request that fails stays failed until someone asks again.
- **Losing the record while working** stops the work at the next thing to write. The request stays working, and when the record is back it is taken up as interrupted.
- **A restart of Launchplane while a request is working**, a deploy included, is an interruption like any other: the request is taken up again.

## What lanes share on the host

A worker acts only on its own lane's containers, data sets and routes file. What two lanes share is protected for as long as it is touched, and no lock is held across the rest of a lane's work:

| Shared | What protects it |
| --- | --- |
| The proxy and back soon page containers, the `launchplane-lanes` network and the proxy's outside network | One lock in the Launchplane process, held while one of them is looked for and made. Two lanes that find the proxy missing make it once. |
| The folder of routes files | Each lane has its own file, put in place by a rename. Each change is one command of [the gate](the-move.md#the-gate), which holds its lock on the host for that command. One lock in the process is held while the folder is read whole and while a gate command runs, so no reading finds the folder mid-change. |
| What the proxy has loaded | Only read. Traefik loads every lane's file itself. |
| Images | Docker, which pulls one image once for two callers. |
| The secrets Launchplane cleans from what it says | One set for every worker and call. A password is added to it as it is made, before Docker or a database server is given it, so no worker can write another's secret to the record or a log. |
| The record | PostgreSQL: up to 10 connections for every call and worker, and one more for the turn of each lane being worked. |

## What the record holds

Four tables in a PostgreSQL database of Launchplane's own: a request, an attempt, an event, and the digest of each key a request was answered for. It holds what was asked and what Launchplane did. It never holds which build a lane runs, which instance receives visitors, whether a part is healthy, or how far a request has got: those are read from the host every time. With every event deleted, Launchplane decides exactly as before.

No secret is written to the record, because the record outlives the container that holds a secret and nothing could then know to remove it on the way out.

A value is judged as an answer is: on the value itself, whatever characters the secret has, against every secret a container holds.

- **What a caller sent** is refused with `422` when it carries one, and nothing is written: the product, lane, build and data set name of a call, and who asked, also when a request is asked for again.
- **What Launchplane writes itself** is cleaned of it: every event, error and answer. The worker reads every container's secrets when it takes a request up, and a secret it makes while working is known from the moment it is made.
- The caller's key is kept as a digest only.
- `asked_by` is the address the connection came from. Launchplane does not read a forwarding header, which is text a caller chooses.

## On the page

- A lane's card has a row for each of its requests that has not ended: what was asked, how it stands, and for the one being worked what it has done so far. Once an attempt of it is over, the row also says how many times it was taken up, that it is taken up again by itself until it ends, and why the last attempt did not end it: Launchplane stopped or lost its record, or the reason the host gave. One a cancel was asked of says by what, and for a move what the gate answered for it when the page was read: that the cancel won, that it came too late, that the gate has not been asked yet, or that the gate could not be asked.
- A request a cancel was asked of stays on its lane's card for 10 minutes after it ended, however it ended: as `cancelled` with by what and what was put back, or as `done` or `failed` with why the cancel was not carried out. A cancel that won and whose lane could not be put back shows the error that was saved: that it won, and what could not be removed. A request that was replaced stays for the same 10 minutes, as `replaced` and saying by which request; it is not shown as something wrong. `/running` has these in `cancels`.
- The request a lane ended last stays on its card for the same 10 minutes when no cancel was asked of it, with how it ended and every line of its events, so the last of them can be read once it has ended. It is not in `cancels`. One that ended `done` or `failed` with a cancel asked of it shows its lines after the cancel's note, also a move whose cancel won and could not be put back, and one that ended `cancelled` shows the lines of what it sent there: its changes of the route and its commands. A request that ends while the status is being read shows once, as ended, on the page and in `/running`.
- A request for a lane with nothing on the host yet has a card of its own.
- A lane is `work under way` while a request for it stands as working in the record. `work` and `a_move_is_at_work` in `/running` come from there too.
- When the record cannot be read, or does not answer a reading within 6 s (3 s to reach the database and 3 s more to answer), the first card says so with the error. The host is still shown, and no request is. Whether a request is at work on a lane is then not known, on either of the two readings of the record a status makes: each lane says so, its condition is `could not be read` unless something in it is wrong whatever a request is doing, and a finding that would be under way or left unfinished shows as `could not be read`. `/running` has the error in `record_error`, an empty `requests`, and `a_move_is_at_work` as `null` for every lane.

## The database

PostgreSQL 17 from Debian's packages runs on the control container as the systemd service `postgresql`. `./scripts/deploy-control` installs it when it is missing, makes the database `launchplane` and the login `launchplane`, and runs the migrations with Alembic before it restarts Launchplane.

- The login has no password and is not a superuser. It is used through the local socket by the system user `launchplane` only. It owns the database, and no other login but the superuser `postgres` may connect to it.
- PostgreSQL listens on `127.0.0.1` and `::1` only.
- `LAUNCHPLANE_DATABASE` names the database, by default `postgresql:///launchplane?host=/var/run/postgresql`.
- A migration is a file in `migrations/versions`, written as SQL. The one that turned keys into digests cannot be undone, and going down past it refuses and says why. Undoing the one that added `cancelled` turns each cancelled request into a failed one, and undoing the one that added `replaced` turns each replaced request into one cancelled by the request that replaced it. The one that lets a lane have one waiting request ends every waiting request of a lane but the newest as replaced by the newest. Undoing the one that keeps every key a request was answered for leaves each request one of its keys. The one that added `generation` gives each request written before it a generation made from chance. It stops, with nothing changed, while a move begun before it is working or needs a person: the Launchplane that began it settles it first. The one that ended the limit of interruptions turns each request that stood as `needs a person` into one that is working, which is taken up again; undoing it turns none back. The one that added the lines of a change of the route and `release_verdict` changes no row: an event and a request written before it have `null` there and are shown as they were written.

## Known and not fixed

- **The database is not backed up.** Losing it loses the requests and their history, and no lane: what runs is on the host.
- **`asked_by` is an address, not a person.** Who may ask is not settled yet.
- **Who may cancel is not settled.** Any caller can, as any caller can ask.
- **A routes file that is not Launchplane's, on a lane with nothing else on the host, does not show on the status page.** A lane is shown from its instances, background services and data sets, and from the routes the proxy has loaded.
- **A loaded route whose target is not a kind Launchplane knows makes the whole reading of the proxy fail.** `/running` and every call then answer `503` for every lane, until that route is removed from the proxy by hand.
- **A lane is put back after the action under way.** Who wins is decided at once at the gate. The worker asks the gate between actions, and one action can take as long as a check, 20 s, or a stop, 30 s.
- **A cancel asked of a retire keeps the data set it named, also when the rest was too late to cancel.** The direction session chose this as the safe side; Chris may reverse it.
- **Ending maintenance that is cancelled answers for every part of the lane,** also one that was wrong before it began: which of them it started, the host cannot show, since a start changes no label.
- **A data set carries its generation on its network only,** the first part of it that is made.
- **A part made before generations carries none,** so a cancel leaves it.
- **An instance left by an earlier request that a move retired before it was cancelled is not brought back.** It received no visitors.
- **A retire or a maintenance call that ends between the cancel and its worker's next reading ends as it would have.** The cancel call then answers `409` and says how it ended.
- **Only a move's cancel is decided at the gate.** A second Launchplane on the same host is held off each lane by the lane's turn. Every change of a routes file, and a release's cancel, are held off each other on the host. The proxy, the back soon page and readings of the routes folder are protected in the process only.
- **A retire names the route it removed from its last attempt alone.** Taken up again, it leaves out the route it removed when that removal had no reply: a removed routes file names nobody. A data set it deleted is named all the same, and the containers it removed are listed from its lines across attempts. A move's `did`, an ended maintenance and what a cancel put back are told across attempts too. The host is as asked, and what each attempt did is in its events.
- **A stop that raced a part stopping by itself can be put down to the request.** Docker's client does not say whether a stop or a start changed anything, so Launchplane goes by what it read just before: a part read as running that stopped by itself before the stop arrived is written as stopped by the request. The same holds for a start.
- **A container this request made, removed and made again shows one make to a later attempt.** A make with no answer is then not put down to the request when a later make of the same name was answered.
- **A make that reaches Docker late can be missing from the account.** When it lands after the next attempt has read the host, that attempt's own make of the same name is refused, and the container is then put down to neither line: the later line was answered, with a refusal, and only the last make of a container can be read back.
- **When Docker makes a container and its client's read of it afterwards fails, the line says `no answer` where Docker had answered.** The client gives one error for the two calls. The make is put down to the request when the next attempt reads the container.
- **A start, a stop or a removal whose answer was lost is never put down to the request,** also when the request did make it. Its line stays `may have been sent, no answer`. A move leaves it out of `did` and names it in `not_answered`, and can answer `unchanged` beside it, as it did before these lines.
- **If Docker restarts after the gate has written a route and before its result can be read, the request can end `failed` with the route installed.** Docker then answers that it does not know the command, which Launchplane takes for a refusal the host answered, so the request ends and no later attempt reads the route back. The line stays `may have been sent, no answer`.
- **A request the host keeps giving no answer to stays working.** It is taken up again every 5 s and its lane takes no other request until the host answers or a cancel ends it. Nobody is told: it shows on the page with why. A cancel ends a move before its switch, and a give-up ends a request only on a lane that is not part way.
- **A failure Launchplane does not know ends any request as `failed` where it is,** a retire or a maintenance too, with the lane as that moment left it. It is a mistake in Launchplane, and it is not tried again by itself.
- **A retire cancelled part way through deleting a named data set,** after its volumes are gone and before its network is, says the data set was kept and lists it in `kept_data_sets`.
- **A retire killed after it deleted its named data set and before that was written to the record** ends `failed` and says `Nothing was deleted`. The data set is gone.
- **A change other than a route edit that Docker accepted and runs late is not stopped.** When its reply was lost and the request went on, a late start can bring a part up behind the back soon page, and one sent by a Launchplane that then died can land after a give-up looked at the lane. The line of such a command says it may have been sent, and a give-up does not wait for it.
- **If Docker runs a route edit and then cannot say what it did,** because its daemon restarted in between, the request ends `failed` as though nothing was sent.
- **A request refused by Docker half way ends `failed` half way.** A stop or a removal Docker answers no to, after the route was changed, ends the request with Docker's error and leaves the lane as it is. Asking again goes on from there.
- **A repair that removed a stopped background service and then lost its next read can be given up with the service missing.** Nothing on the host shows what the lane had before the request. The removal has its line in the request's events, and a give-up does not read it.
- **A Launchplane that dies again and again, and fewer than ten times in 300 seconds, is started again every time.** systemd's limit counts only starts that close together.
- **systemd says `failed`, with the result of the last death,** not that its limit was reached. `journalctl -u launchplane` has `Start request repeated too quickly`. Measured on systemd 257.
- **A reading given up on keeps its connection until the record answers.** At most 4 readings wait at once, so the workers keep connections.
- **A worker can lose its lane's turn while one action on the host is under way.** The turn is asked before each action, not during it. An action can take as long as a check, 20 s, or a stop, 30 s. If the lock's connection ends in that time and another Launchplane takes the lane, that one action still finishes. With one worker in one process this needs a lost connection and a second Launchplane.
- **A secret no container holds any more is not known to be one.** A caller who sends the password of a container already removed has it stored as sent, in a build or a data set name.
- **A request that arrives at the moment a worker takes up the request waiting on its lane is held for 1 s.** PostgreSQL finds the two waiting on each other and ends one. Either the waiting request is replaced and never begins, or it is worked and the call answers `503` saying that whether its request was written is not known; it was not. The same was true of a newer request before the waiting place.
- **The pretend record of the tests lists ended requests newest id first.** PostgreSQL lists them by when they ended, so a request that was replaced comes after the older request that finished later; the pretend record puts it before.
- **Untried:** two Launchplane processes on one record. The lane's turn is built for it and tested against PostgreSQL, not on the test bed.

## Measured

On the test bed on November 17, PostgreSQL 17, one nginx lane.

- A call answered `202` in 20 to 25 ms. The worker took the request up 4 to 6 ms after it was asked.
- A move from one build to another, with `?wait=`: 6.2 to 6.4 s, as before requests were kept. Asking again for a finished move: 0.12 s, where it was 0.07 s; the wait reads the record every 0.1 s.
- Killed with `kill -9` while the new instance was being checked: systemd started Launchplane again, the request was taken up 1.2 s after the kill with nobody calling, and it ended `done` and `switched` 7.5 s after it was asked, with two attempts, the first `interrupted`.
- The 58 test-bed tests passed in 21 minutes.

With each lane its own turn, on November 17, nginx lanes:

- One lane moved from one build to another in 6.3 s. Two at once took 6.3 s, four at once 6.8 s, and six asked at once 12.2 s: four, then two.
- On November 20, with the gate and 3,188 files in its folder: one lane moved in 5.9 to 7.0 s over ten moves, and two at once in 9.6, 10.8 and 9.3 s. Before the routes were read without the gate's files, two at once took 10.5 s. `tests/test_bed_lanes.py` reports that time and does not judge by it.
- A call answered `202` in 50 ms from another machine.
- A move cancelled by the call while its new instance was being checked ended as `cancelled` 2.4 s later, with the instance removed. The web address answered from the old build throughout, and no request failed.
- On November 20, a move held before its switch while a second and a third request arrived for its lane: the second ended `replaced` at once with no attempt, the first ended `switched` 2 s after the hold was let go, and the third `switched` 5 s after that. A probe every 0.5 s was answered by the old build, then the first build, then the third, and never failed.
- Cancelled, then killed with `kill -9` before its worker read the mark: after the restart the request was taken up and ended `cancelled`, with two attempts, `interrupted` and `cancelled`.
- With every PostgreSQL process stopped with `SIGSTOP`, `/running` answered `200` in 6.2 s with the host and `record_error`. Restarting Launchplane then took 3 s.
- Docker's client warned once in 22 minutes of tests that its pool of 10 connections was full. It opens another connection then, and nothing failed.
- On November 20, started by systemd, Launchplane listened 0.85 s later in three starts of three, and answered `GET /requests` within 1.0 s.
- Killed with `kill -9` as soon as it answered, eleven times in 16 s: systemd started it ten times and refused the eleventh, 0.1 s after the kill, and left it `failed`. Three seconds later it was still down. `systemctl reset-failed launchplane && systemctl start launchplane` had it answering again.
- A move held before its switch and killed four times was `working` with attempts `interrupted` four times and a fifth open, and the page said `Taken up 5 times`. Let go, it ended `switched` with one rename, and a probe of the web address never failed.
- The 63 test-bed tests passed in 22 minutes 30 s, one after another. None ran side by side: tests in five of the seven files kill Launchplane, stop PostgreSQL, restart Docker or stop the proxy, which reaches every lane. Lanes are worked side by side inside the tests of `tests/test_bed_lanes.py`.

## On the test bed

```
LAUNCHPLANE_TEST_DATABASE=postgresql://postgres:test@127.0.0.1:55432/postgres uv run pytest
LAUNCHPLANE_URL=http://launchplane.shiny:8700 uv run pytest tests/test_bed_requests.py tests/test_bed_lanes.py -v
```

The tests of the record, the worker and the calls run against a pretend record in memory, and again against PostgreSQL when `LAUNCHPLANE_TEST_DATABASE` names one a test may empty. `docker run -d -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:17-alpine` gives one. CI runs both.

`tests/test_bed_requests.py` stops PostgreSQL on the control container for one test and starts it again, and freezes it for another. A third kills Launchplane until systemd leaves it failed, and starts it again with the command above. Every other test that kills the plain service has systemd forget the starts counted first, so none is refused a restart for the kills before it. `tests/test_bed_lanes.py` moves two lanes at once and cancels a move two ways. Every test-bed test waits for each request with `?wait=`. A kill test either lets Launchplane take the request up again by itself, or ends the request in the record while Launchplane is down, to leave the lane as a request that failed halfway would.
