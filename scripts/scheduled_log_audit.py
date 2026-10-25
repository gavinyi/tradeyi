import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import subprocess

# Windows GBK 控制台打印 ⚠️/🚨 等字符会抛 UnicodeEncodeError（有告警反而推不出去）；
# stdout/stderr 强制 UTF-8 容错，保证巡检链路永不因打印中断。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from futu_quant.store import TradingStore  # noqa: E402
from futu_quant.service_control import status  # noqa: E402
from futu_quant.market_calendar import USMarketCalendar  # noqa: E402
from futu_quant.process_watchdog import (  # noqa: E402
    TARGETS as WATCHDOG_TARGETS,
    evaluate_heartbeat,
    maintenance_active,
)
from futu_quant.alerts import AlertManager  # noqa: E402

CHINA_TZ = timezone(timedelta(hours=8))
NY_TZ = ZoneInfo("America/New_York")

def in_us_regular_session(now_utc: datetime) -> bool:
    """仅美股常规交易时段（XNYS 日历自动处理节假日、半日市与夏令时）。"""
    now_ny = now_utc.astimezone(NY_TZ)
    return USMarketCalendar().session(now_ny).regular_open(now_ny)

def _pid_from_file(path: str) -> int | None:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _process_cmdline(pid: int) -> str | None:
    """用 CIM 查命令行 —— PID 复用判据。查不到返回 None。"""
    ps = (
        "(Get-CimInstance Win32_Process -Filter 'ProcessId=" + str(pid) + "' "
        "-ErrorAction SilentlyContinue).CommandLine"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    cmdline = (result.stdout or "").strip()
    return cmdline or None


def _heartbeat_updated_at(store, component: str, expected_pid: int | None = None):
    """读指定组件的心跳时间。

    P1-13 修复：Worker 心跳名已按租户区分（`trading_service:<tenant>`）。
    为兼容升级前留下的旧行（`trading_service`），只在**旧行的 pid 恰好是本进程**
    时才采用 —— 否则两个租户共用一行会把僵死掩盖掉（这正是要修的那个 bug）。
    """
    rows = store.heartbeats()

    def _parse(row) -> "datetime | None":
        raw = str(row.get("updated_at") or "")
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    for row in rows:
        if str(row.get("strategy_id") or row.get("name") or "") == component:
            return _parse(row)
    if component.startswith("trading_service:"):
        for row in rows:
            if str(row.get("strategy_id") or row.get("name") or "") != "trading_service":
                continue
            row_pid = row.get("pid")
            if expected_pid is not None and str(row_pid) == str(expected_pid):
                return _parse(row)
    return None


def check_processes(store) -> list[str]:
    """P1-13 Layer 1：进程失联双校验（PID 身份 + 心跳时效）。

    **不受交易时段限制**：服务盘外死了也必须知道。正常启停窗口由维护标记豁免。
    """
    if maintenance_active():
        print("P1-13 维护标记存在：正常启停窗口，进程巡检静默。")
        return []
    alerts = AlertManager(store)
    issues: list[str] = []
    for target in WATCHDOG_TARGETS:
        pid = _pid_from_file(target.pid_file)
        cmdline = _process_cmdline(pid) if pid else None
        verdict = evaluate_heartbeat(
            target=target,
            pid_present=pid is not None,
            pid_alive=cmdline is not None,
            cmdline=cmdline,
            heartbeat_updated_at=_heartbeat_updated_at(
                store, target.heartbeat_component, pid,
            ),
        )
        if verdict is None:
            continue
        event, payload = verdict
        reason = str(payload.get("reason") or event)
        issues.append(f"🚨 **{target.name} 失联**：{reason}")
        incident_key = f"{event}:{target.tenant_id or target.heartbeat_component}"
        try:
            alerts.send("CRITICAL", event, f"[{target.name}] {reason}", incident_key=incident_key)
        except Exception as exc:  # 告警不通不能连带把巡检打挂
            print(f"告警发送失败({event}): {exc}")
        try:
            store.audit(event, payload, actor="service:watchdog")
        except Exception as exc:
            print(f"审计写入失败({event}): {exc}")
    return issues


# 训练/回测 CSV 必需列（缺任一即说明是旧版导出，大屏/审计会静默拿旧数据）
ML_CSV_REQUIRED_COLUMNS = (
    "moneyness",
    "gamma_to_theta",
    "moneyness_missing_reason",
    "gamma_to_theta_missing_reason",
    "moneyness_lag_minutes",
)
# 新鲜度容差：按**交易日**算，不是自然日（非交易日不累加）
ML_CSV_MAX_STALE_TRADING_DAYS = 5
ML_CSV_MIN_COVERAGE = 0.99


def check_csv_freshness(root: Path | None = None) -> list[str]:
    """P1-07：训练/回测 CSV 新鲜度与列契约巡检（只读，只告警）。

    背景：`data/ml_training/*.csv` 已于 2026-10-25 解除 git 跟踪，只剩磁盘副本。
    陈旧/缺失时回测大屏与 tie-break 审计仍会跑，拿旧数据出「看起来正常」的结论 ——
    这是「数据过期但没人知道」的典型形态。
    """
    import csv as _csv

    base = root or Path(__file__).resolve().parents[1]
    path = base / "data" / "ml_training" / "volatility_trades_v3_latest.csv"
    if not path.exists():
        return [
            f"⚠️ **训练 CSV 缺失**：`{path}` 不存在；回测大屏与 tie-break 审计将无数据。"
            f"请跑 `scripts/ml_export_training_data_v3.py` 重导。"
        ]

    issues: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            reader = _csv.DictReader(fh)
            header = list(reader.fieldnames or [])
            rows = list(reader)
    except Exception as exc:  # 读不动也要可见，不许静默
        return [f"⚠️ **训练 CSV 不可读**：`{path}`（{exc}）"]

    missing = [col for col in ML_CSV_REQUIRED_COLUMNS if col not in header]
    if missing:
        issues.append(
            f"⚠️ **训练 CSV 缺列**：{missing}；说明是旧版导出（无真行情列），"
            f"回测会 fail-closed 或拿旧口径出结论。请重导。"
        )
        return issues  # 缺列时覆盖率/新鲜度已无意义

    total = len(rows)
    if total:
        covered = sum(1 for r in rows if (r.get("moneyness") or "").strip())
        coverage = covered / total
    else:
        coverage = 0.0
    if coverage < ML_CSV_MIN_COVERAGE:
        issues.append(
            f"⚠️ **训练 CSV 覆盖率不足**：moneyness {coverage:.1%} < {ML_CSV_MIN_COVERAGE:.0%}"
            f"（{total} 行）；可能有样本未匹配到成交前快照。"
        )

    trade_dates = sorted({(r.get("trade_date") or "").strip() for r in rows if (r.get("trade_date") or "").strip()})
    if trade_dates:
        issues.extend(_csv_staleness_issue(trade_dates[-1], ML_CSV_MAX_STALE_TRADING_DAYS))
    return issues


def _csv_staleness_issue(last_trade_date: str, max_stale: int) -> list[str]:
    """最后成交日落后超过 N 个**交易日**才告警（XNYS 日历，不按自然日）。"""
    try:
        import pandas as pd

        calendar = USMarketCalendar()
        last = pd.Timestamp(last_trade_date[:10])
        today = pd.Timestamp(datetime.now(NY_TZ).date())
        if last >= today:
            return []
        elapsed = len(calendar.calendar.sessions_in_range(last, today)) - 1
        if elapsed > max_stale:
            return [
                f"⚠️ **训练 CSV 已陈旧**：最后成交日 {last_trade_date}，"
                f"距今 {elapsed} 个交易日（阈值 {max_stale}）。回测结论基于旧样本，需重导。"
            ]
    except Exception as exc:
        # P1-09：日历/日期解析失败**不得静默**（fail-closed）——
        # 以前这里 `return []`，等于「算不出来就当没问题」，检查会永久失明。
        return [
            f"⚠️ **训练 CSV 陈旧度无法判定（fail-closed）**：{exc!r}。"
            "本轮按异常处理并告警，请排查 XNYS 日历/交易日期取值。"
        ]
    return []


def check_snapshot_gaps(store, max_days: int = 10) -> list[str]:
    """期权历史快照断采巡检：有 FILLED 成交、但当天 option_history 零快照。

    2026-08-04~08-10 就是这样丢了 4 个交易日、约 85 笔成交，且不可回溯
    （只能用成交前快照，补成交后快照＝look-ahead，口径禁止）。

    **成交数必须取 `order_intents`（status='FILLED'）**：`fill_ledger` 只有 743 行 /
    30 个交易日，用它做左表会只查出 1 个缺口，把 8 月那批静默漏掉 —— 守卫写对了
    但数据源选错，等于没守。
    """
    sql = """
    SELECT i.d AS trade_date, COUNT(*) AS fills
    FROM (SELECT DISTINCT substr(created_at,1,10) AS d
          FROM order_intents WHERE status='FILLED') i
    LEFT JOIN (SELECT DISTINCT substr(time_key,1,10) AS d FROM option_history) o
      ON o.d = i.d
    WHERE o.d IS NULL
    GROUP BY i.d
    ORDER BY i.d DESC
    LIMIT ?
    """
    try:
        with store.connection() as connection:
            rows = connection.execute(sql, (int(max_days),)).fetchall()
    except Exception as exc:
        return [f"⚠️ **快照断采巡检失败**：{exc}"]
    issues: list[str] = []
    for trade_date, fills in rows:
        issues.append(
            f"🚨 **期权快照断采**：{trade_date} 有 {fills} 笔成交但当天 option_history "
            f"零快照 —— 这批成交无法进入训练样本且不可回溯。请检查 option_history 采集任务。"
        )
    return issues


#: `fill_ledger.fee_total` 的历史占位值：当年拿不到券商真实佣金时统一写 1.0
FEE_PLACEHOLDER_VALUE = 1.0
#: 占位值占比告警阈值（P1-14）
FEE_PLACEHOLDER_ALERT_RATIO = 0.05
#: 只看最近 N 天的成交：历史占位行**不回填**（真值不可知），
#: 若按全表算占比会永久 >90%，变成天天响的假告警（告警疲劳）。
FEE_PLACEHOLDER_WINDOW_DAYS = 7


def _fill_ledger_has_fee_state_columns(connection) -> bool:
    """迁移 17/18 之后才有三态列；巡检要能在迁移前/后两种库上都跑。"""
    cols = {row[1] for row in connection.execute("pragma table_info(fill_ledger)")}
    return {"fee_is_estimate", "fee_unavailable_reason"} <= cols


def _fee_placeholder_counts(store, window_days: int) -> dict:
    """返回占位值统计，供告警与信息项共用。

    占位值判据（Reviewer 2026-10-25）：
    - 有迁移 17/18 三态列时：`fee_total≈1.0` **且 `fee_unavailable_reason IS NULL` 且
      `fee_is_estimate=0`** —— 即「未标注的 1.0」。否则某笔估算值恰好 1.00 会误报，
      而迁移 19 已把历史行标成 `legacy_placeholder_1usd`，不该再算进新告警。
    - 三态列尚未迁移时：退回 `fee_total≈1.0`（旧库兼容，行为与发布前一致）。

    `filled_at IS NULL` 的行走不了窗口比较，单独计数返回，不混进分母。
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max(1, int(window_days)))
    ).isoformat()
    with store.connection() as connection:
        has_state = _fill_ledger_has_fee_state_columns(connection)
        if has_state:
            predicate = (
                "ABS(fee_total - ?) < 1e-9"
                " AND fee_unavailable_reason IS NULL"
                " AND COALESCE(fee_is_estimate, 0) = 0"
            )
        else:
            predicate = "ABS(fee_total - ?) < 1e-9"
        all_total = connection.execute("SELECT COUNT(*) FROM fill_ledger").fetchone()[0]
        all_ph = connection.execute(
            f"SELECT COUNT(*) FROM fill_ledger WHERE {predicate}",  # noqa: S608 - 固定片段
            (FEE_PLACEHOLDER_VALUE,),
        ).fetchone()[0]
        win_total = connection.execute(
            "SELECT COUNT(*) FROM fill_ledger WHERE filled_at >= ?", (cutoff,)
        ).fetchone()[0]
        win_ph = connection.execute(
            f"SELECT COUNT(*) FROM fill_ledger WHERE filled_at >= ? AND {predicate}",  # noqa: S608
            (cutoff, FEE_PLACEHOLDER_VALUE),
        ).fetchone()[0]
        null_filled = connection.execute(
            "SELECT COUNT(*) FROM fill_ledger"
            " WHERE filled_at IS NULL OR TRIM(filled_at) = ''"
        ).fetchone()[0]
        if has_state:
            # 迁移 19 标记过的历史占位：数值不改、不回填，但**必须始终可见**
            # （否则迁移一落地，「历史 689/743」就变 0，看起来像历史已被修好）。
            legacy = connection.execute(
                "SELECT COUNT(*) FROM fill_ledger"
                " WHERE fee_unavailable_reason = 'legacy_placeholder_1usd'"
            ).fetchone()[0]
        else:
            legacy = all_ph
    return {
        "all": (all_ph, all_total),
        "window": (win_ph, win_total),
        "legacy": (legacy, all_total),
        "null_filled_at": null_filled,
        "three_state": has_state,
    }


def check_fee_placeholder_ratio(
    store,
    threshold: float = FEE_PLACEHOLDER_ALERT_RATIO,
    window_days: int = FEE_PLACEHOLDER_WINDOW_DAYS,
) -> list[str]:
    """P1-14：最近 N 天 `fill_ledger.fee_total` 占位值（$1.0）占比超阈值即告警。

    为什么：历史回填有 689/743 行写死 1.0 占位，按账本算成本会严重低估
    （账本合计 $787.99 vs 真实全费用估算 $1,815.34）。P1-13 修完写入路径后
    新数据应为 `estimate_fees` 估算值，这条巡检验证「占位值已停止增长」。

    **只看窗口内新成交**：历史行不回填，按全表算占比会永久 >90% → 天天假告警。
    全表数字仍在巡检报告的「费用账本状态」信息行里展示（不触发告警）。
    权威成本口径仍是 `order_intents.fees`（估算 ≠ 实付），账本合计不可引用。
    """
    try:
        counts = _fee_placeholder_counts(store, window_days)
    except Exception as exc:
        return [f"⚠️ **fill_ledger 费用占位值巡检失败（fail-closed）**：{exc!r}"]
    placeholders, total = counts["window"]
    if not total:
        return []
    ratio = placeholders / total
    if ratio <= threshold:
        return []
    return [
        f"⚠️ **近 {int(window_days)} 天 fill_ledger 费用占位值占比 {ratio:.1%}**"
        f"（{placeholders}/{total} 行为 ${FEE_PLACEHOLDER_VALUE:.2f} 占位，"
        f"阈值 {threshold:.0%}）：写入路径仍在写占位值，账本成本被低估；"
        "成本口径请继续用 order_intents.fees 的估算值。"
    ]


def fee_ledger_summary_line(
    store, window_days: int = FEE_PLACEHOLDER_WINDOW_DAYS,
) -> str:
    """巡检报告里的**信息项**（不触发告警）：历史占位不回填，但也不能让它消失。"""
    try:
        counts = _fee_placeholder_counts(store, window_days)
    except Exception as exc:
        return f"• **费用账本状态**：无法统计（{exc!r}）"
    all_ph, all_total = counts["all"]
    win_ph, win_total = counts["window"]
    legacy_ph, legacy_total = counts["legacy"]
    all_pct = f"{all_ph / all_total:.1%}" if all_total else "-"
    win_pct = f"{win_ph / win_total:.1%}" if win_total else "-"
    legacy_pct = f"{legacy_ph / legacy_total:.1%}" if legacy_total else "-"
    if counts.get("three_state"):
        # 历史占位（迁移 19 标记）+ 未标注 1.0（回归哨兵）分开显示：
        # 迁移一落地，若只留新判据，历史 689 行会显示成 0，看起来像「历史已修好」。
        line = (
            f"• **费用账本状态**：历史占位 `legacy_placeholder_1usd` "
            f"**{legacy_ph}/{legacy_total} = {legacy_pct}**（不回填，仅展示）"
            f"；未标注 1.0（回归哨兵）全表 **{all_ph}/{all_total}**、"
            f"最近 {int(window_days)} 天 **{win_ph}/{win_total} = {win_pct}**"
        )
    else:
        line = (
            f"• **费用账本状态**：占位 1.0（三态列未迁移，旧判据）"
            f"**{all_ph}/{all_total} = {all_pct}**（历史不回填，仅展示）"
            f"；最近 {int(window_days)} 天 **{win_ph}/{win_total} = {win_pct}**"
        )
    null_filled = counts.get("null_filled_at") or 0
    if null_filled:
        line += f"；另有 `filled_at` 为空 **{null_filled} 行**（无法归窗，未计入分母）"
    return line


def inspect_and_alert():
    now_utc = datetime.now(timezone.utc)
    now_china = now_utc.astimezone(CHINA_TZ)
    time_str = now_china.strftime("%Y-%m-%d %H:%M:%S")

    store = TradingStore()
    # P1-13：进程失联巡检不受交易时段限制（服务盘外死了也要知道）
    process_issues = check_processes(store)

    # 其余巡检与推送仅在美股常规交易时段执行，盘中盘外安静跳过
    if not in_us_regular_session(now_utc):
        now_ny = now_utc.astimezone(NY_TZ)
        print(f"[{time_str}] 当前非美股常规交易时段（美东 {now_ny:%Y-%m-%d %H:%M}），本轮巡检跳过，不推送。")
        return process_issues or None

    svc_status = status()

    issues = list(process_issues)

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
            is_critical = sid in critical_strategies or str(sid).startswith("trading_service:")
            if is_critical and h.get("status") in {"ERROR", "DEGRADED"}:
                issues.append(f"⚠️ **核心任务心跳异常**：{sid} 状态为 `{h.get('status')}` ({h.get('message')})")
    except Exception as e:
        issues.append(f"读取心跳异常: {e}")

    # 4. 检查急停开关 (Kill Switch)
    try:
        if store.kill_switch("281756481476169109"):
            issues.append("🚨 **实盘风控急停触发**：富途香港账户当前处于急停状态，已阻断新开仓！")
    except Exception:
        pass

    # 4.1 检查三账对账与策略挂起熔断状态 (防静默熔断)
    try:
        disabled_map = store.settings_with_prefix("strategy_reconciliation_disabled")
        active_disabled = [k for k, v in disabled_map.items() if str(v).lower() == "true"]
        if active_disabled:
            paused_descs = []
            for k in active_disabled:
                parts = k.split(":")
                paused_descs.append(":".join(parts[1:]))
            issues.append(f"🔴 **三账对账异常熔断**：检测到策略处于挂起暂停状态（{', '.join(paused_descs)}），开仓已被阻断！")
        if str(store.get_setting("recovery_blocked", "")).lower() == "true":
            issues.append("⚠️ **系统对账恢复受阻**：recovery_blocked=true，已阻断自动恢复，需人工对账校准！")
    except Exception as e:
        issues.append(f"检查对账挂起状态异常: {e}")

    # 4.5 关注标的纳管监控（运维配置：runtime_config key ops_watch_symbols，JSON 字符串数组）
    # 纯只读监控：读取订单意图与虚拟仓位，检测孤儿持仓；不触碰下单路径
    watch_lines = []
    try:
        import json as _json
        _raw_watch = store.get_setting("ops_watch_symbols", "")
        _watch_symbols = [w for w in (_json.loads(_raw_watch) if _raw_watch.strip() else []) if isinstance(w, str) and w]
    except Exception:
        _watch_symbols = []
    for _wsym in _watch_symbols:
        try:
            _ws_intents = [o for o in store.intents(limit=300) if o.get("symbol") == _wsym]
            if not _ws_intents:
                watch_lines.append(f"关注标的 {_wsym}：无订单意图记录")
                continue
            _latest = max(_ws_intents, key=lambda o: (o.get("updated_at") or o.get("created_at") or ""))
            _lt = (_latest.get("updated_at") or _latest.get("created_at") or "")[:19]
            # 租户环境从意图键事实派生（尾部 :<account_id>:<env>），不硬编码环境列表（N-Tenant 就绪）
            _env_accounts = {}
            for _o in _ws_intents:
                _parts = str(_o.get("intent_key") or "").rsplit(":", 2)
                if len(_parts) == 3 and _parts[2]:
                    _env_accounts.setdefault(_parts[2], set()).add(_parts[1])
            _tenant_qty = {}
            _read_errs = []
            for _env, _accs in sorted(_env_accounts.items()):
                _q = 0.0
                try:
                    for _v in store.virtual_positions(env=_env):
                        if _v.get("symbol") == _wsym:
                            try:
                                _q += float(_v.get("quantity") or 0)
                            except (TypeError, ValueError):
                                pass
                except Exception as _re:
                    _read_errs.append(f"{_env}:{type(_re).__name__}")
                if _q:
                    _label = _env if len(_accs) == 1 else _env + "@" + ",".join(sorted(a[-4:] for a in _accs))
                    _tenant_qty[_label] = _q
            if _read_errs:
                issues.append(
                    f"🚨 **关注标的 {_wsym} 监控数据读取异常**（{'; '.join(_read_errs)}）——"
                    f"雷达本轮部分致盲，请检查 store/tenant 模块导入是否损坏"
                )
            # 决策流水证据：有近期策略决策 = 状态机在管，非孤儿；同时检测「止损触发但平仓受阻」
            try:
                _ws_decisions = [d for d in store.decisions(limit=2000) if d.get("symbol") == _wsym]
                _recent_decs = [d for d in _ws_decisions if (d.get("created_at") or "") >= cutoff]
            except Exception:
                _recent_decs = []
            for _label, _q in _tenant_qty.items():
                if not _q:
                    continue
                _env = _label.split("@", 1)[0]
                _acts = [
                    o for o in _ws_intents
                    if (o.get("updated_at") or "") >= cutoff and str(o.get("intent_key") or "").endswith(":" + _env)
                ]
                if not _acts and not _recent_decs:
                    issues.append(
                        f"⚠️ **关注标的 {_wsym} [{_label}] 持仓 {_q:g} 且近30分钟无管理动作与决策**"
                        f"（疑似孤儿单，请检查策略状态机是否纳管）"
                    )
                # 硬止损触发但无新 SELL 意图 → 平仓受阻（报价门控/时段门控等）
                _stop_decs = [d for d in _recent_decs if "止损触发" in str(d.get("reason") or "")]
                if _stop_decs and not _acts:
                    _last_reason = str(_stop_decs[-1].get("reason") or "")[:70]
                    issues.append(
                        f"🚨 **关注标的 {_wsym} [{_label}] 硬止损已触发但平仓未推进**：{_last_reason}"
                        f"（近30分钟止损决策 {len(_stop_decs)} 次，无新 SELL 意图，请检查报价新鲜度/时段门控）"
                    )
            _recent_acts = [o for o in _ws_intents if (o.get("updated_at") or "") >= cutoff]
            _recent_fills = [
                o for o in _ws_intents
                if (o.get("updated_at") or "") >= cutoff and str(o.get("status") or "").startswith("FILLED")
            ]
            _tenant_desc = "，".join(f"{_env} {_q:g}" for _env, _q in _tenant_qty.items() if _q) or "0"
            _tail = (_latest.get("intent_key") or "")[-52:]
            watch_lines.append(
                f"关注标的 {_wsym}：虚拟持仓（{_tenant_desc}）；最新意图 …{_tail}"
                f" 状态 `{_latest.get('status')}`（{_lt} UTC，近30分钟动作 {len(_recent_acts)} 次）"
            )
            if _recent_decs:
                _last_dec = sorted(_recent_decs, key=lambda d: d.get("created_at") or "")[-1]
                watch_lines.append(
                    f"关注标的 {_wsym}：策略决策活跃（近30分钟 {len(_recent_decs)} 条），最新："
                    f"{str(_last_dec.get('reason') or '')[:80]}"
                )
            for _rf in _recent_fills[:3]:
                watch_lines.append(
                    f"关注标的 {_wsym} 近30分钟成交：{_rf.get('side')} `{_rf.get('status')}`"
                    f" 均价 ${_rf.get('average_fill_price')} x {_rf.get('filled_quantity')}"
                )
        except Exception as _e:
            issues.append(f"关注标的 {_wsym} 监控读取异常: {_e}")

    # 4.6 关注标的盘口快照（OpenD 行情，子进程 + 25s 硬超时保护，失败不阻断巡检主链路）
    if _watch_symbols:
        try:
            import os as _os
            import tempfile as _tf

            _qscript = Path(__file__).resolve().parents[1] / "data" / "qa" / "ops_watch_quote.py"
            if _qscript.exists():
                _fd, _qp = _tf.mkstemp(suffix=".txt")
                _os.close(_fd)
                try:
                    _sub_env = _os.environ.copy()
                    _sub_env["PYTHONIOENCODING"] = "utf-8"
                    subprocess.run(
                        [sys.executable, str(_qscript)],
                        stdout=open(_qp, "wb"),
                        stderr=subprocess.DEVNULL,
                        cwd=str(Path(__file__).resolve().parents[1]),
                        env=_sub_env,
                        timeout=25,
                        check=False,
                        shell=False,
                    )
                    with open(_qp, "r", encoding="utf-8", errors="replace") as _fh:
                        _qlines = [item[6:].strip() for item in _fh.read().splitlines() if item.startswith("QUOTE|")]
                    if _qlines:
                        watch_lines.extend("盘口：" + item for item in _qlines)
                    else:
                        watch_lines.append("盘口：本轮未取到行情数据（不影响巡检）")
                except subprocess.TimeoutExpired:
                    watch_lines.append("盘口：获取超时（25s），本轮跳过行情，不影响巡检")
                finally:
                    try:
                        _os.unlink(_qp)
                    except OSError:
                        pass
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

    # 5.1 训练/回测 CSV 新鲜度（Chief 2026-10-25 指派 P1-07）
    # 为什么要有：CSV 已解除 git 跟踪、只剩磁盘副本，陈旧或缺失会让回测大屏与
    # tie-break 审计拿着旧数据出「看起来正常」的结论。只告警，不碰交易。
    issues.extend(check_csv_freshness())

    # 5.2 期权历史快照断采（2026-08-04~08-10 事故形态）
    # 有 FILLED 成交但当天 option_history 零快照 → 那批成交永远无法入训且不可回溯。
    # 成交数必须取 order_intents：fill_ledger 只有 30 个交易日、会静默漏掉 8 月那批。
    issues.extend(check_snapshot_gaps(store))

    # 5.3 fill_ledger 费用占位值占比（P1-14；P1-13 修写入路径后的回归守卫）
    # 历史 689/743 行 fee_total 写死 1.0，账本成本因此低估 2.3 倍；
    # 这条只告警、不碰交易，用来确认占位值不再增长。
    issues.extend(check_fee_placeholder_ratio(store))

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

    # 附加关注标的纳管监控摘要
    if watch_lines:
        msg_body += "\n• **关注标的纳管监控**：\n  - " + "\n  - ".join(watch_lines) + "\n"

    # 附加资产与持仓概况
    if acc_summary.get("usd_assets") != "-":
        msg_body += (
            f"\n• **账户资金概况**：\n"
            f"  - 美元总资产: `${acc_summary.get('usd_assets')}` USD\n"
            f"  - 可用现金: `${acc_summary.get('us_cash')}` USD\n"
        )
    if positions_summary:
        msg_body += "  - 当前持仓: " + "，".join(positions_summary) + "\n"

    # 费用账本三态可见性（P1-14）：历史占位不回填，但必须让它可见（信息项，不告警）
    msg_body += "\n" + fee_ledger_summary_line(store) + "\n"

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
