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

# 统一重定向 tempfile 根目录至工作区内部 E 盘 (data/qa/_pytest_tmp_<pid>_<uuid8>)，
# 彻底杜绝回落至 C 盘导致的磁盘跑满 (2026-10-27 事故)，且保证多进程并发执行绝对唯一无冲突。
_qa_dir = pathlib.Path("data/qa").resolve()
try:
    _qa_dir.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

import uuid as _conftest_uuid  # noqa: E402  # 必须在 tempfile 根重定向之后
_session_tmp_dir = _qa_dir / f"_pytest_tmp_{os.getpid()}_{_conftest_uuid.uuid4().hex[:8]}"
try:
    _session_tmp_dir.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(_session_tmp_dir)
    os.environ["TMP"] = str(_session_tmp_dir)
    os.environ["TEMP"] = str(_session_tmp_dir)
    os.environ["TMPDIR"] = str(_session_tmp_dir)
except Exception:
    pass

# 测试环境：允许临时库 bootstrap（生产路径必须显式 bootstrap=True）
os.environ.setdefault("TRADEYI_ALLOW_BOOTSTRAP", "1")


def _assert_code_origin() -> None:
    """验收前置（P0-5 §8.0）：断言 `futu_quant` 解析到**本 worktree**。

    为什么必须有：共享 editable venv 的 `.pth` 会把 `futu_quant` 指向**生产目录**
    （`E:\\Thisyi\\trade\\src`），于是「把过滤器关掉 → 守卫转红」这类**变异测试**
    可能跑的是生产代码，给出**假绿**。
    **验收如果跑错代码，等于给自己发合格证。**
    """
    import futu_quant

    worktree_root = Path(__file__).resolve().parents[1]
    module_path = Path(futu_quant.__file__).resolve()
    if worktree_root not in module_path.parents:
        raise AssertionError(
            f"跑错代码：futu_quant 解析到 {module_path}，"
            f"不在本 worktree {worktree_root} 下 —— 本轮验收无效"
        )


_assert_code_origin()

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

# 生产根目录是**常量**，不读任何环境变量：否则设一个环境变量就能把
# 「生产工作区」搬走，守卫随即全放行（等于守卫可被静默绕过）。
PRODUCTION_ROOT = Path(r"E:\Thisyi\trade").resolve()


def is_production_workspace() -> bool:
    """是否在生产工作区执行测试。

    **只看代码来源目录**，不看 `TRADEYI_ENV`：生产目录本身就是生产工作区。
    在任何分支工作树（`trade-dev\\wt-*`）或独立测试区（`trade-test`）下视为测试工作区。
    """
    current_root = Path(__file__).resolve().parents[1]
    return current_root == PRODUCTION_ROOT


def prod_guard_refusal(current_root: Path | None = None) -> str | None:
    """生产目录**绝不允许以测试身份**运行（`TRADEYI_ENV=test`）—— 一律硬失败。

    这条独立于指纹比对：即使生产库可读、指纹没变，也不能让「生产目录跑测试」这种
    *本身就不该发生* 的形态静默通过。测试必须在 `trade-test` 工作区执行。
    """
    root = (current_root or Path(__file__).resolve().parents[1]).resolve()
    if root != PRODUCTION_ROOT:
        return None
    if os.environ.get("TRADEYI_ENV", "").strip().lower() == "test":
        return (
            "禁止在生产目录以测试身份运行 pytest（TRADEYI_ENV=test）—— "
            "测试必须在 trade-test 工作区执行，生产目录只跑生产运行时。"
        )
    return None


# 隔离环境（TRADEYI_ENV=test）下，本工作区的 DEFAULT_DB 是**测试库**，不是生产库。
# 守卫必须显式知道生产库在哪，否则会把测试库当生产库：要么假绿（测的是副本），
# 要么因测试库不存在而误红。两者都会让「生产是否被写穿」这个问题失去答案。
_IS_TEST_ENV = os.environ.get("TRADEYI_ENV", "").strip().lower() == "test"
_prod_guard_env = os.environ.get("TRADEYI_PROD_DB_PATH", "").strip()
_PROD_DB_PATH_EXPLICIT = _IS_TEST_ENV and bool(_prod_guard_env)
if _IS_TEST_ENV:
    if _prod_guard_env:
        _PROD_GUARD_DB_PATH = Path(_prod_guard_env).resolve()
    else:
        _PROD_GUARD_DB_PATH = Path(_DEFAULT_PROD_DB).resolve()
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


def _changed_keys(
    before: tuple[str, tuple[tuple[str, int, str], ...]],
    after: tuple[str, tuple[tuple[str, int, str], ...]],
) -> list[str]:
    old = {key: (version, value) for key, version, value in before[1]}
    new = {key: (version, value) for key, version, value in after[1]}
    return [key for key in sorted(set(old) | set(new)) if old.get(key) != new.get(key)]


#: 「生产自写」**精确键**白名单的唯一数据源：`tests/fixtures/prod_guard_self_write_keys.txt`。
#:
#: 三条硬约束（2026-11-05 Reviewer 定稿）：
#:   ① **精确键，含租户后缀**，禁止前缀通配 —— `VOLATILITY_CAMPAIGNS*` 会把新租户的键
#:      顺带放过，等于又回到「测试写穿与生产自写不可区分」；
#:   ② 白名单只是**准入资格**，豁免还必须有窗口内的外部 actor 审计归因（见 `_change_exemption`）；
#:   ③ 加/删条目必须是可 review 的 diff；库里不存在的陈旧键 → 会话判红（`allowlist_stale_keys`）。
PROD_GUARD_ALLOWLIST_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "prod_guard_self_write_keys.txt"
)

_ALLOWLIST_CACHE: tuple[str, ...] | None = None

#: —— actor 命名约定（写死；要放宽先过 Reviewer，并同步改这里的注释与断言消息）——
#: `config_versions.actor` 的取值只有三类：
#:   ① `pytest*` / `test*`          → **测试进程**写入 → 一律硬红（写穿生产的元凶）；
#:   ② `streamlit_or_service` / 空  → UI 或服务自动写入、或无法归因 → 不放行；
#:   ③ 其余（`chief:*` / `ops:*` …）→ 人工/外部写入 → 豁免，但**逐条打 `[PROD-GUARD]` 日志**。
#: 只有 ③ 能拿到豁免；任何形如 ① 的写入都必须失败，否则这次放宽会把真写穿放过去。
#: 已知缺口：活着的 Worker/雷达目前也写 `streamlit_or_service`（见 fixture 文件里的记录），
#: 所以 campaign 族在白名单里也拿不到豁免 —— 需要写入方改用专用 actor 才能收敛。
SUSPICIOUS_ACTOR_PREFIXES: tuple[str, ...] = ("pytest", "test", "streamlit_or_service")

#: 会话开始时刻（ISO）。用于判断某个键的改动是否**留下了 store API 审计记录**。
_SESSION_STARTED_AT: str | None = None

#: 本次会话逐条豁免记录（sessionfinish 汇总用）。
_SESSION_EXEMPTIONS: list[tuple[str, object, object, str, dict[str, str] | None]] = []


def load_self_write_allowlist() -> tuple[str, ...]:
    """读精确键白名单。文件缺失/为空 → 直接抛错（守卫读不到输入不能当"全绿"）。"""
    global _ALLOWLIST_CACHE
    if _ALLOWLIST_CACHE is not None:
        return _ALLOWLIST_CACHE
    path = PROD_GUARD_ALLOWLIST_FIXTURE
    assert path.is_file(), f"生产自写白名单文件缺失：{path}"
    keys: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        assert "*" not in line, f"白名单禁止通配（必须是精确键）：{line!r}"
        keys.append(line)
    assert keys, f"生产自写白名单为空：{path}"
    assert len(set(keys)) == len(keys), f"白名单有重复键：{path}"
    _ALLOWLIST_CACHE = tuple(keys)
    return _ALLOWLIST_CACHE


def _reset_allowlist_cache() -> None:
    """测试用：fixture 被改写后需要重读。"""
    global _ALLOWLIST_CACHE
    _ALLOWLIST_CACHE = None


def _is_production_self_write(key: str) -> bool:
    """精确键匹配（含租户后缀）。**不做前缀通配**。"""
    return key in load_self_write_allowlist()


def allowlist_stale_keys(
    after: tuple[str, tuple[tuple[str, int, str], ...]] | None,
) -> list[str]:
    """白名单里**库里已不存在**的陈旧键（返回非空即判红，逼着表跟生产现实走）。"""
    if after is None:
        return []
    existing = {key for key, _version, _value in after[1]}
    return [key for key in load_self_write_allowlist() if key not in existing]


def is_suspicious_actor(actor: str | None) -> bool:
    """actor 是否属于「测试 / 自动 / 无法归因」——这些一律不豁免。"""
    text = (actor or "").strip().lower()
    if not text:
        return True
    return any(text.startswith(prefix) for prefix in SUSPICIOUS_ACTOR_PREFIXES)


def _readonly_prod_connection():
    """只读打开生产库（守卫永远不写生产）。"""
    import sqlite3

    return sqlite3.connect(f"file:{_PROD_GUARD_DB_PATH.as_posix()}?mode=ro", uri=True)


def _latest_write_audit(key: str, since_iso: str | None) -> dict[str, str] | None:
    """该键在会话窗口内**最新一条** store API 审计行（actor / reason / created_at）。

    `TradingStore` 每次带版本写都会落一行 `config_versions`，`created_at` 与
    `_SESSION_STARTED_AT` 同为 `datetime.now(timezone.utc).isoformat()`，可直接字符串比较。
    读不到（库/表不可读、无窗口内记录）返回 `None` —— 由调用方走兜底，不在此处放行。
    """
    if not since_iso:
        return None
    try:
        connection = _readonly_prod_connection()
        try:
            row = connection.execute(
                "SELECT actor, reason, created_at FROM config_versions"
                " WHERE config_key=? AND created_at>=?"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (key, since_iso),
            ).fetchone()
        finally:
            connection.close()
    except Exception:  # noqa: BLE001 - 读不到就当没有可用归因（严格方向）
        return None
    if row is None:
        return None
    return {
        "actor": str(row[0] or ""),
        "reason": str(row[1] or ""),
        "created_at": str(row[2] or ""),
    }


def _runtime_config_updated_at(key: str) -> str:
    """被豁免键的 `runtime_config.updated_at`（只用于审计日志，读不到不阻塞判定）。"""
    try:
        connection = _readonly_prod_connection()
        try:
            row = connection.execute(
                "SELECT updated_at FROM runtime_config WHERE config_key=?", (key,)
            ).fetchone()
        finally:
            connection.close()
    except Exception:  # noqa: BLE001
        return "（不可读）"
    if not row or not row[0]:
        return "（未知）"
    return str(row[0])


def _change_exemption(key: str, since_iso: str | None) -> tuple[bool, str, dict[str, str] | None]:
    """判定一次**值变化**能否豁免，返回 `(是否豁免, 依据, 审计行)`。

    三条必须**同时**成立（Reviewer 2026-11-05 定稿，缺一条即硬失败）：

    1. 键在精确键白名单里（`tests/fixtures/prod_guard_self_write_keys.txt`）；
    2. `config_versions` 里该键在**会话窗口内**有审计行；
    3. 该审计行的 `actor` 前缀 ∉ `SUSPICIOUS_ACTOR_PREFIXES`（`pytest*` / `test*` /
       `streamlit_or_service` / 空都不放行）。

    查不到审计行 → 硬失败（宁可噪声，不可静默）。
    """
    if not _is_production_self_write(key):
        return False, "不在精确键白名单（tests/fixtures/prod_guard_self_write_keys.txt）", None
    audit = _latest_write_audit(key, since_iso)
    if audit is None:
        return False, "白名单键在会话窗口内查不到 config_versions 审计行", None
    if is_suspicious_actor(audit["actor"]):
        return False, f"审计 actor={audit['actor']!r} 属测试/自动写入，不放行", audit
    return True, "精确键白名单 + 窗口内外部 actor 审计归因", audit


def _log_exemptions(
    rows: list[tuple[str, object, object, str, dict[str, str] | None]],
    before_sha: str,
    after_sha: str,
) -> None:
    """逐条打印豁免（键、旧/新 version、actor、reason、前后 sha）——豁免必须可审计。"""
    if not rows:
        return
    _SESSION_EXEMPTIONS.extend(rows)
    lines = [
        "\n[PROD-GUARD] 生产库变化已豁免（非测试写入，不判失败）：",
        f"  数据源：{db_path_under_guard()}",
        f"  会话起点：{_SESSION_STARTED_AT or '（未记录）'}",
        f"  指纹：{before_sha[:16]} -> {after_sha[:16]}",
        "  actor 约定：只有 pytest*/test* 算测试写入；streamlit_or_service 与空 actor 不放行。",
    ]
    for key, old_version, new_version, basis, audit in rows:
        lines.append(
            f"  - key={key} version {old_version}->{new_version}"
            f" updated_at={_runtime_config_updated_at(key)}"
            f" actor={audit['actor'] if audit else '（无审计行）'}"
            f" reason={(audit['reason'] or '-') if audit else '-'}"
            f" audit_at={(audit['created_at'] or '-') if audit else '-'}"
            f" sha={before_sha[:16]}->{after_sha[:16]}"
            f" 依据={basis}"
        )
    print("\n".join(lines), file=sys.stderr, flush=True)


def log_session_exemption_summary() -> None:
    """sessionfinish 汇总：本次会话一共豁免了哪些键（谁在什么时候动的）。"""
    if not _SESSION_EXEMPTIONS:
        return
    keys = ", ".join(sorted({key for key, *_ in _SESSION_EXEMPTIONS}))
    detail = "; ".join(
        f"{key} {old}->{new} actor={(audit or {}).get('actor', '（无审计行）')}"
        for key, old, new, _basis, audit in _SESSION_EXEMPTIONS
    )
    print(
        f"\n[PROD-GUARD] 会话豁免汇总：{len(_SESSION_EXEMPTIONS)} 条变化来自非测试写入"
        f"\n  键：{keys}\n  明细：{detail}",
        file=sys.stderr,
        flush=True,
    )


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
    """整表变化、override 或指纹不可读均为硬违规。

    变化键的判定（2026-11-05 Reviewer 定稿：精确键白名单 + 窗口内外部 actor 审计归因）：
    - **新增键 / 删除键**：一律硬失败（不论审计归属）；
    - **值变化**：逐键按 `_change_exemption` 判定；**只要有一个键不豁免，整次仍硬失败**
      （白名单不能当挡箭牌，混在被豁免键里的写穿照样红）；
    - 全部豁免时逐条打 `[PROD-GUARD]` 日志（键 / 新旧 version / actor / reason / 前后 sha）。
    """
    if os.environ.get("TRADEYI_PROD_DB_OVERRIDE"):
        return "生产守卫禁止 TRADEYI_PROD_DB_OVERRIDE：环境变量会使守卫可被静默绕过。"

    # 路径 1：库里出现生产指纹（启动时已拿到基线）
    if before is not None:
        if after is None:
            return f"生产 runtime_config 指纹不可读（数据源：{db_path_under_guard()}）—— fail-closed。"
        if before != after:
            changed = _changed_keys(before, after)
            if not changed:
                return (
                    "生产 runtime_config 在 pytest 会话期间被改动（指纹变化但无法生成行级差异）"
                    f"（数据源：{db_path_under_guard()}）—— fail-closed。"
                )
            old_rows = {key: (version, value) for key, version, value in before[1]}
            new_rows = {key: (version, value) for key, version, value in after[1]}
            exemptions: list[tuple[str, object, object, str, dict[str, str] | None]] = []
            failures: list[str] = []
            for key in changed:
                old_row = old_rows.get(key)
                new_row = new_rows.get(key)
                if old_row is None or new_row is None:
                    kind = "新增键" if old_row is None else "删除键"
                    failures.append(f"{key}：{kind}（新增/删除一律不豁免）")
                    continue
                exempt, basis, audit = _change_exemption(key, _SESSION_STARTED_AT)
                if exempt:
                    exemptions.append((key, old_row[0], new_row[0], basis, audit))
                else:
                    failures.append(f"{key}：{basis}")
            if not failures:
                _log_exemptions(exemptions, before[0], after[0])
                return None
            failure_detail = "\n".join(f"  - {item}" for item in failures[:8])
            return (
                "生产 runtime_config 在 pytest 会话期间被改动！\n"
                f"  数据源：{db_path_under_guard()}\n"
                f"  会话前：sha={before[0][:16]}\n"
                f"  会话后：sha={after[0][:16]}\n"
                f"  差异：\n{_compact_runtime_diff(before, after)}\n"
                f"  变化键：{', '.join(changed[:8]) or '（无）'}\n"
                f"  未豁免明细：\n{failure_detail}\n"
                "  —— actor 约定：只有 pytest*/test* 算测试进程写入（一律硬红）；"
                "streamlit_or_service 与空 actor 不放行；"
                "白名单外键查不到审计行、以及任何键的新增/删除，同样硬失败。"
            )
        return None

    # 路径 2：before is None（拿不到生产库指纹）
    # 生产目录或测试区显式指定生产库：必须硬失败（fail-closed），禁止静默跳过守卫。
    if is_production_workspace() or (_IS_TEST_ENV and _PROD_DB_PATH_EXPLICIT):
        return f"生产 runtime_config 指纹不可读（数据源：{db_path_under_guard()}）—— fail-closed。"

    # 路径 3：测试区未显式指定生产库：允许以 0 通过，由 sessionfinish 明确打印通知
    return None


def pytest_sessionstart(session):
    """会话开始：记整张生产 `runtime_config` 的指纹；override 立即硬红。"""
    # 生产目录 + TRADEYI_ENV=test：形态本身违规，先拒跑，不进入指纹比对。
    refusal = prod_guard_refusal()
    if refusal:
        print(f"\n[PROD-GUARD] {refusal}", file=sys.stderr, flush=True)
        raise pytest.UsageError(refusal)
    global _SESSION_PROD_BASELINE, _SESSION_STARTED_AT
    from datetime import datetime as _dt, timezone as _tz

    _SESSION_STARTED_AT = _dt.now(_tz.utc).isoformat()
    override = os.environ.get("TRADEYI_PROD_DB_OVERRIDE")
    if override:
        message = (
            "[PROD-GUARD] 禁止 TRADEYI_PROD_DB_OVERRIDE（会话开始即硬红）："
            f"守卫数据源固定为生产库 {_PROD_GUARD_DB_PATH}，不接受环境变量重定向 —— "
            f"实测值 {override!r}。变异单测请 monkeypatch conftest._PROD_GUARD_DB_PATH。"
        )
        print(f"\n{message}", file=sys.stderr, flush=True)
        raise pytest.UsageError(message)
    if is_production_workspace() or (_IS_TEST_ENV and _PROD_DB_PATH_EXPLICIT):
        _SESSION_PROD_BASELINE = prod_config_fingerprint()
    else:
        # A4（2026-10-27）：测试区里的 `data/trading.db` 是**测试库**，不是生产库。
        # 拿它当生产库做会话基线比对，会把「测试自己写的数据」误报成写穿生产 —— 必须
        # 明确不做比对（skip 口径），而不是 fail。
        _SESSION_PROD_BASELINE = None
    if _SESSION_PROD_BASELINE is None:
        if is_production_workspace() or (_IS_TEST_ENV and _PROD_DB_PATH_EXPLICIT):
            print(
                f"\n[PROD-GUARD] 生产 runtime_config 指纹不可读（{_PROD_GUARD_DB_PATH}）"
                "—— 会话结束时将按 fail-closed 硬红。",
                file=sys.stderr, flush=True,
            )
        else:
            print(
                f"\n[PROD-GUARD] 数据源为测试库、本轮不做生产比对（数据源：{_PROD_GUARD_DB_PATH}）",
                file=sys.stderr, flush=True,
            )


def pytest_sessionfinish(session, exitstatus):
    """会话结束：比对**整张** `runtime_config` 指纹，变了/不可读就**红**。

    这是**独立第二道**：它不看"调用点有没有传 store"，只看**生产库变没变** ——
    因此能抓住任何路径，包括绕过 `TradingStore` 的裸 sqlite3 写。
    """
    global _SESSION_PROD_BASELINE
    after = prod_config_fingerprint()
    violation = prod_config_violation(_SESSION_PROD_BASELINE, after)
    baseline = _SESSION_PROD_BASELINE
    _SESSION_PROD_BASELINE = None
    # 白名单自身守卫：库里已不存在的陈旧键必须红（逼着 fixture 跟生产现实走）。
    stale = allowlist_stale_keys(after) if baseline is not None else []
    if stale:
        violation = (violation or "") + (
            f"\n[PROD-GUARD] 白名单存在陈旧键（库里已不存在）：{stale}"
            f"\n  白名单文件：{PROD_GUARD_ALLOWLIST_FIXTURE}"
        )
    log_session_exemption_summary()
    if violation:
        print(f"\n[PROD-GUARD] {violation}", file=sys.stderr, flush=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    elif baseline is None and not is_production_workspace() and not (_IS_TEST_ENV and _PROD_DB_PATH_EXPLICIT):
        print(
            f"\n[PROD-GUARD] 数据源为测试库、本轮不做生产比对（数据源：{db_path_under_guard()}）",
            file=sys.stderr, flush=True,
        )

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

    import uuid
    case_db_name = f"isolated_{os.getpid()}_{uuid.uuid4().hex[:8]}.db"

    def _isolated_init(
        self, path=None, tenant_registry=None, read_only=False, bootstrap=False,
        allow_migrations=False, migration_actor="",
    ):
        # 测试专用 shim（P1，2026-10-27）：库代码已不再读 `TRADEYI_ALLOW_BOOTSTRAP`
        # 环境变量（与迁移授权同源加固），但测试大量用临时路径建库。这里在 conftest
        # 内把环境变量翻译成显式 `bootstrap=True`，被测代码路径保持纯显式参数。
        effective_bootstrap = bootstrap or os.getenv(
            "TRADEYI_ALLOW_BOOTSTRAP", ""
        ).strip() == "1"
        if path is not None and Path(path).resolve() != Path(_PROD_DB).resolve():
            _real_init(self, path, tenant_registry, read_only=read_only,
                       bootstrap=effective_bootstrap,
                       allow_migrations=allow_migrations, migration_actor=migration_actor)
        else:
            # 用例级唯一库名：同用例内多次实例化共享同一库，跨用例/跨运行天然隔离消灭半截库复用
            _real_init(self, tmp_path / case_db_name, tenant_registry,
                       read_only=read_only, bootstrap=effective_bootstrap,
                       allow_migrations=allow_migrations, migration_actor=migration_actor)

    monkeypatch.setattr(_TradingStore, "__init__", _isolated_init)

    # 迁移授权是进程级全局（P0，2026-10-26：「授权不随子进程继承」的代价是它也不随
    # monkeypatch 自动回滚）——每条用例前后都清空，避免上一条用例的授权泄漏给下一条。
    from futu_quant.migrations import revoke_migrations
    revoke_migrations()

    yield

    revoke_migrations()

    # 每条用例后：恢复完整 os.environ 快照，保留 pytest 自身控制变量
    for key in list(os.environ.keys()):
        if key not in _SNAPSHOT_KEYS and not key.startswith("PYTEST_"):
            del os.environ[key]
    for key, value in _SNAPSHOT_VALUES.items():
        if os.environ.get(key) != value:
            os.environ[key] = value
