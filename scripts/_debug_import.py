from __future__ import annotations
import json, subprocess, os

REPO = r"E:\Thisyi\trade"
NODE = r"C:\Program Files\nodejs\node.exe"
RUNJS = r"C:\Users\Administrator\AppData\Roaming\npm\node_modules\@larksuite\cli\scripts\run.js"

cmd = [NODE, RUNJS, "drive", "+import",
       "--file", "data/reports/strategy_review_2026-10-08.md",
       "--type", "docx",
       "--folder-token", "IKiVf2vtTlvnswdhke5ctLhtntd",
       "--name", "策略复盘报告 2026-10-08"]
p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=REPO)
out = (p.stdout or p.stderr or "").strip()
print("returncode:", p.returncode)
print("raw stdout[:300]:", out[:300])
try:
    j = json.loads(out)
    print("ok:", j.get("ok"))
    data = j.get("data") or {}
    print("data type:", type(data).__name__, "is dict:", isinstance(data, dict))
    print("data keys:", list(data.keys()) if isinstance(data, dict) else "N/A")
    print("data.url:", repr(data.get("url")) if isinstance(data, dict) else "N/A")
    # 模拟脚本取值
    url = j.get("data", {}).get("url", "")
    print("script-style url:", repr(url))
except Exception as e:
    print("parse err:", e)
