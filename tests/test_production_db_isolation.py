"""生产库写穿守卫：**任何测试都不得写 `data/trading.db`**。

背景（10-23 事故，Reviewer 实测）：
  只跑 `tests/test_campaign_state_write_read_symmetry.py`（2 passed / 3.23s），
  生产 `runtime_config['VOLATILITY_CAMPAIGNS']` 的版本号立刻 **+2**，
  整表被清空（8 条真实 campaign → 1 条测试夹具）—— **实盘持仓一度失去管理**。

根因三处（缺一不可）：
  ① 调用点不传 `store=`（`test_campaign_state_write_read_symmetry.py:76`）；
  ② `config.upsert_campaign` / `delete_campaign` 的 `store=None` 分支是
     `store or _TradingStore()` → **默认直连生产**；
  ③ `tests/conftest.py` 只隔离 `config.ENV_FILE`，**不隔离 DB 路径**。

**本文件守的是 ③ 的修复**：`conftest` 把 `TradingStore.__init__` 包成
"不传 path（或传的就是生产路径）→ 一律落到临时库"。这条不变量**不依赖调用点是否自觉**，
所以它才能防住"下一个忘记传 store 的人"。

**判据必须有可能为假**：下面第二条用例故意传生产路径，断言它**仍然**被重定向 ——
证明这条守的是"实例化点"，不是"某个具体调用点"。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from futu_quant.store import DEFAULT_DB, TradingStore  # noqa: E402


def test_bare_trading_store_never_points_at_production(tmp_path):
    """`TradingStore()`（不传 path）必须落到临时库，绝不等于生产库。"""
    store = TradingStore()
    assert store.path != Path(DEFAULT_DB).resolve(), (
        "隔离失效：`TradingStore()` 仍指向生产库 "
        f"{DEFAULT_DB} —— 任何不传 store 的写入都会写穿生产"
    )
    assert store.path.exists(), "隔离库未创建"


def test_explicit_production_path_is_also_redirected(tmp_path):
    """故意传生产路径 → 也必须被重定向（守的是实例化点，不是调用点自觉）。"""
    store = TradingStore(DEFAULT_DB)
    assert store.path != Path(DEFAULT_DB).resolve(), (
        "隔离失效：显式传生产路径仍被放行 —— 守的必须是**实例化点**，"
        "否则下一个显式传生产路径的调用点会写穿"
    )


def test_explicit_temp_path_is_respected(tmp_path):
    """反向：显式传临时路径必须**被尊重**（不得把所有实例化都压到同一个库）。"""
    wanted = tmp_path / "my-own.db"
    store = TradingStore(wanted)
    assert store.path == wanted.resolve(), (
        f"显式路径未被尊重：期望 {wanted}，实际 {store.path}"
    )


def test_production_config_unchanged_during_session(tmp_path, monkeypatch):
    """会话守卫（Chief 派）：生产 `VOLATILITY_CAMPAIGNS` 在整个会话期间必须一字未改。

    这是 conftest `pytest_sessionstart/sessionfinish` 的**独立第二道**：
    它不看"调用点有没有传 store"，只看**生产库变没变** → 能抓住任何路径，
    包括绕过 `TradingStore` 的裸 sqlite3 写。
    """
    from conftest import (
        _SESSION_PROD_BASELINE,
        prod_config_fingerprint,
        prod_config_violation,
    )

    assert _SESSION_PROD_BASELINE is not None, "会话基线未记录（sessionstart 没跑）"
    assert prod_config_violation(
        _SESSION_PROD_BASELINE, prod_config_fingerprint()
    ) is None, "生产 VOLATILITY_CAMPAIGNS 在本次会话期间被改动"


def _guard_source_isolated(tmp_path, monkeypatch):
    """把守卫数据源指向**合成**的 runtime_config 库（生产全程不被读写）。

    不用 `backup()` 复制 1.6GB 生产库：副本本身会拖慢并占用磁盘，而这里要验证的
    是**判据逻辑**（整表差异 / override / 不可读），不是生产数据内容。
    守卫是否真指向生产，由下一条用例单独断言。
    """
    import sqlite3

    import conftest

    synthetic = tmp_path / "synthetic_runtime.db"
    connection = sqlite3.connect(str(synthetic))
    try:
        connection.execute(
            "CREATE TABLE runtime_config ("
            "config_key TEXT PRIMARY KEY, value_json TEXT, version INTEGER, updated_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO runtime_config (config_key, value_json, version, updated_at)"
            " VALUES (?, ?, ?, ?)",
            [
                ("VOLATILITY_CAMPAIGNS", "[]", 1, "2026-10-23T00:00:00Z"),
                ("GAMMA_CAMPAIGNS", "[]", 1, "2026-10-23T00:00:00Z"),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(conftest, "_PROD_GUARD_DB_PATH", synthetic.resolve())
    return synthetic


def test_guard_source_is_fixed_to_real_production(tmp_path, monkeypatch):
    """守卫默认数据源必须是**真生产库**。

    - 生产目录：与 `store.DEFAULT_DB` 同源（源文件相对）；
    - 隔离测试区（`TRADEYI_ENV=test`）：本工作区 `DEFAULT_DB` 是测试库，
      守卫数据源必须是 `TRADEYI_PROD_DB_PATH` 指向的真生产库 —— 拿测试库当生产库会假绿。
    """
    import os

    import conftest

    expected = Path(DEFAULT_DB).resolve()
    if os.environ.get("TRADEYI_ENV", "").strip().lower() == "test":
        expected = Path(os.environ["TRADEYI_PROD_DB_PATH"]).resolve()
    assert conftest._PROD_GUARD_DB_PATH == expected, (
        f"守卫数据源不是生产库：{conftest._PROD_GUARD_DB_PATH} != {expected}"
    )
    # 反向：环境变量绝不能改指它
    monkeypatch.setenv("TRADEYI_PROD_DB_OVERRIDE", str(tmp_path / "evil.db"))
    assert conftest._PROD_GUARD_DB_PATH == expected, (
        "TRADEYI_PROD_DB_OVERRIDE 改变了守卫数据源 —— 可被静默绕过"
    )


def test_session_guard_detects_gamma_campaign_write(tmp_path, monkeypatch):
    """变异：非 volatility 配置也必须导致整表守卫硬红。"""
    import sqlite3

    import conftest

    synthetic = _guard_source_isolated(tmp_path, monkeypatch)
    before = conftest.prod_config_fingerprint()
    assert before is not None
    connection = sqlite3.connect(str(synthetic))
    try:
        connection.execute(
            "UPDATE runtime_config SET version = version + 1 WHERE config_key = ?",
            ("GAMMA_CAMPAIGNS",),
        )
        connection.commit()
    finally:
        connection.close()
    after = conftest.prod_config_fingerprint()
    violation = conftest.prod_config_violation(before, after)
    assert violation and "GAMMA_CAMPAIGNS" in violation, f"整表守卫空转：{violation!r}"


def test_session_guard_detects_content_change_with_same_version(tmp_path, monkeypatch):
    """变异：`version` 不变但 `value_json` 被换 —— 只看 version 会漏，整表 sha 必须抓住。"""
    import sqlite3

    import conftest

    synthetic = _guard_source_isolated(tmp_path, monkeypatch)
    before = conftest.prod_config_fingerprint()
    connection = sqlite3.connect(str(synthetic))
    try:
        connection.execute(
            "UPDATE runtime_config SET value_json = ? WHERE config_key = ?",
            ('[{"code": "US.TEST"}]', "VOLATILITY_CAMPAIGNS"),
        )
        connection.commit()
    finally:
        connection.close()
    after = conftest.prod_config_fingerprint()
    assert before[0] != after[0], "内容变了但 sha 没变 —— 守卫抓不住裸 sqlite3 改 value_json"
    violation = conftest.prod_config_violation(before, after)
    assert violation and "VOLATILITY_CAMPAIGNS" in violation


def test_session_guard_rejects_override_even_when_fingerprint_matches(tmp_path, monkeypatch):
    """变异：环境变量 override 本身即硬红，不能把守卫悄悄改指副本。"""
    import conftest

    _guard_source_isolated(tmp_path, monkeypatch)
    baseline = conftest.prod_config_fingerprint()
    monkeypatch.setenv("TRADEYI_PROD_DB_OVERRIDE", str(tmp_path / "not-production.db"))
    violation = conftest.prod_config_violation(baseline, baseline)
    assert violation and "TRADEYI_PROD_DB_OVERRIDE" in violation


def test_session_guard_fails_closed_when_fingerprint_unreadable(tmp_path, monkeypatch):
    """变异：生产库指纹读不到不得放绿。"""
    import conftest

    monkeypatch.setattr(conftest, "_PROD_GUARD_DB_PATH", tmp_path / "missing.db")
    assert conftest.prod_config_fingerprint() is None
    violation = conftest.prod_config_violation(("known", ()), None)
    assert violation and "不可读" in violation
