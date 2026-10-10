# -*- coding: utf-8 -*-
"""M6.3 守卫：**面板有且只有一个入口，且同一对话只开一次**。

背景（2026-10-10 报障）：
    用户反复重拍行程时，对话里**叠了好几扇面板**。
根因（从宿主 app.asar 源码取证，不是推测）：
    `McpAppsGateway.handleAgentToolCall()` 的去重键是
        `${sessionId}::${toolCallId}`
    即 **每一次工具调用 = 新开一扇面板**；宿主只按「工具定义里有没有
    `_meta.ui.resourceUri`」判断这工具算不算 App，**完全不读**工具结果里的
    任何字段（`panelOpened` / `reason` 一概不看）。
    ⇒ 早期那套 A+C「返回 panelOpened=false 防堆叠」在宿主层**从未生效**。
    观测证据：同一会话连调 3 次 `tripplanner_plan_trip`，宿主日志出现 3 条
    `openHostApp.created`（3 个不同 toolCallId / appInstanceId）。

修法：把「会开面板的工具」收成**一个**（`tripplanner_open_plan`），并让
     `tripplanner_plan_trip` 不带面板 ⇒ 重拍多少次都不堆面板。

本文件钉住三条不变式（**行为级**，不是 grep 源码）：
    A. 9 个工具里带 `_meta.ui.resourceUri` 的**恰好一个**，且是 open_plan；
    B. `plan_trip` 的返回：`panelOpened=False`、`panelVia=open_plan`、
       文本里点名让模型去调 open_plan；且**不改**会话的「已开过面板」状态；
    C. `open_plan` 首次 → `panelOpened=True` 且带上当前行程；
       再次调用 → 走**劝阻**分支（文案里明确「不要再调用」）。
"""
import importlib.util
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 默认守**真文件**；`TRIPPLANNER_SERVER` 只给红队脚本指向同目录的临时变体用
# （这样红队不必改动真文件，见 mcp/redteam_panel_single.py）。
SERVER = os.environ.get("TRIPPLANNER_SERVER") or os.path.join(
    ROOT, "mcp", "tripplanner_mcp_server.py")


def _load():
    spec = importlib.util.spec_from_file_location("_tp_server_under_test", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # 隔离副作用：不落盘、不写诊断日志、不打网络。
    mod._plan_state_read = lambda: None
    mod._plan_state_write = lambda value: None
    mod.dbg = lambda *a, **k: None
    mod.log = lambda *a, **k: None
    return mod


srv = _load()


def _reset():
    srv.SESSION.opened_any = False
    srv.SESSION.instances.clear()
    srv.SESSION._last_plan = None
    srv.SESSION._plan_loaded = True          # 已「加载」过 → 不触发磁盘读


def _call(name, args=None):
    r = srv.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                    "params": {"name": name, "arguments": args or {}}})
    assert r and "result" in r, f"{name} 没有正常返回：{r}"
    return r["result"]


FAKE_PLAN = {
    "city": "长沙", "days": 2, "mode": "m7_proposal", "candidates": 10,
    "latency_s": 3.1, "totalTravelKm": 15.1, "totalViolations": 0,
    "query": "长沙两天纯吃", "dayList": [
        {"day": 1, "theme": "吃街", "travel_km": 7.0, "finish": "19:00",
         "timeline": [{"type": "poi", "id": "CS01", "name": "太平老街",
                       "start": "09:00", "end": "10:00"}]},
    ],
}


def test_only_one_panel_tool():
    """不变式 A：面板入口有且只有一个。"""
    panel_tools = [t["name"] for t in srv.TOOLS
                   if (((t.get("_meta") or {}).get("ui") or {}).get("resourceUri"))]
    assert panel_tools == ["tripplanner_open_plan"], (
        "带 _meta.ui.resourceUri 的工具必须恰好是 open_plan，实际=" + repr(panel_tools) +
        "（多一个就多一处「每次调用都堆面板」的入口）")
    # 反面：其余 8 个（尤其 plan_trip）绝不能带 —— 否则重拍就叠面板
    for t in srv.TOOLS:
        if t["name"] != "tripplanner_open_plan":
            assert not t.get("_meta"), f"{t['name']} 不该带 _meta：{t.get('_meta')!r}"
    pd = next(t for t in srv.TOOLS if t["name"] == "tripplanner_plan_trip")["description"]
    assert "不打开" in pd and "tripplanner_open_plan" in pd, pd
    od = next(t for t in srv.TOOLS if t["name"] == "tripplanner_open_plan")["description"]
    assert "唯一入口" in od and "一次" in od, od
    print("✅ A：面板入口唯一（tripplanner_open_plan），plan_trip 已摘掉面板")


def test_plan_trip_never_opens_panel():
    """不变式 B：plan_trip 返回里没有面板，且不污染「已开过」状态。"""
    _reset()
    srv.run_plan = lambda *a, **k: {"ok": True, "plan": dict(FAKE_PLAN)}
    res = _call("tripplanner_plan_trip", {"query": "长沙两天纯吃"})
    sc = res.get("structuredContent") or {}
    assert sc.get("panelOpened") is False, sc.get("panelOpened")
    assert sc.get("panelVia") == "tripplanner_open_plan", sc.get("panelVia")
    text = "".join(c.get("text", "") for c in (res.get("content") or []))
    assert "tripplanner_open_plan" in text, text
    assert sc.get("plan", {}).get("city") == "长沙", "行程本体仍要回给模型"
    assert srv.SESSION.opened_any is False, "plan_trip 不得把「已开过面板」写脏"
    # 连调 3 次（模拟重拍）→ 状态依旧不变
    for _ in range(2):
        _call("tripplanner_plan_trip", {"query": "再拍一版"})
    assert srv.SESSION.opened_any is False, "重拍 N 次后仍未开面板（这正是修复目标）"
    print("✅ B：plan_trip 重拍 3 次 → 0 面板、状态未被污染")


def test_plan_trip_failure_also_no_panel():
    _reset()
    srv.run_plan = lambda *a, **k: {"ok": False, "error": "云端 500"}
    res = _call("tripplanner_plan_trip", {"query": "x"})
    sc = res.get("structuredContent") or {}
    assert sc.get("ok") is False and "panelOpened" not in sc, sc
    assert srv.SESSION.opened_any is False, "失败分支也不得开面板"
    print("✅ B2：规划失败也只回文字、不开面板")


def test_open_plan_opens_once_then_discourages():
    """不变式 C：open_plan 首次开、再次劝阻。"""
    _reset()
    srv.SESSION.last_plan = dict(FAKE_PLAN)
    r1 = _call("tripplanner_open_plan", {})
    sc1 = r1.get("structuredContent") or {}
    assert sc1.get("panelOpened") is True, sc1
    assert sc1.get("hasPlan") is True and (sc1.get("plan") or {}).get("city") == "长沙", sc1
    assert srv.SESSION.opened_any is True

    r2 = _call("tripplanner_open_plan", {})
    sc2 = r2.get("structuredContent") or {}
    assert sc2.get("panelOpened") is False, sc2
    assert sc2.get("reason") == "panel-already-open", sc2
    t2 = "".join(c.get("text", "") for c in (r2.get("content") or []))
    assert "不要再调用" in t2, t2
    assert "新开一扇" in t2, "劝阻文案要说清「宿主每次调用都会新开」的机制"
    print("✅ C：open_plan 首次开面板、再次给劝阻文案")


if __name__ == "__main__":
    test_only_one_panel_tool()
    test_plan_trip_never_opens_panel()
    test_plan_trip_failure_also_no_panel()
    test_open_plan_opens_once_then_discourages()
    print("🎉 全部通过（面板单次显示不变式）")
