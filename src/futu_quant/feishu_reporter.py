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
from futu_quant.tenant import DEFAULT_TENANTS

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
FEISHU_GROUP_CHAT_ID = "oc_880837ba7546cdf0b75834ef88eff5b4"  # Tradeyi 群聊
FEISHU_DELIVERY_CHAT_ID = "oc_880837ba7546cdf0b75834ef88eff5b4"  # 默认统一发至该群，绝不外溢
FEISHU_REVIEW_FOLDER_TOKEN = "IKiVf2vtTlvnswdhke5ctLhtntd"
LARK_RUN_JS = r"C:\Users\Administrator\AppData\Roaming\npm\node_modules\@larksuite\cli\scripts\run.js"

EVENT_TYPE_NAMES: dict[str, str] = {
    # 核心风控与安全门禁
    "DAILY_LOSS_KILL_SWITCH": "日内亏损熔断急停",
    "KILL_SWITCH_ENGAGED": "紧急急停开关触发",
    "REAL_TRADE_REJECTED": "实盘下单门禁拦截",
    "ORDER_ENV_MISMATCH": "下单环境不匹配",
    "LEDGER_MISMATCH": "三账持仓对账异常",
    "RISK_REJECTED": "风控限额拦截",
    "HARD_STOP_LIMIT_INVALID": "硬止损限额配置异常",
    "REAL_STOCK_SELL_UNAUTHORIZED": "未经授权的实盘股票卖出",
    "REAL_PROTECTED_STOCK_SELL_FORBIDDEN": "底仓保护股票禁止卖出",
    "INVALID_EXECUTION_MODE_OVERRIDE": "非法执行模式覆盖",

    # 进程与高可用 (P1-13)
    "PROCESS_TERMINATED": "服务进程异常退出",
    "PROCESS_HEARTBEAT_TIMEOUT": "服务心跳超时（疑似僵死）",

    # 对账与券商连接
    "RECONCILIATION_MISMATCH": "券商与本地流水对账不一致",
    "OPEND_DISCONNECTED": "富途 OpenD 网关断开连接",
    "OPEND_UNAVAILABLE": "富途 OpenD 网关暂不可用",
    "OPEND_API_TIMED_OUT": "富途 OpenD 接口响应超时",

    # 策略与订单执行
    "STRATEGY_ERROR": "策略执行异常",
    "STRATEGY_ERROR_RECOVERED": "策略执行恢复正常",
    "REPRICE_FAILED": "订单智能追价失败",
    "CANCEL_FAILED": "超时撤单失败",
    "OPTION_LIFECYCLE_RISK": "期权生命周期风险预警",
    "PORTFOLIO_STRESS_ALERT": "投资组合压力预警",
    "STRESS_STATE_TENANT_FALLBACK": "组合压力状态租户回落",
    "EQUITY_SNAPSHOT_FAILED": "资产净值快照记录失败",
    "DAILY_LOSS_LIMIT_RESOLVE_FALLBACK": "日亏线解析失败（已退回环境变量值）",

    # 机器学习 (P2)
    "ML_MODEL_STALE": "ML 模型需重训（模型过旧）",
    "ML_SCORING_DEGRADED": "ML 评分降级（回退内置规则）",
    "ML_RETRAIN_FAILED": "ML 模型重训执行失败",

    # 告警与通道自身
    "ALERT_DELIVERY_PROBE": "告警通道心跳探针",
    "ALERT_CHANNEL_NOT_CONFIGURED": "告警通道未配置",
    "ALERT_DELIVERY_FAILED": "告警消息投递失败",
}


def human_event_type(event_type: str) -> str:
    """将英文事件代码转为通俗易懂的中文名称；未登记的 RECONCILIATION_* 前缀按对账异常处理。"""
    name = EVENT_TYPE_NAMES.get(event_type)
    if name:
        return name
    if event_type.startswith("RECONCILIATION_"):
        suffix = event_type.replace("RECONCILIATION_", "").lower()
        return f"对账差异（{suffix}）"
    return event_type


def _lark_node_exe() -> str | None:
    for cand in [r"C:\Program Files\nodejs\node.exe", r"C:\Users\Administrator\AppData\Roaming\npm\node.exe"]:
        if os.path.exists(cand):
            return cand
    import shutil
    return shutil.which("node")


def _run_lark_json(cmd: list[str]) -> dict:
    """在 Windows 沙箱下使用文件重定向执行 lark-cli 命令，避免 CreatePipe 限制。"""
    if "PYTEST_CURRENT_TEST" in os.environ and not os.getenv("FEISHU_ALLOW_TEST_NETWORK"):
        # 单元测试环境下严禁穿透发送飞书外部网络请求，杜绝测试消息污染生产群
        return {"ok": True, "message": "pytest_mocked"}

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
        tenant_id: str | None = None,
    ) -> bool:
        """向飞书群聊或 Webhook 发送人话化结构告警或故障恢复卡片。

        `tenant_id` 传入时标题带【实盘】/【模拟盘】租户标签（用户 2026-11-06 拍板）。
        """
        from .feishu_humanize import humanize_alert

        status_title, lines = humanize_alert(
            event_type, severity, message, is_recovered=is_recovered,
            tenant_id=tenant_id,
        )

        sev_upper = severity.upper()
        if is_recovered:
            header_color = "green"
        elif sev_upper == "CRITICAL" or event_type == "LEDGER_MISMATCH":
            header_color = "red"
        elif sev_upper == "ERROR":
            header_color = "orange"
        elif sev_upper == "WARNING":
            header_color = "yellow"
        else:
            header_color = "blue"

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
        # 统计窗口标注（2026-11-07 用户反馈）：战报按**美股交易日**切窗口，
        # 与中国自然日差一截（北京 22:00 → 次日 09:30）——标题必须写明，
        # 否则用户「今天明明成交了，报表里怎么没有」。
        window_start_cn = f"{session_date} 22:00"
        window_end_cn = f"{session_date} 09:30 (次日)"
        title = (
            f"策略复盘与战报 {session_date}"
            f"（窗口：北京时间 {window_start_cn}–{window_end_cn}）"
        )

        # 提取当日建议与实盘/模拟双轨收益指标
        advice_text = ""
        dual_track_summary = ""
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

            # 严格双轨账户战报数据提取
            real_pnl = float(rev.metrics.get("实盘已实现盈亏", 0.0))
            real_fills = int(rev.metrics.get("实盘成交数", 0))
            sim_pnl = float(rev.metrics.get("模拟已实现盈亏", 0.0))
            sim_fills = int(rev.metrics.get("模拟成交数", 0))
            real_acc_id = str(self.store.get_setting("REAL_ACCOUNT_ID", "") or "")

            # 租户标签（2026-11-06 用户拍板）：复盘标题必须能看出实盘/模拟盘各成交在哪。
            from .feishu_humanize import tenant_tag

            dual_track_lines = ["\n💼 **双轨账户收益核算**:"]
            if real_acc_id:
                real_tenants = [
                    p.tenant_id for p in DEFAULT_TENANTS.values()
                    if p.trading_env == "REAL" and p.is_active
                ]
                real_tag = " + ".join(tenant_tag(t) for t in real_tenants) or "【实盘】"
                dual_track_lines.append(
                    f"- {real_tag} 🔴 **实盘战区 (真金白银)**: 账号 `{real_acc_id}` · "
                    f"实盘已实现盈亏 `{real_pnl:+,.2f} USD` (真实成交 {real_fills} 笔)"
                )
            else:
                # 文案修正（2026-11-07 用户反馈）：0 成交 ≠ 未启用——实盘在跑，
                # 只是开仓尝试被风控/门槛拦截。误导性表述必须带真实口径。
                real_blocks = int(rev.metrics.get("实盘开仓拦截数", 0) or 0)
                dual_track_lines.append(
                    "- 【实盘】🔴 **实盘战区**: 实盘 0 成交"
                    f"（{real_blocks} 次开仓尝试被风控拦截）"
                )
            paper_tenants = [
                p.tenant_id for p in DEFAULT_TENANTS.values()
                if p.trading_env == "SIMULATE" and p.is_active
            ]
            paper_tag = " + ".join(tenant_tag(t) for t in paper_tenants) or "【模拟盘】"
            dual_track_lines.append(
                f"- {paper_tag} 🟢 **模拟战区 (100万沙盒)**: 模拟已实现盈亏 "
                f"`{sim_pnl:+,.2f} USD` (模拟成交 {sim_fills} 笔)"
            )
            dual_track_summary = "\n".join(dual_track_lines)
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
                + (f"{dual_track_summary}\n\n" if dual_track_summary else "")
                + f"🎯 **策略结论**:\n{conclusion}"
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
            ]
            if dual_track_summary:
                lines.append(dual_track_summary)
            lines.append(f"📝 **结论摘要**:\n{conclusion}")
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
