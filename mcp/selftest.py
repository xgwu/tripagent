#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MCP server 本地自检 —— 不开 WorkBuddy 也能验协议（把 server 当子进程拉起，走真实 stdio）

用法：
    D:\\Users\\xgwu\\.workbuddy\\binaries\\python\\envs\\tripagent\\Scripts\\python.exe mcp/selftest.py

验的是：initialize 协商、tools/list（含面板标记）、resources/read（含 _meta.ui.csp、地图 key、
线上后端 base + Basic 凭据 + session id 注入）、tools/call（全量 9 个工具，含 M1 真实规划 +
M2 换点/挪时段/导出）、A+C 面板分流、跨进程取数、**M5 线上后端**（health / plan?shape=mcp /
会话回读 / 换点 / 挪时段 / 导出 / 城市 / 日志，含「没有 src/ 与 data/ 也能跑」的分享包模拟），
以及「stdout 是否被日志污染」。

⚠️ M5 起 MCP server 是**瘦客户端**：规划 / 换点 / 挪时段 / 导出**全部委托云端后端**
   （base + Basic 凭据由 `remote.json` → 环境变量 → `secrets.json` 逐级解析）。
   ⇒ 本自检**需要网络**，且会真的消耗云端 LLM 费用（每处规划用例约 1 次 DeepSeek 调用）。
⚠️ 阶段六打的是**云端**（`/api/plan?shape=mcp` 等）。本地常驻 HTTP 服务（`mcp/local_api.py`）
   自 M5 起已**不在主链路**，本自检也不再拉起它。
"""
from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "tripplanner_mcp_server.py")

OK = "  [PASS]"
NG = "  [FAIL]"
fails = []
passed = [0]      # 计数用容器（闭包改写；check() 里 +1）


def check(label, cond, detail=""):
    print(f"{OK if cond else NG} {label}" + (f"  -> {detail}" if detail else ""))
    if cond:
        passed[0] += 1
    if not cond:
        fails.append(label)


def spawn(env=None):
    """env=None → 继承当前进程环境；传 dict → 完全替换（用于模拟宿主的贫瘠环境）。"""
    p = subprocess.Popen(
        [sys.executable, SERVER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
        env=env,
    )
    return p


def rpc(proc, obj):
    """发一条请求并读回一行。只用于**有 id 的请求**。"""
    proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        return None
    return json.loads(line)


def notify(proc, obj):
    """发一条通知（无 id）——按协议**不该有回复**，所以绝不读 stdout。
    上一版这里误用 rpc() 去读行，server 正确地不回复 -> 读阻塞 -> 假死。"""
    proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def shutdown(proc):
    """收尾：关 stdin 让 server 自然退出，并把 stderr 交回调用方。"""
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
    return err


def init(proc):
    r = rpc(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18",
                   "capabilities": {},
                   "clientInfo": {"name": "selftest", "version": "0"}},
    })
    notify(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    return r


# ===========================================================================
# 阶段一：协议 + 工具面 + A+C 分流（沿用 M0/M1pre 用例）
# ===========================================================================
def phase_protocol():
    print("--- 阶段一：协议与工具面 ---")
    proc = spawn()
    try:
        r = init(proc)
        check("initialize 有响应", r is not None, json.dumps(r, ensure_ascii=False)[:120])
        info = ((r or {}).get("result") or {}).get("serverInfo") or {}
        check("serverInfo.name == tripplanner", info.get("name") == "tripplanner", str(info))
        check("协商回 protocolVersion",
              ((r or {}).get("result") or {}).get("protocolVersion") == "2025-06-18")

        # 通知不该有回复 —— 用一条 ping 请求当「栅栏」
        r = rpc(proc, {"jsonrpc": "2.0", "id": 99, "method": "ping"})
        check("initialized 通知不产生多余回复（栅栏 ping 正常）",
              r is not None and r.get("id") == 99, json.dumps(r, ensure_ascii=False)[:80])

        r = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = ((r or {}).get("result") or {}).get("tools") or []
        names = [t["name"] for t in tools]
        check("tools/list 返回 9 个工具（M1 六个 + M2 三个）", len(tools) == 9, str(names))
        by = {t["name"]: t for t in tools}

        # 面板 URI 从 server **动态取** —— 按项目惯例「源码守卫锁 wiring，别硬编码版本号」，
        # 以后改 PANEL_VERSION 不必回来改测试。
        panel_uri = ((((by.get("tripplanner_plan_trip") or {}).get("_meta") or {})
                      .get("ui") or {}).get("resourceUri")) or ""
        check("plan_trip 带面板 resourceUri（ui://tripplanner/plan-*）",
              panel_uri.startswith("ui://tripplanner/plan"), repr(panel_uri))

        # 带面板：plan_trip / open_plan 必须同一个 resourceUri
        for tname in ("tripplanner_plan_trip", "tripplanner_open_plan"):
            meta = ((by.get(tname) or {}).get("_meta") or {}).get("ui") or {}
            check(f"{tname} 的 resourceUri 与 plan_trip 一致",
                  meta.get("resourceUri") == panel_uri, repr(meta.get("resourceUri")))
            check(f"{tname} visibility == [model, app]",
                  meta.get("visibility") == ["model", "app"], repr(meta.get("visibility")))

        # 不带面板：其余六个（若误配会每次调用都堆一扇面板）
        for tname in ("tripplanner_get_plan", "tripplanner_list_cities",
                      "tripplanner_ping", "tripplanner_panel_ready",
                      "tripplanner_replace_poi", "tripplanner_move_poi",
                      "tripplanner_export"):
            check(f"{tname} 不带 _meta（对照组）",
                  not (by.get(tname) or {}).get("_meta"),
                  repr((by.get(tname) or {}).get("_meta")))

        plan_schema = (by.get("tripplanner_plan_trip") or {}).get("inputSchema") or {}
        check("plan_trip 必填 query",
              "query" in (plan_schema.get("required") or []), str(plan_schema.get("required")))

        r = rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        res = ((r or {}).get("result") or {}).get("resources") or []
        check("resources/list 1 条", len(res) == 1)
        check("资源 mimeType = text/html;profile=mcp-app",
              res and res[0].get("mimeType") == "text/html;profile=mcp-app",
              str(res[0].get("mimeType") if res else None))

        r = rpc(proc, {"jsonrpc": "2.0", "id": 4, "method": "resources/read",
                       "params": {"uri": panel_uri}})
        contents = ((r or {}).get("result") or {}).get("contents") or []
        c0 = contents[0] if contents else {}
        html = c0.get("text") or ""
        nbytes = len(html.encode("utf-8"))
        check("resources/read 返回 HTML", html.startswith("<!DOCTYPE html") and len(html) > 1000,
              f"{nbytes} bytes")
        check("HTML 在 256KiB 预取阈值内", nbytes < 256 * 1024, f"{nbytes} < 262144")
        csp = ((c0.get("_meta") or {}).get("ui", {}) or {}).get("csp", {}) or {}
        check("contents[0] 带 _meta.ui.csp.frameDomains",
              "frameDomains" in json.dumps(csp, ensure_ascii=False),
              json.dumps(csp, ensure_ascii=False)[:100])
        rdom = csp.get("resourceDomains") or []
        check("resourceDomains 放行高德图片域名",
              any("autonavi.com" in str(d) for d in rdom),
              json.dumps(rdom, ensure_ascii=False)[:140])
        # M2：高德 JS API 的 JS 载体走 script-src（= resourceDomains），
        # 其服务端请求走 connect-src（= connectDomains）—— 两条都要放行，
        # 否则地图会「加载了但瓦片/接口全被拦」，且**不报错**（静默失败）。
        cdom = csp.get("connectDomains") or []
        check("connectDomains 放行高德（JS API 的 XHR）",
              any("amap.com" in str(d) for d in cdom),
              json.dumps(cdom, ensure_ascii=False)[:160])
        check("面板零外部引用（无 src=/href= http）",
              ("src=\"http" not in html) and ("href=\"http" not in html))
        check("面板含 A 方案上报（tripplanner_panel_ready）", "tripplanner_panel_ready" in html)
        check("面板含 appInstanceId 提取逻辑", "appInstanceId" in html)
        check("面板含 M1 数据通道（get_plan）", "tripplanner_get_plan" in html)
        check("面板渲染时间轴（timeline）", "timeline" in html)

        # ---- M2 面板能力 ----
        check("面板含 M2 换点入口（replace_poi）", "tripplanner_replace_poi" in html)
        check("面板含 M2 挪时段入口（move_poi）", "tripplanner_move_poi" in html)
        check("面板含 M2 导出入口（export）", "tripplanner_export" in html)
        check("面板含拖拽排序（draggable）", "draggable" in html)
        check("面板含地图加载逻辑（webapi.amap.com）", "webapi.amap.com" in html)
        # 面板 iframe 的 sandbox 没有 allow-downloads ⇒ 导出必须走宿主能力
        check("面板走宿主下载能力（ui/download-file）", "ui/download-file" in html)
        check("resources/read 已把 key 占位符替换掉", "__AMAP_JS_KEY__" not in html)
        # key 安全：磁盘上的 panel 文件必须是占位符，真实 key 只在 read 时注入。
        # 注意**只断言占位符存在** —— 面板的提示文案里会提到 `amap_js_key`
        # 这个字段名（告诉用户去哪配），不能把字段名当 key 值来判。
        with open(os.path.join(HERE, "panel", "plan_panel.html"), encoding="utf-8") as _f:
            raw_panel = _f.read()
        check("panel 文件用 key 占位符（真实 key 不入库）",
              "__AMAP_JS_KEY__" in raw_panel)
        check("panel 文件不含 amap_js_key 的取值形式",
              "amap_js_key\"" not in raw_panel and "amap_js_key:" not in raw_panel)

        # ---- M5：后端**全走线上**（本地 HTTP 服务已下线）----
        # 背景：宿主 tools/call 通道实测不通（面板 ping 10s 超时）；M2 起的本地
        # HTTP 兜底也在 M5 撤掉 —— 因为 MCP App 要打包分享，规划必须在云端做。
        check("面板含后端 HTTP 客户端（probeApi + 五个端点）",
              all(k in html for k in ("probeApi", "/api/health", "/api/plan",
                                      "/api/replace", "/api/move", "/api/export")))
        check("面板保留宿主通道为兜底（tools/call 仍在）", "tools/call" in html)
        check("panel 文件用线上后端占位符（真实地址/凭据不入库）",
              "__REMOTE_API_BASE__" in raw_panel and "__REMOTE_API_AUTH__" in raw_panel
              and "__SESSION_ID__" in raw_panel)
        check("panel 文件已无本地 API 占位符（M5 下线）",
              "__LOCAL_API_BASE__" not in raw_panel
              and "__LOCAL_API_TOKEN__" not in raw_panel)
        # M6：面板自报版本必须由 server 注入（曾写死 `v=m12`，PANEL_VERSION 涨到 m13 后
        # 它就悄悄过期了 —— 「同一口径多处各写必漂移」的老坑）。这里做**交叉校验**：
        # 注入进面板的版本号必须与 resourceUri 里的版本号一致，任何一边漏改都会红。
        _ver = panel_uri.rsplit("-", 1)[-1] if panel_uri else ""
        check("panel 文件用版本占位符（版本号唯一来源是 PANEL_VERSION）",
              "__PANEL_VERSION__" in raw_panel and "v=__PANEL_VERSION__" in raw_panel)
        check(f"resources/read 注入的面板版本与 resourceUri 一致（{_ver}）",
              bool(_ver) and ("v=" + _ver + " base=") in html)
        m_base = re.search(r'var\s+REMOTE_BASE\s*=\s*"(https?://[^"]+)"', html)
        check("resources/read 已注入线上后端 base（真实地址）",
              bool(m_base), m_base.group(1) if m_base else "仍是占位符")
        m_auth = re.search(r'var\s+REMOTE_AUTH\s*=\s*"(Basic [A-Za-z0-9+/=]+)"', html)
        check("resources/read 已注入 Basic 凭据",
              bool(m_auth), f"len={len(m_auth.group(1))}" if m_auth else "未注入")
        check("resources/read 已注入 session id",
              re.search(r'var\s+SESSION_ID\s*=\s*"([0-9A-Za-z_-]{8,})"', html) is not None)

        # 兼容性（2026-10-09 踩坑）：改 PANEL_VERSION 后宿主**注册表里仍是旧 uri**
        # （注册表要等宿主重连才更新）。若 resources/read 严格比对当前 uri，
        # 就会出现「宿主拿旧 uri 请求 → 404 → 面板彻底打不开」的窗口期。
        r_old = rpc(proc, {"jsonrpc": "2.0", "id": 41, "method": "resources/read",
                           "params": {"uri": "ui://tripplanner/plan-m0"}})
        old_c = (((r_old or {}).get("result") or {}).get("contents") or [{}])[0]
        check("旧版本 uri 也能读到面板（防升级窗口期 404）",
              (old_c.get("text") or "").startswith("<!DOCTYPE html"),
              f"{(old_c.get('text') or '')[:12]!r} {len(old_c.get('text') or '')} chars")
        r_bad = rpc(proc, {"jsonrpc": "2.0", "id": 42, "method": "resources/read",
                           "params": {"uri": "ui://someone-else/thing"}})
        check("非本 server 的 uri 仍返回错误",
              bool((r_bad or {}).get("error")),
              str(((r_bad or {}).get("error") or {}).get("code")))

        # ---- A+C 分流 ----
        r = rpc(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "tripplanner_open_plan", "arguments": {}}})
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("首次 open_plan 判定为开面板", sc.get("panelOpened") is True,
              json.dumps(sc, ensure_ascii=False)[:120])
        check("无行程时 open_plan 标记 hasPlan=false", sc.get("hasPlan") is False, str(sc.get("hasPlan")))

        r = rpc(proc, {"jsonrpc": "2.0", "id": 51, "method": "tools/call",
                       "params": {"name": "tripplanner_open_plan", "arguments": {}}})
        sc2 = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("C 兜底：二次 open_plan 不再开面板",
              sc2.get("panelOpened") is False and sc2.get("reason") == "panel-already-open",
              json.dumps(sc2, ensure_ascii=False)[:140])

        r = rpc(proc, {"jsonrpc": "2.0", "id": 52, "method": "tools/call",
                       "params": {"name": "tripplanner_panel_ready",
                                  "arguments": {"app_instance_id": "inst-aaa", "label": "selftest"}}})
        sr = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("panel_ready 登记实例", sr.get("count") == 1 and sr.get("hasInstanceId") is True,
              json.dumps(sr, ensure_ascii=False)[:140])
        check("panel_ready 声明 policy=A+C", sr.get("policy") == "A+C", str(sr.get("policy")))

        r = rpc(proc, {"jsonrpc": "2.0", "id": 53, "method": "tools/call",
                       "params": {"name": "tripplanner_panel_ready",
                                  "arguments": {"app_instance_id": "inst-bbb"}}})
        sr2 = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("panel_ready 幂等累加实例（去重）",
              sr2.get("count") == 2
              and sorted(sr2.get("knownInstances") or []) == ["inst-aaa", "inst-bbb"],
              json.dumps(sr2, ensure_ascii=False)[:140])

        r = rpc(proc, {"jsonrpc": "2.0", "id": 54, "method": "tools/call",
                       "params": {"name": "tripplanner_open_plan",
                                  "arguments": {"app_instance_id": "inst-aaa"}}})
        sc3 = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("A 判据：已登记实例不再开面板",
              sc3.get("panelOpened") is False and "inst-aaa" in (sc3.get("knownInstances") or []),
              json.dumps(sc3, ensure_ascii=False)[:140])

        r = rpc(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                       "params": {"name": "tripplanner_ping", "arguments": {"note": "自检回显"}}})
        scp = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("ping 回显参数正确", scp.get("note") == "自检回显", json.dumps(scp, ensure_ascii=False))

        r = rpc(proc, {"jsonrpc": "2.0", "id": 8, "method": "no/such/method"})
        check("未知方法返回 -32601", ((r or {}).get("error") or {}).get("code") == -32601)
    finally:
        err = shutdown(proc)
    print("   （阶段一 stderr 末行）" + ((err or "").strip().splitlines() or [""])[-1][:100])
    return err


# ===========================================================================
# 阶段二：M1 真实规划（独立进程 —— 保证是「本进程首次调用」，可验开面板）
# ===========================================================================
def phase_planning():
    print()
    print("--- 阶段二：M1 真实规划 ---")
    proc = spawn()
    try:
        init(proc)

        # 城市清单
        r = rpc(proc, {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                       "params": {"name": "tripplanner_list_cities", "arguments": {}}})
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        cities = sc.get("cities") or []
        check("list_cities 返回 31 个城市", sc.get("count") == 31, str(sc.get("count")))
        check("城市条目含 pois 计数",
              bool(cities) and all(isinstance(c.get("pois"), int) for c in cities[:5]),
              json.dumps(cities[:3], ensure_ascii=False))
        check("杭州在库中", any(c.get("city") == "杭州" for c in cities),
              str([c.get("city") for c in cities[:6]]))

        # 无行程时 get_plan 应诚实说没有
        r = rpc(proc, {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                       "params": {"name": "tripplanner_get_plan", "arguments": {}}})
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("无行程时 get_plan hasPlan=false", sc.get("hasPlan") is False, json.dumps(sc)[:80])

        # ★真实规划（本进程首次带面板调用 → 应判定为「开面板」）
        r = call_plan(proc, {"query": "杭州2天，带8岁孩子，想看西湖和灵隐寺"}, 12)
        rc = (r or {}).get("result") or {}
        sc = rc.get("structuredContent") or {}
        check("plan_trip 返回 ok", sc.get("ok") is True, json.dumps(sc, ensure_ascii=False)[:160])

        plan = sc.get("plan") or {}
        check("城市识别为杭州", plan.get("city") == "杭州", str(plan.get("city")))
        check("天数识别为 2", plan.get("days") == 2, str(plan.get("days")))
        days = plan.get("dayList") or []
        check("行程含 2 天", len(days) == 2, str(len(days)))
        check("每天有 timeline",
              bool(days) and all(len(d.get("timeline") or []) > 0 for d in days),
              str([len(d.get("timeline") or []) for d in days]))
        check("timeline 含 poi 节点",
              any(s.get("type") == "poi" for d in days for s in (d.get("timeline") or [])))
        check("天级字段齐全（theme/travel_km/finish）",
              bool(days) and all(("theme" in d and "travel_km" in d and "finish" in d) for d in days))
        check("总里程为数值", isinstance(plan.get("totalTravelKm"), (int, float)),
              str(plan.get("totalTravelKm")))
        check("mode 字段有值", bool(plan.get("mode")), str(plan.get("mode")))
        # M5：mode 来自**云端**内核；LLM 成功的取值是 "m7_proposal"。
        # 判降级只认降级标记（`_not_degraded`），别再用 `mode == "llm"`。
        check("LLM 未静默降级（degraded=false 且 mode 无降级标记）",
              plan.get("degraded") is False and _not_degraded(plan),
              json.dumps({k: plan.get(k) for k in ("mode", "llmEnabled", "degraded")},
                         ensure_ascii=False))
        check("latency_s 随归一化下发（瘦客户端摘要必需）",
              isinstance(plan.get("latency_s"), (int, float)),
              str(plan.get("latency_s")))
        check("文案摘要非空", bool(((rc.get("content") or [{}])[0].get("text") or "").strip()))
        check("首次 plan_trip 判定为开面板", sc.get("panelOpened") is True,
              json.dumps({k: sc.get(k) for k in ("panelOpened", "stage")}))

        # POI 照片：有则必须是 http 直链（面板侧据此决定要不要渲染缩略图）
        photos = [s.get("photo") for d in days for s in (d.get("timeline") or [])
                  if s.get("type") == "poi"]
        check("poi 节点带 photo 字段（可为空串）",
              len(photos) > 0 and all(isinstance(p, str) for p in photos),
              f"{sum(1 for p in photos if p)}/{len(photos)} 有图")

        # M2：地图要坐标 —— 内核 timeline 不给，需 server 从 POI 库补齐（GCJ-02）
        poi_rows = [s for d in days for s in (d.get("timeline") or [])
                    if s.get("type") == "poi"]
        withxy = [s for s in poi_rows
                  if s.get("lat") is not None and s.get("lng") is not None]
        check("poi 节点带 lat/lng（地图所需）",
              len(poi_rows) > 0 and len(withxy) == len(poi_rows),
              f"{len(withxy)}/{len(poi_rows)} 有坐标")

        # 行程已缓存 → get_plan 能取回
        r = rpc(proc, {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
                       "params": {"name": "tripplanner_get_plan", "arguments": {}}})
        sc2 = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("get_plan 取回行程", sc2.get("hasPlan") is True
              and (sc2.get("plan") or {}).get("city") == "杭州",
              str((sc2.get("plan") or {}).get("city")))

        # 显式城市参数优先于需求文本
        r = call_plan(proc, {"query": "两天轻松一点", "city": "成都", "days": 1}, 14)
        sc3 = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("显式 city 参数生效（成都）", (sc3.get("plan") or {}).get("city") == "成都",
              str((sc3.get("plan") or {}).get("city")))
        check("显式 days 参数生效（1 天）", len((sc3.get("plan") or {}).get("dayList") or []) == 1,
              str(len((sc3.get("plan") or {}).get("dayList") or [])))

        # A+C：本进程已开过面板 → 再次 plan_trip 不应再开一扇
        r = call_plan(proc, {"query": "苏州2天园林"}, 15)
        sc4 = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("C 兜底：二次 plan_trip 不再开面板", sc4.get("panelOpened") is False,
              json.dumps({k: sc4.get(k) for k in ("panelOpened", "ok")}))
        check("但仍正常返回新行程（苏州）", (sc4.get("plan") or {}).get("city") == "苏州",
              str((sc4.get("plan") or {}).get("city")))

        # 异常输入：识别不出城市 → 明确报错，而不是静默给个默认城市。
        # ⚠️ M5 起城市解析在**云端**（`plan_route_decision`），措辞是
        #    「没能从需求里识别出城市…」；只断言**语义**（ok=false + 给出支持清单），
        #    不锁死字面（措辞是文案，不该成为发布门槛）。
        r = call_plan(proc, {"query": "两天随便玩玩"}, 16)
        sc5 = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("识别不出城市时明确报错（不静默落默认城市）",
              sc5.get("ok") is False and "城市" in (sc5.get("error") or ""),
              json.dumps(sc5, ensure_ascii=False)[:160])

        r = call_plan(proc, {"query": "玩两天", "city": "火星"}, 17)
        sc6 = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("未知城市明确报错（并列出可用城市）",
              sc6.get("ok") is False and "城市" in (sc6.get("error") or "")
              and "杭州" in (sc6.get("error") or ""),
              json.dumps(sc6, ensure_ascii=False)[:160])

        r = rpc(proc, {"jsonrpc": "2.0", "id": 18, "method": "tools/call",
                       "params": {"name": "tripplanner_plan_trip", "arguments": {}}})
        sc7 = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("缺 query 参数时报错而非崩溃", sc7.get("ok") is False, json.dumps(sc7)[:120])

        r = rpc(proc, {"jsonrpc": "2.0", "id": 19, "method": "tools/call",
                       "params": {"name": "no_such_tool", "arguments": {}}})
        check("未知工具返回 JSON-RPC error", bool((r or {}).get("error")),
              str(((r or {}).get("error") or {}).get("code")))
    finally:
        err = shutdown(proc)
    return err


def phase_env_injection():
    """⚠️ M5 语义已变，但这条用例仍然关键：**贫瘠环境下必须还能规划**。

    M1~M4 守的是「`kernel()` 里有没有 `config.apply_env()`」—— 漏了就会**静默降级**
    到离线链路（跑得飞快、结果看着还挺合理，极难发现）。

    M5 起 LLM key 留在**云端**，瘦客户端本地不需要任何密钥。于是断言升级为：
    把 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / **远端配置环境变量**全部抹掉、
    只留宿主会给的 `TRIPPLANNER_HOME`，仍要拿到**非降级**行程。
    —— 等价于「分享包在别人机器上、什么都没配，也能跑」。
    """
    print("\n=== 阶段三：贫瘠环境下仍能规划（M5：密钥在云端）===")
    env = dict(os.environ)
    for k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "MOONSHOT_API_KEY",
              "TRIPPLANNER_REMOTE_BASE", "TRIPPLANNER_REMOTE_AUTH"):
        env.pop(k, None)
    env["TRIPPLANNER_HOME"] = os.path.dirname(HERE)      # 宿主只会给这一个

    proc = spawn(env=env)
    err = ""
    try:
        init(proc)
        r = call_plan(proc, {"query": "杭州玩两天"}, 90)
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        plan = sc.get("plan") or {}
        check("贫瘠环境（无任何 LLM key）下仍能规划", sc.get("ok") is True,
              json.dumps(sc, ensure_ascii=False)[:160])
        check("贫瘠环境下仍是 LLM 链路（证明密钥在云端，本地无需注入）",
              _not_degraded(plan),
              json.dumps({k: plan.get(k) for k in ("mode", "llmEnabled", "degraded")},
                         ensure_ascii=False))
        check("llmEnabled=True（瘦客户端总是请求 llm=1）",
              plan.get("llmEnabled") is True, str(plan.get("llmEnabled")))
    finally:
        err = shutdown(proc)
    return err


def phase_cross_process():
    """⚠️ 跨进程取数回归（2026-10-09 线上踩坑）。

    宿主对 stdio MCP 是**按需 spawn 进程**的（`daemon.log` 里 `pidSpawned` 每次都不同），
    所以「agent 调 plan_trip 的进程」与「面板调 get_plan 的进程」**未必是同一个**。
    last_plan 若只放进程内存，面板就必然读到空 → 渲染不出行程
    （线上现象：面板能开，但行程区渲染不出每天的时间线）。

    这里用两个**独立进程**复现该场景：A 规划落盘 → B 读取。
    ⚠️ M5 起「当前行程」有**两份**：云端会话（面板侧改动的真相）+ 本地磁盘副本
    （跨进程兜底）。`get_plan` 会先 `_sync_plan_from_remote()` 把云端那份拉回来再落盘，
    故本阶段同时守「落盘可跨进程读」与「云端会话能被拉回本地」两条。
    """
    print("\n=== 阶段四：跨进程取数（防 last_plan 进程内存失效）===")
    state = os.path.join(os.path.dirname(HERE), "output", "mcp_last_plan.json")
    # （状态文件由 main() 统一隔离，这里只验证「落盘 + 跨进程可读」）

    # --- 进程 A：规划 ---
    pa = spawn()
    try:
        init(pa)
        r = call_plan(pa, {"query": "杭州两日游"}, 95)
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("进程 A 规划成功", sc.get("ok") is True,
              json.dumps({k: sc.get(k) for k in ("ok",)}, ensure_ascii=False))
    finally:
        shutdown(pa)
    check("行程已落盘（output/mcp_last_plan.json）", os.path.exists(state), state)

    # --- 进程 B：全新进程读取 ---
    pb = spawn()
    err = ""
    try:
        init(pb)
        r = rpc(pb, {"jsonrpc": "2.0", "id": 96, "method": "tools/call",
                     "params": {"name": "tripplanner_get_plan", "arguments": {}}})
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        plan = sc.get("plan") or {}
        dl = plan.get("dayList") or []
        check("进程 B 能跨进程读到行程（落盘生效）",
              sc.get("hasPlan") is True and bool(dl),
              f"hasPlan={sc.get('hasPlan')} dayList={len(dl)}")
        tl0 = (dl[0] or {}).get("timeline") if dl else None
        check("跨进程拿到的 dayList 含 timeline（面板渲染所需）",
              bool(tl0), f"timeline={len(tl0 or [])}")
        poi_photos = 0
        poi_total = 0
        for d in dl:
            for row in (d.get("timeline") or []):
                if row.get("type") == "poi":
                    poi_total += 1
                    if (row.get("photo") or "").strip():
                        poi_photos += 1
        check("跨进程拿到的 poi 带 photo 字段",
              poi_total > 0 and poi_photos > 0, f"{poi_photos}/{poi_total} 有图")
    finally:
        err = shutdown(pb)
    return err


def phase_interactions():
    """阶段五（M2）：面板内交互 —— 换点 / 挪时段 / 导出 Markdown。

    按面板的真实调用序列走：规划 → 取第一天 → 换点 → 拖拽重排 → 导出。
    每条都验**守门是否生效**（换点不得改变当天点数；重排不得丢点）——
    这正是「面板能点」与「点了不把行程搞坏」的区别。
    """
    print()
    print("--- 阶段五：M2 面板交互（换点 / 挪时段 / 导出）---")
    proc = spawn()
    err = ""
    try:
        init(proc)

        # 前置：真实规划
        r = call_plan(proc, {"query": "杭州2天，喜欢自然风光"}, 60)
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        plan = sc.get("plan") or {}
        days = plan.get("dayList") or []
        check("M2 前置：先拿到真实行程", bool(days), f"dayList={len(days)}")
        if not days:
            return err

        city = plan.get("city") or ""
        d0 = days[0]
        day_ids = [s.get("id") for s in (d0.get("timeline") or []) if s.get("type") == "poi"]
        used = [s.get("id") for d in days for s in (d.get("timeline") or [])
                if s.get("type") == "poi"]
        check("第一天有可换的点（≥2）", len(day_ids) >= 2, str(day_ids))
        if len(day_ids) < 2:
            return err

        # ---------------- 换点 ----------------
        r = rpc(proc, {"jsonrpc": "2.0", "id": 61, "method": "tools/call",
                       "params": {"name": "tripplanner_replace_poi",
                                  "arguments": {"city": city, "day": 1,
                                                "poi_id": day_ids[0],
                                                "day_ids": day_ids,
                                                "used_ids": used,
                                                "query": plan.get("query") or ""}}})
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("replace_poi 返回 ok", sc.get("ok") is True,
              json.dumps(sc, ensure_ascii=False)[:160])
        if not sc.get("ok"):
            return err
        sw = sc.get("swap") or {}
        check("换点给出 from/to 且二者不同",
              bool(sw.get("from")) and bool(sw.get("to")) and sw.get("from") != sw.get("to"),
              json.dumps(sw, ensure_ascii=False))

        nplan = sc.get("plan") or {}
        nd0 = (nplan.get("dayList") or [{}])[0]
        ntl = [s.get("id") for s in (nd0.get("timeline") or []) if s.get("type") == "poi"]
        check("换点后当天点数不变（守门生效）", len(ntl) == len(day_ids),
              f"{len(day_ids)} -> {len(ntl)}")
        check("换点后确有替换（点集变化）", set(ntl) != set(day_ids), str(ntl))
        check("换点后无剔除", not nd0.get("dropped"), str(nd0.get("dropped")))
        added = set(ntl) - set(day_ids)
        check("换入的点不在全程已用中（防重复）", not (added & set(used)),
              f"added={sorted(added)}")
        check("换点写回 last_plan（带 updatedAt）", bool(nplan.get("updatedAt")),
              str(nplan.get("updatedAt")))
        # _normalize_day 幂等性：换点后的那天仍要带 photo，否则面板缩略图会掉
        check("换点后 timeline 仍带 photo 字段",
              all("photo" in s for s in (nd0.get("timeline") or [])
                  if s.get("type") == "poi"),
              str([("photo" in s) for s in (nd0.get("timeline") or [])
                   if s.get("type") == "poi"]))

        # ---------------- 挪时段（拖拽重排） ----------------
        # ⚠️ 不能拿「整天倒序」当**唯一**用例：倒序几乎必然让求解层丢点（餐窗 / 闭馆 /
        #    里程），被 `_too_bad` 守门拒绝 —— 拒绝本身是**正确行为**（见 poi_swap
        #    docstring），但那样就测不到成功路径。这里按「扰动从小到大」逐个试，
        #    取第一个被接受的；**全被拒**才算失败。
        cands = []
        if len(ntl) >= 3:
            c = list(ntl)
            c[1], c[2] = c[2], c[1]
            cands.append(c)                                    # 相邻交换
        if len(ntl) >= 2:
            c = list(ntl)
            c[-1], c[-2] = c[-2], c[-1]
            cands.append(c)                                    # 末尾相邻交换
        cands.append(ntl[1:] + ntl[:1])                        # 首点挪到末位
        cands.append(list(reversed(ntl)))                      # 整段倒序（通常被硬约束拒）
        seen, tries = set(), []
        for c in cands:
            k = tuple(c)
            if len(c) == len(ntl) and set(c) == set(ntl) and k not in seen:
                seen.add(k)
                tries.append(c)
        ok_perm, sc_ok, last_err = None, None, ""
        for c in tries:
            r = rpc(proc, {"jsonrpc": "2.0", "id": 62, "method": "tools/call",
                           "params": {"name": "tripplanner_move_poi",
                                      "arguments": {"city": city, "day": 1, "ordered_ids": c,
                                                    "query": plan.get("query") or ""}}})
            sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
            if sc.get("ok"):
                ok_perm, sc_ok = c, sc
                break
            last_err = str(sc.get("error") or "")
        check("move_poi 在候选顺序上成功（成功路径可达）", ok_perm is not None,
              f"顺序={ok_perm}" if ok_perm is not None
              else f"试了 {len(tries)} 种顺序都被拒 -> {last_err[:110]}")
        if ok_perm is not None:
            sc = sc_ok
            md0 = ((sc.get("plan") or {}).get("dayList") or [{}])[0]
            got = [s.get("id") for s in (md0.get("timeline") or [])
                   if s.get("type") == "poi"]
            check("move_poi 回报 order_respected 布尔",
                  isinstance(sc.get("order_respected"), bool), str(sc.get("order_respected")))
            check("重排后点数不变（守门生效）", len(got) == len(ok_perm),
                  f"{len(got)} vs {len(ok_perm)}")
            check("重排后点集与请求一致（只换顺序不丢点）", set(got) == set(ok_perm), str(got))
            if sc.get("order_respected"):
                check("order_respected=True ⇒ 顺序与请求完全一致", got == ok_perm,
                      f"got={got}")
            else:
                # ⚠️ 这里必须走 check()：原先直接 print(OK+…) ⇒ 这行**不计入总数**，
                # 于是「N/N」长期少报 2（2026-10-10 对账时发现）。观测缺陷也是缺陷。
                check("顺序受硬约束微调（order_respected=False，属既有行为）", True)

        # 边界：点集不一致必须拒绝（防拖拽悄悄丢点）
        base_ids = ok_perm if ok_perm is not None else ntl
        r = rpc(proc, {"jsonrpc": "2.0", "id": 63, "method": "tools/call",
                       "params": {"name": "tripplanner_move_poi",
                                  "arguments": {"city": city, "day": 1,
                                                "ordered_ids": base_ids[:-1]}}})
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("点集不一致时拒绝（防悄悄丢点）",
              sc.get("ok") is False and "点集" in (sc.get("error") or ""),
              json.dumps(sc, ensure_ascii=False)[:160])

        # ---------------- 导出 Markdown ----------------
        r = rpc(proc, {"jsonrpc": "2.0", "id": 64, "method": "tools/call",
                       "params": {"name": "tripplanner_export", "arguments": {}}})
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        text = sc.get("text") or ""
        check("export 返回 ok", sc.get("ok") is True,
              json.dumps(sc, ensure_ascii=False)[:120])
        check("导出内容是 Markdown（标题 + 逐日小节）",
              text.startswith("# 【") and "## Day 1" in text, repr(text[:48]))
        check("导出含总里程与多天", "总里程" in text and "## Day 2" in text,
              f"chars={len(text)}")
        check("导出文件名以 .md 结尾",
              str(sc.get("filename") or "").endswith(".md"), str(sc.get("filename")))
        check("导出 mimeType = text/markdown",
              sc.get("mimeType") == "text/markdown", str(sc.get("mimeType")))
        check("导出内容不含 HTML 标签（便于直接粘贴）",
              "<div" not in text and "<span" not in text)

        # 边界：不支持的格式
        r = rpc(proc, {"jsonrpc": "2.0", "id": 65, "method": "tools/call",
                       "params": {"name": "tripplanner_export",
                                  "arguments": {"format": "pdf"}}})
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("不支持的导出格式明确报错", sc.get("ok") is False,
              json.dumps(sc, ensure_ascii=False)[:120])
    finally:
        err = shutdown(proc)
    return err


# ===========================================================================
# 阶段六（M2.1）：本地 HTTP API —— 面板交互的**实际**通路
# ===========================================================================
def _http(url, token=None, body=None, method=None, auth=None, timeout=20):
    """裸 HTTP 调用（自检要能打**云端**，故支持 Authorization 与自定义超时）。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 method=method or ("POST" if data is not None else "GET"))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Tripplanner-Token", token)
    if auth:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _not_degraded(plan: dict) -> bool:
    """是不是**没**降级。

    M5：`mode` 来自云端内核 —— LLM 成功的取值是 `m7_proposal`（**不是** `llm`）。
    判降级只认模式串里的降级标记；沿用 `mode == "llm"` 会把正常 LLM 判成失败
    （自检实测踩过）。与 `tripplanner_mcp_server.run_plan` 里的判定保持同一串关键词。
    """
    mode = str((plan or {}).get("mode") or "")
    return bool(mode) and not any(k in mode.lower() for k in ("degrad", "offline", "fallback"))


# 云端护栏：每 IP 每窗口最多 N 次 `/api/plan`（保护 LLM 预算）。自检会在一分钟内
# 连打十几次真实规划 ⇒ **必然**撞上 —— 那是**正确行为**，不该让发布门槛变成
# 「跑之前先等 5 分钟」。故撞上就等一个窗口再试；**其它错误一律原样返回，绝不吞**。
_RATE_HINTS = ("过于频繁", "429", "rate limit", "too many")


def _is_rate_limited(*payloads) -> bool:
    for p in payloads:
        err = str((p or {}).get("error") or "") if isinstance(p, dict) else ""
        if any(h.lower() in err.lower() for h in _RATE_HINTS):
            return True
    return False


def _await_rate_window(i: int, total: int, wait_s: float = 25.0) -> None:
    print(f"   （云端限流保护命中，等 {wait_s:.0f}s 后重试 {i}/{total - 1}）")
    time.sleep(wait_s)


def _http_retry(url, tries=4, wait_s=25.0, **kw):
    """带限流重试的 `_http`（只对限流重试，其它错误原样返回）。"""
    code, j = _http(url, **kw)
    for i in range(1, tries):
        if not _is_rate_limited(j):
            break
        _await_rate_window(i, tries, wait_s)
        code, j = _http(url, **kw)
    return code, j


def call_plan(proc, args, id_=1, tries=4):
    """`tools/call plan_trip` + 限流重试，返回**原始 JSON-RPC 响应**。

    为什么统一走这里：自检要在一分钟内发起十几次**真实规划**，云端的
    「每 IP 每窗口 N 次」护栏必然被撞上。散在十几个调用点各写一遍重试必然漏，故收口。
    调用方照旧取 `result.structuredContent`，无需改断言。
    """
    r = None
    for i in range(tries):
        r = rpc(proc, {"jsonrpc": "2.0", "id": id_, "method": "tools/call",
                       "params": {"name": "tripplanner_plan_trip", "arguments": args}})
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        if not _is_rate_limited(sc):
            return r
        _await_rate_window(i + 1, tries)
    return r


def phase_remote_backend():
    """阶段六（M5）：**线上后端** —— 面板与工具的唯一通路。

    为什么必须单独验：M5 把 MCP App 变成**瘦客户端** —— 规划 / 换点 / 挪时段 / 导出
    全部委托云端。这条路不通，面板就是个空壳。这里**直接打云端**（base + Basic 凭据
    由 `remote.json` → 环境变量 → `secrets.json` 逐级解析），逐条复核：
    探活 / 鉴权 / 规划（`?shape=mcp` 归一化）/ 会话回读 / 换点 / 挪时段 / 导出 / 城市 / 日志。

    另外做一次「**假装是分享包**」的验收：临时目录里只放 `remote.json`（没有 `src/`
    与 `data/`），规划仍须成功 —— 这正是 M5 存在的理由本身。
    """
    print()
    print("--- 阶段六：M5 线上后端（瘦客户端的唯一通路）---")
    root = os.path.dirname(HERE)
    if root not in sys.path:
        sys.path.insert(0, root)
    import importlib.util
    spec = importlib.util.spec_from_file_location("_tpmcp_remote", SERVER)
    srvmod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srvmod)

    cfg = srvmod.remote_config()
    base = (cfg.get("base") or "").rstrip("/")
    auth = cfg.get("auth") or ""
    sid = srvmod.session_id()
    check("remote_config 解析出线上 base（合法 http/https URL）",
          re.match(r"https?://", base) is not None, base)
    check("remote_config 解析出 Basic 凭据（随分享包分发）",
          str(auth).lower().startswith("basic ") and len(auth) > 12,
          f"len={len(auth)}（0 表示既没 remote.json 也没 secrets.json）")
    check("session_id 非空且跨调用稳定（落盘 / 环境变量）",
          bool(sid) and sid == srvmod.session_id(), sid)
    # M5：原先断言 `_KERNEL is None`（「导入期不加载规划内核」）。M5 起内核**整个
    # 不存在了**，改验更强的**运行时**证据：在一个干净子进程里加载 server 模块，
    # 看 `sys.modules` 里有没有混进 `src` / `ortools`。
    # 为什么这条比 AST 守卫更强：AST 只看「源码里写了什么 import」，而这里看的是
    # 「真跑起来到底加载了什么」—— 间接 import（函数体里 import、`__import__`）也逃不掉。
    # 这正是「打包成 exe 后对方零依赖」的直接判据。
    probe = (
        "import importlib.util,sys,json;"
        "spec=importlib.util.spec_from_file_location('t', %r);"
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
        "print(json.dumps(sorted({n.split('.')[0] for n in sys.modules"
        " if n.split('.')[0] in ('src','ortools')})))" % SERVER
    )
    pr = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                        text=True, encoding="utf-8", errors="replace", cwd=root)
    leaked = (pr.stdout or "").strip()
    check("瘦客户端加载后**运行时**也没引入 src / ortools（零依赖的运行时证据）",
          leaked == "[]", leaked or (pr.stderr or "").strip()[-160:])
    if not base or not auth:
        print(NG + " 线上 base / 凭据缺失，阶段六余下用例无法进行")
        fails.append("线上后端配置缺失（base/auth）")
        return ""

    # ---- 探活 + 鉴权 ----
    code, j = _http(base + "/api/health", auth=auth)
    check("GET /api/health 200 且 ok", code == 200 and j.get("ok") is True,
          f"{code} {json.dumps(j, ensure_ascii=False)[:110]}")
    check("health 回带身份与版本（供版本闸门用）",
          bool(j.get("server")) and bool(j.get("version")),
          f"server={j.get('server')} version={j.get('version')}")
    check("health stage == M5（确认线上跑的是 M5 后端，不是遗留的 M3）",
          j.get("stage") == "M5", str(j.get("stage")))
    # 版本闸门（云端侧）：本地改了 webui 却没同步部署时，这里必须红 —— 否则
    # 面板会用「看着正常」的旧后端，属于最难发现的一类缺陷。
    wv_src = io.open(os.path.join(root, "webui", "server.py"), encoding="utf-8").read()
    m_wv = re.search(r'^WEBUI_VERSION\s*=\s*"([^"]+)"', wv_src, re.M)
    local_wv = m_wv.group(1) if m_wv else ""
    check("云端 WEBUI_VERSION 与本地代码一致（防「改了没部署」）",
          bool(local_wv) and j.get("version") == local_wv,
          f"cloud={j.get('version')} local={local_wv}")
    # 无凭据必须被拒 —— 否则「凭据随包分发」等于没鉴权（nginx Basic 是唯一门禁）
    code, _ = _http(base + "/api/health")
    check("无凭据访问被拒（nginx Basic 生效，401）", code == 401, str(code))

    # ---- 规划：?shape=mcp（**云端**做归一化）----
    psid = sid + "-p"
    q = urllib.parse.urlencode({"shape": "mcp", "query": "杭州2天，喜欢自然风光",
                                "llm": "1", "session": psid})
    code, j = _http_retry(base + "/api/plan?" + q, auth=auth, timeout=300)
    check("云端 /api/plan?shape=mcp 返回 ok",
          code == 200 and j.get("ok") is True,
          f"{code} " + json.dumps({k: j.get(k) for k in ("ok", "error")},
                                  ensure_ascii=False)[:150])
    plan = j.get("plan") or {}
    days = plan.get("dayList") or []
    check("shape=mcp 回的是**已归一化**行程（dayList 非空）", bool(days), f"dayList={len(days)}")
    check("归一化行程带 city / days / totalTravelKm",
          bool(plan.get("city")) and bool(plan.get("days"))
          and isinstance(plan.get("totalTravelKm"), (int, float)),
          f"{plan.get('city')} / {plan.get('days')}天 / {plan.get('totalTravelKm')}km")
    # 探针实测踩过 KeyError('latency_s') —— 瘦客户端摘要要用它，必须随归一化下发
    check("归一化行程带 latency_s（瘦客户端摘要必需）",
          isinstance(plan.get("latency_s"), (int, float)), str(plan.get("latency_s")))
    check("归一化行程带 mode / droppedPois / generatedAt",
          bool(plan.get("mode")) and isinstance(plan.get("droppedPois"), list)
          and bool(plan.get("generatedAt")),
          json.dumps({k: plan.get(k) for k in ("mode", "generatedAt")}, ensure_ascii=False))
    check("云端规划未降级（非 offline/fallback）", _not_degraded(plan), str(plan.get("mode")))

    d0 = days[0] if days else {}
    tl0 = d0.get("timeline") or []
    pois0 = [s for s in tl0 if s.get("type") == "poi"]
    check("day1 的 timeline 含 poi 节点", bool(pois0),
          f"timeline={len(tl0)} poi={len(pois0)}")
    check("poi 带 photo 字段（云端归一化补齐，面板缩略图靠它）",
          any((s.get("photo") or "") for s in pois0),
          f"{sum(1 for s in pois0 if s.get('photo'))}/{len(pois0)} 有图")
    check("poi 带 lat/lng（面板地图靠它）",
          bool(pois0) and all(s.get("lat") is not None and s.get("lng") is not None
                              for s in pois0),
          f"{sum(1 for s in pois0 if s.get('lat') is not None)}/{len(pois0)} 有坐标")

    # ---- 会话回读：带 session、不带 query ⇒ 取回本会话当前那份（不重新规划）----
    code, j = _http(base + "/api/plan?" + urllib.parse.urlencode(
        {"shape": "mcp", "session": psid}), auth=auth)
    lp = j.get("plan") or {}
    check("会话回读：带 session 不带 query ⇒ 取回同一份行程",
          code == 200 and j.get("ok") is True and lp.get("city") == plan.get("city"),
          f"city={lp.get('city')}")
    # 反证「回读没有偷偷重算」：latency_s 是规划时写进会话的，回读会原样带出
    check("会话回读不触发重新规划（latency_s 与原规划一致）",
          lp.get("latency_s") == plan.get("latency_s"),
          f"{lp.get('latency_s')} vs {plan.get('latency_s')}")
    code, j = _http(base + "/api/plan?" + urllib.parse.urlencode(
        {"shape": "mcp", "session": "selftest-nope-%d" % os.getpid()}), auth=auth)
    check("陌生 session 明确报「会话已过期」（不假装成功）",
          code == 200 and j.get("ok") is False and "会话" in (j.get("error") or ""),
          json.dumps(j, ensure_ascii=False)[:120])

    # ---- strict_city：**两侧都测**（M5 新增开关，两边语义必须都能保住）----
    # MCP App 是 agent 面向：需求没提城市必须报错；Web UI 是对话式输入：落默认城市。
    # 用 llm=0 走离线链路，省 LLM 费用（默认城市判定与 llm 无关）。
    code, j = _http_retry(base + "/api/plan?" + urllib.parse.urlencode(
        {"shape": "mcp", "query": "两天随便玩玩", "llm": "0",
         "session": psid + "-strict", "strict_city": "1"}), auth=auth, timeout=180)
    check("strict_city=1：需求没提城市 ⇒ 明确报错（agent 面向，不静默落默认城市）",
          code == 400 and j.get("ok") is False and "城市" in (j.get("error") or ""),
          f"{code} {json.dumps(j, ensure_ascii=False)[:140]}")
    code, j = _http_retry(base + "/api/plan?" + urllib.parse.urlencode(
        {"shape": "mcp", "query": "两天随便玩玩", "llm": "0",
         "session": psid + "-default"}), auth=auth, timeout=180)
    check("不带 strict_city：Web UI 既有行为保住（落默认城市 上海）",
          code == 200 and j.get("ok") is True
          and (j.get("plan") or {}).get("city") == "上海",
          f"{code} city={(j.get('plan') or {}).get('city')}")

    # ---- 换点（POST /api/replace）----
    city = plan.get("city") or ""
    day_ids = [s.get("id") for s in pois0]
    used = [s.get("id") for d in days for s in (d.get("timeline") or [])
            if s.get("type") == "poi"]
    check("第一天有可换的点（≥2）", len(day_ids) >= 2, str(day_ids))
    ntl = []
    if len(day_ids) >= 2:
        code, j = _http(base + "/api/replace", auth=auth, body={
            "city": city, "day": 1, "poi_id": day_ids[0], "day_ids": day_ids,
            "used_ids": used, "query": "杭州2天，喜欢自然风光",
            "session": psid, "plan": plan})
        check("POST /api/replace ok", code == 200 and j.get("ok") is True,
              json.dumps(j, ensure_ascii=False)[:150])
        if j.get("ok"):
            sw = j.get("swap") or {}
            check("换点给出 from/to 且二者不同",
                  bool(sw.get("from")) and bool(sw.get("to")) and sw.get("from") != sw.get("to"),
                  json.dumps(sw, ensure_ascii=False))
            nd0 = j.get("day") or {}
            ntl = [s.get("id") for s in (nd0.get("timeline") or []) if s.get("type") == "poi"]
            check("换点后当天点数不变（守门生效）", len(ntl) == len(day_ids),
                  f"{len(day_ids)} -> {len(ntl)}")
            check("换点后确有替换（点集变化）", set(ntl) != set(day_ids), str(ntl))
            check("换点后无剔除", not nd0.get("dropped"), str(nd0.get("dropped")))
            added = set(ntl) - set(day_ids)
            check("换入的点不在全程已用中（防重复）", not (added & set(used)),
                  f"added={sorted(added)}")
            # 🔴 M5 回归守卫（本阶段真抓到的一条）：换点回的是**内核形状**的 day，
            #    必须由**云端**归一化 —— 瘦客户端没有 photo_cache / POI 库可补。
            #    不补的后果：面板「换一个」之后缩略图与地图点一起消失（静默）。
            check("换点后的 day 已由云端归一化（poi 带 photo 与坐标）",
                  bool(nd0.get("timeline"))
                  and all(s.get("photo") is not None for s in (nd0.get("timeline") or [])
                          if s.get("type") == "poi")
                  and all(s.get("lat") is not None for s in (nd0.get("timeline") or [])
                          if s.get("type") == "poi"),
                  str([(s.get("id"), bool(s.get("photo")), s.get("lat"))
                       for s in (nd0.get("timeline") or []) if s.get("type") == "poi"]))
            code2, j2 = _http(base + "/api/plan?" + urllib.parse.urlencode(
                {"shape": "mcp", "session": psid}), auth=auth)
            got = [s.get("id") for s in
                   ((((j2.get("plan") or {}).get("dayList") or [{}])[0]).get("timeline") or [])
                   if s.get("type") == "poi"]
            check("换点结果已写入云端会话（再拉一次仍是新点集）",
                  set(got) == set(ntl), str(got))

    # ---- 挪时段（POST /api/move）----
    # 同阶段五：倒序几乎必被硬约束判为「会丢点」⇒ 按扰动从小到大试，取第一个被接受的。
    if len(ntl) >= 2:
        cands = []
        if len(ntl) >= 3:
            c = list(ntl)
            c[1], c[2] = c[2], c[1]
            cands.append(c)
        c = list(ntl)
        c[-1], c[-2] = c[-2], c[-1]
        cands.append(c)
        cands.append(ntl[1:] + ntl[:1])
        cands.append(list(reversed(ntl)))
        seen, tries = set(), []
        for c in cands:
            k = tuple(c)
            if len(c) == len(ntl) and set(c) == set(ntl) and k not in seen:
                seen.add(k)
                tries.append(c)
        ok_perm, mv, last_err = None, None, ""
        for c in tries:
            code, j = _http(base + "/api/move", auth=auth, body={
                "city": city, "day": 1, "ordered_ids": c, "day_ids": ntl,
                "query": "杭州2天，喜欢自然风光", "session": psid, "plan": plan})
            if j.get("ok"):
                ok_perm, mv = c, j
                break
            last_err = str(j.get("error") or "")
        check("POST /api/move 在候选顺序上成功（成功路径可达）", ok_perm is not None,
              f"顺序={ok_perm}" if ok_perm is not None
              else f"试了 {len(tries)} 种顺序都被拒 -> {last_err[:110]}")
        if ok_perm is not None:
            j = mv
            md0 = j.get("day") or {}
            got = [s.get("id") for s in (md0.get("timeline") or []) if s.get("type") == "poi"]
            check("挪时段回带 day（含 timeline）", bool(got), f"day={md0.get('day')}")
            check("挪时段后点数不变（守门生效）", len(got) == len(ok_perm),
                  f"{len(got)} vs {len(ok_perm)}")
            check("挪时段后点集与请求一致（只换顺序不丢点）", set(got) == set(ok_perm), str(got))
            check("挪时段回带 order_respected 布尔",
                  isinstance(j.get("order_respected"), bool), str(j.get("order_respected")))
            if j.get("order_respected"):
                check("order_respected=True ⇒ 顺序与请求完全一致", got == ok_perm, f"got={got}")
            else:
                # ⚠️ 这里必须走 check()：原先直接 print(OK+…) ⇒ 这行**不计入总数**，
                # 于是「N/N」长期少报 2（2026-10-10 对账时发现）。观测缺陷也是缺陷。
                check("顺序受硬约束微调（order_respected=False，属既有行为）", True)
            check("挪时段后的 day 已由云端归一化（poi 带 photo）",
                  bool(md0.get("timeline"))
                  and all(s.get("photo") is not None for s in (md0.get("timeline") or [])
                          if s.get("type") == "poi"))
        base_ids = ok_perm if ok_perm is not None else ntl
        code, j = _http(base + "/api/move", auth=auth, body={
            "city": city, "day": 1, "ordered_ids": base_ids[:-1], "day_ids": ntl})
        check("挪时段点集不一致时拒绝（防拖拽悄悄丢点）",
              code == 200 and j.get("ok") is False and "点集" in (j.get("error") or ""),
              json.dumps(j, ensure_ascii=False)[:130])

    # ---- 导出（POST /api/export）----
    code, j = _http(base + "/api/export", auth=auth, body={"plan": plan, "format": "markdown"})
    text = j.get("text") or ""
    check("POST /api/export ok", code == 200 and j.get("ok") is True,
          json.dumps(j, ensure_ascii=False)[:100])
    check("云端导出是 Markdown（标题 + 逐日小节）",
          text.startswith("# 【") and "## Day 1" in text, repr(text[:40]))
    check("云端导出文件名 .md + mimeType=text/markdown",
          str(j.get("filename") or "").endswith(".md")
          and j.get("mimeType") == "text/markdown", str(j.get("filename")))
    # ⚠️ 线上 /api/export 回的是 `text`（**不是** `markdown`）—— 瘦客户端按此对接，
    #    写死防漂移（本项目已有前科：换了键名不报错，只是导不出内容）。
    check("线上导出字段名为 text（瘦客户端按此对接）",
          "text" in j and "markdown" not in j, str(sorted(j)))
    code, j = _http(base + "/api/export", auth=auth, body={"plan": plan, "format": "pdf"})
    check("不支持的导出格式明确报错（400 + ok=false）",
          code == 400 and j.get("ok") is False,
          f"{code} {json.dumps(j, ensure_ascii=False)[:90]}")

    # ---- 城市清单 + 面板轨迹端点 ----
    code, j = _http(base + "/api/cities", auth=auth)
    cinfo = {c.get("name"): c.get("n_pois") for c in (j.get("cities") or [])}
    check("GET /api/cities 返回城市清单", code == 200 and len(cinfo) >= 20,
          f"{code} cities={len(cinfo)}")
    check("城市清单含杭州及其 POI 数", (cinfo.get("杭州") or 0) > 0, str(cinfo.get("杭州")))
    code, j = _http(base + "/api/log", auth=auth,
                    body={"action": "selftest", "detail": "m5 remote backend"})
    check("POST /api/log 接收面板轨迹（面板改走线上后的观测落点）",
          code == 200 and j.get("ok") is True,
          f"{code} {json.dumps(j, ensure_ascii=False)[:80]}")

    # ---- ★分享包模拟：目录里只有 remote.json，没有 src/ 与 data/ ----
    # 这是 M5 存在的理由：把包发给别人，对方机器上既没有 POI 库、也没有 ortools。
    tmp = tempfile.mkdtemp(prefix="tripplanner-share-")
    try:
        with open(os.path.join(tmp, "remote.json"), "w", encoding="utf-8") as f:
            # ⚠️ 凭据取自**运行时解析结果**，不把口令写进仓库
            json.dump({"base": base, "auth": auth}, f, ensure_ascii=False)
        env = dict(os.environ)
        env["TRIPPLANNER_HOME"] = tmp          # project_root() = tmp ⇒ 没有 src/ data/
        env.pop("TRIPPLANNER_REMOTE_BASE", None)
        env.pop("TRIPPLANNER_REMOTE_AUTH", None)
        sc = {}
        for attempt in range(3):      # 撞上限流就等一个窗口重来（见 _http_retry 注释）
            p = spawn(env=env)
            try:
                init(p)
                r = rpc(p, {"jsonrpc": "2.0", "id": 97, "method": "tools/call",
                            "params": {"name": "tripplanner_plan_trip",
                                       "arguments": {"query": "苏州2天园林"}}})
                sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
            finally:
                shutdown(p)
            if sc.get("ok") or not _is_rate_limited(sc):
                break
            _await_rate_window(attempt + 1, 3)
        sh_plan = sc.get("plan") or {}
        sh_days = sh_plan.get("dayList") or []
        check("★分享包模拟：只有 remote.json（无 src/ data/）也能规划",
              sc.get("ok") is True and bool(sh_days),
              json.dumps({k: sc.get(k) for k in ("ok", "error")},
                         ensure_ascii=False)[:160])
        check("★分享包模拟：photo / 坐标仍由云端补齐（客户端不读 data/）",
              any((s.get("photo") or "") and s.get("lat") is not None
                  for d in sh_days for s in (d.get("timeline") or [])
                  if s.get("type") == "poi"),
              str(sh_plan.get("city")))
        check("★分享包模拟：仍是非降级 LLM 链路", _not_degraded(sh_plan),
              str(sh_plan.get("mode")))
        check("分享包模拟：状态落在 TRIPPLANNER_HOME/output（不写回仓库）",
              os.path.exists(os.path.join(tmp, "output", "mcp_last_plan.json")),
              os.path.join(tmp, "output"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 源码级守卫：瘦客户端不许再拉起本地服务 / 不用第三方 HTTP 库 ----
    sv = io.open(SERVER, encoding="utf-8").read()
    check("MCP server 请求路径不再拉起本地常驻 API（无 ensure_local_api 调用点）",
          "ensure_local_api()" not in sv)
    check("规划 / 换点 / 挪时段 / 导出 全部委托云端",
          'remote_request("/api/plan"' in sv and 'remote_request("/api/replace"' in sv
          and 'remote_request("/api/move"' in sv and 'remote_request("/api/export"' in sv)
    check("list_cities 委托云端（不再扫本地 data/）",
          'remote_request("/api/cities"' in sv)
    check("瘦客户端规划带 strict_city=1（agent 面向的「识别不出城市就报错」语义不丢）",
          '"strict_city": "1"' in sv)
    check("HTTP 客户端用 urllib（打包后零第三方依赖）",
          "import urllib.request" in sv and "import requests" not in sv)
    return ""


# ===========================================================================
# 阶段七：M3 —— 面板交互远端化（公共内核单一实现 + 线上后端端点）
# ===========================================================================
def phase_m3_remote():
    print("=== 阶段七：M3 面板交互远端化 ===")
    root = os.path.dirname(HERE)
    if root not in sys.path:
        sys.path.insert(0, root)

    # ---- 1) 公共内核必须是**单一实现**（Web UI 与 MCP 共用，不复制）----
    # 本项目的既有教训：多副本实现必然漂移。M3 让「同一天的处理」同时跑在本地与线上，
    # 所以实现只能有一份 —— 搬进 src/，两边 import。
    pv_path = os.path.join(root, "src", "plan_view.py")
    md_path = os.path.join(root, "src", "md_export.py")
    check("src/plan_view.py 存在（行程视图公共内核）", os.path.exists(pv_path))
    check("src/md_export.py 存在（导出公共内核）", os.path.exists(md_path))

    sv = io.open(SERVER, encoding="utf-8").read()
    wv = io.open(os.path.join(root, "webui", "server.py"), encoding="utf-8").read()
    check("Web UI 引用公共内核（plan_view / md_export）",
          "from src import plan_view" in wv and "from src import md_export" in wv)
    # 🔴 反向守卫（M5「打包分享」的**硬前提**）：瘦客户端的**实际 import** 里不许出现
    #    `src` / `ortools`。一旦出现，PyInstaller 的依赖分析就会把整个内核打进 exe
    #    （体积暴涨 + 可能直接打包失败），而**运行期完全看不出来**。
    # ⚠️ 必须用 AST 判定、不能用字符串匹配：解释「为什么删掉那些壳」的**注释里**就写着
    #    `` `from src import ...` ``，字符串匹配必然误报（「校验脚本自己的正则比被测
    #    对象更危险」这条坑本项目踩过四次）。AST 判的是「真的会不会被 import」。
    _tree = ast.parse(sv)
    mods = set()
    for node in ast.walk(_tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    check("瘦客户端**实际 import** 里没有 src / ortools（打包零依赖的硬前提）",
          "src" not in mods and "ortools" not in mods, str(sorted(mods)))
    check("瘦客户端只用标准库（白名单，保证 exe 里没有第三方包）",
          mods <= {"__future__", "base64", "datetime", "io", "json", "os", "sys",
                   "time", "urllib", "uuid"}, str(sorted(mods)))
    # 反向守卫：渲染实现已搬走，server 里不该再残留关键片段
    check("MCP server 不再内联 Markdown 渲染实现（导出已交云端）",
          "TYPE_ICON = {" not in sv and "_dur_text(" not in sv)

    # ---- 2) 行为等价性：normalize_day 幂等 + apply_day 语义 ----
    from src import plan_view, md_export  # noqa: E402
    day_raw = {
        "day": 1, "theme": "西湖", "reason": "经典线", "weekday": "周一",
        "travel_km": 3.24, "travel_h": 0.4, "finish": "17:30",
        "violations": [], "dropped": [], "reordered": False,
        "timeline": [
            {"type": "poi", "id": "HZ001", "name": "断桥残雪", "start": "09:00", "end": "10:30"},
            {"type": "hop", "name": "苏堤", "start": "10:30", "end": "10:42", "km": 1.2, "min": 8},
        ],
    }
    n1 = plan_view.normalize_day(day_raw, {})
    n2 = plan_view.normalize_day(n1, {})
    check("normalize_day 幂等（换点/重排后不会掉字段）", n1 == n2)
    check("normalize_day 保留 hop 的 km/min",
          n1["timeline"][1].get("km") == 1.2 and n1["timeline"][1].get("min") == 8)

    plan0 = {"city": "杭州", "days": 2, "dayList": [
        {"day": 1, "travel_km": 1.0, "timeline": []},
        {"day": 2, "travel_km": 2.5, "timeline": []},
    ], "totalTravelKm": 3.5}
    out = plan_view.apply_day(plan0, dict(day_raw, day=2))
    check("apply_day 不改原对象（返回新 plan）",
          plan0["dayList"][1].get("travel_km") == 2.5 and plan0["dayList"][1].get("timeline") == [])
    # day1 保留 1.0 + day2 换成归一化后的 3.2（day_raw.travel_km=3.24 → 保留 1 位）= 4.2
    check("apply_day 刷新总里程", out["totalTravelKm"] == 4.2, f"-> {out['totalTravelKm']}")
    check("apply_day 打上 updatedAt", bool(out.get("updatedAt")))

    md = md_export.plan_to_markdown(out)
    check("md_export 产出合法 Markdown（无 HTML 标签）",
          md.startswith("# ") and "<div" not in md and "<span" not in md)
    check("md_export 文件名含城市与天数",
          md_export.export_filename(out).endswith("杭州-2天.md"),
          md_export.export_filename(out))

    # ---- 2.5) mode 口径（2026-10-10 实测暴露的真缺陷）----
    # 导出内核原来只判 `startswith("llm")`，而**线上主路径**是 m7_proposal（不以 llm
    # 开头）⇒ 每一条 M7 行程导出后都标成「离线兜底」。同一个口径在 Web UI(index.html)、
    # 导出内核(md_export)、面板(plan_panel.html) **各写了一遍** ⇒ 必须三处一起验。
    check("md_export：M7 主路径不得标成兜底",
          "兜底" not in md_export.mode_label("m7_proposal")
          and "M7" in md_export.mode_label("m7_proposal"),
          md_export.mode_label("m7_proposal"))
    check("md_export：降级态要标「降级」",
          "降级" in md_export.mode_label("m7_proposal → m7_degraded"),
          md_export.mode_label("m7_proposal → m7_degraded"))
    check("md_export：M2 库内直选不得标成兜底",
          "兜底" not in md_export.mode_label("m2_toptw"),
          md_export.mode_label("m2_toptw"))
    check("md_export：真兜底仍标「离线兜底」",
          "兜底" in md_export.mode_label("offline_fallback"),
          md_export.mode_label("offline_fallback"))
    _md_m7 = md_export.plan_to_markdown(dict(out, mode="m7_proposal"))
    _mode_line = next((ln for ln in _md_m7.splitlines() if ln.startswith("**模式**")), "")
    check("导出的 Markdown「模式」行对 M7 主路径正确（不是兜底）",
          "M7" in _mode_line and "兜底" not in _mode_line, _mode_line[:90])

    # ---- 3) 线上后端端点（面板的远端兜底）----
    for ep in ('"/api/health"', '"/api/move"', '"/api/export"'):
        check(f"Web UI 含端点源码锚点 {ep}", ep in wv)
    check("Web UI 有版本标识（/api/health 会回给面板）", "WEBUI_VERSION" in wv)
    check("Web UI 的 /api/move 复用公共换点内核（poi_swap.reorder_day）",
          "poi_swap.reorder_day" in wv)
    check("Web UI 的 /api/export 复用公共导出内核（md_export.plan_to_markdown）",
          "md_export.plan_to_markdown" in wv)

    # ---- 4) 面板侧：线上后端占位符 + 双形态消费 + **只挂线上后端** ----
    pan = io.open(os.path.join(HERE, "panel", "plan_panel.html"), encoding="utf-8").read()
    check("面板含线上 base / 凭据 / session 占位符（仓库里不留真值）",
          all(p in pan for p in ("__REMOTE_API_BASE__", "__REMOTE_API_AUTH__",
                                 "__SESSION_ID__")))
    check("面板含行程种子占位符（冷启动 / 会话过期时兜底渲染）", "__PLAN_JSON__" in pan)
    check("面板能消费两种返回形态（plan 或 day → 前端写回）",
          "applyDayToPlan" in pan and "applyActionResult" in pan)
    # M5：本地服务已下线 ⇒ BACKENDS 里**只能**有线上那一个。
    # 反向守卫：残留本地后端会让面板去 fetch 一个不存在的服务（表现为「后端不可达」）。
    check("面板后端列表只有线上（M5 本地服务已下线）",
          'name: "线上"' in pan and 'name: "本地"' not in pan)
    check("面板换点 / 挪时段都带 session 与整份 plan（云端会话才能同步）",
          len(re.findall(r"session:\s*SESSION_ID,\s*plan:\s*LAST", pan)) == 2,
          str(len(re.findall(r"session:\s*SESSION_ID,\s*plan:\s*LAST", pan))))
    check("面板拉行程带 shape=mcp + session（否则拿到的是非归一化形状）",
          "shape=mcp" in pan and "&session=" in pan)

    # ---- 5) server 常量：CSP 与线上 base（直接载入模块读常量，不拉进程）----
    import importlib.util  # noqa: E402
    spec = importlib.util.spec_from_file_location("_tpmcp_m3", SERVER)
    srvmod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srvmod)
    conn = srvmod.UI_RESOURCE_META["ui"]["csp"]["connectDomains"]
    # 🔴 真判据：面板要 fetch 的那个 base，其 **host 必须进 connectDomains** ——
    #   漏了就是「fetch 被 CSP 静默拦」，面板只表现为「后端不可达」（最难查的一类）。
    #   故这里拿**运行时解析出的 base** 去比对，而不是写死某个域名。
    _cfg = srvmod.remote_config()
    _host = urllib.parse.urlparse(_cfg.get("base") or "").netloc
    check("CSP connectDomains 放行**运行时解析出的线上 base**（漏＝静默拦）",
          bool(_host) and any(_host in d for d in conn), f"host={_host}")
    check("CSP connectDomains 仍放行高德（JS API 的服务端请求）",
          any("amap.com" in d for d in conn))
    check("CSP resourceDomains 放行高德（地图 JS / 瓦片 / 缩略图加载）",
          any("amap.com" in d
              for d in srvmod.UI_RESOURCE_META["ui"]["csp"]["resourceDomains"]))
    check("CSP frameDomains 未把线上算进去（面板不 iframe 线上页面）",
          bool(_host) and not any(_host in d
                                  for d in srvmod.UI_RESOURCE_META["ui"]["csp"]["frameDomains"]))
    # 2026-10-10：M4 试验期允许 **http + 公网 IP** 当远端 base（为绕开 sslip 域名的
    #   「未备案拦截」）⇒ 断言从「必须 https」放宽为「必须是合法 http(s) URL」。
    #   ⚠️ 混合内容风险改由**宿主实测**把关（见 server 里 REMOTE_API_DOMAINS 注释）。
    check("server 侧有线上 base 常量（合法 http/https URL）",
          re.match(r"https?://", str(srvmod.REMOTE_API_DEFAULT_BASE)) is not None,
          srvmod.REMOTE_API_DEFAULT_BASE)
    check("read_panel 会注入线上 base / 凭据 / session（占位符替换）",
          all(x in sv for x in ("REMOTE_API_BASE_PLACEHOLDER in html",
                                "REMOTE_API_AUTH_PLACEHOLDER", 'cfg["auth"]',
                                "SESSION_ID_PLACEHOLDER in html")))
    check("read_panel 会注入当前行程种子（冷启动 / 会话过期时的兜底渲染源）",
          "PLAN_JSON_PLACEHOLDER in html" in sv)
    return ""


def main() -> int:
    print(f"server: {SERVER}")
    print(f"python: {sys.executable}")
    print("⚠️ M5：阶段二/三/六 会真的请求**云端后端**（需要网络，且消耗 LLM 费用）")
    print()

    # M5：**会话隔离** —— 后端搬到云端后，「本会话的当前行程」存在云端会话表里。
    # 自检若沿用真实会话 id，就会读到（或写脏）真实使用中的那份行程，导致
    # 「无行程时应当如何」这类用例永远失败。故给自检一个独立 session id。
    os.environ["TRIPPLANNER_SESSION_ID"] = "selftest%d" % os.getpid()

    # 状态文件隔离：M1 起 last_plan 是**跨进程落盘**的（output/mcp_last_plan.json），
    # 而本自检有若干「无行程时应当如何」的用例，需要一个干净起点。
    # 这里先把已有状态挪开，跑完恢复 —— 既不污染真实状态，也不用改断言。
    state = os.path.join(os.path.dirname(HERE), "output", "mcp_last_plan.json")
    backup = state + ".selftest-bak"
    had = os.path.exists(state)
    if had:
        os.replace(state, backup)
    try:
        err1 = phase_protocol()
        err2 = phase_planning()
        err3 = phase_env_injection()
        err4 = phase_cross_process()
        err5 = phase_interactions()
        err6 = phase_remote_backend()      # M5：原 phase_local_api（本地服务已下线）
        err7 = phase_m3_remote()
    finally:
        if had and os.path.exists(backup):
            os.replace(backup, state)
        elif os.path.exists(state):
            os.remove(state)          # 清掉自检自身产生的状态
        if os.path.exists(backup):
            os.remove(backup)

    err = ((err1 or "") + (err2 or "") + (err3 or "") + (err4 or "")
           + (err5 or "") + (err6 or "") + (err7 or ""))
    print()
    print("--- stderr 末 12 行（server 日志，确认没污染 stdout）---")
    for ln in (err or "").strip().splitlines()[-12:]:
        print("   " + ln)
    check("stdout 未被日志污染（上面每条响应都解析成功）", not fails)

    print()
    if fails:
        print(f"[FAIL] {len(fails)} 项未通过（共 {passed[0] + len(fails)} 项）：")
        for f in fails:
            print("   - " + f)
        return 1
    print(f"[PASS] {passed[0]}/{passed[0]} 项全部通过 —— "
          "协议 / M1 规划 / M2 交互 / M5 瘦客户端+线上后端 均正确，"
          "可以交给 WorkBuddy 信任了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
