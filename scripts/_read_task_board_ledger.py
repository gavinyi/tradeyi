"""Read task-board ledger as UTF-8 JSON so scheduled poll runs get a valid record."""
import json
from pathlib import Path

ledger = json.loads(Path(r"C:\Users\Administrator\.dsh\task-board\ledger-v2.json").read_text(encoding="utf-8"))
task = next((row for row in ledger["tasks"] if "复盘" in row.get("title", "") and row.get("schedule", {}).get("enabled")), None)
if task is None:
    print(json.dumps({"ok": False, "error": "target task not found"}, ensure_ascii=False))
    raise SystemExit(1)
print(json.dumps({
    "ok": True,
    "title": task.get("title"),
    "pinSessionId": task.get("pinSessionId"),
    "lastExecution": task.get("executions", [None])[-1],
    "prompt": task.get("prompt"),
}, ensure_ascii=False, indent=2))
