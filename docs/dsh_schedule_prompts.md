# DSH 定时 agent 提示词（任务看板定时器）

> 方案（推荐，已定）：**Python `run_service.py` 精确定时**触发 04:15 生成复盘 + 登记
> 待投递记录、04:30 关机；**DSH Schedule 只挂一条"轮询待投递"提醒**，让 agent 把
> 已登记的复盘通过 dsh IM 微信 chat 插件投到用户微信。时间精确性由 Python 侧保证，
> 微信投递走 agent 能力。
>
> 每个命令的 stdout 都是**单个 JSON 对象**，agent 按 JSON 字段判断分支。

## 一、Python 侧（已写好，无需改动，自动运行）

`scripts/run_service.py` 每天（中国时间）：

- **05:15** `daily_strategy_review`：生成上一个美东交易日的策略复盘与执行质量 HTML
  （`data/reports/strategy_review_<日期>.html`），并把 `conclusion` + 路径登记为
  `DAILY_STRATEGY_REVIEW_IM_PENDING` 待投递记录（默认启用）。
- **05:30** `daily_universe_ranking`：标的池 ML 精排、沙盒自评与外援轮换仲裁。
- **06:00** `computer_shutdown`：电脑关机（默认关闭 + `AUTO_SHUTDOWN_DRY_RUN=true` 演练）。

两者均"当日一次"幂等、停机错过可补跑，可在"系统监控 → 调度任务 → 任务看板定时器"启停。

## 二、DSH 侧（需你在开了 Schedule 插件的会话里建一次）

DSH Schedule 的重复提醒是**固定间隔（≥5 分钟）**、对齐创建时刻，不能精确表达"日历每天 04:15"。
所以 DSH 这边只负责**投递轮询**：每隔一段固定时间让 agent 检查有没有待投递复盘，
有就投微信。生成与关机的精确定时由 Python 侧兜底，两者解耦、互不影响。

### 1. 轮询投递的 agent 提示词（粘进 `schedule_create` 的 `prompt`）

```
你是"任务看板复盘微信投递"轮询 agent，在 E:\Thisyi\trade 目录下执行：

1. 跑 `uv run python scripts/dsh_task_board_agent.py poll`，解析 stdout 的 JSON：
   - pending=false → 本轮无待投递，什么都不做，安静结束（不要发消息）。
   - pending=true → 记下 session_date、conclusion、html_path，继续。
2. pending=true 时：把 conclusion 文本（复盘总结）通过当前 DSH IM 微信 chat 渠道发给用户；
   再调用 `dsh_im_return_file` 把 html_path 指向的 HTML 文件登记到同一渠道，
   渠道会在本轮结束后自动上传到用户的微信 chat。
3. 发完后跑 `uv run python scripts/dsh_task_board_agent.py deliver`，把记录标记为
   DELIVERED（解析 JSON：delivered=true 成功；delivered=false 说明已被别的轮次投过，跳过）。
4. 仅当 pending=true 且投递成功时，才简短回执：已投递 + session_date + html_path。
```

### 2. 建这条轮询提醒的 `schedule_create` 命令

在**开了 DSH Schedule 插件**的会话里，让 agent 执行（或直接用工具）：

```
schedule_create(
  prompt = <上面第 1 段提示词>,
  every_seconds = 600      # 每 10 分钟轮询一次；也可 300（5 分钟）
)
```

说明：
- `every_seconds` 只设 600/300 即可，**不需要**精确对齐 04:15——因为真正的"到点生成"
  由 Python 精确保证；DSH 这边只是"有货就发"，10 分钟内一定能把 04:15 生成的复盘投出去。
- 轮询是幂等且安静的：没有 PENDING 时 `poll` 返回 `pending: false`，agent 不动作、不发噪音；
  发过之后 `deliver` 把记录翻成 DELIVERED，后续轮次不会再重发。
- 想停用：`schedule_delete(id=<schedule_list 里查到的 id>)`。

## 三、06:00 关机（可选挂到 DSH，或直接依赖 Python）

- **推荐**：关机继续由 Python `run_service.py` 精确 06:00 触发（默认演练），最稳。
- 若你想让 DSH agent 也把关机做进轮询里，可在上面提示词第 2 步之后追加：
  `uv run python scripts/dsh_task_board_agent.py shutdown --dry-run`（演练，不真关机）。
  真实关机必须人工把 `.env` 的 `AUTO_COMPUTER_SHUTDOWN_ENABLED=true` 且
  `AUTO_SHUTDOWN_DRY_RUN=false`，且当天复盘确认已投递，再跑不带 `--dry-run` 的 shutdown。

## 四、安全门禁

- 复盘投递默认启用，只把 HTML 文件登记到已接好的 DSH IM 微信 chat 渠道，不调用任何外部 webhook。
- 关机默认关闭 + 演练（`AUTO_COMPUTER_SHUTDOWN_ENABLED=false`、`AUTO_SHUTDOWN_DRY_RUN=true`）；
  真关机需手动改两个开关并在 DSH 里明确确认意图。
