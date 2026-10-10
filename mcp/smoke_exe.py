#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打包产物冒烟：把 `dist/tripplanner-mcp.exe` 当**真 MCP server** 拉起验一遍。

为什么必须有这一步（源码跑通 ≠ exe 跑通）：打包会悄悄改变两件事 ——
  ① 面板 HTML 的**位置**（`--add-data` + `sys._MEIPASS`）⇒ 找不到时 `read_panel()`
     会返回「面板文件读取失败」的**降级页**，宿主照样能渲染，只是用户看到一张错误页；
  ② 「项目根」的**推导**（`sys.frozen`）⇒ 错了就变成「连不上后端 / 状态写错地方」。
两种都不会抛异常，只会表现为「面板空白 / 后端不可达」，极难反查。故这里直接验
**消费方路径**：initialize → tools/list → resources/read（面板 HTML + 注入值）
→ tools/call plan_trip（真实云端规划）→ 状态落盘位置。

用法：
    <venv python> mcp/smoke_exe.py                 # 默认 dist/tripplanner-mcp.exe
    <venv python> mcp/smoke_exe.py <exe路径>
没找到 exe 时**跳过（exit 0）**并提示先跑 `mcp/build_exe.py`。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_EXE = os.path.join(REPO, "dist", "tripplanner-mcp.exe")

OK = "  [PASS]"
NG = "  [FAIL]"
fails: list = []


def check(label, cond, detail=""):
    print(f"{OK if cond else NG} {label}" + (f"  -> {detail}" if detail else ""))
    if not cond:
        fails.append(label)


def rpc(proc, obj):
    proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    return json.loads(line) if line else None


def notify(proc, obj):
    proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def main() -> int:
    exe = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXE)
    print(f"exe: {exe}")
    if not os.path.exists(exe):
        print(f"（跳过）没找到 {exe} —— 先执行：")
        print(f'    "{sys.executable}" mcp/build_exe.py')
        return 0
    dist = os.path.dirname(exe)
    remote_json = os.path.join(dist, "remote.json")
    print(f"dist: {dist}")
    check("dist/remote.json 与 exe 同目录（分享包的必要条件）",
          os.path.exists(remote_json), remote_json)
    print(f"exe 大小: {os.path.getsize(exe) / 1048576:.1f} MB")
    print()

    # 记录 exe **同目录**的 output（验状态落在包目录，不写回仓库）
    out_dir = os.path.join(dist, "output")
    stale = os.path.join(out_dir, "mcp_last_plan.json")
    if os.path.exists(stale):
        os.remove(stale)
    repo_state = os.path.join(REPO, "output", "mcp_last_plan.json")
    repo_before = os.path.getmtime(repo_state) if os.path.exists(repo_state) else None

    env = dict(os.environ)
    env["TRIPPLANNER_HOME"] = dist          # 与 mcp.json 里的 env 一致
    env["TRIPPLANNER_SESSION_ID"] = "exesmoke%d" % os.getpid()
    proc = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            bufsize=1, env=env, cwd=dist)
    try:
        r = rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "smoke", "version": "0"}}})
        notify(proc, {"jsonrpc": "2.0", "method": "notifications/initialized",
                      "params": {}})
        info = ((r or {}).get("result") or {}).get("serverInfo") or {}
        check("exe 能起来并完成 initialize 握手",
              bool(info.get("name")), json.dumps(info, ensure_ascii=False))
        check("exe 报的还是 tripplanner 身份",
              info.get("name") == "tripplanner" and bool(info.get("version")),
              f"{info.get('name')} {info.get('version')}")

        r = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = ((r or {}).get("result") or {}).get("tools") or []
        names = [t.get("name") for t in tools]
        by = {t["name"]: t for t in tools}
        check("exe 暴露全部 9 个工具", len(tools) == 9, str(names))
        panel_uri = ((((by.get("tripplanner_plan_trip") or {}).get("_meta") or {})
                      .get("ui") or {}).get("resourceUri")) or ""
        check("plan_trip 带面板 uri（_meta.ui.resourceUri）",
              panel_uri.startswith("ui://tripplanner/"), panel_uri)

        r = rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "resources/read",
                       "params": {"uri": panel_uri}})
        html = ((((r or {}).get("result") or {}).get("contents") or [{}])[0]
                .get("text") or "")
        check("resources/read 拿到面板 HTML", html.startswith("<!DOCTYPE html"),
              f"{len(html)} chars")
        # 🔴 最关键的一条：打包后**必须**能从 `_MEIPASS` 找到面板文件。
        #    找不到时 read_panel() 会返回降级页，宿主照样渲染 —— 用户只看到一张错误页。
        check("面板文件在 exe 内被打包并读到（不是『读取失败』降级页）",
              "面板文件读取失败" not in html and "tripplanner_panel_ready" in html)
        check("面板里没有占位符残留（注入已完成）",
              not any(p in html for p in ("__REMOTE_API_BASE__", "__REMOTE_API_AUTH__",
                                          "__SESSION_ID__", "__PLAN_JSON__",
                                          "__AMAP_JS_KEY__")),
              str(re.findall(r"__[A-Z_]+__", html)[:4]))
        m_base = re.search(r'var\s+REMOTE_BASE\s*=\s*"(https?://[^"]+)"', html)
        check("exe 从同目录 remote.json 读到并注入了线上 base",
              bool(m_base), m_base.group(1) if m_base else "仍是占位符")
        check("exe 注入了 Basic 凭据",
              re.search(r'var\s+REMOTE_AUTH\s*=\s*"Basic [A-Za-z0-9+/=]+"', html) is not None)
        m_amap = re.search(r'var\s+amapKey\s*=\s*"([A-Za-z0-9]{16,})"', html)
        check("exe 注入了地图 key（本地没有 secrets.json ⇒ 走云端下发）",
              bool(m_amap),
              f"len={len(m_amap.group(1))}" if m_amap
              else "取不到 key 时面板只是没有地图，其余功能不受影响")

        t0 = time.time()
        r = rpc(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "tripplanner_plan_trip",
                                  "arguments": {"query": "杭州2天，喜欢自然风光"}}})
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        plan = sc.get("plan") or {}
        days = plan.get("dayList") or []
        check("★exe 能完成一次**真实云端规划**", sc.get("ok") is True and bool(days),
              json.dumps({k: sc.get(k) for k in ("ok", "error")},
                         ensure_ascii=False)[:160])
        check("行程已由云端归一化（含 photo / 坐标）",
              any((s.get("photo") or "") and s.get("lat") is not None
                  for d in days for s in (d.get("timeline") or [])
                  if s.get("type") == "poi"),
              f"city={plan.get('city')} 耗时={time.time() - t0:.1f}s")
        check("exe 报的 city / days 合理",
              bool(plan.get("city")) and bool(plan.get("days")),
              f"{plan.get('city')} / {plan.get('days')}天")

        r = rpc(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "tripplanner_panel_ready",
                                  "arguments": {"app_instance_id": "smoke-1"}}})
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("exe 的 panel_ready 正常（面板握手通道可用）",
              sc.get("registered") is True and sc.get("hasPlan") is True,
              json.dumps(sc, ensure_ascii=False)[:120])
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            err = proc.stderr.read()
        except Exception:
            err = ""
        try:
            proc.wait(timeout=30)
        except Exception:
            proc.kill()

    print()
    check("状态落盘在**包目录** output/（分享包不写回仓库）",
          os.path.exists(stale), out_dir)
    later = os.path.getmtime(repo_state) if os.path.exists(repo_state) else None
    check("仓库 output/ 的行程**没有被 exe 改写**",
          later == repo_before, f"{repo_before} -> {later}")

    if err.strip():
        print("\n--- exe stderr 末 6 行 ---")
        for ln in err.strip().splitlines()[-6:]:
            print("   " + ln)

    print()
    if fails:
        print(f"[FAIL] {len(fails)} 项未通过：")
        for f in fails:
            print("   - " + f)
        return 1
    print("[PASS] 打包产物冒烟全绿 —— exe 可独立运行（零依赖、无 src/ data/）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
