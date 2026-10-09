#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把 TripPlanner MCP server 注册进 WorkBuddy 的用户级 MCP 配置。

用法：
    python mcp/register.py            # 写入/更新（双写两份配置）
    python mcp/register.py --check    # 只看现状，不写
    python mcp/register.py --remove   # 移除

行为：**合并**进 mcpServers，绝不动用户已有的其它 server（playwright 等）。
写前自动备份为 <file>.bak-tripplanner。

⚠️ 关键（2026-10-09 实测自 app.asar）：
  - 宿主真正读取的是 `join(configDir, "mcp.json")`，configDir 默认 `~/.workbuddy`。
    （源码：`get filePath(){ return join(this.deps.getConfigDir(), "mcp.json") }`）
  - **UserMcpProvider 只在 bootstrap（构造时）读一次磁盘**，之后只有 IPC
    `wb:mcp:refresh`（UI 里点刷新）才会重读。**写完文件必须重载应用**，
    否则面板里不会出现 —— 这是第一次「看不到 tripplanner」的根因。
  - 存储字段白名单：type / command / args / env / cwd / url / headers / timeout。
    多余的 description 会被忽略，这里索性不写。
  - stdio 必须加 `-u`：Python 在管道下默认块缓冲，会把 JSON-RPC 响应憋在缓冲区。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SERVER_KEY = "tripplanner"
HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(HERE, "tripplanner_mcp_server.py")
REPO = os.path.dirname(HERE)

# 用 tripagent venv 的解释器：M1 起要 import ortools 跑真求解，现在就把路径定对
PYTHON = r"D:/Users/xgwu/.workbuddy/binaries/python/envs/tripagent/Scripts/python.exe"

# 双写：用户级 mcp.json 是宿主实际读取的；.mcp.json 作兼容兜底
CONFIG_FILES = [
    os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json"),
    os.path.join(os.path.expanduser("~"), ".workbuddy", ".mcp.json"),
]

ENTRY = {
    "type": "stdio",
    "command": PYTHON,
    "args": ["-u", SERVER_PY.replace("\\", "/")],
    "cwd": HERE.replace("\\", "/"),
    "env": {"TRIPPLANNER_HOME": REPO.replace("\\", "/")},
}


def load(path: str) -> dict:
    if not os.path.exists(path):
        return {"mcpServers": {}}
    with open(path, encoding="utf-8") as f:
        txt = f.read().strip()
    if not txt:
        return {"mcpServers": {}}
    data = json.loads(txt)
    data.setdefault("mcpServers", {})
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只查看，不写入")
    ap.add_argument("--remove", action="store_true", help="移除 tripplanner 条目")
    args = ap.parse_args()

    print(f"server 脚本 : {SERVER_PY}  ({'存在' if os.path.exists(SERVER_PY) else '缺失!'})")
    print(f"解释器      : {PYTHON}  ({'存在' if os.path.exists(PYTHON) else '缺失!'})")
    print(f"仓库根      : {REPO}")
    print()

    for path in CONFIG_FILES:
        print(f"--- {path} ---")
        exists = os.path.exists(path)
        print(f"  文件{'存在' if exists else '不存在（将新建）'}")
        data = load(path)
        before = sorted(data["mcpServers"].keys())
        print(f"  现有 server: {before or '（无）'}")

        if args.check:
            cur = data["mcpServers"].get(SERVER_KEY)
            print(f"  tripplanner  : {'已注册 -> ' + json.dumps(cur, ensure_ascii=False) if cur else '未注册'}")
            print()
            continue

        if args.remove:
            if SERVER_KEY in data["mcpServers"]:
                data["mcpServers"].pop(SERVER_KEY)
                print("  已移除 tripplanner")
            else:
                print("  本来就没有 tripplanner")
        else:
            data["mcpServers"][SERVER_KEY] = ENTRY
            print(f"  已写入 tripplanner -> {PYTHON}")

        if exists:
            bak = path + ".bak-tripplanner"
            shutil.copy2(path, bak)
            print(f"  备份 -> {bak}")
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)

        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")

        after = sorted(json.load(open(path, encoding="utf-8"))["mcpServers"].keys())
        print(f"  写入后 server: {after}")
        lost = set(before) - set(after)
        print(f"  既有条目丢失: {sorted(lost) if lost else '无'}")
        print()

    if not args.check and not args.remove:
        print("⚠️ 写文件只是第一步 —— 宿主只在启动时读一次，必须重载才生效：")
        print("  方案 A（推荐）：在「连接器 → 自定义 MCP」里点「+ 添加」，按下面字段填表")
        print("    名称    tripplanner")
        print("    类型    stdio")
        print(f"    命令    {PYTHON}")
        print(f"    参数    -u  {SERVER_PY.replace(chr(92), '/')}")
        print(f"    环境    TRIPPLANNER_HOME = {REPO.replace(chr(92), '/')}")
        print("    （表单保存走 importServer -> 立即生效 + 自动授权，不用重启）")
        print("  方案 B：完全退出 WorkBuddy 再启动（重读 mcp.json）")
        print("  方案 C：面板里若有刷新/重载按钮，点它（等价 IPC wb:mcp:refresh）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
