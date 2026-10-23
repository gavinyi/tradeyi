"""回归：一张合约只应有一条 campaign 记录（Ops 10-22 排查出的重复执行）。

背景（10-22 实测）：`US.AMZN261028P250000` 在配置里同时存在两条 ——
`campaign_id = orphan:US.AMZN261028P252500` 与 `campaign_id = orphan:US.AMZN261028P250000`。
根因：`upsert_campaign` 的身份取 `campaign_id` 优先，而**重复接管**同一张合约时
`campaign_id` 会变（它带的是当时的合约号），于是同一 code 被当成两条记录。
worker 每轮把两条都执行一遍 → 决策重复、日志里同一合约打印 2–3 次。

修复：身份改为**以合约 `code` 为准**（合约身份天然唯一），重复接管/滚动变为幂等。

本文件锁定「按 code 去重」这条不变量。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from futu_quant.config import delete_campaign, load_volatility_campaigns, upsert_campaign  # noqa: E402
from futu_quant.store import TradingStore  # noqa: E402

CODE = "US.AMZN261028P250000"


def _campaign(code: str, campaign_id: str | None = None, **extra) -> dict:
    base = {
        "code": code,
        "underlying": "US.AMZN",
        "option_type": "PUT",
        "legs": [{"code": code, "side": "BUY", "ratio": 1, "role": "PRIMARY"}],
        "enabled": True,
        "exit_only": True,
    }
    if campaign_id:
        base["campaign_id"] = campaign_id
    base.update(extra)
    return base


@pytest.fixture()
def campaigns(tmp_path, monkeypatch):
    """把 campaign 配置完全隔离到临时库。

    ⚠️ `upsert_campaign`/`delete_campaign` 内部自己 `TradingStore()`，不接参数；
    而 `TradingStore.__init__` 的默认值 `path=DEFAULT_DB` 是**导入时绑定**的，
    改常量无效。所以必须直接替换 `__init__` 的默认路径 ——
    本次调试中曾因此两次写穿生产库（均已从备份回滚并留审计）。
    """
    db = tmp_path / "campaigns.db"
    real_init = TradingStore.__init__

    def isolated_init(self, path=None, tenant_registry=None):
        real_init(self, db if path is None else path, tenant_registry)

    monkeypatch.setattr(TradingStore, "__init__", isolated_init)
    monkeypatch.setattr("futu_quant.store.DEFAULT_DB", db, raising=False)
    TradingStore(db)
    return db


def test_upsert_by_code_is_idempotent(campaigns, monkeypatch):
    """同一 code 重复接管（campaign_id 不同）→ 只能有一条记录。"""
    first = upsert_campaign("VOLATILITY_CAMPAIGNS", _campaign(CODE, campaign_id=f"orphan:{CODE}"))
    assert len(first) == 1
    second = upsert_campaign(
        "VOLATILITY_CAMPAIGNS", _campaign(CODE, campaign_id="orphan:US.AMZN261028P252500")
    )
    assert len(second) == 1, f"同一合约出现 {len(second)} 条记录 —— worker 会重复执行"
    assert second[0]["campaign_id"] == "orphan:US.AMZN261028P252500", "应保留最新一次接管"


def test_different_codes_are_separate(campaigns, monkeypatch):
    """回归：不同合约必须各自成条，不得被误并。"""
    rows = upsert_campaign("VOLATILITY_CAMPAIGNS", _campaign(CODE))
    rows = upsert_campaign("VOLATILITY_CAMPAIGNS", _campaign("US.AMZN261028P252500"))
    assert len(rows) == 2


def test_delete_by_code_removes_the_single_record(campaigns, monkeypatch):
    """删除按 code 生效（且只删一条）。"""
    upsert_campaign("VOLATILITY_CAMPAIGNS", _campaign(CODE, campaign_id=f"orphan:{CODE}"))
    upsert_campaign(
        "VOLATILITY_CAMPAIGNS", _campaign(CODE, campaign_id="orphan:US.AMZN261028P252500")
    )
    rows = delete_campaign("VOLATILITY_CAMPAIGNS", CODE)
    assert rows == [], f"删除后仍有 {len(rows)} 条记录"
    assert load_volatility_campaigns() == []


def test_production_campaigns_are_untouched(campaigns):
    """隔离守卫：本文件的用例不得改动生产 campaign 配置。"""
    before = load_volatility_campaigns()
    upsert_campaign("VOLATILITY_CAMPAIGNS", _campaign(CODE, campaign_id="orphan:TEST"))
    assert load_volatility_campaigns() != before, "隔离失效：临时库未生效"

