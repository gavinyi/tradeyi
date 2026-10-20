# 飞书复盘投递设计（任务看板 → 147 机器人会话）

本文记录每日策略复盘从任务看板生成、到投递到飞书 147 机器人会话的**最终定型链路**，
以及排查过程中踩过的坑，防止日后改回去。配套脚本：`scripts/dsh_task_board_agent.py`。

## 目标

任务看板每天生成 `strategy_review_<date>` 的 HTML/Markdown 报告后，登记一条 PENDING
投递记录；DSH 定时 agent 轮询到 PENDING 后，把报告投递到**飞书 147 机器人会话**
（即用户平时看消息的地方），并标记 DELIVERED。

用户明确要求：**不要改旧调度逻辑，只把原来投微信的链路换成投飞书；且要能收到消息、
链接可见可点。**

## 最终链路（唯一正确形式，勿回退）

```text
1. 生成：run_daily_strategy_review() 写 HTML/Markdown，登记 PENDING
          （src/futu_quant/scheduled_tasks.py）
2. 轮询：uv run python scripts/dsh_task_board_agent.py poll
          - pending=false → 静默结束
          - pending=true  → 继续
3. 投递：uv run python scripts/dsh_task_board_agent.py feishu
          a) lark-cli drive +import 把 .md 导入成飞书在线文档（拿 doc_url）
          b) lark-cli im +messages-send 以 **bot** 身份，**直发 147 的 chat_id**，
             发**一条 post 富文本卡片**（含**明文 doc_url** + 结论摘要）
          c) 发送成功才 deliver_pending_im_review(mark_done=True) 标记 DELIVERED
4. 回执：再 poll 一次确认 pending=false；仍 true 则留待下一轮重试
```

关键常量（`scripts/dsh_task_board_agent.py` 顶部）：

| 常量 | 值 | 含义 |
|---|---|---|
| `FEISHU_DELIVERY_CHAT_ID` | `oc_47d5da371ac34e16cdc7b20bcdd7f27a` | 147 机器人会话，直发目标 |
| `FEISHU_REVIEW_FOLDER_TOKEN` | `IKiVf2vtTlvnswdhke5ctLhtntd` | 导入文档所在飞书文件夹 |
| 发送身份 | `--as bot` | 以 147 机器人身份发，落到用户平时收消息的会话 |

## 为什么是"直发 chat_id"，而不是 `--user-id`

- lark-cli 这个飞书应用（`cli_a96b...`）与 DSH-IM 接入的飞书 bot（`bot_5f688...` / HOME-WIN）
  是**两个不同应用**，不共享 open_id 命名空间。
- 用 `--user-id ou_4cac...`（大头账号）发，消息会被飞书落到**另一个会话**（大头账号
  自己的 `oc_b132...`），**不在**用户盯的 147 会话里 → 用户"收不到"。
- **直发 147 的 chat_id `oc_47d5...`** 才真正落到用户看消息的会话 → 已验证能收到。

> 规则：投递走 `--chat-id oc_47d5...`，**不要**改回 `--user-id`。

## 为什么 post 富文本用"明文 doc_url"，不用 `a` 链接标签

- 147 会话里，post 富文本的 `{"tag":"a","href":...}` 可点击链接**不被渲染**，会退化成
  纯文本、甚至吞掉链接地址 → 用户看到的是"纯文本、没有链接"。
- 改为把 `doc_url` **明文**写进卡片（`文档链接：https://...`），不管富文本渲染与否，
  链接都可见、可复制点开。

> 规则：doc_url 用明文，**不要**改回 `a` 标签。

## 为什么不用 Card 2.0（interactive）卡片

- Card 2.0 交互卡片发到 147 会话时，API 返回 `ok` 但**端上不渲染/不可见**，用户收不到。
- post 富文本在该会话可靠可见。

> 规则：主投递用 post 富文本，**不要**改回 interactive 卡片。

## 为什么去掉固定 `--idempotency-key`（关键根因）

- 早期脚本对同一 `session_date` 用了固定 `--idempotency-key`。
- 飞书对**相同幂等 key 会静默去重**：同一 key 只发第一次，后续发送 API 仍返回 `ok=true`
  但消息被丢弃 → 表现为"反复触发却收不到新消息"。
- 去掉固定幂等 key 后，每次触发都真发。

> 规则：**不要**加回固定幂等 key。如需防重发，由 PENDING/DELIVERED 状态机保证（标记
> 成功才不再触发），不靠幂等 key。

## 投递标记（DELIVERED）语义

- 只有 `feishu` 命令**真正把 post 富文本发送成功**（`lark-cli` 返回 `ok`）后，才调
  `deliver_pending_im_review(mark_done=True)` 标记 DELIVERED。
- 任一步失败（导入无 url / 发送失败）→ 保持 PENDING，**不标 DELIVERED**，等下一轮重试。
- 杜绝"API 说发成功、但端上没收到、却已标 DELIVERED"的状态漂移。

## 手动测试 / 重置

- 查看当前状态：`uv run python scripts/dsh_task_board_agent.py poll`
- 把已 DELIVERED 的记录重置回 PENDING 再测（临时脚本，用完可删）：

  ```python
  # 取 IM_DELIVERY_PENDING_KEY 记录，置 status=PENDING、清 delivered_at/channel，写回
  ```

- 触发一次投递（会真发到 147）：`uv run python scripts/dsh_task_board_agent.py feishu`

## 防回退清单（日后改动前自查）

- [ ] 投递仍走 `--chat-id oc_47d5...`（不是 `--user-id`）
- [ ] doc_url 明文写进卡片（不是 `a` 标签）
- [ ] 主投递是 post 富文本（不是 Card 2.0 interactive）
- [ ] 没有固定 `--idempotency-key`
- [ ] 只有发送成功才标 DELIVERED
- [ ] 不使用 `dsh_im_return_file`（任务看板后台轮次没有 DSH-IM 消费者）

## 相关

- 任务看板提示词：`C:\Users\Administrator\.dsh\task-board\ledger-v2.json`（已同步上述约束）
- 投递记录存储：`src/futu_quant/scheduled_tasks.py` 的 `IM_DELIVERY_PENDING_KEY`
