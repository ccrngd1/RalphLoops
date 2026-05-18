import json, pathlib

p = pathlib.Path(r"C:\Users\lawsnic\OneDrive - amazon.com\Documents\HCLS-ai-book\tasks.json")
tasks = json.loads(p.read_text(encoding="utf-8"))

reset_count = 0
for t in tasks:
    # Reset the 130 expert-review tasks that were pre-emptively stuck (retry_count=0)
    if t["status"] == "stuck" and "expert-review" in t["id"] and t.get("retry_count", 0) == 0:
        t["status"] = "pending"
        reset_count += 1

# Also mark ch04-r01-edit as stuck since it legitimately hit max retries
for t in tasks:
    if t["id"] == "ch04-r01-edit" and t["status"] == "failing":
        t["status"] = "stuck"
        print(f"Marked ch04-r01-edit as stuck")

p.write_text(json.dumps(tasks, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Reset {reset_count} expert-review tasks from stuck to pending")

# Verify new counts
from collections import Counter
c = Counter(t["status"] for t in tasks)
print(f"\nNew task status summary:")
for s, n in c.most_common():
    print(f"  {s}: {n}")
