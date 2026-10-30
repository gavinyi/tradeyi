"""日累计额度必须**按租户分桶** —— 验收守卫。

事故（2026-10-31）：`store.sum_notional_since()` 只有 `(时间, side)` 两个维度，
`order_intents` 里实盘与纸面混存，于是两个账户互相吃掉对方的当日开仓额度：

    实盘 BUY 2,218 + 纸面 BUY 2,304 = 4,522 合并计数

把租户级 `max_daily_notional` 设成 2,500 之后，**实盘下一单会被「模拟盘用掉的
额度」直接拒绝**，而纸面账户也会被实盘的用量顶掉。这类「一个数字管两个账户」
与 10-22 的跨账户急停是同一个病。

修复：
1. `store.sum_notional_since(..., tenant_id=...)` 增加第三维过滤；
2. `RiskEngine.check` 的两个额度桶（开仓 BUY / 减仓 SELL）都显式传租户 ——
   减仓桶走 `store.intents(tenant_id=...)`，因为它的「已用」是 `reduce_notional`
   对 intent 行做的聚合，不在 SQL 里。
3. 解析不出租户时退回全库合并：方向是**超计（更严）**，只会多拦不会放宽。

测试刻意用**股票**（multiplier=1）而不是期权：期权 BUY 在额度门之后还有
Greeks 可用性检查，会让断言看到一个与额度无关的拒绝原因。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from futu_quant.risk import RiskEngine, RiskLimits
from futu_quant.store import TradingStore

REAL_ACCOUNT = "281756481476169109"
PAPER_ACCOUNT = "20170229"
REAL_TENANT = "futu_real_master"
PAPER_TENANT = "futu_paper_sandbox"
SYMBOL = "US.NVDA"


class FakeBroker:
    """最小 broker：显式携带 ``tenant_id``，让 ``tenant_of_broker`` 直接命中注册表。"""

    def __init__(self, tenant_id: str, account_id: str, held: float = 0.0):
        self.tenant_id = tenant_id
        self.account_id = account_id
        self._held = held

    def orders(self, historical: bool = False) -> pd.DataFrame:
        return pd.DataFrame(columns=["order_id", "order_status", "code", "trd_side", "qty"])

    @staticmethod
    def is_cancellable_status(status) -> bool:
        return False

    def positions(self):
        if self._held <= 0:
            return pd.DataFrame(columns=["code", "qty", "market_val"])
        return pd.DataFrame([{"code": SYMBOL, "qty": self._held, "market_val": self._held}])

    def account_info(self):
        return pd.DataFrame([{"total_assets": 1_000_000, "power": 1_000_000, "unrealized_pl": 0}])


def _insert(store: TradingStore, tenant_id: str, *, side: str, quantity: float,
            price: float, account_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    metadata = f'{{"execution_mode":"REAL","account_id":"{account_id}","multiplier":1.0}}'
    with store.connection() as connection:
        connection.execute(
            """INSERT INTO order_intents
               (strategy_id,intent_key,symbol,side,quantity,limit_price,status,
                filled_quantity,average_fill_price,created_at,updated_at,metadata_json,tenant_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "volatility_long_gamma", f"test:{tenant_id}:{side}:{quantity}:{now}",
                SYMBOL, side, quantity, price, "FILLED",
                quantity, price, now, now, metadata, tenant_id,
            ),
        )


def _limits(**overrides) -> RiskLimits:
    base = {
        "max_order_notional": 1_000.0,
        "max_daily_notional": 1_000.0,
        "max_option_premium": 1_000.0,
        "max_daily_reduction_notional": 500_000.0,
        "max_reduction_order_notional": 150_000.0,
    }
    base.update(overrides)
    return RiskLimits(**base)


# --------------------------------------------------------------------------- #
# 原语层
# --------------------------------------------------------------------------- #

def test_sum_notional_since_filters_by_tenant(tmp_path):
    store = TradingStore(tmp_path / "sum.db")
    _insert(store, REAL_TENANT, side="BUY", quantity=500, price=1.0, account_id=REAL_ACCOUNT)
    _insert(store, PAPER_TENANT, side="BUY", quantity=900, price=1.0, account_id=PAPER_ACCOUNT)

    since = "2000-01-01T00:00:00+00:00"
    assert store.sum_notional_since(since, side="BUY", tenant_id=REAL_TENANT) == pytest.approx(500.0)
    assert store.sum_notional_since(since, side="BUY", tenant_id=PAPER_TENANT) == pytest.approx(900.0)
    # 不传租户 = 历史行为（全库合并），供非额度场景使用
    assert store.sum_notional_since(since, side="BUY") == pytest.approx(1_400.0)


# --------------------------------------------------------------------------- #
# 引擎层：真正会拦住下单的那条路径
# --------------------------------------------------------------------------- #

def test_other_tenant_usage_does_not_eat_this_tenant_quota(tmp_path):
    """纸面用掉 900 不得影响实盘 —— 修复前这一单会被「模拟盘的额度」拒掉。"""
    store = TradingStore(tmp_path / "cross.db")
    _insert(store, PAPER_TENANT, side="BUY", quantity=900, price=1.0, account_id=PAPER_ACCOUNT)

    engine = RiskEngine(store, _limits())
    decision = engine.check(
        FakeBroker(REAL_TENANT, REAL_ACCOUNT), SYMBOL, "BUY", 500, 1.0,
        strategy_id="volatility_long_gamma", execution_mode="REAL",
    )

    assert decision.allowed is True, f"被别的租户的用量拦住了：{decision.reason}"


def test_same_tenant_usage_still_blocks(tmp_path):
    """回归：自己用掉 900 之后，再下 500 必须被拦（保护不得因分桶而消失）。"""
    store = TradingStore(tmp_path / "self.db")
    _insert(store, REAL_TENANT, side="BUY", quantity=900, price=1.0, account_id=REAL_ACCOUNT)

    engine = RiskEngine(store, _limits())
    decision = engine.check(
        FakeBroker(REAL_TENANT, REAL_ACCOUNT), SYMBOL, "BUY", 500, 1.0,
        strategy_id="volatility_long_gamma", execution_mode="REAL",
    )

    assert decision.allowed is False
    assert "当日累计名义金额" in decision.reason


def test_reduction_bucket_is_tenant_scoped_too(tmp_path):
    """减仓桶同样是分租户的：别的租户卖出不得占用本租户的减仓额度。"""
    store = TradingStore(tmp_path / "reduce.db")
    _insert(store, PAPER_TENANT, side="SELL", quantity=300, price=20.0, account_id=PAPER_ACCOUNT)

    engine = RiskEngine(store, _limits(max_daily_reduction_notional=100.0))
    decision = engine.check(
        FakeBroker(REAL_TENANT, REAL_ACCOUNT, held=10.0), SYMBOL, "SELL", 1, 5.0,
        strategy_id="volatility_long_gamma", execution_mode="REAL",
    )

    assert decision.allowed is True, f"别的租户的卖出占掉了本租户减仓额度：{decision.reason}"
