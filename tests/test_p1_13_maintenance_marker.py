"""P1-13 Layer 1 第一块：启停维护标记（`data\\.maintenance`）。

为什么需要：守护脚本按「PID 存活性 + 心跳时效」判失联。没有维护标记时，
用户双击 `stop.bat` 的正常停机会被判成「进程暴毙」，每 30 分钟一轮 CRITICAL 告警，
告警被刷成噪音后就没人看 —— 那比不告警更糟。

本文件只守「启停脚本侧」的四条不变量（不依赖 PowerShell/进程，纯静态判据）：

1. `stop.bat` 必须在**任何 taskkill 之前**写入 `data\\.maintenance`；
   （`stop.bat` 是 GBK 编码，必须按 cp936 解码 —— 用 utf-8 读会乱码，判据会失效）
2. `stop.bat` 的**失败退出路径**（有 Worker 没停掉、`exit /b 1`）必须删掉标记：
   否则一次失败的停止会让守护永久静默（下次异常没人知道）；
3. `start.bat` 必须在**拉起任何进程之前**删掉标记（用户点了启动 = 维护窗口结束）；
4. 两个脚本里的标记路径必须一致（`data\\.maintenance`），否则一个写一个删不上。

变异：把 stop.bat 的标记写入移到第一个 taskkill 之后 → 第 1 条转红；
删掉失败路径的 `del` → 第 2 条转红；把 start.bat 的 `del` 删掉 → 第 3 条转红。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MARKER = r"data\.maintenance"


def _read_stop_bat() -> str:
    """`stop.bat` 是 GBK（cp936）编码 —— 用 utf-8 读会得到乱码，判据形同虚设。"""
    return (ROOT / "stop.bat").read_bytes().decode("cp936")


def _read_start_bat() -> str:
    # `start.bat` 与 `stop.bat` 统一用 **cp936**（cmd 默认代码页）：
    # 用 utf-8 读 GBK 文件、或用 GBK 读 UTF-8 文件，都会把多字节序列读花，
    # 判据会变成「看起来像缺陷」的假象（今晚已踩过两次）。
    # `%DATA%` 就是 `%ROOT%data`；统一成字面量再判定，避免变量拼法差异让判据失效。
    return (ROOT / "start.bat").read_bytes().decode("cp936").replace("%DATA%", "data")


def _line_index(text: str, needle: str) -> int:
    for index, line in enumerate(text.splitlines()):
        if needle in line:
            return index
    return -1


def test_stop_bat_writes_maintenance_marker_before_any_kill():
    """标记必须写在**任何杀进程动作之前**，否则暴毙告警会抢在标记前面发出去。"""
    text = _read_stop_bat()
    marker_index = _line_index(text, MARKER)
    assert marker_index >= 0, "stop.bat 没有写维护标记 —— 正常停机会被守护误报成暴毙"
    # 标记行必须是「写」而不是「删」：`> "data\.maintenance"` 或 `echo ... > ...`
    assert "> \"" + MARKER + "\"" in text or ">\"" + MARKER + "\"" in text, (
        "stop.bat 里的标记行是删除/注释，不是写入"
    )
    first_kill = _line_index(text, "taskkill")
    assert first_kill >= 0, "stop.bat 里没有 taskkill，夹具失效"
    assert marker_index < first_kill, (
        f"维护标记写在第一个 taskkill 之后（marker 行 {marker_index + 1}，"
        f"taskkill 行 {first_kill + 1}）—— 停机瞬间仍会误报警"
    )


def test_every_failure_exit_clears_marker():
    """**全量不变量**：`stop.bat` 里每一个 `exit /b 1` 之前都必须清掉标记。

    为什么不能只锚定某一个失败块：`stop.bat` 有两条失败退出（提权重试放弃、
    有 Worker 未停掉），只覆盖一条就会漏。提权那条尤其致命 —— 用户点 UAC「否」
    时原脚本 `exit /b 1`，Worker 还在跑、标记却留在盘上，守护从此永久静默。
    """
    lines = _read_stop_bat().splitlines()
    exit_indexes = [i for i, line in enumerate(lines) if line.strip().lower() == "exit /b 1"]
    assert exit_indexes, "stop.bat 里没有 exit /b 1，夹具失效"
    for index in exit_indexes:
        window = lines[max(0, index - 8):index]
        assert any("del" in line and MARKER in line for line in window), (
            f"第 {index + 1} 行的 exit /b 1 之前没有清理维护标记 —— "
            "这条失败路径会让守护永久静默（Worker 还在跑却不再告警）"
        )


def test_start_bat_clears_marker_before_launching():
    """`start.bat` 必须在拉起任何进程之前删标记（用户启动 = 维护结束）。"""
    text = _read_start_bat()
    lines = text.splitlines()
    del_index = next(
        (i for i, line in enumerate(lines) if MARKER in line and line.strip().lower().startswith("del")),
        -1,
    )
    assert del_index >= 0, "start.bat 没有清除维护标记 —— 一次停止后守护会永久静默"
    launch_index = next(
        (
            i
            for i, line in enumerate(lines)
            if "launch_workers.py" in line or "run_radar_service.py" in line
        ),
        -1,
    )
    assert launch_index >= 0, "start.bat 里找不到拉起进程的动作，夹具失效"
    assert del_index < launch_index, "清标记必须在拉起进程之前"


def test_marker_path_consistent_between_start_and_stop():
    """写/删必须指向同一个文件，否则守护永远看不到标记。"""
    assert MARKER in _read_stop_bat()
    assert MARKER in _read_start_bat()

def test_bat_scripts_share_encoding_and_line_endings():
    """配套两个 `.bat` 必须同编码（cp936）、同换行（CRLF）。

    为什么值得单独立一条守卫：`start.bat` 曾是 UTF-8、`stop.bat` 是 GBK，
    用户双击 start.bat 时中文显示成乱码（与「杜绝乱码」的既有要求相反）；
    而且混合编码会让读脚本的人（含 AI）反复误判，今晚三个人踩过。
    """
    for name in ("start.bat", "stop.bat"):
        raw = (ROOT / name).read_bytes()
        try:
            raw.decode("cp936")
        except UnicodeDecodeError as exc:  # pragma: no cover - 触发时给出可行动信息
            raise AssertionError(f"{name} 不是 cp936 编码：{exc}") from exc
        lone_lf = raw.replace(b"\r\n", b"").count(b"\n")
        assert lone_lf == 0, f"{name} 存在 {lone_lf} 处裸 LF 换行，应统一为 CRLF"
