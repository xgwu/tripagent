# -*- coding: utf-8 -*-
"""M1 单次规划 CLI。

用法：
  python main.py "带5岁孩子去杭州玩2天，不要太累" --days 2
  python main.py "杭州2天经典深度游，喜欢历史文化" --days 2 --no-llm
"""
import argparse, json, os, sys, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src import poi_db, m1_planner, metrics, llm_client


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--city", default="杭州")
    ap.add_argument("--no-llm", action="store_true", help="强制离线兜底模式")
    ap.add_argument("--m2", action="store_true", help="使用 M2 TOPTW 求解链路（需 ortools venv）")
    ap.add_argument("--proposal", action="store_true",
                    help="M7 经验提案模式：LLM 世界知识自由提案 → 落地匹配 → TOPTW")
    ap.add_argument("--date", default=None, help="行程起始日期 YYYY-MM-DD（启用闭馆日约束）")
    ap.add_argument("--hotel", default=None,
                    help="住宿锚点：酒店名（AMAP_KEY 自动定位）或「名称@lng,lat」")
    args = ap.parse_args()

    # 天数解析与 webui 对齐：query 明确写了天数（「一天」「2天」「3日」）→ 优先于 --days
    # （--days 大多是默认值 2 未被改动；query 文本是最强意图信号）
    from src.query_days import extract_days
    args.days = extract_days(args.query) or args.days

    city = poi_db.load_city(args.city)
    use_llm = (not args.no_llm) and llm_client.llm_available()
    if not use_llm:
        print("⚠️  LLM 不可用（未设置 DEEPSEEK_API_KEY 或 --no-llm），使用离线兜底模式\n")

    if args.proposal:
        from src import proposal_planner
        planner = proposal_planner
    elif args.m2:
        from src import m2_planner
        planner = m2_planner
    else:
        planner = m1_planner
    result = planner.plan(city, args.query, args.days, use_llm=use_llm, date0=args.date,
                          hotel_text=args.hotel)
    if args.date:
        print(f"📅 起始日期 {args.date}（{poi_db.trip_weekday(args.date, 1)}），闭馆日约束已启用")
    if result.get("hotel"):
        h = result["hotel"]
        print(f"🏨 酒店：{h['name']}（{h['resolved']}，{h['lat']:.4f},{h['lng']:.4f}）每日出发/返回")
    ev = metrics.evaluate(result, city, args.query)

    print(f"模式: {result['mode']}｜候选池: {result['candidates']}｜耗时: {result['latency_s']}s"
          + (f"｜prompt字符: {result.get('prompt_chars') or '-'}" if use_llm else "")
          + f"｜跨天重复: {result.get('n_dup_across_days', 0)}")
    print(f"幻觉POI: {len(result['invalid_poi_ids'])}｜修复前违规: {result['llm_raw_violations']}｜"
          f"修复后违规: {ev['hard_violations_final']}｜相邻POI均距: {ev['avg_adjacent_km']}km\n")
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(out_dir, exist_ok=True)
    out = {"query": args.query, **{k: v for k, v in result.items() if k != "itinerary"},
           "metrics": {k: v for k, v in ev.items() if k != "per_day"},
           "days": ev["per_day"]}
    path = os.path.join(out_dir, "last_plan.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    for d in ev["per_day"]:
        print(f"Day {d['day']}｜{d['theme']}｜{d['n_pois']} 个点｜日交通 {d['travel_km']}km")
        for s in result["itinerary"]["days"][d["day"] - 1]["timeline"]:
            mark = {"meal": "🍽", "hotel": "🏨"}.get(s["type"], "📍")
            print(f"  {s['start']}-{s['end']} {mark} {s['name']}")
        print(f"  理由: {d['reason'][:80]}\n")
    print(f"明细已写入 {path}")


if __name__ == "__main__":
    main()
