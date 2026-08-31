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
MAX_CONCURRENCY = int(os.getenv("BROWSER_MAX_CONCURRENCY", "2"))
TIMEOUT_S = int(os.getenv("BROWSER_TIMEOUT_SECONDS", "45"))
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
    def fetch(self, url: str, formats: List[str]) -> Dict[str, Any]:
        """Return {markdown?, html?, links[], metadata{}} for a validated URL."""

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

    def fetch(self, url: str, formats: List[str]) -> Dict[str, Any]:
        out = self._post("/v1/scrape", {"url": url, "format": formats}, TIMEOUT_S)
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
    """Validated, throttled, cached, audited page fetch."""
    started = time.time()
    try:
        safe = validate_url(url)
    except UnsafeURL as exc:
        _audit(lead_id, url, "browser", "blocked", None, 0, 0, str(exc))
        return {"status": "blocked", "url": url, "error": str(exc)}

    if not refresh:
        hit = _cache_get(safe)
        if hit:
            _audit(lead_id, safe, "cache", "ok", 200, 0, 0, None)
            hit["status"] = "ok"
            return hit

    _throttle(_domain_of(safe))
    acquired = _sem.acquire(timeout=TIMEOUT_S * 2)
    if not acquired:
        _audit(lead_id, safe, "browser", "timeout", None, 0, 0, "concurrency wait")
        return {"status": "failed", "url": safe, "error": "browser busy; try again"}

    last_err = None
    try:
        for attempt in (1, 2):
            try:
                prov = get_provider()
                # NOTE: "links" is NOT a valid Steel format value — the API
                # returns 400. Links and metadata come back alongside markdown
                # automatically, so request markdown only.
                data = prov.fetch(safe, ["markdown"])
                ms = int((time.time() - started) * 1000)
                nbytes = len(data.get("markdown") or "")
                http = (data.get("metadata") or {}).get("status_code")
                data["status"] = "ok"
                data["_cached"] = False
                _cache_put(lead_id, safe, page_type, prov.name, data, "ok")
                _audit(lead_id, safe, "browser", "ok", http, ms, nbytes, None)
                return data
            except urllib.error.HTTPError as exc:
                last_err = "http %s" % exc.code
                if exc.code < 500 and exc.code != 429:
                    break          # client error: retrying will not help
            except Exception as exc:
                last_err = "%s: %s" % (type(exc).__name__, exc)
            time.sleep(1.5 * attempt)
    finally:
        _sem.release()

    ms = int((time.time() - started) * 1000)
    _audit(lead_id, safe, "browser", "failed", None, ms, 0, last_err)
    return {"status": "failed", "url": safe, "error": last_err or "unknown"}


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
            "field — it is one of ok, blocked, or failed — before using any "
            "content. On blocked or failed, do not invent the page contents."
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
]


def _dispatch(name: str, args: Dict[str, Any]) -> str:
    if name == "fetch_page":
        out = research_fetch(
            args.get("url", ""),
            (args.get("lead_id") or "") or None,
            args.get("page_type") or "other",
            bool(args.get("refresh")),
        )
        return json.dumps(out, ensure_ascii=False)[:400000]
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
