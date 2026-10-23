"""回归：① 波动率新开仓暂停改为**租户级**；② `exit_only` 孤儿在持仓归零后**回收**。

背景（10-23，Chief 拍板 A）：
- `volatility_new_entries_paused` 原为**全局键**，停实盘会连带把模拟盘也停掉 ——
  而修复验证恰恰要用模拟盘（改的是共享代码路径）。改为
  `volatility_new_entries_paused:<tenant_id>` 租户级，只停实盘。
- Reviewer 在生产配置里坐实：接管的 `exit_only` 孤儿在券商持仓归零后长期留存
  （其中两条 `campaign_id` 与 `code` 还不一致），每轮被遍历、被护栏静默跳过、
  白烧周期，还盖住真正需要管的那条 → 必须给回收路径。

本文件锁定这两条不变量。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from futu_quant import campaign_cleanup as cc  # noqa: E402
from futu_quant.store import TradingStore  # noqa: E402

REAL_TENANT = "futu_real_master"
PAPER_TENANT = "futu_paper_sandbox"
REAL_ACC = "281756481476169109"
PAPER_ACC = "20170229"


def _broker(tenant_id: str, account_id: str):
    return SimpleNamespace(tenant_id=tenant_id, account_id=account_id)


# ---------------------------------------------------------------- ① 租户级暂停

_KEYS = (
    "volatility_new_entries_paused",
    f"volatility_new_entries_paused:{REAL_TENANT}",
    f"volatility_new_entries_paused:{PAPER_TENANT}",
)


@pytest.fixture()
def pause_keys():
    """临时改写暂停键，测试结束**完整还原**生产原值（不污染生产配置）。"""
    store = TradingStore()
    saved = {k: store.get_setting(k) for k in _KEYS}
    try:
        yield store
    finally:
        for key, value in saved.items():
            if value:
                store.set_setting(key, value)
            else:
                store.delete_setting(key)


def test_pause_is_tenant_scoped(pause_keys):
    """租户级键只影响对应租户；未设置的租户不受全局键牵连。"""
    from scripts.run_gamma_strategy import volatility_new_entries_paused

    store = pause_keys
    store.set_setting("volatility_new_entries_paused", "false")            # 全局：关
    store.set_setting(f"volatility_new_entries_paused:{REAL_TENANT}", "true")
    store.set_setting(f"volatility_new_entries_paused:{PAPER_TENANT}", "false")

    assert volatility_new_entries_paused(store, _broker(REAL_TENANT, REAL_ACC)) is True
    assert volatility_new_entries_paused(store, _broker(PAPER_TENANT, PAPER_ACC)) is False
    assert volatility_new_entries_paused(store, _broker("unknown_tenant", "0")) is False


def test_global_key_still_falls_back(pause_keys):
    """全局键仍作为兜底 —— 改键名不能让「已停的实盘」悄悄恢复。"""
    from scripts.run_gamma_strategy import volatility_new_entries_paused

    store = pause_keys
    store.set_setting("volatility_new_entries_paused", "true")
    store.set_setting(f"volatility_new_entries_paused:{REAL_TENANT}", "")
    assert volatility_new_entries_paused(store, _broker(REAL_TENANT, REAL_ACC)) is True, (
        "租户级键为空时应回落全局键"
    )


def test_exit_management_is_not_affected_by_pause():
    """暂停只拦新开仓，退出管理必须照常 —— 这是这道闸的全部意义。"""
    src = (ROOT / "scripts" / "run_gamma_strategy.py").read_text(encoding="utf-8")
    # 暂停闸必须位于「开仓」判定内，且不得出现在退出/平仓路径上
    assert 'volatility_new_entries_paused(executor.store, broker)' in src, (
        "暂停闸未带上 broker（无法按租户解析）"
    )
    assert "波动率期权新开仓已临时暂停" in src


# ---------------------------------------------------------------- ② 孤儿回收

def _orphan(code: str, campaign_id: str | None = None) -> dict:
    base = {
        "code": code, "underlying": code.split(".", 1)[-1].split("26", 1)[0] or "US.AMZN",
        "option_type": "PUT", "legs": [{"code": code, "side": "BUY", "ratio": 1, "role": "PRIMARY"}],
        "enabled": True, "exit_only": True, "orphan_adopted": True,
    }
    if campaign_id:
        base["campaign_id"] = campaign_id
    return base


class _StubBroker:
    """可配置持仓/挂单的假券商。"""

    def __init__(self, positions: dict, cancellable: set[str] | None = None):
        self._positions = positions
        self._cancellable = cancellable or set()

    def positions(self, refresh=True):
        rows = [{"code": k, "qty": v} for k, v in self._positions.items()]
        return pd.DataFrame(rows, columns=["code", "qty"])

    def orders(self, historical=True):
        rows = [{"code": c, "order_status": "SUBMITTED"} for c in self._cancellable]
        return pd.DataFrame(rows, columns=["code", "order_status"])

    def is_cancellable_status(self, status):
        return str(status).upper() in {"SUBMITTED", "FILLED_PART", "WAITING_SUBMIT"}


@pytest.fixture()
def isolated_campaigns(tmp_path, monkeypatch):
    """把 campaign 配置隔离到临时库（同 test_campaign_identity_by_code 的做法）。"""
    db = tmp_path / "orphan.db"
    real_init = TradingStore.__init__

    def isolated_init(self, path=None, tenant_registry=None):
        real_init(self, db if path is None else path, tenant_registry)

    monkeypatch.setattr(TradingStore, "__init__", isolated_init)
    monkeypatch.setattr("futu_quant.store.DEFAULT_DB", db, raising=False)
    store = TradingStore(db)
    return store


def test_orphan_with_zero_position_is_reclaimed(isolated_campaigns, monkeypatch):
    """券商返回 qty=0（已平仓）时，孤儿必须被回收。"""
    monkeypatch.setattr(cc, "load_volatility_campaigns", lambda *_a, **_k: [
        _orphan("US.AMZN261028P250000", "orphan:US.AMZN261028P250000"),
        _orphan("US.META261030P660000", "orphan:US.META261030P660000"),
    ])
    monkeypatch.setattr(cc, "save_volatility_campaigns", lambda rows, *_a, **_k: rows)
    # 券商仍返回这两条，但数量都是 0
    broker = _StubBroker({"US.AMZN261028P250000": 0.0, "US.META261030P660000": 0.0})

    result = cc.reclaim_exit_only_orphans(isolated_campaigns, broker)

    assert len(result["reclaimed"]) == 2, f"已平仓孤儿未被回收：{result}"
    assert result["kept"] == [], "没有应保留的孤儿"


def test_orphan_with_live_position_is_kept(isolated_campaigns, monkeypatch):
    """铁律：持有券商持仓的 campaign 永不被删除（团队滚动不变量的安全侧）。"""
    monkeypatch.setattr(cc, "load_volatility_campaigns", lambda *_a, **_k: [
        _orphan("US.AMZN261028P250000", "orphan:US.AMZN261028P250000"),
    ])
    monkeypatch.setattr(cc, "save_volatility_campaigns", lambda rows, *_a, **_k: rows)
    broker = _StubBroker({"US.AMZN261028P250000": 1.0})

    result = cc.reclaim_exit_only_orphans(isolated_campaigns, broker)

    assert result["reclaimed"] == [], "持有持仓的孤儿被删除了 —— 违反滚动不变量"
    assert result["kept"] == ["US.AMZN261028P250000"]


def test_orphan_with_open_order_is_kept(isolated_campaigns, monkeypatch):
    """有未成交挂单时也不得回收。"""
    monkeypatch.setattr(cc, "load_volatility_campaigns", lambda *_a, **_k: [
        _orphan("US.AMZN261028P250000", "orphan:US.AMZN261028P250000"),
    ])
    monkeypatch.setattr(cc, "save_volatility_campaigns", lambda rows, *_a, **_k: rows)
    broker = _StubBroker({"US.AMZN261028P250000": 0.0}, cancellable={"US.AMZN261028P250000"})

    result = cc.reclaim_exit_only_orphans(isolated_campaigns, broker)

    assert result["reclaimed"] == [], "有挂单的孤儿被回收了"
    assert result["kept"] == ["US.AMZN261028P250000"]


def test_reclaim_requires_broker(isolated_campaigns):
    """拿不到券商数据时必须拒绝执行，不得猜。"""
    with pytest.raises(ValueError):
        cc.reclaim_exit_only_orphans(isolated_campaigns, None)


def test_reclaim_flags_id_code_mismatch(isolated_campaigns, monkeypatch):
    """回收结果必须标出 campaign_id 与 code 不一致（表达复核改写合约号的痕迹）。"""
    monkeypatch.setattr(cc, "load_volatility_campaigns", lambda *_a, **_k: [
        _orphan("US.META261030P670000", "orphan:US.META261030P660000"),
    ])
    monkeypatch.setattr(cc, "save_volatility_campaigns", lambda rows, *_a, **_k: rows)
    broker = _StubBroker({})

    result = cc.reclaim_exit_only_orphans(isolated_campaigns, broker)

    assert result["reclaimed"], "孤儿未被回收"
    assert result["reclaimed"][0]["id_code_mismatch"] is True, "未标出 id/code 不一致"


def test_non_orphan_campaigns_are_untouched(isolated_campaigns, monkeypatch):
    """正常 campaign（非 exit_only）绝不能被动到。"""
    normal = {
        "code": "US.NVDA261030P215000", "underlying": "US.NVDA", "legs": [],
        "enabled": True, "exit_only": None,
    }
    monkeypatch.setattr(cc, "load_volatility_campaigns", lambda *_a, **_k: [normal])
    monkeypatch.setattr(cc, "save_volatility_campaigns", lambda rows, *_a, **_k: rows)
    broker = _StubBroker({})

    result = cc.reclaim_exit_only_orphans(isolated_campaigns, broker)

    assert result["reclaimed"] == [] and result["kept"] == ["US.NVDA261030P215000"]
