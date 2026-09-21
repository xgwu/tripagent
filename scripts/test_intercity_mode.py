# -*- coding: utf-8 -*-
"""跨城交通方式守卫（2026-09-21 用户报障）。

报障：「苏杭3天联游」——用户没说自驾，行程里却自动出现「自驾 苏州 → 杭州」转移行与
「从 苏州 驾车到 杭州」通知。要求：**未指定交通方式时留空**，不要假定跨城驾车；
以后用户指定高铁/飞机时再按需填补。

修法：`intercity_mode(query)` 只认用户**明说**的方式（driving/rail/flight）；
未指定返回 None，转移行名称与通知文案按 mode 生成，并新增 `mode` 字段（未指定为 null）
供后续填补。时长仍用驾车口径估算（时间轴必须预留，否则行程物理上不成立），
但会如实披露口径。

用法：python scripts/test_intercity_mode.py
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


print("== 1. 方式识别（只认用户明说的）=")
for q, want, why in [("苏杭3天联游", None, "没提方式 → 留空"),
                     ("苏杭3天自驾游", "driving", "自驾"),
                     ("苏杭3天，我们自己开车", "driving", "自己开车"),
                     ("苏杭3天坐高铁", "rail", "高铁"),
                     ("苏杭3天动车游", "rail", "动车"),
                     ("苏杭3天，坐飞机过去", "flight", "飞机"),
                     ("苏杭3天航班往返", "flight", "航班")]:
    got = S.intercity_mode(q)
    case(f"1 {q} → {want}（{why}）", got == want, f"实测 {got}")

print("\n== 2. 转移行名称：未指定时不得出现「自驾」=")
case("2a 未指定 → 「苏州 → 杭州（城际交通待定）」，且不含「自驾」",
     S.intercity_leg_name(None, "苏州", "杭州") == "苏州 → 杭州（城际交通待定）"
     and "自驾" not in S.intercity_leg_name(None, "苏州", "杭州"),
     S.intercity_leg_name(None, "苏州", "杭州"))
case("2b 指定自驾 → 「自驾 苏州 → 杭州」",
     S.intercity_leg_name("driving", "苏州", "杭州") == "自驾 苏州 → 杭州")
case("2c 指定高铁 → 「高铁 苏州 → 杭州」",
     S.intercity_leg_name("rail", "苏州", "杭州") == "高铁 苏州 → 杭州")
case("2d 指定飞机 → 「飞机 苏州 → 杭州」",
     S.intercity_leg_name("flight", "苏州", "杭州") == "飞机 苏州 → 杭州")

print("\n== 3. 时长短语与口径披露 ==")
case("3a 驾车是本色，不必额外披露", S.intercity_disclaim("driving") == "")
case("3b 未指定方式必须披露「按驾车口径估算」",
     "驾车口径" in S.intercity_disclaim(None) and "未指定" in S.intercity_disclaim(None),
     S.intercity_disclaim(None))
case("3c 指定高铁/飞机时披露「以购票为准」",
     "购票为准" in S.intercity_disclaim("rail")
     and "购票为准" in S.intercity_disclaim("flight"))
case("3d 短语前缀随 mode 变",
     S.intercity_verb("driving") == "驾车约" and S.intercity_verb(None) == "城际交通约")

print("\n== 4. 端到端（伪规划器，确定性）：转移行真的按需求生成 ==")


def _fake_result(city, days):
    return {"mode": "m7_proposal",
            "itinerary": {"days": [{"day": k + 1, "theme": "t",
                                    "timeline": [{"type": "poi", "id": f"{city}{k}",
                                                  "name": "景点", "start": "09:00",
                                                  "end": "10:00"}],
                                    "violations": []} for k in range(days)],
                          "total_violations": 0, "total_travel_km": 1.0,
                          "dropped_pois": []},
            "grounding": {"grounding_rate": 1.0, "gaps": [], "n_proposed": 1,
                          "unmatched": 0, "dup_skipped": 0},
            "notices": [], "latency_s": 0.1, "candidates": 0, "n_dup_across_days": 0}


class FakePlanner:
    def plan(self, city, query, days=2, use_llm=True, date0=None, hotel_text=None,
             progress=None):
        return _fake_result(city.get("city", "?"), days)


def _transfer_rows(q):
    r = S.plan_multi(["苏州", "杭州"], q, 3, None, use_llm=False, planner=FakePlanner())
    rows = [s for d in r["itinerary"]["days"] for s in d.get("timeline", [])
            if s.get("type") == "transfer"]
    msgs = [n.get("message", "") for n in (r.get("notices") or [])
            if n.get("kind") == "intercity_transfer"]
    return rows, msgs


rows, msgs = _transfer_rows("苏杭3天联游")
case("4a 未指定方式：转移行存在但名称不含「自驾」",
     bool(rows) and all("自驾" not in x["name"] for x in rows),
     str([x["name"] for x in rows]))
case("4b 未指定方式：转移行 mode 字段为 None（留空，待填补）",
     bool(rows) and all(x.get("mode") is None for x in rows),
     str([x.get("mode") for x in rows]))
case("4c 未指定方式：通知不说「驾车到」，且披露驾车口径",
     bool(msgs) and all("驾车到" not in m for m in msgs)
     and all("驾车口径" in m for m in msgs),
     str(msgs)[:150])
case("4d 对照：明说自驾时仍按自驾呈现",
     all(x["name"].startswith("自驾 ") and x.get("mode") == "driving"
         for x in _transfer_rows("苏杭3天自驾游")[0])
     and "驾车约" in (_transfer_rows("苏杭3天自驾游")[1] or [""])[0],
     str([x["name"] for x in _transfer_rows("苏杭3天自驾游")[0]]))
case("4e 对照：明说高铁时按高铁呈现，且不写自驾",
     all(x["name"].startswith("高铁 ") and x.get("mode") == "rail"
         for x in _transfer_rows("苏杭3天坐高铁")[0]),
     str([x["name"] for x in _transfer_rows("苏杭3天坐高铁")[0]]))

print("\n== 5. 源码守卫 ==")
src = io.open(os.path.join(ROOT, "webui", "server.py"), encoding="utf-8").read()
case("5a 旧的硬编码转移行名已不存在",
     'f"自驾 {city_from} → {city_to}' not in src
     and 'f"自驾 {use[i - 1]} → {cname}' not in src)
case("5b 三处行名都走 intercity_leg_name",
     src.count("intercity_leg_name(") >= 4, f"命中 {src.count('intercity_leg_name(')} 处")
case("5c 三处通知都带口径披露",
     src.count("intercity_disclaim(_mode)") >= 3,
     f"命中 {src.count('intercity_disclaim(_mode)')} 处")
case("5d plan_multi 里解析了一次 mode",
     "_mode = intercity_mode(query)" in src)

print()
print("全部通过" if not FAIL else f"失败 {len(FAIL)} 项：")
for f in FAIL:
    print("   -", f)
sys.exit(1 if FAIL else 0)
