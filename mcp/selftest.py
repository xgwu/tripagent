#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MCP server 本地自检 —— 不开 WorkBuddy 也能验协议（把 server 当子进程拉起，走真实 stdio）

用法：
    D:\\Users\\xgwu\\.workbuddy\\binaries\\python\\envs\\tripagent\\Scripts\\python.exe mcp/selftest.py

验的是：initialize 协商、tools/list（含面板标记）、resources/read（含 _meta.ui.csp 与地图 key / 本地 API 注入）、
tools/call（全量 9 个工具，含 M1 真实规划 + M2 换点/挪时段/导出）、A+C 面板分流、
跨进程取数、M2.1 本地 HTTP API（health/plan/replace/move/export + token 校验），
以及「stdout 是否被日志污染」。

⚠️ M1/M2/M2.1 的规划用例会真的跑一次规划内核（LLM 可用时约 3-6s，不可用则走离线兜底）。
⚠️ 阶段六会**真的拉起** `mcp/local_api.py`（detached 常驻进程，端口池 51900 起）。
   它是幂等的（已有实例活着则新进程直接退出），跑完不主动杀 —— 生产路径同样靠它。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "tripplanner_mcp_server.py")

OK = "  [PASS]"
NG = "  [FAIL]"
fails = []


def check(label, cond, detail=""):
    print(f"{OK if cond else NG} {label}" + (f"  -> {detail}" if detail else ""))
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

        # ---- M2.1：本地 HTTP API（面板交互的实际通路） ----
        # 背景：宿主 tools/call 通道已实测不通（面板 ping 10s 超时）。
        check("面板含本地 API 客户端（probeApi + 五个端点）",
              all(k in html for k in ("probeApi", "/api/health", "/api/plan",
                                      "/api/replace", "/api/move", "/api/export")))
        check("面板保留宿主通道为兜底（tools/call 仍在）", "tools/call" in html)
        check("panel 文件用本地 API 占位符（真实端口/token 不入库）",
              "__LOCAL_API_BASE__" in raw_panel and "__LOCAL_API_TOKEN__" in raw_panel)
        m_api = re.search(r'var\s+API_BASE\s*=\s*"(http://127\.0\.0\.1:\d+)"', html)
        check("resources/read 已注入本地 API base（真实端口）",
              bool(m_api), m_api.group(1) if m_api else "仍是占位符")
        m_tok = re.search(r'var\s+API_TOKEN\s*=\s*"([0-9a-f]{16,})"', html)
        check("resources/read 已注入本地 API token",
              bool(m_tok), f"len={len(m_tok.group(1))}" if m_tok else "未注入")

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
        r = rpc(proc, {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                       "params": {"name": "tripplanner_plan_trip",
                                  "arguments": {"query": "杭州2天，带8岁孩子，想看西湖和灵隐寺"}}})
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
        check("LLM 未静默降级（mode=llm 且 degraded=false）",
              plan.get("mode") == "llm" and plan.get("degraded") is False,
              json.dumps({k: plan.get(k) for k in ("mode", "llmEnabled", "degraded")}))
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
        r = rpc(proc, {"jsonrpc": "2.0", "id": 14, "method": "tools/call",
                       "params": {"name": "tripplanner_plan_trip",
                                  "arguments": {"query": "两天轻松一点", "city": "成都", "days": 1}}})
        sc3 = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("显式 city 参数生效（成都）", (sc3.get("plan") or {}).get("city") == "成都",
              str((sc3.get("plan") or {}).get("city")))
        check("显式 days 参数生效（1 天）", len((sc3.get("plan") or {}).get("dayList") or []) == 1,
              str(len((sc3.get("plan") or {}).get("dayList") or [])))

        # A+C：本进程已开过面板 → 再次 plan_trip 不应再开一扇
        r = rpc(proc, {"jsonrpc": "2.0", "id": 15, "method": "tools/call",
                       "params": {"name": "tripplanner_plan_trip",
                                  "arguments": {"query": "苏州2天园林"}}})
        sc4 = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("C 兜底：二次 plan_trip 不再开面板", sc4.get("panelOpened") is False,
              json.dumps({k: sc4.get(k) for k in ("panelOpened", "ok")}))
        check("但仍正常返回新行程（苏州）", (sc4.get("plan") or {}).get("city") == "苏州",
              str((sc4.get("plan") or {}).get("city")))

        # 异常输入：识别不出城市 → 明确报错，而不是静默给个默认城市
        r = rpc(proc, {"jsonrpc": "2.0", "id": 16, "method": "tools/call",
                       "params": {"name": "tripplanner_plan_trip",
                                  "arguments": {"query": "两天随便玩玩"}}})
        sc5 = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("识别不出城市时明确报错", sc5.get("ok") is False and bool(sc5.get("error")),
              json.dumps(sc5, ensure_ascii=False)[:160])

        r = rpc(proc, {"jsonrpc": "2.0", "id": 17, "method": "tools/call",
                       "params": {"name": "tripplanner_plan_trip",
                                  "arguments": {"query": "玩两天", "city": "火星"}}})
        sc6 = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("未知城市明确报错", sc6.get("ok") is False and "未知城市" in (sc6.get("error") or ""),
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
    """⚠️ 最关键的一条回归：宿主拉起本进程时 env 里只有 TRIPPLANNER_HOME。

    若 kernel() 里没调 config.apply_env()，llm_client 就只读 os.environ 拿不到 key
    → **静默降级**到离线链路：跑得飞快、结果看着也合理，极难发现。
    这里把 DEEPSEEK_API_KEY / OPENAI_API_KEY 从环境里抹掉再跑一次规划，
    断言仍然是 llm 模式 —— 等于提前复现 WorkBuddy 里的真实环境。
    """
    print("\n=== 阶段三：贫瘠环境下的 LLM 注入（防静默降级）===")
    env = dict(os.environ)
    for k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        env.pop(k, None)
    env["TRIPPLANNER_HOME"] = os.path.dirname(HERE)      # 宿主只会给这一个

    proc = spawn(env=env)
    err = ""
    try:
        init(proc)
        r = rpc(proc, {"jsonrpc": "2.0", "id": 90, "method": "tools/call",
                       "params": {"name": "tripplanner_plan_trip",
                                  "arguments": {"query": "杭州玩两天"}}})
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        plan = sc.get("plan") or {}
        check("抹掉 DEEPSEEK_API_KEY 后仍能规划", sc.get("ok") is True,
              json.dumps(sc, ensure_ascii=False)[:160])
        check("贫瘠环境下仍走 LLM（证明 apply_env 生效）",
              plan.get("mode") == "llm",
              json.dumps({k: plan.get(k) for k in ("mode", "llmEnabled", "degraded")},
                         ensure_ascii=False))
        # 反证：若真没 key，llmEnabled 会是 False —— 这里必须是 True
        check("llmEnabled=True（config.json 的 key 被注入到环境）",
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
    """
    print("\n=== 阶段四：跨进程取数（防 last_plan 进程内存失效）===")
    state = os.path.join(os.path.dirname(HERE), "output", "mcp_last_plan.json")
    # （状态文件由 main() 统一隔离，这里只验证「落盘 + 跨进程可读」）

    # --- 进程 A：规划 ---
    pa = spawn()
    try:
        init(pa)
        r = rpc(pa, {"jsonrpc": "2.0", "id": 95, "method": "tools/call",
                     "params": {"name": "tripplanner_plan_trip",
                                "arguments": {"query": "杭州两日游"}}})
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
        r = rpc(proc, {"jsonrpc": "2.0", "id": 60, "method": "tools/call",
                       "params": {"name": "tripplanner_plan_trip",
                                  "arguments": {"query": "杭州2天，喜欢自然风光"}}})
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
        rev = list(reversed(ntl))
        r = rpc(proc, {"jsonrpc": "2.0", "id": 62, "method": "tools/call",
                       "params": {"name": "tripplanner_move_poi",
                                  "arguments": {"city": city, "day": 1, "ordered_ids": rev,
                                                "query": plan.get("query") or ""}}})
        sc = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("move_poi 返回 ok", sc.get("ok") is True,
              json.dumps(sc, ensure_ascii=False)[:160])
        if sc.get("ok"):
            md0 = ((sc.get("plan") or {}).get("dayList") or [{}])[0]
            got = [s.get("id") for s in (md0.get("timeline") or [])
                   if s.get("type") == "poi"]
            check("重排后顺序与请求一致", got == rev,
                  f"order_respected={sc.get('order_respected')} got={got}")
            check("重排后点数不变", len(got) == len(rev), f"{len(got)} vs {len(rev)}")

        # 边界：点集不一致必须拒绝（防拖拽悄悄丢点）
        r = rpc(proc, {"jsonrpc": "2.0", "id": 63, "method": "tools/call",
                       "params": {"name": "tripplanner_move_poi",
                                  "arguments": {"city": city, "day": 1,
                                                "ordered_ids": rev[:-1]}}})
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
def _http(url, token=None, body=None, method=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 method=method or ("POST" if data is not None else "GET"))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Tripplanner-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"ok": False, "error": f"{type(e).__name__}: {e}"}


def phase_local_api():
    """阶段六（M2.1）：本地 HTTP API。

    为什么必须单独验：面板 → 宿主 → server 的 tools/call 通道在本机**实测不通**
    （宿主 mcp-apps-diag.log 有 rpc.callTool / proxyAppToolCall.ok，但请求从未到达
    任何 server 进程；面板里点 ping 是 10s 超时）。面板交互已改走 127.0.0.1 的本地
    HTTP —— 这条通路若不复测，「M2 全绿」就是个假象（M2 当时正是这个假象）。
    """
    print()
    print("--- 阶段六：M2.1 本地 HTTP API ---")
    proc = spawn()
    err = ""
    # 「还没有行程时应当如何」的用例需要干净起点：main() 只在最外面挪过一次，
    # 而阶段二~五都会真的生成行程并落盘。这里自己挪开、跑完原样放回。
    state6 = os.path.join(os.path.dirname(HERE), "output", "mcp_last_plan.json")
    backup6 = state6 + ".phase6-bak"
    moved6 = os.path.exists(state6)
    if moved6:
        os.replace(state6, backup6)
    try:
        r0 = init(proc)
        srv_ver = (((r0 or {}).get("result") or {}).get("serverInfo") or {}).get("version")
        r = rpc(proc, {"jsonrpc": "2.0", "id": 70, "method": "tools/list"})
        tools = ((r or {}).get("result") or {}).get("tools") or []
        by = {t["name"]: t for t in tools}
        panel_uri = ((((by.get("tripplanner_plan_trip") or {}).get("_meta") or {})
                      .get("ui") or {}).get("resourceUri")) or ""

        # resources/read 会 ensure_local_api()：拉起（或复用）常驻进程并注入 base + token
        r = rpc(proc, {"jsonrpc": "2.0", "id": 71, "method": "resources/read",
                       "params": {"uri": panel_uri}})
        html = ((((r or {}).get("result") or {}).get("contents") or [{}])[0].get("text") or "")
        m_api = re.search(r'var\s+API_BASE\s*=\s*"(http://127\.0\.0\.1:\d+)"', html)
        m_tok = re.search(r'var\s+API_TOKEN\s*=\s*"([0-9a-f]{16,})"', html)
        check("前置：resources/read 已注入本地 API base", bool(m_api),
              m_api.group(1) if m_api else "未注入（ensure_local_api 失败）")
        if not m_api:
            return err
        base = m_api.group(1)
        tok = m_tok.group(1) if m_tok else ""
        check("前置：已注入本地 API token", bool(tok), f"len={len(tok)}")

        # ---- 探活（health 刻意不校验 token）----
        code, j = _http(base + "/api/health")
        check("GET /api/health 200 且 ok", code == 200 and j.get("ok") is True,
              f"{code} {json.dumps(j, ensure_ascii=False)[:90]}")
        check("health 回带身份与版本（供版本闸门用）",
              j.get("server") == "tripplanner" and bool(j.get("version")),
              f"server={j.get('server')} version={j.get('version')}")
        # 常驻进程的版本必须跟当前 server 版本一致 —— 否则「改了代码没生效」且不报错。
        check("常驻进程版本 == server 版本（版本闸门）",
              j.get("version") == srv_ver,
              f"api={j.get('version')} server={srv_ver}")

        # ---- token 校验 ----
        code, j = _http(base + "/api/plan")
        check("无 token 访问 /api/plan 被拒（403）", code == 403, f"{code} {j.get('error')}")
        code, j = _http(base + "/api/plan", token=tok)
        check("带 token 访问 /api/plan 通过", code == 200 and j.get("ok") is True, str(code))
        check("无行程时 hasPlan=False（自检已隔离状态文件）", j.get("hasPlan") is False,
              str(j.get("hasPlan")))
        code, j = _http(base + "/api/export", token=tok, body={"format": "markdown"})
        check("无行程时导出明确报错（200 + ok=false，不是 500）",
              code == 200 and j.get("ok") is False and "行程" in (j.get("error") or ""),
              json.dumps(j, ensure_ascii=False)[:110])

        # ---- 先经 MCP 工具生成行程，再验本地 API 能读到同一份（跨进程共享磁盘状态）----
        r = rpc(proc, {"jsonrpc": "2.0", "id": 72, "method": "tools/call",
                       "params": {"name": "tripplanner_plan_trip",
                                  "arguments": {"query": "杭州2天，喜欢自然风光"}}})
        plan = (((r or {}).get("result") or {}).get("structuredContent") or {}).get("plan") or {}
        days = plan.get("dayList") or []
        check("前置：行程已生成并落盘", bool(days), f"dayList={len(days)}")
        if not days:
            return err

        code, j = _http(base + "/api/plan", token=tok)
        lp = j.get("plan") or {}
        check("本地 API 读到同一份行程（共享 output/mcp_last_plan.json）",
              j.get("hasPlan") is True and lp.get("city") == plan.get("city"),
              f"hasPlan={j.get('hasPlan')} city={lp.get('city')}")

        city = plan.get("city") or ""
        d0 = days[0]
        day_ids = [s.get("id") for s in (d0.get("timeline") or []) if s.get("type") == "poi"]
        used = [s.get("id") for d in days for s in (d.get("timeline") or [])
                if s.get("type") == "poi"]
        check("第一天有可换的点（≥2）", len(day_ids) >= 2, str(day_ids))
        if len(day_ids) < 2:
            return err

        # ---- 换点（HTTP）----
        code, j = _http(base + "/api/replace", token=tok, body={
            "city": city, "day": 1, "poi_id": day_ids[0], "day_ids": day_ids,
            "used_ids": used, "query": plan.get("query") or ""})
        check("POST /api/replace ok", code == 200 and j.get("ok") is True,
              json.dumps(j, ensure_ascii=False)[:140])
        if not j.get("ok"):
            return err
        nplan = j.get("plan") or {}
        nd0 = (nplan.get("dayList") or [{}])[0]
        ntl = [s.get("id") for s in (nd0.get("timeline") or []) if s.get("type") == "poi"]
        check("HTTP 换点后点数不变（守门经 HTTP 仍生效）",
              len(ntl) == len(day_ids), f"{len(day_ids)} -> {len(ntl)}")
        check("HTTP 换点后确有替换（点集变化）", set(ntl) != set(day_ids), str(ntl))
        check("HTTP 换点写回整个行程（带 updatedAt）", bool(nplan.get("updatedAt")))
        code2, j2 = _http(base + "/api/plan", token=tok)
        got = [s.get("id") for s in ((((j2.get("plan") or {}).get("dayList") or [{}])[0])
                                     .get("timeline") or []) if s.get("type") == "poi"]
        check("换点结果已落盘（再 GET 一次仍可见）", got == ntl, str(got))

        # ---- 挪时段（HTTP）----
        rev = list(reversed(ntl))
        code, j = _http(base + "/api/move", token=tok, body={
            "city": city, "day": 1, "ordered_ids": rev,
            "query": plan.get("query") or ""})
        check("POST /api/move ok", code == 200 and j.get("ok") is True,
              json.dumps(j, ensure_ascii=False)[:140])
        if j.get("ok"):
            md0 = ((j.get("plan") or {}).get("dayList") or [{}])[0]
            got = [s.get("id") for s in (md0.get("timeline") or []) if s.get("type") == "poi"]
            check("HTTP 重排后顺序与请求一致", got == rev,
                  f"order_respected={j.get('order_respected')} got={got}")
            check("HTTP 重排后点数不变", len(got) == len(rev), f"{len(got)} vs {len(rev)}")
        code, j = _http(base + "/api/move", token=tok, body={
            "city": city, "day": 1, "ordered_ids": rev[:-1]})
        check("HTTP 重排点集不一致时拒绝（防拖拽悄悄丢点）",
              code == 200 and j.get("ok") is False and "点集" in (j.get("error") or ""),
              json.dumps(j, ensure_ascii=False)[:120])

        # ---- 导出（HTTP）----
        code, j = _http(base + "/api/export", token=tok, body={"format": "markdown"})
        text = j.get("text") or ""
        check("POST /api/export ok", code == 200 and j.get("ok") is True,
              json.dumps(j, ensure_ascii=False)[:90])
        check("HTTP 导出是 Markdown（标题 + 逐日小节）",
              text.startswith("# 【") and "## Day 1" in text, repr(text[:40]))
        check("HTTP 导出文件名 .md + mimeType=text/markdown",
              str(j.get("filename") or "").endswith(".md")
              and j.get("mimeType") == "text/markdown", str(j.get("filename")))
        code, j = _http(base + "/api/export", token=tok, body={"format": "pdf"})
        check("不支持的导出格式明确报错",
              code == 200 and j.get("ok") is False, json.dumps(j, ensure_ascii=False)[:90])

        code, j = _http(base + "/api/nope", token=tok)
        check("未知路径 404", code == 404, f"{code} {j.get('error')}")
    finally:
        if os.path.exists(state6):
            os.remove(state6)          # 本阶段自己生成的行程，别留给收尾逻辑
        if moved6:
            os.replace(backup6, state6)
        err = shutdown(proc)
    return err


def main() -> int:
    print(f"server: {SERVER}")
    print(f"python: {sys.executable}")
    print()

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
        err6 = phase_local_api()
    finally:
        if had and os.path.exists(backup):
            os.replace(backup, state)
        elif os.path.exists(state):
            os.remove(state)          # 清掉自检自身产生的状态
        if os.path.exists(backup):
            os.remove(backup)

    err = (err1 or "") + (err2 or "") + (err3 or "") + (err4 or "") + (err5 or "") + (err6 or "")
    print()
    print("--- stderr 末 12 行（server 日志，确认没污染 stdout）---")
    for ln in (err or "").strip().splitlines()[-12:]:
        print("   " + ln)
    check("stdout 未被日志污染（上面每条响应都解析成功）", not fails)

    print()
    if fails:
        print(f"[FAIL] {len(fails)} 项未通过：")
        for f in fails:
            print("   - " + f)
        return 1
    print("[PASS] 全部通过 —— 协议 / M1 规划 / M2 交互 / M2.1 本地 API 均正确，可以交给 WorkBuddy 信任了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
