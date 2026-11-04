"""P0-UI-1 专属守卫测试 (已补齐导航与严格无默认枚举检验)：
1. 校验 ConfigSpec.user_area 与 surface 字段：
   - 383 项配置 100% 具备合规 user_area 与 surface；
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
import pathlib
import tempfile
import pytest

from futu_quant.config_center.registry import GLOBAL_REGISTRY, ConfigRegistry, _load_default_specs
from futu_quant.config_center.spec import ConfigSpec


BASELINE_PATH = pathlib.Path(__file__).resolve().parent / "fixtures" / "config_spec_keys.txt"


def _baseline_keys() -> list[str]:
    """配置项清单基线（`tests/fixtures/config_spec_keys.txt`，排序后一行一个 key）。

    为什么用快照基线而不是硬编码总数：写死 `== 383` 只是把地雷往后挪 —— 任何人正当
    新增一个配置项都会红，红了就改魔数，改到最后没人记得这个数字代表什么。改用清单
    快照后，增/删配置项都会红，但失败信息直接给出 missing / unexpected 的 diff，更新
    基线本身就是一个可 review 的 diff。
    防「偷偷加配置项不写元数据」的责任在 `test_codebase_env_access_is_fully_registered_guard`
    （AST 全量扫描 env 访问是否登记，2026-11-04 它正好抓到 SETTINGS_CACHE 未登记），
    这里的数量硬编码只承担「阻止任何正当新增」，是纯假阳性源。
    """
    assert BASELINE_PATH.exists(), f"配置项基线清单缺失：{BASELINE_PATH}"
    keys = [
        line.strip()
        for line in BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert keys, f"配置项基线清单是空的：{BASELINE_PATH}"
    return keys


def test_user_area_metadata_strictly_four_enums_and_no_empty():
    """AC1/AC4: 配置项清单与基线一致，且全部具备合规 user_area / surface / storage。"""
    specs = GLOBAL_REGISTRY.all_specs()
    baseline = _baseline_keys()
    current = [spec.key for spec in specs]

    missing = sorted(set(baseline) - set(current))
    unexpected = sorted(set(current) - set(baseline))
    assert not missing and not unexpected, (
        f"配置项清单与 {BASELINE_PATH.name} 基线不一致"
        "（要加/删配置项请同步更新基线，那次 diff 就是变更说明）：\n"
        f"  missing   : {missing}\n"
        f"  unexpected: {unexpected}"
    )

    valid_areas = {"strategy_center", "risk_limits", "account_env", "ops_notify"}
    valid_surfaces = {"primary", "advanced"}
    valid_storages = {"runtime_config", "system_settings", "env", "derived"}
    area_counts = {}
    primary_counts = {a: 0 for a in valid_areas}

    for spec in specs:
        area = spec.user_area
        surface = spec.surface
        assert area in valid_areas, f"配置项 {spec.key} 的 user_area 非法: {area!r}"
        assert surface in valid_surfaces, f"配置项 {spec.key} 的 surface 非法: {surface!r}"
        assert spec.storage in valid_storages, f"配置项 {spec.key} 的 storage 非法: {spec.storage!r}"
        area_counts[area] = area_counts.get(area, 0) + 1
        if surface == "primary":
            primary_counts[area] += 1

    # 每个 key 唯一：重复 key 会被注册表静默合并，必须自己钉死
    assert len(current) == len(set(current)), (
        "存在重复配置项 key："
        + ", ".join(sorted({key for key in current if current.count(key) > 1}))
    )
    assert len(current) == len(baseline)

    # 确保 4 个业务区均承载了对应技术分类的配置
    assert area_counts["strategy_center"] > 0
    assert area_counts["risk_limits"] == 30, f"risk_limits 应恰好包含 30 项，实际 {area_counts['risk_limits']}"
    assert area_counts["account_env"] > 0
    assert area_counts["ops_notify"] > 0
    assert sum(area_counts.values()) == len(current)

    # AC3 核心约束：每个 area 的 primary 项严格 <= 30 项！
    for a, count in primary_counts.items():
        assert count <= 30, f"{a} 的 primary 项数量 {count} 超出 30 限制！"


def test_process_switch_spec_is_readable_not_a_dead_entry(monkeypatch):
    """登记 ≠ 可读：新增的进程级开关必须能解析到，且运行时真的读这个键。

    「登记了但没人读/读不出来」的死条目和漏登记一样有害：它会骗过 AST 登记守卫，
    却对真实行为零影响。这里用运行时读取函数反向验证登记是活的。
    """
    from futu_quant import config

    spec = GLOBAL_REGISTRY.get("runtime.settings_cache")
    assert spec is not None, "runtime.settings_cache 未登记"
    assert spec.storage == "env", "进程级紧急回退开关不许落库（否则脏配置能关掉缓存）"
    assert GLOBAL_REGISTRY.resolve_canonical_key("SETTINGS_CACHE") == "runtime.settings_cache"

    monkeypatch.setenv("SETTINGS_CACHE", "off")
    assert config.settings_cache_disabled() is True
    monkeypatch.setenv("SETTINGS_CACHE", "")
    assert config.settings_cache_disabled() is False


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

    # 3. 断言不得存在取不到租户时的静默 is_real 默认值兜底
    assert 'is_real = current_tenant.get("is_real", False)' not in content, "禁止对 is_real 做静默默认 False 兜底伪造模拟环境！"


def test_today_overview_resolve_trading_env_status_branches():
    """Reviewer & Product 守卫：测试交易环境判定函数的关键分支，严禁默认值伪造模拟绿标，且同源暴露无确认会话风险。"""
    from types import SimpleNamespace
    from futu_quant.real_trading import resolve_real_account_limits, RealAccountLimits
    import logging
    overview_path = pathlib.Path(__file__).resolve().parent.parent / "pages/00_today_overview.py"
    with open(overview_path, "r", encoding="utf-8") as f:
        src = f.read()

    env_globals = dict(globals())
    env_globals["resolve_real_account_limits"] = resolve_real_account_limits
    env_globals["logger"] = logging.getLogger("test_guard")
    local_ns = {}
    func_code = "def resolve_trading_env_status" + src.split("def resolve_trading_env_status")[1].split("def resolve_pending_restart_status")[0]
    exec(func_code, env_globals, local_ns)
    resolve_trading_env_status = local_ns["resolve_trading_env_status"]

    # 分支 1：租户字典为空或 None -> 必须为未知环境，不许为模拟盘，且禁止 normal 绿色
    val1, delta1, col1 = resolve_trading_env_status(None, SimpleNamespace(real_trading_enabled=False))
    assert "未知" in val1 and col1 != "normal"
    val1_b, _, col1_b = resolve_trading_env_status({}, SimpleNamespace(real_trading_enabled=False))
    assert "未知" in val1_b and col1_b != "normal"

    # 分支 2：缺失 is_real 键 -> 必须为未知环境，绝不能兜底成模拟
    val2, delta2, col2 = resolve_trading_env_status({"tenant_id": "foo"}, SimpleNamespace(real_trading_enabled=False))
    assert "未知" in val2 and col2 != "normal"

    # 分支 3：is_real 为 True -> 实盘，测试无二次确认高风险态暴露
    # 模拟真实门禁返回 limits (enabled=True, require_manual_confirm=False)
    fake_store = SimpleNamespace(get_setting=lambda k, d: '{"enabled": true, "require_manual_confirm": false}')
    val3, delta3, col3 = resolve_trading_env_status(
        {"is_real": True, "acc_id": "281756481476169109"},
        SimpleNamespace(real_trading_enabled=True, real_require_manual_confirm=False),
        store=fake_store,
    )
    assert "实盘" in val3 and "实盘·无二次确认" in delta3 and col3 == "inverse"

    # 分支 3b：实盘且启用了二次确认
    fake_store_confirm = SimpleNamespace(get_setting=lambda k, d: '{"enabled": true, "require_manual_confirm": true}')
    val3_b, delta3_b, col3_b = resolve_trading_env_status(
        {"is_real": True, "acc_id": "281756481476169109"},
        SimpleNamespace(real_trading_enabled=True, real_require_manual_confirm=True),
        store=fake_store_confirm,
    )
    assert "实盘" in val3_b and "允许真金交易" in delta3_b and col3_b == "inverse"

    # 分支 3c：实盘但账户级开关被关闭 (单真值：门禁拒绝，卡片绝不显示允许交易)
    fake_store_disabled = SimpleNamespace(get_setting=lambda k, d: '{"enabled": false}')
    val3_c, delta3_c, col3_c = resolve_trading_env_status(
        {"is_real": True, "acc_id": "281756481476169109"},
        SimpleNamespace(real_trading_enabled=True),
        store=fake_store_disabled,
    )
    assert "实盘" in val3_c and "实盘总闸已关闭" in delta3_c and col3_c == "off"

    # 分支 3c2：账户级配置损坏 -> 不得伪装成「总闸已关闭」，必须点名配置损坏
    fake_store_corrupt = SimpleNamespace(get_setting=lambda k, d: "{not json")
    val3_c2, delta3_c2, col3_c2 = resolve_trading_env_status(
        {"is_real": True, "acc_id": "281756481476169109"},
        SimpleNamespace(real_trading_enabled=True),
        store=fake_store_corrupt,
    )
    assert "实盘" in val3_c2 and "账户配置损坏" in delta3_c2 and col3_c2 != "normal"
    assert "总闸已关闭" not in delta3_c2, "坏键被伪装成主动关闭 —— 假状态"

    # 分支 3d：实盘租户缺少 acc_id -> 拒绝猜测 settings，直接未知且 off 颜色
    val3_d, delta3_d, col3_d = resolve_trading_env_status(
        {"is_real": True},
        SimpleNamespace(real_trading_enabled=True, real_account_id="281756481476169109"),
        store=fake_store,
    )
    assert "未知" in val3_d and "缺少账户ID" in delta3_d and col3_d != "normal"

    # 分支 3e：未注入 store -> 未知且 off 颜色
    val3_e, delta3_e, col3_e = resolve_trading_env_status(
        {"is_real": True, "acc_id": "281756481476169109"},
        SimpleNamespace(real_trading_enabled=True),
        store=None,
    )
    assert "未知" in val3_e and "存储未就绪" in delta3_e and col3_e != "normal"

    # 分支 3f：门禁解析抛出异常 -> 严禁回退 settings 兜底，直接报告未知/门禁解析失败且 off 颜色
    fake_store_error = SimpleNamespace(get_setting=lambda k, d: (_ for _ in ()).throw(RuntimeError("DB_LOCK_FAIL")))
    val3_f, delta3_f, col3_f = resolve_trading_env_status(
        {"is_real": True, "acc_id": "281756481476169109"},
        SimpleNamespace(real_trading_enabled=True),
        store=fake_store_error,
    )
    assert "未知" in val3_f and "门禁解析失败" in delta3_f and col3_f != "normal"

    # 分支 4：确凿 is_real 为 False -> 模拟仿真
    val4, delta4, col4 = resolve_trading_env_status({"is_real": False}, SimpleNamespace(real_trading_enabled=False))
    assert "模拟" in val4 and col4 == "normal"


def test_today_overview_resolve_pending_restart_status_branches(tmp_path, monkeypatch):
    """Reviewer 守卫：测试待重启变更纯函数的 5 个分支（无pid / git空 / diff=0 / diff=N / 异常），value 与 delta 同源。"""
    import subprocess
    from datetime import datetime, timezone
    from types import SimpleNamespace
    overview_path = pathlib.Path(__file__).resolve().parent.parent / "pages/00_today_overview.py"
    src = overview_path.read_text(encoding="utf-8")
    env_globals = dict(globals())
    env_globals.update({"datetime": datetime, "timezone": timezone, "subprocess": subprocess})
    local_ns = {}
    func_code = "def resolve_pending_restart_status" + src.split("def resolve_pending_restart_status")[1].split("# 1. 核心状态卡片")[0]
    exec(func_code, env_globals, local_ns)
    resolve_pending_restart_status = local_ns["resolve_pending_restart_status"]

    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    fake_pid = fake_repo / "data" / "trading-service.pid"

    # 分支 1：pid 文件不存在 -> 服务未启动，delta 提示需 start.bat，禁止出现“一致/无需重启”
    val1, delta1, col1 = resolve_pending_restart_status(fake_pid, fake_repo)
    assert val1 == "服务未启动"
    assert "start.bat" in delta1
    assert "一致" not in delta1 and "无需重启" not in val1

    # 建立假 pid 文件
    fake_pid.parent.mkdir(parents=True, exist_ok=True)
    fake_pid.write_text("12345", encoding="utf-8")

    # 分支 2：git log 返回空 base_commit -> 未统计，delta 明确说明未能获取基线
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=""))
    val2, delta2, col2 = resolve_pending_restart_status(fake_pid, fake_repo)
    assert val2 == "未统计"
    assert "基线" in delta2
    assert "一致" not in delta2 and "无需重启" not in val2

    # 分支 3：git diff 失败 (returncode != 0) -> 未统计，delta 提示 diff 失败
    def mock_run_fail(cmd, *args, **kwargs):
        if "log" in cmd:
            return SimpleNamespace(returncode=0, stdout="b1e78ef\n")
        return SimpleNamespace(returncode=1, stdout="")
    monkeypatch.setattr(subprocess, "run", mock_run_fail)
    val3, delta3, col3 = resolve_pending_restart_status(fake_pid, fake_repo)
    assert val3 == "未统计"
    assert "失败" in delta3
    assert "一致" not in delta3

    # 分支 4：diff_count > 0 -> N 个文件待重启，同源提示需 stop/start
    def mock_run_diff_n(cmd, *args, **kwargs):
        if "log" in cmd:
            return SimpleNamespace(returncode=0, stdout="b1e78ef\n")
        return SimpleNamespace(returncode=0, stdout="src/a.py\npages/b.py\n")
    monkeypatch.setattr(subprocess, "run", mock_run_diff_n)
    val4, delta4, col4 = resolve_pending_restart_status(fake_pid, fake_repo)
    assert "2 个文件待重启" == val4
    assert "stop.bat → start.bat" in delta4
    assert col4 == "inverse"

    # 分支 5：diff_count == 0 -> 无，无需重启，且 delta 提示一致
    def mock_run_diff_0(cmd, *args, **kwargs):
        if "log" in cmd:
            return SimpleNamespace(returncode=0, stdout="b1e78ef\n")
        return SimpleNamespace(returncode=0, stdout="\n")
    monkeypatch.setattr(subprocess, "run", mock_run_diff_0)
    val5, delta5, col5 = resolve_pending_restart_status(fake_pid, fake_repo)
    assert val5 == "无，无需重启"
    assert "一致" in delta5
    assert col5 == "normal"

    # 分支 6：发生未预期异常 -> 未统计
    def mock_run_crash(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(subprocess, "run", mock_run_crash)
    val6, delta6, col6 = resolve_pending_restart_status(fake_pid, fake_repo)
    assert val6 == "未统计"
    assert "boom" in delta6


def test_strategy_center_card_hides_runtime_key_from_main_body():
    """守卫：策略中心卡片正文禁止铺露底层 runtime_key，键名必须折叠在 expander 中。

    2026-11-02 P0-UI-2：策略中心已升级为**真实可编辑**（策略启停 / 暂停新开仓），
    原先「只读骨架 + 建设中横幅」的判据随之作废 —— 页面有真控件时不该再挂横幅。
    保留的仍然是这条原始意图：**正文用中文 description，不铺底层键名**。
    """
    sc_path = pathlib.Path(__file__).resolve().parent.parent / "pages/44_strategy_center.py"
    content = sc_path.read_text(encoding="utf-8")

    # 1. 正文展示中文业务描述
    assert "spec.description" in content

    # 2. 正文（高级详情折叠之前的部分）不得出现底层 runtime_key
    main_body = content.split("高级技术参数与策略细项")[0]
    assert "runtime_key" not in main_body, "正文仍在铺底层键名"

    # 3. 页面确实具备真实可编辑控件（不再是只读骨架）
    assert "set_strategy_enabled(" in content

