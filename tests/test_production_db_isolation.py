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

import pytest

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
    import pytest
    from conftest import (
        _SESSION_PROD_BASELINE,
        is_production_workspace,
        prod_config_fingerprint,
        prod_config_violation,
    )

    if _SESSION_PROD_BASELINE is None and not is_production_workspace():
        pytest.skip("测试区拿不到生产库指纹，跳过生产会话基线比对")
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
        # 隔离测试区：显式给了 TRADEYI_PROD_DB_PATH 就必须用它（真生产库）；
        # 没给则按 conftest 的口径回落本工作区 DEFAULT_DB（测试库），并在会话开始/
        # 结束明确打印「数据源为测试库、本轮不做生产比对」—— 不静默。
        guard_env = os.environ.get("TRADEYI_PROD_DB_PATH", "").strip()
        if guard_env:
            expected = Path(guard_env).resolve()
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


def test_prod_guard_test_workspace_allows_without_production_db(monkeypatch):
    """PROD-GUARD 测试区口径：

    1. 测试区拿不到生产库指纹时：明确放行（violation is None），不做生产比对；
    2. 代码来源是生产目录时：拿不到指纹必须硬失败（fail-closed）；
    3. 库里出现生产指纹（before is not None）时：数据被改动或读不到仍必须硬失败。
    """
    import conftest

    # 路 1：仅测试区未显式指定生产库路径才允许明确 skip。
    monkeypatch.setattr(conftest, "is_production_workspace", lambda: False)
    monkeypatch.setattr(conftest, "_IS_TEST_ENV", True)
    monkeypatch.setattr(conftest, "_PROD_DB_PATH_EXPLICIT", False)
    assert conftest.prod_config_violation(None, None) is None

    # 路 1b：测试区显式指定生产库路径 -> 不可读必须硬失败。
    monkeypatch.setattr(conftest, "_PROD_DB_PATH_EXPLICIT", True)
    v_explicit = conftest.prod_config_violation(None, None)
    assert v_explicit and "fail-closed" in v_explicit

    # 路 2：代码来源是生产目录 -> 拿不到指纹硬失败（fail-closed）
    monkeypatch.setattr(conftest, "is_production_workspace", lambda: True)
    v_prod = conftest.prod_config_violation(None, None)
    assert v_prod and "fail-closed" in v_prod

    # 路 3：库里出现生产指纹 -> 变动或损坏仍硬失败
    v_diff = conftest.prod_config_violation(("sha1", ()), ("sha2", ()))
    assert v_diff and "被改动" in v_diff

    v_lost = conftest.prod_config_violation(("sha1", ()), None)
    assert v_lost and "fail-closed" in v_lost


def test_prod_guard_refuses_test_env_in_production_root(monkeypatch):
    """生产目录**不许**以测试身份运行：即使生产库可读、指纹没变也必须拒跑。

    变异：删掉 `prod_guard_refusal` 的 TRADEYI_ENV 分支（或让它恒返回 None）→ 本用例转红。
    """
    import conftest

    monkeypatch.setenv("TRADEYI_ENV", "test")
    refusal = conftest.prod_guard_refusal(conftest.PRODUCTION_ROOT)
    assert refusal is not None, "生产目录 + TRADEYI_ENV=test 必须拒跑，不得静默放行"
    assert "禁止在生产目录以测试身份运行" in refusal
    # 测试区同样设 TRADEYI_ENV=test 是**正常形态**，不得误杀
    assert conftest.prod_guard_refusal(conftest.PRODUCTION_ROOT.parent / "trade-test") is None


def test_production_root_is_not_env_configurable():
    """生产根目录是常量：不许用 `TRADEYI_PROD_ROOT` 之类的环境变量把它搬走。

    搬走生产根的后果是「守卫认为自己在测试区」→ 生产库被写穿也静默变绿，
    所以这里用**源码级**判据（行为判据无法表达"某条读取分支不存在"）。
    变异：把 `PRODUCTION_ROOT` 改回 `os.environ.get("TRADEYI_PROD_ROOT", ...)` → 本用例转红。
    """
    import conftest

    assert conftest.PRODUCTION_ROOT == Path(r"E:\Thisyi\trade").resolve()
    source = Path(conftest.__file__).read_text(encoding="utf-8")
    assert "TRADEYI_PROD_ROOT" not in source, "生产根不允许被环境变量接管（守卫可被静默绕过）"


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2026-11-02：整表指纹的**归因**（生产服务自写 vs 测试直写库）
#
# 背景：开盘后雷达按分钟级写 `VOLATILITY_CAMPAIGNS:<tenant>`（实测 32→34），
# 用「指纹必须不变」当判据等于要求生产为测试让路。正确判据是
# 「测试进程不能写生产库」：测试的 store 已被重定向到临时库，
# 因此生产库里的改动只可能来自生产服务 —— 且必然带 `config_versions` 审计行。
# 反过来，**没有审计行的改动 = 绕过 store 直写库**，正是 10-23 事故形态，必须继续红。
# ---------------------------------------------------------------------------


def _snapshot(rows):
    return ("sha-" + str(len(rows)), tuple(rows))


def _guarded_db(tmp_path, *, with_audit: bool):
    """合成守卫库：`runtime_config`（供指纹）+ 可选 `config_versions`（供归因）。"""
    import sqlite3

    db = tmp_path / "guarded_runtime.db"
    connection = sqlite3.connect(str(db))
    try:
        connection.execute(
            "CREATE TABLE runtime_config ("
            "config_key TEXT PRIMARY KEY, value_json TEXT, version INTEGER, updated_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE config_versions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, config_key TEXT, value_json TEXT,"
            " actor TEXT, reason TEXT, created_at TEXT)"
        )
        if with_audit:
            connection.executemany(
                "INSERT INTO config_versions (config_key, value_json, actor, reason, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    ("VOLATILITY_CAMPAIGNS:futu_real_master", "[]", "streamlit_or_service", "", "2026-11-02T15:00:00+00:00"),
                    ("GAMMA_CAMPAIGNS", "[]", "streamlit_or_service", "", "2026-11-02T15:00:00+00:00"),
                ],
            )
        connection.commit()
    finally:
        connection.close()
    return db


def _wire_guard(monkeypatch, db, since="2026-11-02T14:00:00+00:00"):
    import conftest

    monkeypatch.setattr(conftest, "_PROD_GUARD_DB_PATH", db)
    monkeypatch.setattr(conftest, "_SESSION_STARTED_AT", since)
    monkeypatch.setattr(conftest, "_SESSION_EXEMPTIONS", [])
    return conftest


def test_campaign_change_with_generic_actor_is_hard_fail(tmp_path, monkeypatch):
    """2026-11-05 收紧：白名单键 + `streamlit_or_service` 通用 actor 也**不豁免**。

    实测生产 `config_versions` 里 917 行的 actor 是这个通用值（Worker/雷达写
    `VOLATILITY_CAMPAIGNS:<tenant>` 也用它）。Reviewer 定稿把它放进可疑集，
    宁可噪声也不静默放行；要收敛噪声须由写入方改用专用 actor（`service:*`）。
    """
    conftest = _wire_guard(monkeypatch, _guarded_db(tmp_path, with_audit=True))

    before = _snapshot([("VOLATILITY_CAMPAIGNS:futu_real_master", 32, "[]")])
    after = _snapshot([("VOLATILITY_CAMPAIGNS:futu_real_master", 34, '[{"code": "US.QQQ"}]')])

    violation = conftest.prod_config_violation(before, after)

    assert violation is not None
    assert "VOLATILITY_CAMPAIGNS:futu_real_master" in violation
    assert "streamlit_or_service" in violation


def test_campaign_change_without_audit_row_still_hard_fails(tmp_path, monkeypatch):
    """没有审计行 = 绕过 store 直写库（10-23 事故形态），必须红。"""
    conftest = _wire_guard(monkeypatch, _guarded_db(tmp_path, with_audit=False))

    before = _snapshot([("VOLATILITY_CAMPAIGNS:futu_real_master", 32, "[]")])
    after = _snapshot([("VOLATILITY_CAMPAIGNS:futu_real_master", 34, "[]")])

    violation = conftest.prod_config_violation(before, after)

    assert violation is not None
    assert "VOLATILITY_CAMPAIGNS" in violation
    assert "查不到" in violation, "无审计行必须以『查不到归因』硬失败（宁可噪声，不可静默）"


def test_config_key_change_still_hard_fails(tmp_path, monkeypatch):
    """配置类键即便有审计行也不在白名单里 —— 测试写穿配置才是事故。"""
    conftest = _wire_guard(monkeypatch, _guarded_db(tmp_path, with_audit=True))

    before = _snapshot([("risk_limits:futu_real_master", 3, "{}")])
    after = _snapshot([("risk_limits:futu_real_master", 4, '{"max_daily_loss": 1}')])

    violation = conftest.prod_config_violation(before, after)

    assert violation is not None
    assert "risk_limits:futu_real_master" in violation


def test_mixed_change_is_not_excused(tmp_path, monkeypatch):
    """只要有一个非白名单键变了，整次仍然红（白名单不能当挡箭牌）。"""
    conftest = _wire_guard(monkeypatch, _guarded_db(tmp_path, with_audit=True))

    before = _snapshot([
        ("VOLATILITY_CAMPAIGNS:futu_real_master", 32, "[]"),
        ("strategy_enabled:futu_real_master:core_satellite", 1, "true"),
    ])
    after = _snapshot([
        ("VOLATILITY_CAMPAIGNS:futu_real_master", 34, "[]"),
        ("strategy_enabled:futu_real_master:core_satellite", 2, "false"),
    ])

    assert conftest.prod_config_violation(before, after) is not None


def test_self_write_whitelist_excludes_config_like_keys():
    """白名单是**精确键**：配置键不许进，同前缀的新租户键也不许进（禁止通配）。"""
    import conftest

    assert conftest._is_production_self_write("VOLATILITY_CAMPAIGNS:futu_real_master")
    assert conftest._is_production_self_write("GAMMA_CAMPAIGNS")
    for key in (
        "risk_limits:futu_real_master",
        "real_account_config:281756481476169109",
        "strategy_enabled:futu_real_master:core_satellite",
        "alert_mute_list",
        "VOLATILITY_CAMPAIGNS:futu_new_tenant",
        "limit_profile:unknown_tenant",
    ):
        assert not conftest._is_production_self_write(key), f"{key} 不该进白名单"


# ---------------------------------------------------------------------------
# 2026-11-05：Reviewer 签字版（绑了六条），比 11-04 的 a 案更严
#
# 背景：旧判据 = 「所有变化键都在前缀白名单里 **且** 有 store 审计行」。
# `limit_profile:*` 属配置类键，被设计成一律硬红 —— 结果是**任何一次合法的限额发布**
# 都把带生产库指纹的全量打红（实测 23:55:37 `limit_profile:futu_paper_sandbox`，
# `config_versions.actor=chief:hotfix_republish`，是发 hotfix 时写的）。
#
# 定稿口径：精确键白名单（tests/fixtures/，禁前缀通配）+ 窗口内外部 actor 审计归因；
# 查不到审计行 → 硬失败；键的新增/删除 → 硬失败；豁免逐条打日志并在 sessionfinish 汇总。
# actor 约定写死在 `conftest.SUSPICIOUS_ACTOR_PREFIXES`：
#   ① pytest*/test*          → 测试写入 → 一律硬红；
#   ② streamlit_or_service / 空 → 自动写入/无法归因 → 不放行；
#   ③ 其余（chief:* / ops:*）   → 外部人工写入 → 豁免，但逐条打 `[PROD-GUARD]` 日志。
# ---------------------------------------------------------------------------


def _guarded_db_rows(tmp_path, *, runtime=(), audit=()):
    """合成守卫库：`runtime_config`（含 updated_at）+ `config_versions` 审计行。"""
    import sqlite3

    db = tmp_path / "guarded_rows.db"
    connection = sqlite3.connect(str(db))
    try:
        connection.execute(
            "CREATE TABLE runtime_config ("
            "config_key TEXT PRIMARY KEY, value_json TEXT, version INTEGER, updated_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE config_versions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, config_key TEXT, value_json TEXT,"
            " actor TEXT, reason TEXT, created_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO runtime_config (config_key, value_json, version, updated_at)"
            " VALUES (?, ?, ?, ?)",
            runtime,
        )
        connection.executemany(
            "INSERT INTO config_versions (config_key, value_json, actor, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            audit,
        )
        connection.commit()
    finally:
        connection.close()
    return db


def test_external_actor_write_is_exempted_and_logged(tmp_path, monkeypatch, capsys):
    """⑥ 绿：外部 actor 改 `limit_profile:*` → 绿，逐条打全量审计日志。"""
    db = _guarded_db_rows(
        tmp_path,
        runtime=[
            ("limit_profile:futu_paper_sandbox", '{"risk_ratio": 0.3}', 4,
             "2026-11-04T15:55:37+00:00"),
        ],
        audit=[
            ("limit_profile:futu_paper_sandbox", '{"risk_ratio": 0.3}', "chief:hotfix_republish",
             "hotfix 重发限额档", "2026-11-04T15:55:37+00:00"),
        ],
    )
    conftest = _wire_guard(monkeypatch, db)

    before = _snapshot([("limit_profile:futu_paper_sandbox", 3, "{}")])
    after = _snapshot([("limit_profile:futu_paper_sandbox", 4, '{"risk_ratio": 0.3}')])

    assert conftest.prod_config_violation(before, after) is None

    err = capsys.readouterr().err
    assert "[PROD-GUARD]" in err, f"豁免必须打日志，实际输出：{err!r}"
    assert "limit_profile:futu_paper_sandbox" in err
    assert "version 3->4" in err
    assert "2026-11-04T15:55:37+00:00" in err, "缺 updated_at，豁免不可审计"
    assert "chief:hotfix_republish" in err, "缺 actor，豁免不可归因"
    assert "hotfix 重发限额档" in err, "缺 reason 摘要"
    assert f"sha={before[0][:16]}->{after[0][:16]}" in err, "缺前后 sha"
    # sessionfinish 汇总（谁在什么时候动的）
    conftest.log_session_exemption_summary()
    assert "会话豁免汇总" in capsys.readouterr().err


def test_pytest_actor_write_is_hard_fail(tmp_path, monkeypatch):
    """⑥ 红：actor=`pytest_worker_x` 改**同一个** `limit_profile:*` 键 → 硬红。

    与上一用例构成双向自证：键完全相同，只换 actor，绿/红必须翻转。
    """
    db = _guarded_db_rows(
        tmp_path,
        audit=[
            ("limit_profile:futu_paper_sandbox", "{}", "pytest_worker_x", "",
             "2026-11-04T15:55:37+00:00"),
        ],
    )
    conftest = _wire_guard(monkeypatch, db)

    before = _snapshot([("limit_profile:futu_paper_sandbox", 3, "{}")])
    after = _snapshot([("limit_profile:futu_paper_sandbox", 4, '{"risk_ratio": 0.9}')])

    violation = conftest.prod_config_violation(before, after)

    assert violation is not None, "pytest 进程写生产配置必须硬红"
    assert "limit_profile:futu_paper_sandbox" in violation
    assert "pytest" in violation
    # actor 命名约定必须写进断言消息，否则下次放宽会没人记得边界
    assert "pytest*/test*" in violation
    assert "streamlit_or_service" in violation


def test_deleted_key_still_hard_fails(tmp_path, monkeypatch):
    """键被**删除** → 硬红（即使删除动作留下了外部 actor 审计行）。"""
    db = _guarded_db_rows(
        tmp_path,
        audit=[
            ("alert_mute_list", "[]", "chief:manual_release", "手动清理", "2026-11-04T15:55:37+00:00"),
        ],
    )
    conftest = _wire_guard(monkeypatch, db)

    before = _snapshot([
        ("limit_profile:futu_paper_sandbox", 1, "{}"),
        ("alert_mute_list", 1, "[]"),
    ])
    after = _snapshot([("limit_profile:futu_paper_sandbox", 1, "{}")])

    violation = conftest.prod_config_violation(before, after)

    assert violation is not None
    assert "删除键" in violation
    assert "alert_mute_list" in violation


def test_added_key_still_hard_fails(tmp_path, monkeypatch):
    """键被**新增** → 硬红（新增无法归因为"改写"，一律不放行）。"""
    db = _guarded_db_rows(
        tmp_path,
        audit=[("brand_new_key", "1", "chief:manual_release", "", "2026-11-04T15:55:37+00:00")],
    )
    conftest = _wire_guard(monkeypatch, db)

    before = _snapshot([("limit_profile:futu_paper_sandbox", 1, "{}")])
    after = _snapshot([
        ("limit_profile:futu_paper_sandbox", 1, "{}"),
        ("brand_new_key", 1, "1"),
    ])

    violation = conftest.prod_config_violation(before, after)

    assert violation is not None
    assert "新增键" in violation
    assert "brand_new_key" in violation


def test_value_change_without_audit_row_still_hard_fails(tmp_path, monkeypatch):
    """③ 白名单键、但窗口内查不到审计行 → 硬红（宁可噪声，不可静默）。"""
    conftest = _wire_guard(monkeypatch, _guarded_db_rows(tmp_path))

    before = _snapshot([("limit_profile:futu_real_master", 3, "{}")])
    after = _snapshot([("limit_profile:futu_real_master", 4, '{"max_daily_loss": 1}')])

    violation = conftest.prod_config_violation(before, after)

    assert violation is not None
    assert "limit_profile:futu_real_master" in violation
    assert "查不到" in violation


def test_audit_row_outside_session_window_is_not_exempt(tmp_path, monkeypatch):
    """③ 审计行必须落在**会话窗口内**：窗口外的旧 actor 记录不能拿来归因。"""
    db = _guarded_db_rows(
        tmp_path,
        audit=[
            ("limit_profile:futu_paper_sandbox", "{}", "chief:manual_release", "",
             "2026-11-01T00:00:00+00:00"),  # 早于 _wire_guard 的 since
        ],
    )
    conftest = _wire_guard(monkeypatch, db)

    before = _snapshot([("limit_profile:futu_paper_sandbox", 3, "{}")])
    after = _snapshot([("limit_profile:futu_paper_sandbox", 4, "{}")])

    violation = conftest.prod_config_violation(before, after)

    assert violation is not None
    assert "查不到" in violation


def test_allowlist_is_exact_keys_from_fixture(monkeypatch, tmp_path):
    """① 白名单落在 `tests/fixtures/`，且是**精确键**：加/删条目必须是可 review 的 diff。"""
    import conftest

    keys = conftest.load_self_write_allowlist()
    fixture = conftest.PROD_GUARD_ALLOWLIST_FIXTURE
    assert fixture.is_file() and fixture.name == "prod_guard_self_write_keys.txt"
    assert "tests" in fixture.parts and "fixtures" in fixture.parts
    assert keys, "白名单读不到输入不能当全绿"
    assert len(keys) == len(set(keys)), "精确键白名单不许重复"
    assert all("*" not in key and not key.endswith(":") for key in keys), "禁止前缀通配"
    assert "limit_profile:futu_paper_sandbox" in keys
    assert "VOLATILITY_CAMPAIGNS:futu_real_master" in keys
    assert "limit_profile:unknown_tenant" not in keys, "新租户键不许被通配顺带放过"
    # fixture 文件被删/写空 → 守卫必须自己炸，不能静默放行
    real_fixture = conftest.PROD_GUARD_ALLOWLIST_FIXTURE
    monkeypatch.setattr(conftest, "PROD_GUARD_ALLOWLIST_FIXTURE", tmp_path / "none.txt")
    conftest._reset_allowlist_cache()
    with pytest.raises(AssertionError, match="缺失"):
        conftest.load_self_write_allowlist()
    monkeypatch.setattr(conftest, "PROD_GUARD_ALLOWLIST_FIXTURE", real_fixture)
    conftest._reset_allowlist_cache()


def test_stale_allowlist_key_is_hard_fail():
    """⑤ 白名单里出现库里不存在的陈旧键 → 红（逼着表跟生产现实走）。"""
    import conftest

    after = _snapshot([("limit_profile:futu_paper_sandbox", 1, "{}")])
    stale = conftest.allowlist_stale_keys(after)
    assert stale, "库里只剩一个白名单键，其余都该被判陈旧"
    assert "VOLATILITY_CAMPAIGNS:futu_real_master" in stale
    assert "limit_profile:futu_paper_sandbox" not in stale


def test_deleting_attribution_logic_turns_red(tmp_path, monkeypatch):
    """⑤ 守卫自身要有守卫：把归因查询摘掉（返回 None）→ 白名单键也必须红。

    变异方向：若有人删掉 `_change_exemption` 里的 `_latest_write_audit` 调用、
    改成「白名单键一律放行」，本用例的 assert 立刻转红。
    """
    db = _guarded_db_rows(
        tmp_path,
        audit=[
            ("limit_profile:futu_paper_sandbox", "{}", "chief:manual_release", "",
             "2026-11-04T15:55:37+00:00"),
        ],
    )
    conftest = _wire_guard(monkeypatch, db)
    monkeypatch.setattr(conftest, "_latest_write_audit", lambda key, since: None)

    before = _snapshot([("limit_profile:futu_paper_sandbox", 3, "{}")])
    after = _snapshot([("limit_profile:futu_paper_sandbox", 4, "{}")])

    violation = conftest.prod_config_violation(before, after)

    assert violation is not None, "归因逻辑被摘掉后不能静默放行"
    assert "查不到" in violation


def test_direct_sqlite_write_to_prod_copy_is_detected(tmp_path, monkeypatch):
    """⑤ 直连 `sqlite3` 模拟测试写穿生产副本（无 store 审计行）→ 红。"""
    import shutil
    import sqlite3

    import conftest

    source = _guarded_db_rows(
        tmp_path,
        runtime=[("limit_profile:futu_paper_sandbox", "{}", 3, "2026-11-04T00:00:00+00:00")],
    )
    before = _snapshot([("limit_profile:futu_paper_sandbox", 3, "{}")])

    prod_copy = tmp_path / "production_copy.db"
    shutil.copyfile(source, prod_copy)
    monkeypatch.setattr(conftest, "_PROD_GUARD_DB_PATH", prod_copy)
    monkeypatch.setattr(conftest, "_SESSION_STARTED_AT", "2026-11-04T10:00:00+00:00")

    # 绕过 TradingStore：裸 sqlite3 直写库，不落任何 config_versions 审计行
    connection = sqlite3.connect(str(prod_copy))
    try:
        connection.execute(
            "UPDATE runtime_config SET value_json=?, version=? WHERE config_key=?",
            ('{"hacked": true}', 4, "limit_profile:futu_paper_sandbox"),
        )
        connection.commit()
    finally:
        connection.close()

    after = conftest.prod_config_fingerprint()
    assert after is not None
    violation = conftest.prod_config_violation(before, after)

    assert violation is not None, "裸 sqlite3 写穿生产副本必须红"
    assert "limit_profile:futu_paper_sandbox" in violation
    assert "查不到" in violation
