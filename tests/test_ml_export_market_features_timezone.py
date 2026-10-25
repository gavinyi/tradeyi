"""守卫：训练集真实行情合并的**时区口径**（v3.2，2026-10-25）。

背景：`option_history.time_key` 是 naive 的**交易所本地时间（ET）**（与
`futu_quant.data_quality.quote_freshness` 同一口径），`order_intents.created_at` 是
UTC-aware。直接 `merge_asof` 会抛 `MergeError: incompatible merge keys`；而"能算通
但不报错"的情形更危险——把 naive 当 UTC 会错位 4~5 小时，把**成交之后**的快照当成
入场时点，属 look-ahead 污染，训练指标会虚高且不可复现。

变异（必须转红）：把 `_market_stamp_to_utc` 里的 naive 分支改成 `tz_localize("UTC")`
（即当年那个错口径）→ 第一条用例必须匹配到 15:59 那行、断言失败。
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ml_export_training_data_v3 import add_option_market_features  # noqa: E402

CODE = "US.AAPL240119C175000"


def _make_db(path: Path, rows: list[tuple]) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE option_history ("
            "code TEXT, time_key TEXT, underlying_price REAL, gamma REAL, theta REAL)"
        )
        conn.executemany(
            "INSERT INTO option_history (code, time_key, underlying_price, gamma, theta)"
            " VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_matches_entry_time_snapshot_not_later_one(tmp_path):
    """成交 11:16 ET（=15:16 UTC），只能匹配 11:00 ET 那条。

    第二条刻意造在 **15:00 ET（=19:00 UTC）**：正确口径（naive=ET）下它在成交之后、
    不可选；错口径（naive=UTC）下它变成 15:00 UTC < 15:16 UTC，会被错误选中。
    所以这条用例专治"能算通但错位"的 look-ahead。
    """
    db = _make_db(
        tmp_path / "t.db",
        [
            (CODE, "2026-10-22 11:00:00", 200.0, 0.05, -0.10),  # naive ET，可选
            (CODE, "2026-10-22 15:00:00", 210.0, 0.06, -0.12),  # 成交之后，禁止选中
        ],
    )
    frame = pd.DataFrame(
        {"symbol": [CODE], "created_at": ["2026-10-22T15:16:37+00:00"]}  # = 11:16 ET
    )

    merged = add_option_market_features(frame, str(db))

    assert merged.loc[0, "underlying_price"] == pytest.approx(200.0), (
        "匹配到了成交之后的快照（look-ahead）：时区口径错了"
    )
    assert merged.loc[0, "option_gamma"] == pytest.approx(0.05)


def test_aware_time_key_is_converted_to_utc(tmp_path):
    """混入带时区的 time_key 时也不能报 dtype 错误、且按 UTC 换算后参与匹配。"""
    db = _make_db(
        tmp_path / "t.db",
        [(CODE, "2026-10-22T15:00:00+00:00", 200.0, 0.05, -0.10)],  # 11:00 ET
    )
    frame = pd.DataFrame(
        {"symbol": [CODE], "created_at": ["2026-10-22T15:16:37+00:00"]}
    )

    merged = add_option_market_features(frame, str(db))

    assert merged.loc[0, "underlying_price"] == pytest.approx(200.0)


def test_unmatched_rows_are_dropped_not_filled(tmp_path):
    """没有成交时刻行情的样本必须被丢弃，绝不能补 0 冒充真值。"""
    db = _make_db(
        tmp_path / "t.db",
        [(CODE, "2026-10-18 11:00:00", 200.0, 0.05, -0.10)],  # 超出 1 天容忍
    )
    frame = pd.DataFrame(
        {"symbol": [CODE], "created_at": ["2026-10-22T15:16:37+00:00"]}
    )

    with pytest.raises(RuntimeError, match="没有任何样本"):
        add_option_market_features(frame, str(db))


def test_snapshot_within_tolerance_matches(tmp_path):
    """容差边界内（44 分钟前）的快照必须能匹配。"""
    db = _make_db(
        tmp_path / "t.db",
        [(CODE, "2026-10-22 10:32:00", 200.0, 0.05, -0.10)],  # 10:32 ET = 14:32Z
    )
    frame = pd.DataFrame(
        {"symbol": [CODE], "created_at": ["2026-10-22T15:16:37+00:00"]}  # 11:16 ET
    )

    merged = add_option_market_features(frame, str(db))

    assert merged.loc[0, "underlying_price"] == pytest.approx(200.0)


def test_snapshot_beyond_tolerance_is_dropped(tmp_path):
    """20 小时前的快照必须丢弃 —— 旧的 1 天容差会把它当成交时刻行情（跨夜错配）。"""
    db = _make_db(
        tmp_path / "t.db",
        [(CODE, "2026-10-21 15:00:00", 200.0, 0.05, -0.10)],  # 前一日 15:00 ET
    )
    frame = pd.DataFrame(
        {"symbol": [CODE], "created_at": ["2026-10-22T15:16:37+00:00"]}
    )

    with pytest.raises(RuntimeError, match="没有任何样本"):
        add_option_market_features(frame, str(db))


def test_no_market_history_raises(tmp_path):
    db = _make_db(tmp_path / "t.db", [])
    frame = pd.DataFrame(
        {"symbol": [CODE], "created_at": ["2026-10-22T15:16:37+00:00"]}
    )

    with pytest.raises(RuntimeError, match="option_history 无任何行情记录"):
        add_option_market_features(frame, str(db))


def test_import_does_not_hijack_stdio():
    """导入导出脚本不得替换 stdout/stderr（否则 pytest capture 又被顶掉）。"""
    module = importlib.import_module("scripts.ml_export_training_data_v3")
    stdout_before, stderr_before = sys.stdout, sys.stderr

    assert module is not None
    assert sys.stdout is stdout_before, "导入期替换了 sys.stdout：会顶掉 pytest capture"
    assert sys.stderr is stderr_before, "导入期替换了 sys.stderr：会顶掉 pytest capture"

    source = Path(module.__file__).read_text(encoding="utf-8")
    module_level = [line.strip() for line in source.split("if __name__ == '__main__':")[0].splitlines()]
    assert "_force_utf8_stdio()" not in module_level
    assert "_force_utf8_stdio()" in source.split("if __name__ == '__main__':")[1]


def test_export_entrypoint_survives_gbk_stdout(tmp_path):
    """Step 2 是**手工跑导出**：GBK 控制台下打印 emoji 不得 UnicodeEncodeError。"""
    script = ROOT / "scripts" / "ml_export_training_data_v3.py"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "gbk"  # 强制复现 Windows 控制台 GBK 场景
    env.pop("PYTHONUTF8", None)

    result = subprocess.run(
        [
            sys.executable, str(script),
            "--output", str(tmp_path / "out.csv"),
            "--db", str(tmp_path / "missing.db"),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=ROOT, timeout=120,
    )

    assert "UnicodeEncodeError" not in (result.stderr or ""), (
        "入口没有接管 stdio，GBK 下 emoji 打印直接崩：\n" + (result.stderr or "")[-800:]
    )
    assert "📂" in (result.stdout or ""), "脚本应跑到打印阶段（说明入口 stdio 已修好）"
