# -*- coding: utf-8 -*-
"""M2 主规划器：检索 → LLM 库内选择 → TOPTW 求解 → 文案重生成。

与 M1 的差异：
- 排序由 OR-Tools TOPTW 完成（硬约束在求解器内保证），贪婪重排/剔除修复只作降级兜底
- 候选池 = LLM 主选（高利润）+ 地理邻近备选（低利润），求解器可在池内「换点」
- 修复后由 LLM 重生成文案，对齐最终时间轴
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import poi_db, retrieval, sequencer, llm_client, m1_planner, toptw
from src import hotel as hotel_mod


def _build_day_pool(mains: list, cands: list, used_all: set, used_backup: set,
                    radius_km: float = 8.0, n_backups: int = 8) -> list:
    """单日候选池：LLM 主选 + 地理邻近备选（半径/数量可调，供调参复用）。"""
    from src.poi_db import haversine_km
    cx = sum(p["lat"] for p in mains) / len(mains)
    cy = sum(p["lng"] for p in mains) / len(mains)
    backups = []
    for p in cands:
        if p["id"] in used_all or p["id"] in used_backup:
            continue
        if haversine_km(p["lat"], p["lng"], cx, cy) <= radius_km:
            backups.append(p)
    backups = sorted(backups, key=lambda p: -p["rating"])[:n_backups]
    return mains + backups


def plan(city: dict, query: str, days: int = 2, use_llm: bool = True,
         time_limit_s: float = toptw.TIME_LIMIT_S,
         main_bonus: float = toptw.MAIN_BONUS, soft_w: float = toptw.SOFT_W,
         date0: str | None = None, hotel_text: str | None = None) -> dict:
    # ---- 阶段1：复用 M1 的检索 + LLM 库内选择 ----
    r_m1 = m1_planner.plan(city, query, days, use_llm=use_llm, date0=date0,
                           hotel_text=hotel_text)
    hotel = hotel_mod.resolve_hotel(city, hotel_text)
    if not r_m1["mode"].startswith("llm"):
        r_m1["mode"] = r_m1["mode"] + " → m2_degraded"  # LLM 不可用，M2 无从谈起
        return r_m1

    llm_day_map = {d["day"]: [s["id"] for s in d["timeline"] if s["type"] == "poi"]
                   for d in r_m1["itinerary"]["days"]}
    themes = {d["day"]: {"theme": d.get("theme", ""), "reason": d.get("reason", "")}
              for d in r_m1["itinerary"]["days"]}
    meta = {"candidates": r_m1["candidates"], "invalid_poi_ids": r_m1["invalid_poi_ids"],
            "n_dup_across_days": r_m1.get("n_dup_across_days", 0), "hotel": r_m1.get("hotel")}
    anchor = hotel_mod.match_landmark_poi(city, hotel_text)  # 锚点地标 → TOPTW 必选（开关默认关）
    return compose(city, query, days, llm_day_map, themes, use_llm=use_llm,
                   date0=date0, hotel=hotel, time_limit_s=time_limit_s,
                   main_bonus=main_bonus, soft_w=soft_w, meta=meta, mode="m2_toptw",
                   forced_ids={anchor["id"]}
                   if (anchor and hotel_mod.hard_guarantee_enabled()) else None)


def compose(city: dict, query: str, days: int, day_map: dict, themes: dict,
            use_llm: bool = True, date0: str | None = None, hotel: dict | None = None,
            time_limit_s: float = toptw.TIME_LIMIT_S,
            main_bonus: float = toptw.MAIN_BONUS, soft_w: float = toptw.SOFT_W,
            meta: dict | None = None, mode: str = "m2_toptw",
            forced_ids: set | None = None) -> dict:
    """阶段2-4：逐日 TOPTW → 修复链 → 文案重生成。M2/M7 共用（M7 喂落地后的 day_map）。"""
    meta = meta or {}
    all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}
    cands = retrieval.recall(city, query)
    t0 = time.time()

    # ---- 阶段2：逐日 TOPTW（主选 + 地理邻近备选池）----
    final_day_map, solver_dropped, solved_days = {}, [], {}
    used_all = {i for ids in day_map.values() for i in ids}
    used_backup = set()
    n_mains = n_mains_kept = 0
    for d in sorted(day_map):
        mains = [all_pois[i] for i in day_map[d] if i in all_pois]
        if not mains:
            continue
        # M5 日期感知：求解池剔除当日闭馆 POI（主选闭馆 → 强制求解器换点）
        wd = poi_db.trip_weekday(date0, d) if date0 else None
        if wd:
            closed = {p["id"] for p in mains if wd in p.get("closed_days", [])}
            if closed:
                solver_dropped.extend({"id": x, "name": all_pois[x]["name"],
                                       "reason": f"当日闭馆（{wd}），求解器强制换点"}
                                      for x in closed)
            mains = [p for p in mains if wd not in p.get("closed_days", [])]
        if not mains:
            continue
        pool = [p for p in _build_day_pool(mains, cands, used_all, used_backup)
                if not wd or wd not in p.get("closed_days", [])]
        used_backup.update(p["id"] for p in pool
                           if p["id"] not in {m["id"] for m in mains})
        ordered, dropped, ok = toptw.solve_day(pool, day_map[d], city, all_pois,
                                               time_limit_s=time_limit_s,
                                               main_bonus=main_bonus, soft_w=soft_w,
                                               hotel=hotel,
                                               forced={i for i in day_map[d]
                                                       if i in (forced_ids or set())})
        if ok and ordered:
            final_day_map[d] = ordered
            solved_days[d] = True
            n_mains += len(day_map[d])
            n_mains_kept += sum(1 for i in day_map[d] if i in set(ordered))
            solver_dropped.extend({"id": x, "name": all_pois[x]["name"],
                                   "reason": "TOPTW 求解：时间预算内无法纳入（利润权衡）"}
                                  for x in dropped)
        else:  # 求解失败 → M1 贪婪链路兜底
            final_day_map[d] = day_map[d]
            solved_days[d] = False

    llm_raw_violations = m1_planner._count_violations_before_repair(final_day_map, city, all_pois, date0, hotel)
    itin = sequencer.build_itinerary(final_day_map, city, all_pois, date0=date0, hotel=hotel, query=query)
    # 求解成功的日子理论上 0 违规；记录实际（含餐块偏移后的）违规
    n_viol_after_solver = sum(len(d["violations"]) for d in itin["days"] if solved_days.get(d["day"]))

    # ---- 阶段3：Agent Loop 闭环（LLM 反馈，仅当求解器剔除了点）----
    if solver_dropped:
        day_map2, subs = m1_planner._feedback_loop(city, cands, query, days,
                                                   final_day_map, solver_dropped, all_pois)
        if day_map2:
            itin2 = sequencer.build_itinerary(day_map2, city, all_pois, date0=date0, hotel=hotel, query=query)
            if itin2["total_violations"] == 0:
                itin2["substitutes"] = subs
                itin = itin2

    # ---- 阶段4：文案重生成（对齐最终时间轴）----
    reasons_regen = False
    if use_llm:
        regen, ok = _regen_reasons(city, query, itin, all_pois)
        if ok:
            # 逐键合并：重生成只覆盖 reason，保留原 theme
            for k, v in regen.items():
                themes[k] = {**themes.get(k, {}), **v}
            reasons_regen = True
    for d in itin["days"]:
        info = themes.get(d["day"], {})
        d["theme"], d["reason"] = info.get("theme", ""), info.get("reason", "")

    return {"mode": mode, "query": query, "days": days,
            "candidates": meta.get("candidates"), "invalid_poi_ids": meta.get("invalid_poi_ids"),
            "date0": date0, "n_dup_across_days": meta.get("n_dup_across_days", 0),
            "hotel": meta.get("hotel"),
            "llm_raw_violations": llm_raw_violations,
            "mains_kept": f"{n_mains_kept}/{n_mains}" if n_mains else "n/a",
            "toptw_solved_days": sum(1 for v in solved_days.values() if v),
            "toptw_dropped": solver_dropped,
            "violations_after_solver": n_viol_after_solver,
            "reasons_regen": reasons_regen,
            "latency_s": round(time.time() - t0, 1),
            "itinerary": itin}


REGEN_PROMPT = """以下是已通过约束校验的最终行程时间轴。请为每一天重写 2~3 句「选择理由」，
必须与时间轴完全一致（只能提到时间轴里实际存在的 POI），体现本地人的体验节奏。
用户需求：{query}

{timeline}

严格输出 JSON：{{"days": [{{"day": 1, "reason": "..."}}]}}"""


def _regen_reasons(city, query, itin, all_pois):
    try:
        lines = []
        for d in itin["days"]:
            seq = " → ".join(
                f'{s["name"]}（{s["start"]}-{s["end"]}）'
                if s["type"] == "poi" else f'{s["name"]}（{s["start"]}-{s["end"]}）'
                for s in d["timeline"])
            lines.append(f"Day {d['day']}：{seq}")
        raw = llm_client.chat([
            {"role": "system", "content": m1_planner.system_prompt(city)},
            {"role": "user", "content": REGEN_PROMPT.format(query=query, timeline="\n".join(lines))}],
            temperature=0.2, seed=42)
        parsed = llm_client.parse_json_safe(raw)
        out = {}
        for d in parsed.get("days", []):
            if d.get("reason"):
                out[d.get("day")] = {"reason": d["reason"]}
        return out, bool(out)
    except Exception:  # noqa
        return {}, False
