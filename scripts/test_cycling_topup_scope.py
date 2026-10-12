# -*- coding: utf-8 -*-
"""第 3 层守卫：骑行薄天补强池按「当日环线邻域」而非「距市中心」过滤（2026-10-12）。

背景：骑行补强池此前一律按「距市中心 ≤12km」过滤。环湖/环岛骑行日的点本就
在远郊（29-43km），该过滤会把**全部**环湖点滤掉、只剩 61 个市区点 ⇒ 薄天被补进
拙政园/平江路，骑行日被改造成市区游（实测：明月湾薄天补强池里 6 个环湖点全被滤）。

修法：当当日已有点**全在远郊**（环湖/环岛特征）时，改用「距当日已有点 ≤CYCLE_LOOP_KM
(20km)」口径；否则维持原「距市中心 ≤12km」（市内骑行不受影响）。

本文件钉住：
  A. 常量与源码锚点（防摘除）
  B. 环湖场景：补强池含环湖点、不含市区点（口径语义）
  C. 市内骑行场景不受影响（仍按距市中心 12km）
  D. 邻域半径对环湖日放宽到 CYCLE_LOOP_KM（防止 10km 二次收窄）

用法：python scripts/test_cycling_topup_scope.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from src import poi_db, proposal_planner as pp  # noqa: E402

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


SRC = open(os.path.join(ROOT, "src", "proposal_planner.py"), encoding="utf-8").read()

# ---- A：源码锚点 ----
case("A1 常量 CYCLE_LOOP_KM = 20.0 已定义",
     "CYCLE_LOOP_KM = 20.0" in SRC)
case("A2 补强池含「当日已有点全在远郊」判定（far_loop）",
     "far_loop = bool(day_pts_c) and all(" in SRC)
case("A3 环湖口径用 CYCLE_LOOP_KM 邻域过滤",
     "<= CYCLE_LOOP_KM]" in SRC)
case("A4 邻域半径对环湖日放宽（_near_km = CYCLE_LOOP_KM）",
     "_near_km = CYCLE_LOOP_KM" in SRC)
case("A5 市内骑行仍按距市中心 12km（原口径保留）",
     'float(p.get("dist_center_km") or 0) <= 12.0]' in SRC)

# ---- B/C：口径语义（用真实苏州数据模拟）----
city = poi_db.load_city("苏州")
ALL = {p["id"]: p for p in (poi_db.parse_poi(x, city) for x in city["pois"])}
CENTER_KM = lambda p: float(p.get("dist_center_km") or 0)   # noqa: E731
FAR_BIG_KM = pp.FAR_BIG_KM
LOOP_KM = pp.CYCLE_LOOP_KM


def _pool(day_ids, rest_all):
    """复刻新的补强池过滤逻辑（与源码同口径）。"""
    day_pts = [ALL[i] for i in day_ids if i in ALL]
    far_loop = bool(day_pts) and all(CENTER_KM(p) > FAR_BIG_KM for p in day_pts)
    if far_loop:
        return [p for p in rest_all if min(
            (poi_db.haversine_km(p["lat"], p["lng"], q["lat"], q["lng"])
             for q in day_pts), default=1e9) <= LOOP_KM], far_loop
    return [p for p in rest_all if CENTER_KM(p) <= 12.0], far_loop


rest = [p for i, p in ALL.items() if i not in {"SZ067"}]

# B：环湖薄天（明月湾，距市中心 43km）
pool_b, far_b = _pool(["SZ067"], rest)
pool_ids = {p["id"] for p in pool_b}
case("B1 环湖薄天识别为 far_loop", far_b is True)
case("B2 环湖补强池**含**环湖点（石公山/林屋洞/缥缈峰）",
     {"SZ068", "SZ069", "SZ070"} <= pool_ids,
     f"池含 {len(pool_b)} 点")
urban_in_pool = [p["id"] for p in pool_b if CENTER_KM(p) <= FAR_BIG_KM]
case("B3 环湖补强池**不含**市区点（拙政园/平江路等）",
     len(urban_in_pool) == 0, f"误入市区点={urban_in_pool}")

# C：市内骑行薄天（留园，距市中心 4km）—— 应走原口径
pool_c, far_c = _pool(["SZ007"], rest)
case("C1 市内骑行薄天不走 far_loop（保持距市中心口径）", far_c is False)
c_ids = {p["id"] for p in pool_c}
case("C2 市内骑行补强池**不含**环湖点（石公山/明月湾）",
     "SZ068" not in c_ids and "SZ067" not in c_ids)
case("C3 市内骑行补强池含市区点", len(pool_c) > 20, f"{len(pool_c)} 点")

print("\n" + "=" * 56)
if FAIL:
    print(f"❌ 失败 {len(FAIL)} 项：" + "、".join(FAIL))
    sys.exit(1)
print("✅ 全部通过")
