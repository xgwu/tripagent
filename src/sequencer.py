# -*- coding: utf-8 -*-
"""贪婪排序器 + 硬约束校验/修复 —— M1 的「轻量优化层」（非 TOPTW）。

输入：LLM（或基线）给出的每日 POI 集合
输出：按贪婪最近邻 + 建议时段重排后的时间轴，附约束校验报告
"""
import re

from . import poi_db

DAY_END_SLOT = "21:30"
MAX_MEAL_WAIT_H = 1.5     # 美食 POI 早到餐窗的最多等待时长，超过则判违规交修复链剔除
FOOD_PREF_WIN = {"lunch": "lunch", "dinner": "dinner", "evening": "dinner"}  # best_time → 首选餐窗

# ---- 主题画像：按出行方式设定每日交通里程预算（km），游玩≠拉练，超预算剔除远点 ----
# 优先级从上到下（一个查询命中多个主题时取最严格匹配项之前先按此序）
THEME_PROFILES = [
    ("cycling", re.compile(r"骑行|骑车|单车|自行车|cycling|bike", re.IGNORECASE), 15.0),
    ("hiking", re.compile(r"徒步|暴走|city\s*walk|遛弯", re.IGNORECASE), 8.0),
    ("family", re.compile(r"亲子|带.{0,4}(娃|孩子|小孩|儿童)|遛娃", re.IGNORECASE), 12.0),
]


def detect_theme(query: str | None) -> str | None:
    """从需求文字识别出行主题（cycling/hiking/family），无匹配返回 None。"""
    if not query:
        return None
    for name, pat, _cap in THEME_PROFILES:
        if pat.search(query):
            return name
    return None


def theme_km_cap(query: str | None) -> float | None:
    """需求命中主题画像 → 返回每日里程预算（km）；否则 None。"""
    if not query:
        return None
    for _name, pat, cap in THEME_PROFILES:
        if pat.search(query):
            return cap
    return None


def cycle_km_cap(query: str | None) -> float | None:
    """兼容保留：等价于 theme_km_cap（骑行命中即 15km，否则 None）。"""
    return 15.0 if query and _CYCLE_RE.search(query) else None


_CYCLE_RE = re.compile(r"骑行|骑车|单车|自行车|cycling|bike", re.IGNORECASE)


def _build_timeline(pois: list, city: dict, day_no: int, weekday: str | None = None,
                    hotel: dict | None = None) -> dict:
    """hotel：M6 住宿锚点 —— 每日从酒店出发、day_end 前返回酒店（虚拟节点，dur=0）。

    美食约束：category=food 的 POI 只能安排在用餐时段内（开吃时刻落在餐窗），
    且每个餐窗最多 1 个美食 POI（占用后该窗不再插入普通餐块）；排不进 → 违规跳过。
    """
    meals = city["meal_slots"]
    day_start = poi_db.hhmm_to_h(city["day_start"])
    day_end = poi_db.hhmm_to_h(city["day_end"])
    t = day_start
    timeline, travel_km, travel_h = [], 0.0, 0.0
    violations, repairs = [], 0
    prev = hotel  # 无酒店锚点时 prev=None，行为与 M5 完全一致
    meal_keys = {"lunch": poi_db.hhmm_to_h(meals["lunch"][0]),
                 "dinner": poi_db.hhmm_to_h(meals["dinner"][0])}
    meal_windows = {k: (poi_db.hhmm_to_h(meals[k][0]), poi_db.hhmm_to_h(meals[k][1]))
                    for k in meal_keys}
    used_meals = set()

    def _insert_generic_meals(cur_t):
        """普通餐块：到达时刻已跨过饭点且该餐未被占用（含美食 POI 占用）→ 插入。"""
        nonlocal t
        for key, mstart in meal_keys.items():
            if key not in used_meals and cur_t >= mstart:
                timeline.append({"type": "meal", "name": "午餐" if key == "lunch" else "晚餐",
                                 "start": _fmt(t), "end": _fmt(t + 1.0)})
                t += 1.0
                used_meals.add(key)

    for p in pois:
        is_last = p is pois[-1]
        if p.get("category") == "food":
            # ---- 美食 POI：必须落入未占用的餐窗 ----
            th = poi_db.travel_hours(prev, p) if prev is not None else 0.0
            t2 = t + th
            pref = FOOD_PREF_WIN.get(p.get("best_time"), "lunch")
            win_order = [pref] + [k for k in meal_keys if k != pref]
            for key in win_order:
                ws, we = meal_windows[key]
                if key in used_meals:
                    continue
                start = max(t2, ws, p["open_h"])  # 早于开门则顺延到开门
                if start > we + 1e-9:      # 已过该餐窗（开吃时刻晚于窗尾）
                    continue
                # 早到等待：日中最多等 MAX_MEAL_WAIT_H（防连锁推迟后续景点）；
                # 全天最后一个点不限（傍晚自由活动后吃晚餐是合理节奏）
                if start - t2 > MAX_MEAL_WAIT_H and not is_last:
                    continue
                if prev is not None:
                    travel_h += th
                    travel_km += poi_db.haversine_km(prev["lat"], prev["lng"], p["lat"], p["lng"])
                # 营业时间/闭馆日/当日上限校验（与非美食 POI 同一套）
                if weekday and weekday in p.get("closed_days", []):
                    violations.append({"poi": p["name"], "day": day_no,
                                       "reason": f'当日闭馆（{"、".join(p.get("closed_days", []))}）'})
                if start + p["dur"] > p["close_h"] + 1e-9:
                    violations.append({"poi": p["name"], "day": day_no,
                                       "reason": f'到达{_fmt(start)}+{p["dur"]}h超出营业时间({p["open"]}-{p["close"]})'})
                if start + p["dur"] > day_end:
                    violations.append({"poi": p["name"], "day": day_no,
                                       "reason": f'超出当日活动时间上限 {city["day_end"]}'})
                used_meals.add(key)  # 一个餐窗最多一个美食 POI，普通餐块也不再插
                timeline.append({"type": "poi", "id": p["id"], "name": p["name"],
                                 "start": _fmt(start), "end": _fmt(start + p["dur"]),
                                 "arrive": _fmt(t2), "meal": key})
                t = start + p["dur"]
                prev = p
                break
            else:
                violations.append({"poi": p["name"], "day": day_no,
                                   "reason": "美食POI未能安排进用餐时段（餐窗已被占用或早到等待超限）"})
                _insert_generic_meals(t)  # 该吃的饭照吃，只是这家店去不了
            continue

        # ---- 非美食 POI：原逻辑 ----
        if prev is not None:
            th = poi_db.travel_hours(prev, p)
            km = poi_db.haversine_km(prev["lat"], prev["lng"], p["lat"], p["lng"])
            travel_h += th
            travel_km += km
            t += th
        arrive = t
        # 餐块：若到达时刻已跨过饭点且该餐未安排，先吃再逛（通行途中用餐）
        _insert_generic_meals(t)
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


def order_day(day_pois: list, start_poi=None, hotel=None, city: dict | None = None) -> list:
    """贪婪最近邻 + 建议时段偏置：从早到晚排一条线（有酒店则从酒店出发选首点）。

    city 传入时启用餐窗感知：先排非美食线，再把美食 POI 插入到
    「绕行最小 + 最贴近其目标餐窗时刻」的位置（配合 _build_timeline 的美食硬约束）。
    """
    if not day_pois:
        return []
    foods = [p for p in day_pois if p.get("category") == "food"]
    rest = [p for p in day_pois if p.get("category") != "food"]

    def _greedy(pool: list) -> list:
        if not pool:
            return []
        remaining = list(pool)
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

    if city is None or not foods:
        return _greedy(day_pois)

    seq = _greedy(rest)
    meals = city["meal_slots"]
    win_mid = {k: (poi_db.hhmm_to_h(meals[k][0]) + poi_db.hhmm_to_h(meals[k][1])) / 2
               for k in ("lunch", "dinner")}
    # 美食点按目标餐窗先后插入（午市偏好先插，避免两个点抢同一窗）
    foods.sort(key=lambda p: (win_mid.get(FOOD_PREF_WIN.get(p.get("best_time"), "lunch"), 12.5), -p["rating"]))
    wins_left = list(win_mid)  # 尚未分配的餐窗
    day_start = poi_db.hhmm_to_h(city["day_start"])
    for f in foods:
        pref = FOOD_PREF_WIN.get(f.get("best_time"), "lunch")
        target_win = pref if pref in wins_left else (wins_left[0] if wins_left else pref)
        if target_win in wins_left:
            wins_left.remove(target_win)
        target = win_mid.get(target_win, 12.5)
        # 预计算序列各位置的到达时刻（day_start 起累计 travel+dur，跨过饭点补偿餐块 1h）
        t_est, times, prev_q = day_start, [day_start], None
        for q in seq:
            tt = poi_db.travel_hours(prev_q, q) if prev_q is not None else \
                (poi_db.travel_hours(hotel, q) if hotel is not None else 0)
            t_est += tt + q["dur"]
            for _ms in win_mid.values():  # 跨过饭点 → 排序器会插餐块，预估补偿
                if t_est - tt - q["dur"] < _ms <= t_est:
                    t_est += 1.0
            times.append(t_est)
            prev_q = q
        ws_t, we_t = (poi_db.hhmm_to_h(meals[target_win][0]), poi_db.hhmm_to_h(meals[target_win][1]))
        best_pos, best_cost = 0, float("inf")
        for pos in range(len(seq) + 1):
            a = seq[pos - 1] if pos > 0 else hotel
            b = seq[pos] if pos < len(seq) else None
            detour = poi_db.travel_hours(a, f) if a is not None else 0
            if b is not None and a is not None:
                detour += poi_db.travel_hours(f, b) - poi_db.travel_hours(a, b)
            arrive_est = times[pos] + (poi_db.travel_hours(a, f) if a is not None else 0)
            wait = max(0.0, ws_t - arrive_est)          # 早到等待
            late = max(0.0, arrive_est - we_t)          # 晚于窗尾（基本必被剔除）
            cost = detour + 0.6 * wait + 10.0 * late
            if wait > MAX_MEAL_WAIT_H and pos < len(seq):
                cost += 10.0  # 日中空等过久会被 _build_timeline 剔除，规避该位置
            if cost < best_cost:
                best_pos, best_cost = pos, cost
        seq.insert(best_pos, f)
    return seq


def _repair_by_drop(day_pois: list, city: dict, day_no: int, max_drop: int = 3,
                    weekday: str | None = None, hotel: dict | None = None):
    """二级修复：重排后仍有违规 → 剔除肇事 POI（模拟 Agent Loop 的剔除+反馈）。"""
    pois = list(day_pois)
    dropped = []
    for _ in range(max_drop):
        tl = _build_timeline(order_day(pois, hotel=hotel, city=city), city, day_no, weekday, hotel)
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
    tl = _build_timeline(order_day(pois, hotel=hotel, city=city), city, day_no, weekday, hotel)
    tl["dropped"] = dropped
    return tl


def _cap_km_repair(day_pois: list, city: dict, day_no: int, weekday: str | None,
                   hotel: dict | None, cap: float, theme: str | None = None):
    """主题里程预算修复：当日交通里程超预算 → 迭代剔除「最远腿」POI（贡献最长绕行的点）再重排。"""
    label = {"cycling": "骑行", "hiking": "徒步", "family": "亲子"}.get(theme, "骑行")
    pois = list(day_pois)
    dropped = []
    while len(pois) > 2:
        tl = _build_timeline(order_day(pois, hotel=hotel, city=city), city, day_no, weekday, hotel)
        if tl["travel_km"] <= cap:
            break

        def _far_leg(p, _pois=pois):
            others = [q for q in _pois if q is not p]
            return max((poi_db.haversine_km(p["lat"], p["lng"], q["lat"], q["lng"])
                        for q in others), default=0.0)
        bad = max(pois, key=_far_leg)
        pois.remove(bad)
        dropped.append({"id": bad["id"], "name": bad["name"],
                        "reason": f"{label}里程超预算（>{cap:.0f} km/天），剔除远点收敛路线"})
    tl = _build_timeline(order_day(pois, hotel=hotel, city=city), city, day_no, weekday, hotel)
    tl["dropped"] = dropped
    return tl


def build_itinerary(day_map: dict, city: dict, all_pois: dict, order_given: bool = True,
                    date0: str | None = None, hotel: dict | None = None,
                    query: str | None = None) -> dict:
    """day_map: {1: [poi_id,...], ...}  —— 尊重 LLM 给定顺序(order_given)，逐日排时序。

    date0: 行程起始日期（YYYY-MM-DD）——传入后按天推算星期，闭馆日作为硬约束校验/剔除。
    """
    result_days, all_violations, total_km = [], [], 0.0
    all_dropped = []
    cap = theme_km_cap(query)
    theme = detect_theme(query)
    for d in sorted(day_map):
        ids = day_map[d]
        pois = [all_pois[i] for i in ids if i in all_pois]
        wd = poi_db.trip_weekday(date0, d) if date0 else None
        seq = pois if order_given else order_day(pois, hotel=hotel, city=city)
        tl = _build_timeline(seq, city, d, wd, hotel)
        # 主题画像：先做每日里程预算收敛，再做硬约束修复（两者正交）
        if cap and tl["travel_km"] > cap and len(pois) > 2:
            tl = _cap_km_repair(pois, city, d, wd, hotel, cap, theme)
        if tl["violations"]:
            # 一级修复：放弃原顺序，贪婪重排
            repaired = order_day(pois, hotel=hotel, city=city)
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
