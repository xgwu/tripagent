#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MCP server 本地自检 —— 不开 WorkBuddy 也能验协议（把 server 当子进程拉起，走真实 stdio）

用法：
    D:\\Users\\xgwu\\.workbuddy\\binaries\\python\\envs\\tripagent\\Scripts\\python.exe mcp/selftest.py

验的是：initialize 协商、tools/list（含面板标记）、resources/read（含 _meta.ui.csp）、
tools/call（全量工具，含 M1 真实规划）、A+C 面板分流，以及「stdout 是否被日志污染」。

⚠️ M1 的规划用例会真的跑一次规划内核（LLM 可用时约 3-6s，不可用则走离线兜底）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

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
        check("tools/list 返回 6 个工具", len(tools) == 6, str(names))
        by = {t["name"]: t for t in tools}

        # 带面板：plan_trip / open_plan
        for tname in ("tripplanner_plan_trip", "tripplanner_open_plan"):
            meta = ((by.get(tname) or {}).get("_meta") or {}).get("ui") or {}
            check(f"{tname} 带 _meta.ui.resourceUri",
                  meta.get("resourceUri") == "ui://tripplanner/plan-m1", repr(meta.get("resourceUri")))
            check(f"{tname} visibility == [model, app]",
                  meta.get("visibility") == ["model", "app"], repr(meta.get("visibility")))

        # 不带面板：其余四个（若误配会每次调用都堆一扇面板）
        for tname in ("tripplanner_get_plan", "tripplanner_list_cities",
                      "tripplanner_ping", "tripplanner_panel_ready"):
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
                       "params": {"uri": "ui://tripplanner/plan-m1"}})
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
        check("面板零外部引用（无 src=/href= http）",
              ("src=\"http" not in html) and ("href=\"http" not in html))
        check("面板含 A 方案上报（tripplanner_panel_ready）", "tripplanner_panel_ready" in html)
        check("面板含 appInstanceId 提取逻辑", "appInstanceId" in html)
        check("面板含 M1 数据通道（get_plan）", "tripplanner_get_plan" in html)
        check("面板渲染时间轴（timeline）", "timeline" in html)

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


def main() -> int:
    print(f"server: {SERVER}")
    print(f"python: {sys.executable}")
    print()

    err1 = phase_protocol()
    err2 = phase_planning()
    err3 = phase_env_injection()

    err = (err1 or "") + (err2 or "") + (err3 or "")
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
    print("[PASS] 全部通过 —— server 侧协议与 M1 规划均正确，可以交给 WorkBuddy 信任了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
