# Agency layer

Outreach-agency state and agent definitions layered on top of the Hermes
deployment. These files are **reference and reproducibility artifacts** — the
running system reads its live copies from the persistent disk at `/opt/data`.

## Contents

| File | Purpose |
|---|---|
| `schema.sql` | `agency.db` schema — 17 tables. Applied to `/opt/data/agency.db`. |
| `seed_state_transitions.sql` | The 85 legal lead-state transitions. |
| `souls/ALL_SOULS.md` | Role definitions for ARIA, SENTINEL, ECHO, LEO, ORBIT. |
| `souls/nova.md` | Role definition for NOVA (research). |
| `research_mcp.py` | NOVA's research tools, served over MCP stdio. |

## research_mcp.py

A stdio MCP server giving NOVA two tools: `fetch_page` and `browser_health`.
Deployed to `/opt/data/agency/research_mcp.py` and wired into **NOVA's profile
only** — no other agent can reach it.

`BrowserProvider` is the interface; `SteelBrowserProvider` is the current
implementation, talking to Steel's REST `/v1/scrape`. No local browser, no CDP,
no Playwright dependency. Swapping providers means one subclass and one env var.

Safety properties, all exercised by `--selftest`:

- **SSRF**: http/https only. DNS is resolved and *every* returned address is
  checked, so a hostname pointing at `169.254.169.254`, loopback, RFC1918, or
  link-local space is refused even when the name looks innocuous.
- **Throttling**: one request per domain per interval, plus a concurrency cap.
- **Caching**: keyed on URL with a content hash, so an unchanged page is not
  re-fetched.
- **Auditing**: every attempt lands in `research_fetches` — including blocked
  and failed ones. This table is the source of truth for whether a page was
  actually retrieved; do not trust an agent's claim that it read something.
- **Retries**: transient and 429 responses retry; 4xx does not.

Run `python3 research_mcp.py --selftest` to check provider health and print the
URL-validation decisions.

Config comes from the environment only — `BROWSER_PROVIDER`, `STEEL_API_KEY`,
`STEEL_BASE_URL`, `BROWSER_MAX_CONCURRENCY`, `BROWSER_TIMEOUT_SECONDS`,
`BROWSER_CACHE_TTL_HOURS`. The profile's `mcp_servers` block passes these
through by `${VAR}` reference, so the secret stays in `.env`.

## Agents

One Hermes instance runs seven profiles under `gateway.multiplex_profiles`.
MAYA (the `default` profile) is `kanban.orchestrator_profile` and the only
agent with a chat surface; the rest are worker profiles reached through Kanban
task assignment.

| Profile | Role |
|---|---|
| `default` | MAYA — orchestrator |
| `nova` | Research |
| `aria` | Outreach copy |
| `sentinel` | QA / compliance gate |
| `echo` | Follow-up scheduling |
| `leo` | Reply classification and sales |
| `orbit` | Analytics (read-only) |

## Secrets

**No credentials belong in this repository — it is public.**

Secrets live in per-profile `.env` files on the persistent disk (mode `600`)
or in Render environment variables. `sender_accounts.auth_secret_ref` stores
the *name* of an environment variable, never a value. Nothing in `agency.db`
holds a credential.

Credential isolation is enforced by the profile boundary: a key written to one
profile's `.env` is not visible to any other profile.

## Applying the schema

```bash
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("/opt/data/agency.db")
con.executescript(open("agency/schema.sql").read())
con.executescript(open("agency/seed_state_transitions.sql").read())
con.commit()
PY
```

`agency.db` is separate from Hermes's own `kanban.db` so a Hermes upgrade can
never migrate or clobber campaign data.
