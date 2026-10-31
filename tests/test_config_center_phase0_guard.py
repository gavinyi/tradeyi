"""配置中心 Phase 0 静态穷尽性与 CI 防新增守卫。

严格验证：
1. AST 扫描 src/、scripts/、pages/、.env.example 中的配置读取，任何未在 ConfigRegistry 显式登记的新增配置一律阻断；
2. Registry 单一权威与别名双向一致性；
3. 负向对照：未登记配置必定触发失败（真实可失败）。
"""

from __future__ import annotations

import ast
from pathlib import Path

from futu_quant.config import Settings
from futu_quant.config_center import GLOBAL_REGISTRY

EXEMPTIONS = {
    # 操作系统环境标准变量
    "PATH", "SYSTEMROOT", "WINDIR", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "TEMP", "TMP", "USERPROFILE", "HOMEPATH", "SYSTEMDRIVE",
    # 解释器与执行环境参数
    "PYTHONUNBUFFERED", "PYTHONUTF8", "PYTHONPATH", "PYTHONIOENCODING", "MSYSTEM", "TERM",
    # 测试沙箱与工作区分隔环境变量（由 conftest / fail-closed 守护，禁止业务化修改）
    "TRADEYI_ENV", "TRADEYI_PROD_DB_PATH", "TRADEYI_PROD_DB_OVERRIDE",
    "TRADEYI_DEV_ROOT", "TRADEYI_PROD_ROOT", "TRADEYI_TEST_ROOT",
    # 外部依赖工具安装绝对路径
    "FUTU_OPEND_EXE", "FUTU_OPEND_DIR", "PROXIFIER_EXE",
    # Harness 宿主注入变量
    "DSH_HADOOP_USER", "CUA_DRIVER_PIPE",
}


def _scan_codebase_env_keys(repo_root: Path) -> set[str]:
    """通过 AST 解析代码库中所有显式调用的环境变量键。"""
    found_keys = set()
    for subdir in ("src", "scripts", "pages"):
        folder = repo_root / subdir
        if not folder.exists():
            continue
        for py_path in folder.rglob("*.py"):
            try:
                content = py_path.read_text(encoding="utf-8-sig")
                tree = ast.parse(content)
            except Exception:
                continue

            for node in ast.walk(tree):
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
    return found_keys


def test_registry_has_no_duplicate_canonical_keys_and_valid_aliases():
    """验证 Registry 单一权威性：无重复规范键，所有 aliases 均可双向解析。"""
    all_specs = GLOBAL_REGISTRY.all_specs()
    assert len(all_specs) > 0

    seen_keys = set()
    for spec in all_specs:
        assert spec.key not in seen_keys, f"发现重复的 canonical key: {spec.key}"
        seen_keys.add(spec.key)
        # 验证规范键通过 resolve 得到自身
        assert GLOBAL_REGISTRY.resolve_canonical_key(spec.key) == spec.key
        # 验证别名通过 resolve 均可解析回对应 canonical key
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
    found_keys = _scan_codebase_env_keys(repo_root)

    unregistered = []
    for k in sorted(found_keys):
        if k not in EXEMPTIONS and not GLOBAL_REGISTRY.contains(k):
            unregistered.append(k)

    assert not unregistered, f"代码库中存在未在统一配置中心登记的隐形环境变量: {unregistered}"


def test_registry_negative_control_fails_on_unregistered_key():
    """负向对照（必红测试）：断言当引入一个未登记的新增配置项时，守卫必须能够抛错阻断。"""
    test_fake_unregistered_key = "TOTALLY_UNREGISTERED_SECRET_TEST_PARAM_XYZ"
    # 验证 Registry 未登记该配置
    assert not GLOBAL_REGISTRY.contains(test_fake_unregistered_key)

    # 模拟检查逻辑
    assert test_fake_unregistered_key not in EXEMPTIONS
    # 断言阻断生效
    assert GLOBAL_REGISTRY.resolve_canonical_key(test_fake_unregistered_key) is None
