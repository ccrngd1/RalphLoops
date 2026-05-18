import json, pathlib

p = pathlib.Path(r"C:\Users\lawsnic\OneDrive - amazon.com\Documents\HCLS-ai-book\tasks.json")
tasks = json.loads(p.read_text(encoding="utf-8"))
stuck = [t for t in tasks if t["status"] == "stuck"]

expert_reviews = [t for t in stuck if "expert-review" in t["id"]]
print(f"Stuck expert-review tasks: {len(expert_reviews)}")
print(f"Sample retry counts: {[t.get('retry_count', 0) for t in expert_reviews[:10]]}")
print()

others = [t for t in stuck if "expert-review" not in t["id"]]
print(f"Other stuck tasks ({len(others)}):")
for t in others:
    print(f"  {t['id']} retry_count={t.get('retry_count', 0)}")
