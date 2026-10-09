#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MCP server 本地自检 —— 不开 WorkBuddy 也能验协议（把 server 当子进程拉起，走真实 stdio）

用法：
    D:\\Users\\xgwu\\.workbuddy\\binaries\\python\\envs\\tripagent\\Scripts\\python.exe mcp/selftest.py

验的是：initialize 协商、tools/list（含面板标记）、resources/read（含 _meta.ui.csp）、
tools/call（两个工具），以及「stdout 是否被日志污染」。
"""
from __future__ import annotations

import io
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


def main() -> int:
    print(f"server: {SERVER}")
    print(f"python: {sys.executable}")
    print()

    proc = subprocess.Popen(
        [sys.executable, SERVER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
    )

    try:
        r = rpc(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18",
                       "capabilities": {},
                       "clientInfo": {"name": "selftest", "version": "0"}},
        })
        check("initialize 有响应", r is not None, json.dumps(r, ensure_ascii=False)[:120])
        info = ((r or {}).get("result") or {}).get("serverInfo") or {}
        check("serverInfo.name == tripplanner", info.get("name") == "tripplanner", str(info))
        check("协商回 protocolVersion", ((r or {}).get("result") or {}).get("protocolVersion") == "2025-06-18")

        notify(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        # 通知不该有回复 —— 用一条 ping 请求当「栅栏」，能正常拿到回复即说明前面的通知没有产生多余输出
        r = rpc(proc, {"jsonrpc": "2.0", "id": 99, "method": "ping"})
        check("initialized 通知不产生多余回复（栅栏 ping 正常）",
              r is not None and r.get("id") == 99, json.dumps(r, ensure_ascii=False)[:80])

        r = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = ((r or {}).get("result") or {}).get("tools") or []
        names = [t["name"] for t in tools]
        check("tools/list 返回 3 个工具", len(tools) == 3, str(names))

        by = {t["name"]: t for t in tools}
        open_meta = (by.get("tripplanner_open_plan") or {}).get("_meta") or {}
        uri = ((open_meta.get("ui") or {}).get("resourceUri"))
        check("open_plan 带 _meta.ui.resourceUri", uri == "ui://tripplanner/plan", repr(uri))
        vis = ((open_meta.get("ui") or {}).get("visibility"))
        check("visibility == [model, app]", vis == ["model", "app"], repr(vis))
        ping_meta = (by.get("tripplanner_ping") or {}).get("_meta")
        check("ping 不带 _meta（对照组）", not ping_meta, repr(ping_meta))
        check("panel_ready 工具存在且不带面板",
              "tripplanner_panel_ready" in by
              and not (by.get("tripplanner_panel_ready") or {}).get("_meta"),
              str(sorted(by.keys())))

        r = rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        res = ((r or {}).get("result") or {}).get("resources") or []
        check("resources/list 1 条", len(res) == 1)
        check("资源 mimeType = text/html;profile=mcp-app",
              res and res[0].get("mimeType") == "text/html;profile=mcp-app",
              str(res[0].get("mimeType") if res else None))

        r = rpc(proc, {"jsonrpc": "2.0", "id": 4, "method": "resources/read",
                       "params": {"uri": "ui://tripplanner/plan"}})
        contents = ((r or {}).get("result") or {}).get("contents") or []
        c0 = contents[0] if contents else {}
        html = c0.get("text") or ""
        nbytes = len(html.encode("utf-8"))
        check("resources/read 返回 HTML", html.startswith("<!DOCTYPE html") and len(html) > 1000,
              f"{nbytes} bytes")
        check("HTML 在 256KiB 预取阈值内", nbytes < 256 * 1024, f"{nbytes} < 262144")
        check("contents[0] 带 _meta.ui.csp.frameDomains",
              "frameDomains" in json.dumps(c0.get("_meta") or {}, ensure_ascii=False),
              json.dumps((c0.get("_meta") or {}).get("ui", {}).get("csp", {}), ensure_ascii=False)[:100])
        csp = ((c0.get("_meta") or {}).get("ui", {}) or {}).get("csp", {}) or {}
        rdom = csp.get("resourceDomains") or []
        check("resourceDomains 放行高德图片域名",
              any("autonavi.com" in str(d) for d in rdom),
              json.dumps(rdom, ensure_ascii=False)[:140])
        check("面板零外部引用（无 src=/href= http）",
              ("src=\"http" not in html) and ("href=\"http" not in html))
        check("面板含 A 方案上报（tripplanner_panel_ready）",
              "tripplanner_panel_ready" in html)
        check("面板含 appInstanceId 提取逻辑",
              "appInstanceId" in html)

        r = rpc(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "tripplanner_open_plan", "arguments": {}}})
        rc = (r or {}).get("result") or {}
        check("open_plan 调用有 content",
              bool(rc.get("content")) and rc["content"][0].get("type") == "text",
              (rc.get("content") or [{}])[0].get("text", "")[:40])
        check("open_plan 有 structuredContent", isinstance(rc.get("structuredContent"), dict))
        check("首次 open_plan 判定为开面板",
              (rc.get("structuredContent") or {}).get("panelOpened") is True,
              json.dumps(rc.get("structuredContent"), ensure_ascii=False)[:120])

        # ---- A+C 分流：C 兜底（不开新面板） ----
        r = rpc(proc, {"jsonrpc": "2.0", "id": 51, "method": "tools/call",
                       "params": {"name": "tripplanner_open_plan", "arguments": {}}})
        sc2 = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("C 兜底：二次 open_plan 不再开面板",
              sc2.get("panelOpened") is False and sc2.get("reason") == "panel-already-open",
              json.dumps(sc2, ensure_ascii=False)[:140])

        # ---- A 方案：面板自报实例 ----
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
              sr2.get("count") == 2 and sorted(sr2.get("knownInstances") or []) == ["inst-aaa", "inst-bbb"],
              json.dumps(sr2, ensure_ascii=False)[:140])

        # 已登记的实例再调 open_plan → 精确判据命中（而非只靠 C 兜底）
        r = rpc(proc, {"jsonrpc": "2.0", "id": 54, "method": "tools/call",
                       "params": {"name": "tripplanner_open_plan",
                                  "arguments": {"app_instance_id": "inst-aaa"}}})
        sc3 = ((r or {}).get("result") or {}).get("structuredContent") or {}
        check("A 判据：已登记实例不再开面板",
              sc3.get("panelOpened") is False
              and "inst-aaa" in (sc3.get("knownInstances") or []),
              json.dumps(sc3, ensure_ascii=False)[:140])

        r = rpc(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                       "params": {"name": "tripplanner_ping", "arguments": {"note": "自检回显"}}})
        rp = (r or {}).get("result") or {}
        sc = rp.get("structuredContent") or {}
        check("ping 回显参数正确", sc.get("note") == "自检回显", json.dumps(sc, ensure_ascii=False))

        r = rpc(proc, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": "no_such_tool", "arguments": {}}})
        check("未知工具返回 JSON-RPC error", bool((r or {}).get("error")),
              str(((r or {}).get("error") or {}).get("code")))

        r = rpc(proc, {"jsonrpc": "2.0", "id": 8, "method": "no/such/method"})
        check("未知方法返回 -32601", ((r or {}).get("error") or {}).get("code") == -32601)

    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            err = proc.stderr.read()
        except Exception:
            err = ""
        proc.wait(timeout=10)

    print()
    print("--- stderr（server 日志，确认没污染 stdout）---")
    for ln in (err or "").strip().splitlines()[-12:]:
        print("   " + ln)
    check("stdout 未被日志污染（上面每条都解析成功）", not fails)

    print()
    if fails:
        print(f"[FAIL] {len(fails)} 项未通过：")
        for f in fails:
            print("   - " + f)
        return 1
    print("[PASS] 全部通过 —— server 侧协议正确，可以交给 WorkBuddy 信任了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
