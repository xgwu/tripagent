# -*- coding: utf-8 -*-
"""第 1 层守卫：骑行场景的「停留时长」打卡口径（2026-10-12）。

背景：`duration_h` 是景区适玩时长（石公山 1.5h / 明月湾 2h / 西山岛 4h）—— 那是
「进去好好逛」的口径。骑行游的点本质是快速打卡/补给站。实测 7 个环太湖景区口径点
停留合计 714min > 630min 日预算 ⇒ 50km 骑行日只塞 1-2 点。

修法：`poi_db.parse_poi(p, city, mode="cycling")` 的 `dur` 改用 `cycle_duration_h`
（景区时长 × 类目系数，下限 0.3h/上限 1.2h）；**原始 `duration_h` 不动**
（is_full_day 与 LLM 菜单仍读它）。

本文件钉住：
  A. 常量与函数存在（防摘除）
  B. 类目系数生效：自然/观景/打卡 < 文化/历史；下限抬升极小值、上限压制超长值
  C. 全天大点（≥8h）不被缩（独占日语义不破）
  D. 非骑行 mode（None/其他）dur 与 duration_h 逐点相等（**零回归**）
  E. 骑行口径下装载量显著提升（实测 ≥5 点 vs 景区口径 3-4 点）
  F. 单日主题（"其中一天骑行"）不缩 dur（避免污染非骑行日）

用法：python scripts/test_cycling_duration.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from src import poi_db, sequencer, toptw, m2_planner  # noqa: E402

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


def _p(pid, name, cat, dur, o=9.0, c=17.0, lat=31.0, lng=120.0, rating=4):
    return {"id": pid, "name": name, "category": cat, "duration_h": dur,
            "open": f"{int(o):02d}:00", "close": f"{int(c):02d}:00",
            "lat": lat, "lng": lng, "rating": rating, "tags": ["x"],
            "best_time": "any", "price": 0, "area": "suburb", "family_ok": True,
            "note": "testtest", "closed_days": []}


CITY = {"day_start": "09:00", "day_end": "21:30",
        "center": {"lat": 31.0, "lng": 120.0}, "province": "test"}

# ---- A：常量与函数 ----
case("A1 CYCLE_DUR_FACTOR 已定义且含自然/观景/文化",
     isinstance(poi_db.CYCLE_DUR_FACTOR, dict)
     and {"nature", "view", "culture"} <= set(poi_db.CYCLE_DUR_FACTOR))
case("A2 上下限常量已定义",
     poi_db.CYCLE_DUR_MIN_H == 0.3 and poi_db.CYCLE_DUR_MAX_H == 1.2)
case("A3 cycle_duration_h / parse_all 存在",
     callable(getattr(poi_db, "cycle_duration_h", None))
     and callable(getattr(poi_db, "parse_all", None)))

# ---- B：类目系数 ----
view_p = _p("V1", "观景点", "view", 2.0)
cult_p = _p("C1", "文化点", "culture", 2.0)
dv, dc = poi_db.cycle_duration_h(view_p), poi_db.cycle_duration_h(cult_p)
case("B1 观景类缩放强于文化类（view < culture）", dv < dc, f"view={dv} culture={dc}")
tiny = _p("T1", "极小点", "view", 0.5)
case("B2 下限抬升（0.5h 观景 → 0.3h 不越界）",
     poi_db.CYCLE_DUR_MIN_H <= poi_db.cycle_duration_h(tiny) <= poi_db.CYCLE_DUR_MAX_H,
     f"{poi_db.cycle_duration_h(tiny)}")
huge = _p("H1", "超长点", "culture", 6.0)
case("B3 上限压制（6h 文化 → ≤1.2h）",
     poi_db.cycle_duration_h(huge) <= poi_db.CYCLE_DUR_MAX_H,
     f"{poi_db.cycle_duration_h(huge)}")
case("B4 结果已 round（无浮点毛刺）",
     poi_db.cycle_duration_h(_p("R1", "x", "view", 1.5)) == 0.3)

# ---- C：全天大点不缩 ----
disney = _p("D1", "迪士尼", "family", 8.0)
case("C1 duration_h=8 的全天大点不被缩",
     poi_db.cycle_duration_h(disney) == 8.0, f"{poi_db.cycle_duration_h(disney)}")

# ---- D：非骑行 mode 零回归 ----
city_real = poi_db.load_city("苏州")
raw = poi_db.parse_all(city_real, None)
same = all(raw[p["id"]]["dur"] == float(p["duration_h"]) for p in city_real["pois"])
case("D1 mode=None：dur == duration_h 逐点相等（零回归）", same)
case("D2 mode=None 与 mode='driving' 的 dur 完全一致（只 cycling 变）",
     all(poi_db.parse_poi(p, city_real, "driving")["dur"] == float(p["duration_h"])
         for p in city_real["pois"]))

# ---- E：装载量提升 ----
ids = ["SZ042", "SZ058", "SZ060", "SZ064", "SZ067", "SZ068", "SZ070"]
cyc = poi_db.parse_all(city_real, "cycling")
o_old, _, _ = toptw.solve_day([raw[i] for i in ids], ids, city_real, raw, mode="cycling")
o_new, _, _ = toptw.solve_day([cyc[i] for i in ids], ids, city_real, cyc, mode="cycling")
case("E1 骑行口径装载量 ≥5 点（景区口径仅 3-4）", len(o_new) >= 5,
     f"景区={len(o_old)} → 骑行={len(o_new)}")
case("E2 装载量确有提升（严格大于景区口径）", len(o_new) > len(o_old))

# ---- F：单日主题不缩 ----
SRC = open(os.path.join(ROOT, "src", "m2_planner.py"), encoding="utf-8").read()
case("F1 compose 对单日主题不缩 dur（None if _scoped else t_mode）",
     "None if _scoped else t_mode" in SRC)
q2 = "苏州2天，其中一天骑行环太湖，另一天逛园林"
case("F2 theme_scoped 对单日主题返回 True", sequencer.theme_scoped(q2) is True)

print("\n" + "=" * 56)
if FAIL:
    print(f"❌ 失败 {len(FAIL)} 项：" + "、".join(FAIL))
    sys.exit(1)
print("✅ 全部通过")
