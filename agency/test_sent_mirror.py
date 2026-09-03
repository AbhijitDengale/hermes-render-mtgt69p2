#!/usr/bin/env python3
"""The sent-state mirror, and the reply_received write-back.

Two drifts between what the provider did and what the agency recorded:

  1. A send the provider confirmed stayed marked 'queued' whenever the lead
     left READY_TO_SEND before the next orchestration tick -- a reply landing
     in the same two minutes was enough. Nothing polled that message again,
     so the agency under-counted its own sends and Supabase never learned the
     email had gone out.

  2. inbound_processor mirrored a 'reply_received' event that the write-back
     had no mapping for, so every one of them failed until it gave up.

Everything runs against a temporary database and a fake MailHub. Nothing is
sent, and no test here can send.
"""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_TMP = tempfile.mkdtemp()
os.environ["AGENCY_DB"] = os.path.join(_TMP, "mirror.db")

import lead_ingest as li      # noqa: E402
import orchestrator as O      # noqa: E402
import pipeline as P          # noqa: E402
import supabase_sync as S     # noqa: E402
import tenants                # noqa: E402

P.DB = os.environ["AGENCY_DB"]

PASSED = 0
FAILED = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  PASS %-66s %s" % (name, detail))
    else:
        FAILED += 1
        FAILURES.append(name)
        print("  FAIL %-66s %s" % (name, detail))


def fresh_db():
    if os.path.exists(P.DB):
        os.remove(P.DB)
    con = sqlite3.connect(P.DB)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()


def set_env():
    for k in list(os.environ):
        if k.startswith("MAILHUB_TENANT_") or k == "MAILHUB_API_TOKEN":
            del os.environ[k]
    for i, uid in enumerate((2, 3), 1):
        os.environ["MAILHUB_TENANT_%d_NAME" % i] = "t%d" % uid
        os.environ["MAILHUB_TENANT_%d_USER_ID" % i] = str(uid)
        os.environ["MAILHUB_TENANT_%d_QUEUE_TOKEN" % i] = "q%d" % uid
        os.environ["MAILHUB_TENANT_%d_APPROVE_TOKEN" % i] = "a%d" % uid
        os.environ["MAILHUB_TENANT_%d_LEO_TOKEN" % i] = "l%d" % uid


TO_READY = ["RESEARCH_PENDING", "RESEARCHING", "RESEARCH_COMPLETE", "COPY_PENDING",
            "COPY_READY", "QA_PENDING", "READY_TO_SEND"]


def seed(con, n, queue_id, tenant, state_after=None):
    """A lead whose message was queued to MailHub and never confirmed here."""
    con.execute("INSERT OR IGNORE INTO campaigns (id, name, status, followup_schedule)"
                " VALUES ('C-1','C-1','active','[\"2m\"]')")
    r = li.ingest_one(con, {"email": "p%d@example.com" % n,
                            "business_name": "Prospect %d" % n,
                            "niche": "x", "country": "AE"}, default_campaign="C-1")
    lead = r["lead_id"]
    prev = "NEW"
    for nxt in TO_READY:
        with P.writing(con):
            P.transition(con, lead, nxt, "seed", "fixture", expect=prev)
        prev = nxt
    with P.writing(con):
        P.save_draft(con, lead, "C-1", 0, "S%d" % n, "B%d" % n)
        P.record_qa(con, lead, 0, "approved", approval_id="AP%d" % n)
        con.execute("UPDATE messages SET status='queued', mailhub_queue_id=?,"
                    " tenant_user_id=? WHERE id=?",
                    (str(queue_id), tenant, P.message_id(lead, 0)))
    if state_after:
        with P.writing(con):
            P.transition(con, lead, state_after, "leo", "reply arrived first")
    return lead


SENT = {"status": "sent", "provider_message_id": "PM-%s", "provider_thread_id": "TH-%s",
        "account_id": "acct_x", "sent_at": "2026-09-03T04:00:00Z",
        "from_email": "demon@socialnexa.cv", "from_name": "Lisa Chen"}


def deliver(kind, sid, lead_id, payload):
    """One outbox event, shaped the way the table stores it."""
    return S._deliver({"event_type": kind, "supabase_id": sid,
                       "lead_id": lead_id, "payload_json": json.dumps(payload)})


def main() -> int:
    set_env()
    fresh_db()
    print("=" * 78)
    print("SENT-STATE MIRROR AND reply_received")
    print("=" * 78)

    calls = []

    def fake_mailhub(method, path, body=None, token=None):
        calls.append((method, path, token))
        qid = path.rsplit("/", 1)[-1]
        if qid in ("90",):                       # genuinely still pending
            return {"status": "pending"}
        if qid in ("99",):                       # sent, but no acknowledgement
            return {"status": "sent", "provider_message_id": None}
        if token != "q3":                        # visible only to its own tenant
            return {"error": "http 404"}
        d = {k: (v % qid if isinstance(v, str) and "%s" in v else v)
             for k, v in SENT.items()}
        return d

    real_mailhub, O.mailhub = O.mailhub, fake_mailhub
    mirrored = []
    real_enqueue, S.enqueue = S.enqueue, \
        lambda lead_id, event_type, payload, con=None, force=False: \
        mirrored.append((lead_id, event_type, payload)) or 1
    try:
        with P.connect(P.DB) as con:
            still_ready = seed(con, 1, 41, 3)
            moved_on = seed(con, 2, 42, 3, state_after="HUMAN_REVIEW")
            pending = seed(con, 3, 90, 3)
            unacked = seed(con, 4, 99, 3)
            no_tenant = seed(con, 5, 43, None)

            print("\n--- 1. A provider-confirmed send becomes SENT ---")
            log = O.reconcile_queued(con)
            row = con.execute("SELECT status, provider_message_id, from_email, sent_at"
                              " FROM messages WHERE id=?",
                              (P.message_id(still_ready, 0),)).fetchone()
            state = con.execute("SELECT state FROM leads WHERE id=?",
                                (still_ready,)).fetchone()[0]
            check("the message is recorded as sent, with the provider id",
                  row["status"] == "sent" and row["provider_message_id"] == "PM-41",
                  str(dict(row)))
            check("  the professional sender is recorded with it",
                  row["from_email"] == "Lisa Chen <demon@socialnexa.cv>")
            check("  a lead still waiting to send is advanced to SENT",
                  state == "SENT", state)
            check("  and Supabase is told", ("sent" in [e[1] for e in mirrored
                                                        if e[0] == still_ready]))

            print("\n--- 2. A lead that moved on keeps its state ---")
            row = con.execute("SELECT status, provider_message_id FROM messages"
                              " WHERE id=?", (P.message_id(moved_on, 0),)).fetchone()
            state = con.execute("SELECT state FROM leads WHERE id=?",
                                (moved_on,)).fetchone()[0]
            check("its message is corrected to sent",
                  row["status"] == "sent" and row["provider_message_id"] == "PM-42")
            check("  but the lead is NOT dragged back from HUMAN_REVIEW",
                  state == "HUMAN_REVIEW", state)
            check("  the run says so plainly",
                  any("left READY_TO_SEND" in line for line in log), str(log[-1:]))

            print("\n--- 3. Nothing unconfirmed is touched ---")
            check("a message the provider has not confirmed stays queued",
                  con.execute("SELECT status FROM messages WHERE id=?",
                              (P.message_id(pending, 0),)).fetchone()[0] == "queued")
            check("  a send with no provider acknowledgement stays queued too",
                  con.execute("SELECT status FROM messages WHERE id=?",
                              (P.message_id(unacked, 0),)).fetchone()[0] == "queued")
            check("  their leads are untouched",
                  con.execute("SELECT state FROM leads WHERE id=?",
                              (pending,)).fetchone()[0] == "READY_TO_SEND")

            print("\n--- 4. A message queued before its tenant was recorded ---")
            check("it is resolved by trying the other credentials",
                  con.execute("SELECT status FROM messages WHERE id=?",
                              (P.message_id(no_tenant, 0),)).fetchone()[0] == "sent")
            check("  and the tenant that owned it is written back",
                  con.execute("SELECT tenant_user_id FROM messages WHERE id=?",
                              (P.message_id(no_tenant, 0),)).fetchone()[0] == 3)

            print("\n--- 5. Running it again changes nothing ---")
            before = con.execute("SELECT id, status, provider_message_id, sent_at,"
                                 " updated_at FROM messages ORDER BY id").fetchall()
            n_mirror, n_calls = len(mirrored), len(calls)
            log2 = O.reconcile_queued(con)
            after = con.execute("SELECT id, status, provider_message_id, sent_at,"
                                " updated_at FROM messages ORDER BY id").fetchall()
            check("no row changes on a second pass",
                  [tuple(r) for r in before] == [tuple(r) for r in after])
            check("  no second SENT transition is recorded",
                  con.execute("SELECT COUNT(*) FROM events WHERE to_state='SENT'"
                              " AND lead_id=?", (still_ready,)).fetchone()[0] == 1)
            check("  nothing further is mirrored", len(mirrored) == n_mirror)
            check("  the already-sent rows are not even asked about again",
                  len(calls) - n_calls <= 3, "%d call(s)" % (len(calls) - n_calls))
            check("  and it reports only what is still unresolved", len(log2) == 0,
                  str(log2))

            print("\n--- 6. Reconciliation can never send ---")
            src = (HERE / "orchestrator.py").read_text(encoding="utf-8")
            body = src[src.index("def reconcile_queued"):src.index("def tick(")]
            check("it never POSTs to MailHub", '"POST"' not in body)
            # The docstring explains what it must never do, so the assertion
            # reads the code below it rather than the prose above it.
            code = body[body.index('"""', body.index('"""') + 3):]
            check("  it never writes a subject, body or approval",
                  not any(w in code for w in ("save_draft", "record_qa", "subject=",
                                              "body=", "approval_id")))
            check("  it only ever reads message status",
                  body.count("mailhub(") == 0 and "_status_from_any_tenant" in body)
            check("  and only updates rows still marked queued",
                  "status='queued'" in body)
            check("  every MailHub call made was a GET on a message",
                  all(m == "GET" and p.startswith("/api/v1/messages/")
                      for m, p, _ in calls), str(calls[:1]))
    finally:
        O.mailhub = real_mailhub
        S.enqueue = real_enqueue

    print("\n--- 7. reply_received reaches Supabase, idempotently ---")
    patches = []

    def fake_call(path, method="GET", body=None, prefer=None):
        patches.append((method, path, body))
        return []

    real_call, S._call = S._call, fake_call
    try:
        deliver("reply_received", "SB-1", "L-1",
                {"tenant_user_id": 3, "provider_message_id": "PM-1",
                     "is_bounce": False, "is_auto_reply": False,
                     "followups_cancelled": 2, "state": "REPLIED"})
        check("the event no longer raises 'unknown outbox event type'", True)
        check("  it records the arrival time on the right lead",
              len(patches) == 1 and patches[0][1].startswith("leads?id=eq.SB-1")
              and "replied_at" in patches[0][2], str(patches[:1]))
        check("  and only when there is no arrival time yet",
              "replied_at=is.null" in patches[0][1], patches[0][1])
        check("  it never writes a classification",
              "reply_classification" not in json.dumps(patches[0][2])
              and "reply_status" not in json.dumps(patches[0][2]))

        patches.clear()
        deliver("reply_received", "SB-2", "L-2",
                    {"is_bounce": True, "state": "HUMAN_REVIEW"})
        check("a bounce also records the bounce time",
              len(patches) == 2 and "bounced_at" in patches[1][2]
              and "bounced_at=is.null" in patches[1][1], str(patches[1][1]))

        patches.clear()
        for _ in range(3):
            deliver("reply_received", "SB-1", "L-1", {"is_bounce": False})
        check("replaying it writes the same conditional patch and nothing else",
              len(patches) == 3 and all("replied_at=is.null" in p[1] for p in patches),
              "%d patch(es)" % len(patches))
        check("  so a second reply cannot overwrite the first arrival time",
              all(p[0] == "PATCH" for p in patches))

        check("the classification path is untouched and still separate",
              S.REPLY_CLASS["positive"] == "interested"
              and S.REPLY_CLASS["meeting_request"] == "meeting")
        patches.clear()
        real_rpc, S.rpc = S.rpc, lambda n, a: patches.append(("RPC", n, a))
        try:
            deliver("replied", "SB-1", "L-1", {"classification": "positive"})
        finally:
            S.rpc = real_rpc
        check("  `replied` still maps the classification through its own RPC",
              patches and patches[0][1] == "mark_lead_replied"
              and patches[0][2]["p_classification"] == "interested", str(patches[:1]))
    finally:
        S._call = real_call

    print("\n--- 8-10. Nothing else moved ---")
    orch = (HERE / "orchestrator.py").read_text(encoding="utf-8")
    sync = (HERE / "supabase_sync.py").read_text(encoding="utf-8")
    check("8. sender routing is unchanged: still per-tenant, still persisted",
          "tenants.for_message(draft[\"tenant_user_id\"]" in orch)
    check("9. no limit, pacing or identity value appears in either change",
          not any(w in orch[orch.index("def reconcile_queued"):orch.index("def tick(")]
                  for w in ("daily_limit", "hourly", "next_send_at", "identity_status")))
    import verification_worker as VW
    check("10. the admission contract is untouched",
          VW.claimable_filter() == ("status=eq.ready&hermes_status=eq.not_imported"
                                    "&email_verification_status=eq.valid"
                                    "&email_verified=is.true"),
          VW.claimable_filter())
    check("    and the verifier's selection is untouched by this change",
          "NEEDS_VERDICT" in (HERE / "verification_worker.py").read_text(encoding="utf-8"))
    check("    reconciliation is bounded, so a tick cannot be swamped",
          "LIMIT ?" in orch and "limit: int = 50" in orch)

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
