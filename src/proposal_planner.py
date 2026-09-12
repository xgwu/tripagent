# -*- coding: utf-8 -*-
"""M7 经验提案模式：LLM 世界知识自由提案 → 落地匹配到 POI 库 → 复用 M2 TOPTW 求解。

回归最初架构意图：「LLM 凭经验生成线路，再匹配库内 POI」，同时保留 M1-M6 的防幻觉成果：
- A 提案：LLM 自由发挥（允许提库外点，故意不设防）
- B 落地：精确/包含/模糊/LLM 四级匹配；匹配失败剔除并记入「POI 库缺口」清单
- C 求解：落地后的 day_map 喂给 m2_planner.compose（TOPTW + 修复链 + 文案）
"""
import difflib, hashlib, json, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import poi_db, llm_client, m1_planner, m2_planner, offline_planner, sequencer
from src import hotel as hotel_mod

# ---- P1 提案级缓存：同（城市+天数+需求+日期+库版本）复用上次 LLM 提案，跳过最贵的 A 阶段 ----
# 提案调用 temperature=0.2/seed=42 本身近似确定性，缓存纯省时无行为差异。
# key 含库内 POI 数量：扩城/补库后自动失效；TTL 7 天与 nl_cache 对齐。
PROPOSAL_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "proposal_cache.json")
PROPOSAL_CACHE_TTL_S = 7 * 86400
_proposal_cache: dict | None = None


def _pcache_get(key: str):
    global _proposal_cache
    if _proposal_cache is None:
        try:
            with open(PROPOSAL_CACHE_PATH, encoding="utf-8") as f:
                _proposal_cache = json.load(f)
        except Exception:  # noqa
            _proposal_cache = {}
    ent = _proposal_cache.get(key)
    if not ent or time.time() - ent.get("ts", 0) > PROPOSAL_CACHE_TTL_S:
        return None
    return ent.get("v")


def _pcache_put(key: str, val: dict) -> None:
    global _proposal_cache
    if _proposal_cache is None:
        _proposal_cache = {}
    _proposal_cache[key] = {"ts": time.time(), "v": val}
    try:
        tmp = PROPOSAL_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_proposal_cache, f, ensure_ascii=False)
        os.replace(tmp, PROPOSAL_CACHE_PATH)
    except Exception:  # noqa: 缓存写失败不影响主流程
        pass

PROPOSE_SYSTEM = """你是一位资深旅行规划专家，深谙中国主要旅游城市的经典玩法与本地体验节奏。
请凭你的旅行知识自由设计行程——你的专业推荐优先；用户提示词末尾会附一份系统已收录的地点清单
（带完整数据、可直接排入），仅作查漏补缺的参考。不得虚构不存在的地点。
注意基本常识（如多数博物馆周一闭馆、热门景点需预留排队时间）。"""

PROPOSE_PROMPT = """请为{city}设计 {days} 天行程。
用户需求：{query}
{date_line}{weather_line}
自由发挥设计一条你认为体验最好的路线，包含每天的主题与停留点（每点一句话说明为什么值得去），每天 4-6 个停留点。
停留点必须是游客可游览的真实地点（景点/场馆/历史街区/公园/餐厅/市场等），不要把酒店、商铺门店当作停留点（住宿由系统另行安排）。
路线设计常识：同一天的停留点尽量集中在相邻片区、顺路串联，避免一天内东西横跨全城；优先选择知名度高、位置明确易确认的地点。全天大点规则：时长约 8 小时以上的大型景点（如迪士尼、海昌海洋公园这类主题乐园）须独占一整天，当天不要再排其他停留点。
跨天片区分散规则：不同天安排不同片区——同一条马路/街区（如武康路、田子坊、外滩）及其 1 公里范围内的点位只能出现在其中一天，相邻两天绝不能去同一片区；咖啡店等口碑配套每天最多 1 家，跟着当天主片区选，不要两天都往同一片区跑。配套顺路规则：咖啡店/餐厅等配套必须顺路——选位于当天相邻主选之间、或紧邻某个主选（步行可达，约 1.5 公里内）的店，绝不要为了某家网红店跨区绕路（例如上午在世博、下午去陆家嘴，就选两家之间沿线的店，不要折道武康路）。正餐规则：午餐/晚餐时段安排正经吃饭的地方（餐厅/名小吃/美食街），咖啡馆不能当正餐——咖啡只作为逛点之间的休憩加项，用户喜欢咖啡时另选顺路咖啡店，不要用它顶替午餐。
招牌体验规则：先用世界知识判断用户需求的核心期待——每个城市都有公认必去的招牌景点（如上海的迪士尼度假区、北京环球影城、广州长隆），亲子/带娃类需求通常正期待这类招牌。若需求主题与某招牌景点高度匹配，必须把它作为主选排进某一天（独占一天，勿放 alternates）；只有当你有明确理由认为用户不会感兴趣（如需求明确排斥主题乐园）时才可不放。住宿锚点规则：用户指定住宿位置（如「住迪士尼附近」）时，行程必须包含该位置对应的标志性景点（住迪士尼附近则必含迪士尼），且该景点独占一天、优先安排在第一天，其余天数再安排其他区域。傍晚密度规则：博物馆/美术馆/展馆类场馆普遍 17 点前后闭馆，每天要为傍晚（17 点后）搭配至少 1 个晚间型停留点——夜展/灯光夜景/历史街区夜游/滨江步道/咖啡街区/书院茶馆等，避免傍晚大片空白；每天 4-6 个停留点中应含 1-2 个晚间型。
备选规则：每天可附 0-2 个 alternates——你认为时间充裕时值得加上的点、或主选可能闭馆/排队过久时的同区域替补；备选不必与主选相邻，系统会按约束自动取舍。
参考清单——以下{city}地点带完整数据（坐标/开放时间/适玩时长），排入即可直接落地；若与你更想推荐的地点重合，以你的专业判断为准：
{library_hint}
严格输出 JSON：
{{"days": [{{"day": 1, "theme": "主题", "reason": "整体思路", "stops": [{{"name": "灵隐寺", "note": "为什么去"}}], "alternates": [{{"name": "西溪湿地", "note": "时间充裕可加"}}]}}]}}"""

MATCH_SYSTEM = """你是 POI 匹配助手。给定用户行程中的地点名和 POI 库清单，判断每个地点对应库中哪个 POI。
同一地点的别称、旧称、近似名称、同品牌不同分店（如「XX（苏州河店）」对应库内「XX（武康路店）」）都应匹配到库中最接近的一项；
只有当库中确实没有该地点或其近似项时才输出 null。"""

MATCH_PROMPT = """## 行程中的地点
{stops}

## POI 库清单（id｜名称｜区域｜类目）
{library}

严格输出 JSON：{{"matches": [{{"stop": "地点名", "poi_id": "库内id或null"}}]}}"""

# ---- 闭环反馈（P0）：落地失败/约束剔除 → 反馈 LLM 修正 → 再落地，共享轮次预算 ----
MAX_REVISE_ROUNDS = 2     # 最多修正轮数（每轮一次 LLM 调用）
GROUNDING_RATE_MIN = 0.85 # 落地率低于此阈值必触发修正（有无 gaps 均触发）
DROP_FEEDBACK_MIN = 2     # compose 剔除点数达到该值才触发剔除原因反馈（避免小剔大动）
AREA_OVERLAP_KM = 1.2     # 相邻两天点位距离小于该值视为片区重复（「武康路连去两天」类问题）

REVISE_SYSTEM = """你是旅行行程修正助手。系统已尝试把一份行程提案落地到 POI 库，部分地点无法落地、
或被时间窗/里程等约束剔除。请在保留原行程结构与天数划分的前提下，输出一份修正版提案。"""

REVISE_PROMPT = """原需求：{query}

当前提案 JSON：
{proposal}

## 需要修正的地点
{issues}
规则：
- 「无法落地」：该地点在 POI 库中不存在，须替换为同类/同区域的真实地点（优先参考下方清单）或删除，不要保留原名。
- 「被约束剔除」：附有剔除原因（如里程预算、时间窗冲突），替换为与当天其他点更近、更兼容的点，或删除该点。
- 「片区重复」：该点与相邻一天的某个点同在一片区（附距离），把它换成其他片区的同类点，使各天片区互不重叠；配套（咖啡店等）跟随当天新片区选且必须顺路（位于相邻主选之间或紧邻某主选 1.5 公里内）。
- 其余地点、主题、顺序尽量原样保留；不要增加新的无法落地的地点。

参考清单——以下{city}地点带完整数据（坐标/开放时间/适玩时长），排入即可直接落地：
{library_hint}

严格输出 JSON：
{{"days": [{{"day": 1, "theme": "主题", "reason": "整体思路", "stops": [{{"name": "灵隐寺", "note": "为什么去"}}]}}]}}"""


def _crossday_area_overlap(day_ids: dict, all_pois: dict) -> list:
    """跨天片区重叠检测：相邻两天存在 <AREA_OVERLAP_KM 的点位对 → 晚一天的点记为「片区重复」。

    「武康路连去两天」类问题的确定性守门：M1 精确去重只挡同 POI 跨天重复，
    挡不住「武康路·安福路(Day1) + 武康路咖啡店(Day2)」这类同片区不同点的重复。
    只对晚一天的点出反馈（换点责任在后者）；供闭环 REVISE 消费。
    """
    from src.poi_db import haversine_km
    feedback, days = [], sorted(day_ids)
    for i, j in zip(days, days[1:]):
        for bid in day_ids.get(j, []):
            b = all_pois.get(bid)
            if not b:
                continue
            for aid in day_ids.get(i, []):
                a = all_pois.get(aid)
                if not a:
                    continue
                dkm = haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])
                if dkm < AREA_OVERLAP_KM:
                    feedback.append({
                        "name": b["name"], "day": j,
                        "reason": (f'与 Day{i} 的「{a["name"]}」同在一片区（相距 {dkm:.1f}km），'
                                   "相邻两天不要重复同一片区，请换成其他片区的同类点")})
                    break
    return feedback


def _norm(s: str) -> str:
    return re.sub(r"[\s·・（）()\-—_、，。…「」『』]", "", (s or "")).lower()


def _branch_variants(name: str) -> list:
    """分店名归一化变体：剥离「（XX店）」类括号后缀与尾部「总店/分店」，用于同品牌跨分店匹配。

    如「% Arabica（苏州河店）」→「% Arabica」，可包含匹配到库内「% Arabica（武康路店）」。
    """
    out = []
    base = re.sub(r"[（(][^（）()]*店[）)]$", "", name).strip()
    base = re.sub(r"(总店|分店)$", "", base).strip()
    if base and base != name:
        out.append(base)
    return out


def _library_hint(all_pois: dict) -> str:
    """库内地点菜单（按类目分组），注入提案提示词引导优先选用库内点。"""
    groups = {}
    for p in all_pois.values():
        groups.setdefault(p["category"], []).append(p["name"])
    return "\n".join(f"- {cat}：{'、'.join(names)}"
                     for cat, names in sorted(groups.items()))


def _ground_one(name: str, all_pois: dict, by_name: list) -> tuple:
    """单点落地：返回 (poi_id|None, method)。四级：精确→包含→模糊。"""
    key = _norm(name)
    if not key:
        return None, "empty"
    for p in by_name:  # 精确
        if _norm(p["name"]) == key:
            return p["id"], "exact"
    cands = []
    # 包含（双向，取评分高者）；分店归一化变体一并参与（同品牌跨分店）
    for nm in [name] + _branch_variants(name):
        kn = _norm(nm)
        if not kn:
            continue
        for p in by_name:
            pn = _norm(p["name"])
            if len(pn) >= 2 and (pn in kn or kn in pn):
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


def _llm_match(unmatched: list, all_pois: dict, exclude_ids: set | None = None) -> dict:
    """LLM 辅助匹配剩余未落地项（批量一次调用）。返回 {stop_name: poi_id|None}。

    exclude_ids: 住宿服务类等不可作为停留点的库点 id，从菜单与结果两侧剔除。
    """
    if not unmatched:
        return {}
    exclude_ids = exclude_ids or set()
    lib = "\n".join(f'{p["id"]}｜{p["name"]}｜{p.get("area", "")}｜{p["category"]}'
                    for p in sorted(all_pois.values(), key=lambda x: x["id"])
                    if p["id"] not in exclude_ids)
    stops = "\n".join(f'- {u["name"]}' + (f'（{u["note"]}）' if u.get("note") else "")
                      for u in unmatched)
    try:
        raw = llm_client.chat([
            {"role": "system", "content": MATCH_SYSTEM},
            {"role": "user", "content": MATCH_PROMPT.format(stops=stops, library=lib)}],
            temperature=0.0, seed=42, max_tokens=400)  # P2 轻量路径：匹配结果为短 JSON
        parsed = llm_client.parse_json_safe(raw)
        out = {}
        for m in parsed.get("matches", []):
            pid = m.get("poi_id")
            out[m.get("stop", "")] = pid if (pid in all_pois and pid not in exclude_ids) else None
        return out
    except Exception:  # noqa: 匹配失败按未落地处理
        return {}


def _is_lodging(raw: dict) -> bool:
    """高德采集的住宿服务类点（宾馆/酒店）不是游览停留点，匹配层免疫。"""
    tags = "".join(raw.get("tags") or [])
    return "住宿" in tags or "宾馆" in tags


def _ground(proposal: dict, city: dict, all_pois: dict, days: int):
    """B 阶段：提案落地。返回 (day_map, themes, stats)。

    主选 stops 落地进 day_map；备选 alternates 落地进 stats["alt_map"]（{day: [poi_id]}，
    供 TOPTW 低利润权重换点），备选未落地不计入 gaps/落地率（可选性质）。
    """
    lodging_ids = {r["id"] for r in city["pois"] if _is_lodging(r)}
    by_name = [p for p in all_pois.values() if p["id"] not in lodging_ids]
    day_map, themes, seen = {}, {}, set()
    stats = {"n_proposed": 0, "exact": 0, "contain": 0, "fuzzy": 0, "llm": 0,
             "unmatched": 0, "dup_skipped": 0, "gaps": [],
             "n_alt": 0, "n_alt_hit": 0}
    pending = []  # (day, name) 待 LLM 批量匹配
    pre = {}
    alt_map = {}  # day -> [poi_id]
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
        # 备选 alternates：四级匹配，未落地静默丢弃（不计 gaps）
        for s in (d.get("alternates") or []):
            if not isinstance(s, dict) or not s.get("name"):
                continue
            stats["n_alt"] += 1
            pid, _m = _ground_one(s.get("name", ""), all_pois, by_name)
            if pid:
                alt_map.setdefault(d.get("day"), []).append(pid)
                stats["n_alt_hit"] += 1
            else:
                pending.append({"day": d.get("day"), "name": s.get("name", ""),
                                "note": s.get("note", ""), "alt": True})
    llm_res = _llm_match(pending, all_pois, exclude_ids=lodging_ids)
    for u in pending:
        pid = llm_res.get(u["name"])
        if pid and u.get("alt"):
            alt_map.setdefault(u["day"], []).append(pid)
            stats["n_alt_hit"] += 1
        elif pid:
            pre[(u["day"], u["name"])] = (pid, "llm")
            stats["llm"] += 1
        elif not u.get("alt"):  # 主选未落地才计 gaps；备选可选性质，静默丢弃
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
    # 备选去重：同天重复剔除（与主选/跨天重复由 compose 的 used_all 兜底）
    for dd in list(alt_map):
        alt_map[dd] = list(dict.fromkeys(alt_map[dd]))
        if not alt_map[dd]:
            del alt_map[dd]
    stats["alt_map"] = alt_map
    # 去掉 stop 级重复计数口径：gaps 只保留真正的未落地提案
    stats["grounding_rate"] = (round((stats["n_proposed"] - stats["unmatched"]) /
                                     max(stats["n_proposed"], 1), 3))
    return day_map, themes, stats


def _revise_proposal(city: dict, query: str, days: int, proposal: dict,
                     gaps: list, drops: list, hint: str) -> dict | None:
    """闭环反馈：把未落地 gaps + 约束剔除 drops 打包回 LLM，要一版修正提案。

    gaps: [{"name","note","day"}]；drops: [{"name","reason","day"}]
    LLM 不可用/解析失败返回 None（调用方保留原案）。
    """
    issues = []
    for g in gaps:
        issues.append(f'- 「{g["name"]}」（Day {g.get("day", "?")}）无法落地——POI 库中不存在')
    for d in drops:
        issues.append(f'- 「{d["name"]}」（Day {d.get("day", "?")}）被约束剔除——{d.get("reason", "")}')
    if not issues:
        return None
    try:
        raw = llm_client.chat([
            {"role": "system", "content": REVISE_SYSTEM},
            {"role": "user", "content": REVISE_PROMPT.format(
                query=query, proposal=json.dumps(proposal, ensure_ascii=False),
                issues="\n".join(issues), city=city["city"], library_hint=hint)}],
            temperature=0.2, seed=42)
        revised = llm_client.parse_json_safe(raw)
        return revised if revised.get("days") else None
    except Exception:  # noqa: 网络/Key 问题按不可修正处理
        return None


def _post_ground_fixups(day_map: dict, themes: dict, grounding: dict, city: dict,
                        all_pois: dict, days: int, query: str, anchor_poi: dict | None):
    """落地后确定性修整：锚点注入（开关控制）→ 缺天补齐 → 单天 MIN_STOPS 补强。"""
    # 住宿锚点硬保障（开关默认关）：锚点地标未进行程时确定性注入
    if anchor_poi is not None and hotel_mod.hard_guarantee_enabled():
        note = hotel_mod.ensure_landmark_in_day_map(day_map, days, anchor_poi)
        if note:
            grounding["anchor_injected"] = note
    # 天数保障：提案/落地后不足请求天数（LLM 少给一组或落地失败清空某天）→
    # 用离线规划从剩余未落地候选补齐缺口日
    if day_map:
        missing = [d for d in range(1, days + 1) if d not in day_map]
        if missing:
            used = {pid for ids in day_map.values() for pid in ids}
            rest = [p for i, p in all_pois.items() if i not in used]
            if rest:
                extra_map, extra_themes = offline_planner.plan_days(city, rest, query, len(missing))
                for k, ed in enumerate(sorted(extra_map)):
                    if k >= len(missing):
                        break
                    day_map[missing[k]] = extra_map[ed]
                    if ed in extra_themes:
                        themes[missing[k]] = extra_themes[ed]
                grounding["days_topped_up"] = missing
    # 单天补强：缺口剔除导致某天只剩 1-2 个点 → 从未用候选离线补足（P0-3 缺口二次提案）
    if day_map:
        MIN_STOPS = 3
        used = {pid for ids in day_map.values() for pid in ids}
        topped = []
        for d in range(1, days + 1):
            if any(sequencer.is_full_day(all_pois[pid])
                   for pid in day_map.get(d, []) if pid in all_pois):
                continue  # 全天大点（迪士尼等）独占日不补小点——补了也会被时间约束剔除
            while len(day_map.get(d, [])) < MIN_STOPS:
                rest = [p for i, p in all_pois.items() if i not in used]
                if not rest:
                    break
                extra_map, _et = offline_planner.plan_days(city, rest, query, 1)
                new = [pid for pid in extra_map.get(1, []) if pid not in used]
                if not new:  # 候选耗尽或规划器无法给出新点
                    break
                need = MIN_STOPS - len(day_map.get(d, []))
                day_map[d] = day_map.get(d, []) + new[:need]
                used.update(new[:need])
                if d not in topped:
                    topped.append(d)
        if topped:
            grounding["days_stops_topped_up"] = topped
    return day_map, themes, grounding


def _compose_m7(city: dict, query: str, days: int, day_map: dict, themes: dict,
                date0: str | None, hotel: dict | None, anchor_poi: dict | None,
                grounding: dict, alt_map: dict | None,
                time_limit_s: float, main_bonus: float, soft_w: float,
                progress=None, reuse_days: dict | None = None) -> dict:
    """M7 复用 M2 compose（TOPTW + 修复链 + 文案），参数固定便于闭环重算。"""
    return m2_planner.compose(city, query, days, day_map, themes, use_llm=True,
                              date0=date0, hotel=hotel, time_limit_s=time_limit_s,
                              main_bonus=main_bonus, soft_w=soft_w,
                              progress=progress, reuse_days=reuse_days,
                              alt_map=alt_map,
                              meta={"candidates": None, "invalid_poi_ids": [],
                                    "n_dup_across_days": grounding["dup_skipped"],
                                    "hotel": ({"name": hotel["name"], "lat": hotel["lat"],
                                               "lng": hotel["lng"], "resolved": hotel["note"]}
                                              if hotel else None)},
                              mode="m7_proposal",
                              forced_ids={anchor_poi["id"]}
                              if (anchor_poi and hotel_mod.hard_guarantee_enabled()) else None)


def plan(city: dict, query: str, days: int = 2, use_llm: bool = True,
         date0: str | None = None, hotel_text: str | None = None,
         time_limit_s: float = m2_planner.toptw.TIME_LIMIT_S,
         main_bonus: float = m2_planner.toptw.MAIN_BONUS,
         soft_w: float = m2_planner.toptw.SOFT_W, progress=None) -> dict:
    def _report(stage: str) -> None:
        if progress:
            try:
                progress(stage)
            except Exception:  # noqa: 进度上报失败不拖垮规划
                pass

    if not use_llm or not llm_client.llm_available():
        r = m1_planner.plan(city, query, days, use_llm=False, date0=date0,
                            hotel_text=hotel_text)
        r["mode"] = r["mode"] + " → m7_degraded"
        return r
    t0 = time.time()
    # ---- A 提案：世界知识自由生成（允许库外补充；注入库内菜单引导优先选用）----
    _report("proposal")
    all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}
    hint = _library_hint(all_pois)
    wd1 = poi_db.trip_weekday(date0, 1) if date0 else None
    date_line = (f"出发日期：{date0}（第 1 天为{wd1}），请结合常见闭馆常识安排顺序。\n" if date0 else "")
    # P2-5 天气感知：雨天意图 → 引导室内场馆优先、减少露天点位
    weather_line = ("天气提示：需求含雨天/下雨，请优先安排室内场馆（博物馆/美术馆/科技馆/室内乐园/商场等），"
                    "减少露天观景台、户外步道类点位。\n") if re.search(r"下雨|雨天|降雨|暴雨", query) else ""
    norm_q = re.sub(r"\s+", "", query).lower()
    pkey = hashlib.sha1(json.dumps(
        [city["city"], days, norm_q, date0 or "", bool(weather_line), len(all_pois)],
        ensure_ascii=False).encode("utf-8")).hexdigest()
    cached = _pcache_get(pkey)
    if cached is not None:
        proposal = cached
    else:
        raw = llm_client.chat([
            {"role": "system", "content": PROPOSE_SYSTEM},
            {"role": "user", "content": PROPOSE_PROMPT.format(city=city["city"], days=days,
                                                              query=query, date_line=date_line,
                                                              weather_line=weather_line,
                                                              library_hint=hint)}],
            temperature=0.2, seed=42)
        proposal = llm_client.parse_json_safe(raw)
        if proposal.get("days"):
            _pcache_put(pkey, proposal)
    if not proposal.get("days"):
        r = m1_planner.plan(city, query, days, use_llm=True, date0=date0,
                            hotel_text=hotel_text)
        r["mode"] = "m7_proposal_failed → m2_fallback"
        return r

    # ---- B 落地 + 闭环反馈：未落地 gaps → LLM 修正 → 再落地（共享轮次预算）----
    _report("grounding")
    day_map, themes, grounding = _ground(proposal, city, all_pois, days)
    rounds_used = 0
    while (rounds_used < MAX_REVISE_ROUNDS
           and (grounding["gaps"] or grounding["grounding_rate"] < GROUNDING_RATE_MIN)):
        revised = _revise_proposal(city, query, days, proposal,
                                   grounding["gaps"], [], hint)
        if revised is None:
            break  # LLM 不可用/解析失败，保留原案走离线补强
        dm2, th2, g2 = _ground(revised, city, all_pois, days)
        rounds_used += 1
        if not dm2 or g2["grounding_rate"] < grounding["grounding_rate"]:
            break  # 修正无益（更差/清空），保留原案
        proposal, day_map, themes, grounding = revised, dm2, th2, g2
    grounding["revise_rounds"] = rounds_used
    alt_map = grounding.pop("alt_map", {})

    # 落地后确定性修整：锚点注入 → 缺天补齐 → 单天补强
    anchor_poi = hotel_mod.match_landmark_poi(city, hotel_text)
    day_map, themes, grounding = _post_ground_fixups(
        day_map, themes, grounding, city, all_pois, days, query, anchor_poi)
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
    r = _compose_m7(city, query, days, day_map, themes, date0, hotel,
                    anchor_poi, grounding, alt_map, time_limit_s, main_bonus, soft_w,
                    progress=progress)
    # 闭环第二触发点：约束剔除过多 / 相邻两天片区重复 → 带原因反馈修正 → 重落地重求解（一轮）
    # 注意口径：每日 dropped 只有修复链剔除；主选被 TOPTW 剔除在 r["mains_dropped"]——
    # 求解器剔掉 LLM 主选 = 世界知识被时间预算否决（如宋城/迪士尼），是最需要闭环的信号
    def _collect_issues(res) -> tuple[list, list]:
        drops = [{"name": dr.get("name", ""), "reason": dr.get("reason", ""), "day": d.get("day")}
                 for d in res["itinerary"]["days"] for dr in d.get("dropped", [])]
        seen_d = {x["name"] for x in drops}
        drops += [{"name": x.get("name", ""), "reason": x.get("reason", ""), "day": x.get("day")}
                  for x in res.get("mains_dropped", []) if x.get("name") not in seen_d]
        day_ids = {d["day"]: [s["id"] for s in d["timeline"] if s["type"] == "poi"]
                   for d in res["itinerary"]["days"]}
        return drops, _crossday_area_overlap(day_ids, all_pois)

    drops, overlap = _collect_issues(r)
    # 触发口径：剔除 ≥2 条，或存在任何跨天片区重复（单条即明显不合理，如武康路连去两天）
    if rounds_used < MAX_REVISE_ROUNDS and (len(drops) >= DROP_FEEDBACK_MIN or overlap):
        revised = _revise_proposal(city, query, days, proposal,
                                   grounding["gaps"], drops + overlap, hint)
        if revised is not None:
            dm2, th2, g2 = _ground(revised, city, all_pois, days)
            if dm2 and g2["grounding_rate"] >= grounding["grounding_rate"]:
                alt_map2 = g2.pop("alt_map", {})
                dm2, th2, g2 = _post_ground_fixups(
                    dm2, th2, g2, city, all_pois, days, query, anchor_poi)
                if dm2:
                    # P1 增量重算：修正前后点位集合一致的天复用上次最终解与文案，
                    # 只对变化的天重解+重写文案（全变则等价全量，行为不劣化）
                    prev_days = {
                        d["day"]: {"ids": [s["id"] for s in d["timeline"] if s["type"] == "poi"],
                                   "copy": {"theme": d.get("theme", ""),
                                            "reason": d.get("reason", ""),
                                            "tips": d.get("tips", [])}}
                        for d in r["itinerary"]["days"]}
                    reuse_days = {d: rec for d, rec in prev_days.items()
                                  if rec["ids"] and dm2.get(d)
                                  and set(dm2[d]) == set(rec["ids"])}
                    # C 闭环重算是设计内的第二轮质量优化（非出错回退）：进度阶段
                    # 加 ":r2" 轮次后缀，前端据此显示「质量优化（第 2 轮）」而非
                    # 让用户误以为进度条倒退卡死
                    def _r2_progress(stage: str) -> None:
                        if progress:
                            try:
                                progress(stage + ":r2")
                            except Exception:  # noqa: 进度上报失败不拖垮规划
                                pass
                    r2 = _compose_m7(city, query, days, dm2, th2, date0, hotel,
                                     anchor_poi, g2, alt_map2,
                                     time_limit_s, main_bonus, soft_w,
                                     progress=_r2_progress if progress else None,
                                     reuse_days=reuse_days or None)
                    drops2, overlap2 = _collect_issues(r2)
                    # 修正确实减少问题总数（剔除+片区重复）才采纳，防震荡
                    if len(drops2) + len(overlap2) < len(drops) + len(overlap):
                        r = r2
                        proposal, grounding, alt_map = revised, g2, alt_map2
                        grounding["revise_rounds"] = rounds_used + 1
    final_day_ids = {d["day"]: [s["id"] for s in d["timeline"] if s["type"] == "poi"]
                     for d in r["itinerary"]["days"]}
    r["area_overlap"] = _crossday_area_overlap(final_day_ids, all_pois)
    r["proposal"] = proposal
    r["grounding"] = grounding
    r["latency_s"] = round(time.time() - t0, 1)  # A+B+C 全链路耗时
    return r
