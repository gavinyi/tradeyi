"""
飞书通知与事件人话化呈现引擎 (Feishu Humanization Engine)

将底层技术枚举代码转化为对交易员友好的人话结构：
- 标题：Emoji + 级别 + 业务直观中文
- 📌 业务事实：具体发生了什么
- ⚡ 资金与系统影响：对资金与交易流程的确定性影响
- 👉 交易员行动指引：下一步建议操作与直达链接
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class EventMeta:
    chinese_name: str
    impact: str
    action_guide: str
    page_path: str = ""


EVENT_METAS: dict[str, EventMeta] = {
    # 核心风控与安全门禁
    "DAILY_LOSS_KILL_SWITCH": EventMeta(
        chinese_name="日内亏损熔断急停",
        impact="今日账户浮动亏损触及日限额红线，系统已自动切断新增开仓；已有在仓头寸的止盈、止损和平仓完全正常运行。",
        action_guide="建议前往【风控中心】核验浮动盈亏，若需继续开仓可由管理员评估后临时放行。",
        page_path="pages/7_risk_center.py",
    ),
    "DAILY_LOSS_LIMIT_RESOLVE_FALLBACK": EventMeta(
        chinese_name="日亏线解析失败（已退回环境变量值）",
        impact="账户生效的风控参数不完整，系统已退回环境变量里的兜底日亏线继续巡检 —— 这条兜底线可能比页面配置的更宽松，等于当日急停被放远。",
        action_guide="请前往【风控中心】核对账户/租户的风控参数是否齐全（缺任意一项即拒绝按配置生效）。",
        page_path="pages/7_risk_center.py",
    ),
    "KILL_SWITCH_ENGAGED": EventMeta(
        chinese_name="紧急急停开关触发",
        impact="已开启全系统/租户交易急停制动，所有新增买入加仓被拦截；平仓通道保持畅通。",
        action_guide="请前往【风控中心】检查急停触发原因，排查完毕后手动解除急停。",
        page_path="pages/7_risk_center.py",
    ),
    "REAL_TRADE_REJECTED": EventMeta(
        chinese_name="实盘下单门禁拦截",
        impact="实盘委托触发保护规则（超单笔/单日限额、非白名单策略或确认码失效），该笔委托已被系统安全拦截，未扣减真实资金。",
        action_guide="请前往【实盘交易控制】核验实盘授权会话时效或调整单笔/单日限额。",
        page_path="pages/27_live_trading_control.py",
    ),
    "ORDER_ENV_MISMATCH": EventMeta(
        chinese_name="下单环境不匹配拦截",
        impact="订单目标环境与当前租户或券商网关环境不符，系统自动熔断该笔委托，杜绝串仓或误下单。",
        action_guide="请前往【交易配置】检查对应租户的交易环境配置（REAL vs SIMULATE）。",
        page_path="pages/1_trading_config.py",
    ),
    "LEDGER_MISMATCH": EventMeta(
        chinese_name="三账持仓对账异常",
        impact="券商持仓、本地订单流水与策略账本存在差异，系统已自动锁定受影响标的以防重复开仓。",
        action_guide="请前往【交易诊断中心】或【订单管理】核实券商最新成交状态进行手工平账。",
        page_path="pages/4_order_management.py",
    ),
    "RISK_REJECTED": EventMeta(
        chinese_name="风控限额拦截",
        impact="委托金额或持仓集中度超出风控上限，委托已被拒绝，未发生实际交易。",
        action_guide="请前往【风控中心】核查标的权重、行业敞口与单笔限额。",
        page_path="pages/7_risk_center.py",
    ),
    "HARD_STOP_LIMIT_INVALID": EventMeta(
        chinese_name="硬止损限额配置异常",
        impact="策略止损金额或止损比例配置非法，系统已阻断新开仓，防止头寸在无保护状态下裸奔。",
        action_guide="请前往【风控中心】补齐或修正该策略的双层止损配置参数。",
        page_path="pages/7_risk_center.py",
    ),
    "REAL_STOCK_SELL_UNAUTHORIZED": EventMeta(
        chinese_name="未经授权的实盘股票卖出",
        impact="系统检测到非法的现货卖出指令，已自动拦截，保护账户核心底仓安全。",
        action_guide="请核实是否有人工外部下单，或检查策略是否被授权操作该现货标的。",
        page_path="pages/7_risk_center.py",
    ),
    "REAL_PROTECTED_STOCK_SELL_FORBIDDEN": EventMeta(
        chinese_name="底仓保护标的禁止卖出",
        impact="该标的属于受保护的核心战略资产（如 NVDA/GLD 长期底仓），系统绝对禁止算法卖出。",
        action_guide="如确需调仓，请交易员在券商终端手动处理，或在底仓白名单中调整。",
        page_path="pages/15_strategy_control_center.py",
    ),
    "INVALID_EXECUTION_MODE_OVERRIDE": EventMeta(
        chinese_name="非法执行模式覆盖拦截",
        impact="检测到非法的执行模式覆盖值，系统采取 fail-closed 拒单，绝对不允许非法提升为实盘执行。",
        action_guide="请前往【策略与资金控制】检查策略执行模式配置。",
        page_path="pages/15_strategy_control_center.py",
    ),
    "STRATEGY_NEW_ENTRY_BLOCKED": EventMeta(
        chinese_name="单策略暂停开仓拦截",
        impact="该策略已开启【暂停新开仓 (只减仓)】模式，系统成功拦截买入开仓；在仓旧头寸的止盈、止损平仓管理照常全速运行。",
        action_guide="若行情平稳需恢复新开仓，可前往【策略与资金控制】勾选解除暂停。",
        page_path="pages/15_strategy_control_center.py",
    ),

    # 进程看门狗与高可用 (P1-13)
    "PROCESS_TERMINATED": EventMeta(
        chinese_name="核心服务进程异常退出",
        impact="后台关键进程（Worker/雷达/UI）已失联，对应租户的策略调度可能受到影响。",
        action_guide="请检查服务器后台运行窗口，桌面双击 start.bat 自动拉起与自愈服务。",
        page_path="pages/15_strategy_control_center.py",
    ),
    "PROCESS_HEARTBEAT_TIMEOUT": EventMeta(
        chinese_name="服务心跳超时（疑似僵死）",
        impact="服务进程 PID 虽在，但超过警戒时间未刷新心跳，调度可能陷入阻塞或网络卡死。",
        action_guide="建议桌面双击 stop.bat 清理挂死进程，再双击 start.bat 重启恢复。",
        page_path="pages/15_strategy_control_center.py",
    ),

    # 网关与对账
    "RECONCILIATION_MISMATCH": EventMeta(
        chinese_name="券商与本地流水对账不符",
        impact="每轮自动对账发现订单状态或数量差异，受影响标的已标记预警，禁止盲目加仓。",
        action_guide="请前往【订单管理】页面核对券商成交明细。",
        page_path="pages/4_order_management.py",
    ),
    "OPEND_DISCONNECTED": EventMeta(
        chinese_name="富途 OpenD 网关连接断开",
        impact="量化程序与本地 OpenD 网关网络通信中断，行情订阅与下单接口暂时不可用，系统已自动重试退避。",
        action_guide="请检查本地 Futu OpenD 是否在运行、是否被安全软件阻断或登录态过期。",
        page_path="pages/1_trading_config.py",
    ),
    "OPEND_UNAVAILABLE": EventMeta(
        chinese_name="富途 OpenD 网关暂不可用",
        impact="OpenD 处于未就绪或退避状态，所有依赖券商的任务已自动进入安全等待，不会盲目报错。",
        action_guide="通常在非交易时段维护或网关自愈中，无需人工干预，系统会自动恢复。",
        page_path="pages/1_trading_config.py",
    ),

    # 策略与执行
    "STRATEGY_ERROR": EventMeta(
        chinese_name="策略运行异常",
        impact="策略主循环执行某个子任务时发生错误，该轮循环已跳过并安全退避，避免连锁崩溃。",
        action_guide="请前往【交易诊断中心】查看详细错误堆栈与排查日志。",
        page_path="pages/26_trade_diagnostics_center.py",
    ),
    "STRATEGY_ERROR_RECOVERED": EventMeta(
        chinese_name="策略运行恢复正常",
        impact="此前发生异常的策略已成功自愈，新一轮调度与行情处理已全面恢复正常运行。",
        action_guide="无需人工操作，系统已自动重回正轨。",
        page_path="pages/11_strategy_activity_center.py",
    ),
    "REPRICE_FAILED": EventMeta(
        chinese_name="订单智能追价失败",
        impact="委托未在规定时间内成交，系统尝试自动追价时失败，已维持原限价单状态。",
        action_guide="请前往【订单管理】查看当前挂单价与买卖盘口深度。",
        page_path="pages/4_order_management.py",
    ),
    "CANCEL_FAILED": EventMeta(
        chinese_name="超时撤单失败",
        impact="挂单超时自动撤单请求被券商拒绝（可能已瞬间成交或网络延迟），正在重新同步订单状态。",
        action_guide="请前往【订单管理】确认该笔订单最终是已成交还是已撤单。",
        page_path="pages/4_order_management.py",
    ),
    "OPTION_LIFECYCLE_RISK": EventMeta(
        chinese_name="期权生命周期风险预警",
        impact="持仓期权临近最后到期日且处于价内/平值附近，若不及时处理可能发生被动行权或时间价值急剧归零。",
        action_guide="强烈建议前往【期权盈亏台账】手动提前平仓止盈锁定利润。",
        page_path="pages/28_options_pnl_ledger.py",
    ),
    "PORTFOLIO_STRESS_ALERT": EventMeta(
        chinese_name="投资组合压力告警",
        impact="当前期权或股票组合的希腊字母（Delta/Gamma/Vega）风险敞口触及预警阈值。",
        action_guide="请前往【组合风险】页面查看压力测试与希腊字母敞口分布。",
        page_path="pages/14_portfolio_risk.py",
    ),
    "STRESS_STATE_TENANT_FALLBACK": EventMeta(
        chinese_name="组合压力状态租户回落",
        impact="未注册的临时租户使用了全局风控状态，已记录预警流水，不影响主流程。",
        action_guide="如为新增租户，请前往系统配置注册独立租户配置。",
        page_path="pages/1_trading_config.py",
    ),

    # 机器学习 (P2)
    "ML_MODEL_STALE": EventMeta(
        chinese_name="ML 模型需重训（模型过旧）",
        impact="当前期权打分模型已有较长时间未重新校准，评分权重可能无法反映最新微观市场结构，已进入提醒周期。",
        action_guide="请前往【ML 模型状态与重训】页面查看模型表现或手动发起重训更新权重。",
        page_path="pages/42_ml_model_status.py",
    ),
    "ML_SCORING_DEGRADED": EventMeta(
        chinese_name="ML 评分降级（回退内置规则）",
        impact="由于缺少依赖或模型不可用，系统已无缝回退至内置纯 Python 动量规则打分；交易流程绝不中断，仅标的排序转为保守规则。",
        action_guide="若需使用高级随机森林评分，可联系管理员在后台虚拟环境补齐依赖。",
        page_path="pages/42_ml_model_status.py",
    ),
    "ML_RETRAIN_FAILED": EventMeta(
        chinese_name="ML 模型重训执行失败",
        impact="后台重训子进程发生错误，系统已自动保留上一版稳定模型继续使用，现有打分不受任何影响。",
        action_guide="请前往【ML 模型状态与重训】查看本次重训的失败日志与样本质量。",
        page_path="pages/42_ml_model_status.py",
    ),

    # 告警与探针
    "ALERT_DELIVERY_PROBE": EventMeta(
        chinese_name="告警通道心跳探针",
        impact="用于例行验证飞书机器人通信链路通畅（默认 6 小时一次），无任何系统故障或交易风险。",
        action_guide="此消息为自动化链路巡检，可直接忽略。",
        page_path="pages/1_trading_config.py",
    ),
    "ALERT_CHANNEL_NOT_CONFIGURED": EventMeta(
        chinese_name="告警通道未配置提醒",
        impact="系统检测到部分告警推送通道为空，可能无法在第一时间内接收到推送消息。",
        action_guide="请前往【交易配置】补齐飞书群 Webhook 或群聊 Chat ID。",
        page_path="pages/1_trading_config.py",
    ),
    "ALERT_DELIVERY_FAILED": EventMeta(
        chinese_name="告警消息投递失败",
        impact="尝试向飞书或 Webhook 发送卡片时网络超时或被拒绝，已落库本地 alerts 记录。",
        action_guide="请核查外部网络连通性或 Webhook Token 是否已过期。",
        page_path="pages/1_trading_config.py",
    ),
}


def humanize_alert(
    event_type: str,
    severity: str,
    message: str,
    is_recovered: bool = False,
    host: str = "localhost",
    port: int = 8501,
) -> tuple[str, list[str]]:
    """将事件与消息深度人话化组装为飞书卡片要素。

    返回: (status_title, lines)
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sev_upper = severity.upper()

    meta = EVENT_METAS.get(event_type)
    if not meta:
        if event_type.startswith("RECONCILIATION_"):
            suffix = event_type.replace("RECONCILIATION_", "").lower()
            meta = EventMeta(
                chinese_name=f"对账差异（{suffix}）",
                impact="自动对账发现订单或持仓存在细微差异，已记录流水并由系统持续校准。",
                action_guide="请前往【订单管理】或【对账历史】核对最新明细。",
                page_path="pages/4_order_management.py",
            )
        else:
            meta = EventMeta(
                chinese_name=event_type,
                impact="系统产生了一笔状态变更，已自动按安全策略妥善处置。",
                action_guide="可前往控制台查看日志了解详情。",
                page_path="pages/1_trading_config.py",
            )

    c_name = meta.chinese_name

    # 1. 标题与级别组装
    if is_recovered:
        status_title = f"🟢 【故障自愈】{c_name}已恢复正常"
        level_text = "🟢 **当前状态**: **已恢复正常 (NORMAL)**"
    elif sev_upper in {"CRITICAL", "ERROR"} or event_type == "LEDGER_MISMATCH":
        if sev_upper == "CRITICAL" or event_type == "LEDGER_MISMATCH":
            status_title = f"🚨 【紧急风控】{c_name}"
            level_text = "🔴 **告警级别**: **紧急处理 (CRITICAL)**"
        else:
            status_title = f"⚠️ 【系统异常】{c_name}"
            level_text = "🟠 **告警级别**: **系统错误 (ERROR)**"
    elif sev_upper == "WARNING":
        status_title = f"🟡 【风险预警】{c_name}"
        level_text = "🟡 **告警级别**: **注意预警 (WARNING)**"
    else:
        status_title = f"ℹ️ 【系统提醒】{c_name}"
        level_text = "🔵 **告警级别**: **日常通知 (INFO)**"

    # 2. 构造人话四要素
    lines = [
        level_text,
        f"🕒 **发生时间**: {now_str}",
        f"📌 **业务事实**: {message}",
        f"⚡ **资金与系统影响**: {meta.impact}",
        f"👉 **交易员指引**: {meta.action_guide}",
    ]

    # 3. 构造后台直达链接
    if meta.page_path:
        link_url = f"http://{host}:{port}"
        lines.append(f"🔗 **控制台直达**: [点击打开交易控制台]({link_url})")

    return status_title, lines
