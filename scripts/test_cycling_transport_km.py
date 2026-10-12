# -*- coding: utf-8 -*-
"""第 2+4 层守卫：骑行「长腿通行口径」+「里程预算」（2026-10-12，须成对）。

背景（两因必须成对改）：
  ② 长腿口径：`travel_hours(mode="cycling")` 原仅覆盖 ≤8km，>8km **回落驾车时长**
     ⇒ 环湖长腿低估 3 倍（西山岛→冲山岛 22.8km：驾车 54min vs 骑行 159min）
     ⇒ 求解器以为装得下、排序层再逐个判超时剔除。
  ④ 里程预算：`THEME_PROFILES` cycling 原 15km/天（直线口径），而环太湖 6 点线
     直线即 82km ⇒ 恒超预算、每次剔远点，与「骑行就是骑远」矛盾。
  只改 ② 会把点剔得更多（时间更真实、装载量下降）；只改 ④ 会塞进骑不完的线。
  ⇒ 成对改为 CYCLE_MAX_KM=25km、cap=60km/天。

本文件钉住：
  A. 常量锚点（防摘除 / 防单改一个）
  B. 长腿（15-25km）按骑行 12km/h 计（不再回落驾车值）
  C. 超长腿（>25km）仍回落车驾（拦住荒谬串联）
  D. 里程预算 60km：环太湖典型环线落在预算内（不再恒触发剔远点）
  E. 短腿（<8km）口径不变（零回归）
  F. 徒步/车驾口径不受影响

用法：python scripts/test_cycling_transport_km.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from src import poi_db, sequencer  # noqa: E402

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


# ---- A：常量锚点 ----
case("A1 CYCLE_MAX_KM = 25.0（第 2 层）", poi_db.CYCLE_MAX_KM == 25.0,
     f"={poi_db.CYCLE_MAX_KM}")
case("A2 骑行里程预算 = 60.0km/天（第 4 层）",
     sequencer.theme_km_cap("苏州2天骑行") == 60.0,
     f"={sequencer.theme_km_cap('苏州2天骑行')}")
case("A3 兼容函数 cycle_km_cap 与主口径一致（60，无人调用但不得漂移）",
     sequencer.cycle_km_cap("骑行") == 60.0)
case("A4 徒步预算未受影响（仍 8.0）", sequencer.theme_km_cap("徒步") == 8.0)
case("A5 自驾预算未受影响（仍 400）", sequencer.theme_km_cap("自驾") == 400.0)

city = poi_db.load_city("苏州")
cyc = poi_db.parse_all(city, "cycling")


def th_min(a, b, mode="cycling"):
    return poi_db.travel_hours(cyc[a], cyc[b], mode) * 60


# ---- B：长腿按骑行速度（不回落驾车）----
# 陆巷古村→渔洋山直线 16.5km（8<16.5<25）
km_ab = poi_db.haversine_km(cyc["SZ060"]["lat"], cyc["SZ060"]["lng"],
                            cyc["SZ058"]["lat"], cyc["SZ058"]["lng"])
expect_ab = km_ab * poi_db.CIRCUITY / poi_db.CYCLE_KMH * 60
case(f"B1 16.5km 长腿按骑行 12km/h 计（≈{expect_ab:.0f}min）",
     abs(th_min("SZ060", "SZ058") - expect_ab) < 1.0,
     f"实测 {th_min('SZ060', 'SZ058'):.0f}min（旧口径为缓存驾车 ~81min）")

# 太湖西山岛→冲山岛 22.8km（旧口径驾车 54min，骑行应为 159min）
km_c = poi_db.haversine_km(cyc["SZ042"]["lat"], cyc["SZ042"]["lng"],
                           cyc["SZ064"]["lat"], cyc["SZ064"]["lng"])
expect_c = km_c * poi_db.CIRCUITY / poi_db.CYCLE_KMH * 60
case(f"B2 22.8km 长腿不再回落驾车（实测 {th_min('SZ042', 'SZ064'):.0f}min，"
     f"骑行应为 {expect_c:.0f}min）",
     abs(th_min("SZ042", "SZ064") - expect_c) < 1.0)

# ---- C：超长腿仍回落车驾 ----
# 构造 >25km 两点
a = {"id": "X1", "lat": 31.0, "lng": 120.0, "open_h": 9.0, "close_h": 17.0, "dur": 1.0}
b = {"id": "X2", "lat": 31.5, "lng": 120.5, "open_h": 9.0, "close_h": 17.0, "dur": 1.0}
km_far = poi_db.haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])
case("C1 >25km 直线仍回落车驾口径（拦荒谬串联）",
     km_far > poi_db.CYCLE_MAX_KM
     and poi_db.travel_hours(a, b, "cycling") <= km_far * poi_db.CIRCUITY / poi_db.SPEED_KMH + 1e-6,
     f"直线 {km_far:.1f}km")

# ---- D：里程预算 60km 容纳环太湖典型环线 ----
ids6 = ["SZ042", "SZ058", "SZ060", "SZ064", "SZ067", "SZ068"]
pts = [cyc[i] for i in ids6]
ordered = sequencer.order_day(pts, city=city, mode="cycling")
km_loop = sum(poi_db.haversine_km(p["lat"], p["lng"], q["lat"], q["lng"])
              for p, q in zip(ordered, ordered[1:]))
case(f"D1 环太湖线直线里程 {km_loop:.0f}km 在 60km 预算内"
     f"（旧预算 15km 下恒超 ⇒ 每次剔远点）",
     km_loop <= 60.0, f"实测 {km_loop:.1f}km")
case("D2 旧预算 15km 确实装不下该环线（证明确需放宽）", km_loop > 15.0)

# ---- E：短腿零回归 ----
km_short = poi_db.haversine_km(cyc["SZ067"]["lat"], cyc["SZ067"]["lng"],
                               cyc["SZ068"]["lat"], cyc["SZ068"]["lng"])
case(f"E1 短腿（{km_short:.1f}km <8）口径不变（仍骑行 12km/h）",
     abs(th_min("SZ067", "SZ068")
         - km_short * poi_db.CIRCUITY / poi_db.CYCLE_KMH * 60) < 1.0)

# ---- F：其他 mode 不受影响 ----
case("F1 mode=None（车驾）口径不变（SPEED_KMH 18）",
     abs(poi_db.travel_hours(cyc["SZ060"], cyc["SZ058"], None)
         - max(poi_db.MIN_TRAVEL_H, 81.4 / 60.0)) < 0.02,   # 该腿有缓存值 81.4min
     "该腿走 travel_cache 驾车值")
case("F2 徒步口径不受 CYCLE_MAX_KM 影响",
     poi_db.travel_hours(cyc["SZ067"], cyc["SZ068"], "hiking") > 0)

print("\n" + "=" * 56)
if FAIL:
    print(f"❌ 失败 {len(FAIL)} 项：" + "、".join(FAIL))
    sys.exit(1)
print("✅ 全部通过")
