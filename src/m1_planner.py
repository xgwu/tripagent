# -*- coding: utf-8 -*-
"""M1 主规划器：检索 → LLM 库内选择（结构化输出）→ 校验 → 排时序。

失败降级链：LLM 调用失败 / JSON 解析失败 → 离线启发式规划器（同接口）。
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import poi_db, retrieval, sequencer, llm_client, offline_planner, hotel as hotel_mod

SYSTEM_PROMPT = """你是一位资深{city}本地旅行规划师。你必须【只】使用候选清单中给出的 POI ID 来组线，禁止发明清单之外的任何地点。"""

SYSTEM_PRINCIPLES = """

规划原则：
1. 按 POI 的建议时段与营业时间安排先后（morning 放上午、evening 放晚上）。
2. 同一天的 POI 尽量地理聚集，减少来回奔波。
3. 单日 POI 总游玩时长控制在 6~9 小时（不含交通与用餐）。
4. 充分利用你作为本地人的经验：路线的叙事逻辑与体验节奏（哪些点组合成经典半日线、亲子如何留缓冲）。
5. 每天给出一句主题与选择理由。"""


def system_prompt(city: dict, date0: str | None = None) -> str:
    base = SYSTEM_PROMPT.format(city=city["city"])
    if date0:
        base += ("\n\n日期感知：行程从用户消息给出的起始日期开始。注意 POI 的闭馆日"
                 "（卡片中标注「闭馆」）：绝不能把 POI 安排在其闭馆日当天。")
    return base + SYSTEM_PRINCIPLES


def plan(city: dict, query: str, days: int = 2, use_llm: bool = True,
         date0: str | None = None, hotel_text: str | None = None) -> dict:
    cands = retrieval.recall(city, query)
    all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}
    t0 = time.time()
    prompt_chars = 0
    mode, invalid_ids, raw = "offline_fallback", [], None

    # M5 日期感知：每天的星期名（供 prompt 与闭馆校验使用）
    day_wd = {d: poi_db.trip_weekday(date0, d) for d in range(1, days + 1)} if date0 else {}
    # M6 住宿锚点：酒店虚拟节点（每日出发/返回）
    hotel = hotel_mod.resolve_hotel(city, hotel_text)

    if use_llm and llm_client.llm_available():
        date_line = (f"起始日期：{date0}（第 1 天为{day_wd.get(1, '')}，"
                     f"第 {days} 天为{day_wd.get(days, '')}），"
                     f"各天星期：{'；'.join(f'Day {d}={w}' for d, w in day_wd.items())}\n"
                     if date0 else "")
        hotel_line = (f"住宿锚点：{hotel['name']}（每天从该酒店出发，游玩结束后返回酒店；"
                      f"第一天首站不要离酒店太远）\n" if hotel else "")
        prompt = f"""## 用户需求\n{query}\n行程天数：{days} 天\n{date_line}{hotel_line}\n## 候选 POI 清单（只能从中选择；标注「闭馆」的点不得排在其闭馆日；同一 POI 不得出现在多天；时长≥8h 的全天型景点须独占一天，当天不排其他点）\n{retrieval.candidate_cards(cands)}\n\n## 输出要求\n严格输出 JSON：\n{{"days": [{{"day": 1, "theme": "主题", "poi_ids": ["{cands[0]['id']}", ...], "reason": "选择理由（含体验节奏说明）"}}]}}"""
        prompt_chars = len(prompt)
        try:
            raw = llm_client.chat([
                {"role": "system", "content": system_prompt(city, date0)},
                {"role": "user", "content": prompt}],
                temperature=0.2, seed=42)
            parsed = llm_client.parse_json_safe(raw)
            day_map, themes = {}, {}
            for d in parsed.get("days", []):
                valid = [i for i in d.get("poi_ids", []) if i in all_pois]
                invalid_ids.extend([i for i in d.get("poi_ids", []) if i not in all_pois])
                if valid:
                    day_map[d.get("day", len(day_map) + 1)] = valid
                    themes[d.get("day", 1)] = {"theme": d.get("theme", ""), "reason": d.get("reason", "")}
            if day_map:
                mode = "llm"
        except Exception as e:  # noqa
            day_map, themes = None, None
            detail = ""
            if isinstance(e, llm_client.urllib.error.HTTPError):
                try:
                    detail = e.read().decode("utf-8", "replace")[:160]
                except Exception:  # noqa
                    pass
            mode = f"offline_fallback(llm_error: {type(e).__name__} {detail})"
    else:
        day_map, themes = None, None

    if day_map is None:
        day_map, themes = offline_planner.plan_days(city, cands, query, days)
        mode = mode if mode != "offline_fallback" else "offline_fallback"

    # M6 跨天去重：同一 POI 只保留首次出现（LLM 越权重复在此硬性剥除）
    seen, n_dup = set(), 0
    for d in sorted(day_map):
        deduped = []
        for pid in day_map[d]:
            if pid in seen:
                n_dup += 1
                continue
            seen.add(pid)
            deduped.append(pid)
        day_map[d] = deduped
    day_map = {d: ids for d, ids in day_map.items() if ids}

    # 天数保障：LLM 偶发少给天（如请求 3 天只回 2 组）或去重后某天被清空，
    # 用离线规划从剩余未用候选补齐缺口日，保证输出天数与请求一致
    missing = [d for d in range(1, days + 1) if d not in day_map]
    if missing:
        used = {pid for ids in day_map.values() for pid in ids}
        rest = [c for c in cands if c["id"] not in used]
        if rest:
            extra_map, extra_themes = offline_planner.plan_days(city, rest, query, len(missing))
            for k, ed in enumerate(sorted(extra_map)):
                if k >= len(missing):
                    break
                day_map[missing[k]] = extra_map[ed]
                if ed in extra_themes:
                    themes[missing[k]] = extra_themes[ed]
            mode += "+补天"

    # 库内选择模式下 invalid_ids 恒为 0 —— 这就是要验证的指标
    llm_raw_violations = _count_violations_before_repair(day_map, city, all_pois, date0, hotel)
    itin = sequencer.build_itinerary(day_map, city, all_pois, date0=date0, hotel=hotel, query=query)

    # Agent Loop 闭环：修复剔除的 POI → LLM 从候选池推荐替代 → 复检可行则补入
    substitutes = []
    if itin.get("dropped_pois") and mode == "llm":
        day_map2, substitutes = _feedback_loop(city, cands, query, days, day_map,
                                               itin["dropped_pois"], all_pois)
        if day_map2:
            itin2 = sequencer.build_itinerary(day_map2, city, all_pois, date0=date0, hotel=hotel, query=query)
            if itin2["total_violations"] == 0:
                itin2["substitutes"] = substitutes
                itin = itin2
                day_map, themes = day_map2, themes  # 主题沿用
    itin.setdefault("substitutes", [])

    for d in itin["days"]:
        info = themes.get(d["day"], {})
        d["theme"], d["reason"] = info.get("theme", ""), info.get("reason", "")
        # 修复后文案一致性：发生重排/剔除时明确标注（完整「文案重生成」留 M2）
        marks = []
        if d.get("reordered"):
            marks.append("该日 POI 顺序经约束修复重排")
        if any(s.get("day") == d["day"] for s in itin.get("substitutes", [])):
            marks.append("含 LLM 替代推荐点")
        if marks:
            d["reason"] = (d["reason"] + "　【系统标注：" + "；".join(marks) + "】").strip()

    return {"mode": mode, "query": query, "days": days, "candidates": len(cands),
            "date0": date0, "n_dup_across_days": n_dup,
            "hotel": ({"name": hotel["name"], "lat": hotel["lat"], "lng": hotel["lng"],
                       "resolved": hotel["note"]} if hotel else None),
            "prompt_chars": prompt_chars if (use_llm and day_map is not None) else None,
            "invalid_poi_ids": invalid_ids, "llm_raw_violations": llm_raw_violations,
            "latency_s": round(time.time() - t0, 1), "itinerary": itin}


def _count_violations_before_repair(day_map: dict, city: dict, all_pois: dict,
                                    date0: str | None = None,
                                    hotel: dict | None = None) -> int:
    """LLM 原始排序下的硬约束违规数（修复前），衡量「优化层修复了多少」。"""
    total = 0
    for d in sorted(day_map):
        pois = [all_pois[i] for i in day_map[d] if i in all_pois]
        wd = poi_db.trip_weekday(date0, d) if date0 else None
        total += len(sequencer._build_timeline(pois, city, d, wd, hotel)["violations"])
    return total


FEEDBACK_PROMPT = """你是旅行规划的修正环节。之前的行程中以下 POI 因约束不可行被剔除：

{dropped}

现有行程已包含的 POI（禁止重复推荐）：
{used}

剩余候选池（只能从中选择）：
{candidates}

用户需求：{query}

请为每个被剔除的 POI 推荐最多 1 个替代点（地理相近、类型相配）。严格输出 JSON：
{{"substitutes": [{{"for": "被剔除的POI名", "poi_id": "候选ID", "reason": "推荐理由"}}]}}
若无合适替代，对应项的 poi_id 填空字符串。"""


def _feedback_loop(city, cands, query, days, day_map, dropped, all_pois):
    """剔除 POI → LLM 推荐替代 → 校验后补入原日。失败则静默放弃（行程照常输出）。"""
    try:
        used = [i for ids in day_map.values() for i in ids]
        remaining = [p for p in cands if p["id"] not in used]
        raw = llm_client.chat([
            {"role": "system", "content": system_prompt(city)},
            {"role": "user", "content": FEEDBACK_PROMPT.format(
                dropped="\n".join(f"- {d['name']}：{d['reason']}" for d in dropped),
                used="、".join(all_pois[i]["name"] for i in used if i in all_pois),
                candidates=retrieval.candidate_cards(remaining[:30]),
                query=query)}])
        parsed = llm_client.parse_json_safe(raw)
        day_map2 = {k: list(v) for k, v in day_map.items()}
        subs = []
        for s in parsed.get("substitutes", []):
            pid = s.get("poi_id", "")
            if pid not in all_pois or pid in used:
                continue
            # 补进「被替代点所在的天」—— 找到含被剔除日即原日不可知，放天数最少的一天
            target = min(day_map2, key=lambda k: len(day_map2[k]))
            if pid not in day_map2[target]:
                day_map2[target].append(pid)
                used.append(pid)
                subs.append({"for": s.get("for", ""), "poi_id": pid,
                             "day": target, "reason": s.get("reason", "")})
        return (day_map2, subs) if subs else (None, [])
    except Exception:  # noqa
        return None, []
