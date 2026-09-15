# -*- coding: utf-8 -*-
"""M2 主规划器：检索 → LLM 库内选择 → TOPTW 求解 → 文案重生成。

与 M1 的差异：
- 排序由 OR-Tools TOPTW 完成（硬约束在求解器内保证），贪婪重排/剔除修复只作降级兜底
- 候选池 = LLM 主选（高利润）+ 地理邻近备选（低利润），求解器可在池内「换点」
- 修复后由 LLM 重生成文案，对齐最终时间轴
"""
import os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import poi_db, retrieval, sequencer, llm_client, m1_planner, toptw
from src import hotel as hotel_mod

# ---- 贯穿性湖偏好（「湖边骑行/最好临湖」类 query）----
# 湖线点利润加成：量级取主选加成(600)的 1/4——足以扭转两个主选之间的取舍次序
# （时间预算不足时先剔非湖点），但不干预主选 vs 备选的 600 级大格局。
_LAKE_PREF_RE = re.compile(r"湖")
LAKE_BONUS = 150

# ---- 慢节奏档（「带老人/轮椅/行动不便/不要太累」类 query）----
# 宽松节奏是需求不是缺陷：提案每天 2-3 点、白天为主、傍晚不硬填点、单天补强降档。
# 正则定义在 sequencer（2026-09-14 迁移）：慢节奏餐窗提前量在排时层生效，
# planner 层 re-export 保持既有引用（proposal_planner 的 m2_planner.SLOW_PACE_RE）不破
from .sequencer import SLOW_PACE_RE  # noqa: E402


# ---- 忠实执行模式（toptw_faithful_mode，默认开）----
# 提案层 LLM 已完整理解节奏/强度意图（普通 4-6 点/慢节奏 2-3 点），TOPTW 层不再
# 自作主张加点/换点：求解池只含主选，主选以极大利润锁定（同 forced 口径），
# 求解器只优化顺序与可行性。点数收敛为「落地 ≤ 提案」，剔点必有 reason 可解释，
# 补位只走阶段3 _alt_substitute（净零换位），不做邻域捞点/LLM 兜底加点。
def faithful_mode_enabled() -> bool:
    return os.environ.get("TOPTW_FAITHFUL_MODE", "1").strip().lower() not in (
        "0", "false", "no", "off")


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
         date0: str | None = None, hotel_text: str | None = None,
         progress=None) -> dict:
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
                   progress=progress,
                   forced_ids={anchor["id"]}
                   if (anchor and hotel_mod.hard_guarantee_enabled()) else None)


def _solve_all_days(city: dict, query: str, day_map: dict, all_pois: dict, cands: list,
                    alt_map: dict | None, forced_ids: set | None, date0: str | None,
                    hotel: dict | None, time_limit_s: float, main_bonus: float,
                    soft_w: float, mode: str | dict | None = None,
                    reuse: dict | None = None) -> dict:
    """阶段2：逐日 TOPTW（主选 + LLM 备选 + 地理邻近备选池）。M2/M7/跨天重平衡共用。

    mode: 通行口径 —— str 全局生效；{day: mode} 单日主题（“其中一天骑行”仅承载天
    用主题口径，其余天车驾）；None 全车驾。
    reuse：{day: 上次求解后的有序 id 列表}——重平衡未触及的天直接复用原解，
    不再花 2s/天 重解（跨天重平衡只动少数天，全量重解是纯浪费）。
    """
    final_day_map, solver_dropped, solved_days = {}, [], {}
    mains_dropped = []  # 提案主选被求解器剔除（世界知识被时间预算否决——闭环信号）
    used_all = {i for ids in day_map.values() for i in ids}
    used_backup = set()
    n_mains = n_mains_kept = 0
    n_llm_alts = 0
    # 慢节奏（老人/轮椅/不累）：禁用邻域备选池——TOPTW 只能在提案点+LLM 备选内
    # 求解。否则池内自由换点会把 2-3 点的慢节奏天换/捞成 5-7 站大杂烩
    # （2026-09-14 case：提案 5 点落地 7 站排到 20:47）
    _slow = bool(SLOW_PACE_RE.search(query or ""))
    _faithful = faithful_mode_enabled()  # 忠实执行：池=主选，求解器只排序不做选点
    tasks = []  # (day, pool)：建池串行（维护 used_backup），求解并行
    for d in sorted(day_map):
        if reuse and reuse.get(d):
            final_day_map[d] = list(reuse[d])
            solved_days[d] = True
            n_mains += len(day_map.get(d) or [])
            n_mains_kept += len(reuse[d])  # 复用原解：主选全保留
            continue
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
        if _faithful:
            # 忠实执行模式：池=主选。邻域备选/LLM alts 均不进池——提案点数即承诺，
            # 求解器只排序；主选闭馆已在上面剔除，落选只可能是时间装不下，
            # 补位由阶段3 _alt_substitute 兜底（净零换位）
            pool = list(mains)
        else:
            pool = [p for p in _build_day_pool(mains, [] if _slow else cands,
                                               used_all, used_backup)
                    if not wd or wd not in p.get("closed_days", [])]
            used_backup.update(p["id"] for p in pool
                               if p["id"] not in {m["id"] for m in mains})
            # LLM 备选并入池（低利润权重：不在 rank → 仅评分收益），供求解器换点。
            # 慢节奏不并入：邻域池已禁，alts 再进池等于没禁——TOPTW 会从 4-8 个备选里
            # 把 2-3 点的慢节奏天自由换/捞成 5 站（2026-09-14 实证：沧浪亭/艺圃/留园
            # 全部从 alts 捞入）。剔点后的补位由 _alt_substitute 兜底（带 cap 与夜间过滤）
            if not _slow:
                pool_ids = {p["id"] for p in pool} | {m["id"] for m in mains}
                for i in (alt_map or {}).get(d, []):
                    p = all_pois.get(i)
                    if (p and i not in pool_ids and i not in used_all
                            and (not wd or wd not in p.get("closed_days", []))):
                        pool.append(p)
                        used_backup.add(i)
                        n_llm_alts += 1
        # 贯穿性湖偏好（「湖边骑行/最好临湖」类 query）：池内湖线点利润加成——
        # 时间预算不足时求解器优先剔非湖点，防止湖主题在剔点环节被市区点稀释
        # （2026-09-14 案例：提案两日均贴湖，落地后湖点被 TOPTW/补位换成
        # 麻雀咖啡/淮海街/平江路，用户报「和湖边没关系」）。dict 拷贝防污染 all_pois。
        if _LAKE_PREF_RE.search(query or ""):
            pool = [dict(p, _bonus=LAKE_BONUS) if poi_db.is_lake_poi(p) else p
                    for p in pool]
        tasks.append((d, pool))

    # P0 性能：各日求解相互独立，线程并行（OR-Tools routing 求解释放 GIL，
    # 实测 2 天 4s→2s）。建池/合并保持串行以维护 used_backup 等共享状态。
    def _solve_one(item):
        d, pool = item
        m = mode.get(d) if isinstance(mode, dict) else mode
        return d, toptw.solve_day(pool, day_map[d], city, all_pois,
                                  time_limit_s=time_limit_s,
                                  main_bonus=main_bonus, soft_w=soft_w,
                                  hotel=hotel, mode=m, lock_mains=_faithful,
                                  forced={i for i in day_map[d]
                                          if i in (forced_ids or set())})

    results = {}
    if tasks:
        if len(tasks) == 1:
            d, r = _solve_one(tasks[0])
            results[d] = r
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(len(tasks), 4)) as ex:
                for d, r in ex.map(_solve_one, tasks):
                    results[d] = r

    # 剔点 reason 忠实模式下区分口径：主选已锁定（极大利润），落选=物理装不下，
    # 不是利润权衡——闭环信号含义不同，文案必须可区分
    _drop_reason = ("TOPTW 求解：时间预算内无法纳入（忠实执行，主选锁定仍装不下）"
                    if _faithful else
                    "TOPTW 求解：时间预算内无法纳入（利润权衡）")
    for d in sorted(results):
        ordered, dropped, ok = results[d]
        if ok and ordered:
            final_day_map[d] = ordered
            solved_days[d] = True
            kept = set(ordered)
            n_mains += len(day_map[d])
            n_mains_kept += sum(1 for i in day_map[d] if i in kept)
            for x in dropped:
                if x in day_map[d]:  # 主选被剔（备选池被剔是正常行为，不闭环）
                    mains_dropped.append({"id": x, "name": all_pois[x]["name"],
                                          "day": d, "reason": _drop_reason})
                solver_dropped.append({"id": x, "name": all_pois[x]["name"],
                                       "day": d, "reason": _drop_reason})
        else:  # 求解失败 → M1 贪婪链路兜底
            final_day_map[d] = day_map[d]
            solved_days[d] = False
    return {"final_day_map": final_day_map, "solver_dropped": solver_dropped,
            "mains_dropped": mains_dropped,
            "solved_days": solved_days, "n_mains": n_mains,
            "n_mains_kept": n_mains_kept, "n_llm_alts": n_llm_alts,
            "faithful_mode": _faithful}


# ---- 阶段2.5：跨天重平衡（Google《Optimizing LLM-based trip planning》stage-2 局部搜索的轻量版）----
# 各日 TOPTW 独立求解后，个别 POI 可能落在地理上更邻另一天簇的位置；此处做确定性
# 「移动 POI 到更近的一天」局部搜索：0 违规 + 0 修复剔除 + 总里程改善超过阈值才接受，
# 并对每次移动扣相似度罚分（尊重 LLM 初稿，避免无意义搬运）。
MOVE_PENALTY_KM = 2.0   # 每次跨天移动的相似度惩罚（折算 km）
MIN_IMPROVE_KM = 0.5    # 接受移动所需的最小总里程改善
MAX_REBALANCE_SWEEPS = 2


def _day_horizon(city: dict) -> int:
    """TOPTW 同口径的每日时间预算（分钟）：日长 - 餐块预扣，保底 240。

    挪点容量预检必须与求解器同一口径，否则预检放行、TOPTW 仍剔（报障 14）。
    """
    start_day = poi_db.hhmm_to_h(city["day_start"])
    end_day = poi_db.hhmm_to_h(city["day_end"])
    return max(240, int((end_day - start_day) * 60) - toptw.MEAL_BUFFER_MIN)


def _day_load(ids: list, all_pois: dict, mode: str | None) -> float:
    """单日时间负载估算（分钟）：游玩 dur 合计 + 按给定序列的相邻腿时间。

    腿时间按序列相邻对累加（追加式，非最优插序）——估算偏保守（偏大），
    预检「宁严勿松」：预检放行后 TOPTW 仍可能微调顺序省出时间，反向则不行。
    """
    pts = [all_pois[i] for i in ids if i in all_pois]
    if not pts:
        return 0.0
    total = sum(float(p.get("dur") or 0) for p in pts) * 60
    total += sum(poi_db.travel_hours(a, b, mode) * 60 for a, b in zip(pts, pts[1:]))
    return total


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
    # 慢节奏（老人/轮椅/不累）：供出后天至少保留 3 站——补强凑起来的天被重平衡
    # 挪薄会退回「半天收工」（2026-09-14 偏薄反馈：Day3 补强 3 点被挪走 1 → 2 站）
    min_keep = 3 if SLOW_PACE_RE.search(query or "") else 1
    # 挪点容量预检（报障 14）：目标天负载（dur+腿，按 query 通行口径）超 TOPTW
    # horizon 就不挪——挪完重解必被「主选锁定仍装不下」成片剔除（骑行 Day2 塞 8 点剔 5 实证）。
    # 注：单日主题（"其中一天骑行"）天此处按全局口径近似，预检偏松由 TOPTW 兜底
    _t_mode = sequencer.travel_mode(query or "")
    _horizon = _day_horizon(city)

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
            if len(donor) < min_keep + 1 or not movable:
                continue
            for pid in movable:
                for j in days:
                    if j == i or not day_map.get(j):
                        continue
                    cand = {k: list(v) for k, v in day_map.items()}
                    cand[i].remove(pid)
                    cand[j].append(pid)
                    if _day_load(cand[j], all_pois, _t_mode) > _horizon:
                        continue  # 目标天装不下 → 不挪（TOPTW 剔点比里程差更伤）
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


# ---- 阶段3.4：美食配套绕行守门 ----
FOOD_DETOUR_MIN = 25  # food 点插入相邻点之间允许的最大绕行（分钟），超过即换/剔


def _fix_food_detours(day_map: dict, city: dict, all_pois: dict, date0: str | None,
                      hotel: dict | None, t_mode: str | None,
                      theme_day: int | None = None) -> tuple[dict, dict]:
    """美食/咖啡配套绕行守门：food 点在序列中造成大绕路 → 换成顺路同类店，无顺路替代则剔除。

    theme_day：单日主题承载天 —— 仅该天用主题通行口径评估绕行，其余天车驾。
    根因场景：世博文化公园 →（午间硬约束）→ 武康路 Arabica →（折返）→ 东方明珠，
    为一家网红咖啡店横穿城区。咖啡是配套不是目标——配套必须贴着当日主线路径。
    每次替换/剔除后全量走 sequencer 校验，保证只改善不变糟。
    返回 (更新后的 day_map, {day: [修正描述]}）。
    """
    from src.poi_db import haversine_km
    # 替代池：food 类 + 任何带「咖啡」tag 的场馆（咖啡街/艺术园区咖啡等均可顺路替代）
    def _is_coffee_venue(p):
        return p.get("category") == "food" or any("咖啡" in t for t in p.get("tags", []))
    food_pool = [p for p in all_pois.values()
                 if _is_coffee_venue(p) and not sequencer.is_full_day(p)]
    used = {pid for ids in day_map.values() for pid in ids}
    fixes: dict = {}
    for d in sorted(day_map):
        ids = list(day_map.get(d) or [])
        if len(ids) < 3:
            continue
        m_d = t_mode if (theme_day is None or d == theme_day) else None
        wd = poi_db.trip_weekday(date0, d) if date0 else None
        for _round in range(3):  # 最多 3 轮，每轮修一条最差绕行
            seq = [all_pois[i] for i in ids if i in all_pois]
            if len(seq) < 3:
                break
            # 前后邻点（含酒店锚点边界）
            worst = None  # (det_h, seq_idx, food_p, a, b)
            for k, p in enumerate(seq):
                if p.get("category") != "food":
                    continue
                if hotel is not None:
                    a = hotel if k == 0 else seq[k - 1]
                    b = hotel if k == len(seq) - 1 else seq[k + 1]
                else:
                    if k == 0 or k == len(seq) - 1:
                        continue  # 无酒店锚点时首/末位无完整前后邻点，不评估
                    a, b = seq[k - 1], seq[k + 1]
                det = (poi_db.travel_hours(a, p, m_d)
                       + poi_db.travel_hours(p, b, m_d)
                       - poi_db.travel_hours(a, b, m_d))
                if det * 60 > FOOD_DETOUR_MIN and (worst is None or det > worst[0]):
                    worst = (det, k, p, a, b)
            if worst is None:
                break
            det, k, f, a, b = worst
            # 顺路替代：同类 food、未用、当天不闭馆、经它绕行 ≤ 阈值；咖啡店优先匹配咖啡
            f_is_coffee = "咖啡" in f["name"] or any("咖啡" in t for t in f.get("tags", []))
            repls = []
            for p in food_pool:
                if p["id"] in used:
                    continue
                if wd and wd in p.get("closed_days", []):
                    continue
                d2 = (poi_db.travel_hours(a, p, m_d)
                      + poi_db.travel_hours(p, b, m_d)
                      - poi_db.travel_hours(a, b, m_d))
                if d2 * 60 > FOOD_DETOUR_MIN:
                    continue
                p_coffee = "咖啡" in p["name"] or any("咖啡" in t for t in p.get("tags", []))
                score = (-d2 + 0.05 * p["rating"]
                         + (0.3 if p_coffee == f_is_coffee else 0.0)
                         - 0.05 * haversine_km(f["lat"], f["lng"], p["lat"], p["lng"]))
                repls.append((score, p))
            replaced = False
            for _s, rep in sorted(repls, key=lambda x: -x[0])[:3]:
                trial = ids[:k] + [rep["id"]] + ids[k + 1:]
                it2 = sequencer.build_itinerary({d: trial}, city, all_pois, date0=date0,
                                                hotel=hotel, query=None)
                day2 = it2["days"][0]
                n2 = len([s for s in day2["timeline"] if s["type"] == "poi"])
                if (not day2["violations"] and not day2.get("dropped") and n2 == len(trial)):
                    ids = trial
                    used.add(rep["id"])
                    used.discard(f["id"])
                    fixes.setdefault(d, []).append(
                        f"{f['name']} → {rep['name']}（原店绕行 {int(round(det * 60))} 分钟）")
                    replaced = True
                    break
            if not replaced:  # 无顺路替代 → 剔除（配套宁缺毋滥）
                ids = ids[:k] + ids[k + 1:]
                used.discard(f["id"])
                fixes.setdefault(d, []).append(
                    f"{f['name']}（绕行 {int(round(det * 60))} 分钟且无顺路替代，剔除）")
        day_map[d] = ids
    return day_map, fixes


# ---- 阶段3.45：中间断档治理 ----
GAP_WAIT_MAX_H = 2.0     # 时间线内 poi 早到空跳（start-arrive）超过该值视为中间断档
# （原阶段3.5 日内填空 _fill_evenings 已移除（2026-09-15 用户指令）：傍晚空窗不再
#   自动补点——行程到收尾时刻自然结束，晚间安排交给提案层与用户自主调整。
#   其配套常量 FILL_SLACK_MIN_H/FILL_STOP_SLACK_H/FILL_MAX_PER_DAY/LATE_OPEN_H 一并删除，
#   「真夜间点」判定改由 poi_db.is_night_only（NIGHT_OPEN_H=16.5）承担。）


def _fix_gap_reorder(day_map: dict, city: dict, all_pois: dict,
                     date0: str | None, hotel: dict | None, query: str,
                     t_mode: str | None) -> tuple[dict, dict]:
    """中间断档治理：晚开点（夜游/夜市类 open 晚）排在序列前部时，sequencer 早到
    会空跳至开门时刻（如 11:30 结束→19:00 才有下一站，中间 7h 断档且 0 违规）。

    将造成断档的晚开点移到序列末尾压轴（夜间体验本就该收尾），移动后 0 违规
    且断档改善才接受；中间让出的空间由阶段 3.5 日内填空插点填满。
    返回 (更新后的 day_map, {day: [被移到末尾的点名]}）。
    """
    fixed: dict = {}
    for d in sorted(day_map):
        ids = list(day_map.get(d) or [])
        if len(ids) < 2:
            continue
        for _ in range(2):  # 最多治 2 个断档点
            it = sequencer.build_itinerary({d: ids}, city, all_pois, date0=date0,
                                           hotel=hotel, query=query)
            day = it["days"][0]
            if day["violations"] or day.get("dropped"):
                break
            # 扫描时间线：找第一个早到空跳 > 阈值的 poi（断档制造者）
            gap_id, gap_wait = None, 0.0
            for s in day["timeline"]:
                if s["type"] == "poi" and s.get("arrive"):
                    w = poi_db.hhmm_to_h(s["start"]) - poi_db.hhmm_to_h(s["arrive"])
                    if w > GAP_WAIT_MAX_H and w > gap_wait:
                        gap_id, gap_wait = s["id"], w
            if not gap_id or gap_id == ids[-1]:
                break  # 无断档，或断档点已在末尾（前面空间交给日内填空插点）
            ids2 = [i for i in ids if i != gap_id] + [gap_id]
            it2 = sequencer.build_itinerary({d: ids2}, city, all_pois, date0=date0,
                                            hotel=hotel, query=query)
            d2 = it2["days"][0]
            n2 = len([s for s in d2["timeline"] if s["type"] == "poi"])
            # 接受条件：0 违规 + 点一个不少 + 断档严格改善
            w2 = max((poi_db.hhmm_to_h(s["start"]) - poi_db.hhmm_to_h(s["arrive"]))
                     for s in d2["timeline"] if s["type"] == "poi" and s.get("arrive")) \
                if any(s["type"] == "poi" and s.get("arrive") for s in d2["timeline"]) else 0.0
            if (not d2["violations"] and not d2.get("dropped") and n2 == len(ids2)
                    and w2 < gap_wait):
                name = all_pois.get(gap_id, {}).get("name", gap_id)
                fixed.setdefault(d, []).append(name)
                ids = ids2
            else:
                break
        day_map[d] = ids
    return day_map, fixed


def _alt_substitute(day_map: dict, dropped: list, alt_map: dict | None,
                    all_pois: dict, date0: str | None, slow: bool = False):
    """补点闭环去 LLM 化第一层（P0）：优先用提案 alternates 确定性补位。

    alternates 本就是 LLM 为当天推荐的替补——求解器剔点后，从同天未用备选中
    取第一个（当天开馆、未被其他天占用）直接补位，省掉 ~5s 的 LLM 替代推荐调用。
    slow：慢节奏（老人/轮椅/不累）——真夜间备选（nightlife/晚开门，
    poi_db.is_night_only）不补，防止 evening 软时间窗在稀疏时间轴上拉出
    数小时空档；best_time=evening 全天开放点（外滩类）不算真夜间点。
    返回 (补位后 day_map 或 None, subs 记录, 仍无备选可补的剔点清单)。
    """
    day_map2 = {k: list(v) for k, v in day_map.items()}
    subs, rest = [], []
    used = {i for ids in day_map2.values() for i in ids}
    wd_by_day = ({d: poi_db.trip_weekday(date0, d) for d in day_map2}
                 if date0 else {})
    for dr in dropped:
        d, pid = dr.get("day"), dr["id"]
        # 慢节奏补位 cap：单天已满 4 点不再补位——老人行程宁缺勿堆
        # （提案 5 点 + TOPTW 剔 1 补 1 会把天数维持在高站位，节奏失控）
        if slow and len(day_map2.get(d, [])) >= 4:
            rest.append(dr)
            continue
        cand = [i for i in (alt_map or {}).get(d, [])
                if i not in used and i in all_pois
                and (not wd_by_day.get(d)
                     or wd_by_day[d] not in all_pois[i].get("closed_days", []))
                and not (slow and poi_db.is_night_only(all_pois[i]))]
        # 主题保真（P0）：优先选与被剔点标签相同的备选（如动物换动物、博物馆换博物馆），
        # 防止补位点类型漂移导致用户需求主题在行程中消失
        dtags = set(all_pois[pid].get("tags", [])) if pid in all_pois else set()
        alt = (next((i for i in cand if set(all_pois[i].get("tags", [])) & dtags), None)
               or (cand[0] if cand else None))
        if alt:
            day_map2[d].append(alt)
            used.add(alt)
            subs.append({"for": dr.get("name", ""), "poi_id": alt, "day": d,
                         "reason": "提案备选确定性补位（alternates，未消耗 LLM）"})
        else:
            rest.append(dr)
    return (day_map2 if subs else None), subs, rest


# 主题保真（P0）：值得向用户报告的「内容需求」标签白名单——
# 排除亲子/经典/轻松这类出行方式词（它们不是可被剔除的行程内容）
DEMAND_TAGS = {"动物", "博物馆", "寺庙", "历史", "文化", "自然", "美食",
               "夜景", "夜生活", "小吃", "夜市", "购物", "徒步", "茶文化", "文艺", "小众"}


def _demand_notices(query: str, dropped_recs: list, itin: dict, all_pois: dict) -> list:
    """需求满足检测：用户明确表达的内容需求标签，若因剔点在最终行程中消失 → 生成提示。

    dropped_recs: 各环节剔除记录合集（含 id/name）。返回 [{type,tag,dropped,message}]。
    """
    try:
        demand = [t for t in retrieval.extract_query_tags(query) if t in DEMAND_TAGS]
    except Exception:  # noqa: 提示生成失败不影响规划输出
        return []
    if not demand:
        return []
    kept_ids = {s.get("id") for d in itin.get("days", [])
                for s in d.get("timeline", []) if s.get("type") == "poi"}
    kept_tags = set()
    for i in kept_ids:
        if i in all_pois:
            kept_tags.update(all_pois[i].get("tags", []))
    notices = []
    for t in demand:
        if t in kept_tags:
            continue
        lost = [dr.get("name", "") for dr in dropped_recs
                if dr.get("id") not in kept_ids and dr.get("id") in all_pois
                and t in all_pois[dr["id"]].get("tags", [])]
        if lost:
            notices.append({"type": "demand_lost", "tag": t, "dropped": lost,
                            "message": f"「{t}」需求未满足：{'、'.join(dict.fromkeys(lost))} "
                                       f"已被剔除，行程中已无同主题点位"})
    return notices


def compose(city: dict, query: str, days: int, day_map: dict, themes: dict,
            use_llm: bool = True, date0: str | None = None, hotel: dict | None = None,
            time_limit_s: float = toptw.TIME_LIMIT_S,
            main_bonus: float = toptw.MAIN_BONUS, soft_w: float = toptw.SOFT_W,
            meta: dict | None = None, mode: str = "m2_toptw",
            forced_ids: set | None = None, alt_map: dict | None = None,
            progress=None, reuse_days: dict | None = None) -> dict:
    """阶段2-4：逐日 TOPTW → 修复链 → 文案重生成。M2/M7 共用（M7 喂落地后的 day_map）。

    alt_map: {day: [poi_id]} LLM 备选（M7 提案 alternates）——并入当日求解池但
    不进主选 rank（低利润权重），求解器可在时间充裕/主选不可行时换入。
    progress: 可选阶段回调 fn(stage:str)——P2 前端分阶段进度提示的数据源，
    取值 "solving"（TOPTW 求解）/ "regen"（文案重生成）；异常静默，不影响规划。
    reuse_days: {day: {"ids": [上次最终有序id], "copy": {theme,reason,tips}}}——
    C 闭环增量重算：点位集合未变的天直接复用原解与原文案，只重算变化的天。
    """
    meta = meta or {}

    def _report(stage: str) -> None:
        if progress:
            try:
                progress(stage)
            except Exception:  # noqa: 进度上报失败不拖垮规划
                pass

    all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}
    cands = retrieval.recall(city, query)
    t_mode = sequencer.travel_mode(query)  # 骑行/徒步主题 → 求解器通行矩阵同步切换口径
    # 单日主题（“其中一天骑行”）：仅承载天用主题通行口径，其余天保持车驾
    theme_day = sequencer.scoped_theme_day(day_map, all_pois, query)
    mode_map = ({d: (t_mode if d == theme_day else None) for d in day_map}
                if theme_day is not None else t_mode)
    t0 = time.time()
    _faithful = faithful_mode_enabled()  # 忠实执行模式（toptw_faithful_mode，默认开）

    # ---- 阶段2：逐日 TOPTW（主选 + LLM 备选 + 地理邻近备选池）----
    _report("solving")
    res = _solve_all_days(city, query, day_map, all_pois, cands, alt_map, forced_ids,
                          date0, hotel, time_limit_s, main_bonus, soft_w, mode=mode_map,
                          reuse={d: rec["ids"] for d, rec in (reuse_days or {}).items()
                                 if rec.get("ids")} or None)
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
            # 移动后整体重求解（备选已消费过，不再并入）；主选必须全保留才接受。
            # P0 性能：只重解点位集合变化的天，未触及的天直接复用原解
            moved = {d for d in reb_map
                     if {i for i in reb_map[d] if i in all_pois}
                     != {i for i in (final_day_map.get(d) or []) if i in all_pois}}
            reuse = ({d: ids for d, ids in final_day_map.items() if d not in moved}
                     if moved else None)
            res2 = _solve_all_days(city, query, reb_map, all_pois, cands, None,
                                   forced_ids, date0, hotel, time_limit_s,
                                   main_bonus, soft_w, mode=mode_map, reuse=reuse)
            if res2["n_mains_kept"] == res2["n_mains"]:
                final_day_map = res2["final_day_map"]
                # 剔点记录合并而非覆盖（报障 14）：第一轮剔点 + 重解轮剔点都是
                # 真实发生的求解行为，覆盖会让线上排查只见重解轮残缺记录。
                # 按 (id, day) 去重——挪天后同点重复被剔只记一次。
                _seen_drop = {(x["id"], x["day"]) for x in solver_dropped}
                for x in res2["solver_dropped"]:
                    if (x["id"], x["day"]) not in _seen_drop:
                        solver_dropped.append(x)
                _seen_main = {(x["id"], x["day"]) for x in mains_dropped}
                for x in res2["mains_dropped"]:
                    if (x["id"], x["day"]) not in _seen_main:
                        mains_dropped.append(x)
                solved_days = res2["solved_days"]
                n_mains, n_mains_kept = res2["n_mains"], res2["n_mains_kept"]
            else:
                n_day_moves = 0  # 重求解掉点 → 放弃本次重平衡，沿用原解




    llm_raw_violations = m1_planner._count_violations_before_repair(final_day_map, city, all_pois, date0, hotel)
    itin = sequencer.build_itinerary(final_day_map, city, all_pois, date0=date0, hotel=hotel, query=query)
    # 求解成功的日子理论上 0 违规；记录实际（含餐块偏移后的）违规
    n_viol_after_solver = sum(len(d["violations"]) for d in itin["days"] if solved_days.get(d["day"]))

    # ---- 阶段3：Agent Loop 闭环（仅当求解器剔除了点）----
    # P0 去 LLM 化：第一层用提案 alternates 确定性补位（0 成本）；
    # 仅当剔点无备选可补时，才走 LLM 替代推荐（~5s）兜底
    alt_sub_stat = {"hit": 0, "miss": 0, "rate": None}  # 补位命中率（P2 观测）
    if solver_dropped:
        day_map2, subs, rest = _alt_substitute(final_day_map, solver_dropped,
                                               alt_map, all_pois, date0,
                                               slow=bool(SLOW_PACE_RE.search(query or "")))
        # 补位命中率统计（P2 观测）：命中=备选确定性补位，miss=需 LLM 兜底
        alt_sub_stat = {"hit": len(subs), "miss": len(rest),
                        "rate": round(len(subs) / (len(subs) + len(rest)), 2)
                        if (subs or rest) else None}
        # 忠实执行模式跳过 LLM 兜底：兜底会向「补进点数最少的天」加点（净加点），
        # 违背「落地 ≤ 提案」承诺；剔点留给 REVISE 闭环 + notices 披露，
        # 补位只走上面已完成的 _alt_substitute 净零换位
        if rest and not _faithful:  # 无备选可补的剔点 → LLM 兜底（在已补位结果上继续补）
            day_map3, subs3 = m1_planner._feedback_loop(
                city, cands, query, days, day_map2 or final_day_map, rest, all_pois)
            if day_map3:
                day_map2, subs = day_map3, subs + subs3
        if day_map2:
            itin2 = sequencer.build_itinerary(day_map2, city, all_pois, date0=date0, hotel=hotel, query=query)
            if itin2["total_violations"] == 0:
                itin2["substitutes"] = subs
                itin = itin2

    # ---- 阶段3.4：美食配套绕行守门（确定性，0 LLM 成本）----
    # food 点（咖啡/网红店）造成大绕路 → 换顺路同类店或剔除；先于填空执行，
    # 剔除产生的空窗可由阶段3.5 补晚间点补偿
    final_day_map = {d["day"]: [s["id"] for s in d["timeline"] if s["type"] == "poi"]
                     for d in itin["days"]}
    final_day_map, food_fixes = _fix_food_detours(
        final_day_map, city, all_pois, date0, hotel, t_mode,
        theme_day=sequencer.scoped_theme_day(final_day_map, all_pois, query))
    if food_fixes:
        itin = sequencer.build_itinerary(final_day_map, city, all_pois, date0=date0,
                                         hotel=hotel, query=query)

    # ---- 阶段3.45：中间断档治理（确定性，0 LLM 成本）----
    # 晚开点（夜游/夜市 open 晚）排在序列前部 → sequencer 早到空跳至开门时刻，
    # 中间出现数小时断档（如 11:30 结束→19:00 才有下一站，0 违规静默通过）。
    # 将断档制造者移到序列末尾压轴，中间空间保持留白（原阶段3.5 填空已移除）。
    final_day_map = {d["day"]: [s["id"] for s in d["timeline"] if s["type"] == "poi"]
                     for d in itin["days"]}
    final_day_map, gap_fixes = _fix_gap_reorder(final_day_map, city, all_pois,
                                                date0, hotel, query, t_mode)
    if gap_fixes:
        itin = sequencer.build_itinerary(final_day_map, city, all_pois, date0=date0,
                                         hotel=hotel, query=query)

    # ---- 阶段4：文案重生成（对齐最终时间轴）----
    reasons_regen = False
    if use_llm:
        _report("regen")
        # C 闭环增量重算：reuse_days 中「修复链/填空后点位仍与上次一致」的天复用原文案，
        # 只对变化的天调 LLM（若全部未变则零调用）
        regen_only = None
        if reuse_days:
            now_ids = {d["day"]: [s["id"] for s in d["timeline"] if s["type"] == "poi"]
                       for d in itin["days"]}
            unchanged = {d: rec for d, rec in reuse_days.items()
                         if rec.get("ids") and now_ids.get(d) == list(rec["ids"])}
            for d, rec in unchanged.items():
                themes[d] = {**themes.get(d, {}), **rec["copy"]}
                reasons_regen = True
            regen_only = [d["day"] for d in itin["days"] if d["day"] not in unchanged]
            if not regen_only:
                regen_only = []  # 全部复用 → 零 LLM 调用
        regen, ok = _regen_reasons(city, query, itin, all_pois, only_days=regen_only)
        if ok:
            # 逐键合并：theme+reason 都以最终时间轴重生成结果为准
            for k, v in regen.items():
                themes[k] = {**themes.get(k, {}), **v}
            reasons_regen = True
    for d in itin["days"]:
        info = themes.get(d["day"], {})
        d["theme"], d["reason"] = info.get("theme", ""), info.get("reason", "")
        d["tips"] = info.get("tips", [])

    # ---- 阶段4.5：主题保真检测（P0）——需求标签的点被剔且行程再无同主题点 → 提示 ----
    _dropped_seen, dropped_recs = set(), []
    for dr in (mains_dropped + solver_dropped
               + [dr for d in itin["days"] for dr in d.get("dropped", [])]):
        if dr.get("id") and dr["id"] not in _dropped_seen:
            _dropped_seen.add(dr["id"])
            dropped_recs.append(dr)
    notices = _demand_notices(query, dropped_recs, itin, all_pois)

    return {"mode": mode, "query": query, "days": days,
            "candidates": meta.get("candidates"), "invalid_poi_ids": meta.get("invalid_poi_ids"),
            "date0": date0, "n_dup_across_days": meta.get("n_dup_across_days", 0),
            "hotel": meta.get("hotel"),
            "llm_raw_violations": llm_raw_violations,
            "mains_kept": f"{n_mains_kept}/{n_mains}" if n_mains else "n/a",
            "n_llm_alts": n_llm_alts,
            "n_day_moves": n_day_moves,
            "food_fixes": food_fixes,
            "toptw_solved_days": sum(1 for v in solved_days.values() if v),
            "toptw_dropped": solver_dropped,
            "mains_dropped": mains_dropped,
            "violations_after_solver": n_viol_after_solver,
            "reasons_regen": reasons_regen,
            "alt_sub": alt_sub_stat,
            "faithful_mode": _faithful,
            "notices": notices,
            "latency_s": round(time.time() - t0, 1),
            "itinerary": itin}


REGEN_PROMPT = """以下是已通过约束校验的最终行程时间轴。请为每一天重写「主题」、2~4 句「选点+排序理由」
和 1~3 条「tips」（实用建议），必须与时间轴完全一致（主题和理由都只能提到时间轴里实际存在的 POI），体现本地人的体验节奏。
注意：不要把时间轴里不存在的地点写进主题（例如时间轴没有宋城就不能叫「宋城怀古」）。
理由除说明为什么选这些点外，还须解释顺序逻辑——依据只能来自下方「排程事实」：
顺路串线、开门/用餐时间、离住宿远近、早到等待，不要编造事实之外的理由。
交通/车程细节由界面时间轴展示，文案（reason 和 tips）中禁止出现具体交通时长、
距离数字（如「车程45分钟」「9.5km」「步行15分钟」），只描述片区转移的顺序逻辑
（如「上午运河片区、午后转场西湖」）。
tips 要求：可操作的实用建议（早到避峰/排队策略/片区串玩/返程安排），依据只能是时间轴
事实、排程事实与常识性行前经验；禁止编造具体价格、电话、预约链接等无法核实的信息，
不写「建议查询官网」这类废话，每条不超过 30 字。
闭馆提示规则：涉及闭馆/营业时间的表述只能依据排程事实中给出的各点闭馆日数据，
禁止凭「博物馆周一闭馆」之类的常识假设生成闭馆提示（不少场馆闭馆日并非周一，
如上海城市规划展示馆为周三闭馆）——数据说当天开馆就不要写闭馆。
用户需求：{query}

{timeline}

{facts}

严格输出 JSON：{{"days": [{{"day": 1, "theme": "6~12字主题", "reason": "...", "tips": ["...", "..."]}}]}}"""


def _hm_min(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def _regen_reasons(city, query, itin, all_pois, only_days=None):
    """文案重生成（P1 改造：按天并行）——每天一次独立 LLM 调用，线程池并发。

    原「一天次合并调用」多天串在同一个 prompt 里，2.6s 起步且随天数增长；
    拆成逐天并行后多天耗时 ≈ 单天耗时（约 1~1.5s）。结果口径与守门逻辑不变。
    only_days: None=全部天；[]=不重生成（C 闭环全复用）；[1,2]=仅这些天。
    """
    try:
        day_ctxs = []  # (day, timeline_line, fact_block, closed_ctx)
        for d in itin["days"]:
            if only_days is not None and d["day"] not in only_days:
                continue
            seq = " → ".join(
                f'{s["name"]}（{s["start"]}-{s["end"]}）'
                for s in d["timeline"] if s["type"] != "hop")
            # 通行段带起止点：hop 夹在前后两个非 hop 条目之间，孤立的车程名
            # （如「车程 45 分钟」）不给端点，LLM 会猜错归属（把博物馆→午餐
            # 说成午餐后→下一景点）
            tl = d["timeline"]
            hops = []
            for i, s in enumerate(tl):
                if s["type"] != "hop":
                    continue
                prev_name = next((x["name"] for x in reversed(tl[:i])
                                  if x["type"] != "hop"), "出发")
                next_name = next((x["name"] for x in tl[i + 1:]
                                  if x["type"] != "hop"), "收尾")
                hops.append(f"{prev_name}→{next_name}：{s['name']}")
            pois_seq = [s["name"] for s in d["timeline"] if s["type"] == "poi"]
            waits = [f'{s["name"]} 早到等待 {_hm_min(s["start"]) - _hm_min(s["arrive"])} 分钟'
                     for s in d["timeline"]
                     if s["type"] == "poi" and s.get("arrive")
                     and _hm_min(s["start"]) - _hm_min(s["arrive"]) > 0]
            facts = []
            if pois_seq:
                facts.append(
                    f"Day {d['day']} 排程事实：首点 {pois_seq[0]}；末点 {pois_seq[-1]}；"
                    f"通行段 {'；'.join(hops) if hops else '无（单点）'}；收尾时刻 {d.get('finish')}"
                    + (f"；{'；'.join(waits)}" if waits else ""))
            # 闭馆日事实：给 LLM 权威数据，防止凭常识幻觉出「周一闭馆」类矛盾 tips
            closed_facts = [
                f'{s["name"]} 闭馆日为{"、".join(p["closed_days"])}'
                f'（当天{d["weekday"] or "未知"}）'
                for s in d["timeline"] if s["type"] == "poi"
                for p in [all_pois.get(s.get("id"), {})]
                if p.get("closed_days")]
            if closed_facts:
                facts.append(f"Day {d['day']} 闭馆数据：{'；'.join(closed_facts)}")
            closed_ctx = (d.get("weekday"),
                          {cd for s in d["timeline"] if s["type"] == "poi"
                           for cd in all_pois.get(s.get("id"), {}).get("closed_days", [])})
            day_ctxs.append((d["day"], seq, "\n".join(facts), closed_ctx))
        if not day_ctxs:
            return {}, False

        def _one(item):
            day, seq, facts, closed_ctx = item
            try:
                raw = llm_client.chat([
                    {"role": "system", "content": m1_planner.system_prompt(city)},
                    {"role": "user", "content": REGEN_PROMPT.format(
                        query=query, timeline=f"Day {day}：{seq}", facts=facts)}],
                    temperature=0.2, seed=42)
                parsed = llm_client.parse_json_safe(raw)
            except Exception:  # noqa: 单天失败不拖垮其他天
                return day, None
            days_out = parsed.get("days") or []
            d = next((x for x in days_out if x.get("day") in (day, None)), None)
            d = d or (days_out[0] if len(days_out) == 1 else None)
            if not (isinstance(d, dict) and d.get("reason")):
                return day, None
            # theme+reason+tips 都基于最终时间轴重生成（修复主题残留被剔主选点的问题）
            entry = {"reason": d["reason"]}
            t = d.get("theme")
            if isinstance(t, str) and t.strip():
                entry["theme"] = t.strip()[:20]
            tips = d.get("tips")
            if isinstance(tips, list):
                tips = [str(x).strip() for x in tips
                        if isinstance(x, str) and x.strip()][:3]
                # 确定性守门：提示「闭馆」但数据不支持 → 剔除该条（LLM 常识幻觉兜底）
                wd, day_closed = closed_ctx
                if wd and "闭馆" in "".join(tips) and wd not in day_closed:
                    tips = [t for t in tips
                            if "闭馆" not in t or any(c in t for c in day_closed)]
                if tips:
                    entry["tips"] = tips
            return day, entry

        from concurrent.futures import ThreadPoolExecutor
        out = {}
        with ThreadPoolExecutor(max_workers=min(3, len(day_ctxs))) as ex:
            for day, entry in ex.map(_one, day_ctxs):
                if entry:
                    out[day] = entry
        return out, bool(out)
    except Exception:  # noqa
        return {}, False
