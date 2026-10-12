# -*- coding: utf-8 -*-
"""软时间窗封顶修复守卫（2026-10-12）。

背景：`toptw.solve_day` 的 `best_time` 软窗「封顶」此前靠**收窄硬可行域**实现，
营业窗低于封顶线时硬窗被钳成单点，而软罚仍按全量偏差计 ⇒「下午早关门却标
evening」的点被误弃（实测 SZ067 明月湾古村，软罚 750 vs 利润 855 同量级）。

本文件钉住四条（既防复发，也防修过头）：
  A. 被误拒的点回归：evening + 下午早关门 → 应能排入（不再被软窗罚死）
  B. 对照锚点：同片区同营业窗、标 afternoon 的点行为不变
  C. 上界保护未被破坏：morning 点仍不会被拖到深夜（软上界封顶仍生效）
  D. 语义边界：软边界「越界则不设」——营业窗够得到建议时段时，软窗照常生效

用法：python scripts/test_soft_slot_cap.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from src import poi_db, toptw  # noqa: E402

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


def _p(pid, name, o, c, dur, bt, lat=31.25, lng=120.60, rating=4,
       category="culture"):
    return {"id": pid, "name": name, "open_h": o, "close_h": c, "dur": dur,
            "lat": lat, "lng": lng, "rating": rating, "best_time": bt,
            "category": category, "area": "central"}


CITY = {"day_start": "09:00", "day_end": "21:30",
        "center": {"lat": 31.25, "lng": 120.60}}

# ---- A：被误拒的点应回归 ----
# 「下午早关门却标 evening」：营业 08:30-16:30、dur 2h ⇒ 最晚到达 14:30（<17:00）
A_early = _p("A1", "早关门-earning", 8.5, 16.5, 2.0, "evening")
B_normal = _p("A2", "普通上午点", 8.0, 17.5, 1.5, "morning", lat=31.252, lng=120.602)
ALL = {p["id"]: p for p in (A_early, B_normal)}

o, d, _ = toptw.solve_day([A_early, B_normal], ["A1", "A2"], CITY, ALL)
case("A1 早关门+evening 点不再被软窗误弃（两点场景）", "A1" in o,
     f"ordered={o} dropped={d}")

# ---- B：对照——同营业窗标 afternoon 行为一致 ----
C_after = _p("B1", "早关门-afternoon", 8.5, 16.5, 2.0, "afternoon")
ALL_B = {p["id"]: p for p in (C_after, B_normal)}
o2, d2, _ = toptw.solve_day([C_after, B_normal], ["B1", "A2"], CITY, ALL_B)
case("B1 对照：同营业窗标 afternoon 同样能排入（两类标注行为一致）",
     "B1" in o2, f"ordered={o2}")

# ---- C：上界保护未破坏——morning 点不被拖到深夜 ----
M1 = _p("C1", "上午馆", 9.0, 17.0, 2.0, "morning")
M2 = _p("C2", "下午馆", 9.0, 17.0, 1.5, "afternoon", lat=31.252, lng=120.602)
NT = _p("C3", "夜市", 19.0, 23.0, 1.5, "evening", lat=31.251, lng=120.601,
        category="nightlife")
ALL_C = {p["id"]: p for p in (M1, M2, NT)}
oC, dC, _ = toptw.solve_day([M1, M2, NT], ["C1", "C2", "C3"], CITY, ALL_C)
case("C1 上午+下午+夜间 三点全排入（上界封顶不误伤夜间点）",
     len(oC) == 3, f"ordered={oC} dropped={dC}")
case("C2 nightlife 类目与晚开门点被 is_night_only 正确识别（真夜间点豁免）",
     poi_db.is_night_only(M1) is False and poi_db.is_night_only(NT) is True)

# ---- D：语义边界——软边界越界才跳过，够得到就必须设 ----
# 构造一个「营业窗够得到 evening（17:00）」的点：09:00-21:00、dur 1h ⇒ 最晚到达 20:00
D1 = _p("D1", "够得到evening", 9.0, 21.0, 1.0, "evening")
ALL_D = {p["id"]: p for p in (D1, B_normal)}


def _soft_bounds_set(p):
    """用同口径重算：该点的软下界是否与营业窗有交集（应设）。"""
    start = poi_db.hhmm_to_h(CITY["day_start"])
    hi = (p["close_h"] - start) * 60 - p["dur"] * 60
    sl = toptw.SLOT_WINDOWS[p["best_time"]][0]
    sl_m = (sl - start) * 60
    return sl_m - toptw.SOFT_CAP_MIN <= hi


case("D1 营业窗够得到 evening ⇒ 软下界应设（不被新逻辑跳过）", _soft_bounds_set(D1))
case("D2 早关门的 evening 点 ⇒ 软下界应跳过（越界）",
     not _soft_bounds_set(A_early))

# ---- E：源码锚点（防有人把修复摘掉）----
SRC = open(os.path.join(ROOT, "src", "toptw.py"), encoding="utf-8").read()
case("E1 源码含下界交集守卫（sl_m - SOFT_CAP_MIN <= hi）",
     "if sl_m - SOFT_CAP_MIN <= hi:" in SRC)
case("E2 源码含上界交集守卫（su_m + SOFT_CAP_MIN >= lo）",
     "if su_m + SOFT_CAP_MIN >= lo:" in SRC)

print("\n" + "=" * 56)
if FAIL:
    print(f"❌ 失败 {len(FAIL)} 项：" + "、".join(FAIL))
    sys.exit(1)
print("✅ 全部通过")
