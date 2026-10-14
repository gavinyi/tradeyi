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
    """飞书战报与复盘自动投递器（支持群机器人 Webhook、群聊会话与个人会话多目标广播）"""

    def __init__(self, store: TradingStore | None = None):
        self.store = store or TradingStore()
        # 优先从数据库 runtime_config 读取，支持 UI 动态配置，其次读环境变量
        self.webhook_url = (
            self.store.get_setting("FEISHU_WEBHOOK_URL", "") or os.getenv("FEISHU_WEBHOOK_URL", "")
        ).strip()
        self.webhook_secret = (
            self.store.get_setting("FEISHU_WEBHOOK_SECRET", "") or os.getenv("FEISHU_WEBHOOK_SECRET", "")
        ).strip()
        self.group_chat_id = (
            self.store.get_setting("FEISHU_GROUP_CHAT_ID", "") or os.getenv("FEISHU_GROUP_CHAT_ID", "")
        ).strip()
        self.chat_id = (
            self.store.get_setting("FEISHU_CHAT_ID", "") or os.getenv("FEISHU_CHAT_ID", FEISHU_DELIVERY_CHAT_ID)
        ).strip()
        self.folder_token = os.getenv("FEISHU_FOLDER_TOKEN", FEISHU_REVIEW_FOLDER_TOKEN).strip()

    @staticmethod
    def _generate_signature(secret: str, timestamp: int) -> str:
        """计算飞书群机器人安全签名 (HMAC-SHA256)。"""
        import base64
        import hashlib
        import hmac
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        return base64.b64encode(hmac_code).decode("utf-8")

    def send_via_webhook(
        self,
        title: str,
        content_lines: list[str],
        doc_url: str = "",
        webhook_url: str | None = None,
    ) -> bool:
        """通过标准飞书群自定义机器人 Webhook 发送高颜值卡片消息 (多账号全员可见)。"""
        target_url = webhook_url or self.webhook_url
        if not target_url:
            return False
        try:
            import time
            ts = int(time.time())
            elements = []
            for line in content_lines:
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": line},
                })

            if doc_url:
                elements.append({
                    "tag": "action",
                    "actions": [{
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "📖 在线阅读完整策略复盘文档"},
                        "type": "primary",
                        "url": doc_url,
                    }]
                })

            card_payload = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"tag": "plain_text", "content": f"📊 {title}"},
                        "template": "blue",
                    },
                    "elements": elements,
                }
            }
            if self.webhook_secret:
                card_payload["timestamp"] = str(ts)
                card_payload["sign"] = self._generate_signature(self.webhook_secret, ts)

            payload_bytes = json.dumps(card_payload, ensure_ascii=False).encode("utf-8")
            req = Request(target_url, data=payload_bytes, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=8) as resp:  # noqa: S310
                res = json.loads(resp.read().decode("utf-8"))
                return res.get("code") == 0 or res.get("StatusCode") == 0
        except Exception as exc:
            logger.warning("通过飞书群 Webhook 发送战报失败: %s", exc)
            return False

    def send_alert_card(
        self,
        severity: str,
        event_type: str,
        message: str,
        is_recovered: bool = False,
    ) -> bool:
        """向飞书群聊或 Webhook 发送紧急告警或故障恢复卡片。"""
        from datetime import datetime
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        sev_upper = severity.upper()
        if is_recovered:
            header_color = "green"
            icon = "🟢"
            status_title = f"{icon} 【故障恢复】{event_type} 已恢复正常"
            level_text = "🟢 **级别**: **NORMAL (已恢复正常)**"
        elif sev_upper == "CRITICAL":
            header_color = "red"
            icon = "🚨"
            status_title = f"{icon} 【紧急告警】{event_type}"
            level_text = "🔴 **级别**: **CRITICAL (致命严重)**"
        elif sev_upper == "ERROR":
            header_color = "orange"
            icon = "⚠️"
            status_title = f"{icon} 【系统错误】{event_type}"
            level_text = "🟠 **级别**: **ERROR (系统错误)**"
        else:
            header_color = "blue"
            icon = "ℹ️"
            status_title = f"{icon} 【系统通知】{event_type}"
            level_text = "🔵 **级别**: **INFO (系统提醒)**"

        lines = [
            level_text,
            f"🕒 **时间**: {now_str}",
            f"📌 **事件类型**: `{event_type}`",
            f"📝 **详细情况**:\n{message}",
        ]
        if not is_recovered and sev_upper in {"CRITICAL", "ERROR"}:
            lines.append("🛡️ **系统保护动作**: 已自动暂停新风险开仓，持续保持在岗对账与退出监控。")

        # 严格单目标发送逻辑：配了群就只发群，未配群才发个人，绝不双发重复打扰
        # 1. 优先渠道 A: 飞书群聊会话 (若配置了 group_chat_id)
        if self.group_chat_id:
            msg_md = f"### {status_title}\n──────────────────────\n" + "\n\n".join(lines)
            cmd = [
                "lark-cli", "im", "+messages-send",
                "--chat-id", self.group_chat_id,
                "--as", "bot",
                "--markdown", msg_md,
            ]
            res = _run_lark_json(cmd)
            return bool(res.get("ok", False))

        # 2. 渠道 B: 飞书群自定义机器人 Webhook (若配置)
        if self.webhook_url:
            try:
                import time
                ts = int(time.time())
                elements = [{"tag": "div", "text": {"tag": "lark_md", "content": line}} for line in lines]
                card_payload = {
                    "msg_type": "interactive",
                    "card": {
                        "config": {"wide_screen_mode": True},
                        "header": {
                            "title": {"tag": "plain_text", "content": status_title},
                            "template": header_color,
                        },
                        "elements": elements,
                    },
                }
                if self.webhook_secret:
                    card_payload["timestamp"] = str(ts)
                    card_payload["sign"] = self._generate_signature(self.webhook_secret, ts)

                payload_bytes = json.dumps(card_payload, ensure_ascii=False).encode("utf-8")
                req = Request(self.webhook_url, data=payload_bytes, headers={"Content-Type": "application/json"})
                with urlopen(req, timeout=8) as resp:  # noqa: S310
                    res = json.loads(resp.read().decode("utf-8"))
                    return bool(res.get("code") == 0 or res.get("StatusCode") == 0)
            except Exception as exc:
                logger.warning("通过飞书群 Webhook 发送告警失败: %s", exc)
                return False

        # 3. 渠道 C: 只有在群完全未配置时，才发个人 147 会话
        if self.chat_id:
            msg_md = f"### {status_title}\n──────────────────────\n" + "\n\n".join(lines)
            cmd = [
                "lark-cli", "im", "+messages-send",
                "--chat-id", self.chat_id,
                "--as", "bot",
                "--markdown", msg_md,
            ]
            res = _run_lark_json(cmd)
            return bool(res.get("ok", False))

        return False

    def send_test_message(self) -> dict:
        """发送一条多通道测试消息，智能适配群 Chat ID、群 Webhook 或个人会话。"""
        from datetime import datetime
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. 如果配置了群 Chat ID (如 oc_8808...)，优先走群直发
        if self.group_chat_id:
            msg_body = (
                f"🔔 量化系统 · 飞书群聊推送联调测试\n"
                f"时间：{now_str}\n"
                f"状态：已成功连通飞书群聊！\n"
                f"目标群：{self.group_chat_id}\n"
                f"说明：本群内所有成员现在均可实时接收每日策略复盘与关键战报卡片。"
            )
            cmd = [
                "lark-cli", "im", "+messages-send",
                "--chat-id", self.group_chat_id,
                "--as", "bot",
                "--markdown", msg_body,
            ]
            res = _run_lark_json(cmd)
            if res.get("ok"):
                return {"ok": True, "channel": "群聊会话 (Group Chat)", "target": self.group_chat_id}

        # 2. 如果配置了 Webhook，走 Webhook 卡片
        if self.webhook_url:
            lines = [
                f"**时间**: {now_str}",
                "**状态**: 飞书群多账号推送通道联调成功！",
                "**说明**: 此卡片由量化系统应用层直发，每日清晨 05:15 将自动推送完整复盘与收益卡片。",
            ]
            ok = self.send_via_webhook("飞书群机器人战报联调测试", lines)
            if ok:
                return {"ok": True, "channel": "群机器人 Webhook", "target": self.webhook_url}

        # 3. 个人会话兜底
        msg_body = f"🔔 量化系统 · 飞书个人推送联调测试\n时间：{now_str}\n状态：已连通个人会话！"
        cmd = ["lark-cli", "im", "+messages-send", "--chat-id", self.chat_id, "--as", "bot", "--markdown", msg_body]
        res = _run_lark_json(cmd)
        if res.get("ok"):
            return {"ok": True, "channel": "个人会话 (Personal Chat)", "target": self.chat_id}

        return {"ok": False, "error": "未配置有效的飞书群 Chat ID 或 Webhook 地址"}

    def send_via_lark_cli(self, relative_md: str, title: str, session_date: str, conclusion: str, advice_text: str = "") -> dict:
        """通过 lark-cli 将 Markdown 导入为在线文档并发送至 147 机器人会话。"""
        # 1. 导入在线文档
        import_cmd = [
            "lark-cli", "drive", "+import",
            "--file", relative_md,
            "--type", "docx",
            "--name", title,
            "--as", "bot",
        ]
        import_res = _run_lark_json(import_cmd)
        doc_url = import_res.get("data", {}).get("url", "")

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
        doc_url = ""

        # 1. 优先尝试将报告导入为飞书在线文档，获取在线阅读链接
        if markdown_path and Path(markdown_path).exists():
            try:
                md_resolved = Path(markdown_path).resolve()
                try:
                    relative_md = md_resolved.relative_to(REPO_ROOT.resolve()).as_posix()
                except ValueError:
                    relative_md = str(md_resolved)
                import_res = _run_lark_json([
                    "lark-cli", "drive", "+import",
                    "--file", relative_md,
                    "--type", "docx",
                    "--name", title,
                    "--as", "bot",
                ])
                doc_url = import_res.get("data", {}).get("url", "")
            except Exception as exc:
                logger.warning("导入飞书文档遇到异常: %s", exc)

        # 严格单目标发送逻辑：配了群就只发群，绝不给个人会话双发重复消息
        # 2. 渠道 A: 飞书群聊会话直发 (优先，若配置了 group_chat_id)
        if self.group_chat_id:
            msg_body = (
                f"📊 **量化策略每日复盘战报 ({session_date})**\n"
                f"──────────────────────\n"
                f"🔗 **在线文档**: {doc_url or '可在本地 data/reports/ 查阅'}\n"
                f"🎯 **结论摘要**:\n{conclusion}"
                + (f"\n\n💡 **当日建议**:\n{advice_text}" if advice_text else "")
            )
            group_res = _run_lark_json([
                "lark-cli", "im", "+messages-send",
                "--chat-id", self.group_chat_id,
                "--as", "bot",
                "--markdown", msg_body,
            ])
            success = bool(group_res.get("ok", False))

        # 3. 渠道 B: 飞书群自定义机器人 Webhook (若配置)
        elif self.webhook_url:
            lines = [
                f"📅 **美东交易日**: {session_date}",
                f"📝 **结论摘要**:\n{conclusion}",
            ]
            if advice_text:
                lines.append(f"💡 **当日建议**:\n{advice_text}")
            if doc_url:
                lines.append(f"🔗 **在线文档**: {doc_url}")
            success = bool(self.send_via_webhook(title, lines, doc_url=doc_url))

        # 4. 渠道 C: 只有当群均未配置时，才发个人 147 会话兜底
        elif self.chat_id:
            msg_body = (
                f"📊 **量化策略每日复盘战报 ({session_date})**\n"
                f"──────────────────────\n"
                f"🔗 **在线文档**: {doc_url or '可在本地 data/reports/ 查阅'}\n"
                f"🎯 **结论摘要**:\n{conclusion}"
                + (f"\n\n💡 **当日建议**:\n{advice_text}" if advice_text else "")
            )
            cli_res = _run_lark_json([
                "lark-cli", "im", "+messages-send",
                "--chat-id", self.chat_id,
                "--as", "bot",
                "--markdown", msg_body,
            ])
            success = bool(cli_res.get("ok", False))

        if success:
            mark_im_delivery_done(self.store, date.fromisoformat(session_date))
            self.store.record_universe_lifecycle_event(
                event_type="FEISHU_REVIEW_DELIVERED",
                symbol="ALL",
                details={"session_date": session_date, "success": True, "doc_url": doc_url},
            )
            logger.info("每日复盘战报已成功送达飞书: %s", session_date)
        return success
