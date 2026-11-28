"""intake-capture alert helper (2026-11-28). Runs inside CT 218 as igcap, in ~/work/social-intake.
Reads origin/intake-queue only (never the working tree, never pulls). Prints one JSON line.
owed: incoming scaffolds 48h or older that r2 has not captured (no complete note, or a held note).
sage: incoming scaffolds 48h or older that r2 captured, still waiting for Sage to verify and merge.
Age: hours since the scaffold was first added on the branch (author time), else its queued_at date."""
import json, re, subprocess, sys, time

REF = "origin/intake-queue"


def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True).stdout


fetch_ok = True
if "--no-fetch" not in sys.argv:
    fetch_ok = subprocess.run(["git", "fetch", "-q", "origin", "intake-queue"], capture_output=True).returncode == 0
now = time.time()
owed, sage = [], []
for path in git("ls-tree", "--name-only", REF, "queue/incoming/").split():
    if not path.endswith(".md"):
        continue
    name = path.rsplit("/", 1)[1]
    adds = git("log", REF, "--diff-filter=A", "--format=%at", "--", path).split()
    if adds:
        t0 = min(int(x) for x in adds)
    else:
        m = re.search(r"^queued_at: .?(\d{4}-\d{2}-\d{2})", git("show", f"{REF}:{path}"), re.M)
        if not m:
            continue
        t0 = time.mktime(time.strptime(m.group(1), "%Y-%m-%d"))
    age = (now - t0) / 3600
    if age < 48:
        continue
    m = re.search(r"^capture_status: *['\"]?(\w+)", git("show", f"{REF}:queue/complete/{name}"), re.M)
    status = m.group(1) if m else None
    (sage if status == "complete" else owed).append((round(age), name))
print(json.dumps({"fetch_ok": fetch_ok, "owed": sorted(owed, reverse=True), "sage": sorted(sage, reverse=True)}))
