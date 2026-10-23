"""纸面账户「放宽但不失控」—— **B 方案**（告警线与硬停线是两个字段）的验收守卫。

最终口径（Chief 裁定 B 方案，Product 确认）：

| 字段 | 纸面值 | 语义 | 作用域 |
| :--- | :--- | :--- | :--- |
| `risk_limits:{tenant}.max_daily_loss` | $100,000 | **硬停线，拦单**（与实盘同一语义） | 全账户统一 |
| `paper_daily_loss_warn:{tenant}` | $25,000 | **告警线，只报警不拦单** | **仅纸面账户** |

B 方案的要点：`max_daily_loss` 的既有急停逻辑**一行不动**，只在它前面加一条
「纸面账户过告警线 → 发 WARNING → 继续放行」的旁路。**风控引擎里不得出现
"这个数字是告警还是硬停"的语义分叉**。

**为什么断言可观测行为、不断言实现字段名**：守卫只认「决策 + kill_switch 状态 + 告警行」，
这样换实现不会假红，而**语义**变化一定假红。

**为什么证伪场景取 -$30,000，而不是 -$12,000**：`_loss_guard` 只在
`daily_pl <= -max_daily_loss` 时才会被调用（`risk.py`）。亏损低于告警线时整段守卫
**根本不会被进入** —— 那种情况下断言"放行"是**恒真的空转**。所以 `-$12,000` 只用于
证明**不误报**（什么都不该发生），`-$30,000` 才用于证明**告警优先真的生效**。

相关实现：`src/futu_quant/risk.py`（三处急停触发点）、`src/futu_quant/alerts.py`
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from futu_quant.alerts import FEISHU_GROUP_ALERT_WHITELIST
from futu_quant.risk import RiskEngine, RiskLimits
from futu_quant.store import TradingStore
from futu_quant.tenant import tenant_of_broker

RISK = Path(__file__).resolve().parents[1] / "src" / "futu_quant" / "risk.py"

REAL_ACCOUNT = "281756481476169109"
PAPER_ACCOUNT = "20170229"
PAPER_TENANT = "futu_paper_sandbox"

#: 告警线（`paper_daily_loss_warn`，只报警）；硬停线（`max_daily_loss`，拦单，全账户统一语义）
ALERT_LINE = 25_000.0
HARD_STOP_LINE = 100_000.0
REAL_HARD_STOP = 10_000.0
#: 任何合理硬停线都该远低于它（= 90% 净值），用于证明硬停真的存在且会拦
FAR_BEYOND_HARD_STOP = -900_000.0


class _Broker:
    """最小 broker 替身：只带风控判定真正读取的字段。"""

    def __init__(self, account_id: str, trading_env: str, daily_pl: float,
                 assets: float = 1_000_000.0, power: float = 1_000_000.0):
        self.account_id = account_id
        self.trading_env = trading_env
        self._pl = daily_pl
        self._assets = assets
        self._power = power

    def orders(self, historical: bool = False) -> pd.DataFrame:
        return pd.DataFrame(columns=["order_id", "order_status", "code", "trd_side", "qty"])

    @staticmethod
    def is_cancellable_status(status) -> bool:
        return False

    def positions(self) -> pd.DataFrame:
        return pd.DataFrame(columns=["code", "qty", "market_val"])

    def account_info(self) -> pd.DataFrame:
        return pd.DataFrame([{
            "total_assets": self._assets, "power": self._power, "unrealized_pl": self._pl,
        }])


def _limits(hard_stop: float = HARD_STOP_LINE) -> RiskLimits:
    """注入限额 —— **必须与生产等价**，否则守卫测的是另一组数。

    B 方案下 `max_daily_loss` 就是**硬停线**（$100,000），告警线**不在这里** ——
    它由独立的 `paper_daily_loss_warn` 键承载（见 `_seed_warn_line`）。

    `max_drawdown_pct` 必须显式注入 **0.10**：`RiskLimits` 的默认是 0.05，
    而生产配置（`risk_limits:futu_paper_sandbox`）是 **0.1**。用默认值的守卫
    会拿 5% 当回撤线，于是"回撤 5.7% 不该告警"这条**假红**——测的是默认值不是生产值。
    """
    extra: dict[str, float] = {}
    for name in ("paper_daily_loss_warn", "max_daily_loss_warn"):
        if hasattr(RiskLimits, name):
            extra[name] = ALERT_LINE
    return RiskLimits(max_daily_loss=hard_stop, max_drawdown_pct=0.10, **extra)


def _seed_warn_line(store: TradingStore, broker) -> None:
    """把告警线写到所有合理的解析路径上（全局键 / 租户键 / 账户键）。

    实现读哪一条由它自己决定，守卫不该因为"解析顺序猜错"而假红。
    """
    tenant = tenant_of_broker(broker, strict=False)[0]
    keys = [None, tenant, getattr(broker, "account_id", None), PAPER_TENANT]
    for key in dict.fromkeys(str(c) for c in keys if c):
        store.set_setting(f"paper_daily_loss_warn:{key}", str(ALERT_LINE))
    store.set_setting("paper_daily_loss_warn", str(ALERT_LINE))


def _check(tmp_path, *, account: str, env: str, daily_pl: float,
           hard_stop: float = HARD_STOP_LINE):
    store = TradingStore(tmp_path / f"risk_{env}_{int(daily_pl)}.db")
    broker = _Broker(account, env, daily_pl)
    _seed_warn_line(store, broker)
    engine = RiskEngine(store, limits=_limits(hard_stop))
    with patch(
        "futu_quant.feishu_reporter.FeishuReporter.send_alert_card", return_value=True
    ):
        decision = engine.check(broker, "US.AAPL", "BUY", 1, 100.0)
    return store, decision


def _alert_rows(store, event_type: str) -> list[dict]:
    return [row for row in store.alerts() if row["event_type"] == event_type]


# --------------------------------------------------------------------------- #
# 1. 纸面账户：告警线**不**拦单（10-22 误杀的根因修复）
# --------------------------------------------------------------------------- #

def test_paper_account_warn_line_does_not_halt_the_strategy(tmp_path):
    """纸面当日亏 $12,000：**低于**告警线 $25,000 → 什么都不该发生（不误报对照）。

    「过告警线会告警」由下一条用例（-$30,000）负责，两条别混。
    """
    # 场景前置：$12,000 确实**超过**旧 $10,000 线（旧代码在这里就会 set_kill_switch）
    OLD_LINE = 10_000.0
    assert -12_000.0 <= -OLD_LINE, "前置不成立：亏损未超过旧急停线"
    assert -12_000.0 > -ALERT_LINE, "前置不成立：亏损已超过新告警线"

    store, decision = _check(
        tmp_path, account=PAPER_ACCOUNT, env="SIMULATE", daily_pl=-12_000.0,
    )
    assert decision.allowed, (
        f"纸面账户被拦下了（{decision.reason}）—— 10-22 误杀的根因仍在"
    )
    assert not store.kill_switch(account_id=PAPER_ACCOUNT), (
        "纸面账户的告警线不该写 kill_switch —— 写了就会掐死验证（旧线 $10,000 的形态）"
    )
    assert not _alert_rows(store, "PAPER_DAILY_LOSS_WARN"), (
        "未过告警线却发了告警 —— 告警线不是「任何亏损都报」"
    )


def test_paper_account_warns_between_warn_and_hard_stop(tmp_path):
    """过**告警线**、未过**硬停线**：必须放行 + 发 WARNING + 不写急停。

    这条才是「告警线真的会告警」的证据；上一条只证明它不会误杀。

    **取 $26,000 这个数不是随便定的** —— 它是"只改数值不改语义"的半状态会露馅的那个点：
    旧全局线 $10,000 已被新告警线 $25,000 推远，$12,000 两种实现都不会杀；
    只有 $26,000（过新告警线、远低于硬停线 $100,000）能区分
    「语义已改」与「只是阈值被推远」——前者只告警，后者照旧急停。
    """
    store, decision = _check(
        tmp_path, account=PAPER_ACCOUNT, env="SIMULATE", daily_pl=-26_000.0,
    )
    assert decision.allowed, f"过告警线被拦单（{decision.reason}）—— 告警优先被实现成了照旧急停"
    assert not store.kill_switch(account_id=PAPER_ACCOUNT), "过告警线不该写 kill_switch"
    warns = _alert_rows(store, "PAPER_DAILY_LOSS_WARN")
    assert warns, "纸面告警线必须发 WARNING，否则'放宽'等于静默"
    assert warns[0]["severity"] == "WARNING"


# --------------------------------------------------------------------------- #
# 2. 纸面账户：硬停线仍然拦单（放宽但不失控）
# --------------------------------------------------------------------------- #

def test_paper_account_hard_stop_line_still_halts(tmp_path):
    """纸面当日亏 $900,000（远超硬停线 $100,000）：必须真的拦下并发 ERROR 告警。"""
    store, decision = _check(
        tmp_path, account=PAPER_ACCOUNT, env="SIMULATE", daily_pl=FAR_BEYOND_HARD_STOP,
    )
    assert not decision.allowed, (
        "纸面账户亏到 90% 净值仍被放行 —— 没有硬停就是'完全失控'"
    )
    assert store.kill_switch(account_id=PAPER_ACCOUNT), (
        "纸面硬停线必须写 kill_switch，否则拦单只是一句空话"
    )
    stops = _alert_rows(store, "PAPER_DAILY_LOSS_HARD_STOP")
    assert stops, "纸面硬停必须发 ERROR 级告警（策略可能已坏，必须让人看到）"
    assert stops[0]["severity"] == "ERROR"


def test_paper_hard_stop_alert_carries_account_and_loss(tmp_path):
    """硬停告警必须**带账户与当日亏损金额**（Product 10-23 要求）。

    只发一句"已急停"没用 —— 看的人还得去翻日志才知道亏了多少、哪个账户。
    """
    store, _decision = _check(
        tmp_path, account=PAPER_ACCOUNT, env="SIMULATE", daily_pl=-200_000.0,
    )
    stops = _alert_rows(store, "PAPER_DAILY_LOSS_HARD_STOP")
    assert stops, "未触发硬停告警"
    message = str(stops[0]["message"])
    assert PAPER_ACCOUNT in message, f"告警未带账户：{message}"
    assert "200,000" in message, f"告警未带当日亏损金额：{message}"


def test_paper_warn_carries_amount_in_operational_event(tmp_path):
    """告警文案要可去重（不含浮动金额），所以金额必须落在**运行事件**里可查。"""
    store, _decision = _check(
        tmp_path, account=PAPER_ACCOUNT, env="SIMULATE", daily_pl=-26_000.0,
    )
    events = [
        row for row in store.operational_events(200)
        if row["event_type"] == "PAPER_DAILY_LOSS_WARN"
    ]
    assert events, "告警线未留运行事件 —— 金额就没有可查的落点了"
    payload = json.loads(events[0]["payload_json"])
    # 载荷是**量纲感知**的：`metric` 说明这条告警是哪个分支（当日亏损 / 日内权益损失 / 日内回撤），
    # `metric_value` 是该分支自己的数值（回撤是比例），`metric_amount_usd` 是折算后的美元额。
    assert payload.get("metric") == "当日亏损", f"告警分支标错：{payload}"
    assert float(payload["metric_amount_usd"]) == 26_000.0, (
        f"运行事件未记录当日亏损金额：{payload}"
    )


# --------------------------------------------------------------------------- #
# 3. 实盘账户：行为一字不改
# --------------------------------------------------------------------------- #

def test_real_account_behaviour_is_unchanged(tmp_path):
    """实盘当日亏过 `max_daily_loss`：仍必须急停 + 拒绝开仓（证明真金路径没被动过）。"""
    store, decision = _check(
        tmp_path, account=REAL_ACCOUNT, env="REAL", daily_pl=-12_000.0, hard_stop=REAL_HARD_STOP,
    )
    assert not decision.allowed, "实盘过线仍必须拒绝开仓"
    assert store.kill_switch(account_id=REAL_ACCOUNT), "实盘过线仍必须写 kill_switch"
    assert _alert_rows(store, "DAILY_LOSS_KILL_SWITCH"), "实盘急停必须发 DAILY_LOSS_KILL_SWITCH"
    assert not _alert_rows(store, "PAPER_DAILY_LOSS_WARN"), (
        "实盘账户收到了纸面专用告警 —— 告警线被错误地全局化了"
    )


# --------------------------------------------------------------------------- #
# 4. 两条新告警必须真的推得出去（白名单是必要条件）
# --------------------------------------------------------------------------- #

def test_new_paper_alert_events_are_whitelisted():
    """不在白名单的 ERROR 事件只落库不推送 —— 必须进白名单，否则又是静默。"""
    for event in ("PAPER_DAILY_LOSS_WARN", "PAPER_DAILY_LOSS_HARD_STOP"):
        assert event in FEISHU_GROUP_ALERT_WHITELIST, (
            f"{event} 不在飞书白名单 —— 事件会'库里有、delivered=false、用户看不到'"
        )


# --------------------------------------------------------------------------- #
# 5. 不得新写一套租户判定（一个概念一份实现）
# --------------------------------------------------------------------------- #

def test_paper_account_detection_reuses_the_shared_scope():
    """租户性质判定必须复用 `broker_scope`，不得在风控里另读一份环境变量。"""
    src = RISK.read_text(encoding="utf-8")
    assert "broker_scope(broker)" in src, (
        "risk.py 未使用共享入口 broker_scope —— 租户判定会与别处分叉"
    )
    if "def _paper_account" in src:
        body = src.split("def _paper_account", 1)[1].split("\ndef ", 1)[0]
        assert "broker_scope" in body, (
            "_paper_account 没有走 broker_scope —— 这是第二套租户实现，"
            "今晚已经栽过一次「抽了共享函数却有一条路径漏调」"
        )


# --------------------------------------------------------------------------- #
# 6. B 方案的语义守卫：告警线与硬停线必须是**两个字段**，不得合一
# --------------------------------------------------------------------------- #

def test_warn_line_is_a_separate_field_from_the_hard_stop():
    """风控引擎不得用 `max_daily_loss` 兼任纸面告警线 —— 那是语义分叉（B 方案明令禁止）。"""
    src = RISK.read_text(encoding="utf-8")
    assert "paper_daily_loss_warn" in src, (
        "risk.py 没有读取独立的告警线字段 —— 纸面告警线要么不存在，要么被塞进了 "
        "max_daily_loss（同一个数字两种语义，B 方案明令禁止）"
    )


# --------------------------------------------------------------------------- #
# 7. Product 10-23：告警必须**带上下文**，否则等于没发
# --------------------------------------------------------------------------- #

def test_paper_warn_alert_carries_context(tmp_path):
    """`PAPER_DAILY_LOSS_WARN` 的消息里要有**账户**与**触发线**。

    金额**也在**文案里（Product 10-23 明确要求：告警必须带当日亏损，否则等于没发）。
    代价是 900s 精确去重不再跨金额生效，但本告警只在 **BUY 下单检查**时触发、
    不是定时任务，量级有界；精确金额同时落在运行事件里（下一条用例）。
    """
    store, _decision = _check(
        tmp_path, account=PAPER_ACCOUNT, env="SIMULATE", daily_pl=-30_000.0,
    )
    warns = _alert_rows(store, "PAPER_DAILY_LOSS_WARN")
    assert warns, "未触发纸面告警"
    message = str(warns[0]["message"])
    assert PAPER_ACCOUNT in message, f"告警未带账户：{message}"
    assert f"{ALERT_LINE:,.0f}" in message, f"告警未带触发线：{message}"
    assert "30,000" in message, f"告警未带当日亏损金额：{message}"


def test_paper_account_just_over_warn_line_warns_and_continues(tmp_path):
    """**$25,001**：过告警线 $25,000 一分钱、远低于硬停线 $100,000。

    这是**唯一能证明"告警线真的接上了"**的用例 ——
    只改数值不改语义（阈值从 $10,000 推到 $25,000）的实现会在这里露馅：
    它会照旧急停，而不是"发 WARN 然后继续"。

    前置：$25,001 > 告警线 $25,000（只过一分）且 < 硬停线 $100,000（差 4 倍）。
    """
    assert -25_001.0 <= -ALERT_LINE, "前置不成立：未超过告警线"
    assert -25_001.0 > -HARD_STOP_LINE, "前置不成立：已超过硬停线"

    store, decision = _check(
        tmp_path, account=PAPER_ACCOUNT, env="SIMULATE", daily_pl=-25_001.0,
    )
    assert decision.allowed, (
        f"过告警线 $25,001 被拦单（{decision.reason}）—— 告警线没接上，"
        "实现退化成了「照旧急停」"
    )
    assert not store.kill_switch(account_id=PAPER_ACCOUNT), (
        "过告警线不该写 kill_switch —— 写了就等于没有告警线"
    )
    warns = _alert_rows(store, "PAPER_DAILY_LOSS_WARN")
    assert warns, "过告警线必须有 WARN，否则'提前告警'这个功能不存在"
    assert warns[0]["severity"] == "WARNING"
    assert not _alert_rows(store, "PAPER_DAILY_LOSS_HARD_STOP"), (
        "远低于硬停线却发了硬停告警 —— 两条线被合并了"
    )


# --------------------------------------------------------------------------- #
# 8. 回撤带（与告警带同构，但**各用各的阈值**）
# --------------------------------------------------------------------------- #

def _check_with_peak(tmp_path, *, account: str, env: str, assets: float, peak: float):
    """构造一次"当前权益低于日内峰值"的判定（即日内回撤）。

    回撤判据在 `risk.py`：``drawdown = 1 - assets / peak``，
    其中 ``peak`` 取当日 equity 快照与当前权益的较大者。
    """
    store = TradingStore(tmp_path / f"risk_dd_{int(peak)}.db")
    broker = _Broker(account, env, 0.0, assets=assets)
    _seed_warn_line(store, broker)
    store.record_equity(peak, cash=assets, market_value=0.0)
    engine = RiskEngine(store, limits=_limits(HARD_STOP_LINE))
    with patch(
        "futu_quant.feishu_reporter.FeishuReporter.send_alert_card", return_value=True
    ):
        decision = engine.check(broker, "US.AAPL", "BUY", 1, 100.0)
    return store, decision, 1 - assets / peak


def test_paper_drawdown_below_limit_is_quiet(tmp_path):
    """纸面回撤 **5.7%**（< 10%）→ **不该有任何告警**。

    这条先红：当前实现把"回撤告警"**无条件**挂在纸面分支上（不判阈值），
    所以回撤 3%~5% 也会误报 —— 误报比不报更糟（会让人忽略真告警）。
    """
    store, decision, drawdown = _check_with_peak(
        tmp_path, account=PAPER_ACCOUNT, env="SIMULATE", assets=500_000.0, peak=530_000.0,
    )
    assert 0.0 < drawdown < 0.10, f"前置不成立：回撤 {drawdown:.2%} 不在 (0, 10%) 区间"
    assert decision.allowed, f"回撤 {drawdown:.2%} 被拦单（{decision.reason}）"
    assert not store.kill_switch(account_id=PAPER_ACCOUNT)
    assert not _alert_rows(store, "PAPER_DAILY_LOSS_WARN"), (
        f"回撤 {drawdown:.2%} 未过 10% 线却发了告警 —— 回撤分支没判阈值，会误报"
    )
    assert not _alert_rows(store, "PAPER_DAILY_LOSS_HARD_STOP")


def test_paper_drawdown_over_limit_warns_not_halts(tmp_path):
    """纸面回撤 **12%**（≥ 10%、回撤额 $60,000 < 硬停线 $100,000）→ WARN + 放行。

    注意用**小权益**（$500,000）才造得出"过回撤线但未过硬停线"的区间：
    若权益 $1,000,000，10% 回撤正好等于 $100,000 硬停线，两条线重合、区间为空。
    """
    store, decision, drawdown = _check_with_peak(
        tmp_path, account=PAPER_ACCOUNT, env="SIMULATE", assets=500_000.0, peak=568_182.0,
    )
    assert 0.10 <= drawdown < 0.20, f"前置不成立：回撤 {drawdown:.2%} 不在 [10%, 20%) 区间"
    assert decision.allowed, f"回撤 {drawdown:.2%} 被拦单（{decision.reason}）"
    assert not store.kill_switch(account_id=PAPER_ACCOUNT), "过回撤线不该写 kill_switch"
    warns = _alert_rows(store, "PAPER_DAILY_LOSS_WARN")
    assert warns, f"回撤 {drawdown:.2%} 已过 10% 线，必须告警"
    assert warns[0]["severity"] == "WARNING"
    assert not _alert_rows(store, "PAPER_DAILY_LOSS_HARD_STOP"), (
        "回撤额未达硬停线却发了硬停告警 —— 两条线被合并了"
    )


# --------------------------------------------------------------------------- #
# 9. 租户键解析（防止 `_seed_warn_line` 双写掩盖"只认租户键"这条真路径）
# --------------------------------------------------------------------------- #

def test_paper_warn_line_resolves_from_tenant_key_alone(tmp_path):
    """**只写租户键**、不写全局键 → 告警带仍必须生效。

    为什么单开这一条：`_seed_warn_line` 为了"不因解析顺序猜错而假红"，
    把全局键和租户键都写了。但那会**掩盖**"租户键解析"这条真路径 ——
    如果实现只读全局键、从不读租户键，前几条用例照样全绿。

    所以这条只写 `paper_daily_loss_warn:{tenant}`，**并把 broker 显式注册成租户**
    （否则 `tenant_of_broker` 返回 None，回落读无前缀全局键，测的是假路径）。
    """
    store = TradingStore(tmp_path / "risk_tenant_key.db")
    broker = _Broker(PAPER_ACCOUNT, "SIMULATE", -30_000.0)
    broker.tenant_id = PAPER_TENANT          # 显式注册，保证租户键可解析
    store.set_setting(f"paper_daily_loss_warn:{PAPER_TENANT}", str(ALERT_LINE))
    assert store.get_setting("paper_daily_loss_warn", "") == "", (
        "这条用例必须只写租户键 —— 全局键有值就失去意义了"
    )
    engine = RiskEngine(store, limits=_limits(HARD_STOP_LINE))
    with patch(
        "futu_quant.feishu_reporter.FeishuReporter.send_alert_card", return_value=True
    ):
        decision = engine.check(broker, "US.AAPL", "BUY", 1, 100.0)
    assert decision.allowed, f"只写租户键时被拦单（{decision.reason}）—— 租户键解析没生效"
    assert not store.kill_switch(account_id=PAPER_ACCOUNT)
    assert _alert_rows(store, "PAPER_DAILY_LOSS_WARN"), (
        "只写租户键时没有告警 —— 实现没有真的按租户读告警线"
    )
