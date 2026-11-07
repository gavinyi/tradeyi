"""参数寻优包守卫（P1-②，2026-11-07，PRD data/qa/prd_p1_batch_20261107.md §2）。

四条硬验收：参数空间事前落盘禁变更 / 缺样本外视为未交付 / 衰减>60% 判过拟合 /
寻优链路无任何配置写入 + 同输入确定性一致。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from futu_quant.signal_backtest import optimizer as opt

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "backtest_cli.py"


# ---------- 参数空间（PRD 2.2-1） ----------

def test_space_declaration_freezes_combinations():
    space = opt.declare_parameter_space(
        {"z": [1.8, 2.0], "move": [0.02, 0.03]}, declared_at="w1", name="t",
    )
    assert space.size == 4
    combos = [json.dumps(c, sort_keys=True) for c in space.combinations]
    assert len(set(combos)) == 4
    # frozen：不可追加/裁剪
    import dataclasses
    try:
        space.combinations  # 可读
        object.__setattr__  # noqa: B018 - 占位
        with__setattr = getattr(space, "__dict__", {})
        frozen_ok = dataclasses.is_dataclass(space) and space.__dataclass_params__.frozen
        assert frozen_ok, "ParameterSpace 必须 frozen（杜绝事后改空间）"
    except Exception as exc:  # pragma: no cover
        raise AssertionError(f"frozen 校验失败：{exc}")


def test_empty_axes_rejected():
    import pytest
    with pytest.raises(ValueError):
        opt.declare_parameter_space({}, declared_at="w")
    with pytest.raises(ValueError):
        opt.declare_parameter_space({"z": []}, declared_at="w")


def test_declaration_json_roundtrip():
    space = opt.declare_parameter_space(
        {"z": [2.0, 2.2]}, declared_at="2026-11-07", name="grid",
    )
    doc = json.loads(opt.space_declaration_json(space))
    assert doc["size"] == 2 and doc["declared_at"] == "2026-11-07"
    assert sorted(doc["axes"]["z"]) == [2.0, 2.2]


# ---------- 窗口切分（PRD 2.2-2） ----------

def test_split_window_70_30():
    start, train_end, verify_start, end = opt.split_window(
        "2026-11-01T00:00:00+00:00", "2026-11-10T00:00:00+00:00",
    )
    assert train_end == verify_start
    # 9 天 * 0.7 = 6.3 天
    assert train_end.startswith("2026-11-07T07:12:00")
    assert end == "2026-11-10T00:00:00+00:00"


# ---------- 衰减分档（PRD 2.2-3） ----------

def _report(train_pnl: float, oos_pnl: float, train_trades: int = 40,
            oos_trades: int = 40) -> opt.OptimizerReport:
    space = opt.declare_parameter_space({"z": [2.0]}, declared_at="w")
    arm_train = opt.ArmResult(params={"z": 2.0}, trades=train_trades, pnl=train_pnl)
    arm_oos = opt.ArmResult(params={"z": 2.0}, trades=oos_trades, pnl=oos_pnl)
    return opt.OptimizerReport(
        space=space, train_window=("a", "b"), verify_window=("c", "d"),
        train_results=[arm_train], out_of_sample=[arm_oos],
    )


def test_decay_verdict_three_bands():
    assert "可关注" in _report(100.0, 90.0).decay_verdict()          # 10% <30%
    assert "存疑" in _report(100.0, 55.0).decay_verdict()            # 45%
    assert "过拟合" in _report(100.0, 20.0).decay_verdict()          # 80% >60%
    assert "不建议采用" in _report(100.0, 20.0).decay_verdict()


def test_decay_none_when_train_pnl_not_positive():
    report = _report(-5.0, -7.0)
    assert report.decay_ratio is None
    assert "不作推荐" in report.decay_verdict()


# ---------- 交付完整性与推荐位（PRD 2.3） ----------

def test_missing_out_of_sample_is_not_deliverable():
    space = opt.declare_parameter_space({"z": [2.0]}, declared_at="w")
    report = opt.OptimizerReport(
        space=space, train_window=("a", "b"), verify_window=("c", "d"),
        train_results=[opt.ArmResult(params={"z": 2.0}, trades=40, pnl=50.0)],
        out_of_sample=[],  # 缺样本外列
    )
    assert report.is_deliverable is False
    assert report.recommendation()["eligible"] is False


def test_small_sample_cannot_enter_recommendation():
    report = _report(100.0, 90.0, train_trades=10, oos_trades=10)
    rec = report.recommendation()
    assert rec["eligible"] is False
    assert "样本不足" in rec["reason"]


# ---------- 随机基准（PRD 2.2-4） ----------

def test_random_baseline_is_mean_not_max():
    space = opt.declare_parameter_space({"z": [1.0, 2.0, 3.0]}, declared_at="w")
    arms = [opt.ArmResult(params={"z": z}, trades=40, pnl=p)
            for z, p in ((1.0, 10.0), (2.0, 20.0), (3.0, 300.0))]
    baseline = opt.random_pick_baseline(space, arms)
    assert baseline == (10.0 + 20.0 + 300.0) / 3  # 均值，不是挑最好的 300


# ---------- 确定性（PRD 2.4-5） ----------

def test_same_input_same_output():
    def make():
        return opt.declare_parameter_space(
            {"z": [1.8, 2.0, 2.2], "m": [0.01, 0.02]}, declared_at="w", name="g",
        )
    a, b = make(), make()
    assert [json.dumps(c, sort_keys=True) for c in a.combinations] == \
           [json.dumps(c, sort_keys=True) for c in b.combinations]


# ---------- 评估器空间冻结（PRD 2.2-1 执行点） ----------

def test_evaluate_grid_rejects_out_of_space_results():
    space = opt.declare_parameter_space({"z": [2.0]}, declared_at="w")

    def bad_arm(params, ws, we):
        return opt.ArmResult(params={"z": 9.9}, trades=1, pnl=1.0)  # 串组

    import pytest
    with pytest.raises(ValueError):
        opt.evaluate_grid(space, "a", "b", bad_arm)


# ---------- 硬性禁止：寻优链路无配置写入（PRD 2.4-3） ----------

def test_optimizer_modules_write_no_runtime_config():
    """AST 守卫：optimizer 模块与 CLI optimize 路径不得出现任何配置写入调用。"""
    banned = ("set_setting", "save_strategy_limit", "upsert_setting",
              "mirror_env", "limit_profile_write", "UPDATE runtime_config",
              "INSERT INTO runtime_config", "INSERT OR REPLACE INTO runtime_config")
    for path in (ROOT / "src" / "futu_quant" / "signal_backtest" / "optimizer.py", CLI):
        source = path.read_text(encoding="utf-8")
        assert source, f"{path} 为空"
        for token in banned:
            assert token not in source, f"{path.name} 出现配置写入点：{token}"


def test_cli_optimize_has_no_config_write_import():
    """CLI 的 optimize 分支只允许 read_only 打开与报告落盘。"""
    source = CLI.read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_optimize")
    seg = ast.get_source_segment(source, fn)
    assert "read_only=True" in seg, "寻优必须只读打开库"
    for banned in ("set_setting", "save_", "upsert_", "INSERT"):
        assert banned not in seg, f"run_optimize 出现写入调用：{banned}"
    assert "parameter_space.json" in seg and "report.md" in seg, \
        "寻优产物必须含参数空间声明与报告"


def test_mutation_dropping_out_of_sample_must_be_red():
    """变异：把 is_deliverable 改成恒 True（删样本外验证）→ 守卫必红。"""
    import shutil
    import subprocess
    import sys

    target = ROOT / "src" / "futu_quant" / "signal_backtest" / "optimizer.py"
    original = target.read_text(encoding="utf-8")
    anchor = "        return bool(self.out_of_sample) and self.top_train is not None"
    assert anchor in original, "交付完整性锚点丢失"
    mutated = original.replace(anchor, "        return True", 1)
    assert mutated != original
    try:
        target.write_text(mutated, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"{Path(__file__)}::test_missing_out_of_sample_is_not_deliverable",
             "--capture=no"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        assert result.returncode != 0, "删掉样本外验证后守卫仍绿——未交付报告可溜过"
    finally:
        target.write_text(original, encoding="utf-8")
        shutil.rmtree(ROOT / ".pytest_cache", ignore_errors=True)
