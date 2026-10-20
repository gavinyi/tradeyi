"""Post the pending DSH task-board review report to the fixed Feishu chat.

This helper intentionally avoids ``subprocess`` pipes. The DSH Windows
sandbox may deny child-process handle creation while allowing direct shell
commands, so the Feishu steps are generated here and executed by the caller.

Usage:
    .venv\\Scripts\\python.exe scripts\\_post_task_board_review_to_feishu.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
os.chdir(REPO_ROOT)

from futu_quant.store import TradingStore  # noqa: E402
from futu_quant.scheduled_tasks import pending_im_delivery  # noqa: E402

FEISHU_DELIVERY_CHAT_ID = "oc_47d5da371ac34e16cdc7b20bcdd7f27a"
LARK_CLI = r"C:\Users\Administrator\AppData\Roaming\npm\lark-cli.ps1"


def _cmd(args: list[str]) -> str:
    quoted = []
    for arg in args:
        if all(ch.isalnum() or ch in "._/-" for ch in arg):
            quoted.append(arg)
        else:
            escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
            quoted.append(f'"{escaped}"')
    return " ".join(quoted)


def main() -> int:
    store = TradingStore()
    record = pending_im_delivery(store)
    if record is None:
        print(json.dumps({"ok": True, "pending": False, "note": "没有待投递的复盘记录。"}, ensure_ascii=False))
        return 0

    session_date = record.get("session_date", "unknown")
    markdown_path = Path(record.get("markdown_path", ""))
    html_path = Path(record.get("html_path", ""))
    if not markdown_path.exists():
        print(json.dumps({"ok": False, "pending": True, "error": f"markdown 文件不存在：{markdown_path}"}, ensure_ascii=False))
        return 1
    if not html_path.exists():
        print(json.dumps({"ok": False, "pending": True, "error": f"html 文件不存在：{html_path}"}, ensure_ascii=False))
        return 1

    # Import the markdown report into the review folder as a Feishu online doc.
    import_md = [
        LARK_CLI, "drive", "+import",
        "--file", markdown_path.name,
        "--type", "docx",
        "--folder-token", "IKiVf2vtTlvnswdhke5ctLcd",  # placeholder will be replaced by poll delivery token.
        "--name", f"策略复盘报告 {session_date}",
    ]
    # The record stores the configured folder token; do not hard-code a bad fallback.
    import_md = [
        LARK_CLI, "drive", "+import",
        "--file", markdown_path.name,
        "--type", "docx",
        "--folder-token", "IKiVf2vtTlvnswdhke5ctLhntd",
        "--name", f"策略复盘报告 {session_date}",
    ]
    import_cmd = _cmd(import_md)

    upload_html = [
        LARK_CLI, "im", "+messages-send",
        "--chat-id", FEISHU_DELIVERY_CHAT_ID,
        "--as", "bot",
        "--file", html_path.name,
    ]
    upload_cmd = _cmd(upload_html)

    conclusion = record.get("conclusion", "")
    message_lines = [
        f"任务看板 · 策略复盘报告 {session_date}",
        f"完整 HTML 报告已发送/上传：{html_path.name}",
        "结论摘要：",
        *conclusion.splitlines(),
    ]
    text = "\n".join(line for line in message_lines if line.strip())
    send_text = [
        LARK_CLI, "im", "+messages-send",
        "--chat-id", FEISHU_DELIVERY_CHAT_ID,
        "--as", "bot",
        "--text", text,
    ]
    text_cmd = _cmd(send_text)

    print(json.dumps({
        "ok": True,
        "pending": True,
        "session_date": session_date,
        "markdown_path": str(markdown_path),
        "html_path": str(html_path),
        "chat_id": FEISHU_DELIVERY_CHAT_ID,
        "import_md_cmd": import_cmd,
        "text_cmd": text_cmd,
        "upload_html_cmd": upload_cmd,
        "note": "执行 text_cmd 和 upload_html_cmd；import_md_cmd 可选，用于生成飞书在线文档链接。",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
