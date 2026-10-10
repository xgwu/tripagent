#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""宿主实况检查：**宿主现在实际加载的是哪一版 / 哪个命令**。

为什么单独立一个工具
--------------------
`selftest.py` 验的是「server 进程本身对不对」——它自己把 server 拉起来，
**完全绕过宿主**。而历史踩过的坑恰好都在宿主这一侧：

  · 宿主只在 **bootstrap 读一次** `~/.workbuddy/mcp.json` ⇒ 改了文件没重载，
    表现是「代码明明改了，面板/工具却还是旧的」，**不报任何错**；
  · 宿主按 uri 缓存**注入后的成品** ⇒ 光改注入值不 +1 版本号也看不到变化；
  · 「源码模式 ↔ exe 模式」切换后，若没重载，跑的仍是**上一个进程形态**。

这三件事都无法从 server 侧观测。本工具转而去读宿主的**两份落地文件**并交叉比对：

  `~/.workbuddy/mcp.json`          宿主真正读取的注册表（command 决定 dev/exe）
  `~/.workbuddy/mcp-tool-list.json` 宿主**已加载**的工具表（描述文案会随版本变）

再把工具表里的 `plan_trip` 描述与**当前源码**里的期望值比对 —— 不一致即
「宿主拿着旧版本」，此时无论怎么调工具都不会是新逻辑。

用法：
    <venv python> mcp/check_live.py          # 检查并给结论
    <venv python> mcp/check_live.py -v       # 额外打印全文描述
退出码：0 = 宿主已加载当前源码版本；1 = 陈旧 / 未注册 / 配置缺失。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER_PY = os.path.join(HERE, "tripplanner_mcp_server.py")
USER_MCP_JSON = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")
TOOL_LIST = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp-tool-list.json")
SERVER_KEY = "tripplanner"
TOOL_PREFIX = "tripplanner_"
EXPECT_TOOLS = 9


def ts(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def expected_plan_trip_desc() -> str:
    """从源码 AST 取 `TOOLS[0]["description"]`（不 import，避免副作用）。

    plan_trip 的描述是**相邻字符串字面量拼接** ⇒ 解析器已折成一个 Constant。
    """
    src = open(SERVER_PY, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "TOOLS" for t in node.targets):
            first = node.value.elts[0]                  # plan_trip 排在第一个
            for k, v in zip(first.keys, first.values):
                if isinstance(k, ast.Constant) and k.value == "description":
                    return v.value if isinstance(v, ast.Constant) else ""
    return ""


def wb_process_uptime() -> tuple[str, float] | tuple[None, None]:
    """WorkBuddy 主进程的启动时刻（判「有没有重载」的关键证据）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process WorkBuddy -ErrorAction SilentlyContinue | "
             "Sort-Object StartTime | Select-Object -First 1 -ExpandProperty StartTime).ToString('o')"],
            capture_output=True, text=True, timeout=25)
        s = (out.stdout or "").strip()
        if s:
            return s, time.time()
    except Exception:
        pass
    return None, None


def main() -> int:
    verbose = "-v" in sys.argv
    problem = False

    print("=" * 68)
    print("宿主实况检查（宿主侧，selftest 覆盖不到的那一半）")
    print("=" * 68)

    # ---- 1) 注册表：宿主读的那份 ----
    print(f"\n[1] 注册表 {USER_MCP_JSON}")
    if not os.path.exists(USER_MCP_JSON):
        print("    ✗ 文件不存在 —— 先跑 mcp/register.py")
        return 1
    reg = json.load(open(USER_MCP_JSON, encoding="utf-8")).get("mcpServers", {})
    ent = reg.get(SERVER_KEY)
    if not ent:
        print(f"    ✗ 没注册 {SERVER_KEY}")
        return 1
    cmd = str(ent.get("command") or "")
    is_exe = cmd.lower().endswith(".exe")
    print(f"    形态        : {'打包 exe' if is_exe else '源码'}")
    print(f"    command     : {cmd}")
    print(f"    args        : {ent.get('args')}")
    print(f"    TRIPPLANNER_HOME: {(ent.get('env') or {}).get('TRIPPLANNER_HOME')}")
    print(f"    command 存在: {'是' if os.path.exists(cmd) else '✗ 否 —— 宿主会启动失败'}")
    if not os.path.exists(cmd):
        problem = True
    mtime_reg = os.path.getmtime(USER_MCP_JSON)
    print(f"    注册表 mtime: {ts(mtime_reg)}")

    # exe 形态额外要件：remote.json 必须与 exe 同目录
    if is_exe:
        exe_dir = os.path.dirname(cmd)
        rj = os.path.join(exe_dir, "remote.json")
        ok = os.path.exists(rj)
        print(f"    remote.json : {'有' if ok else '✗ 缺失 —— exe 会退回默认 base'}")
        if not ok:
            problem = True
        # ⚠️ 别拿 exe 跟注册表比：注册总在打包之后，那样必然误报。
        #    真正要判的是「**源码改过之后有没有重新打包**」⇒ 跟源码比。
        exe_mtime = os.path.getmtime(cmd)
        newer = [f for f in (SERVER_PY, os.path.join(HERE, "panel", "plan_panel.html"))
                 if os.path.exists(f) and os.path.getmtime(f) > exe_mtime + 1]
        print(f"    exe mtime   : {ts(exe_mtime)}")
        if newer:
            print("    ⚠️ 源码比 exe 新 ⇒ 改完没重新打包，宿主跑的是旧 exe：")
            for f in newer:
                print(f"         {os.path.relpath(f, REPO)}  ({ts(os.path.getmtime(f))})")
            print(f"       修：先跑 mcp/build_exe.py，再重载（方案 A/B）")
            problem = True
        else:
            print("    ✅ exe 不落后于源码（无需重新打包）")

    # ---- 2) 宿主已加载的工具表 ----
    print(f"\n[2] 宿主已加载的工具表 {TOOL_LIST}")
    exp_desc = expected_plan_trip_desc()
    if not os.path.exists(TOOL_LIST):
        print("    ✗ 文件不存在 —— 宿主还没加载过 MCP，先重启 WorkBuddy")
        problem = True
        loaded = None
    else:
        data = json.load(open(TOOL_LIST, encoding="utf-8"))
        loaded = None
        for k, v in (data.get("entries") or {}).items():
            if any(str(t.get("name", "")).startswith(TOOL_PREFIX) for t in v):
                loaded = (k, v)
                break
        if loaded is None:
            print("    ✗ 工具表里没有 tripplanner_* —— 宿主没认出这个 server")
            problem = True
        else:
            k, tools = loaded
            names = sorted(t.get("name") for t in tools)
            print(f"    server 指纹键: {k}")
            print(f"    工具数       : {len(tools)}"
                  f"{'' if len(tools) == EXPECT_TOOLS else f'  ⚠️ 期望 {EXPECT_TOOLS}'}")
            print(f"    工具         : {', '.join(n.replace(TOOL_PREFIX, '') for n in names)}")
            print(f"    文件 mtime   : {ts(os.path.getmtime(TOOL_LIST))}")

            got = next((t.get("description", "") for t in tools
                        if t.get("name") == TOOL_PREFIX + "plan_trip"), "")
            line_got = got.splitlines()[3] if len(got.splitlines()) > 3 else got
            line_exp = exp_desc.splitlines()[3] if len(exp_desc.splitlines()) > 3 else exp_desc
            print(f"\n    plan_trip 描述（宿主）: {line_got}")
            print(f"    plan_trip 描述（源码）: {line_exp}")
            if got.strip() == exp_desc.strip():
                print("    ✅ 一致 —— 宿主加载的就是当前源码版本")
            else:
                print("    ❌ 不一致 —— 宿主是**旧版本**：改了但没重载（或没重打包）")
                problem = True
            if verbose:
                print("\n    --- 宿主全文 ---\n" + got)
                print("\n    --- 源码全文 ---\n" + exp_desc)

    # ---- 3) 有没有重载 ----
    print("\n[3] 重载判定（宿主只在启动时读一次注册表）")
    if is_exe and os.path.exists(cmd):
        dbg = os.path.normpath(os.path.join(os.path.dirname(cmd), "output", "mcp_debug.log"))
        if os.path.exists(dbg):
            print(f"    exe 调试日志: {dbg}")
            print(f"    mtime       : {ts(os.path.getmtime(dbg))}")
            if os.path.getmtime(dbg) >= mtime_reg - 1:
                print("    ✅ exe 在注册之后**确实被宿主拉起过**（日志晚于注册表改动）")
            else:
                print("    ⏳ 日志早于注册表改动 —— 还没重载过")
        else:
            print(f"    （还没有 {dbg} —— 重启后调一次工具就会出现，它是最硬的证据）")
    up, _ = wb_process_uptime()
    if up:
        print(f"    WorkBuddy 启动: {up}")
    print(f"    判据：WorkBuddy 启动时刻 **晚于** 注册表 mtime（{ts(mtime_reg)}）才算重载过。")
    print("          方案 A 表单重载（立即生效）｜ 方案 B 完全退出再启动")

    print("\n" + "=" * 68)
    print("❌ 宿主侧有问题（见上）" if problem else "🟢 宿主侧 OK：已加载当前源码版本")
    print("=" * 68)
    return 1 if problem else 0


if __name__ == "__main__":
    sys.exit(main())
