# -*- coding: utf-8 -*-
"""P2-5 基准回归：固化跨城基准查询，一键验证「0 违规 + 天数正确」不退化。

默认离线确定性路径（use_llm=False → M7 降级 M1 链路，不受 LLM 随机性影响），
适合每次改动后快速回归；--llm 可选走真实 M7 全链路（结果有随机性，仅观察）。

用法：
    python scripts/eval_regression.py            # 离线回归（CI 友好，非 0 退出码）
    python scripts/eval_regression.py --llm      # 真实 LLM 全链路（观察用）
    python scripts/eval_regression.py --agent-proposal   # Agent-Proposal 双模式（Phase 2）

--agent-proposal（docs/MCP-Agent提案外包-方案探讨.md §6 Phase 2）：用**确定性拟案器**
模拟「理想 agent」（先 find_pois 对齐库内事实 → 拟逐日案 → dry_run 校验 → 正式提交），
走 `proposal_planner.plan(agent_proposal=…)` 真实全链路，断言：
planSource=agent、不落 M2 兜底、天数正确、0 违规、落地率达标、提案保留率 ≥40%（观察性「整批丢弃」守卫：修正闭环会合法重排/替换提案，
详见 run_agent_proposal 内注释）。
B 阶段落地/修正仍走 LLM ⇒ 需要 API key（与 --llm 同一门禁）。
"""
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import poi_db, m1_planner, proposal_planner, config, llm_client  # noqa: E402

# (城市, 查询, 期望天数, 期望落地率下限[仅 --llm 生效], 必含 POI id[可选])
CASES = [
    ("上海", "上海2天经典深度游，喜欢历史文化、寺庙和博物馆", 2, 0.8),
    ("上海", "上海2天骑行，喜欢咖啡和美食", 2, 0.8),
    ("上海", "带5岁孩子去上海玩2天，不要太累，最好有动物或者博物馆", 2, 0.8),
    ("上海", "上海下雨天玩2天，想多安排室内场馆", 2, 0.7),
    ("上海", "上海3日亲子游", 3, 0.8, "SH006"),  # 迪士尼：亲子游必含（全天大点豁免回归）
    ("上海", "上海3日亲子游，住迪士尼附近酒店", 3, 0.8, "SH006", "迪士尼"),  # 住宿锚点地标匹配
    ("杭州", "杭州2天骑行，喜欢咖啡和美食", 2, 0.8),
    ("杭州", "杭州2天历史文化深度游", 2, 0.8),
    ("南京", "带5岁孩子去南京玩2天，不要太累", 2, 0.8),
    ("苏州", "苏州2天园林和美食漫游", 2, 0.8),
    ("武汉", "武汉2天早餐美食和长江桥梁打卡", 2, 0.8),
    ("成都", "成都2天经典深度游，喜欢历史文化、寺庙和博物馆", 2, 0.8),
    ("北京", "北京2天经典深度游，喜欢历史文化、寺庙和博物馆", 2, 0.8),
    ("广州", "广州2天经典深度游，喜欢历史文化、美食和珠江夜景", 2, 0.8),
    ("盐城", "盐城2天经典深度游，喜欢历史文化、寺庙和博物馆", 2, 0.8),
    ("西安", "西安2天经典深度游，喜欢历史文化、寺庙和博物馆", 2, 0.8),
    ("重庆", "重庆2天经典深度游，喜欢历史文化、美食和长江夜景", 2, 0.8),
    ("长沙", "长沙2天经典深度游，喜欢历史文化、美食和博物馆", 2, 0.8),
    # --- 贵州目的地（2026-09-17 方案B 开城：半径 28km/景区 20km，19 个目的地）---
    # 景区型目的地必须先命中招牌点：faithful 模式（lock_mains）本来就这么保证，
    # 这组用例把它变成回归守门（落地率阈值仅 --llm 时生效，必含点两者都查）。
    ("贵阳", "贵阳2天经典深度游，喜欢历史文化、博物馆和甲秀楼", 2, 0.8),
    ("贵阳", "带5岁孩子去贵阳玩2天，不要太累，最好有动物或者科技馆", 2, 0.8),
    ("遵义", "遵义2天红色文化深度游，想看遵义会议会址和海龙屯", 2, 0.8, "ZY002"),
    ("六盘水", "六盘水2天避暑休闲，喜欢自然风光", 2, 0.8),
    ("安顺", "安顺2天，想去龙宫和屯堡古寨", 2, 0.8),
    ("黄果树", "黄果树2日游，想看大瀑布", 2, 0.8, "HG001"),
    ("西江千户苗寨", "西江千户苗寨2日游，想体验苗族风情", 2, 0.8, "XJ001"),
    ("小七孔", "荔波小七孔2日游，喜欢山水和瀑布", 2, 0.8,
     ("XQ001", "XQ004", "XQ005", "XQ006")),   # 伞形景区或其子点任一即可
    ("梵净山", "梵净山2日游，喜欢爬山和自然风光", 2, 0.8, "FJ001"),
    ("织金洞", "织金洞1日游，想看溶洞奇观", 1, 0.8, "ZJ001"),
    ("镇远古城", "镇远古城2日游，喜欢古镇和夜景", 2, 0.8, "ZG001"),
    ("中国天眼", "中国天眼2日游，带孩子看天文科普", 2, 0.8,
     ("TY001", "TY005")),                       # 景区本体或 FAST 任一即可
    ("万峰林", "万峰林2日游，喜欢喀斯特峰林", 2, 0.8, "WF001"),
    # 新疆片区化开城（2026-10-14）：喀纳斯 + 剩余 5 片区
    # ⚠️ 必含点取「行程内可达的招牌点」：离线 2 日受里程约束，远郊招牌（天池 47km/
    #    赛里木湖 63km/魔鬼城 50km）排不进属**正确行为**，别拿它们当必含（会假红）。
    ("喀纳斯", "喀纳斯3日游，看湖景和禾木村", 3, 0.8, "KN006"),
    ("乌鲁木齐", "乌鲁木齐2日游，逛大巴扎和红山公园", 2, 0.8, "WL002"),
    ("伊犁", "伊犁2日游，逛喀赞其和六星街", 2, 0.7, "YL007"),
    ("吐鲁番", "吐鲁番2日游，看火焰山和坎儿井", 2, 0.8, ("TL007", "TL012")),
    ("克拉玛依", "克拉玛依2日游，看黑油山和克拉玛依河", 2, 0.7, "KM003"),
    # 南疆拆分 + 独山子（2026-10-14 第三批）
    # ⚠️ 必含点取**离线 2 日实排**的招牌点（远郊招牌 慕士塔格/卡拉库里湖/天山神秘大峡谷
    #    在 2 日里未必排入属正确行为，别拿来当必含）
    ("喀什", "喀什2日游，逛老城和看香妃园", 2, 0.8, "KS005"),
    ("叶尔羌", "叶尔羌2日游，看叶尔羌汗国王陵和金湖杨", 2, 0.7, "YE005"),
    ("帕米尔", "帕米尔2日游，看帕米尔高原和金草滩", 2, 0.7, "PM006"),
    ("库车", "库车2日游，看天山神秘大峡谷和库车王府", 2, 0.8, "KC011"),
    ("阿克苏", "阿克苏2日游，看天山托木尔大峡谷和博物馆", 2, 0.7, "AK008"),
    ("独山子", "独山子2日游，看独山子大峡谷和泥火山", 2, 0.8, "DS010"),
]


# ---- Agent-Proposal 双模式（Phase 2）评测用例 ---------------------------------
# (城市, 查询, 期望天数, 每天提案点数, 必含 POI id[模拟好 agent 会主动纳入的招牌点])
# 只选 4 个代表性城市控制成本：B 落地/修正走真实 LLM（每例约 3-6 次调用）。
AGENT_CASES = [
    ("上海", "带5岁孩子去上海玩3天，不要太累，最好有动物或者博物馆", 3, 4, "SH006"),
    ("杭州", "杭州2天骑行，喜欢咖啡和美食", 2, 4, None),
    ("苏州", "苏州2天园林和美食漫游", 2, 4, "SZ001"),
    ("黄果树", "黄果树2日游，想看大瀑布", 2, 3, "HG001"),
]


def _draft_proposal(city: dict, days: int, per_day: int, must_id: str | None,
                    query: str) -> dict:
    """确定性拟案器：模拟「理想 agent」——只用库内真实 POI（find_pois 对齐口径）。

    不用 LLM：评测目标是**提案链路本身**（planSource / 落地 / 硬约束），
    拟案质量只要「确定性、可复现、全部库内点」即可。
    装填按**时长预算**（每天累计 duration ≈ 6.5h、上限 per_day 点）——
    教训（首版踩坑）：固定「每天 4 点 × 天数」会把当日真实容量撑爆，
    TOPTW/里程约束剔除超额点 ⇒ 保留率 50~62% 假红。贪心装填后保留率回到 100%。
    """
    family = any(w in query for w in ("孩子", "亲子", "婴儿车"))
    cands = []
    for raw in city["pois"]:
        p = poi_db.parse_poi(raw, city)
        if proposal_planner._is_lodging(raw):
            continue
        if family and p.get("family_ok") is False:
            continue
        cands.append(p)
    # 确定性排序：rating 降序（缺省排后）→ id 升序；必含点强制置顶
    def _key(p):
        r = p.get("rating")
        return (-(float(r) if isinstance(r, (int, float)) else 0.0), p["id"])
    cands.sort(key=_key)
    if must_id:
        cands = [p for p in cands if p["id"] == must_id] + \
                [p for p in cands if p["id"] != must_id]
    by_id = {p["id"]: p for p in cands}
    # 逐日贪心装填：先到先得（全局唯一），每天预算 6.5h 游览时长、至多 per_day 点；
    # **area 多样性**：同一片区全局至多 2 点——真 agent 不会把西湖沿线堆 6 个同质点
    # （2026-10-11 杭州踩坑：主题盲拟案 6 点挤 west 片区 ⇒ 修正闭环整日合法重排 ⇒ 假红）。
    used: set = set()
    area_cnt: dict = {}
    out = []
    for d in range(1, days + 1):
        stops, budget = [], 6.5
        for p in cands:
            if len(stops) >= per_day or budget <= 0:
                break
            if p["id"] in used:
                continue
            if area_cnt.get(p.get("area") or "?", 0) >= 2:
                continue
            dur = p.get("duration_h")
            dur = float(dur) if isinstance(dur, (int, float)) and dur > 0 else 1.5
            stops.append(p)
            used.add(p["id"])
            area_cnt[p.get("area") or "?"] = area_cnt.get(p.get("area") or "?", 0) + 1
            budget -= dur
        out.append({
            "day": d,
            "theme": f"评测拟案 Day {d}",
            "reason": "确定性拟案器生成（eval agent-proposal 模式）",
            "stops": [{"id": p["id"], "name": p["name"], "note": "库内点"}
                      for p in stops],
        })
    return {"days": out}


def run_agent_proposal() -> None:
    """Phase 2 双模式评测：agent 拟案 → dry_run 校验 → 正式提交 → 断言口径。"""
    if not llm_client.llm_available():
        print("❌ --agent-proposal 需要 API key（B 落地/修正走真实 LLM，同 --llm 门禁）。")
        sys.exit(2)
    n_fail = 0
    print(f"{'城市':<4} {'查询':<28} {'天数':>4} {'实际':>4} {'违规':>4} {'落地率':>7}"
          f" {'保留率':>7} {'耗时':>7}  结果")
    print("-" * 100)
    t_all = time.time()
    for case in AGENT_CASES:
        city_name, query, want_days, per_day, must_id = case
        city = poi_db.load_city(city_name)
        prop = _draft_proposal(city, want_days, per_day, must_id, query)
        # 第 1 步：dry_run 校验（与正式规划同一套 _ground_one，零 LLM）——好 agent 的自检
        dry = proposal_planner.validate_proposal(city, prop, want_days)
        t0 = time.time()
        try:
            r = proposal_planner.plan(city, query, want_days, use_llm=True,
                                      agent_proposal=prop)
        except Exception as e:  # noqa
            print(f"{city_name:<4} {query:<28}    --    --    --       --       --"
                  f"       --     ❌ 异常 {type(e).__name__}: {e}")
            n_fail += 1
            continue
        sec = time.time() - t0
        it = r["itinerary"]
        n_days = len(it["days"])
        viol = it["total_violations"]
        g = r.get("grounding") or {}
        rate = g.get("grounding_rate", 1.0)
        # 提案点保留率：拟案里的库内 id 有多少真的排进行程
        kept = {s.get("id") for d in it["days"]
                for s in d.get("timeline", []) if s.get("type") == "poi"}
        draft_ids = {s["id"] for d in prop["days"] for s in d["stops"]}
        keep_rate = (len(draft_ids & kept) / len(draft_ids)) if draft_ids else 1.0
        problems = []
        if not dry.get("ok"):
            problems.append(f"dry_run 未全命中（unmatched={len(dry.get('unmatched') or [])}）")
        if r.get("plan_source") != "agent":
            problems.append(f"planSource={r.get('plan_source')}≠agent")
        if "m2_fallback" in str(r.get("mode", "")):
            problems.append("落入 M2 兜底（提案链路失败）")
        if n_days != want_days:
            problems.append(f"天数 {n_days}≠{want_days}")
        if viol != 0:
            problems.append(f"违规 {viol}")
        if rate < 0.8:
            problems.append(f"落地率 {rate:.0%}<80%")
        # 保留率阈值 40%＝「整批丢弃」守卫（观察性口径，非逐点锁定）：
        # B 阶段修正闭环会**合法重排/替换**提案——整日重组（杭州 Day2→西线）、
        # 质量正向替换（黄果树：夜游点→大瀑布）、TOPTW 剔边际点（2026-10-11 实测
        # 单例保留率天然波动 50~100%）。≥40% 即可证明提案确实被消费而非无视；
        # 「提案被消费」另有 planSource=agent 硬断言兜底。
        if keep_rate < 0.4:
            problems.append(f"提案保留率 {keep_rate:.0%}<40%")
        ok = "✅" if not problems else "❌ " + "；".join(problems)
        n_fail += 0 if not problems else 1
        print(f"{city_name:<4} {query[:26]:<28} {want_days:>4} {n_days:>4} {viol:>4}"
              f" {rate:>6.0%} {keep_rate:>6.0%} {sec:>6.1f}s  {ok}")
    print("-" * 100)
    print(f"Agent-Proposal 双模式：合计 {len(AGENT_CASES)} 例，失败 {n_fail}，"
          f"总耗时 {time.time()-t_all:.1f}s（B 阶段真实 LLM）")
    sys.exit(1 if n_fail else 0)


def main():
    if "--agent-proposal" in sys.argv:
        run_agent_proposal()
    use_llm = "--llm" in sys.argv
    # --llm 前置门禁：缺 key 时 plan 会静默降级到离线链路——「跑得飞快、结果看着还行」，
    # 但测的根本不是 LLM 路径（2026-09-15 踩坑：全程 0.0s，实为降级）。宁可明确失败。
    if use_llm:
        injected = config.apply_env()
        if not llm_client.llm_available():
            print("❌ --llm 需要 API key：secrets.json 缺 deepseek_api_key，"
                  "或显式 export DEEPSEEK_API_KEY。")
            print("   （缺 key 时 plan 不会报错，而是静默降级到离线链路——"
                  "那样跑出来的 14/14 不能代表 LLM 全链路）")
            sys.exit(2)
        print(f"LLM 就绪（本次注入：{injected or '无（环境变量已存在）'}）\n")
    # 锚点用例专测硬保障机制本身，eval 内显式开启（线上默认关）
    os.environ.setdefault("ANCHOR_HARD_GUARANTEE", "1")
    n_fail = 0
    print(f"{'城市':<4} {'查询':<28} {'天数':>4} {'实际':>4} {'违规':>4} {'落地率':>7} {'耗时':>6}  结果")
    print("-" * 88)
    t_all = time.time()
    for case in CASES:
        city_name, query, want_days, min_rate = case[:4]
        must_poi = case[4] if len(case) > 4 else None
        hotel_text = case[5] if len(case) > 5 else None
        city = poi_db.load_city(city_name)
        planner = proposal_planner if use_llm else m1_planner
        t0 = time.time()
        try:
            r = planner.plan(city, query, want_days, use_llm=use_llm,
                             hotel_text=hotel_text)
        except Exception as e:  # noqa
            print(f"{city_name:<4} {query:<28}    --    --    --       --     ❌ 异常 {type(e).__name__}: {e}")
            n_fail += 1
            continue
        sec = time.time() - t0
        it = r["itinerary"]
        n_days = len(it["days"])
        viol = it["total_violations"]
        g = r.get("grounding") or {}
        rate = g.get("grounding_rate", 1.0)
        problems = []
        if n_days != want_days:
            problems.append(f"天数 {n_days}≠{want_days}")
        if viol != 0:
            problems.append(f"违规 {viol}")
        if must_poi:
            kept = {s.get("id") for d in it["days"]
                    for s in d.get("timeline", []) if s.get("type") == "poi"}
            # must_poi 可以是单个 id，也可以是「任一即可」的 id 集合。
            # 景区型城市库里同时存在伞形点（荔波小七孔景区）与其子点（小七孔桥/卧龙潭/
            # 鸳鸯湖），LLM 常在子点层面提案——要求必含伞形点等于要求重复计算同一片景区。
            need = [must_poi] if isinstance(must_poi, str) else list(must_poi)
            if not set(need) & kept:
                problems.append(f"缺少必含点 {'/'.join(need)}")
        if use_llm and rate < min_rate:
            problems.append(f"落地率 {rate:.0%}<{min_rate:.0%}")
        ok = "✅" if not problems else "❌ " + "；".join(problems)
        n_fail += 0 if not problems else 1
        print(f"{city_name:<4} {query[:26]:<28} {want_days:>4} {n_days:>4} {viol:>4} {rate:>6.0%} {sec:>5.1f}s  {ok}")
    print("-" * 88)
    print(f"合计 {len(CASES)} 例，失败 {n_fail}，总耗时 {time.time()-t_all:.1f}s（{'LLM 全链路' if use_llm else '离线确定性'}）")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
