# -*- coding: utf-8 -*-
"""评测指标：借鉴 TripTailor 三维评估的可行版子集。"""
from . import poi_db, retrieval

# best_time 建议时段 → (最早到, 最晚到)；合规 = 实际到达时刻落在窗口内
SLOT_OK = {
    "morning":   (None, 12.0),
    "afternoon": (12.0, None),
    "evening":   (17.0, None),
    "lunch":     (11.0, 13.5),
}


def best_time_rate(itin: dict, all_pois: dict) -> str:
    """建议时段命中率：best_time != any 的 POI 中，实际到达时刻落在偏好窗口的比例。"""
    ok = total = 0
    for d in itin["days"]:
        for s in d["timeline"]:
            if s["type"] != "poi":
                continue
            bt = all_pois[s["id"]].get("best_time", "any")
            if bt not in SLOT_OK:
                continue
            total += 1
            lo, hi = SLOT_OK[bt]
            h = poi_db.hhmm_to_h(s["start"])
            if (lo is None or h >= lo) and (hi is None or h <= hi):
                ok += 1
    return f"{ok}/{total}" if total else "n/a"


def evaluate(result: dict, city: dict, query: str) -> dict:
    itin = result["itinerary"]
    # 1) 可行性：修复后硬约束违规
    violations = itin["total_violations"]
    # 2) 合理性：相邻 POI 平均距离（逐日）
    all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}
    dists = []
    per_day = []
    for d in itin["days"]:
        ids = [s["id"] for s in d["timeline"] if s["type"] == "poi"]
        km_day = []
        for a, b in zip(ids, ids[1:]):
            km = poi_db.haversine_km(all_pois[a]["lat"], all_pois[a]["lng"],
                                     all_pois[b]["lat"], all_pois[b]["lng"])
            dists.append(km)
            km_day.append(round(km, 1))
        per_day.append({"day": d["day"], "n_pois": len(ids), "adjacent_km": km_day,
                        "theme": d.get("theme", ""), "reason": d.get("reason", ""),
                        "tips": d.get("tips", []),
                        "travel_km": round(d["travel_km"], 1),
                        "finish": d["finish"], "reordered": d.get("reordered", False),
                        "repairs": d.get("repairs", 0)})
    avg_km = round(sum(dists) / len(dists), 1) if dists else 0.0
    # 2b) 合理性（M3 口径）：相邻 POI 平均路网通行时间（OSRM L1 缓存，与求解器目标对齐）
    mins = []
    for d in itin["days"]:
        ids = [s["id"] for s in d["timeline"] if s["type"] == "poi"]
        for a, b in zip(ids, ids[1:]):
            mins.append(poi_db.travel_hours(all_pois[a], all_pois[b]) * 60)
    avg_min = round(sum(mins) / len(mins)) if mins else 0
    # 3) 需求覆盖：查询意图标签在选中 POI 中的命中率
    qtags = retrieval.extract_query_tags(query)
    chosen_tags = set()
    for d in itin["days"]:
        for s in d["timeline"]:
            if s["type"] == "poi":
                chosen_tags.update(all_pois[s["id"]]["tags"])
                chosen_tags.add(all_pois[s["id"]]["category"])
    covered = [t for t in qtags if t in chosen_tags]
    # 成本估算（门票 + 两餐/天）
    tickets = sum(all_pois[s["id"]]["price"] for d in itin["days"]
                  for s in d["timeline"] if s["type"] == "poi")
    return {
        "mode": result["mode"],
        "hard_violations_final": violations,
        "dropped_pois": itin.get("dropped_pois", []),
        "llm_raw_violations": result.get("llm_raw_violations", 0),
        "invalid_poi_ids": result.get("invalid_poi_ids", []),
        "avg_adjacent_km": avg_km,
        "avg_adjacent_min": avg_min,
        "n_pois": sum(x["n_pois"] for x in per_day),
        "tag_coverage": f"{len(covered)}/{len(qtags)}" if qtags else "n/a",
        "best_time_rate": best_time_rate(itin, all_pois),
        "mains_kept": result.get("mains_kept", "n/a"),
        "est_cost_cny": tickets + result["days"] * (city["meal_cost"]["lunch"] + city["meal_cost"]["dinner"]),
        "latency_s": result["latency_s"],
        "candidates": result["candidates"],
        "per_day": per_day,
    }
