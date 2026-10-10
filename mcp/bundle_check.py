#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP server「消费方路径」冒烟内核 —— 被 `smoke_exe.py` 与 `build_bundle.py` **共用**。

为什么单独成文件：同一段断言若被复制到两处，早晚漂移。本项目已经因为
「`mode` 口径三处各写一遍」在真机上出过错（面板与导出都误标「离线兜底」）⇒
凡是「怎么算跑通」只准在**这里**定义一次。

覆盖的消费方路径（与 `app.asar` 取证出的宿主行为一致）：
    initialize → notifications/initialized → tools/list
    → resources/read（面板 HTML + 注入值）→ tools/call plan_trip（真实云端）
    → tools/call panel_ready（面板握手通道）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

EXPECT_TOOLS = 9
PANEL_URI_PREFIX = "ui://tripplanner/"
DEFAULT_QUERY = "杭州2天，喜欢自然风光"

# 注入后**不该**再出现的占位符（面板 HTML 里若还有 = 注入没跑完）
PLACEHOLDERS = ("__REMOTE_API_BASE__", "__REMOTE_API_AUTH__",
                "__SESSION_ID__", "__PLAN_JSON__", "__AMAP_JS_KEY__",
                "__PANEL_VERSION__")

# 线上/本地 secrets.json 都取不到 key 时的降级文案（面板仍可用，只是没地图）
_PROBE_STDERR = ("[tripplanner-mcp] resources/read",)
_MISSING_AMAP_PHRASES = ("也拿不到", "取不到")


class Result:
    """一次冒烟的全部产物 + 逐项结论。"""

    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []
        self.stderr = ""
        self.uri = ""
        self.html = ""
        self.tools: list = []
        self.plan: dict = {}
        self.structured: dict = {}
        self.elapsed = 0.0

    def add(self, label: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((label, bool(ok), str(detail)))
        return bool(ok)

    @property
    def failed(self) -> list[str]:
        return [c[0] for c in self.checks if not c[1]]


def _rpc(proc, obj: dict):
    proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    return json.loads(line) if line else None


def _notify(proc, obj: dict) -> None:
    proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def run(launch_cmd: list[str], cwd: str | None = None,
        env_extra: dict | None = None, session_id: str | None = None,
        query: str = DEFAULT_QUERY) -> Result:
    """把某个形态的 server 当真 MCP server 拉起，走一遍消费方路径。

    launch_cmd: 例 `[exe]`（打包形态）或 `[python, server.py]`（源码/启动器形态）
    cwd:        期望工作目录（打包形态 = 包目录）
    env_extra:  额外环境变量（源码/启动器形态必须给 `TRIPPLANNER_HOME=<包目录>`
                —— server 的 `project_root()` 靠它定位 remote.json / output/）
    """
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    env["TRIPPLANNER_SESSION_ID"] = session_id or ("bundleck%d" % os.getpid())

    proc = subprocess.Popen(launch_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            bufsize=1, env=env, cwd=cwd)
    r = Result()
    try:
        resp = _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18",
                                      "capabilities": {},
                                      "clientInfo": {"name": "bundle-check", "version": "0"}}})
        _notify(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        info = ((resp or {}).get("result") or {}).get("serverInfo") or {}
        r.add("server 能起来并完成 initialize 握手", bool(info.get("name")),
              json.dumps(info, ensure_ascii=False))
        r.add("报的还是 tripplanner 身份",
              info.get("name") == "tripplanner" and bool(info.get("version")),
              f"{info.get('name')} {info.get('version')}")

        resp = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        r.tools = ((resp or {}).get("result") or {}).get("tools") or []
        by = {t.get("name"): t for t in r.tools}
        r.add(f"暴露全部 {EXPECT_TOOLS} 个工具", len(r.tools) == EXPECT_TOOLS,
              str([t.get("name") for t in r.tools]))
        pt = by.get("tripplanner_plan_trip") or {}
        r.uri = ((pt.get("_meta") or {}).get("ui") or {}).get("resourceUri") or ""
        r.add("plan_trip 带面板 uri（_meta.ui.resourceUri）",
              r.uri.startswith(PANEL_URI_PREFIX), r.uri)

        resp = _rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "resources/read",
                           "params": {"uri": r.uri}})
        r.html = ((((resp or {}).get("result") or {}).get("contents") or [{}])[0]
                  .get("text") or "")
        r.add("resources/read 拿到面板 HTML", r.html.startswith("<!DOCTYPE html"),
              f"{len(r.html)} chars")
        # 🔴 最关键的一条：面板文件必须**在包内就位**（源码模式 = <dir>/mcp/panel/，
        #    打包模式 = PyInstaller `_MEIPASS`）。找不到时 read_panel() 会返回降级页，
        #    宿主照样渲染 —— 用户只看到一张错误页，不会有任何报错。
        r.add("面板文件已就位并被读到（不是『读取失败』降级页）",
              "面板文件读取失败" not in r.html and "tripplanner_panel_ready" in r.html)
        r.add("面板里没有占位符残留（注入已完成）",
              not any(p in r.html for p in PLACEHOLDERS),
              str(re.findall(r"__[A-Z_]+__", r.html)[:4]))
        # 🔴 包里的面板必须是**当前版本**：win 形态下面板是 PyInstaller 打进 exe 的
        #    （`--add-data`）⇒ 改了 panel 却忘了重打包，宿主拿到的仍是旧面板 HTML，
        #    而且 **uri 也还是旧的**、日志里看不出任何异常。这里用「面板自报版本
        #    （由 server 注入）== resourceUri 里的版本」交叉校验，两边漏一个就红。
        _ver = r.uri.rsplit("-", 1)[-1] if r.uri else ""
        r.add(f"包内面板是当前版本（自报 v={_ver} == uri 里的版本）",
              bool(_ver) and ("v=" + _ver + " base=") in r.html,
              f"uri={r.uri}")
        # M6：面板必须带高度上报（否则宿主给的是定高 iframe ⇒ 面板内又出现滚动条）
        r.add("包内面板带高度上报（M6，宿主据此撑高 iframe）",
              "ui/notifications/size-changed" in r.html
              and "panel-size:block:start" in r.html)

        t0 = time.time()
        resp = _rpc(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                           "params": {"name": "tripplanner_plan_trip",
                                      "arguments": {"query": query}}})
        sc = ((resp or {}).get("result") or {}).get("structuredContent") or {}
        r.structured = sc
        r.plan = sc.get("plan") or {}
        r.elapsed = time.time() - t0
        days = r.plan.get("dayList") or []
        r.add("★能完成一次真实云端规划", sc.get("ok") is True and bool(days),
              json.dumps({k: sc.get(k) for k in ("ok", "error")}, ensure_ascii=False)[:150])
        r.add("行程已由云端归一化（含 photo / 坐标）",
              any((s.get("photo") or "") and s.get("lat") is not None
                  for d in days for s in (d.get("timeline") or [])
                  if s.get("type") == "poi"),
              f"city={r.plan.get('city')} 耗时={r.elapsed:.1f}s")
        r.add("报的 city / days 合理",
              bool(r.plan.get("city")) and bool(r.plan.get("days")),
              f"{r.plan.get('city')} / {r.plan.get('days')}天")

        resp = _rpc(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                           "params": {"name": "tripplanner_panel_ready",
                                      "arguments": {"app_instance_id": "bundleck-1"}}})
        sc = ((resp or {}).get("result") or {}).get("structuredContent") or {}
        r.add("panel_ready 正常（面板握手通道可用）",
              sc.get("registered") is True and sc.get("hasPlan") is True,
              json.dumps(sc, ensure_ascii=False)[:120])
    finally:
        try:
            proc.stdin.close()
        except Exception:                                        # noqa: BLE001
            pass
        try:
            r.stderr = proc.stderr.read()
        except Exception:                                        # noqa: BLE001
            r.stderr = ""
        try:
            proc.wait(timeout=30)
        except Exception:                                        # noqa: BLE001
            proc.kill()
    return r


def print_result(r: Result, *, head: str = "") -> None:
    if head:
        print(head)
    for label, ok, detail in r.checks:
        print(("  [PASS] " if ok else "  [FAIL] ") + label + (f"  -> {detail}" if detail else ""))
    if r.stderr.strip():
        print("\n--- server stderr 末 6 行 ---")
        for ln in r.stderr.strip().splitlines()[-6:]:
            print("   " + ln)
