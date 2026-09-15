# -*- coding: utf-8 -*-
"""报障 15 确定性复现/回归：正餐 POI 与通用餐块的餐窗优先权。

机制：_build_timeline 单趟遍历里，通用餐块（到达补餐/游完就地补餐）先占窗并
used_meals.add(key)，晚于它的正餐 POI 只能退到另一窗；等待超 MAX_MEAL_WAIT_H
即判违规 → 被修复链剔除 → 前端显示「美食POI未能安排进用餐时段」。

用法：python scripts/test_food_window_priority.py
"""
import io, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src import poi_db, sequencer  # noqa: E402

FAIL = []


def _run(city_name: str, ids: list, date0: str | None = None):
    city = poi_db.load_city(city_name)
    all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}
    it = sequencer.build_itinerary({1: ids}, city, all_pois, order_given=True, date0=date0)
    return it["days"][0], all_pois


def _meals(tl):
    return {(s.get("meal") or s["name"]): s for s in tl["timeline"] if s.get("type") == "meal"}


def _food_in_tl(tl, name):
    return next((s for s in tl["timeline"]
                 if s.get("type") == "poi" and s.get("name") == name), None)


def _dropped_names(tl):
    return [d["name"] for d in tl.get("dropped", [])]


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


print("== 用例 1：苏州 Day1（拙政园→苏博→狮子林→平江路→琵琶语 + 双塔市集正餐）")
tl, _ = _run("苏州", ["SZ001", "SZ002", "SZ003", "SZ004", "SZ049", "SZ015"])
tz = _food_in_tl(tl, "双塔市集")
print("  timeline:", [(s.get("start"), s.get("name"), s.get("meal") or s.get("type"))
                      for s in tl["timeline"]])
print("  dropped :", _dropped_names(tl), "| viol:", [v["reason"][:30] for v in tl["violations"]])
case("1a 双塔市集未被剔除", "双塔市集" not in _dropped_names(tl), f"dropped={_dropped_names(tl)}")
case("1b 双塔市集落在午餐窗", bool(tz) and tz.get("meal") == "lunch", str(tz))

print("\n== 用例 2：上海 Day2（自然博物馆→南京路→外滩 + 泰康食品/大壶春两家正餐）")
tl2, _ = _run("上海", ["SH008", "SH003", "SH001", "SH052", "SH021"])
print("  timeline:", [(s.get("start"), s.get("name"), s.get("meal") or s.get("type"))
                      for s in tl2["timeline"]])
print("  dropped :", _dropped_names(tl2), "| viol:", [v["reason"][:30] for v in tl2["violations"]])
tk, dh = _food_in_tl(tl2, "泰康食品（南京东路店）"), _food_in_tl(tl2, "大壶春（四川中路店）")
case("2a 两家正餐均落地", bool(tk) and bool(dh), f"泰康={bool(tk)} 大壶春={bool(dh)}")
case("2b 分别占午餐/晚餐窗", bool(tk) and bool(dh) and {tk.get("meal"), dh.get("meal")} == {"lunch", "dinner"},
     f"泰康={tk and tk.get('meal')} 大壶春={dh and dh.get('meal')}")

print("\n== 用例 3（对照）：无正餐点 → 通用餐块照常插入")
tl3, _ = _run("苏州", ["SZ001", "SZ002", "SZ003"])
m3 = _meals(tl3)
print("  timeline:", [(s.get("start"), s.get("name"), s.get("meal") or s.get("type"))
                      for s in tl3["timeline"]])
case("3a 午餐通用块存在", "午餐" in m3, str(list(m3)))
case("3b 无违规", not tl3["violations"], str(tl3["violations"]))

print("\n== 用例 4：正餐点确实排不进时 → 仍剔除且通用餐块兜底")
# 苏州 Day2 现实构型：虎丘(2.5h)+山塘街(2h) 后再去观前街正餐，午餐窗已过、晚餐太远
tl4, _ = _run("苏州", ["SZ006", "SZ005", "SZ021"])
print("  timeline:", [(s.get("start"), s.get("name"), s.get("meal") or s.get("type"))
                      for s in tl4["timeline"]])
print("  dropped :", _dropped_names(tl4), "| viol:", [v["reason"][:34] for v in tl4["violations"]])
case("4a 松鹤楼落地或被明确披露剔除",
     bool(_food_in_tl(tl4, "松鹤楼（观前店）")) or "松鹤楼（观前店）" in _dropped_names(tl4),
     f"落地={bool(_food_in_tl(tl4, '松鹤楼（观前店）'))} dropped={_dropped_names(tl4)}")
case("4b 无残留违规（修复链收敛）", not tl4["violations"], str(tl4["violations"]))

print("\n== 用例 5：挤爆型构型（虎丘→山塘街→琵琶语 + 观前街正餐排在最末）")
tl5, _ = _run("苏州", ["SZ006", "SZ005", "SZ049", "SZ021"])
print("  timeline:", [(s.get("start"), s.get("name"), s.get("meal") or s.get("type"))
                      for s in tl5["timeline"]])
print("  dropped :", _dropped_names(tl5), "| viol:", [v["reason"][:34] for v in tl5["violations"]])
case("5a 无残留违规（修复链收敛）", not tl5["violations"], str(tl5["violations"]))
case("5b 该天仍有餐食安排（餐块或美食落位）",
     bool(_meals(tl5)) or bool(_food_in_tl(tl5, "松鹤楼（观前店）")),
     f"meals={list(_meals(tl5))} 松鹤楼={bool(_food_in_tl(tl5, '松鹤楼（观前店）'))}")

print("\n== 不变量：以上各天都不得出现「既无餐块也无美食落位」的裸天")
for label, tl in [("用例1 苏州Day1", tl), ("用例2 上海Day2", tl2), ("用例3 苏州对照", tl3),
                  ("用例4 苏州", tl4), ("用例5 苏州", tl5)]:
    has_meal = bool(_meals(tl)) or any(s.get("meal") for s in tl["timeline"])
    case(f"不变量 {label} 有餐食安排", has_meal,
         f"meals={list(_meals(tl))} foods={[s['name'] for s in tl['timeline'] if s.get('meal')]}")

print("\n" + ("全部用例通过" if not FAIL else f"失败用例：{FAIL}"))
sys.exit(1 if FAIL else 0)
