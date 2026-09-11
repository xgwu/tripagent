# -*- coding: utf-8 -*-
"""M5/M6 跨城 A/B 评测：基线（标签硬过滤） vs M1（检索→LLM库内选择） vs M2（TOPTW）。

在 M1 版 eval_ab 基础上扩展：
- 5 城批量评测（杭/宁/沪/苏/汉），--city 可跑单城
- --eval-date 启用日期感知（闭馆日硬约束），报告统计「闭馆违规」
- --hotel 启用 M6 住宿锚点（每城确定性酒店，显式坐标免 API），报告统计「返程违规/最晚收尾/酒店出发腿/跨天重复」
- Personas 按城市模板化生成（菜系本地化）

用法：
  python eval_ab.py                          # 全城 × 全 persona × 3 方案（LLM 可用则用）
  python eval_ab.py --city 苏州              # 单城
  python eval_ab.py --eval-date 2026-09-14   # 周一起始，压力测试闭馆约束
  python eval_ab.py --hotel                  # M6：带酒店锚点评测 → output/m6_hotel_report.html
  python eval_ab.py --no-llm                 # 纯离线管线冒烟
输出：output/m5_multi_city_report.html（或 m6_hotel_report.html）
"""
import argparse, html, io, os, sys, time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src import poi_db, m1_planner, m2_planner, baseline, metrics, llm_client

CITIES = ["杭州", "南京", "上海", "苏州", "武汉"]

# M6 评测酒店锚点：每城一个确定性位置（显式坐标，不走高德 API —— 批量评测要可复现、不烧配额）
HOTELS = {
    "杭州": "西湖国宾馆@120.1337,30.2350",
    "南京": "新街口中心大酒店@118.7835,32.0415",
    "上海": "人民广场酒店@121.4737,31.2304",
    "苏州": "观前街酒店@120.6245,31.3155",
    "武汉": "江汉路酒店@114.2890,30.5810",
}

CUISINE = {"杭州": "杭帮菜", "南京": "金陵风味小吃", "上海": "本帮菜",
           "苏州": "苏帮菜", "武汉": "武汉过早小吃"}

PERSONA_TEMPLATES = [
    {"name": "亲子休闲", "query": "带5岁孩子去{city}玩2天，不要太累，最好有动物或者博物馆"},
    {"name": "经典文化", "query": "{city}2天经典深度游，喜欢历史文化、寺庙和博物馆"},
    {"name": "拍照轻旅", "query": "{city}2天，喜欢拍照打卡和小众地方，晚上想逛夜市"},
    {"name": "雨天室内", "query": "{city}下雨天玩2天，想多安排室内场馆、博物馆和演出"},
    {"name": "美食漫步", "query": "{city}2天美食之旅，想吃地道{food}，顺便逛逛老街"},
    {"name": "户外自然", "query": "{city}2天，喜欢徒步和自然风光，想爬山看水景和园林"},
]

DAY_COLORS = ["#1a5fb4", "#1d7a4c", "#b7791f", "#c0392b", "#6b46c1"]


def personas_for(city: str) -> list:
    food = CUISINE.get(city, "本地菜")
    return [{"name": t["name"], "query": t["query"].format(city=city, food=food), "days": 2}
            for t in PERSONA_TEMPLATES]


def route_svg(result: dict, city: dict, width=420, height=300):
    """每日路线散点 SVG（按坐标投影，颜色分天）。"""
    all_pois = {p["id"]: p for p in city["pois"]}
    lats = [p["lat"] for p in all_pois.values()]
    lngs = [p["lng"] for p in all_pois.values()]
    min_lat, max_lat, min_lng, max_lng = min(lats), max(lats), min(lngs), max(lngs)
    pad = 0.03

    def xy(lat, lng):
        x = (lng - min_lng) / (max_lng - min_lng) * (width - 50) + 30
        y = (1 - (lat - min_lat) / (max_lat - min_lat)) * (height - 40) + 25
        return round(x, 1), round(y, 1)

    parts = [f'<svg viewBox="0 0 {width} {height}" style="width:100%;background:#f7f8fa;border:1px solid #e4e7eb;border-radius:8px;">']
    for d in result["itinerary"]["days"]:
        color = DAY_COLORS[(d["day"] - 1) % len(DAY_COLORS)]
        pts = [xy(all_pois[s["id"]]["lat"], all_pois[s["id"]]["lng"])
               for s in d["timeline"] if s["type"] == "poi"]
        if len(pts) > 1:
            poly = " ".join(f"{x},{y}" for x, y in pts)
            parts.append(f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.5" opacity="0.55" stroke-dasharray="4 3"/>')
        for i, (x, y) in enumerate(pts):
            parts.append(f'<circle cx="{x}" cy="{y}" r="4" fill="{color}" stroke="#fff" stroke-width="1"/>')
    # M6：酒店锚点标记（五角星形）
    if result.get("hotel"):
        hx, hy = xy(result["hotel"]["lat"], result["hotel"]["lng"])
        parts.append(f'<path d="M {hx} {hy - 8} L {hx + 2.4} {hy - 2.4} L {hx + 8} {hy - 2.4} '
                     f'L {hx + 3.5} {hy + 1.5} L {hx + 5} {hy + 7.5} L {hx} {hy + 3.8} '
                     f'L {hx - 5} {hy + 7.5} L {hx - 3.5} {hy + 1.5} L {hx - 8} {hy - 2.4} '
                     f'L {hx - 2.4} {hy - 2.4} Z" fill="#b7791f" stroke="#fff" stroke-width="0.8"/>')
        parts.append(f'<text x="{hx + 11}" y="{hy + 4}" font-size="10" fill="#b7791f">🏨</text>')
    parts.append("</svg>")
    return "".join(parts)


def hotel_metrics(result: dict, city: dict) -> dict:
    """M6 酒店维度指标：返程违规 / 最晚收尾 / Day1 出发腿（酒店→首站，路网）/ 跨天重复。"""
    itin = result["itinerary"]
    ret_viol = sum(1 for d in itin["days"] for v in d.get("violations", [])
                   if v["reason"].startswith("返回酒店"))
    finishes = [d["finish"] for d in itin["days"] if d.get("finish")]
    latest = max(finishes) if finishes else "—"
    dep_min = None
    h = result.get("hotel")
    if h:
        all_pois = {p["id"]: poi_db.parse_poi(p, city) for p in city["pois"]}
        first = next((s for d in itin["days"] for s in d["timeline"] if s["type"] == "poi"), None)
        if first and first["id"] in all_pois:
            dep_min = round(poi_db.travel_hours(h, all_pois[first["id"]]) * 60)
    return {"ret_viol": ret_viol, "latest_finish": latest,
            "dep_min": dep_min, "n_dup": result.get("n_dup_across_days", 0),
            "name": h["name"] if h else None}


def closed_violations(result: dict) -> int:
    """统计「当日闭馆」类硬约束违规数（修复后仍存在 = 日期感知失败）。"""
    return sum(1 for d in result["itinerary"]["days"]
               for v in d.get("violations", []) if v["reason"].startswith("当日闭馆"))


def plan_cell(ev: dict, result: dict, label: str = "", hm: dict | None = None) -> str:
    if ev is None:
        return f'<span class="badge b-red">❌ {result.get("mode", "error")}</span>'
    b_g, b_r = '<span class="badge b-green">', '<span class="badge b-red">'
    v = ev["hard_violations_final"]
    v_badge = f"{b_g}✅ {v} 违规</span>" if v == 0 else f"{b_r}❌ {v} 违规</span>"
    extra = ""
    if label == "M2":
        extra = f'<span class="badge b-blue">求解 {result.get("toptw_solved_days", "?")}/{result["days"]} 日</span>' \
                f'<span class="badge b-amber">求解器换点/删点 {len(result.get("toptw_dropped", []))}</span>' \
                + ("<span class=\"badge b-green\">文案已重生成</span>" if result.get("reasons_regen") else "")
    rows = "".join(
        f'<div class="dayrow"><b>Day {d["day"]}</b>｜{html.escape(d["theme"] or "")}｜{d["n_pois"]} 点｜'
        f'交通 {d["travel_km"]}km{"｜⚠️重排" if d["reordered"] else ""}'
        f'<div class="reason">{html.escape((d["reason"] or "")[:90])}</div></div>'
        for d in ev["per_day"])
    dropped = ev.get("dropped_pois", [])
    drop_note = f'<div class="reason">🗑 修复剔除: {html.escape("; ".join(d["name"] + "（" + d["reason"] + "）" for d in dropped))}</div>' if dropped else ""
    subs = result["itinerary"].get("substitutes", [])
    sub_note = ""
    if subs:
        sub_note = '<div class="reason">🔁 LLM 替代推荐: ' + html.escape(
            "; ".join(f"{s['for']} → {s['poi_id']}（Day {s['day']}）" for s in subs)) + "</div>"
    # M6：酒店维度徽标
    hotel_note = ""
    if hm:
        dep = f'{hm["dep_min"]}min' if hm["dep_min"] is not None else "—"
        ret_badge = (f'<span class="badge b-green">🏨 返程违规 0</span>' if hm["ret_viol"] == 0
                     else f'<span class="badge b-red">🏨 返程违规 {hm["ret_viol"]}</span>')
        hotel_note = (f'<div style="margin-top:6px">{ret_badge}'
                      f'<span class="badge b-amber">最晚收尾 {hm["latest_finish"]}</span>'
                      f'<span class="badge b-blue">Day1 酒店出发 {dep}</span>'
                      f'<span class="badge {"b-green" if hm["n_dup"] == 0 else "b-amber"}">跨天重复 {hm["n_dup"]}</span></div>')
    return (f'{v_badge} 相邻均距 <b>{ev["avg_adjacent_min"]}min</b>路网（{ev["avg_adjacent_km"]}km 直线）｜{ev["n_pois"]} 点｜'
            f'标签覆盖 {ev["tag_coverage"]}｜时段合规 {ev.get("best_time_rate", "n/a")}｜约¥{ev["est_cost_cny"]}'
            f'<div class="days">{rows}</div>{drop_note}{sub_note}{hotel_note}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--city", default=None, help="只跑指定城市（默认全 5 城）")
    ap.add_argument("--eval-date", default=None, help="行程起始日期 YYYY-MM-DD（启用闭馆约束）")
    ap.add_argument("--hotel", action="store_true", help="M6：启用住宿锚点评测（每城确定性酒店）")
    args = ap.parse_args()
    use_llm = (not args.no_llm) and llm_client.llm_available()

    cities = [args.city] if args.city else CITIES
    all_city_rows = []   # 汇总表：每城 × 3 方案
    city_sections = ""   # 分城明细

    for city_name in cities:
        city = poi_db.load_city(city_name)
        persons = personas_for(city_name)
        hotel_text = HOTELS.get(city_name) if args.hotel else None
        n_closed_db = sum(1 for p in city["pois"] if p.get("closed_days"))
        wd0 = poi_db.trip_weekday(args.eval_date, 1) if args.eval_date else None
        n_closed_that_day = (sum(1 for p in city["pois"]
                                 if wd0 in p.get("closed_days", [])) if wd0 else 0)

        results = []
        for ps in persons:
            row = {"persona": ps["name"], "query": ps["query"], "days": ps["days"]}
            t0 = time.time()
            base_res = baseline.plan(city, ps["query"], ps["days"])
            row["baseline"] = (metrics.evaluate(base_res, city, ps["query"]), base_res)
            m1_res = m1_planner.plan(city, ps["query"], ps["days"], use_llm=use_llm,
                                     date0=args.eval_date, hotel_text=hotel_text)
            row["m1"] = (metrics.evaluate(m1_res, city, ps["query"]), m1_res)
            try:
                m2_res = m2_planner.plan(city, ps["query"], ps["days"], use_llm=use_llm,
                                         date0=args.eval_date, hotel_text=hotel_text)
                row["m2"] = (metrics.evaluate(m2_res, city, ps["query"]), m2_res)
            except Exception as e:  # noqa: 求解层异常不阻塞报告
                row["m2"] = (None, {"mode": f"m2_error({type(e).__name__})"})
            row["elapsed"] = round(time.time() - t0, 1)
            results.append(row)
            m2ev = row["m2"][0]
            hotel_tag = f"; 🏨返程违规 {hotel_metrics(m1_res, city)['ret_viol']}+{hotel_metrics(m2_res, city)['ret_viol'] if m2ev else 'ERR'}" if hotel_text else ""
            print(f"✅ [{city_name}] {ps['name']} 完成（基线 "
                  f"{row['baseline'][0]['avg_adjacent_min']}min vs M1 {row['m1'][0]['avg_adjacent_min']}min vs "
                  f"M2 {m2ev['avg_adjacent_min'] if m2ev else 'ERR'}min 路网均程; 违规 "
                  f"{row['baseline'][0]['hard_violations_final']} vs {row['m1'][0]['hard_violations_final']} vs "
                  f"{m2ev['hard_violations_final'] if m2ev else 'ERR'}{hotel_tag}）", flush=True)

        # 分城汇总行
        for key, label in [("baseline", "基线"), ("m1", "M1"), ("m2", "M2")]:
            evs = [r[key][0] for r in results if r[key][0] is not None]
            if not evs:
                all_city_rows.append({"city": city_name, "label": label, "n": 0})
                continue
            agg = {"city": city_name, "label": label, "n": len(evs),
                   "viol": sum(e["hard_violations_final"] for e in evs),
                   "closed": sum(closed_violations(r[key][1]) for r in results if r[key][0]),
                   "min": round(sum(e["avg_adjacent_min"] for e in evs) / len(evs)),
                   "pois": round(sum(e["n_pois"] for e in evs) / len(evs), 1),
                   "lat": round(sum(e["latency_s"] for e in evs) / len(evs), 1)}
            if args.hotel and key != "baseline":
                hms = [hotel_metrics(r[key][1], city) for r in results if r[key][0]]
                deps = [h["dep_min"] for h in hms if h["dep_min"] is not None]
                agg.update({
                    "ret_viol": sum(h["ret_viol"] for h in hms),
                    "latest": max(h["latest_finish"] for h in hms),
                    "dep": round(sum(deps) / len(deps)) if deps else None,
                    "dup": sum(h["n_dup"] for h in hms)})
            all_city_rows.append(agg)

        # 分城明细 HTML
        rows_html = ""
        for r in results:
            rows_html += f"""
    <h3>👤 {r['persona']}（{r['days']} 天）<span class="badge b-blue">{r['elapsed']}s</span></h3>
    <p class="q">「{html.escape(r['query'])}」</p>
    <div class="ab">
      <div class="col"><h4>基线：标签硬过滤 + 最近邻</h4>{plan_cell(r['baseline'][0], r['baseline'][1], '基线')}{route_svg(r['baseline'][1], city)}</div>
      <div class="col"><h4>M1：检索 → LLM 库内选择 → 贪婪+修复</h4>{plan_cell(r['m1'][0], r['m1'][1], 'M1', hotel_metrics(r['m1'][1], city) if args.hotel else None)}{route_svg(r['m1'][1], city)}</div>
      <div class="col"><h4>M2：+ OR-Tools TOPTW 求解层</h4>{plan_cell(r['m2'][0], r['m2'][1], 'M2', hotel_metrics(r['m2'][1], city) if (args.hotel and r['m2'][0]) else None)}{route_svg(r['m2'][1], city) if r['m2'][0] else ''}</div>
    </div>"""

        date_note = (f"｜📅 评测起始日 {args.eval_date}（{wd0}，{n_closed_that_day} 个 POI 当日闭馆）"
                     if args.eval_date else "（未指定日期，闭馆约束未启用）")
        city_sections += f"""
<h2>🏙 {city_name} <span class="badge b-blue">POI 库 {len(city['pois'])} 点，{n_closed_db} 个有闭馆日</span>{date_note}</h2>
{rows_html}"""

    # 总汇总表（M6 酒店模式下追加酒店维度列）
    if args.hotel:
        summary_rows = ""
        for r in all_city_rows:
            city_td = f"<td rowspan='3'>{r['city']}</td>" if r["label"] == "基线" else ""
            if r["label"] == "基线":
                summary_rows += (f"<tr>{city_td}<td>{r['label']}</td>"
                                 f"<td>{r['viol'] if r['n'] else '—'}</td><td>{r['closed'] if r['n'] else '—'}</td>"
                                 f"<td>{(str(r['min']) + 'min') if r['n'] else '—'}</td>"
                                 f"<td colspan='4' style='color:#98a2b3'>不支持酒店锚点（对照项）</td>"
                                 f"<td>{r['pois'] if r['n'] else '—'}</td><td>{r['lat'] if r['n'] else '—'}s</td></tr>")
            else:
                summary_rows += (f"<tr>{city_td}<td>{r['label']}</td>"
                                 f"<td>{r['viol'] if r['n'] else '—'}</td><td>{r['closed'] if r['n'] else '—'}</td>"
                                 f"<td>{(str(r['min']) + 'min') if r['n'] else '—'}</td>"
                                 f"<td>{r.get('ret_viol', '—')}</td><td>{r.get('latest', '—')}</td>"
                                 f"<td>{(str(r['dep']) + 'min') if r.get('dep') is not None else '—'}</td>"
                                 f"<td>{r.get('dup', '—')}</td>"
                                 f"<td>{r['pois'] if r['n'] else '—'}</td><td>{r['lat'] if r['n'] else '—'}s</td></tr>")
        summary_head = ("<tr><th>城市</th><th>方案</th><th>硬约束违规(总)</th><th>闭馆违规(总)</th><th>相邻路网均程</th>"
                        "<th>返程违规(总)</th><th>最晚收尾</th><th>Day1酒店出发均程</th><th>跨天重复(总)</th>"
                        "<th>日均 POI 数</th><th>平均耗时</th></tr>")
    else:
        summary_rows = "".join(
            f"<tr><td rowspan='3'>{r['city']}</td><td>{r['label']}</td>"
            f"<td>{r['viol'] if r['n'] else '—'}</td><td>{r['closed'] if r['n'] else '—'}</td>"
            f"<td>{(str(r['min']) + 'min') if r['n'] else '—'}</td>"
            f"<td>{r['pois'] if r['n'] else '—'}</td><td>{r['lat'] if r['n'] else '—'}s</td></tr>"
            for r in all_city_rows)
        summary_head = ("<tr><th>城市</th><th>方案</th><th>硬约束违规(总)</th><th>闭馆违规(总)</th>"
                        "<th>相邻路网均程</th><th>日均 POI 数</th><th>平均耗时</th></tr>")

    llm_note = f"LLM 运行模式：<b>{'llm' if use_llm else 'offline_fallback'}</b>" + (
        "" if use_llm else "（未检测到 DEEPSEEK_API_KEY，当前为离线兜底管线冒烟）")

    hotel_note = ""
    if args.hotel:
        hotel_note = (f"<p class='note'>🏨 <b>M6 酒店锚点维度</b>：每城使用确定性酒店（显式坐标，免高德 API）："
                      f"{'；'.join(f'{c}→{HOTELS[c].split(chr(64))[0]}' for c in cities)}。"
                      "每日从酒店出发、day_end 前返回酒店（返程超时为硬约束违规）；跨天重复 = 同一 POI 出现在多天（&gt;0 即去重失效）。"
                      "基线无酒店能力，作为对照组保留原口径。</p>")
    mode_label = "M6 · 酒店锚点" if args.hotel else "M5 · 跨城"
    html_doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>TripAgent {mode_label} 评测报告</title><style>
body{{font-family:"Microsoft YaHei",sans-serif;color:#1f2933;background:#fff;max-width:1100px;margin:0 auto;padding:40px;line-height:1.7}}
h1{{font-size:22px;border-bottom:3px solid #1a5fb4;padding-bottom:10px}} h2{{font-size:17px;margin-top:36px;border-bottom:2px solid #e4e7eb;padding-bottom:6px}}
h3{{font-size:15px;margin-top:28px;color:#1a5fb4}} h4{{font-size:14px;margin:8px 0}}
.badge{{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;margin-left:6px}}
.b-green{{background:#eaf6f0;color:#1d7a4c}} .b-amber{{background:#fdf6e7;color:#b7791f}}
.b-red{{background:#fdf0ee;color:#c0392b}} .b-blue{{background:#eef4fc;color:#1a5fb4}}
.ab{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}}
.col{{border:1px solid #e4e7eb;border-radius:10px;padding:14px 16px}}
.q{{color:#52606d;font-size:13.5px;margin-top:0}}
.dayrow{{font-size:13px;border-top:1px dashed #e4e7eb;padding:6px 0}}
.reason{{color:#52606d;font-size:12px}}
.note{{background:#eef4fc;border-left:4px solid #1a5fb4;padding:10px 14px;border-radius:8px;font-size:13.5px}}
table{{border-collapse:collapse;font-size:13px;width:100%}}
th{{background:#1a5fb4;color:#fff;padding:6px 10px;text-align:left}} td{{border-bottom:1px solid #e4e7eb;padding:6px 10px}}
</style></head><body>
<h1>🧪 TripAgent {mode_label} · 跨城三方评测报告</h1>
<p class="note">{llm_note}｜评测城市：{'、'.join(cities)}× {len(PERSONA_TEMPLATES)} persona<br>
指标口径：硬约束违规（营业时间/时间窗/<b>闭馆日</b>/<b>返程</b>）；闭馆违规 = 修复后仍含「当日闭馆」的次数（&gt;0 即日期感知失效）；相邻路网均程 = 相邻 POI 的 OSRM/高德真实路网通行时间均值；标签覆盖率；时段合规 = best_time 命中率。M2 = M1 + OR-Tools TOPTW 求解层，修复后 LLM 重生成文案。</p>
{hotel_note}
<h2>📊 全城汇总</h2>
<table>{summary_head}
{summary_rows}
</table>
{city_sections}
<p style="color:#52606d;font-size:12px;margin-top:30px">生成时间 {time.strftime('%Y-%m-%d %H:%M')}｜TripAgent M5 eval_ab.py</p>
</body></html>"""

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(out, exist_ok=True)
    fname = "m6_hotel_report.html" if args.hotel else "m5_multi_city_report.html"
    path = os.path.join(out, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print(f"\n📄 报告已生成: {path}")


if __name__ == "__main__":
    main()
