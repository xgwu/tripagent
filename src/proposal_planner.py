# -*- coding: utf-8 -*-
"""M7 经验提案模式：LLM 世界知识自由提案 → 落地匹配到 POI 库 → 复用 M2 TOPTW 求解。

回归最初架构意图：「LLM 凭经验生成线路，再匹配库内 POI」，同时保留 M1-M6 的防幻觉成果：
- A 提案：LLM 自由发挥（允许提库外点，故意不设防）
- B 落地：精确/包含/模糊/LLM 四级匹配；匹配失败剔除并记入「POI 库缺口」清单
- C 求解：落地后的 day_map 喂给 m2_planner.compose（TOPTW + 修复链 + 文案）
"""
import difflib, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import poi_db, llm_client, m1_planner, m2_planner
from src import hotel as hotel_mod

PROPOSE_SYSTEM = """你是一位资深旅行规划专家，深谙中国主要旅游城市的经典玩法与本地体验节奏。
请基于你的旅行知识自由设计行程——可以提及任何你知识中真实存在的地点，不受任何列表限制。
注意基本常识（如多数博物馆周一闭馆、热门景点需预留排队时间）。"""

PROPOSE_PROMPT = """请为{city}设计 {days} 天行程。
用户需求：{query}
{date_line}
自由发挥设计一条你认为体验最好的路线，包含每天的主题与停留点（每点一句话说明为什么值得去）。
严格输出 JSON：
{{"days": [{{"day": 1, "theme": "主题", "reason": "整体思路", "stops": [{{"name": "灵隐寺", "note": "为什么去"}}]}}]}}"""

MATCH_SYSTEM = """你是 POI 匹配助手。给定用户行程中的地点名和 POI 库清单，判断每个地点对应库中哪个 POI。
没有可靠对应的输出 null，宁缺毋滥。"""

MATCH_PROMPT = """## 行程中的地点
{stops}

## POI 库清单（id｜名称｜区域｜类目）
{library}

严格输出 JSON：{{"matches": [{{"stop": "地点名", "poi_id": "库内id或null"}}]}}"""


def _norm(s: str) -> str:
    return re.sub(r"[\s·・（）()\-—_、，。…「」『』]", "", (s or "")).lower()


def _ground_one(name: str, all_pois: dict, by_name: list) -> tuple:
    """单点落地：返回 (poi_id|None, method)。四级：精确→包含→模糊。"""
    key = _norm(name)
    if not key:
        return None, "empty"
    for p in by_name:  # 精确
        if _norm(p["name"]) == key:
            return p["id"], "exact"
    cands = []
    for p in by_name:  # 包含（双向，取评分高者）
        pn = _norm(p["name"])
        if len(pn) >= 2 and (pn in key or key in pn):
            cands.append(p)
    if cands:
        return max(cands, key=lambda p: p["rating"])["id"], "contain"
    best, ratio = None, 0.0
    for p in by_name:  # 模糊
        r = difflib.SequenceMatcher(None, key, _norm(p["name"])).ratio()
        if r > ratio:
            best, ratio = p, r
    if best is not None and ratio >= 0.62:
        return best["id"], f"fuzzy({ratio:.2f})"
    return None, "unmatched"


def _llm_match(unmatched: list, all_pois: dict) -> dict:
    """LLM 辅助匹配剩余未落地项（批量一次调用）。返回 {stop_name: poi_id|None}。"""
    if not unmatched:
        return {}
    lib = "\n".join(f'{p["id"]}｜{p["name"]}｜{p.get("area", "")}｜{p["category"]}'
                    for p in sorted(all_pois.values(), key=lambda x: x["id"]))
    stops = "\n".join(f'- {u["name"]}' + (f'（{u["note"]}）' if u.get("note") else "")
                      for u in unmatched)
    try:
        raw = llm_client.chat([
            {"role": "system", "content": MATCH_SYSTEM},
            {"role": "user", "content": MATCH_PROMPT.format(stops=stops, library=lib)}],
            temperature=0.0, seed=42)
        parsed = llm_client.parse_json_safe(raw)
        out = {}
        for m in parsed.get("matches", []):
            pid = m.get("poi_id")
            out[m.get("stop", "")] = pid if pid in all_pois else None
        return out
    except Exception:  # noqa: 匹配失败按未落地处理
        return {}


def _ground(proposal: dict, city: dict, all_pois: dict, days: int):
    """B 阶段：提案落地。返回 (day_map, themes, grounding)。"""
    by_name = list(all_pois.values())
    day_map, themes, seen = {}, {}, set()
    stats = {"n_proposed": 0, "exact": 0, "contain": 0, "fuzzy": 0, "llm": 0,
             "unmatched": 0, "dup_skipped": 0, "gaps": []}
    pending = []  # (day, name) 待 LLM 批量匹配
    pre = {}
    for d in proposal.get("days", []):
        for s in d.get("stops", []):
            stats["n_proposed"] += 1
            pid, method = _ground_one(s.get("name", ""), all_pois, by_name)
            if pid:
                pre[(d.get("day"), s.get("name"))] = (pid, method)
                stats["exact" if method == "exact" else
                      ("contain" if method == "contain" else "fuzzy")] += 1
            else:
                pending.append({"day": d.get("day"), "name": s.get("name", ""),
                                "note": s.get("note", "")})
    llm_res = _llm_match(pending, all_pois)
    for u in pending:
        pid = llm_res.get(u["name"])
        if pid:
            pre[(u["day"], u["name"])] = (pid, "llm")
            stats["llm"] += 1
        else:
            stats["unmatched"] += 1
            stats["gaps"].append({"name": u["name"], "note": u.get("note", ""),
                                  "day": u.get("day")})

    for d in proposal.get("days", []):
        dd = d.get("day")
        if not isinstance(dd, int) or not (1 <= dd <= days):
            continue
        ids, dropped_local = [], []
        for s in d.get("stops", []):
            hit = pre.get((dd, s.get("name")))
            if not hit or hit[0] in seen:
                if hit:  # 命中但跨天重复
                    stats["dup_skipped"] += 1
                continue
            seen.add(hit[0])
            ids.append(hit[0])
        if ids:
            day_map[dd] = ids
            themes[dd] = {"theme": d.get("theme", ""), "reason": d.get("reason", "")}
    # 去掉 stop 级重复计数口径：gaps 只保留真正的未落地提案
    stats["grounding_rate"] = (round((stats["n_proposed"] - stats["unmatched"]) /
                                     max(stats["n_proposed"], 1), 3))
    return day_map, themes, stats


def plan(city: dict, query: str, days: int = 2, use_llm: bool = True,
         date0: str | None = None, hotel_text: str | None = None,
         time_limit_s: float = m2_planner.toptw.TIME_LIMIT_S,
         main_bonus: float = m2_planner.toptw.MAIN_BONUS,
         soft_w: float = m2_planner.toptw.SOFT_W) -> dict:
    if not use_llm or not llm_client.llm_available():
        r = m1_planner.plan(city, query, days, use_llm=False, date0=date0,
                            hotel_text=hotel_text)
        r["mode"] = r["mode"] + " → m7_degraded"
        return r
    t0 = time.time()
    # ---- A 提案：世界知识自由生成（不设防，允许库外）----
    wd1 = poi_db.trip_weekday(date0, 1) if date0 else None
    date_line = (f"出发日期：{date0}（第 1 天为{wd1}），请结合常见闭馆常识安排顺序。\n" if date0 else "")
    raw = llm_client.chat([
        {"role": "system", "content": PROPOSE_SYSTEM},
        {"role": "user", "content": PROPOSE_PROMPT.format(city=city["city"], days=days,
                                                          query=query, date_line=date_line)}],
        temperature=0.2, seed=42)
    proposal = llm_client.parse_json_safe(raw)
    if not proposal.get("days"):
        r = m1_planner.plan(city, query, days, use_llm=True, date0=date0,
                            hotel_text=hotel_text)
        r["mode"] = "m7_proposal_failed → m2_fallback"
        return r

    # ---- B 落地：匹配回 POI 库 ----
    all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}
    day_map, themes, grounding = _ground(proposal, city, all_pois, days)
    if not day_map:  # 全部落地失败 → M2 兜底
        r = m2_planner.plan(city, query, days, use_llm=True, date0=date0,
                            hotel_text=hotel_text, time_limit_s=time_limit_s,
                            main_bonus=main_bonus, soft_w=soft_w)
        r["mode"] = "m7_grounding_empty → m2_fallback"
        r["proposal"] = proposal
        r["grounding"] = grounding
        return r

    # ---- C 求解：复用 M2 compose（TOPTW + 修复链 + 文案）----
    hotel = hotel_mod.resolve_hotel(city, hotel_text)
    r = m2_planner.compose(city, query, days, day_map, themes, use_llm=True,
                           date0=date0, hotel=hotel, time_limit_s=time_limit_s,
                           main_bonus=main_bonus, soft_w=soft_w,
                           meta={"candidates": None, "invalid_poi_ids": [],
                                 "n_dup_across_days": grounding["dup_skipped"],
                                 "hotel": ({"name": hotel["name"], "lat": hotel["lat"],
                                            "lng": hotel["lng"], "resolved": hotel["note"]}
                                           if hotel else None)},
                           mode="m7_proposal")
    r["proposal"] = proposal
    r["grounding"] = grounding
    r["latency_s"] = round(time.time() - t0, 1)  # A+B+C 全链路耗时
    return r
