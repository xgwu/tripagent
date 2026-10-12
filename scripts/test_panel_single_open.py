# -*- coding: utf-8 -*-
"""守卫：**面板有且只有一个入口，且调用节律是「每个需求一次」**（M6.6）。

两轮报障与结论（都从宿主 app.asar 源码取证，不是推测）：

  1) 2026-10-10 报障一：反复重拍时对话里**叠了好几扇面板**。
     `McpAppsGateway.handleAgentToolCall()` 的去重键是 `${sessionId}::${toolCallId}`
     ⇒ **每一次工具调用 = 新开一扇面板**；宿主只按「工具定义里有没有
     `_meta.ui.resourceUri`」判断这工具算不算 App，**完全不读**工具结果里的任何字段
     （`panelOpened` / `reason` 一概不看）。
     ⇒ 早期那套 A+C「返回 panelOpened=false 防堆叠」在宿主层**从未生效**。
     修法：把「会开面板的工具」收成**一个**（`tripplanner_open_plan`），
     `tripplanner_plan_trip` 摘掉面板 ⇒ 重拍多少次都不堆面板。

  2) 2026-10-10 报障二（**修正了 1) 的过度收敛**）：同一会话里提出**第二个需求**时，
     面板再也不出来了 —— 因为 1) 顺手加了「服务端会话级封锁」（`opened_any` 一开就
     不再开、第二次调用回劝阻文案）。**服务端根本拦不住宿主**，那种封锁只拦得住
     模型自己 ⇒ 等于把功能砍掉。
     ⇒ 节律的正确粒度是「**需求**」而不是「会话」，而且只能由**工具描述**约定
       （"调不调 open_plan" 是模型的事前决策，服务端只能在事后回话 —— 说晚了）。

本文件钉住四条不变式（**行为级**：真的调 `handle()`，不是 grep 源码）：
    A. 10 个工具里带 `_meta.ui.resourceUri` 的**恰好一个**，且是 open_plan；
       其余九个（尤其 plan_trip）一个都不许带。
    B. `plan_trip`：不开面板、不改变本会话的展示计数 ⇒ 可安全反复调（重拍不堆面板）。
    B2. `plan_trip` 失败分支同样不开面板。
    C. `open_plan`：**每一次调用都判定为「展示面板」**（`panelOpened=True`）——
       这是「新需求能弹新面板」的充要条件；**不得**出现会话级拒绝
       （`reason=panel-already-open` 之类）。且第 2 次展示必须带上**当时的**最新行程。
    D. `open_plan` 的 description 必须讲清节律（每个需求一次 / 新需求应再调 /
       重拍期间不要调）—— 那条文字是**唯一**的节律约束（服务端拦不住宿主）。
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
    # ⚠️ 2026-10-12：`_plan_state_read/write` 加了 `sid` 参数（P0 会话隔离）⇒ 用 *a/**k 接住。
    mod._plan_state_read = lambda *a, **k: None
    mod._plan_state_write = lambda *a, **k: None
    mod.dbg = lambda *a, **k: None
    mod.log = lambda *a, **k: None
    return mod


srv = _load()

# ⚠️ 2026-10-12 P0：`SESSION` 已是**线程局部代理**，不绑定就每次拿到新的空会话 ⇒
#   测试里对 `srv.SESSION.opened_any` 的读写在「未绑定」时自相矛盾。显式绑一个会话。
_TEST_SESS = srv._Session("test-panel-single-open")
srv.SESSION.bind(_TEST_SESS)


def _reset():
    srv.SESSION.opened_any = False
    srv.SESSION.instances.clear()
    srv.SESSION.panel_opens = 0
    srv.SESSION._last_plan = None
    srv.SESSION._plan_loaded = True          # 已「加载」过 → 不触发磁盘读


def _call(name, args=None):
    r = srv.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                    "params": {"name": name, "arguments": args or {}}})
    assert r and "result" in r, f"{name} 没有正常返回：{r}"
    return r["result"]


def _text(res):
    return "".join(c.get("text", "") for c in (res.get("content") or []))


FAKE_PLAN = {
    "city": "长沙", "days": 2, "mode": "m7_proposal", "candidates": 10,
    "latency_s": 3.1, "totalTravelKm": 15.1, "totalViolations": 0,
    "query": "长沙两天纯吃", "dayList": [
        {"day": 1, "theme": "吃街", "travel_km": 7.0, "finish": "19:00",
         "timeline": [{"type": "poi", "id": "CS01", "name": "太平老街",
                       "start": "09:00", "end": "10:00"}]},
    ],
}
# 「新需求」的产物：城市/天数都不同，模拟同一会话里的第二次规划
FAKE_PLAN2 = dict(FAKE_PLAN, city="苏州", days=3, query="苏州三天园林",
                  dayList=[dict(FAKE_PLAN["dayList"][0], day=1)])


def test_panel_entry_is_unique():
    """不变式 A：带面板的工具恰好一个（这仍是「重拍不堆面板」的真正机制）。"""
    panel_tools = [t["name"] for t in srv.TOOLS
                   if (((t.get("_meta") or {}).get("ui") or {}).get("resourceUri"))]
    assert panel_tools == ["tripplanner_open_plan"], (
        "带 _meta.ui.resourceUri 的工具必须恰好是 open_plan，实际=" + repr(panel_tools) +
        "（多一个就多一处「每次调用都堆面板」的入口）")
    for t in srv.TOOLS:
        if t["name"] != "tripplanner_open_plan":
            assert not t.get("_meta"), f"{t['name']} 不该带 _meta：{t.get('_meta')!r}"
    print("✅ A：面板入口唯一（tripplanner_open_plan），plan_trip 已摘掉面板")


def test_plan_trip_never_opens_panel():
    """不变式 B：plan_trip 不开面板，也不改变展示计数（重拍安全）。"""
    _reset()
    srv.run_plan = lambda *a, **k: {"ok": True, "plan": dict(FAKE_PLAN)}
    res = _call("tripplanner_plan_trip", {"query": "长沙两天纯吃"})
    sc = res.get("structuredContent") or {}
    assert sc.get("panelOpened") is False, sc.get("panelOpened")
    assert sc.get("panelVia") == "tripplanner_open_plan", sc.get("panelVia")
    assert "tripplanner_open_plan" in _text(res), _text(res)
    assert sc.get("plan", {}).get("city") == "长沙", "行程本体仍要回给模型"
    assert srv.SESSION.panel_opens == 0, "plan_trip 不得算作一次「展示面板」"
    for _ in range(2):
        _call("tripplanner_plan_trip", {"query": "再拍一版"})
    assert srv.SESSION.panel_opens == 0, "重拍 3 次后仍 0 次展示（这正是修复目标）"
    print("✅ B：plan_trip 重拍 3 次 → 0 次展示、计数未被污染")


def test_plan_trip_failure_also_no_panel():
    _reset()
    srv.run_plan = lambda *a, **k: {"ok": False, "error": "云端 500"}
    res = _call("tripplanner_plan_trip", {"query": "x"})
    sc = res.get("structuredContent") or {}
    assert sc.get("ok") is False and "panelOpened" not in sc, sc
    assert srv.SESSION.panel_opens == 0, "失败分支也不得算作一次展示"
    print("✅ B2：规划失败也只回文字、不开面板")


def test_open_plan_opens_every_time():
    """不变式 C（本轮报障的核心）：**每次调用都展示**，跨需求可再开。

    这是用户明确要的语义：同一需求内只在该需求定稿后调一次（由描述约束），
    但**新需求**完成规划后必须能再弹一扇新面板。
    """
    _reset()
    srv.SESSION.last_plan = dict(FAKE_PLAN)
    r1 = _call("tripplanner_open_plan", {})
    sc1 = r1.get("structuredContent") or {}
    assert sc1.get("panelOpened") is True, sc1
    assert sc1.get("panelOpenCount") == 1, sc1
    assert sc1.get("hasPlan") is True and (sc1.get("plan") or {}).get("city") == "长沙", sc1
    assert "reason" not in sc1, "首次调用不该带任何拒绝字段：" + repr(sc1)

    # —— 同一会话里的「新需求」：换了行程，再调一次 ——
    srv.SESSION.last_plan = dict(FAKE_PLAN2)
    r2 = _call("tripplanner_open_plan", {})
    sc2 = r2.get("structuredContent") or {}
    assert sc2.get("panelOpened") is True, (
        "❌ 第二个需求被服务端拒了 —— 会话级封锁（opened_any）不得复活：" + repr(sc2))
    assert "reason" not in sc2, "不得出现 panel-already-open 之类的拒绝：" + repr(sc2)
    assert sc2.get("panelOpenCount") == 2, sc2
    assert (sc2.get("plan") or {}).get("city") == "苏州", (
        "第 2 次展示必须带**当时的**最新行程，而不是第 1 次的旧值：" + repr(sc2.get("plan")))
    assert srv.SESSION.panel_opens == 2 and srv.SESSION.opened_any is True
    print("✅ C：open_plan 每次调用都展示（第 1 次=长沙、第 2 次=苏州），无会话级拒绝")


def test_open_plan_description_states_cadence():
    """不变式 D：节律只能靠描述 —— 它必须是「每需求一次」而不是「每会话一次」。"""
    od = next(t for t in srv.TOOLS
              if t["name"] == "tripplanner_open_plan")["description"]
    assert "唯一入口" in od, od
    for must in ("每个需求", "新需求", "再调一次", "重拍"):
        assert must in od, f"open_plan 描述必须讲清节律，缺了「{must}」：{od}"
    assert "同一对话只" not in od, (
        "不得再写「同一对话只调一次」—— 那正是第二个需求出不来面板的成因：" + od)
    print("✅ D：open_plan 描述写明「每个需求一次 / 新需求应再调 / 重拍期间不调」")


if __name__ == "__main__":
    test_panel_entry_is_unique()
    test_plan_trip_never_opens_panel()
    test_plan_trip_failure_also_no_panel()
    test_open_plan_opens_every_time()
    test_open_plan_description_states_cadence()
    print("🎉 全部通过（面板：入口唯一 + 每个需求展示一次）")
