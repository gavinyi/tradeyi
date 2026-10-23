"""验收：在实盘租户执行「暂停全部策略」，模拟租户的策略循环必须仍在运行。

背景（10-23 产品红线，实锤）：
`run_service.service_cycle` 读取暂停状态时**先看租户键、再回落全局键**，
而控制页写的是**全局键** `strategies_paused` —— 于是全局键一写，
**所有租户**的策略循环一起被停：在实盘租户点「暂停全部策略」，模拟盘也被连坐。
我们花了一整轮做 N-Tenant 隔离，一个全局暂停键就能跨租户互相连坐，那隔离就是假的。

修复：写入改为 `set_tenant_setting`（只写当前租户），读取保持「租户键优先、全局键兜底」。

本文件锁定「暂停的作用域 = 单租户」这条不变量。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from futu_quant.store import TradingStore  # noqa: E402

REAL_TENANT = "futu_real_master"
PAPER_TENANT = "futu_paper_sandbox"


@pytest.fixture()
def keys():
    """改写暂停键，测试结束完整还原生产原值（不污染生产配置）。"""
    store = TradingStore()
    saved = {
        "global": store.get_setting("strategies_paused"),
        REAL_TENANT: store.get_tenant_setting(REAL_TENANT, "strategies_paused", ""),
        PAPER_TENANT: store.get_tenant_setting(PAPER_TENANT, "strategies_paused", ""),
    }
    try:
        yield store
    finally:
        for key, value in saved.items():
            tenant = None if key == "global" else key
            if tenant:
                target = f"strategies_paused:{tenant}"
            else:
                target = "strategies_paused"
            if value:
                store.set_setting(target, value)
            else:
                store.delete_setting(target)


def _paused(store, tenant_id: str) -> bool:
    """复刻 run_service 的读取口径：租户键优先、全局键兜底。"""
    tenant_value = store.get_tenant_setting(tenant_id, "strategies_paused", "")
    return (tenant_value or store.get_setting("strategies_paused", "false")).lower() == "true"


def test_pausing_real_does_not_pause_paper(keys):
    """核心验收：暂停实盘 → 实盘停、**模拟盘照跑**。"""
    store = keys
    # 干净起点：全局不暂停，两个租户都不暂停
    store.set_setting("strategies_paused", "false")
    store.delete_setting(f"strategies_paused:{REAL_TENANT}")
    store.delete_setting(f"strategies_paused:{PAPER_TENANT}")
    assert not _paused(store, REAL_TENANT) and not _paused(store, PAPER_TENANT)

    # 在实盘租户点「暂停全部策略」—— 写租户键
    store.set_tenant_setting(REAL_TENANT, "strategies_paused", "true")

    assert _paused(store, REAL_TENANT) is True, "实盘未被暂停"
    assert _paused(store, PAPER_TENANT) is False, (
        "🔴 跨租户连坐：暂停实盘把模拟盘也停了 —— N-Tenant 隔离失效"
    )


def test_pausing_paper_does_not_pause_real(keys):
    """对称验收：暂停模拟盘不得影响实盘。"""
    store = keys
    store.set_setting("strategies_paused", "false")
    store.delete_setting(f"strategies_paused:{REAL_TENANT}")
    store.delete_setting(f"strategies_paused:{PAPER_TENANT}")

    store.set_tenant_setting(PAPER_TENANT, "strategies_paused", "true")

    assert _paused(store, PAPER_TENANT) is True
    assert _paused(store, REAL_TENANT) is False, "暂停模拟盘影响了实盘"


def test_global_key_still_guards_all_tenants(keys):
    """全局键仍作为兜底 —— 兼容历史设置，不让既有全局暂停悄悄失效。"""
    store = keys
    store.set_setting("strategies_paused", "true")
    store.delete_setting(f"strategies_paused:{REAL_TENANT}")
    store.delete_setting(f"strategies_paused:{PAPER_TENANT}")

    assert _paused(store, REAL_TENANT) is True
    assert _paused(store, PAPER_TENANT) is True, "全局键兜底失效 —— 历史全局暂停会失效"


def test_tenant_override_can_opt_out_of_global(keys):
    """租户键可以覆盖全局键（例如全局停、但某租户明确恢复）。"""
    store = keys
    store.set_setting("strategies_paused", "true")
    store.set_tenant_setting(PAPER_TENANT, "strategies_paused", "false")
    store.delete_setting(f"strategies_paused:{REAL_TENANT}")

    assert _paused(store, PAPER_TENANT) is False, "租户键未能覆盖全局键"
    assert _paused(store, REAL_TENANT) is True, "未覆盖的租户应仍随全局键"


def test_control_page_writes_tenant_key():
    """回归：控制页必须写租户键（否则上面的隔离全部失效）。"""
    page = (ROOT / "pages" / "15_strategy_control_center.py").read_text(encoding="utf-8")
    assert "set_tenant_setting" in page, "控制页未按租户写暂停键"
    assert "get_tenant_setting" in page, "控制页读取未做租户键优先"


def test_service_reader_is_tenant_aware():
    """回归：服务侧读取必须租户键优先（这是整条链的口径基准）。"""
    service = (ROOT / "scripts" / "run_service.py").read_text(encoding="utf-8")
    assert "get_tenant_setting(tenant_id, \"strategies_paused\"" in service, (
        "服务侧未按租户读暂停状态"
    )


def test_pause_caption_matches_actual_behavior():
    """守卫：界面文案必须与 `strategies_paused` 的**实际语义**一致。

    实际行为：`run_service` 读到 paused → `configured.clear()` → 波动率策略循环不跑
    → 止盈 / 止损 / 到期退出 / 组合平仓管理**全部停止**（只剩订单对账与风控引擎）。
    此前文案写的是"不会停止…退出管理"，与代码**相反** —— 用户会以为持仓仍被管理，
    而实际上已经没人管了（今晚踩过）。文案必须明示退出管理会停止，并指向正确替代开关。
    """
    page = (ROOT / "pages" / "15_strategy_control_center.py").read_text(encoding="utf-8")
    assert "不会停止" not in page, (
        "界面仍声称「不会停止…退出管理」，与实际行为（会停止）相反"
    )
    assert "组合平仓管理会一并停止" in page, (
        "界面未明示退出管理（止盈/止损/到期退出/组合平仓）会停止，"
        "用户无法判断持仓是否仍被管理"
    )
    assert "暂停新开仓" in page, (
        "界面未指向正确替代开关（只停开新仓、保留退出管理）—— "
        "用户会在两个语义不同的开关之间误判"
    )
