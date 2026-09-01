#!/usr/bin/env python3
"""Research tools for NOVA, exposed as a stdio MCP server.

Design notes
------------
* Provider-agnostic. ``BrowserProvider`` is the interface; ``SteelBrowserProvider``
  is the first implementation. Swapping to self-hosted Steel or another vendor
  means one new subclass and one env var, not a rewrite.
* Credentials come from the environment only. Nothing is hardcoded, nothing is
  written to agency.db, nothing is returned to the model.
* Fail closed. A URL that cannot be proven safe is refused.

Env:
  BROWSER_PROVIDER=steel
  STEEL_API_KEY=...            (never logged, never returned)
  STEEL_BASE_URL=https://api.steel.dev
  BROWSER_MAX_CONCURRENCY=2
  BROWSER_TIMEOUT_SECONDS=45
  BROWSER_CACHE_TTL_HOURS=168
  AGENCY_DB=/opt/data/agency.db
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

DB_PATH = os.getenv("AGENCY_DB", "/opt/data/agency.db")
PROVIDER_NAME = os.getenv("BROWSER_PROVIDER", "steel").strip().lower()
MAX_CONCURRENCY = int(os.getenv("BROWSER_MAX_CONCURRENCY", "6"))
TIMEOUT_S = int(os.getenv("BROWSER_TIMEOUT_SECONDS", "18"))

# --- per-lead research budget ----------------------------------------------
# One slow site used to be able to hold a lead for minutes: each fetch had its
# own timeout and nothing tracked the lead as a whole. These bound the entire
# research lifecycle for one lead, not a single request.
#
# TARGET is what we aim for and report against; HARD_LIMIT is where research
# stops whatever it has. SAVE_MARGIN is kept back so there is always time to
# write the findings down — running the clock to zero and then failing to save
# would waste the work as well as the time.
TARGET_SECONDS = float(os.getenv("NOVA_RESEARCH_TARGET_SECONDS", "30"))
HARD_LIMIT_SECONDS = float(os.getenv("NOVA_RESEARCH_HARD_LIMIT_SECONDS", "40"))
SAVE_MARGIN_SECONDS = float(os.getenv("NOVA_RESEARCH_SAVE_MARGIN_SECONDS", "3"))
MAX_PAGES_PER_LEAD = int(os.getenv("NOVA_MAX_PAGES_PER_LEAD", "3"))

# Evidence thresholds. Enforced by NOVA's own stopping rule rather than here —
# the server cannot judge whether an observation is any good — but exposed
# through research_status so the agent is told the same numbers the operator
# configured, instead of carrying them in its prompt.
MIN_OBSERVATIONS = int(os.getenv("NOVA_MIN_OBSERVATIONS", "3"))
TARGET_OBSERVATIONS = int(os.getenv("NOVA_TARGET_OBSERVATIONS", "4"))
MAX_OBSERVATIONS = int(os.getenv("NOVA_MAX_OBSERVATIONS", "5"))
CACHE_TTL_H = int(os.getenv("BROWSER_CACHE_TTL_HOURS", "168"))
PER_DOMAIN_MIN_INTERVAL_S = float(os.getenv("BROWSER_DOMAIN_INTERVAL_SECONDS", "2"))
MAX_BYTES = int(os.getenv("BROWSER_MAX_BYTES", "2000000"))

_sem = threading.Semaphore(MAX_CONCURRENCY)
_domain_lock = threading.Lock()
_domain_last: Dict[str, float] = {}


# ----------------------------------------------------------------------------
# URL validation / SSRF protection
# ----------------------------------------------------------------------------

_BLOCKED_HOSTS = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata", "metadata.google.internal", "instance-data",
}


class UnsafeURL(ValueError):
    """Raised when a URL fails validation. The message is safe to show."""


def validate_url(raw: str) -> str:
    """Return a normalised URL, or raise UnsafeURL.

    Blocks non-http(s) schemes, credentials in the URL, and any host that
    resolves to loopback, private, link-local, or cloud-metadata space.
    DNS is resolved here and *every* returned address is checked, so a
    hostname that resolves to 169.254.169.254 is rejected even though the
    name itself looks innocuous.
    """
    if not raw or not isinstance(raw, str):
        raise UnsafeURL("empty url")
    raw = raw.strip()
    if len(raw) > 2048:
        raise UnsafeURL("url too long")

    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("scheme %r not allowed; only http/https" % (parts.scheme or "",))
    if parts.username or parts.password:
        raise UnsafeURL("credentials in url are not allowed")

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURL("missing host")
    if host in _BLOCKED_HOSTS or host.endswith(".local") or host.endswith(".internal"):
        raise UnsafeURL("host %r is blocked" % host)

    # Resolve and check every address the name maps to.
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURL("dns resolution failed: %s" % exc)

    if not infos:
        raise UnsafeURL("host did not resolve")

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise UnsafeURL("unparseable address for host")
        if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            raise UnsafeURL("host resolves to a blocked address range (%s)" % ip)
        # AWS/GCP/Azure instance metadata
        if str(ip) in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200"):
            raise UnsafeURL("cloud metadata endpoint is blocked")

    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/",
                                    parts.query, ""))


def _domain_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _throttle(domain: str) -> None:
    """Politeness gate: at most one request per domain per interval."""
    while True:
        with _domain_lock:
            now = time.time()
            last = _domain_last.get(domain, 0.0)
            wait = (last + PER_DOMAIN_MIN_INTERVAL_S) - now
            if wait <= 0:
                _domain_last[domain] = now
                return
        time.sleep(min(wait, 5.0))


# ----------------------------------------------------------------------------
# Per-lead budget
# ----------------------------------------------------------------------------

_budget_lock = threading.Lock()
_budgets: Dict[str, Dict[str, Any]] = {}


def _budget(lead_id: str) -> Dict[str, Any]:
    """The running budget for a lead, started on first contact.

    Keyed by lead, not by thread, because six leads research concurrently and
    each one's clock has to be its own.
    """
    with _budget_lock:
        b = _budgets.get(lead_id)
        if b is None:
            b = {"started": time.time(), "pages": 0, "cache_hits": 0,
                 "succeeded": 0, "failed": 0, "refused": 0,
                 "fetch_seconds": 0.0, "exhausted": False}
            _budgets[lead_id] = b
            _run_start(lead_id)
        return b


def _elapsed(b: Dict[str, Any]) -> float:
    return time.time() - b["started"]


def _remaining(b: Dict[str, Any]) -> float:
    return HARD_LIMIT_SECONDS - _elapsed(b)


def budget_state(lead_id: str) -> Dict[str, Any]:
    """What NOVA is allowed to do next, in numbers rather than prose."""
    b = _budget(lead_id)
    with _budget_lock:
        elapsed = _elapsed(b)
        remaining = max(0.0, HARD_LIMIT_SECONDS - elapsed)
        usable = max(0.0, remaining - SAVE_MARGIN_SECONDS)
        return {
            "lead_id": lead_id,
            "elapsed_seconds": round(elapsed, 2),
            "remaining_seconds": round(remaining, 2),
            "usable_seconds": round(usable, 2),
            "target_seconds": TARGET_SECONDS,
            "hard_limit_seconds": HARD_LIMIT_SECONDS,
            "pages_fetched": b["pages"],
            "pages_remaining": max(0, MAX_PAGES_PER_LEAD - b["pages"]),
            "max_pages": MAX_PAGES_PER_LEAD,
            "cache_hits": b["cache_hits"],
            "min_observations": MIN_OBSERVATIONS,
            "target_observations": TARGET_OBSERVATIONS,
            "max_observations": MAX_OBSERVATIONS,
            "may_fetch": bool(usable > 0 and b["pages"] < MAX_PAGES_PER_LEAD),
            "stop_reason": (
                "page limit reached" if b["pages"] >= MAX_PAGES_PER_LEAD else
                "time budget exhausted" if usable <= 0 else None),
        }


def _run_start(lead_id: str) -> None:
    try:
        with _db() as con:
            con.execute(
                "INSERT INTO research_runs (lead_id, started_at) "
                "VALUES (?, datetime('now')) "
                "ON CONFLICT(lead_id) DO UPDATE SET started_at=datetime('now'),"
                "  completed_at=NULL, duration_ms=NULL, pages_attempted=0,"
                "  pages_succeeded=0, pages_from_cache=0, pages_failed=0,"
                "  pages_refused=0, observations_count=NULL, budget_exhausted=0,"
                "  timed_out=0, research_status=NULL, fetch_seconds=0",
                (lead_id,))
    except Exception:
        pass          # measurement must never break research


def _run_update(lead_id: str, b: Dict[str, Any]) -> None:
    """Persist the counters, from a snapshot taken under the lock.

    Reading b field by field while another thread is incrementing it is how a
    page goes missing: two threads read the same total, both write it back, and
    one increment is lost. Six leads research at once, so this is not
    theoretical — it cost one page in twelve when the snapshot was taken
    outside the lock.
    """
    with _budget_lock:
        snap = (b["pages"], b["succeeded"], b["cache_hits"], b["failed"],
                b["refused"], 1 if b["exhausted"] else 0,
                round(b["fetch_seconds"], 3), int(_elapsed(b) * 1000))
    try:
        with _db() as con:
            # max() so a late-arriving stale snapshot cannot walk a counter
            # backwards; the counters only ever grow.
            con.execute(
                "UPDATE research_runs SET"
                "  pages_attempted=MAX(pages_attempted, ?),"
                "  pages_succeeded=MAX(pages_succeeded, ?),"
                "  pages_from_cache=MAX(pages_from_cache, ?),"
                "  pages_failed=MAX(pages_failed, ?),"
                "  pages_refused=MAX(pages_refused, ?),"
                "  budget_exhausted=MAX(budget_exhausted, ?),"
                "  fetch_seconds=MAX(fetch_seconds, ?),"
                "  duration_ms=MAX(COALESCE(duration_ms, 0), ?)"
                " WHERE lead_id=?", snap + (lead_id,))
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _audit(lead_id, url, tier, status, http_status, duration_ms, nbytes, error) -> None:
    try:
        with _db() as con:
            con.execute(
                "INSERT INTO research_fetches (lead_id,url,domain,tier,status,"
                "http_status,duration_ms,bytes,error) VALUES (?,?,?,?,?,?,?,?,?)",
                (lead_id, url, _domain_of(url), tier, status, http_status,
                 duration_ms, nbytes, (error or "")[:500]),
            )
    except Exception:
        pass  # auditing must never break a fetch


def _cache_get(url: str) -> Optional[Dict[str, Any]]:
    try:
        with _db() as con:
            row = con.execute(
                "SELECT structured_data, retrieved_at FROM research_cache "
                "WHERE url=? AND (expires_at IS NULL OR expires_at > datetime('now'))",
                (url,),
            ).fetchone()
        if row and row[0]:
            data = json.loads(row[0])
            data["_cached"] = True
            data["_retrieved_at"] = row[1]
            return data
    except Exception:
        pass
    return None


def _cache_put(lead_id, url, page_type, method, payload, status) -> None:
    try:
        blob = json.dumps(payload)
        chash = hashlib.sha256(
            (payload.get("markdown") or payload.get("html") or "").encode("utf-8")
        ).hexdigest()
        with _db() as con:
            con.execute(
                "INSERT INTO research_cache (lead_id,url,source_url,page_type,"
                "retrieval_method,content_hash,structured_data,status,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?, datetime('now', ?)) "
                "ON CONFLICT(url) DO UPDATE SET lead_id=excluded.lead_id,"
                "page_type=excluded.page_type,retrieval_method=excluded.retrieval_method,"
                "content_hash=excluded.content_hash,structured_data=excluded.structured_data,"
                "status=excluded.status,retrieved_at=datetime('now'),"
                "expires_at=excluded.expires_at",
                (lead_id, url, url, page_type, method, chash, blob, status,
                 "+%d hours" % CACHE_TTL_H),
            )
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Provider interface
# ----------------------------------------------------------------------------

class BrowserProvider(ABC):
    name = "abstract"

    @abstractmethod
    def health_check(self) -> Dict[str, Any]: ...

    @abstractmethod
    def fetch(self, url: str, formats: List[str],
              timeout: Optional[int] = None) -> Dict[str, Any]:
        """Return {markdown?, html?, links[], metadata{}} for a validated URL.

        `timeout` is the caller's ceiling, which may be lower than the
        configured default when a lead's research budget is nearly spent.
        """

    @abstractmethod
    def screenshot(self, url: str) -> Dict[str, Any]: ...


class SteelBrowserProvider(BrowserProvider):
    """Steel.dev via its REST surface — no local browser, no CDP dependency."""

    name = "steel"

    def __init__(self) -> None:
        self.api_key = os.getenv("STEEL_API_KEY", "").strip()
        self.base = os.getenv("STEEL_BASE_URL", "https://api.steel.dev").rstrip("/")
        if not self.api_key:
            raise RuntimeError("STEEL_API_KEY is not set for this profile")

    def _post(self, path: str, body: Dict[str, Any], timeout: int) -> Dict[str, Any]:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"steel-api-key": self.api_key,
                     "Content-Type": "application/json",
                     "User-Agent": "hermes-agency-research/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES)
            return json.loads(raw.decode("utf-8", errors="replace"))

    def health_check(self) -> Dict[str, Any]:
        try:
            out = self._post("/v1/scrape", {"url": "https://example.com",
                                            "format": ["markdown"]}, 30)
            ok = bool((out.get("content") or {}).get("markdown"))
            return {"provider": self.name, "healthy": ok}
        except Exception as exc:
            return {"provider": self.name, "healthy": False,
                    "error": type(exc).__name__}

    def fetch(self, url: str, formats: List[str],
              timeout: Optional[int] = None) -> Dict[str, Any]:
        out = self._post("/v1/scrape", {"url": url, "format": formats},
                         int(timeout or TIMEOUT_S))
        content = out.get("content") or {}
        meta = out.get("metadata") or {}
        return {
            "url": url,
            "markdown": content.get("markdown"),
            "html": content.get("html"),
            "links": out.get("links") or [],
            "metadata": {
                "status_code": meta.get("statusCode"),
                "title": meta.get("title"),
                "description": meta.get("description"),
                "language": meta.get("language"),
                "og_title": meta.get("ogTitle"),
                "og_site_name": meta.get("ogSiteName"),
                "published_time": meta.get("publishedTime"),
                "json_ld": meta.get("jsonLd") or [],
                "source_url": meta.get("urlSource") or url,
            },
        }

    def screenshot(self, url: str) -> Dict[str, Any]:
        out = self._post("/v1/screenshot", {"url": url}, TIMEOUT_S)
        return {"url": url, "screenshot": out}


def get_provider() -> BrowserProvider:
    if PROVIDER_NAME == "steel":
        return SteelBrowserProvider()
    raise RuntimeError("unknown BROWSER_PROVIDER: %r" % PROVIDER_NAME)


# ----------------------------------------------------------------------------
# Public operation
# ----------------------------------------------------------------------------

def research_fetch(url: str, lead_id: Optional[str] = None,
                   page_type: str = "other", refresh: bool = False) -> Dict[str, Any]:
    """Validated, throttled, cached, audited, budgeted page fetch.

    The budget is enforced here rather than trusted to the agent. NOVA decides
    which pages are worth reading; it does not decide how long it may take.
    """
    started = time.time()
    try:
        safe = validate_url(url)
    except UnsafeURL as exc:
        _audit(lead_id, url, "browser", "blocked", None, 0, 0, str(exc))
        return {"status": "blocked", "url": url, "error": str(exc)}

    b = _budget(lead_id) if lead_id else None

    # --- cache first, and free ---------------------------------------------
    # Checked before any budget arithmetic: a cached page costs no Steel call
    # and effectively no time, so refusing one on time grounds would throw away
    # evidence for nothing.
    if not refresh:
        hit = _cache_get(safe)
        if hit:
            if b is not None:
                with _budget_lock:
                    b["pages"] += 1
                    b["cache_hits"] += 1
                    b["succeeded"] += 1
                _run_update(lead_id, b)
            _audit(lead_id, safe, "cache", "ok", 200, 0, 0, None)
            hit["status"] = "ok"
            hit["source"] = "cache"
            hit["_cached"] = True
            return hit

    # --- budget gates -------------------------------------------------------
    if b is not None:
        with _budget_lock:
            pages = b["pages"]
        if pages >= MAX_PAGES_PER_LEAD:
            with _budget_lock:
                b["refused"] += 1
            _run_update(lead_id, b)
            _audit(lead_id, safe, "browser", "refused", None, 0, 0,
                   "page limit %d reached" % MAX_PAGES_PER_LEAD)
            return {"status": "budget_exhausted", "url": safe,
                    "reason": "page_limit",
                    "error": "already read %d pages for this lead; finalise with "
                             "the evidence you have" % pages,
                    "budget": budget_state(lead_id)}

        usable = _remaining(b) - SAVE_MARGIN_SECONDS
        if usable <= 0:
            with _budget_lock:
                b["refused"] += 1
                b["exhausted"] = True
            _run_update(lead_id, b)
            _audit(lead_id, safe, "browser", "refused", None, 0, 0,
                   "budget exhausted")
            return {"status": "budget_exhausted", "url": safe,
                    "reason": "time_limit",
                    "error": "research budget for this lead is spent; save what "
                             "you have now and do not fetch again",
                    "budget": budget_state(lead_id)}
        # A request must never be allowed to outlive the lead's budget. Eighteen
        # seconds is the ceiling, not the floor.
        effective_timeout = max(1, int(min(TIMEOUT_S, usable)))
    else:
        effective_timeout = TIMEOUT_S

    _throttle(_domain_of(safe))

    # Waiting for a slot counts against the lead's clock too, so the wait is
    # bounded by what is left rather than by a multiple of the fetch timeout.
    wait_budget = effective_timeout if b is None else max(
        0.5, min(float(effective_timeout), _remaining(b) - SAVE_MARGIN_SECONDS))
    acquired = _sem.acquire(timeout=wait_budget)
    if not acquired:
        if b is not None:
            with _budget_lock:
                b["failed"] += 1
            _run_update(lead_id, b)
        _audit(lead_id, safe, "browser", "timeout", None, 0, 0, "concurrency wait")
        return {"status": "failed", "url": safe,
                "error": "browser busy; try again"}

    last_err = None
    try:
        # One retry only when time genuinely allows it. A second attempt that
        # cannot finish inside the budget is worse than none: it burns the
        # margin reserved for saving the work.
        for attempt in (1, 2):
            if b is not None and (_remaining(b) - SAVE_MARGIN_SECONDS) <= 0:
                last_err = last_err or "budget exhausted before attempt %d" % attempt
                with _budget_lock:
                    b["exhausted"] = True
                break
            try:
                prov = get_provider()
                # NOTE: "links" is NOT a valid Steel format value — the API
                # returns 400. Links and metadata come back alongside markdown
                # automatically, so request markdown only.
                per_call = effective_timeout
                if b is not None:
                    per_call = max(1, int(min(
                        effective_timeout,
                        _remaining(b) - SAVE_MARGIN_SECONDS)))
                data = prov.fetch(safe, ["markdown"], timeout=per_call)
                ms = int((time.time() - started) * 1000)
                nbytes = len(data.get("markdown") or "")
                http = (data.get("metadata") or {}).get("status_code")
                data["status"] = "ok"
                data["_cached"] = False
                data["source"] = "steel"
                _cache_put(lead_id, safe, page_type, prov.name, data, "ok")
                _audit(lead_id, safe, "browser", "ok", http, ms, nbytes, None)
                if b is not None:
                    with _budget_lock:
                        b["pages"] += 1
                        b["succeeded"] += 1
                        b["fetch_seconds"] += (time.time() - started)
                    _run_update(lead_id, b)
                return data
            except urllib.error.HTTPError as exc:
                last_err = "http %s" % exc.code
                if exc.code < 500 and exc.code != 429:
                    break          # client error: retrying will not help
            except Exception as exc:
                last_err = "%s: %s" % (type(exc).__name__, exc)
            # Back off only if there is room to try again afterwards.
            if b is not None and (_remaining(b) - SAVE_MARGIN_SECONDS) <= 1.5:
                break
            time.sleep(1.5 * attempt)
    finally:
        _sem.release()

    ms = int((time.time() - started) * 1000)
    _audit(lead_id, safe, "browser", "failed", None, ms, 0, last_err)
    if b is not None:
        with _budget_lock:
            b["pages"] += 1
            b["failed"] += 1
            b["fetch_seconds"] += (time.time() - started)
        _run_update(lead_id, b)
    return {"status": "failed", "url": safe, "error": last_err or "unknown",
            "budget": budget_state(lead_id) if lead_id else None}


def finalize_research(lead_id: str, observations: int,
                      status: str = "ok") -> Dict[str, Any]:
    """Close the run and write down what it cost. Called when NOVA saves."""
    b = _budget(lead_id)
    elapsed = _elapsed(b)
    try:
        with _db() as con:
            con.execute(
                "UPDATE research_runs SET completed_at=datetime('now'),"
                "  duration_ms=?, observations_count=?, research_status=?,"
                "  budget_exhausted=?, timed_out=?, pages_attempted=?,"
                "  pages_succeeded=?, pages_from_cache=?, pages_failed=?,"
                "  pages_refused=?, fetch_seconds=? WHERE lead_id=?",
                (int(elapsed * 1000), observations, status,
                 1 if b["exhausted"] else 0,
                 1 if elapsed >= HARD_LIMIT_SECONDS else 0,
                 b["pages"], b["succeeded"], b["cache_hits"], b["failed"],
                 b["refused"], round(b["fetch_seconds"], 3), lead_id))
    except Exception:
        pass
    with _budget_lock:
        _budgets.pop(lead_id, None)
    return {"lead_id": lead_id, "duration_ms": int(elapsed * 1000),
            "observations": observations, "status": status,
            "pages": b["pages"], "from_cache": b["cache_hits"]}


# ----------------------------------------------------------------------------
# MCP server
# ----------------------------------------------------------------------------

TOOLS = [
    {
        "name": "fetch_page",
        "description": (
            "Fetch a public web page and return it as markdown, with its links "
            "and metadata (title, description, OpenGraph, JSON-LD). Only public "
            "http/https addresses are allowed; private, loopback and cloud "
            "metadata addresses are refused. Results are cached, so re-reading "
            "an unchanged page is cheap. ALWAYS check the returned `status` "
            "field — ok, blocked, failed, or budget_exhausted — before using "
            "any content. On blocked, failed or budget_exhausted, do not "
            "invent the page contents. Every result carries a `budget` object: "
            "when `may_fetch` is false you have run out of pages or time, and "
            "must finalise with the evidence already gathered."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute http(s) URL"},
                "lead_id": {"type": "string", "description": "Lead this fetch belongs to"},
                "page_type": {
                    "type": "string",
                    "enum": ["home", "about", "services", "contact", "pricing",
                             "booking", "locations", "other"],
                },
                "refresh": {"type": "boolean", "description": "Bypass the cache"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_health",
        "description": "Check whether the browser provider is reachable.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "research_status",
        "description": (
            "How much research budget is left for this lead: seconds remaining, "
            "pages already read, pages still allowed, and the observation "
            "thresholds you are working to. Call it before deciding whether to "
            "fetch another page. When `may_fetch` is false, stop and save what "
            "you have — further fetches will be refused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"lead_id": {"type": "string"}},
            "required": ["lead_id"],
        },
    },
]


def _dispatch(name: str, args: Dict[str, Any]) -> str:
    if name == "fetch_page":
        lead = (args.get("lead_id") or "") or None
        out = research_fetch(
            args.get("url", ""),
            lead,
            args.get("page_type") or "other",
            bool(args.get("refresh")),
        )
        # Attached to every result so the stopping decision is made from
        # numbers the agent has just been handed, not from its own counting.
        if lead and "budget" not in out:
            out["budget"] = budget_state(lead)
        return json.dumps(out, ensure_ascii=False)[:400000]
    if name == "research_status":
        lead = (args.get("lead_id") or "").strip()
        if not lead:
            return json.dumps({"error": "lead_id is required"})
        return json.dumps(budget_state(lead))
    if name == "browser_health":
        try:
            return json.dumps(get_provider().health_check())
        except Exception as exc:
            return json.dumps({"healthy": False, "error": type(exc).__name__})
    raise KeyError("unknown tool: %s" % name)


def _serve() -> None:
    """Minimal MCP stdio server: newline-delimited JSON-RPC on stdin/stdout.

    Implemented directly rather than against an SDK because the bundled `mcp`
    package version moves its server API between releases; the wire protocol
    does not.
    """
    import sys

    out = sys.stdout

    def send(payload: Dict[str, Any]) -> None:
        out.write(json.dumps(payload) + "\n")
        out.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue

        method = msg.get("method")
        mid = msg.get("id")

        # Notifications carry no id and must never be answered.
        if mid is None:
            continue

        try:
            if method == "initialize":
                client_ver = (msg.get("params") or {}).get("protocolVersion")
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": client_ver or "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agency-research", "version": "1.0.0"},
                }})
            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                params = msg.get("params") or {}
                text = _dispatch(params.get("name", ""), params.get("arguments") or {})
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                }})
            elif method in ("ping", "shutdown"):
                send({"jsonrpc": "2.0", "id": mid, "result": {}})
            else:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32601, "message": "method not found: %s" % method}})
        except Exception as exc:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32603, "message": "%s: %s" % (type(exc).__name__, exc)}})


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        print(json.dumps(get_provider().health_check()))
        for u in ("http://127.0.0.1/x", "file:///etc/passwd", "http://169.254.169.254/",
                  "http://localhost:8080/", "ftp://example.com/", "https://example.com"):
            try:
                print("  ALLOW ", validate_url(u))
            except UnsafeURL as e:
                print("  BLOCK ", u, "->", e)
    else:
        _serve()
