import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from futu_quant.store import TradingStore
from futu_quant.service_control import status
from futu_quant.market_calendar import USMarketCalendar

CHINA_TZ = timezone(timedelta(hours=8))
NY_TZ = ZoneInfo("America/New_York")

def in_us_regular_session(now_utc: datetime) -> bool:
    """仅美股常规交易时段（XNYS 日历自动处理节假日、半日市与夏令时）。"""
    now_ny = now_utc.astimezone(NY_TZ)
    return USMarketCalendar().session(now_ny).regular_open(now_ny)

def inspect_and_alert():
    now_utc = datetime.now(timezone.utc)
    now_china = now_utc.astimezone(CHINA_TZ)
    time_str = now_china.strftime("%Y-%m-%d %H:%M:%S")

    # 巡检与推送仅在美股常规交易时段执行，盘中盘外安静跳过
    if not in_us_regular_session(now_utc):
        now_ny = now_utc.astimezone(NY_TZ)
        print(f"[{time_str}] 当前非美股常规交易时段（美东 {now_ny:%Y-%m-%d %H:%M}），本轮巡检跳过，不推送。")
        return None

    store = TradingStore()
    svc_status = status()
    
    issues = []

    # 1. 检查后台交易调度服务进程
    if not svc_status.running:
        issues.append(f"🚨 **交易调度服务离线**：服务进程未运行！({svc_status.message})")

    # 2. 检查最近 30 分钟的操作异常事件 (ERROR/DEGRADED)
    cutoff = (now_utc - timedelta(minutes=30)).isoformat()
    try:
        events = store.operational_events(limit=300)
        recent_errs = [
            e for e in events
            if e.get("created_at") >= cutoff and e.get("status") in {"ERROR", "DEGRADED", "FAILED"}
        ]
        if recent_errs:
            err_summary = ", ".join(f"{e.get('component')}:{e.get('event_type')}" for e in recent_errs[:5])
            issues.append(f"⚠️ **近30分钟新增 {len(recent_errs)} 条异常事件**：{err_summary}")
    except Exception as e:
        issues.append(f"读取事件日志异常: {e}")

    # 3. 检查核心策略心跳状态
    try:
        hb_list = store.heartbeats()
        critical_strategies = {"core_satellite", "volatility_long_gamma", "trading_service", "order_push"}
        for h in hb_list:
            sid = h.get("strategy_id")
            if sid in critical_strategies and h.get("status") in {"ERROR", "DEGRADED"}:
                issues.append(f"⚠️ **核心任务心跳异常**：{sid} 状态为 `{h.get('status')}` ({h.get('message')})")
    except Exception as e:
        issues.append(f"读取心跳异常: {e}")

    # 4. 检查急停开关 (Kill Switch)
    try:
        if store.kill_switch("281756481476169109"):
            issues.append("🚨 **实盘风控急停触发**：富途香港账户当前处于急停状态，已阻断新开仓！")
    except Exception:
        pass

    # 5. 检查本地日志中的 FATAL / Traceback 致命报错 (最近30分钟)
    log_path = Path(__file__).resolve().parents[1] / "data" / "service.log"
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                recent_lines = lines[-200:]
                critical_lines = [
                    line.strip() for line in recent_lines
                    if "Traceback" in line or "CRITICAL" in line or "Fatal" in line
                ]
                if critical_lines:
                    sample = critical_lines[-1][:120]
                    issues.append(f"⚠️ **日志检测到严重错误追踪**：`{sample}`")
        except Exception:
            pass

    print(f"[{time_str}] 系统巡检完成，发现问题数: {len(issues)}")
    for iss in issues:
        print("  - " + iss)

    # 6. 获取最新实战交易动态（近 30 分钟是否有新成交）
    recent_fills = []
    try:
        intents = store.intents(limit=20)
        for o in intents:
            ca = o.get("created_at") or ""
            if ca >= cutoff and o.get("status") in {"FILLED", "FILLED_ALL"}:
                recent_fills.append(o)
    except Exception:
        pass

    # 7. 汇总最新账户资金与持仓情况
    acc_summary = {}
    positions_summary = []
    try:
        snap = store.broker_snapshot("account_info_futuhk_real") or {}
        pos_snap = store.broker_snapshot("positions_futuhk_real") or {}
        p = (snap.get("payload") or [{}])[0]
        acc_summary = {
            "usd_assets": p.get("usd_assets", "-"),
            "us_cash": p.get("us_cash", "-"),
        }
        for r in (pos_snap.get("payload") or []):
            code = r.get("code")
            qty = r.get("qty")
            if code and qty:
                positions_summary.append(f"`{code}`: {qty} 股/张")
    except Exception:
        pass

    # 8. 组装定时巡检概况消息推送到飞书
    # 如果有异常，发红色报警；如果没有异常，发送绿色“运行健康快报 + 账户与最新成交摘要”
    if issues:
        msg_header = f"🚨 **量化交易系统异常告警** ({time_str})\n\n"
        msg_body = "• " + "\n• ".join(issues)
    else:
        msg_header = f"🟢 **量化交易系统健康巡检报告** ({time_str})\n\n"
        msg_body = "• **系统运行状态**：各项任务指标健康，OpenD 连接与订单推送通道稳定。\n"

    # 附加最新成交动态
    if recent_fills:
        fill_lines = [
            f"`{f.get('symbol')}` ({f.get('side')}) 成交均价 `${f.get('average_fill_price')}` x {f.get('filled_quantity')} 张/股"
            for f in recent_fills[:3]
        ]
        msg_body += "\n• **近30分钟新成交**：\n  - " + "\n  - ".join(fill_lines) + "\n"

    # 附加资产与持仓概况
    if acc_summary.get("usd_assets") != "-":
        msg_body += (
            f"\n• **账户资金概况**：\n"
            f"  - 美元总资产: `${acc_summary.get('usd_assets')}` USD\n"
            f"  - 可用现金: `${acc_summary.get('us_cash')}` USD\n"
        )
    if positions_summary:
        msg_body += "  - 当前持仓: " + "，".join(positions_summary) + "\n"

    full_msg = msg_header + msg_body
    print(f"[{time_str}] 系统巡检完成，推送消息到飞书...")
    # 保存最新巡检报告到数据库，供系统或守护会话随时取用并保障持久化
    try:
        store.set_setting("latest_scheduled_audit_report", full_msg)
        store.set_setting("latest_scheduled_audit_time", time_str)
        store.set_setting("latest_scheduled_audit_has_issue", "true" if issues else "false")
    except Exception:
        pass

    try:
        # Windows 沙盒会拒绝创建子进程管道（capture_output 报 WinError 5），
        # 改用 argv 数组直调 lark-cli 的 node 入口 + 临时文件捕获输出。
        import os as _os
        import tempfile as _tempfile

        node_candidates = [
            r"C:\Program Files\nodejs\node.exe",
            r"C:\Users\Administrator\AppData\Roaming\npm\node.exe",
        ]
        _node = next((p for p in node_candidates if Path(p).exists()), "node")
        _run_js = r"C:\Users\Administrator\AppData\Roaming\npm\node_modules\@larksuite\cli\scripts\run.js"
        argv = [
            _node, _run_js, "im", "+messages-send",
            "--chat-id", "oc_880837ba7546cdf0b75834ef88eff5b4",
            "--markdown", full_msg,
            "--as", "bot",
        ]
        fd_out, out_path = _tempfile.mkstemp(suffix=".json")
        fd_err, err_path = _tempfile.mkstemp(suffix=".err")
        _os.close(fd_out)
        _os.close(fd_err)
        try:
            res = subprocess.run(
                argv,
                stdout=open(out_path, "wb"),
                stderr=open(err_path, "wb"),
                cwd=str(Path(__file__).resolve().parents[1]),
                env={**_os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                check=False,
                shell=False,
            )
            with open(out_path, "r", encoding="utf-8", errors="replace") as fh:
                _stdout = fh.read()
            with open(err_path, "r", encoding="utf-8", errors="replace") as fh:
                _stderr = fh.read()
        finally:
            for p in (out_path, err_path):
                try:
                    _os.unlink(p)
                except OSError:
                    pass
        if res.returncode == 0:
            print("飞书消息推送结果:", _stdout)
        else:
            print("飞书推送返回非0:", _stderr)
    except Exception as exc:
        print("推送飞书提示:", exc)
    return full_msg

if __name__ == "__main__":
    inspect_and_alert()
