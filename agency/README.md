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
