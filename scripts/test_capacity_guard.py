# -*- coding: utf-8 -*-
"""报障 14 修复 A/B/D 确定性单测（纯离线）：

A1) _far_big_point_regroup 容量预检：目标天装不下 → 不挪（错配点留在原天）；
A2) _crossday_rebalance 容量预检：目标天负载超 horizon → 不挪；
B)  骑行 query 补强池 dist≤12km 过滤（天文馆 70km 不得补入）；
helper) _day_horizon 与 toptw 同口径；_day_load 与探针口径一致。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import poi_db, proposal_planner, sequencer, m2_planner, toptw  # noqa: E402

city = poi_db.load_city("上海")
all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}

# ---- helper 口径 ----
h = m2_planner._day_horizon(city)
assert h == max(240, int((poi_db.hhmm_to_h(city["day_end"])
                          - poi_db.hhmm_to_h(city["day_start"])) * 60) - toptw.MEAL_BUFFER_MIN), \
    f"horizon 口径与 toptw 不一致: {h}"
load = m2_planner._day_load(["SH001", "SH003"], all_pois, "cycling")
dur_sum = sum(float(all_pois[i]["dur"]) * 60 for i in ("SH001", "SH003"))
leg = poi_db.travel_hours(all_pois["SH001"], all_pois["SH003"], "cycling") * 60
assert abs(load - (dur_sum + leg)) < 1e-6, f"_day_load 口径错误: {load} vs {dur_sum + leg}"
print(f"helper ✅ horizon={h}min  Day[SH001,SH003] 骑行负载={load:.0f}min")

# ---- B: 骑行补强过滤 ----
# 构造：Day1 只剩 1 个点（<MIN_STOPS=2 触发补强），骑行 query → 补进点必须 dist≤12km
dm = {1: ["SH038"]}  # 天文馆自身 70km（占位，补强不看它）
themes = {1: {"theme": "", "reason": ""}}
g = {"grounding_rate": 1.0, "gaps": []}
dm2, _t, _g = proposal_planner._post_ground_fixups(
    dict(dm), dict(themes), dict(g), city, all_pois, 1,
    "上海2日骑行，喜欢历史文化", None)
new_ids = [i for i in dm2[1] if i != "SH038"]
assert len(dm2[1]) >= 2, f"骑行薄天应被补强: {dm2[1]}"
far = [i for i in new_ids if float(all_pois[i].get("dist_center_km") or 0) > 12.0]
assert not far, f"骑行口径补进远郊点: {[all_pois[i]['name'] for i in far]}"
print(f"B ✅ 骑行补强 2 点全部 ≤12km（补入 {[all_pois[i]['name'] for i in new_ids]}）")

# ---- A1: regroup 容量预检 ----
# 构造：Day1 = 远郊大点（天文馆 4h/70km，触发 big 判定）+ 市区错配点（武康路）；
#        Day2 贪心装填大点到临界（自身 ≤horizon，但 +错配点即爆）→ 预检应挡住挪入。
big_far = all_pois["SH038"]
assert (float(big_far.get("dist_center_km") or 0) > 12.0
        and float(big_far.get("dur") or 0) >= 4.0), "fixture 需要天文馆远郊大点"
urban = all_pois["SH009"]  # 武康路：市区点、非 full_day、距天文馆 >10km
assert (float(urban.get("dist_center_km") or 0) <= 8.0
        and not sequencer.is_full_day(urban)
        and poi_db.haversine_km(urban["lat"], urban["lng"],
                                big_far["lat"], big_far["lng"]) > 10.0), "fixture urban 不合条件"
# 贪心装填：加满大 dur 非 full_day 点（自身不超 horizon），且 +urban+预留腿 即爆
big_durs = sorted([p for p in all_pois.values()
                   if p["id"] != "SH038" and p["id"] != "SH009"
                   and not sequencer.is_full_day(p)
                   and float(p.get("dur") or 0) >= 3.0],
                  key=lambda p: -float(p["dur"]))
day2_ids = []
for p in big_durs:
    cand = day2_ids + [p["id"]]
    load_cand = m2_planner._day_load(cand, all_pois, "cycling")
    if load_cand <= h and load_cand + float(urban["dur"]) * 60 + 60.0 > h:
        day2_ids = cand  # 加入后 urban 放不下 → 正是我们要的临界
        break
    if load_cand <= h - float(urban["dur"]) * 60 - 60.0:
        day2_ids = cand  # 还有余量，继续装
assert day2_ids, "fixture Day2 构造失败"
base_load_a1 = m2_planner._day_load(day2_ids, all_pois, "cycling")
assert base_load_a1 + float(urban["dur"]) * 60 > h, \
    f"fixture 不构成容量约束：{base_load_a1:.0f}+{urban['dur'] * 60:.0f} ≤ {h}"
dm_a1 = {
    1: ["SH038", "SH009"],
    2: list(day2_ids),
}
moves = proposal_planner._far_big_point_regroup(
    dict(dm_a1), all_pois, family=False, city=city,
    query="上海2日骑行，喜欢历史文化")
moved_urban = [m for m in moves if m["id"] == "SH009" and m["to"] == 2]
assert not moved_urban, f"容量预检失效：urban 被挪入爆容量天 {moved_urban}"
print(f"A1 ✅ Day2 负载 {base_load_a1:.0f}min +武康路({urban['dur']}h) 超限 → 未挪（moves={len(moves)}）")

# 对照：Day2 空闲时同样的错配应正常挪（预检不误伤）
dm_a1_free = {1: ["SH038", "SH009"], 2: ["SH036"]}  # 邮政博物馆 dur 小
moves_free = proposal_planner._far_big_point_regroup(
    dict(dm_a1_free), all_pois, family=False, city=city,
    query="上海2日骑行，喜欢历史文化")
assert any(m["id"] == "SH009" for m in moves_free), \
    f"预检误伤：空闲天也没接住错配点 {moves_free}"
print(f"A1 对照 ✅ 空闲天正常挪入武康路（moves={len(moves_free)}）")

# ---- A2: rebalance 容量预检 ----
# 构造：Day2 负载临界，Day1 的点离 Day2 簇更近（里程改善成立）但挪入即爆 → 不挪
# 用真实场景：Day1=[武康路,新天地]（西南），Day2=外滩+邮政+世博（东北，负载高）
day2_a2 = []
for p in big_durs:
    trial = day2_a2 + [p["id"]]
    if m2_planner._day_load(trial, all_pois, "cycling") <= h:
        day2_a2 = trial
    if len(day2_a2) >= 4:
        break
dm_a2 = {
    1: ["SH009", "SH012"],
    2: list(day2_a2),
}
base_load = m2_planner._day_load(day2_a2, all_pois, "cycling")
small_dur = min(float(all_pois[i]["dur"]) for i in ("SH009", "SH012"))
assert base_load + small_dur * 60 > h, \
    f"fixture 不构成容量约束：{base_load:.0f}+{small_dur * 60:.0f} ≤ {h}"
rebalanced, n_moves = m2_planner._crossday_rebalance(
    dict(dm_a2), city, all_pois, None, None, "上海2日骑行，喜欢历史文化")
moved_in = [i for i in rebalanced[2] if i not in day2_a2]
assert not moved_in, f"容量预检失效：Day2 被挪入 {moved_in}"
print(f"A2 ✅ Day2 负载 {base_load:.0f}min → rebalance 不挪入（moves={n_moves}）")

print("\n全部 4 组用例通过")
