#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把 TripPlanner MCP server 注册进 WorkBuddy 的用户级 MCP 配置。

用法：
    python mcp/register.py                 # 源码模式：python -u <server.py>
    python mcp/register.py --check         # 只看现状，不写
    python mcp/register.py --remove        # 移除
    python mcp/register.py --exe <exe路径>  # 打包模式：直接指向 tripplanner-mcp.exe

  两种模式对宿主来说都是 stdio server，差别只是 command/args：
    - 源码模式：command=python, args=[-u, .../tripplanner_mcp_server.py],
                env=TRIPPLANNER_HOME=<仓库根>（remote.json / output 都在这）
    - 打包模式：command=<exe>, args=[], env=TRIPPLANNER_HOME=<exe 所在目录>
                （要让 exe 自己按 sys.executable 推断也行，显式写上更不容易踩坑）

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
  - **源码模式**的 stdio 必须加 `-u`：Python 在管道下默认块缓冲，会把 JSON-RPC
    响应憋在缓冲区。（打包后的 exe 里 `send()` 每次都显式 flush，故无需该参数。）

⚠️ M5：server 已是**瘦客户端**（规划/换点/导出全走云端），**不再需要 ortools /
   data/ 目录** —— 所以源码模式的 env 只需要 TRIPPLANNER_HOME 用来定位 remote.json
   与 output/。
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

# 源码模式用的解释器：优先 tripagent venv（M5 起瘦客户端其实只需标准库，
# 这里只是保持与历次一致）；路径不存在就退回当前解释器，别硬崩。
PYTHON = r"D:/Users/xgwu/.workbuddy/binaries/python/envs/tripagent/Scripts/python.exe"
if not os.path.exists(PYTHON):
    PYTHON = sys.executable

# 双写：用户级 mcp.json 是宿主实际读取的；.mcp.json 作兼容兜底
CONFIG_FILES = [
    os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json"),
    os.path.join(os.path.expanduser("~"), ".workbuddy", ".mcp.json"),
]


def entry_dev() -> dict:
    """源码模式条目。"""
    return {
        "type": "stdio",
        "command": PYTHON,
        "args": ["-u", SERVER_PY.replace("\\", "/")],
        "cwd": HERE.replace("\\", "/"),
        "env": {"TRIPPLANNER_HOME": REPO.replace("\\", "/")},
    }


def entry_exe(exe: str) -> dict:
    """打包模式条目（分享包）。TRIPPLANNER_HOME 显式指到 exe 所在目录。"""
    exe_abs = os.path.abspath(exe)
    d = os.path.dirname(exe_abs)
    return {
        "type": "stdio",
        "command": exe_abs.replace("\\", "/"),
        "args": [],
        "cwd": d.replace("\\", "/"),
        "env": {"TRIPPLANNER_HOME": d.replace("\\", "/")},
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
    ap.add_argument("--exe", metavar="PATH",
                    help="注册打包好的 exe（分享包模式），而不是源码模式")
    args = ap.parse_args()

    if args.exe:
        if not os.path.exists(args.exe):
            print(f"✗ 找不到 exe：{args.exe}")
            return 1
        ENTRY = entry_exe(args.exe)
        print(f"模式        : 打包 exe")
        print(f"exe         : {ENTRY['command']}  (存在)")
        print(f"TRIPPLANNER_HOME: {ENTRY['env']['TRIPPLANNER_HOME']}")
        rj = os.path.join(os.path.dirname(os.path.abspath(args.exe)), "remote.json")
        print(f"remote.json : {rj}  ({'存在' if os.path.exists(rj) else '缺失 —— exe 会退回默认 base'})")
    else:
        ENTRY = entry_dev()
        print(f"模式        : 源码")
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
            print(f"  已写入 tripplanner -> {ENTRY['command']}")

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
        print(f"    命令    {ENTRY['command']}")
        print(f"    参数    {' '.join(ENTRY['args']) or '（留空）'}")
        print(f"    环境    TRIPPLANNER_HOME = {ENTRY['env']['TRIPPLANNER_HOME']}")
        print("    （表单保存走 importServer -> 立即生效 + 自动授权，不用重启）")
        print("  方案 B：完全退出 WorkBuddy 再启动（重读 mcp.json）")
        print("  方案 C：面板里若有刷新/重载按钮，点它（等价 IPC wb:mcp:refresh）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
