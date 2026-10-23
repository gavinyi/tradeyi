"""全局测试隔离与沙箱临时目录兼容。

生产 runtime_config（data/trading.db）里的动态设置会被
`Settings.load(refresh=True)` 折进 os.environ（见 config.refresh_runtime_environment），
monkeypatch 只还原它自己设置的键，于是 EXECUTION_MODE=REAL、REAL_ACCOUNT_ID 等
生产配置会泄漏给同进程后续测试，造成跨用例污染。

这里在每条用例前后对整个 os.environ 做快照/还原，同时把 config.ENV_FILE 指向
一个空文件，彻底切断测试进程与生产 .env / 生产 SQLite 的联系。
"""

from __future__ import annotations

import logging.handlers
import os
import pathlib
import sys
import tempfile
from pathlib import Path

# 统一重定向 tempfile 根目录至工作区内部，杜绝在系统 Temp 目录下创建 sqlite 数据库导致的 WinError 5 沙箱拦截
_qa_dir = pathlib.Path("data/qa").resolve()
_qa_dir.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(_qa_dir)
os.environ["TMP"] = str(_qa_dir)
os.environ["TEMP"] = str(_qa_dir)
os.environ["TMPDIR"] = str(_qa_dir)

# Windows 下若在 mkdir 显式传入 0o700/0o600 等 UNIX 权限，Python 底层会生成受限的 DACL 安全描述符，
# 剥离沙箱令牌访问权，导致后续 sqlite3 无法创建数据库文件。在此去除显式 mode，统一继承父目录完整 ACL。
_orig_path_mkdir = pathlib.Path.mkdir
def _safe_path_mkdir(self, mode=None, parents=False, exist_ok=False):
    return _orig_path_mkdir(self, parents=parents, exist_ok=exist_ok)
pathlib.Path.mkdir = _safe_path_mkdir

_orig_os_mkdir = os.mkdir
def _safe_os_mkdir(path, mode=None, *args, **kwargs):
    return _orig_os_mkdir(path, *args, **kwargs)
os.mkdir = _safe_os_mkdir

# tempfile 内部使用 `import os as _os`，直接修补其内部私有引用
_orig_tempfile_os_mkdir = getattr(tempfile, "_os", os).mkdir
def _safe_tempfile_os_mkdir(path, mode=None, *args, **kwargs):
    return _orig_tempfile_os_mkdir(path, *args, **kwargs)
if hasattr(tempfile, "_os"):
    tempfile._os.mkdir = _safe_tempfile_os_mkdir
if hasattr(tempfile, "_resetperms"):
    tempfile._resetperms = lambda path: None

# Windows 默认以 GBK 解码文件，测试读取带有中文注释的代码时会抛 UnicodeDecodeError，统一默认 UTF-8
_orig_read_text = pathlib.Path.read_text
def _safe_read_text(self, encoding=None, errors=None):
    if encoding is None:
        encoding = "utf-8"
    return _orig_read_text(self, encoding=encoding, errors=errors)
pathlib.Path.read_text = _safe_read_text

# futu 库 import 时默认写 C:\Users\Administrator\AppData\Roaming\com.futunn.FutuOpenD\Log，
# 在 workspace-write 沙箱下会因越权写抛出 PermissionError，在此安全重定向至工作区内。
_orig_trfh_init = logging.handlers.TimedRotatingFileHandler.__init__
def _safe_trfh_init(self, filename, *args, **kwargs):
    s_fn = str(filename)
    if "com.futunn.FutuOpenD" in s_fn or ("AppData" in s_fn and "Log" in s_fn):
        filename = str(_qa_dir / "futu_test.log")
    return _orig_trfh_init(self, filename, *args, **kwargs)
logging.handlers.TimedRotatingFileHandler.__init__ = _safe_trfh_init

import pytest  # noqa: E402  # 必须在上述 monkeypatch 之后导入：先装好兼容补丁再进入 pytest 装载流程

# 会话守卫的生产数据源固定于导入时的 DEFAULT_DB；测试只能 monkeypatch 此模块变量。
# 禁止环境变量接管数据源：否则一次遗留的验证环境会让真实生产写穿静默变绿。
from futu_quant.store import DEFAULT_DB as _DEFAULT_PROD_DB  # noqa: E402

_SESSION_PROD_BASELINE: tuple[str, tuple[tuple[str, int, str], ...]] | None = None

# 隔离环境（TRADEYI_ENV=test）下，本工作区的 DEFAULT_DB 是**测试库**，不是生产库。
# 守卫必须显式知道生产库在哪，否则会把测试库当生产库：要么假绿（测的是副本），
# 要么因测试库不存在而误红。两者都会让「生产是否被写穿」这个问题失去答案。
if os.environ.get("TRADEYI_ENV", "").strip().lower() == "test":
    _prod_guard_env = os.environ.get("TRADEYI_PROD_DB_PATH", "").strip()
    if not _prod_guard_env:
        raise pytest.UsageError(
            "[PROD-GUARD] TRADEYI_ENV=test 时必须显式提供 TRADEYI_PROD_DB_PATH"
            "（生产库绝对路径），否则无法判断「生产是否被测试写穿」—— fail-closed。"
        )
    _PROD_GUARD_DB_PATH = Path(_prod_guard_env).resolve()
else:
    _PROD_GUARD_DB_PATH = Path(_DEFAULT_PROD_DB).resolve()


def production_db_path() -> Path:
    """生产库路径（供断言"生产未被写穿"的用例使用）。

    - 生产目录（未设 ``TRADEYI_ENV=test``）：源文件相对的 ``DEFAULT_DB``；
    - 隔离测试区（``TRADEYI_ENV=test``）：``TRADEYI_PROD_DB_PATH`` 指定的真生产库 ——
      因为该工作区的 ``DEFAULT_DB`` 是测试库，直接拿它当"生产库"会假绿。
    """
    return _PROD_GUARD_DB_PATH


def prod_config_fingerprint() -> tuple[str, tuple[tuple[str, int, str], ...]] | None:
    """只读指纹整张生产 ``runtime_config``；不可读返回 ``None``，由守卫硬失败。

    返回整体 SHA 与逐行 ``(config_key, version, value_json)`` 快照。后者仅用于输出
    紧凑、可审计的差异；它不是可配置的测试数据源。
    """
    import hashlib
    import sqlite3

    try:
        connection = sqlite3.connect(f"file:{_PROD_GUARD_DB_PATH.as_posix()}?mode=ro", uri=True)
        try:
            rows = tuple(
                (str(key), int(version), str(value_json))
                for key, version, value_json in connection.execute(
                    "SELECT config_key, version, value_json FROM runtime_config ORDER BY config_key"
                ).fetchall()
            )
        finally:
            connection.close()
    except Exception:
        return None
    if not rows:
        return None
    payload = "\n".join(f"{key}\x1f{version}\x1f{value}" for key, version, value in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), rows


def db_path_under_guard() -> str:
    """会话守卫实际读取的固定生产路径（仅测试可 monkeypatch 模块变量）。"""
    return str(_PROD_GUARD_DB_PATH)


def _compact_runtime_diff(
    before: tuple[str, tuple[tuple[str, int, str], ...]],
    after: tuple[str, tuple[tuple[str, int, str], ...]],
) -> str:
    """输出最多 8 项变更，避免 SHA 告警不可读；不以内容形态决定是否失败。"""
    import json

    old = {key: (version, value) for key, version, value in before[1]}
    new = {key: (version, value) for key, version, value in after[1]}
    changes: list[str] = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) == new.get(key):
            continue
        old_ver = old.get(key, ("缺失", ""))[0]
        new_ver = new.get(key, ("缺失", ""))[0]
        detail = ""
        if key == "VOLATILITY_CAMPAIGNS":
            try:
                campaigns = json.loads(new.get(key, (0, "[]"))[1])
                preview = [
                    f"{c.get('tenant_id') or '-'}:{c.get('code') or c.get('campaign_id') or '?'}"
                    f"(ratio={c.get('max_position_loss_ratio')},usd={c.get('max_position_loss_usd')})"
                    for c in campaigns[:4] if isinstance(c, dict)
                ]
                detail = f" campaigns={','.join(preview) or '[]'}"
            except Exception:
                detail = " campaigns=<无法解析>"
        changes.append(f"{key}: version {old_ver}->{new_ver}{detail}")
    return "\n".join(f"  - {item}" for item in changes[:8]) or "  - 指纹变化但无法生成行级差异"


def prod_config_violation(
    before: tuple[str, tuple[tuple[str, int, str], ...]] | None,
    after: tuple[str, tuple[tuple[str, int, str], ...]] | None,
) -> str | None:
    """整表变化、override 或指纹不可读均为硬违规。"""
    if os.environ.get("TRADEYI_PROD_DB_OVERRIDE"):
        return "生产守卫禁止 TRADEYI_PROD_DB_OVERRIDE：环境变量会使守卫可被静默绕过。"
    if before is None or after is None:
        return f"生产 runtime_config 指纹不可读（数据源：{db_path_under_guard()}）—— fail-closed。"
    if before == after:
        return None
    return (
        "生产 runtime_config 在 pytest 会话期间被改动！\n"
        f"  数据源：{db_path_under_guard()}\n"
        f"  会话前：sha={before[0][:16]}\n"
        f"  会话后：sha={after[0][:16]}\n"
        f"  差异：\n{_compact_runtime_diff(before, after)}\n"
        "  —— 未显式审计许可的生产配置变化一律硬失败。"
    )


def pytest_sessionstart(session):
    """会话开始：记整张生产 `runtime_config` 的指纹；override 立即硬红。"""
    global _SESSION_PROD_BASELINE
    override = os.environ.get("TRADEYI_PROD_DB_OVERRIDE")
    if override:
        message = (
            "[PROD-GUARD] 禁止 TRADEYI_PROD_DB_OVERRIDE（会话开始即硬红）："
            f"守卫数据源固定为生产库 {_PROD_GUARD_DB_PATH}，不接受环境变量重定向 —— "
            f"实测值 {override!r}。变异单测请 monkeypatch conftest._PROD_GUARD_DB_PATH。"
        )
        print(f"\n{message}", file=sys.stderr, flush=True)
        raise pytest.UsageError(message)
    _SESSION_PROD_BASELINE = prod_config_fingerprint()
    if _SESSION_PROD_BASELINE is None:
        print(
            f"[PROD-GUARD] 生产 runtime_config 指纹不可读（{_PROD_GUARD_DB_PATH}）"
            "—— 会话结束时将按 fail-closed 硬红。",
            file=sys.stderr, flush=True,
        )


def pytest_sessionfinish(session, exitstatus):
    """会话结束：比对**整张** `runtime_config` 指纹，变了/不可读就**红**。

    这是**独立第二道**：它不看"调用点有没有传 store"，只看**生产库变没变** ——
    因此能抓住任何路径，包括绕过 `TradingStore` 的裸 sqlite3 写。
    """
    global _SESSION_PROD_BASELINE
    violation = prod_config_violation(_SESSION_PROD_BASELINE, prod_config_fingerprint())
    _SESSION_PROD_BASELINE = None
    if violation:
        print(f"\n[PROD-GUARD] {violation}", file=sys.stderr, flush=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED

_SNAPSHOT_KEYS: tuple[str, ...] = tuple(sorted(os.environ.keys()))
_SNAPSHOT_VALUES: dict[str, str] = dict(os.environ)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch, tmp_path):
    # 每条用例前：ENV_FILE 指向空临时文件，生产 .env / 生产 runtime_config 不再进入进程
    env_file = tmp_path / "isolation.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr("futu_quant.config.ENV_FILE", env_file)

    # 每条用例前：DB 路径也隔离 —— 这是**写穿生产**的根因修复。
    #
    # 为什么必须包 `__init__`：`TradingStore.__init__(path=DEFAULT_DB)` 的默认值
    # 在**类定义时**绑定，改 `store.DEFAULT_DB` 常量**无效**。
    # 而 `config.upsert_campaign` / `delete_campaign` 在 `store=None` 时
    # `store = store or _TradingStore()` —— 不传 store 就直连生产库。
    #
    # 10-23 事故：`tests/test_campaign_state_write_read_symmetry.py:76` 不传 store，
    # 生产 `VOLATILITY_CAMPAIGNS` 被整表清空（8 条真实 campaign → 1 条测试夹具）。
    # 修个案不够 —— **任何**忘记传 store 的调用点都会重演，所以在唯一的实例化点堵住。
    from futu_quant.store import DEFAULT_DB as _PROD_DB, TradingStore as _TradingStore

    _real_init = _TradingStore.__init__

    def _isolated_init(self, path=None, tenant_registry=None):
        if path is not None and Path(path).resolve() != Path(_PROD_DB).resolve():
            _real_init(self, path, tenant_registry)
        else:
            _real_init(self, tmp_path / "isolated.db", tenant_registry)

    monkeypatch.setattr(_TradingStore, "__init__", _isolated_init)

    yield

    # 每条用例后：恢复完整 os.environ 快照，保留 pytest 自身控制变量
    for key in list(os.environ.keys()):
        if key not in _SNAPSHOT_KEYS and not key.startswith("PYTEST_"):
            del os.environ[key]
    for key, value in _SNAPSHOT_VALUES.items():
        if os.environ.get(key) != value:
            os.environ[key] = value
