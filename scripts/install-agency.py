#!/usr/bin/env python3
"""Install or update the agency system on a Hermes container.

Safe to run repeatedly. Every step checks the current state before changing
anything, so a redeploy converges on the same result instead of stacking a
second copy of it. In particular the cron jobs are matched by name: running
this twice does not give you two ECHO schedulers quietly double-sending.

    python3 scripts/install-agency.py            # install / update
    python3 scripts/install-agency.py --check    # report only, change nothing

What it does NOT do: write secrets. Profile .env files, API tokens and the
runtime databases live on the persistent disk and are never touched here.
This script installs code, schema and schedules; the operator supplies
credentials once, out of band.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

HERMES_HOME = pathlib.Path(os.getenv("HERMES_HOME", "/opt/data"))
AGENCY_DIR = HERMES_HOME / "agency"
AGENCY_DB = pathlib.Path(os.getenv("AGENCY_DB", str(HERMES_HOME / "agency.db")))
HERMES_BIN = os.getenv("HERMES_BIN", "/opt/hermes/.venv/bin/hermes")
REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "agency"

PROFILES = ("nova", "aria", "sentinel", "echo", "leo", "orbit")   # + root = MAYA

# Discord delivery needs a concrete channel, not just the platform name: a
# bare "discord" target resolves to nothing and the job runs but posts
# nowhere. The ids belong to the operator, so they come from the environment
# rather than being baked into a public repository.
ALERTS_CHANNEL = os.getenv("AGENCY_DISCORD_ALERTS_CHANNEL", "").strip()
REPORT_CHANNEL = os.getenv("AGENCY_DISCORD_REPORT_CHANNEL", "").strip()


def discord(channel_id: str) -> str:
    return "discord:%s" % channel_id if channel_id else "discord"


# name -> (profile home, schedule, script, delivery)
# Matched by name, so an existing job is left alone rather than duplicated.
CRON_JOBS = {
    # MAYA's orchestration tick: the job that makes the pipeline advance
    # without a human. Deterministic, --no-agent, delivered locally because
    # its output is a work log, not something to page anyone about.
    "maya-orchestrator": (HERMES_HOME, "every 2m", "maya_orchestrator.py",
                          "local"),
    "echo-followups": (HERMES_HOME / "profiles" / "echo", "every 2m",
                       "echo_followups.py", "local"),
    "supabase-lead-sync": (HERMES_HOME, "every 2m",
                           "supabase_lead_sync.py", "local"),
    "review-alerts": (HERMES_HOME, "every 2m", "review_alerts.py",
                      discord(ALERTS_CHANNEL)),
    # Delivered locally on purpose: the script posts the report itself as
    # Discord embed cards (orbit_embeds). Letting the cron deliver stdout would
    # post the plaintext a second time, wrapped in a "Cronjob Response" header.
    "orbit-daily": (HERMES_HOME, "0 8 * * *", "orbit_daily.py", "local"),
}
# The alerts job runs from the root profile deliberately: Discord is
# configured there and nowhere else, so a job scheduled on a sub-profile
# executes fine and then fails to deliver. ORBIT runs there for the same
# reason: the bot token it posts with lives in the root env.

# Cron wrappers: repo path -> where the profile expects to find it.
WRAPPERS = {
    "maya_orchestrator.py": HERMES_HOME / "scripts",
    "echo_followups.py": HERMES_HOME / "profiles" / "echo" / "scripts",
    "review_alerts.py": HERMES_HOME / "scripts",
    "supabase_lead_sync.py": HERMES_HOME / "scripts",
    "orbit_daily.py": HERMES_HOME / "scripts",
}

MODULES = ("pipeline.py", "lead_ingest.py", "orchestrator.py", "followups.py",
           "inbound_processor.py", "echo_tick.py", "review.py",
           "review_tick.py", "orbit.py", "orbit_embeds.py", "agency_mcp.py",
           "research_mcp.py",
           "research_metrics.py", "supabase_sync.py",
           "schema.sql", "seed_state_transitions.sql")

# Owned by the Auto_Email repository, not vendored here — one source of truth.
MAILHUB_MCP_URL = ("https://raw.githubusercontent.com/AbhijitDengale/"
                   "Auto_Email/main/mailhub_mcp.py")

OK, WARN, BAD = [], [], []


def ok(msg):
    OK.append(msg)
    print("  ok    %s" % msg)


def warn(msg):
    WARN.append(msg)
    print("  WARN  %s" % msg)


def bad(msg):
    BAD.append(msg)
    print("  FAIL  %s" % msg)


def hermes(home: pathlib.Path, *args, check=False):
    env = dict(os.environ, HERMES_HOME=str(home))
    return subprocess.run([HERMES_BIN, *args], env=env, check=check,
                          capture_output=True, text=True, timeout=180)


# --------------------------------------------------------------------------
# 1. source
# --------------------------------------------------------------------------

def install_source(check: bool) -> None:
    print("\n[1] agency source -> %s" % AGENCY_DIR)
    if not check:
        AGENCY_DIR.mkdir(parents=True, exist_ok=True)
    for name in MODULES:
        src, dst = SRC / name, AGENCY_DIR / name
        if not src.exists():
            bad("%s missing from the repository" % name)
            continue
        same = dst.exists() and dst.read_bytes() == src.read_bytes()
        if same:
            ok("%s up to date" % name)
        elif check:
            warn("%s would be %s" % (name, "updated" if dst.exists() else "installed"))
        else:
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o750)
            ok("%s %s" % (name, "updated" if dst.exists() else "installed"))

    for name, target in WRAPPERS.items():
        src = SRC / "scripts" / name
        if not src.exists():
            bad("cron wrapper %s missing from the repository" % name)
            continue
        dst = target / name
        if dst.exists() and dst.read_bytes() == src.read_bytes():
            ok("wrapper %s up to date" % name)
        elif check:
            warn("wrapper %s would be installed into %s" % (name, target))
        else:
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o750)
            ok("wrapper %s installed into %s" % (name, target))


def install_mailhub_mcp(check: bool) -> None:
    print("\n[2] MailHub MCP client")
    dst = AGENCY_DIR / "mailhub_mcp.py"
    if dst.exists():
        ok("mailhub_mcp.py present (owned by the Auto_Email repo)")
        return
    if check:
        warn("mailhub_mcp.py absent; would fetch from the Auto_Email repo")
        return
    try:
        import urllib.request
        with urllib.request.urlopen(MAILHUB_MCP_URL, timeout=60) as r:
            body = r.read()
        if b"def " not in body:
            raise ValueError("fetched file does not look like Python")
        dst.write_bytes(body)
        os.chmod(dst, 0o750)
        ok("mailhub_mcp.py fetched from the Auto_Email repo")
    except Exception as exc:
        warn("could not fetch mailhub_mcp.py (%s) — copy it manually" % exc)


# --------------------------------------------------------------------------
# 3. schema and migrations
# --------------------------------------------------------------------------

def apply_migrations(check: bool) -> None:
    print("\n[3] schema and migrations -> %s" % AGENCY_DB)
    fresh = not AGENCY_DB.exists()
    if fresh and check:
        warn("database absent; would create it from schema.sql")
        return

    con = sqlite3.connect(AGENCY_DB, isolation_level=None, timeout=30)
    try:
        if fresh:
            con.executescript((SRC / "schema.sql").read_text(encoding="utf-8"))
            con.executescript((SRC / "seed_state_transitions.sql")
                              .read_text(encoding="utf-8"))
            ok("created a new database from schema.sql")

        # A ledger, so "which migrations are pending" is a fact rather than a
        # guess. Migrations applied before this table existed are recorded on
        # first run by probing for what they created.
        con.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "  name TEXT PRIMARY KEY,"
                    "  applied_at TEXT NOT NULL DEFAULT (datetime('now')))")

        def has_column(table, column):
            return any(r[1] == column for r in
                       con.execute("PRAGMA table_info(%s)" % table))

        # Fingerprints: what each migration leaves behind if it already ran.
        FINGERPRINTS = {
            "001_phase_c_pipeline.sql":
                lambda: has_column("leads", "state"),
            "002_phase_de_followups.sql":
                lambda: bool(con.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='followups'"
                    ).fetchone()),
            "003_escalation_paths.sql":
                lambda: bool(con.execute(
                    "SELECT 1 FROM state_transitions WHERE from_state="
                    "'FOLLOWUP_WAITING' AND to_state='HUMAN_REVIEW'"
                    ).fetchone()),
            "004_followup_lifecycle.sql":
                lambda: has_column("followups", "dispatched_at"),
            "005_campaign_pause_and_generation.sql":
                lambda: has_column("followups", "last_blocked_reason"),
            "007_supabase_sync.sql":
                lambda: bool(con.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='supabase_leads'"
                    ).fetchone()),
            "006_research_budget.sql":
                lambda: bool(con.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='research_runs'"
                    ).fetchone()),
        }

        applied = {r[0] for r in con.execute(
            "SELECT name FROM schema_migrations")}
        for path in sorted((SRC / "migrations").glob("*.sql")):
            name = path.name
            if name in applied:
                ok("%s already applied" % name)
                continue
            probe = FINGERPRINTS.get(name)
            if probe is not None and probe():
                if not check:
                    con.execute("INSERT OR IGNORE INTO schema_migrations (name)"
                                " VALUES (?)", (name,))
                ok("%s already present; recorded in the ledger" % name)
                continue
            if check:
                warn("%s is PENDING" % name)
                continue
            body = "\n".join(l for l in path.read_text(encoding="utf-8")
                             .splitlines() if not l.strip().startswith("--"))
            con.executescript(body)
            con.execute("INSERT OR IGNORE INTO schema_migrations (name)"
                        " VALUES (?)", (name,))
            ok("%s applied" % name)

        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        (ok if integrity == "ok" else bad)("integrity_check: %s" % integrity)
    finally:
        con.close()


# --------------------------------------------------------------------------
# 4. souls
# --------------------------------------------------------------------------

def install_souls(check: bool) -> None:
    print("\n[4] SOUL files")
    souls = SRC / "souls"
    if not souls.is_dir():
        warn("no souls/ directory in the repository")
        return
    for path in sorted(souls.glob("*.md")):
        stem = path.stem.lower()
        if stem == "all_souls":
            continue
        home = HERMES_HOME if stem == "maya" else HERMES_HOME / "profiles" / stem
        if not home.is_dir():
            warn("profile home for %s not found (%s)" % (stem, home))
            continue
        dst = home / "SOUL.md"
        if dst.exists() and dst.read_bytes() == path.read_bytes():
            ok("%s SOUL up to date" % stem)
        elif check:
            warn("%s SOUL would be updated" % stem)
        else:
            # The operator may have edited a SOUL by hand; keep the old one.
            if dst.exists():
                shutil.copyfile(dst, dst.with_suffix(".md.bak.install"))
            shutil.copyfile(path, dst)
            ok("%s SOUL installed (previous kept as SOUL.md.bak.install)" % stem)


# --------------------------------------------------------------------------
# 5. profiles, multiplexing, MCP
# --------------------------------------------------------------------------

def _mcp_servers(path: pathlib.Path):
    """The MCP server names declared in one config.yaml."""
    if not path.exists():
        return []
    import re
    cfg = path.read_text(encoding="utf-8")
    block = re.search(r"^mcp_servers:.*?(?=^\S|\Z)", cfg, re.S | re.M)
    if not block:
        return []
    return [l.strip().rstrip(":") for l in block.group(0).splitlines()
            if re.match(r"^  [a-z_-]+:$", l)]


def verify_profiles() -> None:
    print("\n[5] profiles and gateway")
    missing = [p for p in PROFILES
               if not (HERMES_HOME / "profiles" / p).is_dir()]
    if missing:
        bad("missing profile(s): %s" % ", ".join(missing))
    else:
        ok("all six sub-profiles present (%s) plus root MAYA" % ", ".join(PROFILES))

    cfg = HERMES_HOME / "config.yaml"
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    if "multiplex_profiles: true" in text:
        ok("gateway.multiplex_profiles is on — one gateway ticks every profile")
    else:
        bad("gateway.multiplex_profiles is NOT on; sub-profile crons will not fire")

    # MCP servers are declared per profile, not globally. Substring-matching
    # the root config reported "registered" for servers that only exist on a
    # sub-profile — and printed the same sentence whether it passed or failed.
    expected = {
        "": ("mailhub",),              # root == MAYA: queues mail
        "nova": ("agency", "research"),   # research == Steel
        "aria": ("agency",),
        "sentinel": ("agency",),
        "leo": ("agency",),
    }
    for profile, want in expected.items():
        home = HERMES_HOME if not profile else HERMES_HOME / "profiles" / profile
        have = set(_mcp_servers(home / "config.yaml"))
        label = profile or "maya(root)"
        for server in want:
            if server in have:
                ok("%s has the %s MCP server" % (label, server))
            else:
                bad("%s is MISSING the %s MCP server" % (label, server))
    # The two that must stay empty: neither may reach MailHub directly.
    for profile in ("echo", "orbit"):
        have = set(_mcp_servers(
            HERMES_HOME / "profiles" / profile / "config.yaml"))
        if "mailhub" in have:
            bad("%s has the mailhub MCP server; it must not be able to send"
                % profile)
        else:
            ok("%s has no mailhub MCP server, as intended" % profile)


def verify_credentials() -> None:
    print("\n[6] per-profile credentials (names only, never values)")
    expect = {
        "nova": ("STEEL_API_KEY", "AGENCY_DB", "AGENCY_ROLE"),
        "aria": ("AGENCY_DB", "AGENCY_ROLE"),
        "sentinel": ("AGENCY_DB", "AGENCY_ROLE", "MAILHUB_API_TOKEN"),
        "leo": ("AGENCY_DB", "AGENCY_ROLE", "MAILHUB_API_TOKEN"),
        "orbit": ("MAILHUB_API_TOKEN", "MAILHUB_BASE_URL"),
        "echo": (),
    }
    for profile, keys in expect.items():
        env = HERMES_HOME / "profiles" / profile / ".env"
        have = set()
        if env.exists():
            have = {l.split("=", 1)[0].strip() for l in
                    env.read_text(encoding="utf-8").splitlines()
                    if "=" in l and not l.strip().startswith("#")}
        gap = [k for k in keys if k not in have]
        if gap:
            warn("%s is missing %s — set it in %s" % (profile, ", ".join(gap), env))
        else:
            ok("%s has the variables it needs" % profile)
    if "MAILHUB_API_TOKEN" in _names(HERMES_HOME / "profiles" / "echo" / ".env"):
        bad("ECHO holds a MailHub token; it must not be able to send")
    else:
        ok("ECHO holds no MailHub credential, as intended")


def _names(env: pathlib.Path):
    if not env.exists():
        return set()
    return {l.split("=", 1)[0].strip() for l in
            env.read_text(encoding="utf-8").splitlines()
            if "=" in l and not l.strip().startswith("#")}


# --------------------------------------------------------------------------
# 7. cron
# --------------------------------------------------------------------------

def jobs_for(home: pathlib.Path):
    path = home / "cron" / "jobs.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("jobs", [])
    except Exception:
        return []


def install_crons(check: bool) -> None:
    print("\n[7] scheduled jobs")
    for name, (home, schedule, script, deliver) in CRON_JOBS.items():
        existing = [j for j in jobs_for(home) if j.get("name") == name]
        if len(existing) > 1:
            bad("%d duplicate jobs named %s — remove all but one"
                % (len(existing), name))
            continue
        if existing:
            j = existing[0]
            ok("%s exists (%s, %s, deliver=%s)"
               % (name, j["id"], j.get("schedule_display") or schedule,
                  j.get("deliver")))
            if not j.get("no_agent"):
                warn("%s is not --no-agent; it should be deterministic" % name)
            continue
        if deliver == "discord":
            warn("%s has no channel id; set AGENCY_DISCORD_ALERTS_CHANNEL and"
                 " AGENCY_DISCORD_REPORT_CHANNEL or the job will post nowhere"
                 % name)
        if check:
            warn("%s is MISSING and would be created" % name)
            continue
        args = ["cron", "create", schedule, "--name", name,
                "--script", script, "--no-agent"]
        # Delivery targets are channel ids the operator owns, so an existing
        # job's target is never rewritten; only a brand new job gets one.
        if deliver != "local":
            args += ["--deliver", deliver]
        r = hermes(home, *args)
        if r.returncode == 0:
            ok("%s created" % name)
        else:
            bad("%s could not be created: %s"
                % (name, (r.stderr or r.stdout).strip()[:160]))


# --------------------------------------------------------------------------
# 8. health
# --------------------------------------------------------------------------

def health() -> None:
    print("\n[8] health")
    if not AGENCY_DB.exists():
        bad("no agency database at %s" % AGENCY_DB)
        return
    con = sqlite3.connect(AGENCY_DB, timeout=30)
    try:
        for table in ("leads", "campaigns", "state_transitions", "followups",
                      "human_escalations", "messages", "events"):
            try:
                n = con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                ok("%-20s %d row(s)" % (table, n))
            except sqlite3.Error as exc:
                bad("%s unreadable: %s" % (table, exc))
        legal = con.execute("SELECT COUNT(*) FROM state_transitions").fetchone()[0]
        (ok if legal else bad)("%d legal state transitions seeded" % legal)
    finally:
        con.close()

    sys.path.insert(0, str(AGENCY_DIR))
    for mod in ("pipeline", "followups", "review", "review_tick", "orbit"):
        try:
            __import__(mod)
            ok("%s imports" % mod)
        except Exception as exc:
            bad("%s does not import: %s" % (mod, exc))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="report what would change without changing it")
    args = ap.parse_args()

    print("Hermes agency installer  (HERMES_HOME=%s, %s)"
          % (HERMES_HOME, "CHECK ONLY" if args.check else "applying"))
    install_source(args.check)
    install_mailhub_mcp(args.check)
    apply_migrations(args.check)
    install_souls(args.check)
    verify_profiles()
    verify_credentials()
    install_crons(args.check)
    health()

    print("\n" + "=" * 72)
    print("ok: %d   warnings: %d   failures: %d" % (len(OK), len(WARN), len(BAD)))
    for m in WARN:
        print("  WARN  %s" % m)
    for m in BAD:
        print("  FAIL  %s" % m)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
