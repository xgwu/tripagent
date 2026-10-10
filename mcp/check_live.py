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

第四件事（2026-10-10 实测挖出，最阴）：**「信任」是按配置指纹记的**。
`~/.workbuddy/mcp-approvals.json` 的键形如 `<sha256>::<server 名>` —— 只要
command/args/env 变了，指纹就变，**旧信任对新配置无效**，宿主会**静默丢弃整个
server**：工具表里凭空少一项，日志里一句报错都没有，看起来像「没注册」。

这四件事都无法从 server 侧观测。本工具转而去读宿主的**三份落地文件**并交叉比对：

  `~/.workbuddy/mcp.json`            宿主真正读取的注册表（command 决定 dev/exe）
  `~/.workbuddy/mcp-approvals.json`  信任记录（指纹 + 时间戳，判「改了配置有没有重新信任」）
  `~/.workbuddy/mcp-tool-list.json`  宿主**已加载**的工具表（描述文案会随版本变）

再把工具表里的 `plan_trip` 描述与**当前源码**里的期望值比对 —— 不一致即
「宿主拿着旧版本」，此时无论怎么调工具都不会是新逻辑。

⚠️ 指纹的确切输入字段未能逆出（10 万+ 组合试过：单纯对配置 JSON 做 sha256
都不对，显然掺了 uid / 项目路径等不可复现的输入）⇒ **别试图自己写 approvals**，
改配置后一律走 UI 重新信任。

用法：
    <venv python> mcp/check_live.py          # 检查并给结论
    <venv python> mcp/check_live.py -v       # 额外打印全文描述
退出码：0 = 宿主已加载当前源码版本；1 = 陈旧 / 未注册 / 配置缺失。
"""
from __future__ import annotations

import ast
import datetime
import json
import os
import re
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER_PY = os.path.join(HERE, "tripplanner_mcp_server.py")
USER_MCP_JSON = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")
APPROVALS = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp-approvals.json")
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


LOG_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "logs")


def host_stdio_connected_ts() -> float | None:
    """从**宿主自己的日志**里找 tripplanner 的 stdio-connected 时刻（最硬的外部证据）。

    宿主日志格式：daemon.log 每行一条 JSON，timestamp 是 UTC ISO（`…Z`）；
    命中行形如（⚠️ message 是**转义过的字符串**，正文里是 `\\"pid\\":` 而非 `"pid":`，
    所以只能用不含引号的子串判定，别拿未转义的 JSON 片段去匹配）：
        [MCP-AuthDiag] stdio-connected {"pid":28100,"id":"custom-mcp:tripplanner",…}
    返回本地时间戳（epoch 秒），没有记录则 None。
    """
    newest = None
    for name in ("daemon.log", "daemon.old.log"):
        path = os.path.join(LOG_DIR, name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "stdio-connected" not in line or "custom-mcp:" + SERVER_KEY not in line:
                        continue
                    m = re.search(r'"timestamp":"([0-9T:.\-]+Z)"', line)
                    if not m:
                        continue
                    try:
                        t = datetime.datetime.strptime(
                            m.group(1), "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                            tzinfo=datetime.timezone.utc).timestamp()
                    except ValueError:
                        continue
                    newest = t if newest is None else max(newest, t)
        except OSError:
            continue
    return newest


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


def expected_panel_uri() -> str | None:
    """从源码 AST 取 `PANEL_VERSION`（不 import，避免副作用）。"""
    try:
        tree = ast.parse(open(SERVER_PY, encoding="utf-8").read())
    except Exception:
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "PANEL_VERSION" for t in node.targets):
            continue
        if isinstance(node.value, ast.Constant):
            return f"ui://tripplanner/plan-{node.value.value}"
    return None


def server_in_tool_list() -> bool:
    """宿主已加载的工具表里有没有 `tripplanner_*`（[2] 假阳性的兜底判据）。"""
    try:
        data = json.load(open(TOOL_LIST, encoding="utf-8"))
    except Exception:
        return False
    for v in (data.get("entries") or {}).values():
        if any(str(t.get("name", "")).startswith(TOOL_PREFIX) for t in v):
            return True
    return False


def host_panel_uri() -> tuple[str | None, float | None]:
    """宿主**实际请求过的**面板 uri（最近一次）+ 该记录的本地时间戳。

    证据来源：`~/.workbuddy/logs/mcp-apps-diag.log`，形如
        openHostApp.created … resourceUri=ui://tripplanner/plan-m<N>
    （示例里刻意不写死版本号 —— 写死就会在下一次 `PANEL_VERSION` +1 时过期。）

    ⚠️ 2026-10-10 纠正：**不要**用 server 侧 `output/mcp_debug.log` 里的
    `resources/read` 记录当判据 —— `log()` 只写 **stderr**（宿主环境吞掉），
    **从不落盘**，那个「没有记录＝命中缓存」的推断是错的（我曾据此得出错误结论）。
    真正的外部硬证据只有宿主这份日志。
    """
    path = os.path.join(LOG_DIR, "mcp-apps-diag.log")
    if not os.path.exists(path):
        return None, None
    last, last_ts = None, None
    pat = re.compile(
        r"\[(\d{4}-\d{2}-\d{2}T[\d:.]+)Z\].*?"
        r"resourceUri=(ui://tripplanner/plan-[A-Za-z0-9]+)")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = pat.search(line)
                if not m:
                    continue
                last = m.group(2)
                try:
                    dt = datetime.datetime.strptime(m.group(1)[:19], "%Y-%m-%dT%H:%M:%S")
                    last_ts = dt.replace(tzinfo=datetime.timezone.utc).timestamp()
                except Exception:
                    last_ts = None
    except Exception:
        return None, None
    return last, last_ts


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

    # ---- 2) 信任状态（决定宿主会不会加载它）----
    # 为什么单列一节：这是 2026-10-10 实测挖出的**静默丢弃**陷阱 ——
    # 把 command 从 python 改成 exe 后，宿主启动时 tripplanner 直接消失，
    # 工具表里没有、日志里没报错，看起来像「没注册」。
    # 真因：approvals 的键 = <配置指纹>::<server 名>，配置一变指纹就变，
    # 旧信任对新配置无效 ⇒ 未信任的 server 被静默丢弃。
    print(f"\n[2] 信任状态 {APPROVALS}")
    approved_ts = None
    if not os.path.exists(APPROVALS):
        print("    ✗ 文件不存在 —— 该 server 从未被信任过，宿主会**静默丢弃**它")
        problem = True
    else:
        try:
            appr = json.load(open(APPROVALS, encoding="utf-8"))
        except Exception as e:
            appr = {}
            print(f"    ✗ 读不出来：{e}")
        hits = {k: v for k, v in appr.items() if k.endswith("::" + SERVER_KEY)}
        if not hits:
            print(f"    ✗ 没有 ::{SERVER_KEY} 的信任记录 ⇒ 宿主会**静默丢弃**它")
            problem = True
        else:
            for k, v in hits.items():
                approved_ts = max(approved_ts or 0, float(v) / 1000.0)
                print(f"    指纹: {k.split('::')[0][:16]}…  "
                      f"信任于 {ts(float(v) / 1000.0)}")
            if approved_ts < mtime_reg - 1:
                if server_in_tool_list():
                    # 2026-10-10 实测：UI 重新保存会**无实质变更地重写** mcp.json
                    # （mtime 前移、指纹不变）⇒ 这条「时间戳」判据会误报。
                    # 宿主**确实已加载**它，就说明信任没问题，以 [3]/[4] 为准。
                    print("    ℹ️ 信任时间早于注册表 mtime，但宿主**确实已加载**该 server")
                    print("       （UI 重新保存会无实质变更地重写 mcp.json ⇒ mtime 前移、")
                    print("        指纹不变 —— 本判据的**已知假阳性**，以 [3]/[4] 为准）")
                else:
                    print("    ❌ 信任时间 **早于**注册表 mtime，且工具表里没有它 ⇒ 静默丢弃")
                    print("       表现：工具表里凭空少一项、日志一句报错都没有")
                    print("       修：连接器 → 自定义 MCP → 找到 tripplanner → 保存/信任"
                          "（表单通道立即生效）")
                    problem = True
            else:
                print(f"    ✅ 信任晚于配置改动（{ts(approved_ts)} ≥ {ts(mtime_reg)}）")

    # ---- 3) 宿主已加载的工具表 ----
    print(f"\n[3] 宿主已加载的工具表 {TOOL_LIST}")
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

    # ---- 4) 有没有重载 ----
    print("\n[4] 重载判定（宿主只在启动时读一次注册表）")
    # ⚠️ 别拿 exe 侧的 output/mcp_debug.log 当「宿主拉起过」的证据 ——
    #    `smoke_exe.py` / 手工探针同样会往那里写（我就被这条假绿骗过一次）。
    #    真正的外部证据只能来自**宿主自己的日志**：daemon.log 里的
    #    `[MCP-AuthDiag] stdio-connected {"id":"custom-mcp:tripplanner"}`。
    host_ts = host_stdio_connected_ts()
    if host_ts is None:
        print("    ✗ 宿主日志里**从来没有** tripplanner 的 stdio-connected 记录")
        print("       ⇒ 宿主从未成功加载过它（连尝试都没有 ⇒ 多为未信任被静默丢弃）")
        problem = True
    else:
        print(f"    宿主 stdio-connected: {ts(host_ts)}")
        if host_ts >= mtime_reg - 1:
            print("    ✅ 宿主在这次配置之后**确实拉起过**它（宿主日志为证）")
        else:
            print("    ⏳ 该记录早于本次配置改动 —— 还没有按新配置加载过")
            print("       修：重新信任（方案 A）或重载（方案 B）后再调一次工具")
            problem = True
    if is_exe and os.path.exists(cmd):
        dbg = os.path.normpath(os.path.join(os.path.dirname(cmd), "output", "mcp_debug.log"))
        print(f"    （参考，**不能当宿主证据**）{dbg}"
              + (f"  mtime {ts(os.path.getmtime(dbg))}" if os.path.exists(dbg) else " 不存在"))
    up, _ = wb_process_uptime()
    if up:
        print(f"    WorkBuddy 启动: {up}")
    print(f"    判据：WorkBuddy 启动时刻 **晚于** 注册表 mtime（{ts(mtime_reg)}）才算重载过。")
    print("          方案 A 表单重载（立即生效）｜ 方案 B 完全退出再启动")

    # ---- 5) 宿主拿到的是哪一版「面板」 ----
    # 改面板后**唯一有效**的判据：宿主日志里实际请求过的 resourceUri。
    # 为什么不用别的手段：server 侧 `resources/read` 只写 stderr、不落盘（见 host_panel_uri 注释）；
    # 且 `PANEL_VERSION` +1 只在宿主**重新描述工具**（重启）时才被采纳 —— UI 保存/重连不换 uri。
    print("\n[5] 面板版本（宿主**实际请求过**的 uri）")
    exp_uri = expected_panel_uri()
    got_uri, got_ts = host_panel_uri()
    print(f"    源码 PANEL_VERSION → {exp_uri or '?（源码里没找到）'}")
    if got_uri is None:
        print("    ⚠️ 宿主日志里没有记录 —— 可能从未打开过面板（先调一次 plan_trip）")
    else:
        print(f"    宿主最近请求       → {got_uri}" + (f"   （{ts(got_ts)}）" if got_ts else ""))
        if exp_uri and got_uri == exp_uri:
            print("    ✅ 一致 —— 面板已是最新版（宿主缓存已失效）")
        elif exp_uri:
            print("    ❌ 落后 —— 宿主用的还是**旧面板**（改了面板但宿主没重新描述）")
            print("       修：**完全退出 WorkBuddy 再启动**")
            print("           （UI 保存/重新连接**不换 uri** ⇒ 按 uri 缓存的旧面板不会失效）")
            problem = True

    print("\n" + "=" * 68)
    print("❌ 宿主侧有问题（见上）" if problem else "🟢 宿主侧 OK：已加载当前源码版本")
    print("=" * 68)
    return 1 if problem else 0


if __name__ == "__main__":
    sys.exit(main())
