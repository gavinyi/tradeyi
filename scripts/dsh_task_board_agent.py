"""DSH scheduled-agent helper for the daily task-board timers.

Run from the DSH Schedule agent (or any agent turn that has the user's
IM/WeChat channel connected) to:

1. Generate the daily 策略复盘与执行质量 HTML for the latest US session
   and queue it for delivery to WeChat via the DSH IM plugin.
2. Mark the queued review as delivered (after the agent has sent it).
3. Trigger the computer shutdown timer.

Usage (inside a DSH agent turn, working directory = the repo):

    uv run python scripts/dsh_task_board_agent.py review
    uv run python scripts/dsh_task_board_agent.py deliver
    uv run python scripts/dsh_task_board_agent.py shutdown

Each command prints a single JSON object on stdout so the DSH agent can
parse the result reliably. The `review` step generates the HTML + queues a
pending-delivery record; `deliver` flips that record to DELIVERED once the
agent has actually sent it through the DSH IM/WeChat channel. Both are
idempotent per US session date, so repeated same-day runs are no-ops.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
os.chdir(REPO_ROOT)

from futu_quant.store import TradingStore  # noqa: E402
from futu_quant.scheduled_tasks import (  # noqa: E402
    DAILY_STRATEGY_REVIEW_TASK,
    SHUTDOWN_TASK,
    _timer_due,
    deliver_pending_im_review,
    pending_im_delivery,
    run_daily_strategy_review,
    set_timer_enabled,
    shutdown_computer,
    timer_default_enabled,
    timer_enabled_setting_key,
)

FEISHU_DELIVERY_BOT_ID = "bot_5f688de9a61349d19fed109587e47910"
FEISHU_DELIVERY_TARGET_ID = "tgt_813f3c4aed8fc2cd"
# lark-cli 应用下 147 机器人会话（你实际收消息的地方）。
# 必须直发 chat_id；用 --user-id ou_4cac 会落到另一个会话，端上收不到。
FEISHU_DELIVERY_CHAT_ID = "oc_47d5da371ac34e16cdc7b20bcdd7f27a"
FEISHU_REVIEW_FOLDER_TOKEN = "IKiVf2vtTlvnswdhke5ctLhtntd"


def _is_enabled(store: TradingStore, timer) -> bool:
    return (
        store.get_setting(
            timer_enabled_setting_key(timer),
            str(timer_default_enabled(timer)).lower(),
        )
        == "true"
    )


def do_review(args: argparse.Namespace) -> int:
    store = TradingStore()
    if args.enable:
        set_timer_enabled(store, DAILY_STRATEGY_REVIEW_TASK, True)
    if not _is_enabled(store, DAILY_STRATEGY_REVIEW_TASK):
        print_json({
            "ok": False,
            "error": "任务看板定时器：策略复盘未启用。请先启用（--enable）或在系统监控页面打开。",
        })
        return 1
    path = run_daily_strategy_review(store)
    record = pending_im_delivery(store)
    payload = {
        "ok": True,
        "html_path": path,
        "queued_for_im": record is not None,
    }
    if record:
        payload["session_date"] = record.get("session_date")
        payload["conclusion"] = record.get("conclusion")
        payload["next_step"] = (
            "agent 将 conclusion 文本与 html_path 文件通过 dsh IM 微信 chat 插件投递给用户，"
            "然后运行 `uv run python scripts/dsh_task_board_agent.py deliver` 标记已投递。"
        )
    else:
        payload["next_step"] = "未登记待投递记录（可能当日已投递过），无需再投递。"
    print_json(payload)
    return 0


def do_poll(args: argparse.Namespace) -> int:
    """Poll for a PENDING review without generating or marking anything.

    Intended for the DSH Schedule "delivery poll" reminder (e.g. every 10
    minutes): the agent runs this, and only when ``pending`` is true does it
    import the report as a Feishu online doc (via lark-cli) and push the doc
    link to the user's WeChat through scripts/weixin-send.mjs (a direct
    proactive ilink bot send — the only path that actually reaches WeChat),
    then call ``deliver`` to flip the record to DELIVERED.

    IMPORTANT: do NOT use ``dsh_im_return_file`` to register the HTML/md file
    (it only lands in the DSH web session, not WeChat), and do NOT rely on
    writing the link into this chat's reply (task-board sessions are not
    synced back to the user's WeChat conversation).
    When nothing is pending it returns ``pending: false`` so the agent can
    quietly do nothing (no duplicate sends, no noise).
    """
    store = TradingStore()
    record = pending_im_delivery(store)
    if record is None:
        print_json({
            "ok": True,
            "pending": False,
            "note": "没有待投递的复盘记录，本轮无需操作。",
        })
        return 0
    markdown = record.get("markdown") or ""
    if not markdown:
        # Old record without markdown: fall back to the md file if present.
        md_path = Path(record.get("markdown_path", ""))
        if md_path.exists():
            markdown = md_path.read_text(encoding="utf-8")
    if not markdown:
        markdown = record.get("conclusion", "")
    session_date = record.get("session_date") or "未知日期"
    print_json({
        "ok": True,
        "pending": True,
        "session_date": record.get("session_date"),
        "conclusion": record.get("conclusion"),
        "html_path": record.get("html_path"),
        "markdown_path": record.get("markdown_path"),
        "markdown": markdown,
        "delivery": {
            "channel": "feishu",
            "bot_id": FEISHU_DELIVERY_BOT_ID,
            "target_id": FEISHU_DELIVERY_TARGET_ID,
            "chat_id": FEISHU_DELIVERY_CHAT_ID,
            "folder_token": FEISHU_REVIEW_FOLDER_TOKEN,
        },
        "next_step": (
            "运行 `uv run python scripts/dsh_task_board_agent.py feishu`。\n"
            "该命令会：(a) 把 markdown 导入飞书在线文档；(b) 以 bot 身份直发 147 机器人会话\n"
            f"（chat_id={FEISHU_DELIVERY_CHAT_ID}），发一条 post 富文本卡片（含明文 doc_url + 结论摘要）；\n"
            "(c) 发送成功后内部标记 DELIVERED。\n"
            "若返回 ok=false → 不要手动 deliver，保持 PENDING 等下一轮重试。\n"
            "注意：必须走 chat_id 直发 147 会话；不要用 --user-id；不要加固定幂等 key；"
            "不要用 Card 2.0 卡片；不要用 dsh_im_return_file（后台轮次没有 DSH-IM 消费者）。"
        ),
    })
    return 0


LARK_RUN_JS = r"C:\Users\Administrator\AppData\Roaming\npm\node_modules\@larksuite\cli\scripts\run.js"


def _lark_node_exe() -> str | None:
    candidates = [
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Users\Administrator\AppData\Roaming\npm\node.exe",
    ]
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    found = shutil.which("node")
    return found


def _json_text(payload: dict) -> str:
    """Serialize a JSON envelope for stdout without BOM or GBK console issues."""
    text = json.dumps(payload, ensure_ascii=True, indent=2)
    text = text.replace("\\ufeff", "").replace("\ufeff", "")
    return text


def print_json(payload: dict) -> None:
    print(_json_text(payload))


def _sanitize_json_output(payload: str) -> str:
    return payload.replace("\ufeff", "")


def _lark_json(cmd: list[str]) -> dict:
    """Run a lark-cli command and parse its stdout as a JSON envelope.

    Uses ``subprocess`` with an explicit argv list (no shell) so that CJK
    arguments are passed as UTF-8 strings intact — no PowerShell re-encoding.
    Stdout/stderr are captured via temp files to avoid sandbox pipe restrictions.
    """
    import tempfile
    argv = list(cmd)
    if argv and argv[0] == "lark-cli" and os.path.exists(LARK_RUN_JS):
        node = _lark_node_exe()
        if node:
            argv = [node, LARK_RUN_JS, *argv[1:]]

    fd_out, out_path = tempfile.mkstemp(suffix=".json")
    fd_err, err_path = tempfile.mkstemp(suffix=".err")
    os.close(fd_out)
    os.close(fd_err)
    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        proc = subprocess.run(
            argv,
            stdout=open(out_path, "wb"),
            stderr=open(err_path, "wb"),
            cwd=str(REPO_ROOT),
            env=env,
            check=False,
        )
        with open(out_path, "r", encoding="utf-8") as fh:
            output = fh.read()
        with open(err_path, "r", encoding="utf-8") as fh:
            stderr_text = fh.read()
    except OSError as exc:
        proc = subprocess.run(
            argv,
            check=False,
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
        output = ""
        stderr_text = str(exc)
    finally:
        for p in (out_path, err_path):
            try:
                os.unlink(p)
            except OSError:
                pass

    cleaned = output.lstrip("\ufeff").strip()
    if not cleaned:
        return {"ok": False, "error": f"lark-cli exited {proc.returncode} with no output; stderr={stderr_text[:300]}"}
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            inner_start = cleaned.find("{")
            inner_end = cleaned.rfind("}")
            if inner_start != -1 and inner_end > inner_start:
                payload = json.loads(cleaned[inner_start:inner_end + 1])
            else:
                return {"ok": False, "error": cleaned or f"lark-cli exited {proc.returncode}"}
        except json.JSONDecodeError:
            return {"ok": False, "error": cleaned or f"lark-cli exited {proc.returncode}"}
    return payload


def do_feishu(args: argparse.Namespace) -> int:
    """Import the pending review as a Feishu doc and send it to the fixed Feishu target."""
    store = TradingStore()
    record = pending_im_delivery(store)
    if record is None:
        print_json({
            "ok": True,
            "pending": False,
            "note": "没有待投递的复盘记录，本轮无需操作。",
        })
        return 0
    markdown_path = record.get("markdown_path", "")
    if not markdown_path or not Path(markdown_path).exists():
        print_json({
            "ok": False,
            "pending": True,
            "error": "缺少 markdown 文件或路径不存在，无法投递飞书。",
            "markdown_path": markdown_path,
        })
        return 1
    session_date = record.get("session_date", "未知日期")
    title = f"策略复盘报告 {session_date}"
    relative_md = Path(markdown_path).relative_to(REPO_ROOT).as_posix()
    # 个人 user 上传到固定复盘目录；若目录不可写则退回 user 根目录，
    # 文档链接随后由 bot 发送到 147 会话。
    import_cmd = [
        "lark-cli", "drive", "+import",
        "--file", relative_md,
        "--type", "docx",
        "--folder-token", FEISHU_REVIEW_FOLDER_TOKEN,
        "--name", title,
        "--as", "user",
    ]
    import_result = _lark_json(import_cmd)
    if not import_result.get("ok") and FEISHU_REVIEW_FOLDER_TOKEN:
        import_cmd_fallback = [
            "lark-cli", "drive", "+import",
            "--file", relative_md,
            "--type", "docx",
            "--name", title,
            "--as", "user",
        ]
        fallback_result = _lark_json(import_cmd_fallback)
        if fallback_result.get("ok"):
            import_result = fallback_result
    if not import_result.get("ok"):
        print_json({
            "ok": False,
            "pending": True,
            "error": "飞书文档导入失败，未标记已投递。",
            "import_result": import_result,
        })
        return 1
    doc_url = import_result.get("data", {}).get("url", "")
    if not doc_url:
        # 导入可能成功但 data.url 缺失：给出可排查的错误，避免发空链接
        print_json({
            "ok": False,
            "pending": True,
            "error": "飞书文档导入成功但未返回 url，无法生成可点击链接。",
            "import_result": import_result,
        })
        return 1
    # 只发一条 post 富文本（含可点击"打开飞书文档"链接），直接发到
    # 147 机器人会话（oc_47d5...），bot 身份。不通过 --user-id 发到
    # ou_4cac（那条会落到别的会话，收不到）。
    base_send = [
        "lark-cli", "im", "+messages-send",
        "--chat-id", FEISHU_DELIVERY_CHAT_ID,
        "--as", "bot",
    ]
    # 147 会话里 post 的 a 标签链接不渲染成可点击（退化成纯文本、链接被吞）。
    # 所以把 doc_url 明文写进卡片，确保链接一定可见、可复制点开。
    # 使用 --markdown 代替 --content post JSON，避免 shell 引号解析问题。
    # 附：简要问题与建议（基于同一份 StrategyReview 重新生成，确定性、不依赖 LLM）。
    advice_text = ""
    try:
        from datetime import date as _date
        from futu_quant.strategy_review import build_strategy_review, _review_issue_advice
        from futu_quant.trade_diagnostics import diagnostic_window
        _sd = _date.fromisoformat(str(record.get("session_date", "")))
        _review = build_strategy_review(store, diagnostic_window(_sd))
        issues, advice = _review_issue_advice(_review)
        if issues or advice:
            advice_lines = ["\n当日问题与建议："]
            if issues:
                advice_lines.append("问题：" + "；".join(issues[:3]))
            if advice:
                advice_lines.append("建议：" + "；".join(advice[:3]))
            advice_text = "\n".join(advice_lines)
    except Exception:
        advice_text = ""

    markdown_body = (
        f"任务看板 · 策略复盘报告 {session_date}\n"
        f"文档链接：{doc_url}\n"
        f"结论摘要：\n"
        f"- {record.get('conclusion', '')}"
        + (f"\n{advice_text}" if advice_text else "")
    )
    send_cmd = base_send + [
        "--markdown", markdown_body,
    ]
    if args.dry_run:
        send_cmd.append("--dry-run")
    send_result = _lark_json(send_cmd)
    if not send_result.get("ok"):
        print_json({
            "ok": False,
            "pending": True,
            "error": "飞书 post 富文本发送失败，未标记已投递。",
            "doc_url": doc_url,
            "send_result": send_result,
        })
        return 1
    if not args.dry_run:
        deliver_pending_im_review(store, mark_done=True)
    print_json({
        "ok": True,
        "pending": args.dry_run,
        "session_date": session_date,
        "doc_url": doc_url,
        "file_path": record.get("markdown_path"),
        "html_path": record.get("html_path"),
        "marked_delivered": not args.dry_run,
        "bot_id": FEISHU_DELIVERY_BOT_ID,
        "target_id": FEISHU_DELIVERY_TARGET_ID,
    })
    return 0


def do_deliver(args: argparse.Namespace) -> int:
    """Mark the pending review as delivered (run after the agent has sent it to WeChat)."""
    store = TradingStore()
    result = deliver_pending_im_review(store, mark_done=True)
    if result is None:
        print_json({
            "ok": True,
            "delivered": False,
            "note": "没有待投递的复盘记录（可能已投递或未生成）。",
        })
        return 0
    print_json({
        "ok": True,
        "delivered": True,
        "session_date": result.get("session_date"),
        "html_path": result.get("html_path"),
        "note": "已标记 DSH IM 微信投递完成。",
    })
    return 0


def do_shutdown(args: argparse.Namespace) -> int:
    store = TradingStore()
    if args.enable:
        set_timer_enabled(store, SHUTDOWN_TASK, True)
    if not _is_enabled(store, SHUTDOWN_TASK):
        print_json({
            "ok": False,
            "error": "任务看板定时器：电脑关机未启用。请先启用（--enable）或在系统监控页面打开。",
        })
        return 1
    if not _timer_due(store, SHUTDOWN_TASK):
        print_json({
            "ok": True, "shut_down": False,
            "note": "今日关机定时器已触发过，跳过。",
        })
        return 0
    dry_run = args.dry_run or os.getenv("AUTO_SHUTDOWN_DRY_RUN", "false").lower() == "true"
    result = shutdown_computer(store, dry_run=dry_run)
    print_json({
        "ok": True,
        "dry_run": dry_run,
        "shut_down": not dry_run,
        "message": result,
    })
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="DSH scheduled-agent helper for task-board timers.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    review_parser = sub.add_parser("review", help="生成当日策略复盘 HTML 并登记 DSH IM 待投递。")
    review_parser.add_argument("--enable", action="store_true", help="同时启用策略复盘定时器。")
    review_parser.set_defaults(func=do_review)

    deliver_parser = sub.add_parser("deliver", help="标记 DSH IM 微信投递完成（agent 发完微信后调用）。")
    deliver_parser.set_defaults(func=do_deliver)

    poll_parser = sub.add_parser("poll", help="检查是否有待投递复盘（DSH Schedule 轮询提醒用，不生成不标记）。")
    poll_parser.set_defaults(func=do_poll)

    feishu_parser = sub.add_parser("feishu", help="把待投递复盘导入飞书并发送到固定 DSH-IM 飞书 target。")
    feishu_parser.add_argument("--dry-run", action="store_true", help="只预演飞书导入与发送，不标记已投递。")
    feishu_parser.set_defaults(func=do_feishu)

    shutdown_parser = sub.add_parser("shutdown", help="触发每日电脑关机定时器（默认演练）。")
    shutdown_parser.add_argument("--enable", action="store_true", help="同时启用关机定时器。")
    shutdown_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="演练模式：只写审计记录，不真正关机。",
    )
    shutdown_parser.set_defaults(func=do_shutdown)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
