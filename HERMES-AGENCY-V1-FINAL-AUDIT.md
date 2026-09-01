# Hermes Agency — V1 Final Audit

**Date:** 2026-09-01
**Scope:** release-candidate audit. Deploy, version, verify, test, document.
No new features were added in this phase.

---

## Readiness classification

> ### CONTROLLED TESTING
>
> Three things block the upgrade, all named in §24. One is a security fix that
> is written, tested and pushed but **still not running**; one is a workflow
> gap; one is simply a lack of evidence.

The pipeline is complete and every safety mechanism has been verified against
the live system. Both edge cases found by this audit are now fixed and covered
by tests (§23a). What remains:

1. **MailHub commit `e6d8e21` is not deployed.** Until it is, the `suppress`
   scope is not enforced (§2).
2. **Nothing advances `READY_TO_SEND` to `SENT` automatically** — the
   orchestrator has no cron, so a confirmed send needs a manual tick (§24.9).
   That is a genuine workflow gap, not just missing evidence.
3. **Twelve leads and three real messages** say nothing about deliverability,
   reply rate or spam placement.

BULK PRODUCTION is not appropriate while (2) stands, regardless of how much
volume is run.

---

## 1. Deployed Hermes commit

| | |
|---|---|
| Repository | `AbhijitDengale/hermes-render-mtgt69p2` |
| Commit | `9bf3009` — *Bring the repository level with the running agency, and make it installable* |
| Previous | `508736e` |
| Agency code on the box | byte-for-byte identical to `9bf3009`'s `agency/` |

Verified by installing from the repository onto the running container:
`install-agency.py` reported **62 ok, 0 warnings, 0 failures**, and reported the
same on a second run.

## 2. Deployed MailHub commit

| | |
|---|---|
| Repository | `AbhijitDengale/Auto_Email` |
| **Running** | `ea77499` |
| **Pushed, not deployed** | `e6d8e21` — *Gate suppression behind its scope and fix per-tenant opt-out* |

**This is the release blocker.** `e6d8e21` is on `origin/main` and Render has not
picked it up. Confirmed by reading the running container directly:

```
grep -c 'SUPPRESSION_REASONS' /srv/app/main.py        -> 0
grep -c 'require_scope(caller, "suppress")' ...        -> 0
```

Its database migration (`20260901000005_suppression_tenancy.sql`) **is** applied
to production and is backwards-compatible, so nothing is broken in the meantime.
But until the code deploys, any API key belonging to an admin or owner can add
suppressions and cancel queued mail regardless of its scope.

## 3. Hermes version

```
Hermes Agent v0.20.6 (2026.8.27) · upstream 5fc308a7
Python 3.13.5 · /opt/hermes
```

## 4. Render services

| Service | ID | Role |
|---|---|---|
| hermes | `srv-daah83u7bikc738fngjg` | 7 agent profiles, gateway, cron, `agency.db` |
| mailhub | `srv-daark9bbc2fs738gd11g` | Gmail send/receive, queue, suppression, API |

Persistent disk `/opt/data` — 4.9 G, 23% used. `agency.db` is 448 K.

## 5. The seven profiles

`default` (MAYA), `nova`, `aria`, `sentinel`, `echo`, `leo`, `orbit`.

One gateway serves all seven: `gateway.multiplex_profiles: true`. Confirmed in
the log —

```
Multiplex cron scheduler started for 7 profile(s):
['default', 'aria', 'echo', 'leo', 'nova', 'orbit', 'sentinel']
```

and demonstrated in practice: ECHO's cron has fired 73+ times while ECHO's own
gateway shows `stopped`.

## 6. Permissions matrix

Read off the live configuration, not from intent.

| Agent | MCP servers | Credentials | Can send? | Can approve? | Can suppress? |
|---|---|---|---|---|---|
| **MAYA** (root) | `mailhub`, `render` | MailHub key `read,queue,suppress` | **yes** | no | yes |
| **NOVA** | `agency`, `research` | Steel key | no | no | no |
| **ARIA** | `agency` | none | no | no | no |
| **SENTINEL** | `agency` | MailHub key `read,approve` | no | **yes** | no |
| **ECHO** | *(none)* | *(none)* | no | no | no |
| **LEO** | `agency` | MailHub key `read,suppress` | no | no | yes |
| **ORBIT** | *(none)* | MailHub key `read` | no | no | no |

Ten invariants, all passing:

```
ok  ECHO holds no MailHub credential          ok  NOVA holds no MailHub credential
ok  ECHO has no MCP server at all             ok  MAYA has the mailhub MCP server
ok  ARIA has no research/browser MCP          ok  SENTINEL has no mailhub MCP server
ok  ARIA holds no MailHub credential          ok  LEO has no mailhub MCP server
ok  NOVA has research (Steel) access          ok  ORBIT has no MCP server at all
```

**No key holds both `approve` and `queue`.** That separation is what makes the
QA gate structural rather than a polite instruction: the agent that reviews the
copy physically cannot be the one that sends it.

Nothing gained permissions during Phase F/G. The two credentials minted in this
phase are both narrower than any that existed before:

| Key name | Scopes | For |
|---|---|---|
| `orbit-read` | `read` | ORBIT sender health |
| `review-suppress` | `read`, `suppress` | the human-review CLI's `dnc` |

(Key prefixes and values are deliberately not recorded here — this repository
is public. Both keys are listed under **API access** in the MailHub dashboard.)

Verified against the live API: the read-only key gets `200` on `/accounts`,
`403` on `/messages` (*"this key does not have the 'queue' capability"*) and
`403` on `/approvals`.

## 7. MCP integrations

| Server | Where | Purpose |
|---|---|---|
| `mailhub` | MAYA only | queue, message status, accounts |
| `agency` | nova, aria, sentinel, leo | role-scoped pipeline tools |
| `research` | nova only | Steel scraping |
| `render` | MAYA only | Render platform tools |

`agency_mcp.py` is role-scoped by `AGENCY_ROLE`, so the same file exposes
different tools per profile: NOVA gets `get_assignment`/`save_research`, ARIA
`get_assignment`/`save_draft`, SENTINEL `submit_verdict`, LEO
`submit_classification`.

## 8. Steel status

**Working.** `health_check` returns `{"provider": "steel", "healthy": true}`.

Scrapes verified live during this audit: basecamp.com (3,756 chars),
plausible.io (6,449), linear.app (7,864), posthog.com (2,151).

Two things worth recording, because both looked like Steel being broken and
neither was:

- A transient `403 error code: 1010` (a Cloudflare browser-signature ban at
  Steel's edge) affected every request for a few minutes and then cleared on its
  own. It was not the API key — a deliberately invalid key returns a clean
  `401` with a helpful message.
- `cadenceworks.com` returns `HTTP 500` from Steel consistently while other
  sites succeed. That is a per-site failure, not an integration failure.

The second one produced the most reassuring result in this audit: NOVA hit the
500, **refused to fall back on what it already knew about the company**, and the
lead was escalated to `HUMAN_REVIEW` with `research failed: http 500`. The
evidence protocol in NOVA's SOUL worked exactly as written.

## 9. MailHub status

Running, healthy, `dry_run` off. One connected mailbox, `warming`, daily cap 5.

Warmup and rate-limit policy is **unchanged** — no cap was raised at any point
during this phase.

## 10. State machine

**90 legal transitions across 21 states.** Every expected state is present, none
is unreachable from `NEW`, and `CLOSED` is the only terminal state.

Every transition the code depends on exists:

```
ok  FOLLOWUP_WAITING -> HUMAN_REVIEW      ok  FOLLOWUP_WAITING -> REPLIED
ok  SENT -> HUMAN_REVIEW                  ok  REPLIED -> HUMAN_REVIEW
ok  SENT -> FOLLOWUP_WAITING              ok  HUMAN_REVIEW -> CLOSED
ok  FOLLOWUP_WAITING -> FOLLOWUP_PENDING  ok  HUMAN_REVIEW -> UNSUBSCRIBED
ok  FOLLOWUP_PENDING -> SENT              ok  HUMAN_REVIEW -> FOLLOWUP_WAITING
ok  QA_PENDING -> QA_REJECTED             ok  QA_REJECTED -> COPY_PENDING
```

`FOLLOWUP_WAITING -> HUMAN_REVIEW` (migration 003) is the one added for the
vague-out-of-office case. Without it the escalation was silently swallowed and
`blocked_reason` returned `None`, which meant a follow-up would have been sent
to somebody who had said they were away.

**No dead or unreachable states.** One note, not a defect: `UNSUBSCRIBED` leads
only to `CLOSED`. That is deliberate — there is no route from an opt-out back
into outreach.

### Out-of-office behaviour, as currently implemented

- **A human reply** cancels every scheduled follow-up first, *then* the lead
  moves to `REPLIED` and LEO classifies. Cancellation precedes reasoning, so a
  classifier failure cannot result in chasing someone who already answered.
- **An auto-reply / OOO** cancels the currently scheduled follow-up but does
  **not** move the lead to `REPLIED`.
- If a return date can be parsed **unambiguously** — ISO format only, and only
  within a 120-day window — the follow-up is rescheduled after it.
- Otherwise the lead is paused at `HUMAN_REVIEW`. `parse_return_date` refuses
  "back Monday" and "the 3rd" rather than guessing, because guessing wrong means
  emailing somebody while they are still away.

## 11. Kanban

MAYA dispatches work by creating Kanban tasks assigned to a profile; the
gateway's embedded dispatcher spawns the agent. Observed live:

```
kanban dispatcher [default]: spawned=3 reclaimed=0 crashed=0 timed_out=0
```

`max_in_progress=8` (memory-derived default). Leases are time-bounded: a task
whose worker dies is reclaimable, verified by regression check 7.

## 12. Cron jobs

Three, each deterministic (`--no-agent`), **no duplicates**.

| Name | ID | Home | Schedule | Script | Delivery | Runs | Last | Failures |
|---|---|---|---|---|---|---|---|---|
| `echo-followups` | `49ce35ce006c` | `profiles/echo` | every 2m | `echo_followups.py` | local | 73+ | ok | streak 0 |
| `review-alerts` | `87f459aea57b` | root (MAYA) | every 2m | `review_alerts.py` | `discord:…0898` (#alerts) | 18+ | ok | streak 0 |
| `orbit-daily` | `eeb1258ca105` | root (MAYA) | `0 8 * * *` | `orbit_daily.py` | `discord:…5817` (#maya-office) | 1 | ok | streak 0 |

No LLM decides whether a follow-up is due, whether an escalation needs a human,
or what a metric is. All three are code.

Both Discord jobs run from the **root** profile deliberately: Discord is
configured there and nowhere else. A job scheduled on a sub-profile executes
fine and then silently fails to deliver — which is exactly what happened on the
first attempt (`no delivery target resolved for deliver=discord`) and was fixed
by moving the job and giving it an explicit channel id.

## 13. Inbound filtering

Match order, strongest first: `provider_thread_id` → `In-Reply-To` →
`References` → sender + looks-like-a-reply. The last is the weakest and is
gated by a `_NEVER` regex blocking no-reply and notification senders.

Unrelated mail cannot reach LEO: without a match there is no `lead_id`, and
without a `lead_id` nothing is dispatched. Duplicates are collapsed on
`provider_message_id` (UNIQUE) — verified, two inserts produce one row.

## 14. SENTINEL gate

Structural, not advisory:

- A new draft is written with **no QA verdict**. Nothing can be queued without
  a matching approval.
- Approvals are bound to a **content hash** of `subject \x00 body`. Change one
  character and the approval no longer matches. Surrounding whitespace is
  normalised, so a trailing newline cannot invalidate a legitimate approval.
- Approvals are **single-use** — `consume` is conditional on `consumed_at IS NULL`.
- `requires_approval` **fails closed** when the owner is unknown.
- Idempotency is checked **before** the gate, so a retried send returns
  `duplicate` rather than `rejected`. Before that fix, the orchestrator would
  have retried a message that had already gone out, forever.

During Phase F, SENTINEL rejected one of ARIA's follow-ups unprompted for using
a deceptive `Re:` prefix on a thread that had never had a reply.

## 15. Follow-up system

ECHO holds no MailHub credential and cannot send. When a follow-up is genuinely
due it hands the lead to MAYA by moving it to `FOLLOWUP_PENDING`; the normal
pipeline takes over and the follow-up gets its own SENTINEL approval like any
other message.

`blocked_reason` is evaluated **live** at tick time, not when the follow-up was
scheduled, so a reply that arrived in between stops the send.

Dispatched follow-ups are marked `dispatched` with a timestamp; `due()` only
selects `scheduled`, so a stage cannot be re-dispatched and `attempts` stops
incrementing. Cancellation behaviour is unchanged: only `scheduled` rows cancel.

## 16. LEO

Classifies inbound replies. Holds `read,suppress` — it cannot queue and cannot
approve. `ALWAYS_HUMAN` classifications bypass automatic handling entirely.
Cancellation of pending follow-ups happens **before** LEO is dispatched.

## 17. HUMAN_REVIEW

Six audited actions, all deterministic:

| Action | Effect |
|---|---|
| `approve` | records intent — **does not send**; a reply still needs SENTINEL |
| `reject` | nothing is sent |
| `edit` | writes a **new** draft at `stage+1` with no QA verdict |
| `close` | cancels follow-ups, lead → `CLOSED` |
| `dnc` | cancels follow-ups, lead → `UNSUBSCRIBED`, **and** suppresses in MailHub |
| `resume` | only where the state machine permits it |

Every action writes to `audit_logs` **and** `events`. Acting twice on one
escalation is refused. A `CLOSED` lead cannot be resumed.

`dnc` reaching MailHub's suppression list matters: our own state is not enough,
or a different campaign could mail the same person tomorrow.

## 18. Discord delivery

Working and verified end to end — `last_delivery_error=None` on both jobs.

Escalation alerts carry company, lead id, campaign, state, LEO's classification
and confidence, an excerpt of the reply, the suggested action, the draft, and
the exact commands to respond. Each escalation is announced **once**:
`notified_at` is set in the same breath as the output.

Alerts are paginated at `REVIEW_ALERTS_PER_TICK` (default 5). Across ticks no
escalation is ever announced twice — verified with nine escalations over two
ticks, then silence.

## 19. ORBIT

Read-only. Every figure is a SQL query; no model produces a number. Sources are
`agency.db` and MailHub — the obsolete `agency.db` mail tables are not read.

It may recommend pausing a sender; it will not do it. During this audit it
reported the 5/5 warmup cap as *"allocation is the limit, not a fault"*, which
is the correct reading.

Below `ORBIT_MIN_SAMPLE` (20) it refuses to name a best campaign or niche and
labels every rate `insufficient data`. Rates are `None`, never `0.0`, when
there is no denominator: zero replies out of zero sends is an unknown rate, not
a zero one.

**Every rate is per-lead-contacted**, with numerator and denominator drawn from
the same population, so no rate can exceed 100%. This was not true when Phase G
first ran — the first live report said **"Reply rate: 400.0%"**.

## 20. Source-of-truth ownership

| `agency.db` owns | MailHub / Supabase owns |
|---|---|
| leads, campaigns | sender accounts |
| pipeline state, `state_transitions` | outbound queue |
| research, drafts, workflow data | provider message + thread ids |
| followups | inbound mail |
| human escalations | suppression |
| events, audit logs | delivery state |

Three obsolete tables survive in `agency.db` from before the split:
`sender_accounts`, `send_jobs`, `suppression`. **All three are empty (0 rows).**

No production code reads or writes them — every `suppression` reference in
`agency_mcp.py`, `review.py` and `orbit.py` is a call to MailHub's
`/api/v1/suppression`, not a local table. The only code touching them is
`test_scenarios.py`, a legacy suite now marked as such.

They are marked **DEPRECATED / NON-AUTHORITATIVE** in `schema.sql`. Per the
finalization brief, no destructive cleanup was performed.

## 21. Git and reproducibility

Everything needed to rebuild the agency on a fresh container is committed;
nothing secret or runtime is.

**Committed:** source, SOUL definitions, schema, migrations, tests, cron
wrappers, live harnesses, the installer, documentation.

**Ignored:** `*.db` and WAL/SHM files, `cron/`, `sessions/`, `memories/`,
`logs/`, caches, `profiles/*/.env`, `.review.env`, `*.secrets`, lock and pid
files.

Two deliberate exclusions: `mailhub_mcp.py`, which the Auto_Email repository
already owns — the installer fetches it rather than forking it — and
`combined.sql`, a concatenation of two files that are both already committed.

`scripts/install-agency.py` is idempotent. Cron jobs are matched **by name**
before creation, so a redeploy cannot produce a second ECHO scheduler quietly
double-sending. Migrations are tracked in a `schema_migrations` ledger with
fingerprints, so a database migrated by hand is recognised rather than migrated
twice. Discord channel ids come from `AGENCY_DISCORD_ALERTS_CHANNEL` and
`AGENCY_DISCORD_REPORT_CHANNEL`.

`.gitattributes` forces LF. Without it a Windows checkout rewrites every file
with CRLF, the installer's byte comparison never matches, and each run
reinstalls everything — shipping CRLF Python to a Linux box.

**Recovered in this phase:** five of the seven SOUL files (`aria`, `sentinel`,
`echo`, `leo`, `orbit`) existed **only** on the persistent disk and had never
been committed. NOVA's committed SOUL was also stale — the deployed one carries
an evidence protocol that more than doubles its length. The box was treated as
authoritative in every case.

## 22. Test totals

| Suite | Result |
|---|---|
| `test_pipeline.py` | 34 passed, 0 failed |
| `test_lead_ingest.py` | 44 passed, 0 failed |
| `test_followups.py` | 57 passed, 0 failed |
| `test_regressions.py` | 29 passed, 0 failed |
| `test_orbit_review.py` | 80 passed, 0 failed |
| `test_v1_regressions.py` | 29 passed, 0 failed |
| `test_edge_cases.py` | 37 passed, 0 failed |
| `test_scenarios.py` (legacy, on the box) | 34 passed, 0 failed |
| MailHub `test_web.py` | 121 passed, 0 failed |
| MailHub `test_sender.py` | 25 passed, 0 failed |
| **Automated total** | **490 passed, 0 failed** |
| Phase F live (on the box) | 30 passed, 0 failed |
| Integrated scenario (on the box) | 22 passed, 0 failed |
| **Grand total** | **542 passed, 0 failed** |

`test_v1_regressions.py` and `test_edge_cases.py` were also run **on the box**
against the deployed code, not only locally: 29 and 37, both clean.

Three suites had pinned a subset of migrations when building their fixture
(`001` only, or a hard-coded pair). That let a fixture drift behind the real
schema, and it is why they broke the moment ingestion started using a table
migration 005 adds. They now apply every migration in order.

### The twenty regressions, each pinned by name

Every one was a real failure on a running system.

| # | Bug | Pinned by |
|---|---|---|
| 1 | SENTINEL bypassed | `test_v1_regressions` 1 |
| 2 | approval hashing not exact-content | 2 |
| 3 | approval reusable | MailHub `approvals.consume` |
| 4 | idempotency checked after approval | MailHub `sender.enqueue` |
| 5 | sent message rewritable | 5 |
| 6 | follow-up overwrote stage 0 | 6 |
| 7 | dead task not re-dispatchable | 7 |
| 8 | vague OOO did not block | 8 |
| 9 | dated OOO did not reschedule | 9 |
| 10 | unrelated mail reached LEO | 10 |
| 11 | duplicate inbound processed twice | 11 |
| 12 | reply did not cancel before reasoning | 12 |
| 13 | ECHO did not survive restart | 13 |
| 14 | dispatched follow-up reprocessed | 14 |
| 15 | ORBIT rate above 100% | 15 |
| 16 | uncontacted leads counted in rates | 16 |
| 17 | review alert repeated | 17 |
| 18 | suppression scope unenforced | MailHub `test_web` |
| 19 | suppression reason → 500 | MailHub `test_web` |
| 20 | multi-tenant suppression collision | MailHub `test_web` |

## 23. Final controlled end-to-end

One lead, real agents, no stage skipped or simulated. Recipient is the
operator's own mailbox.

| | |
|---|---|
| Lead | `L-617a6cecc3a4d6fd` — Plausible Analytics, `https://plausible.io` |
| Campaign | `C-V1-FINAL` |
| Recipient | the operator's own address (plus-addressed, so it is distinguishable) |

```
11:19:18  ingest                            NEW                 source=manual, generation 1
11:19:46  MAYA                NEW        -> RESEARCH_PENDING     admitted
11:19:47  MAYA   RESEARCH_PENDING        -> RESEARCHING          task t_d30352bd -> NOVA
11:20:40  NOVA        RESEARCHING        -> RESEARCH_COMPLETE    6 observations, each with a source_url
11:20:40  MAYA   RESEARCH_COMPLETE       -> COPY_PENDING         ready for copy
11:21:36  ARIA        COPY_PENDING       -> COPY_READY           draft M-L-617a6cecc3a4d6fd-0
11:21:36  MAYA          COPY_READY       -> QA_PENDING           sent for QA
11:22:31  SENTINEL      QA_PENDING       -> READY_TO_SEND        approval 7
11:26     MAYA        READY_TO_SEND         queued as MailHub #20
```

Every component was exercised: Kanban dispatch, Steel research, the evidence
protocol, ARIA's copy, SENTINEL's content-bound approval, and MailHub's queue
with a content-derived idempotency key.

**Current state: queued, awaiting warmup capacity.** MailHub #20 is `pending`
with `approval_id: 7`. The mailbox is at **5/5** and `sent_today` resets when
the calendar date rolls over in Postgres (UTC), roughly twelve hours after this
audit. The cap was **not** raised. The provider handoff itself is not
unproven — it has succeeded twice before with real provider ids
(`1a05bfa243ee938a`, `1a05c213cc68445b`).

### What the two failed attempts taught us

Neither was wasted, and both are worth recording because both looked like
system failures and only one was.

**Attempt 1** targeted `cadenceworks.com`, which Steel returns `HTTP 500` for
while scraping other sites happily. NOVA hit the 500 and **refused to write
anything from prior knowledge**, escalating to `HUMAN_REVIEW` with
`research failed: http 500`. That is the evidence protocol in NOVA's SOUL
working exactly as intended: a well-known fact about a business is still a
fabrication if it was not retrieved this session.

**Attempt 2** exposed a real defect — the Kanban idempotency collision now
fixed in §23a below. The lead had been deleted and re-ingested, drew the same
deterministic id, and matched the *completed* task from its first life. Kanban
returned that task, no worker spawned, and the lead sat in `RESEARCHING`
indefinitely.

A third observation, benign: ARIA wrote drafts for stages 0 **and** 1–3 in one
session rather than only the one asked for. Those follow-up drafts carry
`qa_status = NULL`, so the gate still blocks them — `queue_and_send` routes any
unapproved draft to `HUMAN_REVIEW` rather than sending it. Untidy, not unsafe.

## 23a. The two edge cases, fixed

Both were found by this audit and both are now closed, with migration 005 and
`test_edge_cases.py` (37 checks).

### A — a paused campaign now actually stops ECHO

`due()` and `blocked_reason()` never consulted `campaigns.status`, so marking a
campaign paused documented an intention and changed nothing.

`due()` now carries the campaign's status alongside the lead's state, and
`blocked_reason()` checks it **first**: if the campaign is not running, nothing
under it sends, whatever the individual lead is doing. `draft`, `paused` and
`archived` all count as not running; only `active` runs. A follow-up whose
campaign row is missing is treated as **active**, so a bookkeeping gap cannot
silently halt live outreach.

The important part is that a pause is *reversible*. A blocked follow-up is
**held, not skipped**:

- status stays `scheduled`, so resuming the campaign brings it straight back
- `attempts` is **not** incremented — a paused day is not a failed delivery
- the reason and timestamp are written to `followups.last_blocked_reason` /
  `last_blocked_at`
- an event is written only when the reason **changes**, not on every tick.
  ECHO runs every two minutes; one event per tick per held follow-up would
  bury the real history within a day.

Verified: eleven consecutive ticks against a paused campaign produced one
event, zero attempts, and no state change; reactivating the campaign made ECHO
dispatch it on the next tick.

### B — a re-ingested lead gets a fresh task generation

Idempotency keys were `agency:<lead_id>:<stage>`, and lead ids are
deterministic. The fix adds a lifecycle counter to the key:

```
agency:<lead_id>:gen:<generation>:<stage>
```

The counter deliberately lives in its own table, `lead_generations`, which is
**not** foreign-keyed to `leads` and is never cascaded — it is the memory of
how many lives an id has had. Putting it only on the lead row would have reset
it to 1 on deletion and brought the collision straight back. Migration 005
backfills every existing lead, so nothing already in the system starts a second
life at generation 1.

The global protection is untouched: within one lifecycle the key is unchanged,
so retries, restarts and concurrent ticks still collapse onto a single task.
Verified — five retries in one lifecycle produced one key; six concurrent
bumps produced six distinct generations; and the generation-1 and generation-2
keys are identical apart from the generation, which is precisely why the old
completed task can no longer answer for the new lifecycle.

## 24. Known limitations

1. **The MailHub suppression fix is not deployed.** `e6d8e21` is pushed;
   Render is still serving `ea77499`. Until it deploys, a `read`-only key can
   still suppress addresses and cancel queued mail. This is the single reason
   the classification is not higher.
2. **Warmup cap reached.** The one connected mailbox is `warming` at 5/5 for
   the day, so no further real send was possible during this audit. The cap was
   deliberately **not** raised.
3. **One mailbox, one tenant.** Nothing about multi-mailbox rotation has been
   exercised at volume.
4. **Sample size.** 11 leads, 3 real messages. No rate computed from this is
   meaningful, and ORBIT says so rather than pretending otherwise.
5. **Eight orphan inbound replies** from Phase D/E fixtures have no send on
   record. ORBIT excludes them from every rate and reports the count. They are
   test residue, deliberately not deleted — they are the record of those phases.
6. **`cadenceworks.com` cannot be scraped by Steel** (consistent HTTP 500).
   Per-site, not systemic.
7. **Steel had a transient Cloudflare block** during this audit that cleared on
   its own. If NOVA research fails in bursts, check this before the API key.
8. **No automated deploy verification.** Nothing currently notices that a push
   did not become a deploy — which is exactly how limitation 1 went unnoticed.
9. **Nothing advances `READY_TO_SEND` to `SENT` on its own.** The orchestrator
   has no cron job, so after MailHub confirms a send somebody must run
   `orchestrator.py tick` for the lead to pick up the provider id and move on.
   ECHO, review alerts and ORBIT are scheduled; the pipeline itself is not.
   Deliberately not changed here — installing a job that autonomously drives
   the whole pipeline is not a change to make silently during a release
   freeze, and it would advance the other in-flight leads too. **This is the
   one gap that keeps the classification below LOW-VOLUME PRODUCTION on
   workflow grounds rather than evidence grounds.**
10. **ARIA writes unrequested follow-up drafts.** In the final E2E she produced
    stages 1–3 alongside stage 0. They carry no QA verdict and cannot be sent,
    so this is clutter rather than risk.
11. **Credential rotation is out of scope** for this phase, as instructed.

Items 9 and 10 of the previous revision — a paused campaign that still sent,
and a re-ingested lead that could never be researched — are **fixed**; see
§23a.

---

## 25. Operational instructions

All commands run on the Hermes box:

```bash
ssh srv-daah83u7bikc738fngjg@ssh.oregon.render.com
```

Everything below runs as the `hermes` user; prefix with
`su hermes -s /bin/sh -c '…'` if you land as root.

### 26. How to import leads

Single lead:

```bash
cd /opt/data/agency && python3 lead_ingest.py add --campaign C-MYCAMP --email person@company.com --company "Company Ltd" --website https://company.com --niche saas --country UK
```

From CSV (columns: `email`, `business_name`, `website`, `niche`, `country`):

```bash
cd /opt/data/agency && python3 lead_ingest.py csv /path/to/leads.csv --campaign C-MYCAMP
```

Lead ids are deterministic — `sha256(campaign‖email)` — so re-importing the
same file does not create duplicates.

### 27. How to pause outreach

Stop the scheduler; nothing new is dispatched and nothing in flight is lost:

```bash
HERMES_HOME=/opt/data/profiles/echo /opt/hermes/.venv/bin/hermes cron pause 49ce35ce006c
```

To stop **all** sending immediately, disable the mailbox in MailHub — the
`/accounts/{id}/toggle` endpoint or the dashboard. The queue holds; nothing is
discarded.

To pause one campaign only:

```bash
cd /opt/data/agency && python3 -c "import pipeline as P; con=P.connect().__enter__(); con.execute(\"UPDATE campaigns SET status='paused' WHERE id='C-MYCAMP'\")"
```

### 28. How to resume

```bash
HERMES_HOME=/opt/data/profiles/echo /opt/hermes/.venv/bin/hermes cron resume 49ce35ce006c
```

Re-enable the mailbox in MailHub, or set the campaign `status='active'`.

### 29. How to inspect human reviews

```bash
/opt/data/agency/review list
```

Then act on one — each is audited and none of them sends anything by itself:

```bash
/opt/data/agency/review approve H-4 --note "send our rate card"
/opt/data/agency/review reject  H-4 --note "not a fit"
/opt/data/agency/review edit    H-4 --text "The reply I actually want sent."
/opt/data/agency/review close   H-4
/opt/data/agency/review dnc     H-4
/opt/data/agency/review resume  H-4
```

An `edit` is saved as a **new** draft with no QA verdict — SENTINEL must approve
your words before they can be queued.

### 29a. How to advance a confirmed send

Nothing does this on a schedule (§24.9). After MailHub confirms a send, run a
tick so the lead picks up the provider id and moves on:

```bash
cd /opt/data/agency && set -a && . /opt/data/.env && set +a && python3 orchestrator.py tick
```

Use MAYA's own credential, as above — the review CLI's key is `read,suppress`
and will be refused at the queue with a 403.

```bash
cd /opt/data/agency && python3 orchestrator.py status
cd /opt/data/agency && python3 orchestrator.py timeline <lead-id>
```

### 30. How to inspect cron

```bash
HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes cron list
HERMES_HOME=/opt/data/profiles/echo /opt/hermes/.venv/bin/hermes cron list
HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes cron runs 87f459aea57b
HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes cron incidents
```

Output of every run is kept under `<profile>/cron/output/<job-id>/`.

### 31. How to inspect ORBIT reports

On demand:

```bash
cd /opt/data/agency && python3 orbit.py report
cd /opt/data/agency && python3 orbit.py metrics --json
```

Force the daily report to Discord now:

```bash
HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes cron run eeb1258ca105
```

Sender health needs ORBIT's read-only credential:
`set -a; . /opt/data/profiles/orbit/.env; set +a` first.

### 32. How to stop a lead

Stop chasing but keep the record:

```bash
/opt/data/agency/review close <escalation-id>
```

Or, with no escalation open, cancel the follow-ups directly:

```bash
cd /opt/data/agency && python3 -c "
import followups as F, pipeline as P
with P.connect() as c:
    with P.writing(c):
        n = F.cancel_all(c, 'L-xxxx', 'stopped by operator')
        P.transition(c, 'L-xxxx', 'CLOSED', 'operator', 'stopped by operator')
print('cancelled', n)"
```

### 33. How to suppress a recipient

Never contact again, across every campaign:

```bash
/opt/data/agency/review dnc <escalation-id>
```

Directly, without an escalation — requires a `suppress`-scoped key:

```bash
curl -s -X POST https://autoemail-39jr.onrender.com/api/v1/suppression -H "Authorization: Bearer $MAILHUB_API_TOKEN" -H "Content-Type: application/json" -d '{"email":"person@company.com","reason":"do_not_contact"}'
```

Valid reasons: `unsubscribed`, `bounced`, `do_not_contact`, `complaint`,
`manual`. Anything else is a `400`. Suppression is **per tenant** — it cancels
whatever was already queued for that address.

### 34. How to add a campaign

```bash
cd /opt/data/agency && python3 -c "
import pipeline as P
with P.connect() as c:
    with P.writing(c):
        c.execute(\"INSERT INTO campaigns (id,name,status,followup_schedule) VALUES ('C-NEW','My campaign','active','[3,7,12]')\")
print('created')"
```

`followup_schedule` is a JSON list — integers are days (`[3,7,12]`), strings are
short intervals for testing (`[\"2m\",\"4m\"]`).

### 35. How to add Gmail accounts through MailHub

Self-service, per user — no operator involvement and no credential ever reaches
an agent:

1. Sign in at the MailHub dashboard with your Supabase account.
2. **Connect mailbox** → Google's consent screen → grant Gmail access.
3. The refresh token is encrypted at rest (Fernet) and never leaves MailHub.
4. New mailboxes start in `warming` with a low daily cap that rises over time.
   Do not raise it manually.
5. Mint a scoped API key under **API access**. Give each agent the narrowest
   scope it needs, and never one key holding both `approve` and `queue`.

Agents never receive raw Gmail credentials. They hold a scoped MailHub API key
and nothing else.

---

## Sign-off

**CONTROLLED TESTING.**

To reach **LOW-VOLUME PRODUCTION**: deploy `e6d8e21` and verify the suppression
matrix, then decide how `READY_TO_SEND` should advance — either an orchestrator
cron or a documented manual step someone actually performs.

To reach **BULK PRODUCTION**: the above, plus enough real volume to say
something honest about deliverability, plus more than one warmed mailbox.

Bulk outreach is not enabled, and nothing in this phase enabled it.
