import json
import subprocess

doc_url = "https://my.feishu.cn/docx/CpIUdt4troau86xYbGZcxczOnKg"
conclusion = (
    "运行健康层面没有发现任务异常或退化。\n"
    "波动率期权产生 19 个合约记录，已实现盈亏约 $306.00，当前仍有 0 个未平合约。\n"
    "按合约成交表现看，盈利 3 个、亏损 1 个，胜率约 75%；最佳是 US.AMD261016P610000（约 $277），最弱是 US.TSM261016P450000（约 $-92）。"
)

card = {
    "schema": "2.0",
    "config": {"wide_screen_mode": "default"},
    "header": {
        "title": {"tag": "plain_text", "content": "策略复盘报告 2026-10-08"},
        "template": "blue",
    },
    "body": [
        {
            "tag": "markdown",
            "content": f"**结论摘要**\n{conclusion}",
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "打开飞书文档"},
            "type": "primary",
            "behaviors": [{"type": "open_url", "url": doc_url}],
        },
    ],
}

cmd = [
    r"C:\Users\Administrator\AppData\Roaming\npm\lark-cli.cmd",
    "im", "+messages-send",
    "--user-id", "ou_4cac2ff663fb66d684601aed508c9426",
    "--msg-type", "interactive",
    "--content", json.dumps(card, ensure_ascii=False),
    "--as", "bot",
]
proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=r"E:\Thisyi\trade")
print(proc.stdout or proc.stderr)
