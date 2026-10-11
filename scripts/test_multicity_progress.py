# -*- coding: utf-8 -*-
"""多城联游分阶段进度守卫（2026-09-21 报障：「苏杭3天联游」进度条只显示「解析需求」）。

根因：单城路径用 `_plan_extra_kw(planner, progress_cb)` 透传 progress（两处），
**多城路径 `plan_multi(...)` 根本没接 progress**，段内 `planner.plan(...)` 也没传
→ 前端轮询 `/api/progress` 永远只拿到打点在第一行的 "parse"，进度条停在「解析需求」。

本测试用**伪规划器**验证逐段上报（不花 LLM、确定性），并守住「无 progress 参数的
规划器（m1）不得收到该关键字参数」这条兼容约束。

用法：python scripts/test_multicity_progress.py
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from webui import server as S  # noqa: E402

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


def _fake_result(city, days):
    """伪规划结果：结构够 plan_multi 跑完合并即可（不做真实规划）。"""
    return {
        "mode": "m7_proposal",
        "itinerary": {"days": [{"day": k + 1, "theme": f"{city}D{k+1}",
                                "timeline": [{"type": "poi", "id": f"{city}{k}",
                                              "name": "景点", "start": "09:00",
                                              "end": "10:00"}],
                                "violations": []} for k in range(days)],
                      "total_violations": 0, "total_travel_km": 1.0, "dropped_pois": []},
        "grounding": {"grounding_rate": 1.0, "gaps": [], "n_proposed": 1,
                      "unmatched": 0, "dup_skipped": 0},
        "notices": [], "latency_s": 0.1, "candidates": 0, "n_dup_across_days": 0,
    }


class ProgressPlanner:
    """带 progress 参数的伪规划器：每次 plan 上报两个阶段。"""

    def __init__(self):
        self.calls = []

    def plan(self, city, query, days=2, use_llm=True, date0=None, hotel_text=None,
             progress=None):
        # plan_multi 传的是 poi_db.load_city() 的**城市字典**，不是城市名
        self.calls.append(city.get("city") if isinstance(city, dict) else city)
        if progress:
            progress("proposal")
            progress("grounding")
        return _fake_result(city.get("city", "?") if isinstance(city, dict) else str(city),
                            days)


class PlainPlanner:
    """不带 progress 参数的伪规划器（模拟 m1_planner）。"""

    def plan(self, city, query, days=2, use_llm=True, date0=None, hotel_text=None):
        return _fake_result(city.get("city", "?") if isinstance(city, dict) else str(city),
                            days)


print("== 1. 多城逐段上报 ==")
seen = []
pl = ProgressPlanner()
r = S.plan_multi(["苏州", "杭州"], "苏杭3天联游", 3, None, use_llm=False, planner=pl,
                 progress=lambda st, seg=None: seen.append((st, seg)))
case("1a 两座城都走到了 plan()", pl.calls == ["苏州", "杭州"], str(pl.calls))
case("1b 每个阶段都被上报（不是只上报第一个）",
     [s for s, _ in seen] == ["proposal", "grounding", "proposal", "grounding"],
     str([s for s, _ in seen]))
case("1c 段信息带「第 i/n 段 + 城市」，且 i 从 1 起",
     [seg for _, seg in seen] == [
         {"i": 1, "n": 2, "city": "苏州"}, {"i": 1, "n": 2, "city": "苏州"},
         {"i": 2, "n": 2, "city": "杭州"}, {"i": 2, "n": 2, "city": "杭州"}],
     str([seg for _, seg in seen]))

print("\n== 2. 兼容：规划器没有 progress 参数时不得报错 ==")
try:
    S.plan_multi(["苏州", "杭州"], "苏杭3天联游", 3, None, use_llm=False,
                 planner=PlainPlanner(), progress=lambda st, seg=None: None)
    ok, err = True, ""
except TypeError as e:
    ok, err = False, str(e)
case("2a m1 类规划器（无 progress 参数）不因透传而 TypeError", ok, err)
case("2b 对照：有 progress 参数的规划器确实收到了 progress",
     "progress" in __import__("inspect").signature(ProgressPlanner.plan).parameters)

print("\n== 3. 单城路径不受影响 ==")
case("3a 单城仍用单参 progress 回调（_plan_extra_kw 仍在用）",
     S._plan_extra_kw(ProgressPlanner(), (lambda s: None)) != {}
     and S._plan_extra_kw(PlainPlanner(), (lambda s: None)) == {})

print("\n== 4. 阶段打点带段信息 ==")
S._stage_put("sid-test", "solving", seg={"i": 2, "n": 3, "city": "杭州"})
ent = S._plan_stages.get("sid-test")
case("4a _stage_put 存下 seg（前端据此显示「第 i/n 段」）",
     ent and ent.get("seg") == {"i": 2, "n": 3, "city": "杭州"}, str(ent))

print("\n== 5. 源码/前端守卫 ==")
src = io.open(os.path.join(ROOT, "webui", "server.py"), encoding="utf-8").read()
html = io.open(os.path.join(ROOT, "webui", "index.html"), encoding="utf-8").read()
case("5a plan_multi 签名带 progress（+ skip_copy）",
     "progress=None, skip_copy: bool = False) -> dict:" in src)
case("5b 多城调用点把 progress=progress_cb_m 传进去",
     "progress=progress_cb_m" in src)
case("5c 段内 plan() 经 _plan_extra_kw 透传段回调（含可选 skip_copy）",
     "**_plan_extra_kw(planner, _seg_cb," in src)
case("5d /api/progress 回传 seg", '"seg": ent.get("seg")' in src)
case("5e 前端渲染段信息", "pr.seg" in html and "段 · " in html)
case("5f 前端阶段表未被改（后端阶段名必须与它精确对齐）",
     '[["parse","解析需求"],["proposal","经验提案"],["grounding","落地匹配"],'
     '["solving","求解排程"],["regen","生成文案"]]' in html)

S._stage_pop("sid-test")
print()
print("全部通过" if not FAIL else f"失败 {len(FAIL)} 项：")
for f in FAIL:
    print("   -", f)
sys.exit(1 if FAIL else 0)
