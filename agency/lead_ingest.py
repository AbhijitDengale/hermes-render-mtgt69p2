#!/usr/bin/env python3
"""Lead ingestion for the agency — CSV import and single-record insert.

Phase B scope: get leads into `agency.db` in state NEW and stop. Nothing here
triggers research, copy, QA or sending; no agent is invoked and no outbound
call is made. Ingestion and outreach are deliberately separate steps so a bad
import cannot start emailing anyone.

Duplicate strategy — two independent guards
-------------------------------------------
1. `lead_id` is derived from (campaign_id, normalised email) unless one is
   supplied, so re-importing the same file produces the same primary key and
   collides on INSERT.
2. A UNIQUE index on `(campaign_id, lower(email))` catches the case where a
   caller supplies its own `lead_id` for a lead that already exists.

Both are enforced by the database, not by a pre-read. A check-then-insert would
race two concurrent imports; INSERT OR IGNORE cannot.

Missing campaigns are normalised to a real row rather than left NULL: SQLite
treats NULLs as distinct in a UNIQUE index, so a NULL campaign would silently
allow the same lead to be imported twice.

    python3 lead_ingest.py import leads.csv --campaign C-1
    python3 lead_ingest.py add --email a@b.com --business-name "B Ltd" --campaign C-1
    python3 lead_ingest.py list --limit 20
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sqlite3
import sys
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit, urlunsplit

DB = os.getenv("AGENCY_DB", "/opt/data/agency.db")

# Without a campaign, dedupe would be unreliable (see module docstring), so an
# unassigned lead gets a real campaign row instead of a NULL.
DEFAULT_CAMPAIGN = "unassigned"

# Both are needed downstream: NOVA researches the business, ARIA writes to a
# named company, and MailHub cannot queue without an address.
REQUIRED = ("email", "business_name")

# Deliberately permissive on the local part and strict on the shape. This is
# not an RFC 5322 parser; it rejects what is obviously unusable and leaves the
# real verdict to the provider.
_EMAIL = re.compile(r"^[^@\s,;<>\"]+@[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
                    r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$")

# CSV headers people actually use, mapped to columns. `state` is the trap: in a
# lead list it means the US state, but in this schema `state` is the workflow
# state machine, so it maps to `region`.
ALIASES = {
    "lead_id": "id", "id": "id",
    "business_name": "business_name", "business": "business_name",
    "company": "business_name", "company_name": "business_name",
    "contact_name": "contact_name", "contact": "contact_name",
    "name": "contact_name", "full_name": "contact_name",
    "email": "email", "email_address": "email",
    "phone": "phone", "phone_number": "phone", "telephone": "phone",
    "website": "website", "url": "website", "site": "website", "domain": "website",
    "city": "city", "town": "city",
    "state": "region", "region": "region", "province": "region",
    "country": "country",
    "niche": "niche", "industry": "niche", "category": "niche",
    "campaign_id": "campaign_id", "campaign": "campaign_id",
    "source": "source", "notes": "notes", "note": "notes",
}

COLUMNS = ("id", "campaign_id", "business_name", "contact_name", "email",
           "phone", "website", "city", "region", "country", "niche",
           "source", "notes")


# --- normalisation ----------------------------------------------------------

def normalize_email(value: Any) -> str:
    """Lowercase and trim. Casing is not significant in the domain, and the
    local part is case-sensitive only in theory — treating them as distinct
    would let the same person be imported twice."""
    return (str(value or "")).strip().strip("<>").lower()


def normalize_website(value: Any) -> Optional[str]:
    """Reduce a URL to a stable form so two spellings of one site dedupe.

    `example.com`, `http://Example.com/`, and `https://example.com` all become
    `https://example.com`.
    """
    raw = (str(value or "")).strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if not parts.netloc:
        return None
    scheme = "https" if parts.scheme in ("", "http", "https") else parts.scheme
    host = parts.netloc.lower()
    if host.endswith(":80") or host.endswith(":443"):
        host = host.rsplit(":", 1)[0]
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, host, path, parts.query, ""))


def valid_email(value: str) -> bool:
    return bool(value) and len(value) <= 254 and bool(_EMAIL.match(value))


def make_lead_id(campaign_id: str, email: str) -> str:
    """Deterministic id, so the same lead in the same campaign is the same row.

    This is what makes a re-import a no-op rather than a duplicate.
    """
    digest = hashlib.sha256(
        ("%s\x00%s" % (campaign_id, email)).encode("utf-8")).hexdigest()
    return "L-" + digest[:16]


def normalize_row(raw: Dict[str, Any],
                  default_campaign: Optional[str] = None) -> Dict[str, Any]:
    """Map arbitrary CSV headers onto columns and clean the values."""
    out: Dict[str, Any] = {}
    for key, value in (raw or {}).items():
        col = ALIASES.get((key or "").strip().lower().replace(" ", "_"))
        if not col:
            continue
        text = str(value).strip() if value is not None else ""
        out[col] = text or None

    out["email"] = normalize_email(out.get("email")) or None
    out["website"] = normalize_website(out.get("website"))
    out["campaign_id"] = (out.get("campaign_id") or default_campaign
                          or DEFAULT_CAMPAIGN)
    return out


# --- database ---------------------------------------------------------------

def connect(path: str = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def ensure_campaign(con: sqlite3.Connection, campaign_id: str) -> None:
    """Create the campaign if absent, so the foreign key holds and the unique
    index always has a non-NULL campaign to work with."""
    con.execute(
        "INSERT OR IGNORE INTO campaigns (id, name, status) VALUES (?,?,'draft')",
        (campaign_id, campaign_id))


def ingest_one(con: sqlite3.Connection, raw: Dict[str, Any],
               source: str = "manual",
               default_campaign: Optional[str] = None) -> Dict[str, Any]:
    """Insert one lead in state NEW. Returns created / duplicate / rejected.

    Never raises on bad input — a single malformed row in a large CSV must not
    abort the import.
    """
    row = normalize_row(raw, default_campaign)

    missing = [f for f in REQUIRED if not row.get(f)]
    if missing:
        return {"status": "rejected", "reason": "missing required field(s): %s"
                % ", ".join(missing), "email": row.get("email")}
    if not valid_email(row["email"]):
        return {"status": "rejected", "reason": "malformed email",
                "email": row["email"]}

    row["source"] = row.get("source") or source
    lead_id = row.get("id") or make_lead_id(row["campaign_id"], row["email"])
    row["id"] = lead_id

    ensure_campaign(con, row["campaign_id"])

    cur = con.execute(
        "INSERT OR IGNORE INTO leads (%s, state, state_reason) "
        "VALUES (%s, 'NEW', 'ingested')"
        % (", ".join(COLUMNS), ", ".join("?" * len(COLUMNS))),
        [row.get(c) for c in COLUMNS])

    if cur.rowcount == 0:
        # Either the derived id already exists, or the (campaign, email) unique
        # index fired for a caller-supplied id. Both mean: already have them.
        return {"status": "duplicate", "lead_id": lead_id, "email": row["email"]}

    con.execute(
        "INSERT INTO events (lead_id, campaign_id, agent, event_type,"
        "                    from_state, to_state, detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (lead_id, row["campaign_id"], "ingest", "lead.ingested", None, "NEW",
         "source=%s" % row["source"]))
    con.execute(
        "INSERT INTO audit_logs (actor, action, subject_type, subject_id, detail) "
        "VALUES ('ingest','lead.created','lead',?,?)",
        (lead_id, "%s / %s" % (row["business_name"], row["email"])))

    return {"status": "created", "lead_id": lead_id, "email": row["email"],
            "state": "NEW"}


def ingest_rows(rows: Iterable[Dict[str, Any]], source: str = "manual",
                default_campaign: Optional[str] = None,
                db_path: str = None) -> Dict[str, Any]:
    """Ingest many rows in ONE transaction.

    All or nothing: a crash halfway through a 5,000-row file must not leave a
    partial import that a second run would then treat as already done.
    """
    created, duplicate, rejected = [], [], []
    con = connect(db_path)
    try:
        with con:
            for raw in rows:
                res = ingest_one(con, raw, source, default_campaign)
                {"created": created, "duplicate": duplicate,
                 "rejected": rejected}[res["status"]].append(res)
    finally:
        con.close()
    return {"created": len(created), "duplicate": len(duplicate),
            "rejected": len(rejected), "rejected_detail": rejected[:20],
            "lead_ids": [r["lead_id"] for r in created]}


def ingest_csv(path: str, default_campaign: Optional[str] = None,
               source: Optional[str] = None,
               db_path: str = None) -> Dict[str, Any]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    return ingest_rows(rows, source or "csv:%s" % os.path.basename(path),
                       default_campaign, db_path)


# --- CLI --------------------------------------------------------------------

def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import", help="import a CSV file")
    imp.add_argument("path")
    imp.add_argument("--campaign", default=None)
    imp.add_argument("--source", default=None)

    add = sub.add_parser("add", help="insert a single lead")
    add.add_argument("--email", required=True)
    add.add_argument("--business-name", required=True)
    for opt in ("contact-name", "phone", "website", "city", "region",
                "country", "niche", "campaign", "source", "notes", "lead-id"):
        add.add_argument("--" + opt, default=None)

    lst = sub.add_parser("list", help="show recent leads")
    lst.add_argument("--limit", type=int, default=20)
    lst.add_argument("--state", default=None)

    args = ap.parse_args(argv)

    if args.cmd == "import":
        out = ingest_csv(args.path, args.campaign, args.source)
        print("created   : %d" % out["created"])
        print("duplicate : %d" % out["duplicate"])
        print("rejected  : %d" % out["rejected"])
        for r in out["rejected_detail"]:
            print("    %-38s %s" % (r.get("email") or "(no email)", r["reason"]))
        return 0

    if args.cmd == "add":
        row = {"email": args.email, "business_name": args.business_name,
               "contact_name": args.contact_name, "phone": args.phone,
               "website": args.website, "city": args.city,
               "region": args.region, "country": args.country,
               "niche": args.niche, "campaign_id": args.campaign,
               "source": args.source or "manual", "notes": args.notes,
               "id": args.lead_id}
        con = connect()
        try:
            with con:
                res = ingest_one(con, row, args.source or "manual", args.campaign)
        finally:
            con.close()
        print(res)
        return 0 if res["status"] != "rejected" else 1

    con = connect()
    try:
        sql = ("SELECT id, business_name, email, campaign_id, state, created_at "
               "FROM leads")
        params: list = []
        if args.state:
            sql += " WHERE state = ?"
            params.append(args.state)
        sql += " ORDER BY created_at DESC, id LIMIT ?"
        params.append(args.limit)
        print("%-20s %-26s %-30s %-12s %s"
              % ("LEAD_ID", "BUSINESS", "EMAIL", "STATE", "CAMPAIGN"))
        for r in con.execute(sql, params):
            print("%-20s %-26s %-30s %-12s %s"
                  % (r["id"], (r["business_name"] or "")[:26],
                     (r["email"] or "")[:30], r["state"], r["campaign_id"]))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
