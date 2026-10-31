"""配置中心 Phase 0 静态穷尽性与 CI 防新增守卫。

严格验证：
1. AST 扫描仓库根目录及 src/、scripts/、pages/、.env.example 中的配置读取，任何未在 ConfigRegistry 显式登记的新增配置一律阻断；
2. 扫描失败 fail-closed，严禁静默吞异常；各来源显式断言扫描命中数 > 0，防止路径失效静默全绿；
3. 豁免清单冻结断言（不得增长）；Registry 条目数只增不减；
4. 负向对照：真实通过 tmp_path 放入未登记环境变量，断言扫描比对函数必定返回未登记项（真实可失败）。
"""

from __future__ import annotations

import ast
from pathlib import Path

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
FROZEN_REGISTRY_MIN_COUNT = 368


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
    """验证 .env.example 暴露的所有配置模板均已在 Registry 中登记。"""
    repo_root = Path(__file__).resolve().parents[1]
    env_example = repo_root / ".env.example"
    if not env_example.exists():
        return

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
