import json, pathlib
from collections import Counter

p = pathlib.Path(r"C:\Users\lawsnic\OneDrive - amazon.com\Documents\HCLS-ai-book\tasks.json")
tasks = json.loads(p.read_text(encoding="utf-8"))
by_id = {t["id"]: t for t in tasks}
stuck_ids = {t["id"] for t in tasks if t["status"] == "stuck"}
pending = [t for t in tasks if t["status"] == "pending"]

blocked_by_stuck = []
ready_to_run = []
blocked_by_other_pending = []

for t in pending:
    deps = t.get("depends_on", []) or []
    if not deps:
        ready_to_run.append(t)
    else:
        dep_statuses = set()
        for d in deps:
            dt = by_id.get(d)
            if dt:
                dep_statuses.add(dt["status"])
        if "stuck" in dep_statuses:
            blocked_by_stuck.append(t)
        elif all(by_id.get(d, {}).get("status") == "passing" for d in deps):
            ready_to_run.append(t)
        else:
            blocked_by_other_pending.append(t)

print(f"Pending tasks: {len(pending)}")
print(f"  Ready to run (deps satisfied): {len(ready_to_run)}")
print(f"  Blocked by stuck tasks: {len(blocked_by_stuck)}")
print(f"  Blocked by other pending: {len(blocked_by_other_pending)}")
print()

# Check task types in ready pool
types = Counter()
for t in ready_to_run:
    tag = t["id"].rsplit("-", 1)[-1] if "-" in t["id"] else "other"
    types[tag] += 1
print("Ready tasks by type:")
for typ, n in types.most_common():
    print(f"  {typ}: {n}")
print()

# Show first 15 ready tasks
print("Ready to run (first 15):")
for t in ready_to_run[:15]:
    print(f"  {t['id']} -> {t.get('target_persona', 'none')}")
print()

# What chapters are represented in ready pool?
chapters = Counter()
for t in ready_to_run:
    ch = t["id"].split("-")[0]
    chapters[ch] += 1
print("Ready tasks by chapter:")
for ch, n in chapters.most_common():
    print(f"  {ch}: {n}")
print()

# What about the stuck tasks - are they ALL expert-review?
stuck = [t for t in tasks if t["status"] == "stuck"]
stuck_types = Counter(t["id"].rsplit("-", 1)[-1] for t in stuck)
print("Stuck tasks by type:")
for typ, n in stuck_types.most_common():
    print(f"  {typ}: {n}")
print()

# How many edit tasks are blocked specifically by expert-review?
edit_blocked = [t for t in blocked_by_stuck if t["id"].endswith("-edit")]
print(f"Edit tasks blocked by stuck expert-reviews: {len(edit_blocked)}")

# Could we unblock edits by removing expert-review from their deps?
for t in edit_blocked[:5]:
    deps = t.get("depends_on", [])
    stuck_deps = [d for d in deps if by_id.get(d, {}).get("status") == "stuck"]
    other_deps = [d for d in deps if by_id.get(d, {}).get("status") != "stuck"]
    other_statuses = [(d, by_id.get(d, {}).get("status")) for d in other_deps]
    print(f"  {t['id']}: stuck_deps={stuck_deps}, other_deps={other_statuses}")
