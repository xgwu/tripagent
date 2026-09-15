# -*- coding: utf-8 -*-
"""2026-09-15 日内空档治理：确定性复现/回归。

缺陷现象：时间轴上两块活动之间凭空少 75~135min，前端像「排程断了」。侦察结论——
**所有 ≥45min 空档都出现在正餐点之前**：旧写法 `start = max(t2, ws, p["open_h"])`
把餐厅钉死在 12:00/18:00 整点，10:45 到场也要干等 75min（南京科举博物馆→绿柳居、
北京故宫→四季民福、苏州琵琶语→朱鸿兴、武汉长江大桥→户部巷 同族）。

三处修复，本文各配「含对照」用例：
  1. `MEAL_EARLY_TOL_H=1.0` 餐窗提前容差（正餐点可早 1h 开吃）→ 消灭整点干等
  2. `scan_gaps` 空档扫描 + 分类（wait_open/wait_meal/free）→ 空档从"隐形"变可披露
  3. `_fill_wait_with_meal` 等待开门的硬空白里先吃饭 → 「2h15 空白 + 晚餐被判途中
     解决」双输 → 至少晚餐有着落

用法：python scripts/test_gap_manage.py
"""
import io, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src import poi_db, sequencer  # noqa: E402

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


CITY = poi_db.load_city("苏州")     # 只借 day_start/day_end/meal_slots 三个口径


def _poi(pid, name, cat, o, c, dur, lat=31.300, lng=120.600, **kw):
    d = {"id": pid, "name": name, "category": cat, "lat": lat, "lng": lng,
         "open": o, "close": c, "open_h": poi_db.hhmm_to_h(o), "close_h": poi_db.hhmm_to_h(c),
         "dur": dur, "open_h_raw": o, "closed_days": [], "tags": [], "best_time": None,
         "rating": 4.5}
    d.update(kw)
    return d


def _tl(pois, **kw):
    return sequencer._build_timeline(pois, CITY, 1, **kw)


def _row(tl, name):
    return next((s for s in tl["timeline"] if s.get("type") == "poi" and s.get("name") == name), None)


def _meal_rows(tl):
    return [s for s in tl["timeline"] if s.get("type") == "meal"]


# ==================== 用例 1：scan_gaps 纯函数（含单位口径回归） ====================
print("== 用例 1：scan_gaps 扫描/分类/文案格式")
_pois = {"A": _poi("A", "景点甲", "culture", "09:00", "17:00", 2.0)}
_t1 = [{"type": "poi", "id": "A", "name": "景点甲", "start": "16:00", "end": "18:00"},
       {"type": "poi", "id": "B", "name": "夜间点", "start": "18:00", "end": "19:30"}]
g1 = sequencer.scan_gaps(_t1, _pois)
case("1a 相邻无空档 → 不产生 gap", not g1, str(g1))

_t2 = [{"type": "poi", "id": "A", "name": "景点甲", "start": "15:00", "end": "16:00"},
       {"type": "hop", "name": "驾车", "start": "16:00", "end": "16:05", "min": 5, "km": 1.0},
       {"type": "poi", "id": "C", "name": "夜间点", "start": "17:00", "end": "18:00"}]
g2 = sequencer.scan_gaps(_t2, {"A": _pois["A"], "C": _poi("C", "夜间点", "nightlife", "17:00", "22:00", 1.0)})
case("1b 扣通行后 55min → 低于阈值不报", not g2, str(g2))

_t3 = [{"type": "poi", "id": "A", "name": "景点甲", "start": "15:00", "end": "16:00"},
       {"type": "hop", "name": "驾车", "start": "16:00", "end": "16:20", "min": 20, "km": 5.0},
       {"type": "poi", "id": "C", "name": "珠江夜游", "start": "19:00", "end": "20:30"}]
g3 = sequencer.scan_gaps(_t3, {"A": _pois["A"], "C": _poi("C", "珠江夜游", "nightlife", "19:00", "21:30", 1.5)})
print("  gaps:", g3)
case("1c 扣通行后 160min → 报 1 段", len(g3) == 1, str(len(g3)))
case("1d 归因为 wait_open（下一站 19:00 才开门）",
     bool(g3) and g3[0]["reason"] == "wait_open", str(g3 and g3[0]["reason"]))
# ⚠️ 单位口径回归（2026-09-15 踩坑）：scan_gaps 全程按**分钟**运算，早期误用 _fmt()
# （收小时 float）→ 16:20 被渲染成 "980:00"。文案必须是 HH:MM。
case("1e 文案时刻为 HH:MM（不是分钟数直译）",
     bool(g3) and g3[0]["start"] == "16:20" and g3[0]["end"] == "19:00",
     str(g3 and (g3[0]["start"], g3[0]["end"])))

_t4 = [{"type": "poi", "id": "A", "name": "景点甲", "start": "16:00", "end": "16:45"},
       {"type": "meal", "name": "晚餐", "start": "18:00", "end": "19:00"}]
g4 = sequencer.scan_gaps(_t4, _pois)
case("1f 餐点前空档归因为 wait_meal", bool(g4) and g4[0]["reason"] == "wait_meal",
     str([(x["start"], x["min"], x["reason"]) for x in g4]))


# ==================== 用例 2：餐窗提前容差（空档唯一成因） ====================
# 构型：09:00 开门、玩 1.75h（10:45 出来）→ 下一站是 11:00 开门的午餐餐厅。
# 旧口径 start=max(10:45, 12:00, 11:00)=12:00 → 干等 75min（复现缺陷）
# 新口径 start=max(10:45, 11:00, 11:00)=11:00 → 空档 15min（正常衔接）
print("\n== 用例 2：MEAL_EARLY_TOL_H —— 早到餐厅不再钉死 12:00 整点")
_cfg2 = [_poi("A", "博物馆甲", "culture", "09:00", "17:00", 1.75),
         _poi("F", "老字号饭店", "food", "11:00", "14:00", 1.0, lng=120.601, best_time="lunch")]

_tol = sequencer.MEAL_EARLY_TOL_H
tl_new = _tl(_cfg2)
f_new = _row(tl_new, "老字号饭店")
print("  修复后 timeline:", [(s.get("start"), s.get("name"), s.get("meal") or s.get("type"))
                            for s in tl_new["timeline"]])
print("  修复后 gaps:", tl_new["gaps"])
case("2a 餐厅 11:00 开吃（未被钉在 12:00）", bool(f_new) and f_new["start"] == "11:00",
     str(f_new and f_new["start"]))
case("2b 该天无 ≥60min 空档", not tl_new["gaps"], str(tl_new["gaps"]))

# 对照：把容差关掉 → 复现旧缺陷（干等 75min，并被 scan_gaps 抓到）
sequencer.MEAL_EARLY_TOL_H = 0.0
try:
    tl_old = _tl(_cfg2)
finally:
    sequencer.MEAL_EARLY_TOL_H = _tol
f_old = _row(tl_old, "老字号饭店")
print("  对照（容差=0）timeline:", [(s.get("start"), s.get("name"), s.get("meal") or s.get("type"))
                                   for s in tl_old["timeline"]])
print("  对照 gaps:", tl_old["gaps"])
case("2c 对照组餐厅被钉在 12:00（缺陷可复现）", bool(f_old) and f_old["start"] == "12:00",
     str(f_old and f_old["start"]))
case("2d 对照组产生 ≥60min 空档", bool(tl_old["gaps"]) and tl_old["gaps"][0]["min"] >= 60,
     str([(x["start"], x["min"]) for x in tl_old["gaps"]]))


# ==================== 用例 3：等待开门的硬空白里先吃饭 ====================
# 构型（空档治理实测场景）：白天长游 09:00-16:45 → 夜间点 19:00 才开门。
# 旧实现：16:45→19:00 的 2h15 空白无任何安排，晚餐在收尾补餐时因「已过窗尾」被判
# 「途中解决」→ 全天无晚餐。新实现：等待段内 18:00-19:00 补晚餐块，正好吃到开门。
print("\n== 用例 3：_fill_wait_with_meal —— 等待开门段先吃晚饭")
_cfg3 = [_poi("A", "长游景点", "culture", "08:00", "20:00", 7.75),
         _poi("N", "珠江夜游", "nightlife", "19:00", "21:30", 1.5, lng=120.601)]
tl3 = _tl(_cfg3)
rows3 = [(s.get("start"), s.get("end"), s.get("name"), s.get("meal") or s.get("type"))
         for s in tl3["timeline"]]
print("  timeline:", rows3)
meals3 = [s for s in tl3["timeline"] if s.get("type") == "meal"]
case("3a 等待段内补出晚餐块", any(s["name"] == "晚餐" for s in meals3), str(meals3))
case("3b 晚餐紧贴夜间点开门时刻（18:00-19:00）",
     any(s["name"] == "晚餐" and s["start"] == "18:00" and s["end"] == "19:00" for s in meals3),
     str([(s["start"], s["end"], s["name"]) for s in meals3]))
case("3c 该天有餐食安排（不是裸天）", bool(meals3) and tl3["violations"] == [],
     f"meals={len(meals3)} viol={tl3['violations']}")


# ==================== 用例 4：真实城市场景回归 ====================
# 说明：单层 _build_timeline 对「排不进餐窗的正餐 POI」报违规是**设计行为**（那是
# 修复链的输入，上层 build_itinerary 会剔除该点并补通用餐块）——此处不据此判失败，
# 只验本缺陷相关的两条：① 空档不凭空产生 ② 产生的空档都有可解释归因。
print("\n== 用例 4：真实构型回归（武汉/南京/苏州）")
for city_name, ids, label in (("武汉", ["WH001", "WH010", "WH020"], "武汉Day"),
                              ("南京", ["NJ001", "NJ030", "NJ031"], "南京Day"),
                              ("苏州", ["SZ006", "SZ005", "SZ021"], "苏州Day")):
    c = poi_db.load_city(city_name)
    allp = {p["id"]: p for p in (poi_db.parse_poi(x, c) for x in c["pois"])}
    use = [allp[i] for i in ids if i in allp]
    if not use:
        print(f"  (跳过 {label}：id 不存在)")
        continue
    tlr = sequencer._build_timeline(use, c, 1)
    for g in tlr["gaps"]:
        print(f"  {label} gap: {g['start']}-{g['end']} {g['min']}min {g['reason']} ({g['note']})")
    case(f"4 {label} 空档均有可解释归因（非排版黑洞）",
         all(g["reason"] in ("wait_open", "wait_meal", "free") for g in tlr["gaps"]),
         str([g["reason"] for g in tlr["gaps"]]))
    case(f"4 {label} 序内跳转为真实通行（空档文案为 HH:MM）",
         all(len(g["start"]) == 5 and g["start"][2] == ":" for g in tlr["gaps"]),
         str([g["start"] for g in tlr["gaps"]]))

print("\n" + ("全部用例通过" if not FAIL else f"失败用例：{FAIL}"))
sys.exit(1 if FAIL else 0)
