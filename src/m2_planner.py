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


def _solve_all_days(city: dict, query: str, day_map: dict, all_pois: dict, cands: list,
                    alt_map: dict | None, forced_ids: set | None, date0: str | None,
                    hotel: dict | None, time_limit_s: float, main_bonus: float,
                    soft_w: float, mode: str | None = None) -> dict:
    """阶段2：逐日 TOPTW（主选 + LLM 备选 + 地理邻近备选池）。M2/M7/跨天重平衡共用。"""
    final_day_map, solver_dropped, solved_days = {}, [], {}
    mains_dropped = []  # 提案主选被求解器剔除（世界知识被时间预算否决——闭环信号）
    used_all = {i for ids in day_map.values() for i in ids}
    used_backup = set()
    n_mains = n_mains_kept = 0
    n_llm_alts = 0
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
                                       "day": d,
                                       "reason": f"当日闭馆（{wd}），求解器强制换点"}
                                      for x in closed)
            mains = [p for p in mains if wd not in p.get("closed_days", [])]
        if not mains:
            continue
        pool = [p for p in _build_day_pool(mains, cands, used_all, used_backup)
                if not wd or wd not in p.get("closed_days", [])]
        used_backup.update(p["id"] for p in pool
                           if p["id"] not in {m["id"] for m in mains})
        # LLM 备选并入池（低利润权重：不在 rank → 仅评分收益），供求解器换点
        pool_ids = {p["id"] for p in pool} | {m["id"] for m in mains}
        for i in (alt_map or {}).get(d, []):
            p = all_pois.get(i)
            if (p and i not in pool_ids and i not in used_all
                    and (not wd or wd not in p.get("closed_days", []))):
                pool.append(p)
                used_backup.add(i)
                n_llm_alts += 1
        ordered, dropped, ok = toptw.solve_day(pool, day_map[d], city, all_pois,
                                               time_limit_s=time_limit_s,
                                               main_bonus=main_bonus, soft_w=soft_w,
                                               hotel=hotel, mode=mode,
                                               forced={i for i in day_map[d]
                                                       if i in (forced_ids or set())})
        if ok and ordered:
            final_day_map[d] = ordered
            solved_days[d] = True
            kept = set(ordered)
            n_mains += len(day_map[d])
            n_mains_kept += sum(1 for i in day_map[d] if i in kept)
            for x in dropped:
                if x in day_map[d]:  # 主选被剔（备选池被剔是正常行为，不闭环）
                    mains_dropped.append({"id": x, "name": all_pois[x]["name"],
                                          "day": d,
                                          "reason": "TOPTW 求解：时间预算内无法纳入（利润权衡）"})
                solver_dropped.append({"id": x, "name": all_pois[x]["name"],
                                       "day": d,
                                       "reason": "TOPTW 求解：时间预算内无法纳入（利润权衡）"})
        else:  # 求解失败 → M1 贪婪链路兜底
            final_day_map[d] = day_map[d]
            solved_days[d] = False
    return {"final_day_map": final_day_map, "solver_dropped": solver_dropped,
            "mains_dropped": mains_dropped,
            "solved_days": solved_days, "n_mains": n_mains,
            "n_mains_kept": n_mains_kept, "n_llm_alts": n_llm_alts}


# ---- 阶段2.5：跨天重平衡（Google《Optimizing LLM-based trip planning》stage-2 局部搜索的轻量版）----
# 各日 TOPTW 独立求解后，个别 POI 可能落在地理上更邻另一天簇的位置；此处做确定性
# 「移动 POI 到更近的一天」局部搜索：0 违规 + 0 修复剔除 + 总里程改善超过阈值才接受，
# 并对每次移动扣相似度罚分（尊重 LLM 初稿，避免无意义搬运）。
MOVE_PENALTY_KM = 2.0   # 每次跨天移动的相似度惩罚（折算 km）
MIN_IMPROVE_KM = 0.5    # 接受移动所需的最小总里程改善
MAX_REBALANCE_SWEEPS = 2


def _crossday_rebalance(day_map: dict, city: dict, all_pois: dict, date0: str | None,
                        hotel: dict | None, query: str, forced_ids: set | None = None,
                        max_sweeps: int = MAX_REBALANCE_SWEEPS) -> tuple[dict, int]:
    """跨天局部搜索：把 POI 从当前天移动到使其总里程更小的天。

    约束：不动全天大点（is_full_day，主题锚）与 forced 锚点；供出点后天至少保留 1 点；
    接受条件 = 移动后全行程 0 违规、0 修复剔除，且总里程 + 罚分 < 原值 - MIN_IMPROVE_KM。
    返回 (新 day_map, 实际移动次数)。
    """
    days = sorted(day_map)
    if len(days) < 2:
        return day_map, 0
    forced = forced_ids or set()

    def metric(dm):
        itin = sequencer.build_itinerary(dm, city, all_pois, date0=date0, hotel=hotel,
                                         query=query)
        return (itin["total_travel_km"], itin["total_violations"], len(itin["dropped_pois"]))

    best_km, viol, n_drop = metric(day_map)
    if viol or n_drop:  # 已有违规/剔除 → 交由既有修复链处理，不在此折腾
        return day_map, 0
    n_moves = 0
    for _ in range(max_sweeps):
        improved = False
        for i in days:
            donor = day_map.get(i, [])
            movable = [pid for pid in donor
                       if pid not in forced and pid in all_pois
                       and not sequencer.is_full_day(all_pois[pid])]
            if len(donor) < 2 or not movable:
                continue
            for pid in movable:
                for j in days:
                    if j == i or not day_map.get(j):
                        continue
                    cand = {k: list(v) for k, v in day_map.items()}
                    cand[i].remove(pid)
                    cand[j].append(pid)
                    km, v2, nd2 = metric(cand)
                    if v2 == 0 and nd2 == 0 and km + MOVE_PENALTY_KM < best_km - MIN_IMPROVE_KM:
                        day_map = cand
                        best_km = km
                        n_moves += 1
                        improved = True
                        break
                if improved:
                    break
            if improved:
                break
        if not improved:
            break
    return day_map, n_moves


def compose(city: dict, query: str, days: int, day_map: dict, themes: dict,
            use_llm: bool = True, date0: str | None = None, hotel: dict | None = None,
            time_limit_s: float = toptw.TIME_LIMIT_S,
            main_bonus: float = toptw.MAIN_BONUS, soft_w: float = toptw.SOFT_W,
            meta: dict | None = None, mode: str = "m2_toptw",
            forced_ids: set | None = None, alt_map: dict | None = None) -> dict:
    """阶段2-4：逐日 TOPTW → 修复链 → 文案重生成。M2/M7 共用（M7 喂落地后的 day_map）。

    alt_map: {day: [poi_id]} LLM 备选（M7 提案 alternates）——并入当日求解池但
    不进主选 rank（低利润权重），求解器可在时间充裕/主选不可行时换入。
    """
    meta = meta or {}
    all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}
    cands = retrieval.recall(city, query)
    t_mode = sequencer.travel_mode(query)  # 骑行/徒步主题 → 求解器通行矩阵同步切换口径
    t0 = time.time()

    # ---- 阶段2：逐日 TOPTW（主选 + LLM 备选 + 地理邻近备选池）----
    res = _solve_all_days(city, query, day_map, all_pois, cands, alt_map, forced_ids,
                          date0, hotel, time_limit_s, main_bonus, soft_w, mode=t_mode)
    final_day_map = res["final_day_map"]
    solver_dropped, solved_days = res["solver_dropped"], res["solved_days"]
    mains_dropped = res["mains_dropped"]
    n_mains, n_mains_kept, n_llm_alts = res["n_mains"], res["n_mains_kept"], res["n_llm_alts"]

    # ---- 阶段2.5：跨天重平衡（确定性局部搜索，0 LLM 成本）----
    n_day_moves = 0
    if len(final_day_map) >= 2 and sum(solved_days.values()) == len(final_day_map):
        reb_map, n_day_moves = _crossday_rebalance(final_day_map, city, all_pois,
                                                   date0, hotel, query,
                                                   forced_ids=forced_ids)
        if n_day_moves:
            # 移动后整体重求解（备选已消费过，不再并入）；主选必须全保留才接受
            res2 = _solve_all_days(city, query, reb_map, all_pois, cands, None,
                                   forced_ids, date0, hotel, time_limit_s,
                                   main_bonus, soft_w, mode=t_mode)
            if res2["n_mains_kept"] == res2["n_mains"]:
                final_day_map = res2["final_day_map"]
                solver_dropped = res2["solver_dropped"]
                mains_dropped = res2["mains_dropped"]
                solved_days = res2["solved_days"]
                n_mains, n_mains_kept = res2["n_mains"], res2["n_mains_kept"]
            else:
                n_day_moves = 0  # 重求解掉点 → 放弃本次重平衡，沿用原解

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
            # 逐键合并：theme+reason 都以最终时间轴重生成结果为准
            for k, v in regen.items():
                themes[k] = {**themes.get(k, {}), **v}
            reasons_regen = True
    for d in itin["days"]:
        info = themes.get(d["day"], {})
        d["theme"], d["reason"] = info.get("theme", ""), info.get("reason", "")
        d["tips"] = info.get("tips", [])

    return {"mode": mode, "query": query, "days": days,
            "candidates": meta.get("candidates"), "invalid_poi_ids": meta.get("invalid_poi_ids"),
            "date0": date0, "n_dup_across_days": meta.get("n_dup_across_days", 0),
            "hotel": meta.get("hotel"),
            "llm_raw_violations": llm_raw_violations,
            "mains_kept": f"{n_mains_kept}/{n_mains}" if n_mains else "n/a",
            "n_llm_alts": n_llm_alts,
            "n_day_moves": n_day_moves,
            "toptw_solved_days": sum(1 for v in solved_days.values() if v),
            "toptw_dropped": solver_dropped,
            "mains_dropped": mains_dropped,
            "violations_after_solver": n_viol_after_solver,
            "reasons_regen": reasons_regen,
            "latency_s": round(time.time() - t0, 1),
            "itinerary": itin}


REGEN_PROMPT = """以下是已通过约束校验的最终行程时间轴。请为每一天重写「主题」、2~4 句「选点+排序理由」
和 1~3 条「tips」（实用建议），必须与时间轴完全一致（主题和理由都只能提到时间轴里实际存在的 POI），体现本地人的体验节奏。
注意：不要把时间轴里不存在的地点写进主题（例如时间轴没有宋城就不能叫「宋城怀古」）。
理由除说明为什么选这些点外，还须解释顺序逻辑——依据只能来自下方「排程事实」：
顺路串线、开门/用餐时间、离住宿远近、早到等待，不要编造事实之外的理由。
tips 要求：可操作的实用建议（早到避峰/排队策略/片区串玩/返程安排），依据只能是时间轴
事实、排程事实与常识性行前经验；禁止编造具体价格、电话、预约链接等无法核实的信息，
不写「建议查询官网」这类废话，每条不超过 30 字。
用户需求：{query}

{timeline}

{facts}

严格输出 JSON：{{"days": [{{"day": 1, "theme": "6~12字主题", "reason": "...", "tips": ["...", "..."]}}]}}"""


def _hm_min(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def _regen_reasons(city, query, itin, all_pois):
    try:
        lines, fact_lines = [], []
        for d in itin["days"]:
            seq = " → ".join(
                f'{s["name"]}（{s["start"]}-{s["end"]}）'
                for s in d["timeline"] if s["type"] != "hop")
            lines.append(f"Day {d['day']}：{seq}")
            hops = [s["name"] for s in d["timeline"] if s["type"] == "hop"]
            pois_seq = [s["name"] for s in d["timeline"] if s["type"] == "poi"]
            waits = [f'{s["name"]} 早到等待 {_hm_min(s["start"]) - _hm_min(s["arrive"])} 分钟'
                     for s in d["timeline"]
                     if s["type"] == "poi" and s.get("arrive")
                     and _hm_min(s["start"]) - _hm_min(s["arrive"]) > 0]
            if pois_seq:
                fact_lines.append(
                    f"Day {d['day']} 排程事实：首点 {pois_seq[0]}；末点 {pois_seq[-1]}；"
                    f"通行段 {'；'.join(hops) if hops else '无（单点）'}；收尾时刻 {d.get('finish')}"
                    + (f"；{'；'.join(waits)}" if waits else ""))
        raw = llm_client.chat([
            {"role": "system", "content": m1_planner.system_prompt(city)},
            {"role": "user", "content": REGEN_PROMPT.format(
                query=query, timeline="\n".join(lines), facts="\n".join(fact_lines))}],
            temperature=0.2, seed=42)
        parsed = llm_client.parse_json_safe(raw)
        out = {}
        for d in parsed.get("days", []):
            if d.get("reason"):
                # theme+reason+tips 都基于最终时间轴重生成（修复主题残留被剔主选点的问题）
                entry = {"reason": d["reason"]}
                t = d.get("theme")
                if isinstance(t, str) and t.strip():
                    entry["theme"] = t.strip()[:20]
                tips = d.get("tips")
                if isinstance(tips, list):
                    tips = [str(x).strip() for x in tips
                            if isinstance(x, str) and x.strip()][:3]
                    if tips:
                        entry["tips"] = tips
                out[d.get("day")] = entry
        return out, bool(out)
    except Exception:  # noqa
        return {}, False
