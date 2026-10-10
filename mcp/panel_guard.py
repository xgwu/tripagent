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
  - M5 起同一套办法也套在**后端**上：`REMOTE_BASE` / `REMOTE_AUTH` / `SESSION_ID`
    的初值必须是占位符（真实地址与凭据只在服务端注入，仓库里绝不硬编码）。
"""
from __future__ import annotations

import json
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
                   "/api/health", "/api/plan", "/api/replace", "/api/move", "/api/export",
                   "probeApi",
                   # M2.2：可观测性（失败必须看得见，否则又回到「点了没反应」）
                   "showAlert", "unhandledrejection", "/api/log", "ensureApi",
                   # M2.3：掉线自愈（后端不可达时面板要能自己重试等待）
                   "waitForApi", "startWatchdog", "watchdogTick", "retryApi",
                   # M3/M5：线上后端 + 前端写回（后端只回「一天」时前端复现 apply_day）
                   "__REMOTE_API_BASE__", "REMOTE_BASE", "BACKENDS", "pickBackend",
                   "applyDayToPlan", "applyActionResult",
                   # M5：线上后端 = 自建云主机（http+IP + nginx Basic 认证）
                   "__REMOTE_API_AUTH__", "REMOTE_AUTH", "Authorization",
                   # M5：会话（云端无状态 ⇒「我这份行程」靠 session 认领）
                   "__SESSION_ID__", "SESSION_ID", "__PLAN_JSON__", "PLAN_SEED",
                   # M4 诊断：上报宿主上下文（判定混合内容是否拦）
                   "panel.remoteProbe", "location.protocol"):
        chk(f"含锚点 {anchor}", anchor in html)

    # 3b) M2.2 专项守卫 —— 这几条都是踩过的真坑，写死防复发
    # GET/HEAD 带 body 会让 fetch **同步**抛 TypeError，启动链静默断掉（2026-10-09 实测：
    # 日志里连一条 /api/plan 都没有，用户侧就是「点了没反应」）。
    chk("GET/HEAD 不带 body 的守卫仍在",
        'm !== "GET" && m !== "HEAD"' in html)
    chk("面板带醒目失败横幅（#alert）", 'id="alert"' in html)
    chk("启动即打印面板轨迹（panel.boot）", 'trace("panel.boot"' in html)

    # 3c) M3 专项守卫
    # 线上兜底 base 的初值必须是占位符（真实域名只在服务端注入；仓库里不留硬编码地址）
    mrem = re.search(r'var\s+REMOTE_BASE\s*=\s*"([^"]*)"', html)
    chk("REMOTE_BASE 初值是占位符（真实域名只在服务端注入）",
        bool(mrem) and mrem.group(1) == "__REMOTE_API_BASE__",
        repr(mrem.group(1)) if mrem else "未找到 REMOTE_BASE 定义")
    # M5：后端列表**只允许线上** —— 「本地服务下线、后端全走线上」是本版的设计前提
    #   （MCP App 要打包分享给别人，对方不该被要求装 Python + 数据 + Key）。
    chk("BACKENDS 只挂线上后端（M5 本地服务已下线）",
        bool(re.search(r'var\s+BACKENDS\s*=\s*\[\s*\]', html))
        and bool(re.search(r'if\s*\(\s*REMOTE_BASE\s*\)\s*BACKENDS\.push\(\{\s*name:\s*"线上"',
                           html))
        and "本地" not in re.search(r'var\s+BACKENDS[\s\S]{0,220}', html).group(0),
        "BACKENDS 应为空数组 + 仅 push 线上")
    # 导出必须把当前行程带给后端（线上无状态，不带就没有内容可导）
    chk("导出把当前行程传给后端（plan: LAST）",
        re.search(r'apiCall\(\s*"/api/export"[^)]*plan:\s*LAST', html, re.S) is not None)
    # 「拉行程」走 shape=mcp + session（云端无状态，靠 session 认领「本会话当前行程」）
    chk("拉行程带 shape=mcp + session",
        re.search(r'"/api/plan\?shape=mcp"', html) is not None
        and re.search(r'path\s*\+=\s*"&session="', html) is not None)
    # 交互（换点 / 挪时段）必须把 session + 整份 plan 带上 —— 云端据此更新会话表，
    # 否则「重新拉取行程」会拉回改动前的那份（M5 起这是新的正确性前提）。
    chk("换点 / 挪时段带 session 与整份 plan",
        len(re.findall(r'session:\s*SESSION_ID,\s*plan:\s*LAST', html)) == 2)
    # M4/M5：远端凭据的初值必须是占位符（真实 Basic 头只在服务端注入，绝不入库）
    mauth = re.search(r'var\s+REMOTE_AUTH\s*=\s*"([^"]*)"', html)
    chk("REMOTE_AUTH 初值是占位符（凭据只在服务端注入）",
        bool(mauth) and mauth.group(1) == "__REMOTE_API_AUTH__",
        repr(mauth.group(1)) if mauth else "未找到 REMOTE_AUTH 定义")
    # 远端请求必须把凭据发出去（否则线上后端开了 Basic 认证就 401）
    chk("httpJsonOn 给远端后端带 Authorization",
        bool(re.search(r'bk\.auth\)\s*opt\.headers\[["\']Authorization["\']\]', html)))
    # M4 诊断（2026-10-10）：远端走 http+IP 时能否成立，取决于宿主 iframe 的上下文
    #   （https ⇒ 浏览器把 http://公网IP 当 active mixed content 直接 block）。
    #   故面板必须**自报上下文**、并在启动时**主动探一次远端**，把结论落盘。
    chk("启动诊断上报宿主上下文（location.protocol/origin）",
        "location.protocol" in html and "location.origin" in html)
    chk("启动即主动探远端后端（panel.remoteProbe 落盘）",
        'trace("panel.remoteProbe"' in html)

    # 3.5) mode 徽标口径 —— **真跑一遍**（2026-10-10 实测暴露的真缺陷：原逻辑只判
    #      /^llm/，而线上主路径是 `m7_proposal`（不以 llm 开头）⇒ 每一次 M7 行程
    #      都被错标成「离线兜底规划」。同一个口径在 Web UI / 导出内核 / 面板**各写
    #      了一遍**，所以必须验**行为**、不能只 grep 源码。
    #      ⚠️ 中文/箭头别过命令行（Windows GBK 污染）→ 写成 UTF-8 的 .js 再跑。
    mblk = re.search(r"(var _m = String\(plan\.mode.*?\+ '</span>';)", html, re.S)
    if not mblk:
        chk("mode 徽标逻辑可定位（供行为验证）", False,
            "未找到 `var _m = String(plan.mode …` 代码块")
    else:
        expect = [("m7_proposal", "M7 经验提案", False),
                  ("m7_proposal → m7_degraded", "（降级）", False),
                  ("m7_proposal_failed → m2_fallback", "（降级）", False),
                  ("llm", "LLM 规划", False),
                  ("m2_toptw", "M2 库内直选", False),
                  ("offline_fallback", "离线兜底", True)]
        js_p = os.path.join(HERE, "_mode_check.js")
        src = ("var plan = {mode: ''};\n"
               "function badge(){\n" + mblk.group(1) + "\n  return modeBadge;\n}\n"
               "var CASES = " + json.dumps([c for c, _, _ in expect], ensure_ascii=False) + ";\n"
               "console.log(JSON.stringify(CASES.map(function(c){"
               "plan.mode = c; return badge(); })));\n")
        with open(js_p, "w", encoding="utf-8") as f:
            f.write(src)
        try:
            jr = subprocess.run([NODE, js_p], capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
            got = json.loads((jr.stdout or "[]").strip() or "[]")
        except Exception as e:                                    # noqa: BLE001
            got = None
            print(f"  (mode 探针执行失败: {e})")
        finally:
            if os.path.exists(js_p):
                os.remove(js_p)
        if got is None or len(got) != len(expect):
            chk("mode 徽标口径正确（主路径不得标成兜底）", False,
                f"探针未产出结果 stdout={jr.stdout!r} stderr={(jr.stderr or '')[:120]!r}")
        else:
            bad = []
            for (mode, must, wants_fallback), out in zip(expect, got):
                out = str(out)
                if must not in out:
                    bad.append(f"{mode} -> {out}（缺 {must}）")
                if wants_fallback and "兜底" not in out:
                    bad.append(f"{mode} -> {out}（未标兜底）")
                if not wants_fallback and "兜底" in out:
                    bad.append(f"{mode} -> {out}（误标兜底）")
            chk("mode 徽标口径正确（主路径不得标成兜底）", not bad, "; ".join(bad)[:180])

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

    # 6) M5：会话 id 与行程种子都必须是占位符（真实值只在服务端注入）
    msid = re.search(r'var\s+SESSION_ID\s*=\s*"([^"]*)"', html)
    chk("SESSION_ID 初值是占位符（真实 id 只在服务端注入）",
        bool(msid) and msid.group(1) == "__SESSION_ID__",
        repr(msid.group(1)) if msid else "未找到 SESSION_ID 定义")
    chk("行程种子 PLAN_SEED 由注入而来（__PLAN_JSON__）",
        re.search(r'var\s+PLAN_SEED\s*=\s*__PLAN_JSON__\s*;', html) is not None)
    chk("panel 里没有本地 API 残留（本地服务已下线）",
        "__LOCAL_API_BASE__" not in html and "__LOCAL_API_TOKEN__" not in html
        # ⚠️ 用 \b 边界匹配：`__REMOTE_API_BASE__` 里也含子串 "API_BASE"，
        #    直接 in 会误报（本项目「校验脚本自己的正则比被测对象更危险」的老坑）。
        and re.search(r"\bAPI_BASE\b", html) is None
        and re.search(r"\bAPI_TOKEN\b", html) is None)

    print()
    if fails:
        print(f"[FAIL] {len(fails)} 项未通过")
        return 1
    print("[PASS] 面板守卫全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
