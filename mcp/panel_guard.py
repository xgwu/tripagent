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
                   "panel.remoteProbe", "location.protocol",
                   # M6：面板高度上报（宿主据此把 iframe 撑到内容高度 ⇒ 面板内不再滚动）
                   "__PANEL_VERSION__", "ui/notifications/size-changed",
                   "contentHeight", "syncHeight", "scheduleHeight", "watchSize",
                   'id="diagSize"', "panel-size:block:start"):
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

    # 3.7) 面板高度上报 —— 同样**真跑**（M6，2026-10-10）
    #      为什么必须验行为：这两条错了都**不报错**，只是面板里又出现滚动条（正是要修的东西）：
    #        ① method 名写错 ⇒ 宿主 bridge 直接忽略 ⇒ 静默退回定高 iframe；
    #        ② 只报「变大」不报「变小」⇒ 换天/换点后内容变矮，iframe 收不回去、留一大片空白。
    #      另外 `contentHeight()` 在「内容比视口矮」时必须走 body 分支 —— 根元素 scrollHeight
    #      至少等于视口高度，用它就永远缩不回去。
    msize = re.search(r"// ---- panel-size:block:start ----(.*?)// ---- panel-size:block:end ----",
                      html, re.S)
    if not msize:
        chk("高度上报逻辑可定位（供行为验证）", False, "未找到 panel-size:block 标记")
    else:
        sz_p = os.path.join(HERE, "_size_check.js")
        harness = (
            "var SENT = [];\n"
            "var window = { innerHeight: 400,\n"
            "  getComputedStyle: function(){ return { marginTop: '0px', marginBottom: '0px' }; },\n"
            "  addEventListener: function(){} };\n"
            "window.parent = { postMessage: function(m){ SENT.push(m); } };\n"
            "var document = { documentElement: { scrollHeight: 0 },\n"
            "  body: { getBoundingClientRect: function(){ return { height: 0 }; } } };\n"
            "function setDiag(){}\nfunction log(){}\n"
            + msize.group(1) + "\n"
            "function setState(vh, sh, bodyH){\n"
            "  window.innerHeight = vh;\n"
            "  document.documentElement.scrollHeight = sh;\n"
            "  document.body.getBoundingClientRect = function(){ return { height: bodyH }; };\n"
            "}\n"
            "var out = [];\n"
            "setState(400, 3000, 3000); syncHeight('长内容');\n"
            "out.push({ n: SENT.length, last: SENT[SENT.length-1], h: contentHeight() });\n"
            "syncHeight('同高重复');\n"
            "out.push({ n: SENT.length });\n"
            "setState(3000, 3000, 500); syncHeight('内容变矮');\n"
            "out.push({ n: SENT.length, last: SENT[SENT.length-1], h: contentHeight() });\n"
            "setState(100, 5000, 5000); syncHeight('内容再变高');\n"
            "out.push({ n: SENT.length, last: SENT[SENT.length-1], h: contentHeight() });\n"
            "console.log(JSON.stringify(out));\n"
        )
        with open(sz_p, "w", encoding="utf-8") as f:
            f.write(harness)
        try:
            sr = subprocess.run([NODE, sz_p], capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
            sz = json.loads((sr.stdout or "[]").strip() or "[]")
        except Exception as e:                                    # noqa: BLE001
            sz = None
            print(f"  (高度探针执行失败: {e})")
        finally:
            if os.path.exists(sz_p):
                os.remove(sz_p)
        if not sz or len(sz) != 4:
            chk("高度上报行为正确（撑着长内容 / 会收回去）", False,
                f"探针未产出结果 stdout={sr.stdout!r} stderr={(sr.stderr or '')[:140]!r}")
        else:
            bad = []
            first = sz[0].get("last") or {}
            if sz[0].get("n") != 1:
                bad.append(f"长内容应上报 1 次，实际 {sz[0].get('n')}")
            if sz[0].get("h") != 3000:
                bad.append(f"长内容时高度应为 3000，实际 {sz[0].get('h')}")
            if first.get("method") != "ui/notifications/size-changed":
                bad.append(f"method 名错：{first.get('method')!r}（宿主会静默忽略）")
            if (first.get("params") or {}).get("height") != 3000:
                bad.append(f"params.height 错：{(first.get('params') or {}).get('height')!r}")
            if sz[1].get("n") != 1:
                bad.append(f"同高重复上报未去抖（{sz[1].get('n')} 次）")
            if sz[2].get("n") != 2 or sz[2].get("h") != 500:
                bad.append(f"内容变矮应收回到 500 并上报，实际 h={sz[2].get('h')} n={sz[2].get('n')}")
            if sz[3].get("n") != 3 or sz[3].get("h") != 5000:
                bad.append(f"内容再变高应上报 5000，实际 h={sz[3].get('h')} n={sz[3].get('n')}")
            chk("高度上报行为正确（撑着长内容 / 会收回去）", not bad, "; ".join(bad)[:200])

    # 3.8) hop 段不得重复渲染里程/时长 —— 同样**真跑**（2026-10-10 报障）
    #      症状：面板出现「→ 车程 15 分钟 · 3.7km　3.7km · 15分钟」（前后各一遍）。
    #      根因：hop 的 `name` 已经是 `src/sequencer.py::_hop_label()` 产出的
    #            「车程 15 分钟 · 3.7km」——**自带** km 与分钟；而渲染层又拿结构化
    #            `s.km`/`s.min` 拼了一遍。同一个坑 `src/md_export.py` 里也有一份
    #            （已一并修）⇒ 属于「同一口径复制到多处」，必须验**行为**、不能只 grep。
    # ⚠️ 锚点两个坑都踩过（「锚点比被测对象更危险」）：
    #    ① 开头必须带 `var poiSeq = 0;` —— 面板里有 **4 处** `dl.forEach(function(d){`
    #       （675/741/859/870），只写 `dl.forEach` 会命中别的块（探针报 SyntaxError / 假绿）；
    #    ② 结尾必须用 **4 空格**的 `\n    });`（= `dl.forEach` 自己的闭合，804 行）——
    #       写成 6 空格的 `\n      });` 会命中**内层** `(d.timeline||[]).forEach(...)` 的闭合
    #       （792 行）⇒ 抽出的块少一层闭合、node 报 Unexpected end of input。
    mhop = re.search(r"dl\.forEach\(function\(d\)\{\n      var poiSeq = 0;([\s\S]*?)\n    \}\);",
                     html)
    if not mhop:
        chk("hop 渲染逻辑可定位（供行为验证）", False,
            "未找到 `dl.forEach(function(d){` 渲染块")
    else:
        hp = os.path.join(HERE, "_hop_check.js")
        hsrc = (
            "var esc = function(x){ return String(x == null ? '' : x); };\n"
            "var durText = function(){ return ''; };\n"
            "var dayPoiIds = {};\n"
            "function replacePoi(){}\n"      # 只在 on 字符串里出现，仍需存在
            "var h = '';\n"
            "var dl = [{ day: 1, theme: '西湖亲子日', travel_km: 7.7, travel_h: 0.5,\n"
            "  finish: '19:00', timeline: [\n"
            "    { type: 'poi', name: '断桥残雪', start: '09:00', end: '10:00', id: 'HZ01', photo: '' },\n"
            "    { type: 'hop', name: '车程 15 分钟 · 3.7km', start: '10:00', end: '10:15',\n"
            "      km: 3.7, min: 15 },\n"
            "    { type: 'poi', name: '灵隐寺', start: '10:15', end: '11:15', id: 'HZ02', photo: '' } ] }];\n"
            # body 从 `var poiSeq = 0;` **之后**开始（锚点把它消费掉了）⇒ 这里补回，
            # 否则块内 `poiSeq++` 会 ReferenceError。
            "dl.forEach(function(d){\n      var poiSeq = 0;" + mhop.group(1) + "\n});\n"
            "console.log(h);\n"
        )
        with open(hp, "w", encoding="utf-8") as f:
            f.write(hsrc)
        try:
            hr = subprocess.run([NODE, hp], capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
            hout = hr.stdout or ""
        except Exception as e:                                    # noqa: BLE001
            hout = ""
            print(f"  (hop 探针执行失败: {e})")
        finally:
            if os.path.exists(hp):
                os.remove(hp)
        n_km = hout.count("3.7km")
        chk("hop 行的里程只渲染一次（name 已含，不得再拼 s.km/s.min）",
            n_km == 1 and "车程 15 分钟" in hout,
            f"『3.7km』出现 {n_km} 次（应为 1）｜渲染片段={hout[:200]!r}")

    # 3.9) 地图：逐段真实路网（对齐 webapp）—— 2026-10-10（M6.2）
    #      旧实现只有「当天 POI 按顺序连成**一条** AMap.Polyline」＝直线折线；webapp
    #      (webui/index.html::drawRealRoutes) 是逐段调 `/api/route` 画**真实路网**。
    #      这属于「同一功能两份渲染代码、其中一份没跟上」⇒ 必须验**行为**，
    #      光 grep 到 `/api/route` 字符串不算（曾踩：锚点/字面量比被测对象更危险）。
    for _need, _why in (
        ("/api/route", "面板没调用 /api/route ⇒ 地图只可能是直线"),
        ("DAY_COLORS", "面板缺按天配色"),
        ("DAY_VIS", "面板缺按天显隐"),
        ("buildLegs", "面板缺「腿」收集（酒店锚点 / 城际转移）"),
        ("toggleDayLine", "面板缺 Day 开关行为"),
    ):
        chk("地图对齐 webapp：面板含 " + _need, _need in html, _why)

    def _fnbody(_re):
        _m = re.search(_re, html)
        return _m.group(1) if _m else None

    _fr = _fnbody(r"function routeMode\(q\)\{([\s\S]*?)\n  \}")
    _fb = _fnbody(r"function buildLegs\(plan\)\{([\s\S]*?)\n  \}")
    _fd = _fnbody(r"function drawLeg\(leg, a, b, token\)\{([\s\S]*?)\n  \}")
    if _fr is None or _fb is None or _fd is None:
        chk("地图渲染逻辑可定位（供行为验证）", False,
            "未找到 routeMode / buildLegs / drawLeg 之一（函数签名被改？）")
    else:
        _mp = os.path.join(HERE, "_map_check.js")
        _msrc = (
            "var DAY_COLORS = ['#a', '#b'];\n"
            "var DAY_LINES = {}, DAY_VIS = {}, DAY_MARKERS = {};\n"
            "var MAP_TOKEN = 7;\n"
            "var calls = [], polys = [], added = 0;\n"
            "var esc = function(x){ return String(x == null ? '' : x); };\n"
            "function httpJson(path, body, method){\n"
            "  calls.push({ path: path, method: method });\n"
            "  return Promise.resolve({ ok: true, points: [[1,1],[2,2]] });\n"
            "}\n"
            "var amapObj = { add: function(l){ added++; } };\n"
            "var AMap = { Polyline: function(o){ polys.push({ o: o, hidden: false,\n"
            "  show: function(){ this.hidden = false; },\n"
            "  hide: function(){ this.hidden = true; } }); } };\n"
            "function routeMode(q){" + _fr + "\n}\n"
            "function buildLegs(plan){" + _fb + "\n}\n"
            "function drawLeg(leg, a, b, token){" + _fd + "\n}\n"
            "var plan = { query: '杭州亲子2天', hotel: { name: 'H', lat: 30.25, lng: 120.15 },\n"
            "  dayList: [\n"
            "    { day: 1, timeline: [ { type: 'hotel', name: 'H' },\n"
            "      { type: 'poi', name: 'A', lat: 30.20, lng: 120.10 },\n"
            "      { type: 'hop', name: 'h', km: 1, min: 5 },\n"
            "      { type: 'poi', name: 'B', lat: 30.21, lng: 120.11 } ] },\n"
            "    { day: 2, timeline: [ { type: 'poi', name: 'C', lat: 30.22, lng: 120.12 },\n"
            "      { type: 'poi', name: 'D', lat: 30.23, lng: 120.13 } ] } ] };\n"
            "var plan2 = { query: '骑行环湖', hotel: null, dayList: [\n"
            "    { day: 1, timeline: [ { type: 'poi', name: 'A', lat: 30.2, lng: 120.1 } ] },\n"
            "    { day: 2, timeline: [ { type: 'transfer', name: 't' },\n"
            "      { type: 'poi', name: 'B', lat: 30.3, lng: 120.2 } ] } ] };\n"
            "var legs = buildLegs(plan);\n"
            "var legs2 = buildLegs(plan2);\n"
            "var jobs = [];\n"
            "legs.forEach(function(leg){\n"
            "  for (var i = 0; i < leg.pts.length - 1; i++){\n"
            "    jobs.push(drawLeg(leg, leg.pts[i], leg.pts[i+1], MAP_TOKEN)); } });\n"
            "Promise.all(jobs).then(function(oks){\n"
            "  console.log(JSON.stringify({\n"
            "    nLegs: legs.length, legPts: legs.map(function(l){ return l.pts.length; }),\n"
            "    modes: legs.map(function(l){ return l.mode; }),\n"
            "    nCalls: calls.length,\n"
            "    allGet: calls.every(function(c){ return c.method === 'GET'; }),\n"
            "    routePath: calls.every(function(c){ return c.path.indexOf('/api/route?mode=') === 0; }),\n"
            "    nPolys: polys.length, nAdded: added, days: Object.keys(DAY_LINES),\n"
            "    dash: polys.map(function(p){ return p.o.strokeStyle; }),\n"
            "    nOk: oks.filter(function(o){ return o === true; }).length,\n"
            "    legs2: legs2.length, city2: legs2[0] ? legs2[0].intercity : null,\n"
            "    mode2: legs2[0] ? legs2[0].mode : null, color2: legs2[0] ? legs2[0].color : null,\n"
            "    pts2: legs2[0] ? legs2[0].pts.length : null }));\n"
            "});\n"
        )
        with open(_mp, "w", encoding="utf-8") as f:
            f.write(_msrc)
        try:
            _mr = subprocess.run([NODE, _mp], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=60)
            _mout = (_mr.stdout or "").strip()
        except Exception as e:                                    # noqa: BLE001
            _mout = ""
            print(f"  (地图探针执行失败: {e})")
        finally:
            if os.path.exists(_mp):
                os.remove(_mp)
        import json as _json
        _j = None
        for _line in reversed(_mout.splitlines()):
            try:
                _j = _json.loads(_line)
                break
            except Exception:                                     # noqa: BLE001
                continue
        if not _j:
            chk("地图渲染行为验证（node 真跑）", False,
                f"探针无 JSON 输出：{_mout[:200]!r}")
        else:
            _ok = (
                _j.get("nLegs") == 2 and _j.get("legPts") == [4, 2]
                and _j.get("modes") == ["walking", "walking"]
                and _j.get("nCalls") == 4 and _j.get("allGet") is True
                and _j.get("routePath") is True
                and _j.get("nPolys") == 4 and _j.get("nAdded") == 4
                and sorted(_j.get("days") or []) == ["1", "2"]
                and all(_d == "dashed" for _d in (_j.get("dash") or []))
                and _j.get("nOk") == 4
                and _j.get("legs2") == 1 and _j.get("city2") is True
                and _j.get("mode2") == "driving" and _j.get("color2") == "#6b46c1"
                and _j.get("pts2") == 2
            )
            chk("地图逐段真实路网 + 酒店腿 + 城际腿 + 按天归组（node 真跑）", _ok,
                f"实测={_j}｜期望 nLegs=2 legPts=[4,2] nCalls=4 nPolys=4 城际=1(driving)")

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
