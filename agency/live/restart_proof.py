import datetime, json, os, subprocess, time

print("=== when did the gateway last start? ===")
pid = subprocess.run(["pgrep", "-f", "hermes gateway run"],
                     capture_output=True, text=True).stdout.split()[0]
started = subprocess.run(["ps", "-o", "lstart=", "-p", pid],
                         capture_output=True, text=True).stdout.strip()
print("  gateway pid %s started: %s" % (pid, started))

log = [float(x) for x in open("/opt/data/gateway-starts.log") if x.strip()]
print("  gateway-starts.log entries: %d" % len(log))
for t in log[-4:]:
    print("    %s" % datetime.datetime.utcfromtimestamp(t).strftime(
        "%Y-%m-%d %H:%M:%SZ"))

print("\n=== did maya-orchestrator survive it? ===")
d = json.load(open("/opt/data/cron/jobs.json"))
j = [x for x in d["jobs"] if x["name"] == "maya-orchestrator"][0]
print("  created_at : %s" % j["created_at"])
print("  runs       : %s" % j["repeat"]["completed"])
print("  last_run_at: %s" % j["last_run_at"])
print("  enabled=%s state=%s failure_streak=%s"
      % (j["enabled"], j["state"], j["failure_streak"]))
print("  duplicates named maya-orchestrator: %d"
      % sum(1 for x in d["jobs"] if x["name"] == "maya-orchestrator"))

print("\n=== cron runs recorded on each side of the restart ===")
import glob
runs = sorted(os.path.basename(p).replace(".md", "")
              for p in glob.glob("/opt/data/cron/output/%s/*.md" % j["id"]))
print("  first: %s   last: %s   total: %d" % (runs[0], runs[-1], len(runs)))
print("  runs:", ", ".join(r[11:] for r in runs))
