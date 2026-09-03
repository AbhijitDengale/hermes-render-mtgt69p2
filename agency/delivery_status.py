#!/usr/bin/env python3
"""What a bounce message actually says, decided from its own text.

A delivery status notification carries the answer in a machine-readable form:
an RFC 3463 enhanced status code, or failing that an SMTP reply code and a
diagnostic line. Reading those is arithmetic, not judgement, so it happens
here rather than being handed to a model that can only guess at it.

That mattered: every bounce used to reach a person labelled "unclear", because
the only bounce signal in the system was whether the From header mentioned
mailer-daemon. A permanent "550 5.1.1 no such user" and a temporary "452
mailbox full" looked identical, and neither could be acted on automatically.

The distinction this draws is the one that decides what may be done:

    permanent   the address will never work; suppress that exact address
    temporary   it might work later; change nothing, let the retry happen
    policy      the server refused us, not the address; a person decides

Nothing here suppresses a domain. A bad server is not a bad company, and the
recipient whose address bounced is the only address the evidence is about.

Pure functions, no I/O, no model.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# --- the failure taxonomy ----------------------------------------------------

HARD_BOUNCE = "HARD_BOUNCE"
TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
DOMAIN_FAILURE = "DOMAIN_FAILURE"
MAILBOX_FULL = "MAILBOX_FULL"
POLICY_REJECTION = "POLICY_REJECTION"
RELAY_DENIED = "RELAY_DENIED"
SPAM_REJECTION = "SPAM_REJECTION"
AUTHENTICATION_FAILURE = "AUTHENTICATION_FAILURE"
UNKNOWN_DELIVERY_FAILURE = "UNKNOWN_DELIVERY_FAILURE"
NOT_A_BOUNCE = "NOT_A_BOUNCE"

# Which of them justify never writing to that address again. Only these.
PERMANENT = (HARD_BOUNCE, DOMAIN_FAILURE)
# Which of them are the receiving side's policy rather than a bad address.
POLICY = (POLICY_REJECTION, RELAY_DENIED, SPAM_REJECTION, AUTHENTICATION_FAILURE)

HUMAN_LABEL = {
    HARD_BOUNCE: "Hard bounce",
    TEMPORARY_FAILURE: "Temporary delivery failure",
    DOMAIN_FAILURE: "Domain does not accept mail",
    MAILBOX_FULL: "Mailbox full",
    POLICY_REJECTION: "Rejected by policy",
    RELAY_DENIED: "Relay access denied",
    SPAM_REJECTION: "Rejected as spam",
    AUTHENTICATION_FAILURE: "Authentication failure",
    UNKNOWN_DELIVERY_FAILURE: "Delivery failed, reason unclear",
    NOT_A_BOUNCE: "Not a delivery notification",
}

# --- evidence ----------------------------------------------------------------

# Senders that only ever send delivery notifications.
DAEMON_SENDERS = ("mailer-daemon", "postmaster", "mail delivery subsystem",
                  "mail delivery system", "mailer_daemon", "no-reply@dmarc")

DAEMON_SUBJECTS = ("undelivered mail returned to sender", "delivery status notification",
                   "returned mail", "mail delivery failed", "undeliverable",
                   "delivery failure", "failure notice", "message not delivered",
                   "address not found")

# RFC 3463 enhanced codes, which are unambiguous where they appear.
ENHANCED = {
    "5.1.1": HARD_BOUNCE,          # bad destination mailbox address
    "5.1.2": DOMAIN_FAILURE,       # bad destination system address
    "5.1.3": HARD_BOUNCE,          # bad destination mailbox address syntax
    "5.1.6": HARD_BOUNCE,          # mailbox has moved, no forwarding
    "5.1.10": HARD_BOUNCE,         # recipient address has null MX
    "5.2.1": HARD_BOUNCE,          # mailbox disabled
    "5.2.2": MAILBOX_FULL,         # mailbox full, permanent
    "5.4.1": HARD_BOUNCE,          # no answer from host / recipient rejected
    "5.4.4": DOMAIN_FAILURE,       # unable to route
    "5.5.0": POLICY_REJECTION,
    "5.7.0": POLICY_REJECTION,
    "5.7.1": POLICY_REJECTION,     # delivery not authorised -- often relay denied
    "5.7.13": POLICY_REJECTION,
    "5.7.23": AUTHENTICATION_FAILURE,   # SPF failure
    "5.7.25": AUTHENTICATION_FAILURE,   # reverse DNS failure
    "5.7.26": AUTHENTICATION_FAILURE,   # multiple auth checks failed
    "5.7.509": AUTHENTICATION_FAILURE,  # DMARC failure
    "4.2.2": MAILBOX_FULL,         # mailbox full, temporary
    "4.3.2": TEMPORARY_FAILURE,
    "4.4.1": TEMPORARY_FAILURE,
    "4.4.2": TEMPORARY_FAILURE,
    "4.7.0": TEMPORARY_FAILURE,
}

# Phrases, checked only when a code does not settle it. Ordered: the first
# match wins, so the more specific phrases come first.
PHRASES: List[Tuple[str, str]] = [
    ("relay access denied", RELAY_DENIED),
    ("unable to relay", RELAY_DENIED),
    ("relaying denied", RELAY_DENIED),
    ("relay denied", RELAY_DENIED),
    ("nosuchuser", HARD_BOUNCE),
    ("no such user", HARD_BOUNCE),
    ("user unknown", HARD_BOUNCE),
    ("unknown user", HARD_BOUNCE),
    ("recipient address rejected", HARD_BOUNCE),
    ("address does not exist", HARD_BOUNCE),
    ("address not found", HARD_BOUNCE),
    ("recipient not found", HARD_BOUNCE),
    ("mailbox does not exist", HARD_BOUNCE),
    ("mailbox not found", HARD_BOUNCE),
    ("no mailbox here by that name", HARD_BOUNCE),
    ("invalid recipient", HARD_BOUNCE),
    ("unrouteable address", HARD_BOUNCE),
    ("unroutable address", HARD_BOUNCE),
    ("account has been disabled", HARD_BOUNCE),
    ("account is disabled", HARD_BOUNCE),
    ("mailbox is full", MAILBOX_FULL),
    ("mailbox full", MAILBOX_FULL),
    ("over quota", MAILBOX_FULL),
    ("quota exceeded", MAILBOX_FULL),
    ("insufficient system storage", MAILBOX_FULL),
    ("domain not found", DOMAIN_FAILURE),
    ("host or domain name not found", DOMAIN_FAILURE),
    ("no mx record", DOMAIN_FAILURE),
    ("unable to route", DOMAIN_FAILURE),
    ("spam", SPAM_REJECTION),
    ("blocked using", SPAM_REJECTION),
    ("blacklist", SPAM_REJECTION),
    ("blocklist", SPAM_REJECTION),
    ("reputation", SPAM_REJECTION),
    ("dmarc", AUTHENTICATION_FAILURE),
    ("spf check failed", AUTHENTICATION_FAILURE),
    ("dkim", AUTHENTICATION_FAILURE),
    ("not authenticated", AUTHENTICATION_FAILURE),
    ("access denied", POLICY_REJECTION),
    ("not permitted", POLICY_REJECTION),
    ("policy", POLICY_REJECTION),
    ("try again later", TEMPORARY_FAILURE),
    ("temporarily deferred", TEMPORARY_FAILURE),
    ("temporary failure", TEMPORARY_FAILURE),
    ("greylist", TEMPORARY_FAILURE),
    ("resources temporarily unavailable", TEMPORARY_FAILURE),
]

_ENHANCED_RE = re.compile(r"\b([45]\.\d{1,3}\.\d{1,3})\b")
_SMTP_RE = re.compile(r"\b([45]\d{2})[ -]")
_FINAL_RECIPIENT_RE = re.compile(r"^final-recipient:\s*rfc822;\s*(.+)$",
                                 re.I | re.M)
_ORIGINAL_RECIPIENT_RE = re.compile(r"^original-recipient:\s*rfc822;\s*(.+)$",
                                    re.I | re.M)
_ACTION_RE = re.compile(r"^action:\s*(failed|delayed|delivered|relayed|expanded)\s*$",
                        re.I | re.M)
_DIAGNOSTIC_RE = re.compile(r"^diagnostic-code:\s*(.+(?:\n[ \t]+.+)*)", re.I | re.M)
_ANGLE_EMAIL_RE = re.compile(r"<([^@<>\s]+@[^@<>\s]+)>")
_BARE_EMAIL_RE = re.compile(r"\b([\w.+-]+@[\w.-]+\.[a-z]{2,})\b", re.I)


def _text(*parts: Optional[str]) -> str:
    return "\n".join(p for p in parts if p)


def looks_like_dsn(from_email: str = "", subject: str = "",
                   body: str = "") -> bool:
    """Whether this is a delivery notification at all.

    Deliberately broad: being wrong here only means the message is examined,
    and `classify` returns NOT_A_BOUNCE when the evidence is not there.
    """
    frm = (from_email or "").lower()
    subj = (subject or "").lower()
    if any(s in frm for s in DAEMON_SENDERS):
        return True
    if any(s in subj for s in DAEMON_SUBJECTS):
        return True
    blob = (body or "").lower()
    return bool(_FINAL_RECIPIENT_RE.search(body or "")
                or "content-type: message/delivery-status" in blob)


def failed_recipient(body: str = "", subject: str = "",
                     fallback: Optional[str] = None) -> Optional[str]:
    """The address that failed, taken from the DSN's own fields first.

    Final-Recipient is what the reporting server says it could not deliver to,
    which is the only address the bounce is evidence about. Guessing from the
    body text instead risks suppressing whichever address happens to appear
    first, and in a bounce that is often our own sender.
    """
    body = body or ""
    for rx in (_FINAL_RECIPIENT_RE, _ORIGINAL_RECIPIENT_RE):
        m = rx.search(body)
        if m:
            addr = m.group(1).strip().strip("<>").strip()
            if "@" in addr:
                return addr.lower()
    for text in (subject or "", body):
        m = _ANGLE_EMAIL_RE.search(text)
        if m:
            return m.group(1).strip().lower()
    return (fallback or "").strip().lower() or None


def _diagnostic(body: str) -> str:
    m = _DIAGNOSTIC_RE.search(body or "")
    if m:
        return " ".join(m.group(1).split())
    return ""


def reason_excerpt(body: str = "", limit: int = 300) -> str:
    """The most useful line of the bounce, not the whole envelope.

    Prefers the Diagnostic-Code the reporting server wrote; otherwise the first
    line carrying a status code. A DSN quotes the entire original message, so
    passing the raw body to a person is passing them the wrong thing.
    """
    diag = _diagnostic(body or "")
    if diag:
        return diag[:limit]
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(">"):
            continue
        if _ENHANCED_RE.search(stripped) or _SMTP_RE.search(stripped):
            return " ".join(stripped.split())[:limit]
    for line in (body or "").splitlines():
        stripped = " ".join(line.split())
        if len(stripped) > 20 and not stripped.startswith(">"):
            return stripped[:limit]
    return ""


def classify(from_email: str = "", subject: str = "", body: str = "",
             to_email: Optional[str] = None) -> Dict[str, Any]:
    """What kind of delivery failure this is, and how sure that is.

    Returns a dict with:
        status       one of the constants above
        permanent    True only when the address itself is proven bad
        confidence   1.0 from an enhanced status code, 0.8 from a phrase with
                     a matching SMTP class, 0.6 from a phrase alone
        recipient    the address that failed, if the DSN names one
        code         the enhanced or SMTP code that decided it
        reason       a short human-readable excerpt
        needs_human  True when the evidence does not settle it

    Confidence is evidence, not opinion. Only 1.0 -- an enhanced code, which
    the sending server generated -- is allowed to drive suppression without a
    person, and only when that code means the mailbox does not exist.
    """
    blob = _text(subject, body)
    low = blob.lower()
    out: Dict[str, Any] = {
        "status": NOT_A_BOUNCE, "permanent": False, "confidence": 0.0,
        "recipient": None, "code": None, "reason": "", "needs_human": False,
        "label": HUMAN_LABEL[NOT_A_BOUNCE],
    }
    if not looks_like_dsn(from_email, subject, body):
        return out

    out["recipient"] = failed_recipient(body, subject, to_email)
    out["reason"] = reason_excerpt(body)

    # A DSN that reports success is not a failure at all.
    action = _ACTION_RE.search(body or "")
    if action and action.group(1).lower() in ("delivered", "relayed", "expanded"):
        return out

    enhanced = _ENHANCED_RE.search(blob)
    smtp = _SMTP_RE.search(blob)
    code = enhanced.group(1) if enhanced else (smtp.group(1) if smtp else None)
    out["code"] = code

    # 1. An enhanced status code settles it outright.
    if enhanced and enhanced.group(1) in ENHANCED:
        status = ENHANCED[enhanced.group(1)]
        # 5.7.1 is the one code that is genuinely two different things: a
        # relay refusal and a generic policy refusal share it, and only the
        # wording separates them. Neither is a bad address.
        if enhanced.group(1) == "5.7.1" and any(
                p in low for p in ("relay", "unable to relay")):
            status = RELAY_DENIED
        out.update(status=status, confidence=1.0,
                   permanent=status in PERMANENT,
                   label=HUMAN_LABEL[status])
        out["needs_human"] = status in POLICY
        return out

    # 2. Otherwise a phrase, corroborated by the SMTP class where there is one.
    for phrase, status in PHRASES:
        if phrase in low:
            temporary_class = bool(smtp and smtp.group(1).startswith("4"))
            if temporary_class and status in PERMANENT:
                # A 4xx reply is by definition not permanent, whatever the
                # wording suggests. Believing the phrase over the code is how
                # a mailbox that was merely busy gets suppressed for ever.
                status = TEMPORARY_FAILURE
            out.update(status=status, confidence=0.8 if smtp else 0.6,
                       permanent=status in PERMANENT and not temporary_class,
                       label=HUMAN_LABEL[status])
            # A phrase alone is never enough to stop writing to someone.
            out["needs_human"] = (status in POLICY) or out["confidence"] < 0.8
            return out

    # 3. It is a bounce, but nothing in it says why.
    if smtp and smtp.group(1).startswith("4"):
        out.update(status=TEMPORARY_FAILURE, confidence=0.7,
                   label=HUMAN_LABEL[TEMPORARY_FAILURE])
        return out
    out.update(status=UNKNOWN_DELIVERY_FAILURE, confidence=0.5,
               needs_human=True, label=HUMAN_LABEL[UNKNOWN_DELIVERY_FAILURE])
    return out


def may_suppress(verdict: Dict[str, Any]) -> bool:
    """Whether this verdict alone justifies never writing to that address.

    Three conditions, all required: the address itself is what failed, the
    reporting server said so in a machine-readable code, and we know which
    address it meant. A policy refusal, a temporary failure and an unparsed
    bounce all fail this and go to a person instead.
    """
    return bool(verdict.get("permanent")
                and verdict.get("confidence", 0) >= 1.0
                and verdict.get("recipient"))


ACTIONS = {
    HARD_BOUNCE: ["Cancel scheduled follow-ups",
                  "Suppress this exact recipient",
                  "Mark the lead's email as bounced",
                  "Look for an alternate contact"],
    DOMAIN_FAILURE: ["Cancel scheduled follow-ups",
                     "Suppress this exact recipient",
                     "Check whether the company has a different domain"],
    MAILBOX_FULL: ["Leave the address in place; the mailbox may be emptied",
                   "Retry on the next follow-up rather than suppressing"],
    TEMPORARY_FAILURE: ["No action: the receiving server asked us to retry",
                        "Leave follow-ups scheduled"],
    RELAY_DENIED: ["Do not suppress: the server refused us, not the address",
                   "Check whether this domain accepts external mail at all",
                   "Consider an alternate contact route"],
    POLICY_REJECTION: ["Do not suppress on this evidence alone",
                       "Review why the receiving server refused the message"],
    SPAM_REJECTION: ["Do not suppress the recipient",
                     "Review sending reputation and content for this domain",
                     "Escalate if more than one domain is refusing"],
    AUTHENTICATION_FAILURE: ["Do not suppress the recipient",
                             "Check SPF, DKIM and DMARC for the sending domain"],
    UNKNOWN_DELIVERY_FAILURE: ["Read the diagnostic text and decide",
                               "Do not suppress until the reason is known"],
}


def recommended_actions(verdict: Dict[str, Any]) -> List[str]:
    return list(ACTIONS.get(verdict.get("status"), []))
