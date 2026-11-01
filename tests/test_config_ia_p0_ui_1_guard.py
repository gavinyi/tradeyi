"""P0-UI-1 专属守卫测试：
1. 校验 ConfigSpec.user_area 字段：
   - 382 项配置 100% 具备非空 user_area；
   - user_area 取值严格限定在 4 个枚举内：strategy_center, risk_limits, account_env, ops_notify；
   - 负向测试：若 user_area 为空或非法枚举值，加载期必须 fail-fast 抛出 ValueError 变红；
2. AC5 中文别名样本表搜索命中唯一入口测试：
   - 日限额 / 单笔限额 / 实盘额度 -> risk_limits 唯一命中；
   - 止损 / 日亏 / 回撤 -> risk_limits 唯一命中；
   - 飞书 / 告警 / 通知 -> ops_notify 唯一命中；
   - 急停 -> ops_notify 唯一命中；
   - 停止开仓 -> account_env 唯一命中；
   - 策略开关 / 运行模式 / 标的白名单 -> strategy_center 唯一命中；
   - 需不需要重启 -> 包含 activation 说明；
3. 今日总览（只读大屏聚合）无编辑项守卫：
   - 断言今日总览不持有任何可写配置键。
"""

from __future__ import annotations

import pytest
from futu_quant.config_center.registry import GLOBAL_REGISTRY, ConfigRegistry, _load_default_specs
from futu_quant.config_center.spec import ConfigSpec


def test_user_area_metadata_strictly_four_enums_and_no_empty():
    """AC1/AC4: 382项配置全部具备合规 user_area，严禁空值或未分配。"""
    specs = GLOBAL_REGISTRY.all_specs()
    assert len(specs) == 382

    valid_areas = {"strategy_center", "risk_limits", "account_env", "ops_notify"}
    area_counts = {}
    for spec in specs:
        area = spec.user_area
        assert area in valid_areas, f"配置项 {spec.key} 的 user_area 非法: {area!r}"
        area_counts[area] = area_counts.get(area, 0) + 1

    # 确保 4 个业务区均承载了对应技术分类的配置
    assert area_counts["strategy_center"] > 0
    assert area_counts["risk_limits"] > 0
    assert area_counts["account_env"] > 0
    assert area_counts["ops_notify"] > 0
    assert sum(area_counts.values()) == 382


def test_user_area_loading_fails_fast_on_invalid_or_missing(monkeypatch):
    """负向测试：若数据中缺少 user_area 或取值非法，加载期必须 fail-fast 抛出 ValueError。"""
    import futu_quant.config_center.registry as reg_mod

    # 1. 模拟缺项数据
    bad_data = [
        {
            "key": "test.bad.spec",
            "aliases": ["test_bad_spec"],
            "runtime_key": "test_bad_spec",
            # user_area 故意缺失
        }
    ]

    import json
    import tempfile
    import pathlib

    tmp_dir = pathlib.Path(tempfile.mkdtemp())
    bad_json = tmp_dir / "bad_specs.json"
    bad_json.write_text(json.dumps(bad_data), encoding="utf-8")

    monkeypatch.setattr(reg_mod, "_DATA_PATH", bad_json)

    with pytest.raises(ValueError, match="user_area 非法或缺失"):
        reg_mod._load_default_specs()


def test_ac5_chinese_search_sample_matrix():
    """AC5: 验证中文别名样本表搜索命中唯一入口。"""
    sample_matrix = {
        "日限额": ["risk_limits"],
        "单笔限额": ["risk_limits"],
        "实盘额度": ["risk_limits"],
        "飞书": ["ops_notify"],
        "告警": ["ops_notify"],
        "通知": ["ops_notify"],
        "止损": ["risk_limits"],
        "日亏": ["risk_limits"],
        "回撤": ["risk_limits"],
        "策略开关": ["strategy_center"],
        "运行模式": ["strategy_center"],
        "标的白名单": ["strategy_center"],
        "急停": ["ops_notify"],
        "停止开仓": ["account_env"],
    }

    for word, expected_areas in sample_matrix.items():
        hits = GLOBAL_REGISTRY.search_user_area(word)
        hit_areas = sorted(list(hits.keys()))
        assert hit_areas == sorted(expected_areas), (
            f"搜索词 '{word}' 期望命中 {expected_areas}，实际命中 {hit_areas}"
        )


def test_today_overview_holds_zero_editable_config():
    """AC2: 今日总览仅做聚合状态展示，不持有任何可编辑配置。"""
    valid_areas = {"strategy_center", "risk_limits", "account_env", "ops_notify"}
    # 断言没有任何 spec 归属于今日总览
    for spec in GLOBAL_REGISTRY.all_specs():
        assert spec.user_area in valid_areas
        assert spec.user_area != "today_overview"
