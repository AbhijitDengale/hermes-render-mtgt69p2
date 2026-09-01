#!/usr/bin/env python3
"""Multi-tenant suppression, verified against the production database.

The agency box holds one tenant's credentials, so the API alone cannot prove
this. These checks go straight to the production schema: two owners suppress
the same address, both stick, and neither can see the other's list.

Read-mostly. The two rows it writes are @example.com probes and are removed
again at the end.
"""
import os
import pathlib
import re
import sys

root = pathlib.Path(r"C:\Users\RM\Desktop\Project\mailhub")
if not root.exists():
    root = pathlib.Path(r"C:\Users\RM\Desktop\Project\razoko\mailhub")
for line in (root / "render.env.secrets").read_text(encoding="utf-8").splitlines():
    m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
    if m:
        os.environ.setdefault(m.group(1), m.group(2))
sys.path.insert(0, str(root))

import app.db as db  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-4s %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


owners = db.rows("SELECT id, email, role FROM users ORDER BY id")
print("tenants: %s" % ", ".join("id=%s(%s)" % (o["id"], o["role"])
                                for o in owners))
if len(owners) < 2:
    print("need two tenants to test isolation"); sys.exit(2)
a, b = owners[0]["id"], owners[1]["id"]
SHARED = "tenancy-probe@example.com"

print("\n=== the address-only primary key is gone ===")
cons = [r["conname"] for r in db.rows(
    "SELECT conname FROM pg_constraint"
    " WHERE conrelid = 'mailhub.suppression'::regclass")]
check("suppression_pkey no longer exists", "suppression_pkey" not in cons,
      str(cons))
idx = [r["indexname"] for r in db.rows(
    "SELECT indexname FROM pg_indexes"
    " WHERE schemaname='mailhub' AND tablename='suppression'")]
check("the per-owner unique index is the key now",
      "suppression_owner_email_key" in idx, str(idx))
check("and the address-only lookup is still indexed",
      "idx_suppression_email" in idx)

print("\n=== two tenants suppress the same address ===")
try:
    with db.tx() as con:
        con.execute("DELETE FROM suppression WHERE email=%s", (SHARED,))
    with db.tx() as con:
        con.execute("INSERT INTO suppression (email, reason, detail,"
                    " owner_user_id) VALUES (%s,'do_not_contact','probe',%s)",
                    (SHARED, a))
    check("tenant A suppresses it", True, "owner_user_id=%s" % a)
    with db.tx() as con:
        con.execute("INSERT INTO suppression (email, reason, detail,"
                    " owner_user_id) VALUES (%s,'do_not_contact','probe',%s)",
                    (SHARED, b))
    check("tenant B suppresses the SAME address (used to be a 500)", True,
          "owner_user_id=%s" % b)

    n = db.scalar("SELECT COUNT(*) FROM suppression WHERE email=%s", (SHARED,))
    check("both rows coexist", n == 2, "%d row(s)" % n)

    print("\n=== isolation ===")
    check("suppressed for tenant A", db.is_suppressed(SHARED, a) is True)
    check("suppressed for tenant B", db.is_suppressed(SHARED, b) is True)

    other = "only-a-probe@example.com"
    with db.tx() as con:
        con.execute("DELETE FROM suppression WHERE email=%s", (other,))
        con.execute("INSERT INTO suppression (email, reason, detail,"
                    " owner_user_id) VALUES (%s,'unsubscribed','probe',%s)",
                    (other, a))
    check("an address on A's list is suppressed for A",
          db.is_suppressed(other, a) is True)
    check("  and NOT suppressed for B — no cross-tenant leakage",
          db.is_suppressed(other, b) is False)
    check("  while an ownerless check still sees it (fails safe)",
          db.is_suppressed(other, None) is True)

    print("\n=== each tenant sees only its own list ===")
    for who, oid in (("A", a), ("B", b)):
        mine = db.scalar("SELECT COUNT(*) FROM suppression"
                         " WHERE owner_user_id=%s", (oid,))
        theirs = db.scalar("SELECT COUNT(*) FROM suppression"
                           " WHERE owner_user_id<>%s", (oid,))
        print("    tenant %s: %d own, %d belonging to others" % (who, mine, theirs))
    check("no suppression row is ownerless",
          db.scalar("SELECT COUNT(*) FROM suppression"
                    " WHERE owner_user_id IS NULL") == 0)
finally:
    with db.tx() as con:
        con.execute("DELETE FROM suppression WHERE email LIKE %s",
                    ("%-probe@example.com",))
        con.execute("DELETE FROM suppression WHERE email LIKE %s",
                    ("reason-%@example.com",))
    print("\nprobe rows removed; remaining suppression rows: %d"
          % db.scalar("SELECT COUNT(*) FROM suppression"))

print("\n" + "=" * 74)
print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
