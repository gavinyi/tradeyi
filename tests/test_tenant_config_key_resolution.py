# -*- coding: utf-8 -*-
"""``tenant.resolve_tenant_config_key`` 共享解析入口的守卫测试。

背景：``VOLATILITY_CAMPAIGNS`` / 策略暂停开关 / 风险参数都曾是**全局键**，
两个租户共用一份 → 一次误判就是跨租户连坐（2026-10-22 夜实盘成交串进模拟盘、
模拟账户撞全局 $10,000 急停线被误停）。

本文件锁三件事：

1. **拼键只有一处**，且缺租户/租户未注册一律**抛异常**，绝不静默回落无前缀全局键；
2. **读侧规则**固定为「租户键优先、无前缀旧键兜底」，且兜底必须显式可见；
3. 两个迁移脚本的**归属判据**按 ①②③④ 优先级走，④ 兜底必须带 WARN。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import sys
from unittest.mock import MagicMock

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import migrate_volatility_campaigns_tenant_scope as mig  # noqa: E402

from futu_quant import tenant as tenant_mod  # noqa: E402,F401
from futu_quant.tenant import (  # noqa: E402
    RISK_LIMITS_KEY,
    VOLATILITY_CAMPAIGNS_KEY,
    DEFAULT_TENANTS,
    TenantConfigKeyError,
    TenantProfile,
    campaigns_config_key,
    legacy_config_key,
    real_account_config_key,
    resolve_tenant_config_key,
    risk_limits_config_key,
    tenant_config_key_candidates,
    tenant_id_of_broker,
    tenant_of_broker,
)

REAL = DEFAULT_TENANTS["futu_real_master"]
PAPER = DEFAULT_TENANTS["futu_paper_sandbox"]
REAL_ACCOUNT = REAL.account_id
PAPER_ACCOUNT = PAPER.account_id


# --------------------------------------------------------------------------
# 1. 拼键唯一入口
# --------------------------------------------------------------------------


def test_resolve_builds_scoped_key_for_both_tenants():
    assert resolve_tenant_config_key("futu_real_master", "VOLATILITY_CAMPAIGNS") == (
        "VOLATILITY_CAMPAIGNS:futu_real_master"
    )
    assert resolve_tenant_config_key(PAPER.tenant_id, "strategy_enabled") == (
        "strategy_enabled:futu_paper_sandbox"
    )


def test_resolve_accepts_tenant_profile_object():
    assert resolve_tenant_config_key(REAL, "strategy_enabled") == (
        f"strategy_enabled:{REAL.tenant_id}"
    )


def test_resolve_rejects_missing_tenant():
    for bad in (None, "", "   "):
        with pytest.raises(TenantConfigKeyError):
            resolve_tenant_config_key(bad, "VOLATILITY_CAMPAIGNS")


def test_resolve_rejects_unregistered_tenant():
    """未注册租户必须 fail-closed —— 静默写全局键就是跨租户连坐。"""
    with pytest.raises(TenantConfigKeyError) as exc:
        resolve_tenant_config_key("futu_unknown_tenant", "VOLATILITY_CAMPAIGNS")
    assert "不在注册表" in str(exc.value)


def test_resolve_rejects_missing_base_key():
    for bad in (None, "", "   "):
        with pytest.raises(TenantConfigKeyError):
            resolve_tenant_config_key(REAL.tenant_id, bad)


def test_resolve_rejects_double_scoping():
    """``X:{t}`` 再传一次不能变成 ``X:{t}:{t}``。"""
    with pytest.raises(TenantConfigKeyError) as exc:
        resolve_tenant_config_key(REAL.tenant_id, f"strategy_enabled:{REAL.tenant_id}")
    assert "重复加前缀" in str(exc.value)


def test_resolve_never_returns_unscoped_key():
    """核心不变量：解析失败时**不得**返回无前缀键。"""
    for tenant_id in (None, "", "nope"):
        with pytest.raises(TenantConfigKeyError):
            resolve_tenant_config_key(tenant_id, "VOLATILITY_CAMPAIGNS")


def test_extra_ids_allows_account_scope():
    """账户级键（``real_account_config:{account_id}``）走同一把 fail-closed 校验。"""
    assert resolve_tenant_config_key(
        REAL_ACCOUNT, "real_account_config", extra_ids=(REAL_ACCOUNT,)
    ) == f"real_account_config:{REAL_ACCOUNT}"
    with pytest.raises(TenantConfigKeyError):
        resolve_tenant_config_key("999999999", "real_account_config", extra_ids=(REAL_ACCOUNT,))


def test_campaigns_and_risk_key_helpers_are_scoped():
    assert campaigns_config_key(REAL.tenant_id) == f"{VOLATILITY_CAMPAIGNS_KEY}:{REAL.tenant_id}"
    assert risk_limits_config_key(PAPER.tenant_id) == f"{RISK_LIMITS_KEY}:{PAPER.tenant_id}"
    assert real_account_config_key(REAL_ACCOUNT) == f"real_account_config:{REAL_ACCOUNT}"
    with pytest.raises(TenantConfigKeyError):
        campaigns_config_key("")


def test_real_account_config_key_rejects_missing_account():
    for bad in (None, "", "   ", True):
        with pytest.raises(TenantConfigKeyError):
            real_account_config_key(bad)


# --------------------------------------------------------------------------
# 2. 读侧规则：租户键优先、无前缀旧键兜底
# --------------------------------------------------------------------------


def test_candidates_put_tenant_key_first():
    tenant_key, legacy = tenant_config_key_candidates(REAL.tenant_id, "VOLATILITY_CAMPAIGNS")
    assert tenant_key == f"VOLATILITY_CAMPAIGNS:{REAL.tenant_id}"
    assert legacy == "VOLATILITY_CAMPAIGNS"
    assert legacy_config_key("VOLATILITY_CAMPAIGNS") == "VOLATILITY_CAMPAIGNS"


def test_candidates_expose_fallback_explicitly():
    """无租户上下文时返回 ``None`` —— 调用方必须显式记录"回落旧键"，不得无声继续。"""
    tenant_key, legacy = tenant_config_key_candidates(None, "VOLATILITY_CAMPAIGNS")
    assert tenant_key is None
    assert legacy == "VOLATILITY_CAMPAIGNS"

    tenant_key, _ = tenant_config_key_candidates("unregistered", "VOLATILITY_CAMPAIGNS")
    assert tenant_key is None


def test_candidates_cover_all_registered_tenants():
    for profile in DEFAULT_TENANTS.values():
        tenant_key, legacy = tenant_config_key_candidates(profile.tenant_id, "strategy_enabled")
        assert tenant_key == f"strategy_enabled:{profile.tenant_id}"
        assert legacy == "strategy_enabled"


# --------------------------------------------------------------------------
# 3. 迁移脚本的归属判据（①②③④）
# --------------------------------------------------------------------------


@pytest.fixture()
def evidence_db(tmp_path):
    """一个只含 ``broker_snapshots`` + ``order_intents`` 的最小证据库。"""
    path = tmp_path / "evidence.db"
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE broker_snapshots (
            snapshot_key TEXT PRIMARY KEY, payload_json TEXT, captured_at TEXT
        );
        CREATE TABLE order_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, tenant_id TEXT, created_at TEXT,
            side TEXT, status TEXT, filled_quantity REAL
        );
        CREATE TABLE runtime_config (
            config_key TEXT PRIMARY KEY, value_json TEXT, version INTEGER, updated_at TEXT
        );
        CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, strategy_id TEXT,
            payload_json TEXT, created_at TEXT
        );
        """
    )
    con.commit()
    con.close()
    return path


def _snapshot(con, key, codes, captured_at="2026-10-22T22:28:03+00:00"):
    con.execute(
        "INSERT INTO broker_snapshots(snapshot_key,payload_json,captured_at) VALUES(?,?,?)",
        (key, json.dumps([{"code": c} for c in codes]), captured_at),
    )
    con.commit()


def _intent(con, code, tenant_id, created_at):
    con.execute(
        "INSERT INTO order_intents(symbol,tenant_id,created_at) VALUES(?,?,?)",
        (code, tenant_id, created_at),
    )
    con.commit()


def _filled(con, code, tenant_id, side, qty, status="FILLED"):
    """一条**已成交**的意图（净持仓推算只认 FILLED）。"""
    con.execute(
        "INSERT INTO order_intents(symbol,tenant_id,side,status,filled_quantity,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (code, tenant_id, side, status, float(qty), "2026-10-22T00:00:00+00:00"),
    )
    con.commit()


def _build(con, campaigns, primary_tenant, tenants=None, positions=None):
    return mig.build_attribution(
        con, campaigns, tenants or list(DEFAULT_TENANTS.values()),
        primary_tenant=primary_tenant, positions=positions,
    )


def test_rule1_campaign_carries_tenant_id(evidence_db):
    con = sqlite3.connect(str(evidence_db))
    campaigns = [{"code": "US.A", "tenant_id": PAPER.tenant_id, "enabled": True}]
    attributions, _, _ = _build(con, campaigns, REAL)
    con.close()
    assert attributions[0].rule == mig.RULE_FIELD
    assert attributions[0].tenant_id == PAPER.tenant_id
    assert not attributions[0].warn


def test_rule2_net_position_wins_over_everything(evidence_db):
    """② 净持仓归属优先 —— 成交流水说谁持有，就归谁（**快照会过期，不能作主证**）。"""
    con = sqlite3.connect(str(evidence_db))
    # 快照说实盘持有，但成交流水显示模拟净 +1 —— 以流水为准
    _snapshot(con, f"positions_{REAL.account_id}", ["US.A"])
    _filled(con, "US.A", PAPER.tenant_id, "BUY", 1)
    campaigns = [{"code": "US.A", "enabled": True}]
    attributions, _, _ = _build(con, campaigns, REAL)
    con.close()
    assert attributions[0].rule == mig.RULE_POSITION
    assert attributions[0].tenant_id == PAPER.tenant_id
    assert attributions[0].warn, "快照与流水不一致必须留 WARN"


def test_rule2_snapshot_only_when_no_flow_records(evidence_db):
    """只有快照、成交流水无该 code → 2b 快照佐证（标注"仅快照"）。"""
    con = sqlite3.connect(str(evidence_db))
    _snapshot(con, f"positions_{REAL.account_id}", ["US.A"])
    _intent(con, "US.A", PAPER.tenant_id, "2026-10-22T00:00:00+00:00")  # 意图但无 FILLED
    campaigns = [{"code": "US.A", "enabled": True}]
    attributions, _, _ = _build(con, campaigns, REAL)
    con.close()
    assert attributions[0].rule == mig.RULE_POSITION_SNAPSHOT
    assert attributions[0].tenant_id == REAL.tenant_id


def test_rule2_net_position_zero_falls_through_to_intent(evidence_db):
    """净持仓为 0（买过又卖了）→ 落到 ③ 成交意图，而不是硬判给某个租户。"""
    con = sqlite3.connect(str(evidence_db))
    _filled(con, "US.A", REAL.tenant_id, "BUY", 1)
    _filled(con, "US.A", REAL.tenant_id, "SELL", 1)
    campaigns = [{"code": "US.A", "enabled": True}]
    attributions, _, _ = _build(con, campaigns, REAL)
    con.close()
    assert attributions[0].rule == mig.RULE_INTENT
    assert attributions[0].tenant_id == REAL.tenant_id


def test_rule2_primary_snapshot_maps_to_primary_tenant(evidence_db):
    """``positions``（无后缀）归**主连接租户**，不能算到别的租户头上。"""
    con = sqlite3.connect(str(evidence_db))
    _snapshot(con, "positions", ["US.A"])
    campaigns = [{"code": "US.A", "enabled": True}]
    attributions, index, unmapped = _build(con, campaigns, REAL)
    con.close()
    assert attributions[0].tenant_id == REAL.tenant_id
    assert attributions[0].rule == mig.RULE_POSITION_SNAPSHOT
    assert "US.A" in index[REAL.tenant_id][0]["codes"]
    assert "US.A" not in {
        c for snap in index[PAPER.tenant_id] for c in snap["codes"]
    }
    assert unmapped == []


def test_rule2_unmappable_snapshot_is_reported_not_guessed(evidence_db):
    con = sqlite3.connect(str(evidence_db))
    _snapshot(con, "positions_totally_unknown", ["US.Z"])
    campaigns = [{"code": "US.Z", "enabled": True}]
    attributions, index, unmapped = _build(con, campaigns, REAL)
    con.close()
    assert unmapped == ["positions_totally_unknown"]
    # 无持仓且推定不出 → ④b 默认归模拟（不是待确认，也不是实盘）
    assert attributions[0].tenant_id == PAPER.tenant_id
    assert attributions[0].pending is False
    assert attributions[0].warn is True


def test_rule3_intent_uses_most_recent_and_flags_ambiguity(evidence_db):
    con = sqlite3.connect(str(evidence_db))
    _intent(con, "US.A", PAPER.tenant_id, "2026-10-22T01:00:00+00:00")
    _intent(con, "US.A", REAL.tenant_id, "2026-10-20T01:00:00+00:00")
    campaigns = [{"code": "US.A", "enabled": True}]
    attributions, _, _ = _build(con, campaigns, REAL)
    con.close()
    assert attributions[0].rule == "3-order-intent"
    assert attributions[0].tenant_id == PAPER.tenant_id
    assert attributions[0].ambiguous is True
    assert attributions[0].warn is True


def test_rule3_single_tenant_intent_is_not_ambiguous(evidence_db):
    con = sqlite3.connect(str(evidence_db))
    _intent(con, "US.A", REAL.tenant_id, "2026-10-22T01:00:00+00:00")
    campaigns = [{"code": "US.A", "enabled": True}]
    attributions, _, _ = _build(con, campaigns, REAL)
    con.close()
    assert attributions[0].rule == "3-order-intent"
    assert attributions[0].ambiguous is False
    assert attributions[0].warn is False


def test_rule4b_assigns_to_paper_not_real(evidence_db):
    """④b 默认归模拟租户 —— 见 test_rule4b_defaults_to_paper_tenant。

    这里钉住"既不 pending、也不落实盘"这两个否定式。
    """
    con = sqlite3.connect(str(evidence_db))
    campaigns = [{"code": "US.NOPE", "enabled": True}]
    attributions, _, _ = _build(con, campaigns, REAL)
    con.close()
    assert attributions[0].rule == mig.RULE_FALLBACK
    assert attributions[0].tenant_id == PAPER.tenant_id
    assert attributions[0].pending is False
    assert attributions[0].tenant_id != REAL.tenant_id
    assert attributions[0].warn is True
    assert any("绝不判给真金" in w for w in attributions[0].warnings)


def test_rule4_exit_only_infers_real_tenant(evidence_db):
    """④a exit_only=True → 按推定归主账户租户（有理由，不是猜）。"""
    con = sqlite3.connect(str(evidence_db))
    campaigns = [{"code": "US.NOPE", "enabled": True, "exit_only": True}]
    attributions, _, _ = _build(con, campaigns, REAL)
    con.close()
    assert attributions[0].rule == mig.RULE_EXIT_ONLY
    assert attributions[0].tenant_id == REAL.tenant_id
    assert attributions[0].pending is False
    assert attributions[0].warn is True, "推定仍必须留 WARN"


def test_rule4_both_tenants_hold_same_code_is_pending(evidence_db):
    """规则 4：两个账户都持有同 code → **不自动裁定**，进待确认清单。"""
    con = sqlite3.connect(str(evidence_db))
    _filled(con, "US.A", REAL.tenant_id, "BUY", 1)
    _filled(con, "US.A", PAPER.tenant_id, "BUY", 1)
    campaigns = [{"code": "US.A", "enabled": True}]
    attributions, _, _ = _build(con, campaigns, REAL)
    con.close()
    assert attributions[0].pending is True
    assert attributions[0].tenant_id == ""
    assert any("两个账户都持有" in w for w in attributions[0].warnings)


def test_window_positions_snapshot_is_authoritative(evidence_db):
    """给了 --positions-file 快照 → 以它为准，成交流水只作佐证。"""
    con = sqlite3.connect(str(evidence_db))
    _snapshot(con, f"positions_{REAL.account_id}", ["US.A"])          # 快照说实盘
    _filled(con, "US.A", PAPER.tenant_id, "BUY", 1)                    # 流水说模拟
    campaigns = [{"code": "US.A", "enabled": True}]
    attributions, _, _ = _build(
        con, campaigns, REAL, positions={PAPER.tenant_id: {"US.A"}},
    )
    con.close()
    assert attributions[0].rule == mig.RULE_POSITION
    assert attributions[0].tenant_id == PAPER.tenant_id
    assert attributions[0].warn, "窗口快照与 broker_snapshots 冲突必须留 WARN"


# --------------------------------------------------------------------------
# 3b. 窗口持仓快照（fail-closed 输入）
# --------------------------------------------------------------------------


def _positions_file(tmp_path, payload, name="positions.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _positions_payload(tenants=("futu_real_master", "futu_paper_sandbox"),
                       captured_at=None):
    return {
        "captured_at": captured_at or datetime.now().astimezone().isoformat(),
        "accounts": [
            {"tenant_id": t, "account_id": "1", "positions": [{"code": "US.X", "qty": 1}]}
            for t in tenants
        ],
    }


def test_positions_file_requires_captured_at(tmp_path):
    path = _positions_file(tmp_path, {"accounts": []})
    with pytest.raises(RuntimeError, match="captured_at"):
        mig.load_positions_file(path, tenants=list(DEFAULT_TENANTS.values()))


def test_positions_file_rejects_stale_snapshot(tmp_path):
    old = datetime.now().astimezone() - timedelta(minutes=120)
    path = _positions_file(tmp_path, _positions_payload(captured_at=old.isoformat()))
    with pytest.raises(RuntimeError, match="过期"):
        mig.load_positions_file(path, tenants=list(DEFAULT_TENANTS.values()))


def test_positions_file_requires_every_active_tenant(tmp_path):
    """缺一个启用租户 → 拒绝（缺失**不等于**无持仓）。"""
    path = _positions_file(tmp_path, _positions_payload(tenants=("futu_real_master",)))
    with pytest.raises(RuntimeError, match="缺启用租户"):
        mig.load_positions_file(path, tenants=list(DEFAULT_TENANTS.values()))


def test_positions_file_rejects_bad_qty(tmp_path):
    payload = _positions_payload()
    payload["accounts"][0]["positions"] = [{"code": "US.X", "qty": "abc"}]
    path = _positions_file(tmp_path, payload)
    with pytest.raises(RuntimeError, match="qty"):
        mig.load_positions_file(path, tenants=list(DEFAULT_TENANTS.values()))


def test_positions_file_ignores_zero_qty(tmp_path):
    payload = _positions_payload()
    payload["accounts"][0]["positions"] = [{"code": "US.X", "qty": 0}]
    payload["accounts"][1]["positions"] = [{"code": "US.Y", "qty": 2}]
    path = _positions_file(tmp_path, payload)
    result = mig.load_positions_file(path, tenants=list(DEFAULT_TENANTS.values()))
    assert result["futu_real_master"] == set()
    assert result["futu_paper_sandbox"] == {"US.Y"}


def test_rule4b_defaults_to_paper_tenant(evidence_db):
    """④b（Chief 10-23 裁定）：判不出来的**默认归模拟租户** —— 规则属于代码，不属于记忆。"""
    con = sqlite3.connect(str(evidence_db))
    campaigns = [{"code": "US.NOPE", "enabled": True}]
    attributions, _, _ = _build(con, campaigns, REAL)
    con.close()
    assert attributions[0].rule == mig.RULE_FALLBACK
    assert attributions[0].tenant_id == PAPER.tenant_id, "默认落模拟盘，绝不落真金"
    assert attributions[0].pending is False
    assert attributions[0].warn is True, "默认归属仍必须留 WARN 供人工复核"
    assert any("绝不判给真金" in w for w in attributions[0].warnings)


def test_rule4b_never_falls_back_to_real_when_no_paper(evidence_db):
    """边界：没有启用的非实盘租户 → 保持待确认，**绝不回落实盘**。"""
    con = sqlite3.connect(str(evidence_db))
    # 只保留实盘租户（模拟停用）→ 没有安全的非实盘落点
    real_only = [DEFAULT_TENANTS["futu_real_master"]]
    campaigns = [{"code": "US.NOPE", "enabled": True}]
    attributions, _, _ = _build(con, campaigns, REAL, tenants=real_only)
    con.close()
    assert attributions[0].pending is True
    assert attributions[0].tenant_id == ""
    assert attributions[0].warn is True


def test_rule4b_never_assigns_to_real(evidence_db):
    """④b 的硬不变量：结果里**绝不出现实盘租户**。"""
    con = sqlite3.connect(str(evidence_db))
    campaigns = [
        {"code": "US.NOPE1", "enabled": True},
        {"code": "US.NOPE2", "enabled": True, "exit_only": False},
    ]
    attributions, _, _ = _build(con, campaigns, REAL)
    con.close()
    assert all(a.tenant_id != REAL.tenant_id for a in attributions)
    assert all(a.tenant_id == PAPER.tenant_id for a in attributions)


def test_apply_refused_when_any_code_pending(tmp_path, monkeypatch):
    """有待人工确认的 code → apply **拒绝**（退出码 6），不猜。

    ④b 落地后，"待确认"只可能出现在**没有启用的非实盘租户**时 ——
    所以这里把注册表换成只有实盘租户，触发那条边界，再断言 apply 被拒。
    """
    import futu_quant.tenant as tenant_mod

    monkeypatch.setattr(
        tenant_mod, "DEFAULT_TENANTS",
        {"futu_real_master": DEFAULT_TENANTS["futu_real_master"]},
    )
    # 主连接租户固定为实盘，避免它去注册表里找 SIMULATE（已被上面的补丁移除）
    monkeypatch.setattr(mig, "resolve_primary_tenant", lambda tenants: REAL)
    db = tmp_path / "trading.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE runtime_config (config_key TEXT PRIMARY KEY, value_json TEXT,
            version INTEGER, updated_at TEXT);
        CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT,
            strategy_id TEXT, payload_json TEXT, created_at TEXT);
        CREATE TABLE broker_snapshots (snapshot_key TEXT PRIMARY KEY, payload_json TEXT,
            captured_at TEXT);
        CREATE TABLE order_intents (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT,
            tenant_id TEXT, created_at TEXT, side TEXT, status TEXT, filled_quantity REAL);
        CREATE TABLE campaign_state (state_key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT);
        CREATE TABLE virtual_allocations (strategy_id TEXT, symbol TEXT, quantity REAL,
            average_cost REAL, updated_at TEXT, tenant_id TEXT);
        CREATE TABLE position_batches (batch_id TEXT PRIMARY KEY, strategy_id TEXT,
            campaign_id TEXT, symbol TEXT, underlying TEXT, structure TEXT, combo_id TEXT,
            opened_at TEXT, closed_at TEXT, status TEXT, allocation_policy TEXT,
            entry_qty REAL, remaining_qty REAL, entry_cost REAL, initial_risk REAL,
            realized_pnl REAL, mfe_pnl_ratio REAL, mae_pnl_ratio REAL, r_multiple REAL,
            metadata_json TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE exit_allocations (id INTEGER PRIMARY KEY, batch_id TEXT,
            close_fill_id INTEGER, close_intent_id INTEGER, allocated_qty REAL,
            open_price REAL, close_price REAL, realized_pnl REAL, r_multiple REAL,
            allocation_rank INTEGER, created_at TEXT);
        """
    )
    con.execute(
        "INSERT INTO runtime_config VALUES('VOLATILITY_CAMPAIGNS',?,1,'t')",
        (json.dumps([{"code": "US.NO_PAPER", "enabled": True}]),),
    )
    con.commit()
    con.close()

    rc = mig.run(["--db", str(db), "--apply", "--skip-backup", "--report",
                  str(tmp_path / "r.md")])
    assert rc == 6, "无安全非实盘落点时必须拒绝 apply，绝不回落实盘"
    report = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "待人工确认" in report

    con = sqlite3.connect(str(db))
    keys = [r[0] for r in con.execute("SELECT config_key FROM runtime_config")]
    con.close()
    assert keys == ["VOLATILITY_CAMPAIGNS"], "拒绝 apply 时不得写入任何租户键"


def test_apply_succeeds_when_4b_defaults_to_paper(tmp_path):
    """④b 落地后：8 条全部有归属 → apply 不再被拒，三个租户键都写出。"""
    from futu_quant.store import TradingStore

    db = tmp_path / "trading.db"
    store = TradingStore(db)
    legacy_payload = json.dumps(
        [{"code": "US.NOPE", "enabled": True}], separators=(",", ":"), ensure_ascii=False
    )
    with store.connection() as con:
        con.execute(
            "INSERT INTO runtime_config(config_key,value_json,version,updated_at) "
            "VALUES('VOLATILITY_CAMPAIGNS',?,1,'t')",
            (legacy_payload,),
        )
    rc = mig.run(["--db", str(db), "--apply", "--skip-backup", "--report",
                  str(tmp_path / "r.md")])
    assert rc == 0, "④b 默认归模拟后，无待确认项，apply 应当成功"

    con = sqlite3.connect(str(db))
    rows = dict(con.execute("SELECT config_key, value_json FROM runtime_config"))
    con.close()
    assert rows["VOLATILITY_CAMPAIGNS"] == legacy_payload, "无前缀旧键必须逐字节不变"
    assert json.loads(rows[f"VOLATILITY_CAMPAIGNS:{PAPER.tenant_id}"]) == [
        {"code": "US.NOPE", "enabled": True}
    ], "④b 的默认落点必须是模拟租户"


def test_manual_override_beats_all_rules(evidence_db):
    con = sqlite3.connect(str(evidence_db))
    _intent(con, "US.A", REAL.tenant_id, "2026-10-22T01:00:00+00:00")
    campaigns = [{"code": "US.A", "enabled": True}]
    attributions, _, _ = mig.build_attribution(
        con, campaigns, list(DEFAULT_TENANTS.values()),
        primary_tenant=REAL, overrides={"US.A": PAPER.tenant_id},
    )
    con.close()
    assert attributions[0].rule == mig.RULE_OVERRIDE
    assert attributions[0].tenant_id == PAPER.tenant_id


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    """dry-run 绝不写库 —— 连版本号都不能动。"""
    db = tmp_path / "trading.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE runtime_config (config_key TEXT PRIMARY KEY, value_json TEXT,
            version INTEGER, updated_at TEXT);
        CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT,
            strategy_id TEXT, payload_json TEXT, created_at TEXT);
        INSERT INTO runtime_config VALUES('VOLATILITY_CAMPAIGNS','[]',7,'2026-10-22T00:00:00');
        """
    )
    con.commit()
    con.close()

    rc = mig.run(["--db", str(db), "--report", str(tmp_path / "report.md")])
    assert rc == 0

    con = sqlite3.connect(str(db))
    row = con.execute(
        "SELECT value_json, version FROM runtime_config WHERE config_key='VOLATILITY_CAMPAIGNS'"
    ).fetchone()
    keys = [r[0] for r in con.execute("SELECT config_key FROM runtime_config")]
    con.close()

    assert row == ("[]", 7)
    assert keys == ["VOLATILITY_CAMPAIGNS"], "dry-run 不得新增任何租户键"
    assert (tmp_path / "report.md").exists(), "dry-run 报告必须留档"


def test_apply_writes_only_tenant_keys_and_keeps_legacy_byte_identical(tmp_path):
    from futu_quant.store import TradingStore

    db = tmp_path / "trading.db"
    legacy_payload = json.dumps(
        [{"code": "US.A", "enabled": True}], separators=(",", ":"), ensure_ascii=False
    )
    # 用真实 schema 建库（TradingStore.initialize 会补齐全部表）
    store = TradingStore(db)
    with store.connection() as con:
        con.execute(
            "INSERT INTO runtime_config(config_key,value_json,version,updated_at) "
            "VALUES('VOLATILITY_CAMPAIGNS',?,1,'2026-10-22T00:00:00')",
            (legacy_payload,),
        )
    store.create_intent(
        "volatility_long_gamma", "test|US.A:REAL", "US.A", "BUY", 1, 1.0,
        tenant_id=REAL.tenant_id,
    )

    rc = mig.run(["--db", str(db), "--apply", "--skip-backup", "--report", str(tmp_path / "r.md")])
    assert rc == 0

    con = sqlite3.connect(str(db))
    rows = dict(con.execute("SELECT config_key, value_json FROM runtime_config"))
    con.close()

    assert rows["VOLATILITY_CAMPAIGNS"] == legacy_payload, "无前缀旧键必须逐字节不变"
    assert rows[f"VOLATILITY_CAMPAIGNS:{REAL.tenant_id}"] == (
        json.dumps([{"code": "US.A", "enabled": True}], separators=(",", ":"), ensure_ascii=False)
    )
    assert f"VOLATILITY_CAMPAIGNS:{PAPER.tenant_id}" in rows
    assert set(rows) == {
        "VOLATILITY_CAMPAIGNS",
        f"VOLATILITY_CAMPAIGNS:{REAL.tenant_id}",
        f"VOLATILITY_CAMPAIGNS:{PAPER.tenant_id}",
        "VOLATILITY_CAMPAIGNS:moomoo_real",
    }


def test_apply_refuses_without_backup_when_disk_is_short(tmp_path, monkeypatch):
    """余量不足时，``--apply`` 必须直接拒绝 —— 绝不带着不足的空间做写迁移。"""
    monkeypatch.setattr(mig.shutil, "disk_usage", lambda _p: type(
        "U", (), {"free": 0}
    )())
    with pytest.raises(RuntimeError, match="磁盘余量不足"):
        mig.check_free_space(tmp_path, 4.0)


def test_migration_script_uses_the_shared_key_entry():
    """迁移脚本必须走共享入口，不得手拼 ``VOLATILITY_CAMPAIGNS:{tenant}``。"""
    source = (_SCRIPTS / "migrate_volatility_campaigns_tenant_scope.py").read_text(
        encoding="utf-8"
    )
    assert "from futu_quant.tenant import" in source
    assert "campaigns_config_key" in source
    assert "VOLATILITY_CAMPAIGNS_KEY" in source
    # 不得出现手拼形态 f"VOLATILITY_CAMPAIGNS:{...}"
    assert 'f"VOLATILITY_CAMPAIGNS:' not in source
    assert "resolve_tenant_config_key" in (
        (Path(__file__).resolve().parents[1] / "src" / "futu_quant" / "tenant.py").read_text(
            encoding="utf-8"
        )
    )


def test_report_records_design_vs_actual_discrepancy(tmp_path):
    """报告必须显式写明「设计 11 / 实测 N」的差异 —— 不得默默按实测值抹掉设计口径。"""
    db = tmp_path / "trading.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE runtime_config (config_key TEXT PRIMARY KEY, value_json TEXT,
            version INTEGER, updated_at TEXT);
        CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT,
            strategy_id TEXT, payload_json TEXT, created_at TEXT);
        INSERT INTO runtime_config VALUES('VOLATILITY_CAMPAIGNS','[]',7,'2026-10-22T00:00:00');
        """
    )
    con.commit()
    con.close()

    report = tmp_path / "report.md"
    assert mig.run(["--db", str(db), "--report", str(report)]) == 0
    text = report.read_text(encoding="utf-8")
    assert "设计口径 vs 实测" in text
    assert f"设计 {mig.DESIGN_DOC_EXPECTED_CAMPAIGNS} 条 / 实测" in text
    assert "不得默默" in text


# --------------------------------------------------------------------------
# 4. 残留核查（Chief 放行前置条件）
# --------------------------------------------------------------------------


def test_residual_check_flags_open_virtual_position(tmp_path):
    """虚拟持仓非零 → 硬阻断；清单变空会把仓位变成无主仓位。"""
    db = tmp_path / "r.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE campaign_state (state_key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT);
        CREATE TABLE virtual_allocations (strategy_id TEXT, symbol TEXT, quantity REAL,
            average_cost REAL, updated_at TEXT, tenant_id TEXT);
        CREATE TABLE position_batches (batch_id TEXT PRIMARY KEY, strategy_id TEXT,
            campaign_id TEXT, symbol TEXT, underlying TEXT, structure TEXT, combo_id TEXT,
            opened_at TEXT, closed_at TEXT, status TEXT, allocation_policy TEXT,
            entry_qty REAL, remaining_qty REAL, entry_cost REAL, initial_risk REAL,
            realized_pnl REAL, mfe_pnl_ratio REAL, mae_pnl_ratio REAL, r_multiple REAL,
            metadata_json TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE exit_allocations (id INTEGER PRIMARY KEY, batch_id TEXT,
            close_fill_id INTEGER, close_intent_id INTEGER, allocated_qty REAL,
            open_price REAL, close_price REAL, realized_pnl REAL, r_multiple REAL,
            allocation_rank INTEGER, created_at TEXT);
        """
    )
    con.execute(
        "INSERT INTO virtual_allocations(strategy_id,symbol,quantity,tenant_id) "
        "VALUES('volatility_long_gamma','US.QQQ261029C735000',2.0,'')"
    )
    con.commit()

    residual = mig.collect_residual(con, ["US.QQQ261029C735000"])
    con.close()
    assert residual["has_open_risk"] is True
    assert residual["virtual_positions"][0]["symbol"] == "US.QQQ261029C735000"
    assert float(residual["virtual_positions"][0]["quantity"]) == 2.0


def test_residual_check_clean_when_nothing_open(tmp_path):
    db = tmp_path / "r.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE campaign_state (state_key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT);
        CREATE TABLE virtual_allocations (strategy_id TEXT, symbol TEXT, quantity REAL,
            average_cost REAL, updated_at TEXT, tenant_id TEXT);
        CREATE TABLE position_batches (batch_id TEXT PRIMARY KEY, strategy_id TEXT,
            campaign_id TEXT, symbol TEXT, underlying TEXT, structure TEXT, combo_id TEXT,
            opened_at TEXT, closed_at TEXT, status TEXT, allocation_policy TEXT,
            entry_qty REAL, remaining_qty REAL, entry_cost REAL, initial_risk REAL,
            realized_pnl REAL, mfe_pnl_ratio REAL, mae_pnl_ratio REAL, r_multiple REAL,
            metadata_json TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE exit_allocations (id INTEGER PRIMARY KEY, batch_id TEXT,
            close_fill_id INTEGER, close_intent_id INTEGER, allocated_qty REAL,
            open_price REAL, close_price REAL, realized_pnl REAL, r_multiple REAL,
            allocation_rank INTEGER, created_at TEXT);
        """
    )
    con.execute(
        "INSERT INTO virtual_allocations(strategy_id,symbol,quantity,tenant_id) "
        "VALUES('volatility_long_gamma','US.QQQ261029C735000',0.0,'')"
    )
    con.execute(
        "INSERT INTO campaign_state(state_key,value_json,updated_at) VALUES "
        "('volatility_long_gamma:US.QQQ261029C735000','{}','2026-10-22T19:59:34+00:00')"
    )
    con.commit()

    residual = mig.collect_residual(con, ["US.QQQ261029C735000"])
    con.close()
    assert residual["has_open_risk"] is False
    assert residual["campaign_state"], "campaign_state 命中必须被登记"


def test_residual_classifies_orphan_state_keys(tmp_path):
    """状态键必须分类登记，并给出「会不会成孤儿」的判定与处置建议。"""
    db = tmp_path / "r.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE campaign_state (state_key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT);
        CREATE TABLE virtual_allocations (strategy_id TEXT, symbol TEXT, quantity REAL,
            average_cost REAL, updated_at TEXT, tenant_id TEXT);
        CREATE TABLE position_batches (batch_id TEXT PRIMARY KEY, strategy_id TEXT,
            campaign_id TEXT, symbol TEXT, underlying TEXT, structure TEXT, combo_id TEXT,
            opened_at TEXT, closed_at TEXT, status TEXT, allocation_policy TEXT,
            entry_qty REAL, remaining_qty REAL, entry_cost REAL, initial_risk REAL,
            realized_pnl REAL, mfe_pnl_ratio REAL, mae_pnl_ratio REAL, r_multiple REAL,
            metadata_json TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE exit_allocations (id INTEGER PRIMARY KEY, batch_id TEXT,
            close_fill_id INTEGER, close_intent_id INTEGER, allocated_qty REAL,
            open_price REAL, close_price REAL, realized_pnl REAL, r_multiple REAL,
            allocation_rank INTEGER, created_at TEXT);
        INSERT INTO campaign_state VALUES
            ('volatility_long_gamma:US.QQQ261029C735000','{}','t'),
            ('volatility_cooldown:US.QQQ','{}','t'),
            ('volatility_long_gamma:signal:US.QQQ','{}','t'),
            ('unrelated:key','{}','t');
        """
    )
    con.commit()
    residual = mig.collect_residual(con, ["US.QQQ261029C735000"])
    con.close()

    assert residual["orphan_state"] is True
    assert len(residual["state_keys_code"]) == 1
    assert residual["state_keys_code"][0]["key"] == "volatility_long_gamma:US.QQQ261029C735000"
    assert len(residual["state_keys_underlying"]) == 2
    assert residual["has_open_risk"] is False

    report = mig.render_report(
        attributions=[], legacy_key="VOLATILITY_CAMPAIGNS", tenant_keys={},
        legacy_raw="[]", positions_index={}, unmapped_snapshots=[],
        primary_tenant=None, applied=False, overrides={}, residual=residual,
    )
    assert "孤儿状态判定：会" in report
    assert "P0-9" in report


def test_residual_check_tolerates_missing_tables(tmp_path):
    """库结构不含这些表（旧库 / 隔离库）→ 不抛异常，按无证据处理。"""
    db = tmp_path / "r.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE runtime_config (config_key TEXT PRIMARY KEY, value_json TEXT)")
    con.commit()
    residual = mig.collect_residual(con, ["US.X"])
    con.close()
    assert residual["has_open_risk"] is False
    assert residual["virtual_positions"] == []


# --------------------------------------------------------------------------
# 5. 风险迁移脚本：生效值口径 + 只写实盘账户
# --------------------------------------------------------------------------


class _FakeStore:
    """够用的 TradingStore 替身：实现 build_plan / verify_written 用到的读方法。"""

    def __init__(self, settings):
        self._settings = settings
        self.audited = []

    def get_setting(self, key, default=""):
        return self._settings.get(key, default)

    def set_setting(self, key, value):
        self._settings[key] = value

    def delete_setting(self, key):
        self._settings.pop(key, None)

    def audit(self, event_type, payload, strategy_id=None, *, actor=None):
        self.audited.append((event_type, payload))

    def settings_with_prefix(self, prefix):
        return {k: v for k, v in self._settings.items() if k.startswith(prefix)}


def test_risk_migration_values_come_from_settings_load(monkeypatch):
    """必须经 ``Settings.load`` 取生效值 —— 裸 os.getenv 拿到的是 dataclass 默认值。"""
    import futu_quant.config as config

    sys.path.insert(0, str(_SCRIPTS))
    import migrate_risk_limits_to_accounts as risk_mig

    def _fake_load(cls, refresh=False):
        os.environ["RISK_MAX_DAILY_LOSS"] = "7777"
        return None

    monkeypatch.setattr(config.Settings, "load", classmethod(_fake_load))

    values = risk_mig.load_effective_risk_values()
    assert values["max_daily_loss"] == 7777.0, "生效值必须来自 Settings.load 折入 .env 后的环境"


def test_risk_migration_skips_non_real_and_inactive_accounts():
    """REAL 账户写第①层；非 REAL 的**启用**租户写第②层种子；未启用跳过。"""
    sys.path.insert(0, str(_SCRIPTS))
    import migrate_risk_limits_to_accounts as risk_mig

    effective = {name: 1.0 for name in risk_mig.RISK_FIELD_NAMES}
    plans = risk_mig.build_plan(
        _FakeStore({f"real_account_config:{REAL_ACCOUNT}": "{}"}),
        DEFAULT_TENANTS, effective=effective,
    )
    by_account = {p.account_id: p for p in plans}

    real = by_account[REAL_ACCOUNT]
    assert real.action == "write"
    assert real.layer == "account"
    assert real.key == f"real_account_config:{REAL_ACCOUNT}"

    # 非实盘的**启用**租户：写第②层种子（否则它永远只能 env 回落）
    paper = by_account[PAPER_ACCOUNT]
    assert paper.action == "write", "模拟租户必须有第②层种子，否则 P0-7 只修了实盘那一半"
    assert paper.layer == "tenant"
    assert paper.key == f"risk_limits:{PAPER.tenant_id}"
    assert paper.risk == effective

    moomoo = by_account["283445327916889204"]
    assert moomoo.action == "skip"
    assert "未启用" in moomoo.reason


def test_risk_migration_tenant_seed_skippable():
    sys.path.insert(0, str(_SCRIPTS))
    import migrate_risk_limits_to_accounts as risk_mig

    effective = {name: 1.0 for name in risk_mig.RISK_FIELD_NAMES}
    plans = risk_mig.build_plan(
        _FakeStore({f"real_account_config:{REAL_ACCOUNT}": "{}"}),
        DEFAULT_TENANTS, effective=effective, seed_tenant_layer=False,
    )
    paper = next(p for p in plans if p.account_id == PAPER_ACCOUNT)
    assert paper.action == "skip"
    assert "--skip-tenant-seed" in paper.reason


def test_risk_migration_tenant_seed_does_not_clobber():
    """已有第②层配置 → 不覆盖，需 --overwrite-risk。"""
    sys.path.insert(0, str(_SCRIPTS))
    import migrate_risk_limits_to_accounts as risk_mig

    effective = {name: 1.0 for name in risk_mig.RISK_FIELD_NAMES}
    store = _FakeStore({
        f"real_account_config:{REAL_ACCOUNT}": "{}",
        f"risk_limits:{PAPER.tenant_id}": json.dumps(
            {name: 9.0 for name in risk_mig.RISK_FIELD_NAMES}
        ),
    })
    plans = risk_mig.build_plan(store, DEFAULT_TENANTS, effective=effective)
    paper = next(p for p in plans if p.account_id == PAPER_ACCOUNT)
    assert paper.action == "skip"
    assert "已存在" in paper.reason
    assert paper.risk["max_daily_loss"] == 9.0


def test_risk_migration_writes_tenant_layer_on_apply():
    """apply 后第②层键真的落地，且回读比对通过。"""
    sys.path.insert(0, str(_SCRIPTS))
    import migrate_risk_limits_to_accounts as risk_mig

    effective = {name: 1.0 for name in risk_mig.RISK_FIELD_NAMES}
    store = _FakeStore({f"real_account_config:{REAL_ACCOUNT}": "{}"})
    plans = risk_mig.build_plan(store, DEFAULT_TENANTS, effective=effective)
    paper = next(p for p in plans if p.account_id == PAPER_ACCOUNT)
    assert risk_mig.apply_plan(store, paper) is True
    assert risk_mig.verify_written(store, paper) is None
    assert store.get_setting(f"risk_limits:{PAPER.tenant_id}", "") != ""
    assert store.audited, "写入必须留审计"
    # 回滚也要能删掉它
    risk_mig.restore_plan(store, paper, None)
    assert store.get_setting(f"risk_limits:{PAPER.tenant_id}", "") == ""


def test_risk_migration_documents_same_name_different_layer():
    """开仓侧两键在 risk 段与账户顶层同名不同层 —— 必须显式写明，不得让人以为冲突。"""
    sys.path.insert(0, str(_SCRIPTS))
    import migrate_risk_limits_to_accounts as risk_mig

    effective = {name: 100000.0 for name in risk_mig.RISK_FIELD_NAMES}
    store = _FakeStore({
        f"real_account_config:{REAL_ACCOUNT}": json.dumps({
            "max_order_notional": 2500.0, "max_daily_notional": 5000.0,
        }),
    })
    plans = risk_mig.build_plan(store, DEFAULT_TENANTS, effective=effective)
    real_plan = next(p for p in plans if p.account_id == REAL_ACCOUNT)
    assert real_plan.action == "write"
    joined = "\n".join(real_plan.notes)
    assert "同名不同层" in joined
    assert "max_entry_notional" in joined and "2500" in joined
    assert "max_entry_daily_notional" in joined and "5000" in joined
    # risk 段按生效值写入（不改数值），顶层门禁额度原样保留
    assert real_plan.risk["max_entry_notional"] == 100000.0
    assert real_plan.before.get("max_order_notional") == 2500.0
    assert real_plan.after["max_order_notional"] == 2500.0


def test_risk_migration_reports_before_and_after_per_field():
    """dry-run 必须逐条列出「从多少到多少」—— 让人看见，不让脚本替人决定。"""
    sys.path.insert(0, str(_SCRIPTS))
    import migrate_risk_limits_to_accounts as risk_mig

    effective = {name: 100000.0 for name in risk_mig.RISK_FIELD_NAMES}
    plans = risk_mig.build_plan(
        _FakeStore({f"real_account_config:{REAL_ACCOUNT}": "{}"}),
        DEFAULT_TENANTS, effective=effective,
    )
    real_plan = next(p for p in plans if p.account_id == REAL_ACCOUNT)
    assert set(real_plan.before_value) == set(risk_mig.RISK_FIELD_NAMES)
    assert set(real_plan.risk) == set(risk_mig.RISK_FIELD_NAMES)
    report = risk_mig.render_report(
        plans, applied=False, effective=effective,
    )
    assert "迁移前（全局生效）" in report
    assert "写入 risk 段" in report
    assert "从多少到多少" in report


def test_risk_migration_verify_written_detects_mismatch():
    """回读比对：写错数必须被证伪（返回失败说明），不能只靠打印。"""
    sys.path.insert(0, str(_SCRIPTS))
    import migrate_risk_limits_to_accounts as risk_mig

    effective = {name: 100000.0 for name in risk_mig.RISK_FIELD_NAMES}
    plans = risk_mig.build_plan(
        _FakeStore({f"real_account_config:{REAL_ACCOUNT}": "{}"}),
        DEFAULT_TENANTS, effective=effective,
    )
    plan = next(p for p in plans if p.account_id == REAL_ACCOUNT)

    good = _FakeStore({
        real_account_config_key(REAL_ACCOUNT): json.dumps({"risk": dict(plan.risk)}),
    })
    assert risk_mig.verify_written(good, plan) is None

    bad = _FakeStore({
        real_account_config_key(REAL_ACCOUNT): json.dumps(
            {"risk": {**plan.risk, "max_daily_loss": 1.0}}
        ),
    })
    failure = risk_mig.verify_written(bad, plan)
    assert failure and "max_daily_loss" in failure

    missing = _FakeStore({})
    assert risk_mig.verify_written(missing, plan)


def test_risk_migration_restore_plan_rolls_back():
    """回滚：原来没有的键要删掉，原来有的要还原原值。"""
    sys.path.insert(0, str(_SCRIPTS))
    import migrate_risk_limits_to_accounts as risk_mig

    effective = {name: 1.0 for name in risk_mig.RISK_FIELD_NAMES}
    plans = risk_mig.build_plan(
        _FakeStore({f"real_account_config:{REAL_ACCOUNT}": "{}"}),
        DEFAULT_TENANTS, effective=effective,
    )
    plan = next(p for p in plans if p.account_id == REAL_ACCOUNT)

    store = _FakeStore({})
    risk_mig.restore_plan(store, plan, None)
    assert store.get_setting(real_account_config_key(REAL_ACCOUNT), "") == ""

    store = _FakeStore({real_account_config_key(REAL_ACCOUNT): "ORIGINAL"})
    risk_mig.restore_plan(store, plan, "ORIGINAL")
    assert store.get_setting(real_account_config_key(REAL_ACCOUNT), "") == "ORIGINAL"


def test_risk_migration_uses_shared_key_entry():
    source = (_SCRIPTS / "migrate_risk_limits_to_accounts.py").read_text(encoding="utf-8")
    assert "real_account_config_key" in source
    assert 'f"real_account_config:{' not in source


def test_single_scoped_key_entry_point():
    """Reviewer 裁定：只留一个拼键函数 —— 不得再出现第二个同义名。"""
    source = (Path(__file__).resolve().parents[1] / "src" / "futu_quant" / "tenant.py").read_text(
        encoding="utf-8"
    )
    assert "def resolve_tenant_config_key" in source
    assert "def tenant_setting_key" not in source, "同义名必须删掉，只留一个入口"


# --------------------------------------------------------------------------
# 6. tenant_of_broker —— 从 broker 反查租户的唯一入口
# --------------------------------------------------------------------------


class _Broker:
    """最小 broker 替身：只有属性，没有方法。"""

    def __init__(self, **attrs):
        for key, value in attrs.items():
            setattr(self, key, value)


def test_tenant_of_broker_uses_explicit_registered_tenant_id():
    broker = _Broker(tenant_id=PAPER.tenant_id, account_id=REAL_ACCOUNT)
    assert tenant_of_broker(broker) == (PAPER.tenant_id, REAL_ACCOUNT)


def test_tenant_of_broker_resolves_account_without_tenant_id():
    """生产 broker 不带 tenant_id，只有 account_id —— 必须能反解出租户。"""
    broker = _Broker(account_id=REAL_ACCOUNT)
    assert tenant_of_broker(broker) == (REAL.tenant_id, REAL_ACCOUNT)
    broker = _Broker(account_id=PAPER_ACCOUNT)
    assert tenant_of_broker(broker) == (PAPER.tenant_id, PAPER_ACCOUNT)


def test_tenant_of_broker_does_not_silently_default_to_real():
    """未注册账户**不得**被静默判成实盘租户 —— 那是 fail-open。"""
    broker = _Broker(account_id="99999999999")
    assert tenant_of_broker(broker) == (None, "99999999999")
    with pytest.raises(TenantConfigKeyError):
        tenant_of_broker(broker, strict=True)


def test_tenant_of_broker_requires_strict_for_write_paths():
    """无租户上下文时：非 strict 返回 None，strict 抛异常 —— 绝不静默。"""
    assert tenant_of_broker(_Broker()) == (None, None)
    with pytest.raises(TenantConfigKeyError):
        tenant_of_broker(_Broker(), strict=True)


def test_tenant_of_broker_rejects_unregistered_explicit_tenant():
    broker = _Broker(tenant_id="ghost_tenant", account_id=REAL_ACCOUNT)
    # 显式租户未注册 → 回落到按账户反解（实盘账户 → futu_real_master）
    assert tenant_of_broker(broker) == (REAL.tenant_id, REAL_ACCOUNT)
    # 既无有效租户也无账户 → strict 报错
    with pytest.raises(TenantConfigKeyError):
        tenant_of_broker(_Broker(tenant_id="ghost_tenant"), strict=True)


def test_tenant_of_broker_filters_mock_attributes():
    """测试替身（MagicMock）的任意属性访问不得被当成真实值带进解析。"""
    mock = MagicMock()
    assert tenant_of_broker(mock) == (None, None)
    with pytest.raises(TenantConfigKeyError):
        tenant_of_broker(mock, strict=True)


def test_tenant_id_of_broker_reuses_the_same_entry():
    broker = _Broker(account_id=REAL_ACCOUNT)
    assert tenant_id_of_broker(broker) == REAL.tenant_id
    assert tenant_id_of_broker(_Broker(account_id="99999999999")) is None


def test_tenant_of_broker_accepts_custom_registry():
    custom = dict(DEFAULT_TENANTS)
    custom["custom_tenant"] = TenantProfile(
        tenant_id="custom_tenant", display_name="自定义", broker_type="futu",
        trading_env="REAL", account_id="555", is_active=True,
    )
    broker = _Broker(account_id="555")
    assert tenant_of_broker(broker, registry=custom) == ("custom_tenant", "555")
    assert tenant_of_broker(broker) == (None, "555"), "默认注册表里没有就不许猜"


def test_legacy_fallback_is_recorded_by_the_tool(monkeypatch):
    """回落必须由工具自己留痕 —— 调用方忘了也不会静默。"""
    records: list[tuple] = []

    def _fake_warning(msg, *args):
        records.append((msg, args))

    monkeypatch.setattr(tenant_mod._LEGACY_FALLBACK_LOGGER, "warning", _fake_warning)
    tenant_mod.reset_legacy_fallback_count()
    try:
        tenant_key, legacy = tenant_config_key_candidates(None, "VOLATILITY_CAMPAIGNS")
        assert tenant_key is None
        assert legacy == "VOLATILITY_CAMPAIGNS"
        assert tenant_mod.legacy_fallback_count() == 1
        assert records, "回落必须留下 WARNING 日志"
        assert "VOLATILITY_CAMPAIGNS" in str(records[0])
    finally:
        tenant_mod.reset_legacy_fallback_count()


def test_legacy_fallback_counter_accumulates():
    tenant_mod.reset_legacy_fallback_count()
    try:
        for _ in range(3):
            tenant_config_key_candidates("nope", "strategy_enabled")
        assert tenant_mod.legacy_fallback_count() == 3
    finally:
        tenant_mod.reset_legacy_fallback_count()


def test_valid_tenant_key_does_not_record_fallback():
    tenant_mod.reset_legacy_fallback_count()
    try:
        tenant_config_key_candidates(REAL.tenant_id, "strategy_enabled")
        assert tenant_mod.legacy_fallback_count() == 0
    finally:
        tenant_mod.reset_legacy_fallback_count()
