#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP App 面板前端守卫：U+FFFD / 标签配对 / 内联 JS 语法 / key 安全。

用法：
    D:\\Users\\xgwu\\.workbuddy\\binaries\\python\\envs\\tripagent\\Scripts\\python.exe mcp/panel_guard.py

为什么不靠人工 review：本项目**三次**踩过「校验脚本自己的正则比被测对象更危险」
（`\\s` 在 raw f-string / `node -e` 双引号里退化成 `s`；本次写守卫时又两次把
提示文案里的字段名误判成 key）。所以这里刻意：
  - 结构性判断**只用 `count()` 计数比对**，不用正则数标签；
  - key 安全用**精确锚点**（`var amapKey = "..."` 的初值），不用「全文不许有 hex」这种宽正则
    —— 面板里 M1 就有的 CSP 验证探针本身带一个高德图片 hash，宽正则必然误报。
  - M2.1 起同一套办法也套在本地 API 的 `API_BASE` / `API_TOKEN` 上：初值必须是占位符。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, "panel", "plan_panel.html")
NODE = shutil.which("node") or r"D:\Users\xgwu\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"

fails = []


def chk(label, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + label + (f"  -> {detail}" if detail else ""))
    if not cond:
        fails.append(label)


def main() -> int:
    print(f"panel: {PANEL}")
    html = open(PANEL, encoding="utf-8").read()

    # 1) U+FFFD（GBK 污染的直接证据；Read 工具看不出来，只有码点能暴露）
    n_bad = html.count("\ufffd")
    chk("无 U+FFFD 替换字符", n_bad == 0, f"count={n_bad}")

    # 2) 标签配对（计数法）
    for tag in ("div", "script", "style", "details", "ul", "li", "span", "button"):
        op = len(re.findall(r"<" + tag + r"[\s>]", html))
        cl = html.count("</" + tag + ">")
        chk(f"<{tag}> 配对", op == cl, f"{op} open / {cl} close")

    # 3) 关键锚点（面板与 server 的工具名 / 占位符必须对得上）
    for anchor in ("__AMAP_JS_KEY__", "tripplanner_replace_poi", "tripplanner_move_poi",
                   "tripplanner_export", "tripplanner_get_plan", "tripplanner_panel_ready",
                   "ui/download-file", "draggable", "webapi.amap.com",
                   # M2.1：本地 HTTP API（宿主 tools/call 通道已实测不通）
                   "__LOCAL_API_BASE__", "__LOCAL_API_TOKEN__",
                   "/api/health", "/api/plan", "/api/replace", "/api/move", "/api/export",
                   "probeApi",
                   # M2.2：可观测性（失败必须看得见，否则又回到「点了没反应」）
                   "showAlert", "unhandledrejection", "/api/log", "ensureApi"):
        chk(f"含锚点 {anchor}", anchor in html)

    # 3b) M2.2 专项守卫 —— 这几条都是踩过的真坑，写死防复发
    # GET/HEAD 带 body 会让 fetch **同步**抛 TypeError，启动链静默断掉（2026-10-09 实测：
    # 日志里连一条 /api/plan 都没有，用户侧就是「点了没反应」）。
    chk("GET/HEAD 不带 body 的守卫仍在",
        'm !== "GET" && m !== "HEAD"' in html)
    chk("面板带醒目失败横幅（#alert）", 'id="alert"' in html)
    chk("启动即打印面板轨迹（panel.boot）", 'trace("panel.boot"' in html)

    # 4) 内联 JS 语法
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    js = m.group(1) if m else ""
    js_path = os.path.join(HERE, "_panel_check.js")
    open(js_path, "w", encoding="utf-8").write(js)
    try:
        r = subprocess.run([NODE, "--check", js_path], capture_output=True, text=True)
    finally:
        if os.path.exists(js_path):
            os.remove(js_path)
    chk("内联 JS 通过 node --check", r.returncode == 0,
        (r.stderr or "").strip().splitlines()[-1] if r.returncode else "ok")

    # 5) key 安全：amapKey 的初值必须是占位符（精确锚点，别用宽正则）
    mkey = re.search(r'var\s+amapKey\s*=\s*"([^"]*)"', html)
    chk("amapKey 初值是占位符（真实 key 只在服务端注入）",
        bool(mkey) and mkey.group(1) == "__AMAP_JS_KEY__",
        repr(mkey.group(1)) if mkey else "未找到 amapKey 定义")
    chk("panel 里没有 amap_js_key 的**值**（字段名出现在提示文案里是允许的）",
        not re.search(r'amap_js_key["\']?\s*[:=]\s*["\'][0-9A-Za-z]{16,}', html))

    # 6) M2.1：本地 API 的 base/token 初值必须是占位符（真实值只在服务端注入）
    mbase = re.search(r'var\s+API_BASE\s*=\s*"([^"]*)"', html)
    chk("API_BASE 初值是占位符（真实端口只在服务端注入）",
        bool(mbase) and mbase.group(1) == "__LOCAL_API_BASE__",
        repr(mbase.group(1)) if mbase else "未找到 API_BASE 定义")
    mtok = re.search(r'var\s+API_TOKEN\s*=\s*"([^"]*)"', html)
    chk("API_TOKEN 初值是占位符（真实 token 只在服务端注入）",
        bool(mtok) and mtok.group(1) == "__LOCAL_API_TOKEN__",
        repr(mtok.group(1)) if mtok else "未找到 API_TOKEN 定义")

    print()
    if fails:
        print(f"[FAIL] {len(fails)} 项未通过")
        return 1
    print("[PASS] 面板守卫全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
