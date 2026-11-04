from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.account_metrics import account_pnl_metrics
from futu_quant.combo_execution import ComboExecutionManager, create_combo, validate_combo
from futu_quant.combo_analytics import (
    build_template, capacity_assessment, combo_execution_quality, combo_market_snapshot,
    expiry_payoff, rank_vertical_candidates, roll_symbol, run_combo_backtest,
)
from futu_quant.config import Settings
from futu_quant.execution import ExecutionManager
from futu_quant.risk import RiskLimits
from futu_quant.replay import run_combo_resilience_drill
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.strategy_plugins import strategy_plugins
from futu_quant.ui import china_time_columns, chinese_columns, render_admin_login, render_sidebar


st.set_page_config(page_title="期权组合执行", page_icon="🧩", layout="wide")
# 本页在页面内真实提交组合单（ComboExecutionManager），settings 会流进下单/风控路径。
# 不走 Settings.load_cached()：缓存最多陈旧 2 秒，但在下单侧「2 秒前的行情/额度」
# 不能接受。改动前就是裸 load()，这里保持原样（2026-11-04 Reviewer B2）。
settings = Settings.load()
render_sidebar(settings)
store = TradingStore()
broker = FutuPaperBroker(settings)
executor = ExecutionManager(broker, store)
combo_executor = ComboExecutionManager(broker, store, executor)
admin = render_admin_login()

st.title("期权组合执行")
st.warning("组合订单仅提交到富途美股模拟盘。系统采用受控分腿执行，不会把分腿订单误称为券商原子组合单。")
st.caption(
    "开仓先成交保护腿，再提交短风险腿；平仓先买回短腿，再卖出保护腿。"
    "组合装配失败时自动撤销未成交腿，并对已成交腿执行反向回滚。"
)


@st.cache_data(ttl=15, max_entries=1, show_spinner=False)
def combo_entry_preflight() -> tuple[bool, str]:
    current_store = TradingStore()
    if current_store.kill_switch():
        return False, "系统急停已开启"
    if current_store.get_setting("recovery_blocked", "false") == "true":
        return False, "启动恢复对账尚未通过"
    if current_store.get_setting("connectivity_degraded", "false") == "true":
        return False, "OpenD 连接处于降级状态"
    current_broker = FutuPaperBroker(Settings.load())
    pnl = account_pnl_metrics(current_broker.positions())
    daily_loss = max(0.0, -float(pnl["today_position_pnl"]))
    limit = RiskLimits.load().max_daily_loss
    if daily_loss >= limit:
        return False, f"当日持仓亏损 ${daily_loss:,.2f}，已超过 ${limit:,.2f} 新增风险急停线"
    return True, f"新增风险预检通过；当日持仓盈亏 ${float(pnl['today_position_pnl']):,.2f}"


try:
    combo_entry_allowed, combo_entry_reason = combo_entry_preflight()
except (BrokerError, ValueError, KeyError) as exc:
    combo_entry_allowed, combo_entry_reason = False, f"组合开仓预检数据不可用：{exc}"

if combo_entry_allowed:
    st.success(combo_entry_reason)
else:
    st.warning(f"当前禁止新增组合风险：{combo_entry_reason}。这不是页面故障；平仓与撤单仍可使用。")

status_labels = {
    "DRAFT": "草稿", "EXECUTING": "正在分腿开仓", "OPEN": "已完整开仓",
    "CLOSING": "正在组合平仓", "ROLLBACK_PENDING": "正在失败回滚",
    "CLOSED": "已平仓", "FAILED": "失败", "PLANNED": "待提交",
    "WORKING": "待成交", "FILLED": "已成交", "CLOSE_WORKING": "平仓待成交",
    "CLOSE_FAILED": "平仓失败",
}

create_tab, template_tab, manage_tab, candidate_tab, backtest_tab, quality_tab, drill_tab, audit_tab, guide_tab = st.tabs(
    ["创建组合", "结构模板", "组合管理", "候选生成", "组合回测", "执行质量", "故障演练", "执行审计", "安全规则"]
)

with create_tab:
    st.subheader("创建 2–4 腿期权组合")
    strategy_choices = [plugin.strategy_id for plugin in strategy_plugins()]
    with st.form("create_option_combo", border=True):
        with st.container(horizontal=True):
            name = st.text_input("组合名称", value="垂直价差")
            underlying = st.text_input("正股代码", value="US.NVDA").strip().upper()
            strategy_id = st.selectbox("策略归属", strategy_choices)
            structure = st.selectbox(
                "组合结构", ["VERTICAL", "IRON_CONDOR", "BUTTERFLY", "CALENDAR", "CUSTOM"],
                format_func=lambda value: {
                    "VERTICAL": "垂直价差", "IRON_CONDOR": "铁鹰",
                    "BUTTERFLY": "蝶式", "CALENDAR": "日历价差", "CUSTOM": "自定义",
                }[value],
            )
        legs_seed = pd.DataFrame([
            {"腿角色": "PROTECTIVE", "合约代码": "", "方向": "BUY", "数量": 1, "限价": 1.00},
            {"腿角色": "RISK", "合约代码": "", "方向": "SELL", "数量": 1, "限价": 0.50},
        ])
        edited = st.data_editor(
            legs_seed, key="combo_leg_editor", num_rows="dynamic", hide_index=True,
            column_config={
                "腿角色": st.column_config.SelectboxColumn(
                    options=["PROTECTIVE", "RISK"], required=True,
                ),
                "合约代码": st.column_config.TextColumn(required=True),
                "方向": st.column_config.SelectboxColumn(options=["BUY", "SELL"], required=True),
                "数量": st.column_config.NumberColumn(min_value=1, step=1, required=True),
                "限价": st.column_config.NumberColumn(min_value=0.01, format="$%.2f", required=True),
            },
        )
        with st.container(horizontal=True):
            max_debit = st.number_input("最大净借记（每股）", min_value=0.0, value=1.0, step=0.05)
            min_credit = st.number_input("最小净贷记（每股）", min_value=0.0, value=0.0, step=0.05)
            assembly_timeout = st.number_input("组合装配超时（秒）", min_value=60, max_value=1800, value=300, step=30)
            naked_timeout = st.number_input("裸腿保护时限（秒）", min_value=5, max_value=60, value=15, step=5)
        auto_rollback = st.checkbox("任一腿失败后自动回滚已成交腿", value=True)
        confirmed = st.checkbox("我确认所有订单仅进入富途模拟盘，并理解分腿执行存在价差和部分成交风险")
        submitted = st.form_submit_button("校验并保存组合草稿", type="primary", disabled=not admin)
    if submitted:
        legs = [{
            "role": str(row["腿角色"]), "symbol": str(row["合约代码"]).strip().upper(),
            "side": str(row["方向"]), "quantity": int(row["数量"]),
            "limit_price": float(row["限价"]),
        } for _, row in edited.dropna(how="all").iterrows()]
        combo = {
            "strategy_id": strategy_id, "name": name, "underlying": underlying,
            "structure": structure, "max_net_debit": max_debit,
            "min_net_credit": min_credit, "assembly_timeout_seconds": assembly_timeout,
            "naked_timeout_seconds": naked_timeout, "auto_rollback": auto_rollback,
        }
        try:
            if not confirmed:
                raise ValueError("请先勾选模拟盘风险确认")
            debit, credit = validate_combo(combo, legs)
            combo_id = create_combo(store, combo, legs)
            st.success(
                f"组合 {combo_id} 已保存为草稿；预计净借记 ${debit:.2f} / 净贷记 ${credit:.2f}，"
                "尚未提交任何订单。"
            )
            st.rerun()
        except (ValueError, TypeError) as exc:
            st.error(f"组合校验失败：{exc}")

with template_tab:
    st.subheader("标准组合结构向导")
    st.caption("按当前期权链自动生成结构，生成后只保存草稿，不会直接下单。")
    with st.form("combo_template_form", border=True):
        template_underlying = st.text_input("模板正股", value="US.NVDA").strip().upper()
        template_expiration = st.date_input("近月/模板到期日", value=date(2026, 7, 24))
        template_far_expiration = st.date_input("日历价差远月到期日", value=date(2026, 8, 21))
        template_structure = st.selectbox(
            "结构模板", ["VERTICAL_CALL", "VERTICAL_PUT", "IRON_CONDOR", "BUTTERFLY", "STRADDLE", "STRANGLE", "CALENDAR"],
            format_func=lambda value: {
                "VERTICAL_CALL": "Call 信用价差", "VERTICAL_PUT": "Put 信用价差",
                "IRON_CONDOR": "铁鹰", "BUTTERFLY": "蝶式", "STRADDLE": "跨式",
                "STRANGLE": "宽跨式", "CALENDAR": "Call 日历价差",
            }[value],
        )
        template_submit = st.form_submit_button("生成结构", icon=":material/auto_awesome:")
    if template_submit:
        try:
            chain = broker.option_chain(template_underlying, template_expiration.isoformat())
            if template_structure == "CALENDAR":
                chain = pd.concat([
                    chain, broker.option_chain(template_underlying, template_far_expiration.isoformat())
                ], ignore_index=True).drop_duplicates("code")
            codes = chain["code"].astype(str).tolist()
            quotes = broker.snapshots(codes)
            enriched = chain.drop(columns=[column for column in quotes.columns if column != "code" and column in chain], errors="ignore").merge(quotes, on="code", how="left")
            spot = float(broker.snapshots([template_underlying]).iloc[0]["last_price"])
            st.session_state.combo_template = build_template(enriched, template_structure, template_underlying, spot)
            st.session_state.combo_template_meta = (template_underlying, template_structure)
        except (BrokerError, ValueError, KeyError) as exc:
            st.error(f"模板生成失败：{exc}")
    template_legs = st.session_state.get("combo_template", [])
    if template_legs:
        st.dataframe(chinese_columns(pd.DataFrame(template_legs)), hide_index=True)
        net = sum((1 if leg["side"] == "BUY" else -1) * leg["quantity"] * leg["limit_price"] for leg in template_legs)
        st.metric("模板限价净借记", f"${net:.2f}" if net >= 0 else f"净贷记 ${-net:.2f}", border=True)
        if st.button("保存模板为组合草稿", type="primary", disabled=not admin):
            try:
                meta_underlying, meta_structure = st.session_state.combo_template_meta
                combo_id = create_combo(store, {
                    "strategy_id": "volatility_long_gamma", "name": f"{meta_structure} 模板",
                    "underlying": meta_underlying, "structure": meta_structure,
                    "max_net_debit": max(0.01, net + 0.1) if net >= 0 else 0,
                    "min_net_credit": max(0, -net - 0.1), "assembly_timeout_seconds": 300,
                    "naked_timeout_seconds": 15, "auto_rollback": True,
                }, template_legs)
                st.success(f"已保存草稿 {combo_id}")
            except ValueError as exc:
                st.error(str(exc))

with manage_tab:
    combos = pd.DataFrame(store.option_combos())
    if combos.empty:
        st.info("还没有期权组合。请先在“创建组合”保存草稿。")
    else:
        summary = china_time_columns(combos.copy())
        summary["status"] = summary["status"].map(lambda value: status_labels.get(value, value))
        st.dataframe(
            chinese_columns(summary.drop(columns=["error"], errors="ignore")), hide_index=True,
            column_config={
                "最大净借记": st.column_config.NumberColumn(format="$%.2f"),
                "最小净贷记": st.column_config.NumberColumn(format="$%.2f"),
                "自动回滚": st.column_config.CheckboxColumn(),
            },
        )
        selected_id = st.selectbox(
            "选择组合", combos["combo_id"].tolist(),
            format_func=lambda value: (
                f"{value} · {combos.loc[combos['combo_id'] == value, 'name'].iloc[0]} · "
                f"{status_labels.get(combos.loc[combos['combo_id'] == value, 'status'].iloc[0], '')}"
            ),
        )
        selected = store.option_combo(selected_id)
        legs = pd.DataFrame(store.option_combo_legs(selected_id))
        if not legs.empty:
            legs["status"] = legs["status"].map(lambda value: status_labels.get(value, value))
            st.dataframe(
                chinese_columns(legs.drop(columns=["created_at", "updated_at"], errors="ignore")),
                hide_index=True,
                column_config={"限价": st.column_config.NumberColumn(format="$%.2f")},
            )
        with st.container(horizontal=True):
            if st.button(
                "开始模拟盘组合开仓", type="primary", icon=":material/play_arrow:",
                disabled=(not admin or not combo_entry_allowed
                          or selected["status"] not in {"DRAFT", "FAILED"}),
            ):
                try:
                    result = combo_executor.start(selected_id)
                    st.toast(status_labels.get(result["status"], result["status"]))
                    st.rerun()
                except (ValueError, BrokerError) as exc:
                    st.error(f"组合开仓失败：{exc}")
            if st.button(
                "组合一键平仓", icon=":material/close:",
                disabled=not admin or selected["status"] != "OPEN",
            ):
                try:
                    combo_executor.request_close(selected_id)
                    st.rerun()
                except (ValueError, BrokerError) as exc:
                    st.error(f"组合平仓失败：{exc}")
            if st.button("立即同步组合状态", icon=":material/refresh:"):
                try:
                    combo_executor.advance(selected_id)
                    st.rerun()
                except (ValueError, BrokerError) as exc:
                    st.error(f"组合状态同步失败：{exc}")
        if selected.get("error"):
            st.error(f"最近一次组合执行失败：{selected['error']}")
            failed_legs = pd.DataFrame([
                {"合约代码": leg["symbol"], "状态": status_labels.get(leg["status"], leg["status"]),
                 "失败原因": leg.get("error", "")}
                for leg in store.option_combo_legs(selected_id) if leg.get("error")
            ])
            if not failed_legs.empty:
                st.caption("请先处理下方失败原因；未成交的失败组合可在风险解除后重新启动。")
                st.dataframe(failed_legs, hide_index=True)
        st.divider()
        st.subheader("实时组合净价、容量与风险")
        if st.button("读取组合实时行情", icon=":material/candlestick_chart:"):
            try:
                raw_legs = store.option_combo_legs(selected_id)
                quotes = broker.snapshots([leg["symbol"] for leg in raw_legs])
                detail, package = combo_market_snapshot(raw_legs, quotes)
                capacity = capacity_assessment(detail)
                st.session_state.combo_live = (selected_id, detail, package, capacity)
            except (BrokerError, ValueError, KeyError, IndexError) as exc:
                st.error(f"组合实时行情读取失败：{exc}")
        live = st.session_state.get("combo_live")
        if live and live[0] == selected_id:
            _, detail, package, capacity = live
            raw_legs = store.option_combo_legs(selected_id)
            with st.container(horizontal=True):
                st.metric("组合中间净借记", f"${package['mid_net_debit']:.2f}", border=True)
                st.metric("立即成交净借记", f"${package['executable_net_debit']:.2f}", border=True)
                st.metric("组合盘口成本", f"${package['package_spread'] * 100:.2f}", border=True)
                st.metric("估算容量", f"{capacity['capacity']} 组", border=True)
            with st.container(horizontal=True):
                st.metric("净 Delta", f"{package['net_delta']:.2f}", border=True)
                st.metric("净 Gamma", f"{package['net_gamma']:.2f}", border=True)
                st.metric("净 Theta", f"{package['net_theta']:.2f}", border=True)
                st.metric("净 Vega", f"{package['net_vega']:.2f}", border=True)
            (st.success if capacity["allowed"] else st.error)(capacity["reason"])
            st.dataframe(chinese_columns(detail), hide_index=True)
            try:
                spot_quote = broker.snapshots([selected["underlying"]])
                spot = float(spot_quote.iloc[0].get("last_price", 0) or 0)
                if spot > 0:
                    payoff = expiry_payoff(raw_legs, spot * 0.7, spot * 1.3)
                    st.line_chart(payoff, x="正股到期价格", y="组合到期盈亏")
                    st.caption(f"图中最大收益约 ${payoff['组合到期盈亏'].max():,.2f}，最大亏损约 ${payoff['组合到期盈亏'].min():,.2f}。")
            except (BrokerError, ValueError, KeyError, IndexError) as exc:
                st.warning(f"到期盈亏图暂不可用：{exc}")
        st.subheader("滚仓与调整")
        new_expiry = st.date_input("滚动到新到期日", value=date(2026, 8, 21), key="combo_roll_expiry")
        if st.button("生成滚仓草稿", disabled=not admin or selected["status"] != "OPEN"):
            try:
                old_legs = store.option_combo_legs(selected_id)
                rolled = [{**leg, "symbol": roll_symbol(leg["symbol"], new_expiry)} for leg in old_legs]
                combo_id = create_combo(store, {
                    "strategy_id": selected["strategy_id"], "name": f"{selected['name']} 滚仓",
                    "underlying": selected["underlying"], "structure": selected["structure"],
                    "max_net_debit": selected["max_net_debit"], "min_net_credit": selected["min_net_credit"],
                    "assembly_timeout_seconds": selected["assembly_timeout_seconds"],
                    "naked_timeout_seconds": selected["naked_timeout_seconds"], "auto_rollback": True,
                    "parent_combo_id": selected_id, "requires_parent_closed": True,
                }, rolled)
                store.option_combo_event(selected_id, "ROLL_DRAFT_CREATED", "OPEN", f"已创建滚仓草稿 {combo_id}")
                st.success(f"滚仓草稿 {combo_id} 已创建。安全顺序是先平旧组合，再人工确认启动新草稿。")
            except ValueError as exc:
                st.error(str(exc))
        if selected["status"] == "OPEN" and not legs.empty:
            adjust_leg_id = st.selectbox(
                "需要调整的组合腿", legs["id"].tolist(),
                format_func=lambda value: str(legs.loc[legs["id"] == value, "symbol"].iloc[0]),
            )
            replacement = st.text_input("替换为新合约代码", key="combo_adjust_symbol").strip().upper()
            if st.button("生成单腿调整草稿", disabled=not admin or not replacement):
                try:
                    adjusted = store.option_combo_legs(selected_id)
                    for leg in adjusted:
                        if leg["id"] == adjust_leg_id:
                            leg["symbol"] = replacement
                    combo_id = create_combo(store, {
                        "strategy_id": selected["strategy_id"], "name": f"{selected['name']} 调整",
                        "underlying": selected["underlying"], "structure": "ADJUSTMENT",
                        "max_net_debit": selected["max_net_debit"], "min_net_credit": selected["min_net_credit"],
                        "assembly_timeout_seconds": selected["assembly_timeout_seconds"],
                        "naked_timeout_seconds": selected["naked_timeout_seconds"], "auto_rollback": True,
                        "parent_combo_id": selected_id, "requires_parent_closed": True,
                    }, adjusted)
                    st.success(f"调整草稿 {combo_id} 已创建；旧组合完整平仓前系统禁止启动它。")
                except ValueError as exc:
                    st.error(str(exc))

with candidate_tab:
    st.subheader("组合候选生成与排序")
    with st.form("combo_candidates_form", border=True):
        candidate_underlying = st.text_input("候选正股", value="US.NVDA").strip().upper()
        candidate_expiration = st.date_input("候选到期日", value=date(2026, 7, 24), key="candidate_expiry")
        generate_candidates = st.form_submit_button(
            "生成并排序候选", icon=":material/sort:"
        )
    if generate_candidates:
        try:
            chain = broker.option_chain(candidate_underlying, candidate_expiration.isoformat())
            quotes = broker.snapshots(chain["code"].astype(str).tolist())
            enriched = chain.drop(columns=[column for column in quotes.columns if column != "code" and column in chain], errors="ignore").merge(quotes, on="code", how="left")
            spot = float(broker.snapshots([candidate_underlying]).iloc[0]["last_price"])
            st.session_state.combo_candidate_results = rank_vertical_candidates(enriched, spot)
        except (BrokerError, ValueError, KeyError) as exc:
            st.error(f"候选生成失败：{exc}")
    candidates = st.session_state.get("combo_candidate_results", pd.DataFrame())
    if isinstance(candidates, pd.DataFrame) and not candidates.empty:
        st.dataframe(candidates, hide_index=True, column_config={
            "净贷记": st.column_config.NumberColumn(format="$%.2f"),
            "最大亏损": st.column_config.NumberColumn(format="$%.2f"),
            "风险收益比": st.column_config.NumberColumn(format="%.2f"),
            "最差腿价差": st.column_config.NumberColumn(format="percent"),
            "综合评分": st.column_config.NumberColumn(format="%.1f"),
        })
        st.caption("评分综合净贷记/最大亏损、离现价距离、最小未平仓量和最差腿价差；属于候选排序，不是收益承诺。")

with backtest_tab:
    combos_for_test = store.option_combos()
    if not combos_for_test:
        st.info("请先创建组合草稿。")
    else:
        with st.form("combo_backtest_form", border=True):
            backtest_combo = st.selectbox("回测组合", [row["combo_id"] for row in combos_for_test])
            initial_cash = st.number_input("初始资金", min_value=1000.0, value=100000.0, step=1000.0)
            take_profit = st.number_input("组合止盈倍数", min_value=0.0, value=0.5, step=0.1)
            stop_loss = st.number_input("组合止损倍数", min_value=0.0, value=0.5, step=0.1)
            run_backtest = st.form_submit_button("运行多腿历史回测", type="primary")
        if run_backtest:
            try:
                legs = store.option_combo_legs(backtest_combo)
                histories = {leg["symbol"]: pd.DataFrame(store.option_history(leg["symbol"])) for leg in legs}
                equity, trades, metrics = run_combo_backtest(
                    histories, legs, initial_cash, take_profit=take_profit or None, stop_loss=stop_loss or None,
                )
                with st.container(horizontal=True):
                    st.metric("总收益率", f"{metrics['总收益率']:.2%}", border=True)
                    st.metric("最大回撤", f"{metrics['最大回撤']:.2%}", border=True)
                    st.metric("手续费", f"${metrics['手续费']:.2f}", border=True)
                    st.metric("退出原因", metrics["退出原因"], border=True)
                st.line_chart(equity, x="时间", y="账户权益")
                st.dataframe(trades, hide_index=True)
            except ValueError as exc:
                st.error(f"组合回测失败：{exc}")

with quality_tab:
    quality = combo_execution_quality(store)
    if quality.empty:
        st.info("尚无组合执行数据。")
    else:
        st.dataframe(quality, hide_index=True, column_config={
            "计划净借记": st.column_config.NumberColumn(format="$%.2f"),
            "实际成交净借记": st.column_config.NumberColumn(format="$%.2f"),
            "组合不利滑点": st.column_config.NumberColumn(format="$%.2f"),
        })
        st.caption("组合不利滑点 = 实际成交净借记 − 计划净借记；正值表示成交成本变差。")

with drill_tab:
    st.caption("故障演练使用临时数据库和模拟券商，不会触碰当前模拟账户订单。")
    if st.button("运行组合故障演练", type="primary", icon=":material/emergency:"):
        try:
            st.dataframe(run_combo_resilience_drill(), hide_index=True)
        except Exception as exc:
            st.error(f"组合故障演练失败：{exc}")

with audit_tab:
    events = pd.DataFrame(store.option_combo_events())
    if events.empty:
        st.info("暂无组合执行事件。")
    else:
        events["status"] = events["status"].map(lambda value: status_labels.get(value, value))
        st.dataframe(
            chinese_columns(china_time_columns(events).drop(columns=["id"], errors="ignore")),
            hide_index=True,
        )

with guide_tab:
    st.markdown(
        """
        1. **净价先校验**：所有腿按限价计算净借记或净贷记，越界时一笔都不会提交。
        2. **保护腿先行**：同类型短腿必须由数量足够、到期日相同或更远的长腿形成有限风险结构；保护腿未成交前短腿不会提交。
        3. **平仓顺序反转**：先买回短腿，确认风险解除后才卖出长保护腿。
        4. **失败回滚**：装配超时或任一腿失败时，撤销未成交订单并反向处理已成交腿。
        5. **禁止裸卖**：当前不支持裸 Call、裸 Put，也不会因选择 CUSTOM 绕过保护关系校验。
        6. **模拟盘限定**：每一条腿仍通过统一执行器、全局急停、策略预算和富途 SIMULATE 锁。
        """
    )
