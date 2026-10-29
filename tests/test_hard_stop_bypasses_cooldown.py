"""回归：硬止损必须绕过退出冷却（P0-3，10-22 $366 超调的根因）。

背景：
`run_campaign` 的冷却分支一旦命中就 `return`，会挡住后面的硬止损判定 ——
冷却本意是「止损后 24h 内不再开新仓」，却把「已持仓的退出」一起挡住。
10-22 实盘 META 看跌就是这样：亏损已过 $300 线，但因为冷却先命中，
硬止损要等下一个周期才有机会，最终由 orphan recovery 以 $366 被动平仓。

修复：把硬止损判定提前到所有「提前 return」分支之前，命中即置 action="SELL"
并标记 `hard_stop_fired`，让后续常规决策不覆盖它。

本文件锁定「冷却存在时硬止损仍必须触发」这条不变量。
"""
from __future__ import annotations

import ast
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "scripts" / "run_gamma_strategy.py"


def _run_campaign_source() -> str:
    src = TARGET.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_campaign")
    return "\n".join(src.splitlines()[fn.lineno - 1 : fn.end_lineno])


def test_hard_stop_is_evaluated_before_cooldown_block():
    """硬止损判定必须出现在冷却块之前（否则会被冷却的提前 return 挡住）。

    锚点用**赋值行**（`max_loss_usd=...`，由共享解析器 `_hard_stop_limits` 产出），
    并限定在 `run_campaign` 函数体内 —— 加了模块级 helper 后行号会漂移，
    用固定行号区间过滤会失效（实测踩过）。
    """
    lines = TARGET.read_text(encoding="utf-8").splitlines()
    tree = ast.parse("\n".join(lines))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_campaign")
    body = lines[fn.lineno - 1 : fn.end_lineno]
    offset = fn.lineno - 1
    # 锚点用共享解析器的调用点（唯一、稳定）—— 两层都走它。
    # 2026-10-29 起锚点换成 `_locked_hard_stop_limits`：绝对额要在建仓时锁定，
    # 由它包装 `_hard_stop_limits`，两层仍共用同一处实现（守卫语义不变）。
    hard_stop_line = next(
        i + offset for i, t in enumerate(body)
        if "_locked_hard_stop_limits(" in t
    )
    cooldown_line = next(
        i + offset for i, t in enumerate(body) if "cooldown_key = f\"volatility_cooldown:" in t
    )
    assert hard_stop_line < cooldown_line, (
        "硬止损判定位于冷却块之后 —— 冷却命中即 return，硬止损将永远等不到执行"
    )


def test_hard_stop_sets_sell_action_and_flag():
    """命中硬止损时必须置 action=SELL 并打标记，防止后续常规决策覆盖。"""
    body = _run_campaign_source()
    assert 'action, reason = "SELL", _hard_reason' in body, "硬止损未置 SELL action"
    assert "hard_stop_fired = True" in body, "硬止损未打标记，后续决策会覆盖它"


def test_normal_decision_does_not_override_hard_stop():
    """常规决策与分批止盈分支必须在硬止损命中时跳过（否则会覆盖 SELL）。

    10-23 修正：此前这条守卫断言的是 `if not hard_stop_fired:` ——
    而那是**死代码**（`hard_stop_fired` 只在 VOLATILITY 分支置位，
    该条件在 EARNINGS 分支恒真）。断言一个恒真条件 = 空转守卫。
    现改为断言**真实的三段式结构**：`if hard_stop_fired: pass` 必须存在，
    且必须排在分批止盈分支之前。
    """
    body = _run_campaign_source()
    assert "if hard_stop_fired:" in body, (
        "缺少硬止损优先分支 —— 分批止盈/常规决策会覆盖硬止损的 SELL"
    )
    # 结构顺序：硬止损分支必须排在分批止盈分支之前
    hs = body.index("if hard_stop_fired:")
    pe = body.index("elif partial_exit_quantity > 0")
    assert hs < pe, (
        "硬止损分支位于分批止盈之后 —— 分批止盈会先命中并覆盖 action"
    )


def test_partial_exit_does_not_override_hard_stop():
    """分批止盈不得无条件覆盖硬止损（P0-3 的原缺陷）。"""
    body = _run_campaign_source()
    # 分批止盈分支必须带 hard_stop_fired 保护（或位于硬止损的 elif 之后）
    assert "elif partial_exit_quantity > 0" in body, (
        "分批止盈分支未受 hard_stop_fired 三段式保护"
    )


def test_dead_always_true_wrapper_removed():
    """回归：`if not hard_stop_fired:` 死代码包裹必须已删除。

    它恒为真（EARNINGS 分支下 hard_stop_fired 恒 False），
    留着会让人误以为那里有保护 —— 实际保护在 VOLATILITY 分支。

    用 **AST** 判定而非字符串匹配：注释里出现同样的字样不算违规。
    """
    src = TARGET.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_campaign")
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        # 判定 `not hard_stop_fired` / `hard_stop_fired == False` 形态
        dump = ast.dump(node.test)
        if "hard_stop_fired" in dump and ("Not" in dump or "Eq" in dump):
            pytest_fail = (
                "死代码 `if not hard_stop_fired:` 仍在（恒真条件）—— "
                "它让人误以为那里有保护，实际保护在 VOLATILITY 分支"
            )
            raise AssertionError(pytest_fail)


def test_defense_layers_are_documented():
    """两层的职责必须写进注释 —— 否则下一个人会以为"重复"而删掉一层。

    Chief 裁定（10-23）：保留两层，是纵深防御不是冗余。
    注释在 `run_campaign` 函数体内（`_hard_stop_limits` 的 docstring 里也有
    同样字样，不能作为判据）。
    """
    body = _run_campaign_source()
    assert "硬止损纵深防御 · 第 1 层" in body, "第 1 层未写明职责"
    assert "硬止损纵深防御 · 第 2 层" in body, "第 2 层未写明职责"


def test_valid_limits_are_not_rejected():
    """**反向守卫（Chief 规矩）**：合法限额不得被误拒。

    校验器变严（缺键 + 非法值都拒绝）最容易出的错是**误伤合法值** ——
    那会让所有正常 campaign 都开不了仓。这条钉住"合法值必须通过"。
    """
    from scripts.run_gamma_strategy import _stop_limit_problems

    cases = [
        {"max_position_loss_usd": 300.0, "max_position_loss_ratio": 0.25},
        {"max_position_loss_usd": 1, "max_position_loss_ratio": 1},       # 边界：最小/最大
        {"max_position_loss_usd": 150.0, "max_position_loss_ratio": 0.001},
    ]
    for campaign in cases:
        missing, invalid = _stop_limit_problems(campaign)
        assert not missing and not invalid, (
            f"合法限额被误判为配置错误：campaign={campaign} → missing={missing} invalid={invalid}"
        )


def test_invalid_limits_are_caught():
    """非法限额必须被检出（0 / 负数 / 超范围 / 不可解析）。"""
    from scripts.run_gamma_strategy import _stop_limit_problems

    bad = [
        ({"max_position_loss_usd": 0, "max_position_loss_ratio": 0.25}, "usd=0"),
        ({"max_position_loss_usd": -100, "max_position_loss_ratio": 0.25}, "usd<0"),
        ({"max_position_loss_usd": 300, "max_position_loss_ratio": 0}, "ratio=0"),
        ({"max_position_loss_usd": 300, "max_position_loss_ratio": -0.25}, "ratio<0"),
        ({"max_position_loss_usd": 300, "max_position_loss_ratio": 1.5}, "ratio>1"),
        ({"max_position_loss_usd": "abc", "max_position_loss_ratio": 0.25}, "不可解析"),
        ({"max_position_loss_usd": 300}, "缺 ratio 键"),
    ]
    for campaign, label in bad:
        missing, invalid = _stop_limit_problems(campaign)
        assert missing or invalid, f"非法限额未被检出（{label}）：{campaign}"


def test_entry_validation_uses_the_shared_helper():
    """开仓校验必须走 `_stop_limit_problems`（不得内联第二份判定）。"""
    body = _run_campaign_source()
    assert "_stop_limit_problems(campaign)" in body, (
        "开仓校验未走共享 helper —— 会与测试形成两份实现"
    )


def test_both_layers_share_one_limit_resolver():
    """**数据层保护（Reviewer 保留意见）**：两层必须读**同一份**限额解析。

    两层在控制流上独立，但数据上耦合 —— 共享数据源是共同失败点。
    收敛到 `_hard_stop_limits` 一处后，数据层出错不会再"两层各算一遍"。

    2026-10-29：绝对额改为按建仓权利金缩放并在建仓时锁定，因此外层多了
    `_locked_hard_stop_limits`（锁 + 比例缩减）。两层仍然只走**这一个**入口，
    且它内部只调 `_hard_stop_limits` 一次 —— 解析实现依旧只有一份。
    """
    src = TARGET.read_text(encoding="utf-8")
    # 3 = 1 处定义 + 2 处调用点（两层各一）
    assert src.count("def _locked_hard_stop_limits(") == 1, "锁定包装器被复制成多份"
    assert src.count("_locked_hard_stop_limits(") == 3, (
        "两处硬止损判定未都走共享解析器 —— 数据层又分叉了"
    )
    # 不得再出现裸读取（各读各的 = 共同失败点）
    body = _run_campaign_source()
    assert 'float(campaign.get("max_position_loss_usd", 300))' not in body, (
        "仍有裸读取限额 —— 与共享解析器并存会形成两个数据源"
    )


def test_zero_or_negative_limit_is_rejected_loudly(tmp_path):
    """限额 <= 0 时必须记 ERROR 并回落保守默认（否则止损永不触发）。"""
    from scripts.run_gamma_strategy import _hard_stop_limits

    class _Ex:
        class store:
            @staticmethod
            def operational_event(*a, **k):
                _Ex.events.append((a, k))

        events: list = []

        class alerts:
            @staticmethod
            def send(*a, **k):
                _Ex.alerts_sent.append((a, k))

        alerts_sent: list = []

    limits = _hard_stop_limits(
        {"max_position_loss_usd": 0, "max_position_loss_ratio": 0.25},
        _Ex(), "US.TEST",
    )
    assert limits["max_loss_usd"] == 300.0 and limits["max_loss_ratio"] == 0.25, (
        "限额为 0 时未回落保守默认 —— 止损会静默永不触发"
    )
    assert limits["premium_basis"] is None, "回落路径不应声称有权利金基数"
    assert _Ex.events and _Ex.events[0][0][1] == "HARD_STOP_LIMIT_INVALID", (
        "限额为 0 未记 ERROR —— 这是静默失效"
    )
    assert _Ex.events[0][0][2] == "ERROR", "必须是 ERROR 级别"
    assert _Ex.alerts_sent and _Ex.alerts_sent[0][0][1] == "HARD_STOP_LIMIT_INVALID", (
        "非法止损限额必须同时触发 alerts.send，不能只落 operational_event 而静默"
    )


def test_valid_limits_pass_through():
    """未提供权利金基数时（老调用点/单测），上限即实际止损额。"""
    from scripts.run_gamma_strategy import _hard_stop_limits

    limits = _hard_stop_limits(
        {"max_position_loss_usd": 150.0, "max_position_loss_ratio": 0.20},
        None, "US.TEST",
    )
    assert limits["max_loss_usd"] == 150.0
    assert limits["max_loss_ratio"] == 0.20


def test_layer1_precedes_layer2():
    """两层顺序必须正确：第 1 层（分支内）在第 2 层（分支后）之前。

    锚点用**代码**而非注释（`_hard_stop_limits` 的 docstring 里也含"第 1/2 层"
    字样，用注释定位会双双指向同一行 —— 实测踩过）。
    """
    lines = TARGET.read_text(encoding="utf-8").splitlines()
    l1 = next(i for i, t in enumerate(lines, start=1) if t.strip() == "if hard_stop_fired:")
    l2 = next(
        i for i, t in enumerate(lines, start=1)
        if 'action, reason = "SELL", hard_loss_reason' in t
    )
    assert l1 < l2, "第 1 层必须在第 2 层之前"


def test_no_branch_downgrades_action_after_layer2():
    """**结构守卫（AST）**：第 2 层之后不得有任何分支把 action 改回非 SELL。

    第 2 层在决策分支全部走完后重算硬止损并置 action="SELL"。
    此后若出现 `action = "HOLD"` / `action = "BUY"` 之类的赋值，
    止损就会被无声降级 —— 这正是 $366 那笔的失效形态。

    用 AST 判定（不是字符串匹配）：只看**真实的赋值语句**，注释不算。
    **第 2 层的定位锚用它的赋值语句**（`action, reason = "SELL", hard_loss_reason`）
    —— 用注释定位会被第 1 层注释里的"第 2 层"字样误导（实测踩过）。
    """
    src = TARGET.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_campaign")

    marker = 'action, reason = "SELL", hard_loss_reason'
    layer2_line = next(
        (i for i, t in enumerate(lines, start=1) if marker in t), None
    )
    assert layer2_line is not None, "找不到第 2 层的赋值锚点 —— 结构已变，守卫需同步更新"

    offenders = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or node.lineno <= layer2_line:
            continue
        # 赋值目标可能是 Name（`action = ...`）**或 Tuple**（`action, reason = ...`）——
        # 只认 Name 会把所有元组解包赋值静默跳过（实测：计数 0，守卫空转）。
        names: list[str] = []
        for t in node.targets:
            if isinstance(t, ast.Name):
                names.append(t.id)
            elif isinstance(t, ast.Tuple):
                names.extend(e.id for e in t.elts if isinstance(e, ast.Name))
        if "action" not in names:
            continue
        # 取 **action 那一位**的字面量 —— `action, reason = "SELL", "到期..."` 里
        # 第二个字面量是 reason，不是 action；按位置取，避免把 reason 误判成降级。
        idx = names.index("action")
        value = node.value
        if isinstance(value, ast.Tuple) and idx < len(value.elts):
            slot = value.elts[idx]
        else:
            slot = value
        literal = slot.value if isinstance(slot, ast.Constant) and isinstance(slot.value, str) else None
        if literal is not None and literal != "SELL":
            offenders.append((node.lineno, [literal]))
    assert not offenders, (
        f"第 2 层之后存在把 action 降级为非 SELL 的赋值：{offenders} —— "
        "止损会被无声降级（$366 的失效形态）"
    )
    """回归：既有的硬止损块必须保留（幂等，重复命中无副作用）。"""
    body = _run_campaign_source()
    assert "hard_loss_reason" in body, "既有硬止损块被误删"
