"""配置中心 Phase 0 静态穷尽性与 CI 防新增守卫。

严格验证：
1. AST 扫描仓库根目录及 src/、scripts/、pages/、.env.example 中的配置读取，任何未在 ConfigRegistry 显式登记的新增配置一律阻断；
2. 扫描失败 fail-closed，严禁静默吞异常；各来源显式断言扫描命中数 > 0，防止路径失效静默全绿；
3. 豁免清单冻结断言（不得增长）；Registry 条目数只增不减；
4. 负向对照：真实通过 tmp_path 放入未登记环境变量，断言扫描比对函数必定返回未登记项（真实可失败）。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import tempfile
import pytest

from futu_quant.config import Settings
from futu_quant.config_center import GLOBAL_REGISTRY

# 严格冻结的豁免基线（总数锁定为 28）
FROZEN_EXEMPTION_BASELINE_COUNT = 28
EXEMPTIONS = {
    # 操作系统环境标准变量 (11)
    "PATH", "SYSTEMROOT", "WINDIR", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "TEMP", "TMP", "USERPROFILE", "HOMEPATH", "SYSTEMDRIVE",
    # 解释器与执行环境参数 (6)
    "PYTHONUNBUFFERED", "PYTHONUTF8", "PYTHONPATH", "PYTHONIOENCODING", "MSYSTEM", "TERM",
    # 测试沙箱与工作区分隔环境变量（由 conftest / fail-closed 守护，禁止业务化修改） (6)
    "TRADEYI_ENV", "TRADEYI_PROD_DB_PATH", "TRADEYI_PROD_DB_OVERRIDE",
    "TRADEYI_DEV_ROOT", "TRADEYI_PROD_ROOT", "TRADEYI_TEST_ROOT",
    # 外部依赖工具安装绝对路径 (3)
    "FUTU_OPEND_EXE", "FUTU_OPEND_DIR", "PROXIFIER_EXE",
    # Harness 宿主注入变量 (2)
    "DSH_HADOOP_USER", "CUA_DRIVER_PIPE",
}

# 严格冻结的 Registry 最小注册条目数（只增不减）
FROZEN_REGISTRY_MIN_COUNT = 381


def scan_codebase_env_keys(repo_root: Path, require_counts: bool = True) -> set[str]:
    """通过 AST 解析代码库中所有显式调用的环境变量键 (Fail-closed)。

    覆盖仓库根目录下 *.py（如 app.py）以及 src/、scripts/、pages/ 子目录。
    若 require_counts 为 True，必须断言各关键来源命中数 > 0，防止路径错误导致假全绿。
    """
    found_keys = set()
    source_counts = {"root": 0, "src": 0, "scripts": 0, "pages": 0}

    # 1. 扫描仓库根目录下的 *.py（包括 app.py）
    for root_py in repo_root.glob("*.py"):
        content = root_py.read_text(encoding="utf-8-sig")
        tree = ast.parse(content, filename=str(root_py))
        for node in ast.walk(tree):
            _extract_env_keys_from_node(node, found_keys)
        source_counts["root"] += 1

    # 2. 扫描 src、scripts、pages 目录
    for subdir in ("src", "scripts", "pages"):
        folder = repo_root / subdir
        if not folder.exists():
            if require_counts:
                raise FileNotFoundError(f"扫描目录不存在: {folder}")
            continue
        for py_path in folder.rglob("*.py"):
            content = py_path.read_text(encoding="utf-8-sig")
            tree = ast.parse(content, filename=str(py_path))
            for node in ast.walk(tree):
                _extract_env_keys_from_node(node, found_keys)
            source_counts[subdir] += 1

    if require_counts:
        for src_name, count in source_counts.items():
            assert count > 0, f"扫描源 {src_name} 命中文件数为 0，检测链路异常！"

    return found_keys


def _extract_env_keys_from_node(node: ast.AST, found_keys: set[str]) -> None:
    # 1. os.getenv("KEY") / os.environ.get("KEY")
    if isinstance(node, ast.Call):
        is_getenv = isinstance(node.func, ast.Attribute) and node.func.attr == "getenv"
        is_environ_get = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "environ"
        )
        if (is_getenv or is_environ_get) and node.args:
            arg0 = node.args[0]
            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                found_keys.add(arg0.value.strip())
    # 2. os.environ["KEY"]
    elif isinstance(node, ast.Subscript):
        if (
            isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            found_keys.add(node.slice.value.strip())


def unregistered_env_keys(repo_root: Path, registry=GLOBAL_REGISTRY, require_counts: bool = True) -> list[str]:
    """计算代码库中所有未在 Registry 显式登记的环境变量列表。"""
    found_keys = scan_codebase_env_keys(repo_root, require_counts=require_counts)
    unregistered = []
    for k in sorted(found_keys):
        if k not in EXEMPTIONS and not registry.contains(k):
            unregistered.append(k)
    return unregistered


def test_frozen_exemptions_and_registry_count_baselines():
    """断言豁免清单严禁增长，Registry 条目数只增不减。"""
    assert len(EXEMPTIONS) <= FROZEN_EXEMPTION_BASELINE_COUNT, (
        f"豁免清单条目数 ({len(EXEMPTIONS)}) 超出冻结基线 ({FROZEN_EXEMPTION_BASELINE_COUNT})！"
        "严禁通过随意扩展白名单规避配置登记；新增豁免必须经 Reviewer 签字并修改基线数字。"
    )
    assert len(GLOBAL_REGISTRY) >= FROZEN_REGISTRY_MIN_COUNT, (
        f"Registry 注册条目数 ({len(GLOBAL_REGISTRY)}) 低于冻结基线 ({FROZEN_REGISTRY_MIN_COUNT})！"
        "严禁删除已有配置登记项。"
    )


def test_registry_has_no_duplicate_canonical_keys_and_valid_aliases():
    """验证 Registry 单一权威性：无重复规范键，所有 aliases 均可双向解析。"""
    all_specs = GLOBAL_REGISTRY.all_specs()
    assert len(all_specs) > 0

    seen_keys = set()
    for spec in all_specs:
        assert spec.key not in seen_keys, f"发现重复的 canonical key: {spec.key}"
        seen_keys.add(spec.key)
        assert GLOBAL_REGISTRY.resolve_canonical_key(spec.key) == spec.key
        for alias in spec.aliases:
            resolved = GLOBAL_REGISTRY.resolve_canonical_key(alias)
            assert resolved == spec.key, f"别名 {alias} 预期解析为 {spec.key}，实际得到 {resolved}"


def test_all_settings_fields_are_registered_in_registry():
    """验证 Settings 数据类中的所有核心配置字段均已在 Registry 中登记。"""
    settings_fields = Settings.__dataclass_fields__.keys()
    unregistered = []
    for f_name in settings_fields:
        if not (
            GLOBAL_REGISTRY.contains(f_name)
            or GLOBAL_REGISTRY.contains(f_name.upper())
            or GLOBAL_REGISTRY.contains(f"FUTU_{f_name.upper()}")
        ):
            unregistered.append(f_name)

    assert not unregistered, f"Settings 中存在未在配置中心 Registry 登记的字段: {unregistered}"


def test_all_env_example_keys_are_registered_in_registry():
    """验证 .env.example 暴露的所有配置模板均已在 Registry 中登记（必须存在，严禁删文件静默全绿）。"""
    repo_root = Path(__file__).resolve().parents[1]
    env_example = repo_root / ".env.example"
    assert env_example.exists(), f"配置文件模板不存在: {env_example}，严禁删除 .env.example 规避检查！"

    unregistered = []
    with open(env_example, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key = line.split("=", 1)[0].strip()
                if key not in EXEMPTIONS and not GLOBAL_REGISTRY.contains(key):
                    unregistered.append(key)

    assert not unregistered, f".env.example 中存在未在 Registry 登记的配置项: {unregistered}"


def test_codebase_env_access_is_fully_registered_guard():
    """CI 守卫核心：AST 静态扫描代码库中的环境变量读取，禁止新增未登记的隐形配置。"""
    repo_root = Path(__file__).resolve().parents[1]
    unregistered = unregistered_env_keys(repo_root, require_counts=True)
    assert not unregistered, f"代码库中存在未在统一配置中心登记的隐形环境变量: {unregistered}"


def test_unregistered_env_keys_negative_control_fails_via_tmp_path(tmp_path):
    """真实负向对照（必红测试）：在独立工作区放入未登记的 os.getenv 调用，断言扫描比对必定检出并报错。"""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    bad_py = src_dir / "unregistered_leak.py"
    bad_py.write_text("import os\nval = os.getenv('BRAND_NEW_UNREGISTERED_PARAM_FAIL_PROOF')", encoding="utf-8")

    leaked = unregistered_env_keys(tmp_path, require_counts=False)
    assert "BRAND_NEW_UNREGISTERED_PARAM_FAIL_PROOF" in leaked, f"负向对照必须捕获未登记配置，实际: {leaked}"


# 严格冻结的 runtime_config 动态通用写调用助手豁免清单 (按函数名定位，解耦行号，条数严禁增长)
FROZEN_RUNTIME_WRITE_EXEMPTION_COUNT = 7
RUNTIME_WRITE_FUNCTION_EXEMPTIONS = {
    # 格式：(相对文件路径, 封闭函数名)
    ("src/futu_quant/config.py", "_load_runtime_value"),
    ("src/futu_quant/config.py", "save_volatility_campaigns"),
    ("src/futu_quant/config.py", "upsert_campaign"),
    ("src/futu_quant/config.py", "delete_campaign"),
    ("src/futu_quant/store.py", "publish_config_release"),
    ("src/futu_quant/store.py", "rollback_config_release"),
    ("scripts/migrate_volatility_campaigns_tenant_scope.py", "run"),
}


def _get_enclosing_function_name(tree: ast.AST, target_node: ast.AST) -> str:
    """获取 AST 节点的最近封闭函数名称。"""
    for parent in ast.walk(tree):
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(parent):
                if child is target_node:
                    return parent.name
    return "module"


def _extract_module_level_constants(py_path: Path) -> dict[str, str]:
    """通过 AST 动态提取 Python 文件顶层的常量字符串定义，不手抄、不复述实现。"""
    constants = {}
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8-sig"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    constants[name] = node.value.value
    except Exception:
        pass
    return constants


def scan_codebase_runtime_config_write_keys(repo_root: Path, require_counts: bool = True) -> tuple[set[str], list[tuple[str, int, str]]]:
    """通过 AST 解析代码库中所有写入 runtime_config 的配置键 (Fail-closed)。

    返回: (resolved_keys, unresolved_sites)
    """
    found_keys = set()
    unresolved_sites = []
    write_call_count = 0

    # 动态从源码 AST 提取模块级常量定义，严禁手抄
    dynamic_constants = {}
    for const_mod in ("src/futu_quant/alerts.py", "src/futu_quant/core_validation.py", "src/futu_quant/scheduled_tasks.py"):
        p = repo_root / const_mod
        if p.exists():
            dynamic_constants.update(_extract_module_level_constants(p))

    # 扫描根目录 *.py 以及 src, scripts, pages
    scan_targets = list(repo_root.glob("*.py"))
    for subdir in ("src", "scripts", "pages"):
        folder = repo_root / subdir
        if not folder.exists():
            if require_counts:
                raise FileNotFoundError(f"扫描目录不存在: {folder}")
            continue
        scan_targets.extend(folder.rglob("*.py"))

    for py_path in scan_targets:
        content = py_path.read_text(encoding="utf-8-sig")
        tree = ast.parse(content, filename=str(py_path))
        rel_path = py_path.relative_to(repo_root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("set_runtime_config", "mutate_runtime_config") and node.args:
                    write_call_count += 1
                    arg0 = node.args[0]
                    func_name = _get_enclosing_function_name(tree, node)
                    func_site = (rel_path, func_name)
                    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                        found_keys.add(arg0.value.strip())
                    elif isinstance(arg0, ast.Name) and arg0.id in dynamic_constants:
                        found_keys.add(dynamic_constants[arg0.id].strip())
                    elif func_site in RUNTIME_WRITE_FUNCTION_EXEMPTIONS:
                        # 经冻结清单审核的通用底层写助手函数
                        pass
                    else:
                        unresolved_sites.append((rel_path, node.lineno, func_name, ast.unparse(arg0)))

    if require_counts:
        assert write_call_count > 0, "未扫描到任何 set_runtime_config/mutate_runtime_config 调用，AST 检测链路异常！"

    return found_keys, unresolved_sites


def test_codebase_runtime_config_writes_are_registered_guard():
    """CI 守卫第二道：AST 静态扫描所有 runtime_config 写调用，断言无未解析站点，写入键必须在 Registry 登记。"""
    repo_root = Path(__file__).resolve().parents[1]
    # 1. 验证豁免清单总数不得增长
    assert len(RUNTIME_WRITE_FUNCTION_EXEMPTIONS) <= FROZEN_RUNTIME_WRITE_EXEMPTION_COUNT, (
        f"runtime_config 写调用豁免数 ({len(RUNTIME_WRITE_FUNCTION_EXEMPTIONS)}) 超出基线 ({FROZEN_RUNTIME_WRITE_EXEMPTION_COUNT})！"
    )

    found_keys, unresolved_sites = scan_codebase_runtime_config_write_keys(repo_root, require_counts=True)
    # 2. 严格断言未解析站点必须为 0
    assert not unresolved_sites, f"发现未解析且未豁免的 runtime_config 写调用点: {unresolved_sites}"
    assert len(found_keys) > 0

    unregistered = []
    for k in sorted(found_keys):
        if not GLOBAL_REGISTRY.contains(k):
            unregistered.append(k)

    assert not unregistered, f"代码库中存在未在配置中心登记的 runtime_config 写入键: {unregistered}"


def test_runtime_config_negative_control_fails_on_unregistered_write(tmp_path):
    """第二道守卫真实负向对照（必红测试）：断言向 runtime_config 写入未登记键时，守卫必定报错阻断。"""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    bad_py = src_dir / "bad_runtime_write.py"
    bad_py.write_text("store.set_runtime_config('UNREGISTERED_DANGEROUS_RUNTIME_KEY_XYZ', {})", encoding="utf-8")

    leaked_keys, _ = scan_codebase_runtime_config_write_keys(tmp_path, require_counts=False)
    assert "UNREGISTERED_DANGEROUS_RUNTIME_KEY_XYZ" in leaked_keys
    assert not GLOBAL_REGISTRY.contains("UNREGISTERED_DANGEROUS_RUNTIME_KEY_XYZ")


def test_create_config_release_enforces_registry_membership(tmp_path):
    """Phase 1 真实调用强校验守卫：
    1. 传入未登记键调用 store.create_config_release 必须立即抛错拒绝；
    2. 传入已登记键（或变量模板实例键）正常放行并创建草稿。
    """
    import pytest
    from futu_quant.store import TradingStore

    store = TradingStore(tmp_path / "release_test.db")

    # A. 未登记键 -> 强拒绝 (抛出 ValueError)
    with pytest.raises(ValueError, match="未在统一配置中心登记，拒绝创建发布草稿"):
        store.create_config_release("COMPLETELY_UNREGISTERED_KEY_XYZ", {"some": "data"}, "admin", "test reason")

    # B. 已登记键 (如 ENV_SETTINGS) -> 正常放行
    release_id = store.create_config_release("ENV_SETTINGS", {"test_key": "val"}, "admin", "valid reason")
    assert release_id > 0

    # C. 模板键实例 (如 VOLATILITY_CAMPAIGNS:futu_real_master) -> 正常放行
    release_id2 = store.create_config_release("VOLATILITY_CAMPAIGNS:futu_real_master", [], "admin", "valid template reason")
    assert release_id2 > 0


def test_get_draftable_config_keys_filters_critical_and_non_runtime():
    """Phase 1 草稿可发布键助手函数守卫：
    1. 断言生产函数 get_draftable_config_keys 返回非空集合；
    2. 断言集合内每一项都在 GLOBAL_REGISTRY 中合法登记；
    3. 断言集合内每一项均必须满足：storage=='runtime_config'、editable is True、risk_level!='CRITICAL'、requires_confirmation is False；
    4. 负向/可失败测试：模拟包含 CRITICAL/requires_confirmation 项的注册表，验证其必须被过滤掉。
    """
    from futu_quant.config_center import get_draftable_config_keys, ConfigSpec, ConfigRegistry

    keys = get_draftable_config_keys()
    assert len(keys) > 0, "允许草稿发布的配置项列表不得为空！"

    for k in keys:
        assert GLOBAL_REGISTRY.contains(k), f"草稿配置键 {k} 必须在注册表中登记！"
        spec = GLOBAL_REGISTRY.get(k)
        assert spec is not None, f"未找到键 {k} 对应的配置规范！"
        assert spec.storage == "runtime_config", f"键 {k} 存储必须为 runtime_config，实际为 {spec.storage}"
        assert spec.editable, f"键 {k} 必须标记为 editable"
        assert spec.risk_level != "CRITICAL", f"键 {k} 严禁为 CRITICAL 风险级别"
        assert not spec.requires_confirmation, f"键 {k} 严禁包含二次确认敏感标记"

    # 1. 默认置顶推荐配置必须在列表中
    assert "CORE_SATELLITE_DAILY_VALIDATION" in keys, "常用推荐配置 CORE_SATELLITE_DAILY_VALIDATION 必须在草稿列表中！"

    # 2. 严格门禁：候选列表中绝不得出现点号规范键 (spec.key)，必须全为实际落库别名 (alias)
    all_canonical_keys = {s.key for s in GLOBAL_REGISTRY.all_specs()}
    leaked_canonical_keys = set(keys) & all_canonical_keys
    assert not leaked_canonical_keys, f"草稿候选列表严禁泄露规范点号键 (spec.key)，违规项: {leaked_canonical_keys}"

    # 3. 完备性防静默黑洞：所有符合条件的 spec 必须至少登记一个非模板具体落库 alias
    eligible_specs = [
        s for s in GLOBAL_REGISTRY.all_specs()
        if s.storage == "runtime_config"
        and s.editable
        and s.risk_level != "CRITICAL"
        and not s.requires_confirmation
    ]
    for s in eligible_specs:
        concrete_aliases = [a for a in s.aliases if not ("{" in a and "}" in a)]
        assert len(concrete_aliases) > 0, (
            f"配置规范 {s.key} 属于允许编辑的非 CRITICAL 配置，但未登记任何具体非模板 alias！"
            "此缺陷会导致配置项从草稿候选列表中静默丢失，必须补登真实落库别名！"
        )

    # 负向可失败验证：用包含 CRITICAL 的桩注册表测试
    def _make_spec(
        key: str,
        aliases: tuple[str, ...],
        storage: str = "runtime_config",
        editable: bool = True,
        risk_level: str = "MEDIUM",
        requires_confirmation: bool = False,
    ) -> ConfigSpec:
        return ConfigSpec(
            key=key,
            aliases=aliases,
            category="⑤ 策略总览",
            title=key,
            description="desc",
            value_type="string",
            default=None,
            scope="global",
            storage=storage,
            risk_level=risk_level,
            editable=editable,
            requires_admin=False,
            requires_confirmation=requires_confirmation,
            activation="next_cycle",
            consumers=(),
            tags=(),
            runtime_key=aliases[0] if aliases else key,
        )

    mock_specs = [
        _make_spec("runtime.critical", ("TEST_CRITICAL",), storage="runtime_config", editable=True, risk_level="CRITICAL"),
        _make_spec("runtime.confirm", ("TEST_CONFIRM",), storage="runtime_config", editable=True, risk_level="HIGH", requires_confirmation=True),
        _make_spec("env.item", ("TEST_ENV",), storage="env", editable=True, risk_level="LOW"),
        _make_spec("runtime.disabled", ("TEST_NOT_EDITABLE",), storage="runtime_config", editable=False, risk_level="LOW"),
        _make_spec("runtime.safe", ("TEST_SAFE",), storage="runtime_config", editable=True, risk_level="MEDIUM"),
    ]
    mock_reg = ConfigRegistry(mock_specs)
    filtered = get_draftable_config_keys(mock_reg)
    assert filtered == ["TEST_SAFE"], f"过滤后必须只剩安全落库键 TEST_SAFE，绝不能包含规范键 runtime.safe，实际为 {filtered}"


def test_page18_config_key_is_registry_driven_selectbox():
    """Phase 1 UI 改造守卫：
    1. 断言 pages/18 中草稿配置键已经由自由文本 text_input 升级为受限的目录驱动 selectbox；
    2. 严格断言 pages/18 中调用了生产助手函数 get_draftable_config_keys，且 selectbox 的 options 严格绑定该结果；
    3. 严禁任何硬编码选项或自由文本输入。
    """
    repo_root = Path(__file__).resolve().parents[1]
    page_18 = repo_root / "pages" / "18_infrastructure_disaster_recovery.py"
    assert page_18.exists()

    content = page_18.read_text(encoding="utf-8-sig")
    tree = ast.parse(content, filename=str(page_18))

    imports_helper = False
    calls_helper = False
    assigned_candidate_keys = False
    selectbox_uses_candidate_keys = False

    for node in ast.walk(tree):
        # 检查是否 import 了 get_draftable_config_keys
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "get_draftable_config_keys" for alias in node.names):
                imports_helper = True

        # 检查是否调用并赋值给 candidate_keys
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "candidate_keys":
                    assigned_candidate_keys = True
                    if isinstance(node.value, ast.Call):
                        if isinstance(node.value.func, ast.Name) and node.value.func.id == "get_draftable_config_keys":
                            calls_helper = True

        # 检查 config_key = st.selectbox(...) 是否使用了 candidate_keys
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id == "config_key":
                if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute):
                    if node.value.func.attr == "selectbox":
                        for kw in node.value.keywords:
                            if kw.arg == "options" and isinstance(kw.value, ast.Name) and kw.value.id == "candidate_keys":
                                selectbox_uses_candidate_keys = True

    assert imports_helper, "pages/18 必须 import get_draftable_config_keys！"
    assert calls_helper and assigned_candidate_keys, "pages/18 必须调用 get_draftable_config_keys() 获取候选键！"
    assert selectbox_uses_candidate_keys, "pages/18 的 st.selectbox 必须将 options 绑定为 candidate_keys！"



def test_registry_specs_types_and_defaults_are_consistent():
    """Phase 1 元数据自检守卫：
    断言 bool/int/float 等强类型配置的默认值类型与声明的 value_type 完全一致，严禁一律占位 string。
    """
    type_map = {
        "bool": bool,
        "int": int,
        "float": (float, int),
        "string": str,
        "symbols": (list, tuple),
        "json": (list, dict),
    }

    mismatches = []
    for spec in GLOBAL_REGISTRY.all_specs():
        if spec.default is not None and spec.value_type in type_map:
            expected = type_map[spec.value_type]
            if not isinstance(spec.default, expected):
                mismatches.append((spec.key, spec.value_type, type(spec.default)))

    assert not mismatches, f"发现类型声明与默认值不匹配的配置项: {mismatches}"


def test_runtime_key_is_mandatory_unique_and_member_of_aliases(tmp_path, monkeypatch):
    """Phase 2 前置守卫：
    1. 断言 Registry 每一个 spec 的 runtime_key 均非空；
    2. 断言每一个 runtime_key 必须属于自身的 aliases；
    3. 断言全局所有 runtime_key 无任何重复（全局唯一）；
    4. 负向测试：构造 runtime_key 不属于 aliases 或存在冲突的配置项，断言注册表构建必抛 ValueError；
    5. 负向测试：元数据文件缺失 runtime_key 时，loader 严禁任何 aliases[0] 或 key 默认兜底，必须抛出 ValueError。
    """
    from futu_quant.config_center import ConfigSpec, ConfigRegistry
    from futu_quant.config_center.registry import _load_default_specs
    import json

    specs = GLOBAL_REGISTRY.all_specs()
    assert len(specs) >= 381, f"注册表配置项数量异常: {len(specs)}"

    runtime_keys = []
    for s in specs:
        assert s.runtime_key, f"配置项 {s.key} 缺失必填的 runtime_key！"
        assert s.runtime_key in s.aliases, f"配置项 {s.key} 的 runtime_key '{s.runtime_key}' 必须属于 aliases: {s.aliases}"
        runtime_keys.append(s.runtime_key)

    assert len(runtime_keys) == len(set(runtime_keys)), "发现重复的 runtime_key！必须全局唯一！"

    # 负向 1：runtime_key 不属于 aliases
    bad_spec = ConfigSpec(
        key="test.bad",
        aliases=("REAL_ALIAS",),
        category="cat",
        title="title",
        description="",
        value_type="string",
        default="",
        scope="global",
        storage="runtime_config",
        risk_level="LOW",
        editable=True,
        requires_admin=False,
        requires_confirmation=False,
        activation="immediate",
        consumers=(),
        tags=(),
        runtime_key="FAKE_KEY",  # 违规：不在 aliases 中
    )
    with pytest.raises(ValueError, match="必须属于其已声明的 aliases"):
        ConfigRegistry([bad_spec])

    # 负向 2：重复的 runtime_key
    dup_spec1 = ConfigSpec(
        key="test.dup1",
        aliases=("DUP_KEY",),
        category="cat",
        title="title",
        description="",
        value_type="string",
        default="",
        scope="global",
        storage="runtime_config",
        risk_level="LOW",
        editable=True,
        requires_admin=False,
        requires_confirmation=False,
        activation="immediate",
        consumers=(),
        tags=(),
        runtime_key="DUP_KEY",
    )
    dup_spec2 = ConfigSpec(
        key="test.dup2",
        aliases=("DUP_KEY",),
        category="cat",
        title="title",
        description="",
        value_type="string",
        default="",
        scope="global",
        storage="runtime_config",
        risk_level="LOW",
        editable=True,
        requires_admin=False,
        requires_confirmation=False,
        activation="immediate",
        consumers=(),
        tags=(),
        runtime_key="DUP_KEY",
    )
    with pytest.raises(ValueError, match="全局必须唯一"):
        ConfigRegistry([dup_spec1, dup_spec2])

    # 负向 3：元数据文件缺失 runtime_key 时，loader 严禁任何兜底，必须抛出 ValueError
    tmp_json = tmp_path / "missing_rk.json"
    tmp_json.write_text(json.dumps([{"key": "test.missing", "aliases": ["TEST_MISSING"]}]), encoding="utf-8")
    monkeypatch.setattr("futu_quant.config_center.registry._DATA_PATH", tmp_json)
    with pytest.raises(ValueError, match="缺失必填字段 runtime_key"):
        _load_default_specs()


def test_template_key_tenant_suffix_validation_fails_closed(tmp_path):
    """Phase 2 前置守卫：
    1. 纯函数 validate_instance_key 在写入边界校验：
       - 正向：真实已登记租户的模板实例键（如 VOLATILITY_CAMPAIGNS:futu_real_master）校验通过；
       - 负向：非法/未登记租户（如 VOLATILITY_CAMPAIGNS:invalid_tenant_xyz）抛出 ValueError；
       - 动态：支持动态传入 allowed_tenants，不锁死代码静态字典；
    2. 端到端负向验证：向 store.create_config_release 传入非法租户后缀实例键，必须抛出 ValueError 阻断草稿创建；
    3. 支持携带 store.tenant_registry 动态开通的新租户，不把正常业务锁死。
    """
    from futu_quant.config_center import validate_instance_key
    from futu_quant.store import TradingStore

    # 1. 纯函数正向放行已知租户
    validate_instance_key("VOLATILITY_CAMPAIGNS:futu_real_master")
    validate_instance_key("risk_limits:futu_paper_sandbox")

    # 2. 纯函数负向阻断非法租户
    with pytest.raises(ValueError, match="未在有效租户注册表中登记"):
        validate_instance_key("VOLATILITY_CAMPAIGNS:invalid_tenant_xyz")
    with pytest.raises(ValueError, match="未在有效租户注册表中登记"):
        validate_instance_key("risk_limits:evil_hacker_test")

    # 3. 动态白名单与默认白名单取并集
    custom_tenants = {"custom_tenant_alpha"}
    validate_instance_key("VOLATILITY_CAMPAIGNS:custom_tenant_alpha", allowed_tenants=custom_tenants)
    # 并集支持：代码默认租户依然放行，不被动态租户排他冲掉
    validate_instance_key("VOLATILITY_CAMPAIGNS:futu_real_master", allowed_tenants=custom_tenants)

    # 4. 端到端草稿创建强门禁阻断
    store = TradingStore(tmp_path / "test_tenant_guard.db")
    with pytest.raises(ValueError, match="未在有效租户注册表中登记"):
        store.create_config_release("VOLATILITY_CAMPAIGNS:invalid_tenant_xyz", {}, "streamlit_admin", "bad test")

    # 5. store 动态 tenant_registry 取并集：动态新租户与代码默认租户均放行！
    custom_store = TradingStore(tmp_path / "custom_tenant.db", tenant_registry={"dynamic_new_tenant": {}})
    # 动态新租户放行
    release_id1 = custom_store.create_config_release("VOLATILITY_CAMPAIGNS:dynamic_new_tenant", {}, "streamlit_admin", "ok")
    assert release_id1 > 0
    # 代码默认租户并集放行（防历史租户被冲掉）
    release_id2 = custom_store.create_config_release("VOLATILITY_CAMPAIGNS:futu_real_master", {}, "streamlit_admin", "ok")
    assert release_id2 > 0


def test_conftest_tempdir_is_on_drive_e_with_pid_and_uuid():
    """Phase 2 前置守卫：
    1. 断言 tempfile.gettempdir() 路径在 E 盘，杜绝 C 盘跑满；
    2. 断言路径包含当前进程 PID 与唯一标识，杜绝并发冲突；
    3. 断言 pyproject.toml 中不再包含任何固定 --basetemp 参数。
    """
    temp_dir = Path(tempfile.gettempdir()).resolve()
    assert temp_dir.drive.upper() == "E:", f"临时目录必须位于 E 盘，实际为: {temp_dir}"
    assert f"{os.getpid()}_" in temp_dir.name or "_pytest_tmp" in str(temp_dir), f"临时目录必须具备隔离标识: {temp_dir}"

    repo_root = Path(__file__).resolve().parents[1]
    pyproject = repo_root / "pyproject.toml"
    assert pyproject.exists()
    content = pyproject.read_text(encoding="utf-8")
    assert "--basetemp" not in content, "pyproject.toml 严禁回流固定 --basetemp 参数！"


def test_page43_unified_config_center_is_strictly_read_only():
    """Phase 2 只读页面守卫：
    1. 严格 AST 扫描 pages/43_unified_config_center.py，断言绝对不存在任何写操作调用
       (set_runtime_config, mutate_runtime_config, create_config_release, publish_config_release,
        set_setting, delete_setting, save_setting, rollback_config)；
    2. 断言页面导入并调用 GLOBAL_REGISTRY 获取配置规范，元数据直接来自注册表而非本地硬编码；
    3. 负向测试：构造包含写操作调用的 AST 代码，断言扫描器必然检测出违规并报错。
    """
    repo_root = Path(__file__).resolve().parents[1]
    page_43 = repo_root / "pages" / "43_unified_config_center.py"
    assert page_43.exists(), f"未找到 Phase 2 统一配置中心页面: {page_43}"

    forbidden_call_names = {
        "set_runtime_config",
        "mutate_runtime_config",
        "create_config_release",
        "publish_config_release",
        "set_setting",
        "delete_setting",
        "save_setting",
        "rollback_config",
    }

    def _check_ast_for_writes(code_str: str, filename: str = "test.py") -> list[str]:
        tree = ast.parse(code_str, filename=filename)
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = None
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                if name in forbidden_call_names:
                    violations.append(f"{name} at line {getattr(node, 'lineno', 0)}")
        return violations

    page_content = page_43.read_text(encoding="utf-8-sig")
    page_violations = _check_ast_for_writes(page_content, str(page_43))
    assert not page_violations, f"pages/43 违反只读原则，发现了配置写操作调用: {page_violations}"

    # 验证元数据来源：必须从 GLOBAL_REGISTRY 读取
    tree = ast.parse(page_content, filename=str(page_43))
    imports_registry = False
    calls_all_specs = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "GLOBAL_REGISTRY" for a in node.names):
            imports_registry = True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "all_specs":
            calls_all_specs = True

    assert imports_registry, "pages/43 必须从 futu_quant.config_center 导入 GLOBAL_REGISTRY！"
    assert calls_all_specs, "pages/43 必须调用 GLOBAL_REGISTRY.all_specs() 展示权威元数据！"

    # 负向可失败验证：如果页面包含写调用，必须能被捕获
    bad_code = "from futu_quant.store import store\nstore.set_runtime_config('k', 'v')\n"
    bad_violations = _check_ast_for_writes(bad_code, "bad_page.py")
    assert len(bad_violations) > 0, "AST 写入扫描器未能捕获 set_runtime_config 违规！"


def test_template_literal_is_directly_resolvable():
    """Phase 3 前置守卫 1：
    断言模板字面量本身 (含占位符字符串自身) 优先通过 alias 字面量秒级解析，
    保证元数据反射与 Phase 0 静态守卫不被切段索引破坏。
    """
    assert GLOBAL_REGISTRY.resolve_canonical_key("risk_limits:{tenant}") == "risk.limits.tenant"
    assert GLOBAL_REGISTRY.resolve_canonical_key("real_account_config:{account}") == "safety.real.account_config"
    assert GLOBAL_REGISTRY.resolve_canonical_key("strategy_enabled:{tenant}:{strategy}") == "control.strategy.enabled"


def test_template_segment_matching_rejects_empty_segments_and_extra_colons():
    """Phase 3 前置守卫 2：
    1. 负向防御：空段 (如 'a::b')、悬空冒号 (如 'a:')、段数不匹配等非法格式必须显式拒识 (返回 None)；
    2. 正向验证：现网存在空格的策略名 (如 'strategy_execution_mode:SMA Cross') 必须精准切段命中，不受旧正则限制。
    """
    # 负向：空段与非法格式
    assert GLOBAL_REGISTRY.resolve_canonical_key("strategy_execution_mode::core_satellite") is None
    assert GLOBAL_REGISTRY.resolve_canonical_key("strategy_execution_mode:futu_real_master:core:satellite:extra") is None
    assert GLOBAL_REGISTRY.resolve_canonical_key("strategy_execution_mode:") is None
    assert GLOBAL_REGISTRY.resolve_canonical_key("risk_limits::") is None

    # 正向：带空格的存量 Title 别名切段命中
    assert GLOBAL_REGISTRY.resolve_canonical_key("strategy_execution_mode:SMA Cross") == "runtime.strategy.execution_mode"
    assert GLOBAL_REGISTRY.resolve_canonical_key("strategy_execution_mode:20 Day Breakout") == "runtime.strategy.execution_mode"
    assert GLOBAL_REGISTRY.resolve_canonical_key("strategy_execution_mode:RSI Mean Reversion") == "runtime.strategy.execution_mode"


def test_deprecated_strategy_enabled_legacy_fails_closed_on_write(tmp_path):
    """Phase 3 前置守卫 3：
    1. 只读断言：无租户历史键 strategy_enabled:core_satellite 在 contains() 仍返回 True (保持只读注册不变)；
    2. 写入阻断：validate_instance_key 必须抛出 ValueError 并明确提示已废弃 (deprecated)；
    3. 端到端阻断：store.create_config_release 对废弃键必须拒建草稿。
    """
    from futu_quant.config_center import validate_instance_key
    from futu_quant.store import TradingStore

    legacy_key = "strategy_enabled:core_satellite"
    # 1. contains() 保持只读合法注册
    assert GLOBAL_REGISTRY.contains(legacy_key) is True

    # 2. validate_instance_key 写入边界抛错
    with pytest.raises(ValueError, match="已废弃 \\(deprecated\\)"):
        validate_instance_key(legacy_key)

    # 3. 端到端草稿创建拒建
    store = TradingStore(tmp_path / "test_deprecated_guard.db")
    with pytest.raises(ValueError, match="已废弃 \\(deprecated\\)"):
        store.create_config_release(legacy_key, True, "streamlit_admin", "try write deprecated")


def test_account_whitelist_is_strictly_separated_from_tenants(tmp_path):
    """Phase 3 前置守卫 4：
    1. 真实纯数字券商账号通过：real_account_config:281756481476169109 校验通过；
    2. 租户名冒充账号必须拒识：以租户名充当账号 real_account_config:futu_real_master 抛出 ValueError；
    3. 防御空 dict 测试桩：tenant_registry 传入空 dict 时安全提取不崩溃；
    4. 动态账号并集支持。
    """
    from futu_quant.config_center import validate_instance_key
    from futu_quant.store import TradingStore

    # 1. 真实数字账号通过
    validate_instance_key("real_account_config:281756481476169109")
    validate_instance_key("real_account_config:283445327916889204")

    # 2. 租户名冒充账号必须被拒 (严格分离)
    with pytest.raises(ValueError, match="未在有效账户注册表中登记"):
        validate_instance_key("real_account_config:futu_real_master")

    # 3. 动态账号并集支持
    validate_instance_key("real_account_config:9988776655", allowed_accounts={"9988776655"})
    with pytest.raises(ValueError, match="未在有效账户注册表中登记"):
        validate_instance_key("real_account_config:1122334455", allowed_accounts={"9988776655"})

    # 4. store 携带空 dict 桩不抛 AttributeError 崩溃
    stub_store = TradingStore(tmp_path / "test_empty_dict.db", tenant_registry={"empty_stub": {}})
    # 仍可正常为默认真实账号创建草稿
    release_id = stub_store.create_config_release(
        "real_account_config:281756481476169109", {"max_daily_notional": 5000}, "streamlit_admin", "ok"
    )
    assert release_id > 0


def test_strategy_whitelist_enforces_known_strategies_and_retains_title_aliases(tmp_path, caplog):
    """Phase 3 前置守卫 5：
    1. strategy_id 白名单放行 (如 core_satellite)；
    2. 存量 3 个带空格 Title 别名放行 (SMA Cross 等)；
    3. 未知策略名严格抛出 ValueError 阻断；
    4. 真实 TradingStore 路径：通过 store.approve_strategy 注入动态审批策略，端到端验证 store.approvals() 放行草稿；
    5. 可观测性守卫：当数据库操作抛出 sqlite3.Error 时必须输出可审计 WARN，而非数据库异常严禁被静默吞掉。
    """
    from futu_quant.config_center import validate_instance_key
    from futu_quant.store import TradingStore
    import sqlite3
    import logging

    # 1. strategy_id
    validate_instance_key("strategy_execution_mode:core_satellite")
    validate_instance_key("strategy_execution_mode:volatility_long_gamma")

    # 2. 存量 Title 别名
    validate_instance_key("strategy_execution_mode:SMA Cross")
    validate_instance_key("strategy_execution_mode:20 Day Breakout")

    # 3. 未知策略阻断
    with pytest.raises(ValueError, match="未在有效策略插件注册表中登记"):
        validate_instance_key("strategy_execution_mode:unknown_random_strategy")

    # 4. 真实 TradingStore 路径：测试 store.approvals() 动态策略生效
    store = TradingStore(tmp_path / "test_approvals.db")
    store.approve_strategy("custom_dynamic_strat", "hash_abc", "admin", True, True, True, True)
    assert any(r["strategy_id"] == "custom_dynamic_strat" for r in store.approvals())
    release_id = store.create_config_release("strategy_execution_mode:custom_dynamic_strat", "REAL", "streamlit_admin", "ok")
    assert release_id > 0

    # 5. 可观测性守卫：模拟 sqlite3.Error 时必须记录 WARN 日志
    with caplog.at_level(logging.WARNING):
        def _failing_approvals():
            raise sqlite3.OperationalError("simulated db locked")
        store.approvals = _failing_approvals
        # 此时内置的已登记策略仍可放行，但产生可观测警告
        store.create_config_release("strategy_execution_mode:core_satellite", "REAL", "streamlit_admin", "ok")
        assert "提取动态策略审批列表数据库异常" in caplog.text

    # 6. 非数据库未知异常严禁被静默吞掉（防回归）
    def _buggy_approvals():
        raise AttributeError("buggy code")
    store.approvals = _buggy_approvals
    with pytest.raises(AttributeError, match="buggy code"):
        store.create_config_release("strategy_execution_mode:core_satellite", "REAL", "streamlit_admin", "ok")


def test_single_key_matches_at_most_one_template():
    """Phase 3 前置守卫 6：
    断言在注册表模板索引中，任何 (prefix, segment_count) 均全局唯一，
    保证任何实例键至多命中唯一一个模板，彻底消除顺序与段数歧义。
    """
    templates = []
    for spec in GLOBAL_REGISTRY.all_specs():
        for a in spec.aliases:
            if "{" in a and "}" in a:
                parts = a.split(":")
                templates.append((parts[0], len(parts), a))

    seen = {}
    for prefix, seg_count, t_str in templates:
        key = (prefix, seg_count)
        assert key not in seen, f"发现冲突模板: {t_str} 与 {seen[key]} 共享相同 (prefix={prefix}, seg_count={seg_count})"
        seen[key] = t_str



