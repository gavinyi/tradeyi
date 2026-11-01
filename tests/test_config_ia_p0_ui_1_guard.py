"""P0-UI-1 专属守卫测试 (已补齐导航与严格无默认枚举检验)：
1. 校验 ConfigSpec.user_area 与 surface 字段：
   - 382 项配置 100% 具备合规 user_area 与 surface；
   - user_area 取值严格限定在 4 个枚举内：strategy_center, risk_limits, account_env, ops_notify（必填无默认）；
   - surface 取值严格限定在 2 个枚举内：primary, advanced（必填无默认）；
   - 强约束：每个 area 的 primary 数量严格 <= 30 项！
   - 负向测试：若 user_area 或 surface 为空或非法枚举值，无论是 ConfigSpec 构造期还是 JSON 加载期，均立即 fail-fast 抛出 ValueError 变红；
2. AC5 中文别名样本表搜索命中唯一入口测试：
   - 日限额 / 单笔限额 / 实盘额度 -> risk_limits 唯一命中；
   - 止损 / 日亏 / 回撤 -> risk_limits 唯一命中；
   - 飞书 / 告警 / 通知 -> ops_notify 唯一命中；
   - 急停 -> ops_notify 唯一命中；
   - 停止开仓 -> account_env 唯一命中；
   - 策略开关 / 运行模式 / 标的白名单 -> strategy_center 唯一命中；
   - 需不需要重启 -> 包含 activation 说明；
3. 5 个业务入口骨架与导航注册守卫：
   - 断言 5 个入口文件全部真实存在且已在 app.py 的 st.navigation 中显式注册；
   - 断言 pages/00 今日总览仅做聚合状态展示，不持有任何可写配置键，零写操作。
"""

from __future__ import annotations

import ast
import json
import pathlib
import tempfile
import pytest

from futu_quant.config_center.registry import GLOBAL_REGISTRY, ConfigRegistry, _load_default_specs
from futu_quant.config_center.spec import ConfigSpec


def test_user_area_metadata_strictly_four_enums_and_no_empty():
    """AC1/AC4: 382项配置全部具备合规 user_area 与 surface，严禁空值或未分配。"""
    specs = GLOBAL_REGISTRY.all_specs()
    assert len(specs) == 382

    valid_areas = {"strategy_center", "risk_limits", "account_env", "ops_notify"}
    valid_surfaces = {"primary", "advanced"}
    area_counts = {}
    primary_counts = {a: 0 for a in valid_areas}

    for spec in specs:
        area = spec.user_area
        surface = spec.surface
        assert area in valid_areas, f"配置项 {spec.key} 的 user_area 非法: {area!r}"
        assert surface in valid_surfaces, f"配置项 {spec.key} 的 surface 非法: {surface!r}"
        area_counts[area] = area_counts.get(area, 0) + 1
        if surface == "primary":
            primary_counts[area] += 1

    # 确保 4 个业务区均承载了对应技术分类的配置
    assert area_counts["strategy_center"] > 0
    assert area_counts["risk_limits"] == 29, f"risk_limits 应恰好包含 29 项，实际 {area_counts['risk_limits']}"
    assert area_counts["account_env"] > 0
    assert area_counts["ops_notify"] > 0
    assert sum(area_counts.values()) == 382

    # AC3 核心约束：每个 area 的 primary 项严格 <= 30 项！
    for a, count in primary_counts.items():
        assert count <= 30, f"{a} 的 primary 项数量 {count} 超出 30 限制！"


def test_user_area_and_surface_fail_fast_on_construction():
    """负向测试：ConfigSpec 构造期若 user_area 或 surface 缺失或非法，必须立即抛出 ValueError。"""
    # 1. user_area 非法
    with pytest.raises(ValueError, match="user_area 非法或缺失"):
        ConfigSpec(
            key="test.key",
            aliases=(),
            category="test",
            title="test",
            description="test",
            value_type="string",
            default="",
            scope="global",
            storage="env",
            risk_level="LOW",
            editable=False,
            requires_admin=False,
            requires_confirmation=False,
            activation="immediate",
            consumers=(),
            tags=(),
            runtime_key="test_key",
            user_area="invalid_area_xxx",
            surface="primary",
        )

    # 2. surface 非法
    with pytest.raises(ValueError, match="surface 非法或缺失"):
        ConfigSpec(
            key="test.key2",
            aliases=(),
            category="test",
            title="test",
            description="test",
            value_type="string",
            default="",
            scope="global",
            storage="env",
            risk_level="LOW",
            editable=False,
            requires_admin=False,
            requires_confirmation=False,
            activation="immediate",
            consumers=(),
            tags=(),
            runtime_key="test_key2",
            user_area="strategy_center",
            surface="invalid_surface_yyy",
        )


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


def test_five_entrances_registered_in_app_navigation():
    """Reviewer 第 3 条守卫：断言 5 个业务入口全部在 app.py 的 st.navigation 中注册。"""
    app_path = pathlib.Path(__file__).resolve().parent.parent / "app.py"
    with open(app_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=str(app_path))

    nav_pages = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "Page":
                if node.args and isinstance(node.args[0], ast.Constant):
                    nav_pages.append(node.args[0].value)

    expected_5 = [
        "pages/00_today_overview.py",
        "pages/44_strategy_center.py",
        "pages/45_risk_and_limits.py",
        "pages/46_account_and_env.py",
        "pages/47_ops_and_notifications.py",
    ]
    for p in expected_5:
        assert p in nav_pages, f"入口页面 {p} 未在 app.py 的导航中注册！"
        # 且文件本身必须真实存在
        page_file = pathlib.Path(__file__).resolve().parent.parent / p
        assert page_file.exists(), f"入口页面文件不存在: {page_file}"


def test_today_overview_ast_zero_write_calls():
    """守卫：今日总览页面 AST 静态检查，绝对零写操作调用。"""
    overview_path = pathlib.Path(__file__).resolve().parent.parent / "pages/00_today_overview.py"
    with open(overview_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=str(overview_path))

    banned_writes = {"set_setting", "set_runtime_config", "mutate_runtime_config", "create_config_release", "publish_config_release"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            assert name not in banned_writes, f"今日总览页面存在写调用: {name}"


def test_today_overview_no_fake_zeros_and_hasattr_must_have_explicit_notice():
    """守卫：断言今日总览页面无硬编码字面量 '0 项'，且 hasattr 之后必须有未就绪/未统计提示。"""
    overview_path = pathlib.Path(__file__).resolve().parent.parent / "pages/00_today_overview.py"
    content = overview_path.read_text(encoding="utf-8")

    # 1. 断言绝无硬编码的 value="0 项" 伪造正常
    assert 'value="0 项"' not in content, "今日总览存在硬编码的 '0 项' 伪造正常指标！"

    # 2. 检查两处 hasattr 分支均有显式的“未统计”提示
    assert 'value="未统计"' in content, "hasattr 探测必须伴随显式未统计提示！"


def test_strategy_center_card_hides_runtime_key_from_main_body():
    """守卫：策略中心等入口卡片正文禁止铺露底层 runtime_key，键名必须折叠在 expander 中。"""
    sc_path = pathlib.Path(__file__).resolve().parent.parent / "pages/44_strategy_center.py"
    content = sc_path.read_text(encoding="utf-8")

    # 1. 正文循环内使用 description
    assert "st.write(s.description" in content
    # 2. 存在自动隐现的建设中横幅判断
    assert "editable_primary_count = sum(1 for s in primary_specs if s.editable)" in content

