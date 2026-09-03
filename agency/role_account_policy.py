#!/usr/bin/env python3
"""Which role addresses this business is willing to write to.

A verifier that reports `role_account` is stating a fact about the local part,
not a defect: `info@` is a shared mailbox rather than a person. For consumer
mail that is a reason for caution. For B2B outreach to small businesses it is
frequently the only address the company publishes, and treating it as a fault
held 431 otherwise-perfect leads out of the pipeline -- every one of them with
valid syntax, valid MX and a domain that accepts mail.

So the verdict stays as the verifier issued it, and this decides separately
what the business does about it. Three tiers:

    APPROVED     a published front-door mailbox. Eligible, provided every
                 technical check passed and the address has no history
                 against it.
    RESTRICTED   an internal function. Held for a person; it may be the right
                 contact, but not one to mail unattended.
    EXCLUDED     an address that must not receive outreach at all, either
                 because it is automated (postmaster, noreply) or because
                 mailing it is a nuisance (jobs, careers, hr).

This changes nothing else. Syntax, MX, disposable, typo, scraper-artifact,
suspicious-local-part, hard-bounce suppression and the unknown retry ladder
are all untouched, and any one of them still overrides this file: a promotion
happens only when the address is clean on every other axis.

Pure functions. No I/O, no network, no database.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

POLICY_VERSION = "role-account-2026-09-03"

# Published, front-of-house mailboxes. Mail sent here is expected.
APPROVED = frozenset((
    "info", "hello", "contact", "sales", "enquiries", "enquiry",
    "inquiries", "inquiry", "office", "business", "team",
))

# Internal functions. Possibly the right person, never an automatic send.
RESTRICTED = frozenset((
    "admin", "support", "billing", "accounts", "accounting", "finance",
    "security", "webmaster", "it", "helpdesk", "help",
))

# Never mail these. Automated endpoints, or addresses where cold outreach is
# simply unwelcome.
EXCLUDED = frozenset((
    "abuse", "postmaster", "mailer-daemon", "mailerdaemon", "noreply",
    "no-reply", "donotreply", "do-not-reply", "bounce", "bounces",
    "jobs", "careers", "career", "recruitment", "recruiting", "hr",
    "spam", "unsubscribe",
))

APPROVED_TIER = "approved"
RESTRICTED_TIER = "restricted"
EXCLUDED_TIER = "excluded"
NOT_ROLE = "not_role"

# Free mailbox providers. A role address on one of these is not a company
# front door -- it is a personal account that happens to be named `info`, and
# it stays under review until that policy is deliberately revisited.
FREE_PROVIDERS = frozenset((
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com",
    "icloud.com", "me.com", "aol.com", "protonmail.com", "proton.me",
    "gmx.com", "gmx.net", "mail.ru", "yandex.com", "yandex.ru",
    "zoho.com", "rediffmail.com", "qq.com", "163.com", "126.com",
))

# Flags that describe something wrong with the address itself. Any one of them
# outranks this policy: a role account is only ever promoted when it is clean.
DISQUALIFYING_FLAGS = frozenset((
    "disposable", "typo", "scraper_artifact", "suspicious_local_part",
    "leading_hyphen", "domain_only", "malformed", "invalid_syntax",
    "catch_all", "spamtrap", "blocklisted",
))

REASON_ALLOWED = "role_account_allowed"
REASON_RESTRICTED = "role_account_restricted"
REASON_EXCLUDED = "role_account_excluded"
REASON_FREE = "role_account_free_provider"

_LOCAL_RE = re.compile(r"^[a-z0-9]+(?:[._+-][a-z0-9]+)*$")


def split(email: str) -> Tuple[str, str]:
    e = (email or "").strip().lower()
    if "@" not in e:
        return e, ""
    local, _, domain = e.rpartition("@")
    return local, domain


def role_base(local: str) -> str:
    """The role word in a local part, or "" if there is not one.

    The whole local part is tried first, so hyphenated names that ARE roles --
    `no-reply`, `mailer-daemon` -- are recognised as themselves. Only then is
    a dotted or plussed suffix stripped, because `info.dubai` and `info+leads`
    are the same shared mailbox wearing a label.

    A hyphen is deliberately never split on. `sales-director` is a person and
    `info-uae` is a mailbox, and nothing in the string says which; treating
    the hyphen as a separator promotes the person. Not recognising `info-uae`
    costs a lead that stays risky, which is the safe direction to be wrong in.
    """
    local = (local or "").strip().lower()
    if not local:
        return ""
    if local in EXCLUDED or local in RESTRICTED or local in APPROVED:
        return local
    first = re.split(r"[._+]", local)[0]
    return first if first else ""


def tier(email: str) -> str:
    base = role_base(split(email)[0])
    if not base:
        return NOT_ROLE
    if base in EXCLUDED:
        return EXCLUDED_TIER
    if base in RESTRICTED:
        return RESTRICTED_TIER
    if base in APPROVED:
        return APPROVED_TIER
    return NOT_ROLE


def is_free_provider(email: str) -> bool:
    return split(email)[1] in FREE_PROVIDERS


def technical_checks(record: Dict[str, Any], email: str) -> Dict[str, bool]:
    """Everything that must be true before policy is even consulted.

    Read from the verifier's own stored evidence. This never re-derives a
    verdict the verifier already issued; it only reads what it recorded.
    """
    local, domain = split(email)
    flags = {str(f).strip().lower() for f in (record.get("flags") or [])}
    return {
        "syntax_valid": bool(local and domain and "." in domain
                             and _LOCAL_RE.match(local) is not None),
        "mx_valid": bool(record.get("mx_host")),
        "domain_accepts_mail": record.get("deliverable") is True,
        "no_disqualifying_flag": not (flags & DISQUALIFYING_FLAGS),
        "no_typo_suggestion": not record.get("did_you_mean"),
    }


def evaluate(email: str, record: Optional[Dict[str, Any]] = None,
             bounced: bool = False, suppressed: bool = False,
             unsubscribed: bool = False) -> Dict[str, Any]:
    """What to do with one role address, and why.

    Returns {status, reason, tier, eligible, blockers, checks, policy_version}
    where `status` is the verification status this policy would record. It is
    only ever more permissive than the verifier on the single axis of
    role_account; every other reason to withhold is preserved.

    History is decisive and comes first. An address that has already bounced,
    or that someone has opted out of, is never promoted no matter how tidy it
    looks -- that is the whole point of recording those events.
    """
    record = record or {}
    t = tier(email)
    checks = technical_checks(record, email)
    blockers: List[str] = []

    if bounced:
        blockers.append("previously bounced")
    if suppressed:
        blockers.append("suppressed")
    if unsubscribed:
        blockers.append("unsubscribed")
    for name, ok in checks.items():
        if not ok:
            blockers.append(name.replace("_", " ") + " failed")

    out = {"tier": t, "checks": checks, "blockers": blockers,
           "policy_version": POLICY_VERSION, "eligible": False,
           "free_provider": is_free_provider(email)}

    if t == EXCLUDED_TIER:
        out.update(status="invalid", reason=REASON_EXCLUDED, eligible=False)
        return out
    if t == RESTRICTED_TIER:
        out.update(status="risky", reason=REASON_RESTRICTED, eligible=False)
        return out
    if t != APPROVED_TIER:
        # Not a role address at all: this policy has nothing to say, and the
        # verifier's own verdict stands untouched.
        out.update(status=None, reason=None, eligible=False)
        return out

    if out["free_provider"]:
        out.update(status="risky", reason=REASON_FREE, eligible=False)
        out["blockers"].append("role account on a free provider")
        return out
    if blockers:
        out.update(status="risky", reason=REASON_RESTRICTED, eligible=False)
        return out

    out.update(status="valid", reason=REASON_ALLOWED, eligible=True)
    return out


def sole_reason_is_role(record: Dict[str, Any]) -> bool:
    """True when role_account is the only thing the verifier held against it.

    A row carrying any other flag is out of scope for this policy: the point
    is to stop role_account alone forcing risky, not to overrule a second
    finding that has nothing to do with it.
    """
    flags = {str(f).strip().lower() for f in (record.get("flags") or [])}
    reason = (record.get("reason") or "").strip().lower()
    if reason and reason != "role_account":
        return False
    if not flags:
        return reason == "role_account"
    # free_provider travels with role_account and is handled by the policy
    # itself rather than disqualifying the row from evaluation.
    return flags <= {"role_account", "free_provider"} and "role_account" in flags


def audit_entry(previous: Dict[str, Any], verdict: Dict[str, Any],
                now_iso: str) -> Dict[str, Any]:
    """What changed and why, kept beside the original evidence rather than
    replacing it. The verifier's own record is never edited."""
    return {
        "previous_verification_status": previous.get("status"),
        "previous_reason": previous.get("reason"),
        "new_verification_status": verdict.get("status"),
        "new_reason": verdict.get("reason"),
        "policy_version": POLICY_VERSION,
        "re_evaluated_at": now_iso,
        "tier": verdict.get("tier"),
        "blockers": list(verdict.get("blockers") or []),
    }


def categorise(email: str, record: Dict[str, Any], **history) -> str:
    """A|B|C|D for reporting, before anything is written.

        A  approved B2B role only          -> may become valid
        B  role + free provider            -> stays risky
        C  restricted or excluded role     -> stays risky / rejected
        D  carries some other risk flag    -> stays risky, out of scope
    """
    if not sole_reason_is_role(record):
        return "D"
    v = evaluate(email, record, **history)
    if v["tier"] in (RESTRICTED_TIER, EXCLUDED_TIER):
        return "C"
    if v.get("free_provider"):
        return "B"
    return "A" if v["eligible"] else "D"
