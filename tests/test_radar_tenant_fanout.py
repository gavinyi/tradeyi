"""#2 雷达多租户扇出的行为守卫。

守三件事，全部落在**行为**上，不看源码字符串：

1. **两个启用租户各被扫描一次** —— 旧代码只用一个 broker 扫一次，
   另一租户永远发现不到机会（静默少发现，不报警）。
2. **单租户失败不拖垮整轮** —— 一个租户抛异常，其余租户照常产出，
   并记 `VOLATILITY_TENANT_SCAN_FAILED`。
3. **broker 按租户缓存** —— 握手昂贵，同一租户第二次扫描必须复用同一个实例。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from scripts import run_radar_service as radar  # noqa: E402


REAL = SimpleNamespace(
    tenant_id="futu_real_master", trading_env="REAL",
    account_id="281756481476169109", is_active=True,
)
PAPER = SimpleNamespace(
    tenant_id="futu_paper_sandbox", trading_env="SIMULATE",
    account_id="20170229", is_active=True,
)
INACTIVE = SimpleNamespace(
    tenant_id="moomoo_real", trading_env="REAL",
    account_id="283445327916889204", is_active=False,
)


class _Store:
    def __init__(self):
        self.audits = []
        self.heartbeats = []
        self.settings = {}

    def audit(self, event, payload, strategy_id=None):
        self.audits.append((event, payload))

    def heartbeat(self, sid, status, message, pid=None):
        self.heartbeats.append((sid, status, message))

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


@pytest.fixture
def fanout(monkeypatch):
    """把租户表、broker 缓存、两层扫描都换成可控替身。"""
    monkeypatch.setattr(radar, "_active_tenant_profiles", lambda: [REAL, PAPER])
    monkeypatch.setattr(radar, "_TENANT_BROKERS", {})
    built = []

    def fake_broker(profile):
        broker = radar._TENANT_BROKERS.get(profile.tenant_id)
        if broker is None:
            broker = SimpleNamespace(tenant_id=profile.tenant_id)
            radar._TENANT_BROKERS[profile.tenant_id] = broker
            built.append(profile.tenant_id)
        return broker

    monkeypatch.setattr(radar, "_tenant_broker", fake_broker)
    market_calls = []

    def fake_market(broker, store, symbols=None, progress=None):
        market_calls.append(broker.tenant_id)
        return SimpleNamespace(symbols=symbols or (), rows=[])

    monkeypatch.setattr(radar, "scan_market_layer", fake_market)
    return built, market_calls


def test_market_layer_runs_exactly_once(fanout, monkeypatch):
    """昂贵行情层**只跑一次** —— 每租户各跑一遍等于没拆层（#2 的核心约束）。"""
    built, market_calls = fanout
    monkeypatch.setattr(
        radar, "fan_out_to_tenant",
        lambda store, broker, profile, market, symbols: SimpleNamespace(rows=[]),
    )

    radar._scan_all_tenants(_Store(), ("US.NVDA",))

    assert len(market_calls) == 1, (
        f"scan_market_layer 应只调用 1 次，实际 {len(market_calls)} 次 "
        f"({market_calls}) —— 逐租户重跑会让 OpenD 调用量随租户数线性翻倍"
    )


def test_every_active_tenant_gets_scanned(fanout, monkeypatch):
    """两个启用租户各扇出一次；停用租户不扫。"""
    built, _ = fanout
    seen = []

    def fake_fanout(store, broker, profile, market, symbols):
        seen.append(profile.tenant_id)
        return SimpleNamespace(rows=[{"code": f"US.X.{profile.tenant_id}", "active": True}])

    monkeypatch.setattr(radar, "fan_out_to_tenant", fake_fanout)
    store = _Store()

    results, failed = radar._scan_all_tenants(store, ("US.NVDA",))

    assert seen == ["futu_real_master", "futu_paper_sandbox"], (
        f"启用租户必须各扇出一次，实际 {seen} —— 漏一个租户=它静默发现不到机会"
    )
    assert failed == []
    assert len(results) == 2, "两个租户的扫描结果都要收上来"


def test_one_tenant_failure_does_not_kill_the_round(fanout, monkeypatch):
    """一个租户抛异常，另一个租户必须照常产出，并留下审计。"""

    def fake_fanout(store, broker, profile, market, symbols):
        if profile.tenant_id == "futu_real_master":
            raise RuntimeError("OpenD 该账户连接失败")
        return SimpleNamespace(rows=[{"code": "US.QQQ261029C735000", "active": True}])

    monkeypatch.setattr(radar, "fan_out_to_tenant", fake_fanout)
    store = _Store()

    results, failed = radar._scan_all_tenants(store, ("US.NVDA",))

    assert failed == ["futu_real_master"]
    assert len(results) == 1, "另一租户的结果必须保留 —— 旧代码整轮都不产出"
    assert any(e == "VOLATILITY_TENANT_SCAN_FAILED" for e, _ in store.audits), (
        "租户级失败必须留审计，否则静默"
    )


def test_all_tenants_failing_raises_for_backoff(fanout, monkeypatch):
    """全部租户失败要抛错，让主循环计错误并退避 —— 不能静默当作成功。"""

    def fake_fanout(store, broker, profile, market, symbols):
        raise RuntimeError("OpenD 全挂")

    monkeypatch.setattr(radar, "fan_out_to_tenant", fake_fanout)

    with pytest.raises(RuntimeError, match="全部租户扫描失败"):
        radar._scan_all_tenants(_Store(), ("US.NVDA",))


def test_broker_is_cached_per_tenant(fanout, monkeypatch):
    """同一租户第二次扫描必须复用同一个 broker（握手昂贵）。"""
    built, _ = fanout
    monkeypatch.setattr(
        radar, "fan_out_to_tenant",
        lambda store, broker, profile, market, symbols: SimpleNamespace(rows=[]),
    )

    radar._scan_all_tenants(_Store(), ("US.NVDA",))
    radar._scan_all_tenants(_Store(), ("US.NVDA",))

    assert sorted(built) == ["futu_paper_sandbox", "futu_real_master"], (
        f"broker 应只构造一次/租户，实际构造了 {built}"
    )


# --------------------------------------------------------------------------- #
# 更细的三条（比"各扫一次"深一层）
# --------------------------------------------------------------------------- #

from futu_quant.store import TradingStore  # noqa: E402
from futu_quant.volatility_scanner import (  # noqa: E402
    MarketScan,
    MarketSymbolResult,
    TenantScanResult,
)


def _market(symbol: str = "US.NVDA", active: bool = True) -> MarketScan:
    """手工构造市场层产物 —— 让隔离守卫不依赖真实行情。"""
    regime = {
        "active": active, "reason": "测试信号", "checks": [],
        "intraday_return": 0.04, "zscore_20d": 3.0, "atr_ratio": 2.0,
        "volume_ratio": 2.0, "direction_score": 3.0,
    }
    profile = {"entry_zscore": 2.0, "minimum_underlying_move": 0.02}
    row = {"symbol": symbol, "active": active, "reason": "测试信号"}
    return MarketScan(
        symbols=(symbol,), market_move=0.0,
        results={symbol: MarketSymbolResult(
            symbol, {"last_price": 100.0}, None, profile, regime, 0.01, True, row,
        )},
        rows=[row],
    )


def _scan_broker(tenant_id: str):
    class _B:
        pass

    broker = _B()
    broker.tenant_id = tenant_id
    broker.account_id = "1"
    broker.trading_env = "SIMULATE"
    broker.positions = lambda: None
    broker.orders = lambda: None
    broker.is_cancellable_status = lambda _s: False
    return broker


def test_each_tenant_writes_only_its_own_campaign_key(monkeypatch, tmp_path):
    """扇出写入必须落在**各自租户键**上，且记录带本租户 tenant_id。

    这条比"两个租户各扫一次"深一层：各扫一次只证明**跑了**，
    不证明**写对了地方** —— 都写全局键的话，两个租户会互相顶掉，
    表现为"一个租户发现、另一个永远没有"（静默少发现）。
    """
    written: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        "futu_quant.volatility_scanner.upsert_campaign",
        lambda config_key, campaign, store=None: written.append((config_key, campaign)),
    )
    monkeypatch.setattr(
        "futu_quant.volatility_scanner.roll_campaign",
        lambda broker, seed, store: ({**seed, "code": "US.NVDA260101C100000"}, "测试滚动"),
    )
    monkeypatch.setattr(
        "futu_quant.volatility_scanner.resolve_volatility_radar_budget",
        lambda symbol: 1000.0,
    )

    for profile in (REAL, PAPER):
        outcome = radar.fan_out_to_tenant(
            TradingStore(tmp_path / f"{profile.tenant_id}.db"), _scan_broker(profile.tenant_id),
            profile, _market(), ("US.NVDA",),
        )
        assert outcome.errors == [], f"{profile.tenant_id} 扇出报错：{outcome.errors}"

    keys = [key for key, _ in written]
    assert f"VOLATILITY_CAMPAIGNS:{REAL.tenant_id}" in keys, f"实盘没写自己的键：{keys}"
    assert f"VOLATILITY_CAMPAIGNS:{PAPER.tenant_id}" in keys, f"模拟没写自己的键：{keys}"
    assert "VOLATILITY_CAMPAIGNS" not in keys, (
        f"扇出写到了全局键 —— 两个租户会互相顶掉：{keys}"
    )
    by_key = dict(written)
    assert by_key[f"VOLATILITY_CAMPAIGNS:{REAL.tenant_id}"]["tenant_id"] == REAL.tenant_id
    assert by_key[f"VOLATILITY_CAMPAIGNS:{PAPER.tenant_id}"]["tenant_id"] == PAPER.tenant_id


def test_tenant_a_campaigns_do_not_leak_into_tenant_b(monkeypatch, tmp_path):
    """租户 A 的清单里不得出现租户 B 的条目（发现源头隔离的端到端断言）。"""
    monkeypatch.setattr(
        "futu_quant.volatility_scanner.roll_campaign",
        lambda broker, seed, store: ({**seed, "code": "US.NVDA260101C100000"}, "ok"),
    )
    monkeypatch.setattr(
        "futu_quant.volatility_scanner.resolve_volatility_radar_budget",
        lambda symbol: 1000.0,
    )

    store = TradingStore(tmp_path / "iso.db")
    for profile in (REAL, PAPER):
        radar.fan_out_to_tenant(
            store, _scan_broker(profile.tenant_id), profile, _market(), ("US.NVDA",),
        )

    real_rows = store.campaigns(tenant_id=REAL.tenant_id)
    paper_rows = store.campaigns(tenant_id=PAPER.tenant_id)
    assert len(real_rows) == 1 and len(paper_rows) == 1, (
        f"两租户应各持一条：实盘 {len(real_rows)} / 模拟 {len(paper_rows)}"
    )
    assert all(row.get("tenant_id") == REAL.tenant_id for row in real_rows), "实盘清单混入别租户"
    assert all(row.get("tenant_id") == PAPER.tenant_id for row in paper_rows), "模拟清单混入别租户"


def test_failed_tenant_gets_its_own_backoff(fanout, monkeypatch, tmp_path):
    """失败租户要被**独立退避** —— 否则一个挂掉的账户让整轮每 5 分钟失败一次。"""
    def fake_fanout(store, broker, profile, market, symbols):
        raise RuntimeError(f"{profile.tenant_id} 挂了")

    monkeypatch.setattr(radar, "fan_out_to_tenant", fake_fanout)
    monkeypatch.setattr(radar, "_active_tenant_profiles", lambda: [REAL, PAPER])

    store = TradingStore(tmp_path / "backoff.db")
    # 两个都失败 → 全部失败会抛错（主循环据此退避）；断言退避键在抛错前已写
    with pytest.raises(RuntimeError, match="全部租户扫描失败"):
        radar._scan_all_tenants(store, ("US.NVDA",))

    assert store.get_setting(f"volatility_tenant_scan_backoff:{REAL.tenant_id}"), (
        "失败租户未写独立退避键 —— 下次整轮还会再撞它"
    )
    assert store.get_setting(f"volatility_tenant_scan_backoff:{PAPER.tenant_id}")


def test_backoff_key_skips_only_that_tenant(fanout, monkeypatch, tmp_path):
    """退避生效时只跳过**该租户**，另一个照常（这是"独立"两个字的全部意义）。

    实盘那条**故意写成 naive 时间**（`"2000-01-01T00:00:00"`）：
    这条比较在 per-tenant 的 try 之外，坏值会让整轮雷达崩 —— 所以它必须 fail-safe。
    本用例同时钉住两件事：坏值不崩 + 只有退避中的租户被跳过。
    """
    store = TradingStore(tmp_path / "skip.db")
    store.set_setting(
        f"volatility_tenant_scan_backoff:{REAL.tenant_id}",
        "2000-01-01T00:00:00",   # naive 且已过期 → 实盘照常扫
    )
    store.set_setting(
        f"volatility_tenant_scan_backoff:{PAPER.tenant_id}",
        "2999-01-01T00:00:00",   # 模拟租户在退避中 → 跳过
    )
    seen: list[str] = []
    monkeypatch.setattr(
        radar, "fan_out_to_tenant",
        lambda store, broker, profile, market, symbols: (
            seen.append(profile.tenant_id) or TenantScanResult(tenant_id=profile.tenant_id)
        ),
    )

    radar._scan_all_tenants(store, ("US.NVDA",))   # 不得抛异常

    assert seen == [REAL.tenant_id], (
        f"退避应只跳过模拟租户，实盘照常；实际被扫的是 {seen}"
    )


def test_legacy_entry_point_still_runs_market_layer_once(monkeypatch, tmp_path):
    """兼容入口 `scan_volatility_watchlist` 也必须只跑一次共享层。"""
    calls = {"market": 0}

    def counting(broker, store, symbols=None, progress=None):
        calls["market"] += 1
        return _market()

    monkeypatch.setattr("futu_quant.volatility_scanner.scan_market_layer", counting)
    monkeypatch.setattr(
        "futu_quant.volatility_scanner.roll_campaign",
        lambda broker, seed, store: ({**seed, "code": "US.NVDA260101C100000"}, "ok"),
    )
    monkeypatch.setattr(
        "futu_quant.volatility_scanner.resolve_volatility_radar_budget",
        lambda symbol: 1000.0,
    )

    from futu_quant.volatility_scanner import scan_volatility_watchlist

    rows = scan_volatility_watchlist(
        _scan_broker(REAL.tenant_id), TradingStore(tmp_path / "legacy.db"), ("US.NVDA",),
    )
    assert calls["market"] == 1, "兼容入口也必须只跑一次共享层"
    assert isinstance(rows, list)
