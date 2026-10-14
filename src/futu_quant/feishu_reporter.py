"""
飞书战报与策略复盘应用内直连推送器 (Feishu Reporter)
"""
from __future__ import annotations

from datetime import date
import json
import logging
import os
from pathlib import Path
import subprocess
from urllib.request import Request, urlopen

from futu_quant.scheduled_tasks import (
    mark_im_delivery_done,
    pending_im_delivery,
)
from futu_quant.store import TradingStore

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
FEISHU_DELIVERY_CHAT_ID = "oc_47d5da371ac34e16cdc7b20bcdd7f27a"
FEISHU_REVIEW_FOLDER_TOKEN = "IKiVf2vtTlvnswdhke5ctLhtntd"
LARK_RUN_JS = r"C:\Users\Administrator\AppData\Roaming\npm\node_modules\@larksuite\cli\scripts\run.js"


def _lark_node_exe() -> str | None:
    for cand in [r"C:\Program Files\nodejs\node.exe", r"C:\Users\Administrator\AppData\Roaming\npm\node.exe"]:
        if os.path.exists(cand):
            return cand
    import shutil
    return shutil.which("node")


def _run_lark_json(cmd: list[str]) -> dict:
    """在 Windows 沙箱下使用文件重定向执行 lark-cli 命令，避免 CreatePipe 限制。"""
    argv = list(cmd)
    if argv and argv[0] == "lark-cli" and os.path.exists(LARK_RUN_JS):
        node = _lark_node_exe()
        if node:
            argv = [node, LARK_RUN_JS, *argv[1:]]

    out_file = REPO_ROOT / "data" / "_lark_out.json"
    err_file = REPO_ROOT / "data" / "_lark_err.log"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        with open(out_file, "wb") as f_out, open(err_file, "wb") as f_err:
            proc = subprocess.run(argv, stdout=f_out, stderr=f_err, cwd=str(REPO_ROOT), env=env, check=False)
        output = out_file.read_text(encoding="utf-8", errors="ignore").lstrip("\ufeff").strip()
        stderr_text = err_file.read_text(encoding="utf-8", errors="ignore")
        if not output:
            return {"ok": False, "error": f"lark-cli exited {proc.returncode} with no output; stderr={stderr_text[:300]}"}
        inner_start = output.find("{")
        inner_end = output.rfind("}")
        if inner_start != -1 and inner_end > inner_start:
            return json.loads(output[inner_start:inner_end + 1])
        return {"ok": False, "error": output}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        out_file.unlink(missing_ok=True)
        err_file.unlink(missing_ok=True)


class FeishuReporter:
    """飞书战报与复盘自动投递器"""

    def __init__(self, store: TradingStore | None = None):
        self.store = store or TradingStore()
        self.webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
        self.chat_id = os.getenv("FEISHU_CHAT_ID", FEISHU_DELIVERY_CHAT_ID).strip()
        self.folder_token = os.getenv("FEISHU_FOLDER_TOKEN", FEISHU_REVIEW_FOLDER_TOKEN).strip()

    def send_via_webhook(self, title: str, content_lines: list[str]) -> bool:
        """通过标准飞书群自定义机器人 Webhook 发送富文本消息。"""
        if not self.webhook_url:
            return False
        try:
            elements = [{"tag": "text", "text": f"{line}\n"} for line in content_lines]
            payload = json.dumps({
                "msg_type": "post",
                "content": {
                    "post": {
                        "zh_cn": {
                            "title": title,
                            "content": [elements],
                        }
                    }
                }
            }).encode("utf-8")
            req = Request(self.webhook_url, data=payload, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=8) as resp:  # noqa: S310
                res = json.loads(resp.read().decode("utf-8"))
                return res.get("code") == 0 or res.get("StatusCode") == 0
        except Exception as exc:
            logger.warning("通过飞书 Webhook 发送战报失败: %s", exc)
            return False

    def send_via_lark_cli(self, relative_md: str, title: str, session_date: str, conclusion: str, advice_text: str = "") -> dict:
        """通过 lark-cli 将 Markdown 导入为在线文档并发送至 147 机器人会话。"""
        # 1. 导入在线文档
        import_cmd = [
            "lark-cli", "drive", "+import",
            "--file", relative_md,
            "--type", "docx",
            "--folder-token", self.folder_token,
            "--name", title,
            "--as", "user",
        ]
        import_res = _run_lark_json(import_cmd)
        doc_url = import_res.get("data", {}).get("url", "")
        if not doc_url and self.folder_token:
            # 降级尝试不带 folder-token
            fallback_res = _run_lark_json([
                "lark-cli", "drive", "+import",
                "--file", relative_md,
                "--type", "docx",
                "--name", title,
                "--as", "user",
            ])
            doc_url = fallback_res.get("data", {}).get("url", "")

        # 2. 发送消息到 147 会话
        msg_body = (
            f"量化系统 · 每日策略复盘报告 {session_date}\n"
            f"文档链接：{doc_url or '可在本地 data/reports/ 查阅'}\n"
            f"结论摘要：\n"
            f"- {conclusion}"
            + (f"\n{advice_text}" if advice_text else "")
        )
        send_cmd = [
            "lark-cli", "im", "+messages-send",
            "--chat-id", self.chat_id,
            "--as", "bot",
            "--markdown", msg_body,
        ]
        send_res = _run_lark_json(send_cmd)
        return {"ok": send_res.get("ok", False), "doc_url": doc_url, "send_res": send_res}

    def dispatch_pending_review(self) -> bool:
        """主入口：读取待投递复盘记录，自动发送至飞书并标记已投递。"""
        record = pending_im_delivery(self.store)
        if not record:
            return True

        session_date = str(record.get("session_date", "未知日期"))
        conclusion = str(record.get("conclusion", ""))
        markdown_path = str(record.get("markdown_path", ""))
        title = f"策略复盘与战报 {session_date}"

        # 提取当日建议
        advice_text = ""
        try:
            from futu_quant.strategy_review import build_strategy_review, _review_issue_advice
            from futu_quant.trade_diagnostics import diagnostic_window
            sd = date.fromisoformat(session_date)
            rev = build_strategy_review(self.store, diagnostic_window(sd))
            issues, advice = _review_issue_advice(rev)
            if issues or advice:
                lines = ["\n当日问题与建议："]
                if issues:
                    lines.append("问题：" + "；".join(issues[:3]))
                if advice:
                    lines.append("建议：" + "；".join(advice[:3]))
                advice_text = "\n".join(lines)
        except Exception:
            pass

        success = False

        # 优先方式 1: 飞书标准群 Webhook (若配置)
        if self.webhook_url:
            lines = [
                f"美东交易日：{session_date}",
                f"核心结论：{conclusion}",
            ]
            if advice_text:
                lines.append(advice_text)
            success = self.send_via_webhook(title, lines)

        # 降级/默认方式 2: lark-cli 机器人直发 147 会话
        if not success and markdown_path and Path(markdown_path).exists():
            relative_md = Path(markdown_path).relative_to(REPO_ROOT).as_posix()
            res = self.send_via_lark_cli(relative_md, title, session_date, conclusion, advice_text)
            success = res.get("ok", False)

        if success:
            mark_im_delivery_done(self.store, date.fromisoformat(session_date))
            self.store.record_universe_lifecycle_event(
                event_type="FEISHU_REVIEW_DELIVERED",
                symbol="ALL",
                details={"session_date": session_date, "success": True},
            )
            logger.info("每日复盘战报已成功送达飞书: %s", session_date)
        return success
