# -*- coding: utf-8 -*-
"""贪婪排序器 + 硬约束校验/修复 —— M1 的「轻量优化层」（非 TOPTW）。

输入：LLM（或基线）给出的每日 POI 集合
输出：按贪婪最近邻 + 建议时段重排后的时间轴，附约束校验报告
"""
from . import poi_db

DAY_END_SLOT = "21:30"


def _build_timeline(pois: list, city: dict, day_no: int, weekday: str | None = None,
                    hotel: dict | None = None) -> dict:
    """hotel：M6 住宿锚点 —— 每日从酒店出发、day_end 前返回酒店（虚拟节点，dur=0）。"""
    meals = city["meal_slots"]
    day_start = poi_db.hhmm_to_h(city["day_start"])
    day_end = poi_db.hhmm_to_h(city["day_end"])
    t = day_start
    timeline, travel_km, travel_h = [], 0.0, 0.0
    violations, repairs = [], 0
    prev = hotel  # 无酒店锚点时 prev=None，行为与 M5 完全一致
    meal_keys = {"lunch": poi_db.hhmm_to_h(meals["lunch"][0]),
                 "dinner": poi_db.hhmm_to_h(meals["dinner"][0])}
    used_meals = set()
    for p in pois:
        th = 0.0
        if prev is not None:
            th = poi_db.travel_hours(prev, p)
            km = poi_db.haversine_km(prev["lat"], prev["lng"], p["lat"], p["lng"])
            travel_h += th
            travel_km += km
            t += th
        arrive = t
        # 餐块：若到达时刻已跨过饭点且该餐未安排，先吃再逛（通行途中用餐）
        for key, mstart in meal_keys.items():
            if key not in used_meals and t >= mstart:
                timeline.append({"type": "meal", "name": "午餐" if key == "lunch" else "晚餐",
                                 "start": _fmt(t), "end": _fmt(t + 1.0)})
                t += 1.0
                used_meals.add(key)
        if t < p["open_h"]:
            t = p["open_h"]
        # M5：日期感知 —— 闭馆日为硬约束（违规会被二级修复剔除，保证最终行程不踩闭馆）
        if weekday and weekday in p.get("closed_days", []):
            violations.append({"poi": p["name"], "day": day_no,
                               "reason": f'当日闭馆（{"、".join(p.get("closed_days", []))}）'})
        if t + p["dur"] > p["close_h"] + 1e-9:
            violations.append({"poi": p["name"], "day": day_no,
                               "reason": f'到达{_fmt(t)}+{p["dur"]}h超出营业时间({p["open"]}-{p["close"]})'})
        if t + p["dur"] > day_end:
            violations.append({"poi": p["name"], "day": day_no,
                               "reason": f'超出当日活动时间上限 {city["day_end"]}'})
        timeline.append({"type": "poi", "id": p["id"], "name": p["name"],
                         "start": _fmt(t), "end": _fmt(t + p["dur"]),
                         "arrive": _fmt(arrive)})
        t += p["dur"]
        prev = p
    # M6：返程腿 —— day_end 前回到酒店（无酒店不约束）
    if hotel is not None and pois:
        th = poi_db.travel_hours(prev, hotel)
        travel_h += th
        travel_km += poi_db.haversine_km(prev["lat"], prev["lng"], hotel["lat"], hotel["lng"])
        t += th
        timeline.append({"type": "hotel", "name": "返回酒店",
                         "start": _fmt(t - th), "end": _fmt(t)})
        if t > day_end + 1e-9:
            violations.append({"poi": "返程", "day": day_no,
                               "reason": f'返回酒店时刻{_fmt(t)}超出当日活动时间上限 {city["day_end"]}'})
    return {"timeline": timeline, "travel_km": travel_km, "travel_h": travel_h,
            "violations": violations, "repairs": repairs, "finish": _fmt(t)}


def _fmt(h: float) -> str:
    return f"{int(h):02d}:{int(round((h - int(h)) * 60)):02d}"


def _day_score_key(p: dict):
    pref = {"morning": 0, "any": 1, "afternoon": 2, "evening": 3}.get(p.get("best_time", "any"), 1)
    return pref


def order_day(day_pois: list, start_poi=None, hotel=None) -> list:
    """贪婪最近邻 + 建议时段偏置：从早到晚排一条线（有酒店则从酒店出发选首点）。"""
    if not day_pois:
        return []
    remaining = list(day_pois)
    # 起点：给定锚点（酒店→最近点）；否则建议上午的、评分高的
    if hotel is not None:
        cur = min(remaining, key=lambda p: poi_db.travel_hours(hotel, p))
    else:
        cur = start_poi or max(remaining, key=lambda p: (p["rating"] - 3 * _day_score_key(p), p["rating"]))
    seq = [cur]
    remaining.remove(cur)
    while remaining:
        nxt = min(remaining, key=lambda p: (
            poi_db.travel_hours(cur, p) + 0.8 * abs(_day_score_key(p) - _day_score_key(cur)),
            -p["rating"]))
        seq.append(nxt)
        remaining.remove(nxt)
        cur = nxt
    return seq


def _repair_by_drop(day_pois: list, city: dict, day_no: int, max_drop: int = 3,
                    weekday: str | None = None, hotel: dict | None = None):
    """二级修复：重排后仍有违规 → 剔除肇事 POI（模拟 Agent Loop 的剔除+反馈）。"""
    pois = list(day_pois)
    dropped = []
    for _ in range(max_drop):
        tl = _build_timeline(order_day(pois, hotel=hotel), city, day_no, weekday, hotel)
        if not tl["violations"]:
            break
        bad_name = tl["violations"][-1]["poi"]
        bad = next((p for p in pois if p["name"] == bad_name), None)
        if bad is None and bad_name == "返程" and pois:
            # M6 修复链补洞：返程超时的「肇事点」不在列表里 → 剔除离酒店最远的点（返程腿最长的贡献者）
            bad = (max(pois, key=lambda p: poi_db.travel_hours(p, hotel))
                   if hotel is not None else pois[-1])
        if bad is None or len(pois) <= 1:
            break
        pois.remove(bad)
        dropped.append({"id": bad["id"], "name": bad["name"],
                        "reason": tl["violations"][-1]["reason"]})
    tl = _build_timeline(order_day(pois, hotel=hotel), city, day_no, weekday, hotel)
    tl["dropped"] = dropped
    return tl


def build_itinerary(day_map: dict, city: dict, all_pois: dict, order_given: bool = True,
                    date0: str | None = None, hotel: dict | None = None) -> dict:
    """day_map: {1: [poi_id,...], ...}  —— 尊重 LLM 给定顺序(order_given)，逐日排时序。

    date0: 行程起始日期（YYYY-MM-DD）——传入后按天推算星期，闭馆日作为硬约束校验/剔除。
    """
    result_days, all_violations, total_km = [], [], 0.0
    all_dropped = []
    for d in sorted(day_map):
        ids = day_map[d]
        pois = [all_pois[i] for i in ids if i in all_pois]
        wd = poi_db.trip_weekday(date0, d) if date0 else None
        seq = pois if order_given else order_day(pois, hotel=hotel)
        tl = _build_timeline(seq, city, d, wd, hotel)
        if tl["violations"]:
            # 一级修复：放弃原顺序，贪婪重排
            repaired = order_day(pois, hotel=hotel)
            tl2 = _build_timeline(repaired, city, d, wd, hotel)
            tl["repairs"] = len(tl["violations"])
            if len(tl2["violations"]) < len(tl["violations"]):
                tl2["reordered"] = True
                tl2["repairs"] = tl["repairs"]
                tl = tl2
            else:
                tl["reordered"] = False
            # 二级修复：重排无效说明总量超载 → 剔除肇事 POI
            if tl["violations"]:
                tl3 = _repair_by_drop(pois, city, d, weekday=wd, hotel=hotel)
                tl3["repairs"] = tl["repairs"]
                tl3["reordered"] = True
                tl = tl3
        all_violations.extend(tl["violations"])
        all_dropped.extend(tl.get("dropped", []))
        total_km += tl["travel_km"]
        result_days.append({"day": d, **tl})
    return {"days": result_days, "total_violations": len(all_violations),
            "total_travel_km": total_km, "dropped_pois": all_dropped}
