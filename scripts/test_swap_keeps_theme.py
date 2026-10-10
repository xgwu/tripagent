# -*- coding: utf-8 -*-
"""换点 / 挪时段不得丢掉「当日主题」（2026-10-10 报障）。

**根因**（不是面板渲染问题，是数据在写回时被清空）：
  1. `sequencer.build_itinerary` 输出的 day 里**没有** `theme` / `reason`
     —— 主题是 compose 阶段（`m2_planner.compose`）按**最终时间轴**由 LLM 生成的；
  2. 换点内核 `poi_swap.replace_poi` 只回一个内核形状的 day，
     `/api/replace` 再用 `plan_view.normalize_day` 补字段 ⇒ `theme: ""`、`reason: ""`
     （`normalize_day` 对缺失字段一律补空串，这是它「形状稳定」的设计）；
  3. 写回端做的是**整日替换**：面板 `applyDayToPlan`、云端会话 `plan_view.apply_day`
     、瘦客户端 `_merge_day_into_plan` ⇒ **空串成了「明确的空」**，
     当天标题退化成「未命名主题」。

**修法**：换点 / 挪时段**只改时间轴**，新结果没给出的 `theme` 一律沿用旧值。
`reason` 刻意**不**保留 —— 它逐点点名（"从断桥残雪开始，沿湖依次游览苏堤春晓…"），
换掉其中一点后就是过期信息，宁可没有；面板的 tips 同理（webui 早已这么处理）。

四处写回点必须同一套语义（本项目既有教训：多副本实现必须有守测试）：
  `src/plan_view.py::apply_day` / `mcp/tripplanner_mcp_server.py::_merge_day_into_plan`
  / `mcp/panel/plan_panel.html::applyDayToPlan` / `webui/index.html::swapPoi`
  —— 后两处由 `mcp/panel_guard.py`（node 真跑）守。
"""
import importlib.util
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import plan_view, poi_swap, sequencer  # noqa: E402

OLD_THEME = "姑苏园林漫步"
CITY = "苏州"
# 苏州库：SZ001 拙政园 / SZ007 留园 / SZ013 观前街（culture + shopping，容量安全）
DAY_IDS = ["SZ001", "SZ007", "SZ013"]
SWAP_OUT = "SZ007"


def _realitinerary():
    """真跑内核，产出两天的原始 itinerary（第二天空着，只为凑 shape）。"""
    city = sequencer.poi_db.load_city(CITY)
    allp = {p["id"]: sequencer.poi_db.parse_poi(p, city) for p in city["pois"]}
    it = sequencer.build_itinerary({1: list(DAY_IDS)}, city, allp,
                                   order_given=False, date0="2026-10-02")
    return it["days"][0]


def _plan_with_theme():
    """模拟面板手上的那份 plan：day1 有 theme/reason，其余字段走归一化。"""
    raw = _realitinerary()
    coords = plan_view.poi_coords(CITY)
    d1 = plan_view.normalize_day(raw, coords)
    d1["theme"] = OLD_THEME
    d1["reason"] = "逐点点名的旧理由（含留园）"
    return {"city": CITY, "query": "", "dayList": [d1],
            "totalTravelKm": d1["travel_km"]}


def _swap(plan):
    """走真实换点链路：内核换点 → 归一化（与 /api/replace 逐行一致）。"""
    used = [s["id"] for d in plan["dayList"] for s in d["timeline"] if s["type"] == "poi"]
    code, data = poi_swap.replace_poi(CITY, SWAP_OUT, 1, list(DAY_IDS),
                                      used_ids=used, date0="2026-10-02")
    assert code == 200 and data.get("ok"), f"换个点都没成：{code} {data}"
    day = plan_view.normalize_day(data["day"], plan_view.poi_coords(CITY))
    return data, day


def test_kernel_day_has_no_theme():
    """内核事实：换点回的 day 里 theme 不是「空」而是根本没有（归一化才补空串）。"""
    raw = _realitinerary()
    assert "theme" not in raw, "内核若已产 theme，本守卫的前提就变了"
    nd = plan_view.normalize_day(raw, {})
    assert nd["theme"] == "" and nd["reason"] == ""
    print("✅ 内核 day 无 theme（归一化补空串）—— 根因前提成立")


def test_apply_day_keeps_theme():
    """云端会话写回：换点后当天 theme 不得变空。"""
    plan = _plan_with_theme()
    _, day = _swap(plan)
    out = plan_view.apply_day(plan, day)
    d1 = [d for d in out["dayList"] if d["day"] == 1][0]
    assert d1["theme"] == OLD_THEME, f"主题丢了：{d1['theme']!r}"
    assert d1["reason"] == "", f"reason 应作废（逐点点名会过期），实际 {d1['reason']!r}"
    # 点集确实换了（不是「什么都没做」的假绿）
    assert SWAP_OUT not in [s.get("id") for s in d1["timeline"]]
    print("✅ apply_day 保留当日主题")


def test_apply_day_respects_new_theme():
    """反向守卫：云端将来若**明确回填** theme，不许被旧值盖掉。"""
    plan = _plan_with_theme()
    _, day = _swap(plan)
    day["theme"] = "换点后的新主题"
    out = plan_view.apply_day(plan, day)
    d1 = [d for d in out["dayList"] if d["day"] == 1][0]
    assert d1["theme"] == "换点后的新主题", f"新主题被旧值覆盖：{d1['theme']!r}"
    print("✅ 新主题优先（旧值只在缺失时兜底）")


def test_reorder_day_keeps_theme():
    """挪时段同理：只换顺序，主题不该掉。"""
    plan = _plan_with_theme()
    order = ["SZ013", "SZ001", "SZ007"]
    code, data = poi_swap.reorder_day(CITY, 1, order, date0="2026-10-02")
    assert code == 200 and data.get("ok"), f"挪时段失败：{code} {data}"
    day = plan_view.normalize_day(data["day"], plan_view.poi_coords(CITY))
    out = plan_view.apply_day(plan, day)
    d1 = [d for d in out["dayList"] if d["day"] == 1][0]
    assert d1["theme"] == OLD_THEME, f"挪时段后主题丢了：{d1['theme']!r}"
    print("✅ 挪时段保留当日主题")


def test_merge_day_into_plan_keeps_theme():
    """瘦客户端 `_merge_day_into_plan`：与 apply_day 同一套语义（不依赖内核）。"""
    spec = importlib.util.spec_from_file_location(
        "_tpmcp_theme", os.path.join(ROOT, "mcp", "tripplanner_mcp_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    plan = _plan_with_theme()
    _, day = _swap(plan)
    out = mod._merge_day_into_plan(plan, day)
    d1 = [d for d in out["dayList"] if d["day"] == 1][0]
    assert d1["theme"] == OLD_THEME, f"瘦客户端主题丢了：{d1['theme']!r}"
    assert SWAP_OUT not in [s.get("id") for s in d1["timeline"]]
    assert out["totalTravelKm"] == round(
        sum(float(d.get("travel_km") or 0) for d in out["dayList"]), 1)
    print("✅ _merge_day_into_plan 保留当日主题")


if __name__ == "__main__":
    test_kernel_day_has_no_theme()
    test_apply_day_keeps_theme()
    test_apply_day_respects_new_theme()
    test_reorder_day_keeps_theme()
    test_merge_day_into_plan_keeps_theme()
    print("🎉 全部通过")
