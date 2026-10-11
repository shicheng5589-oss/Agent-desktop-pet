"""状态桥写入器：一行命令把任意 agent 的状态喂给桌宠。

用法::

    python status-bridge.py --agent openclaw --state working --title "修复登录 bug" --activity "运行测试"
    python status-bridge.py --agent hermes --state done --say "改好了" --tools 3
    python status-bridge.py --agent openclaw --json "{\"state\":\"waiting\",\"title\":\"等你确认\"}"
    python status-bridge.py --agent openclaw --clear

写入 ``~/.codexpet/status/<agent>.json``（先写 .tmp 再原子替换，桌宠不会读到半截文件）。
桌宠每秒读一次，取 3 分钟内更新过的那份覆盖在本地记录解析出来的状态上面。

state 取值：idle / thinking / working / waiting / review / syncing / blocked / error / done / interrupted

挂到 agent 的 hook / 插件 / 计划任务里就能用；也可以在自己写的 harness 里直接调用本文件。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

BRIDGE_DIR = os.path.join(os.path.expanduser("~"), ".codexpet", "status")
STATES = (
    "idle", "thinking", "working", "waiting", "review",
    "syncing", "blocked", "error", "done", "interrupted",
)


def _parse_json(value: str) -> dict:
    if value == "-":
        value = sys.stdin.read()
    try:
        data = json.loads(value)
    except ValueError as exc:
        raise SystemExit("--json 不是合法的 JSON：%s" % exc)
    if not isinstance(data, dict):
        raise SystemExit("--json 需要是一个 JSON 对象，比如 {\"state\":\"working\"}")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="把 agent 状态写进桌宠的状态桥文件")
    parser.add_argument("--agent", required=True, help="agent id（openclaw / hermes / 自建 harness 的名字都行）")
    parser.add_argument("--state", default="", choices=[""] + list(STATES), help="状态")
    parser.add_argument("--label", default="", help="状态文字（默认按 state 填）")
    parser.add_argument("--title", default="", help="任务名")
    parser.add_argument("--activity", default="", help="正在做什么")
    parser.add_argument("--say", default="", help="最后一句回复")
    parser.add_argument("--elapsed", type=int, default=0, help="已用时（秒）")
    parser.add_argument("--tools", type=int, default=0, help="工具调用数")
    parser.add_argument("--tokens", default="", help="上下文用量（显示用字符串，如 22%% · 45k/200k）")
    parser.add_argument("--thread-id", default="", help="会话 id（可选）")
    parser.add_argument("--json", dest="json_data", default="", help="直接给一个 JSON 对象（或 - 表示从 stdin 读）")
    parser.add_argument("--clear", action="store_true", help="删掉状态桥文件（收工）")
    parser.add_argument("--quiet", action="store_true", help="不打印结果（挂 hook 时用）")
    args = parser.parse_args()

    agent = re.sub(r"[^a-z0-9_-]", "", str(args.agent or "").lower())
    if not agent:
        print("agent id 不合法（只允许字母 / 数字 / 下划线 / 连字符）", file=sys.stderr)
        return 2

    path = os.path.join(BRIDGE_DIR, "%s.json" % agent)
    if args.clear:
        try:
            os.remove(path)
            message = "已清除：%s" % path
        except OSError:
            message = "本来就没有：%s" % path
        if not args.quiet:
            print(message)
        return 0

    data: dict = {}
    if args.json_data:
        data.update(_parse_json(args.json_data))
    if args.state:
        data["state"] = args.state
    if args.label:
        data["label"] = args.label
    for key, value in (
        ("title", args.title), ("activity", args.activity), ("say", args.say),
        ("tokens", args.tokens), ("threadId", args.thread_id),
    ):
        if value:
            data[key] = value
    if args.elapsed:
        data["elapsed"] = args.elapsed
    if args.tools:
        data["tools"] = args.tools
    if data.get("state") and data["state"] not in STATES:
        print("state 不合法：%s（可选：%s）" % (data["state"], " / ".join(STATES)), file=sys.stderr)
        return 2
    if not data:
        print("什么都没写：至少给一个字段（比如 --state working，或 --json {...}）", file=sys.stderr)
        return 2

    os.makedirs(BRIDGE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)          # 原子替换
    if not args.quiet:
        print("已写入：%s -> %s" % (path, json.dumps(data, ensure_ascii=False)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
