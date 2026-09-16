# -*- coding: utf-8 -*-
"""M7 A/B 评测：M2（库内直选） vs M7（经验提案→落地→求解）。

5 城 × 2 persona × 2 方案，指标：
- 落地率（M7 特有）：LLM 提案点成功匹配回库的比例
- 硬约束违规（修复后）、相邻路网均程、日均 POI 数、耗时
- POI 库缺口清单（M7 未落地提案 = 数据建设采购清单）

用法：
  python eval_m7.py                # 全城真实 LLM
  python eval_m7.py --city 杭州   # 单城
  python eval_m7.py --no-llm      # 管线冒烟
输出：output/m7_report.html
"""
import argparse, html, io, os, sys, time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src import poi_db, m2_planner, proposal_planner, metrics, llm_client

CITIES = ["杭州", "南京", "上海", "苏州", "武汉", "广州", "盐城", "西安", "重庆", "长沙"]
PERSONAS = [
    {"name": "经典文化", "q": "{c}2天经典深度游，喜欢历史文化、寺庙和博物馆", "days": 2},
    {"name": "亲子休闲", "q": "带5岁孩子去{c}玩2天，不要太累，最好有动物或者科技馆", "days": 2},
]

CSS = """
body{font-family:"Microsoft YaHei",sans-serif;margin:0;background:#f5f7fa;color:#1f2937}
.wrap{max-width:1080px;margin:0 auto;padding:24px 20px}
h1{font-size:21px}h2{font-size:17px;margin-top:28px}
.badge{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;margin-right:6px}
.b-green{background:#e6f4ea;color:#1d7a4c}.b-red{background:#fdecec;color:#c0392b}
.b-blue{background:#e8f0fe;color:#1a5fb4}.b-amber{background:#fef3e2;color:#b7791f}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;font-size:13.5px}
th{background:#1a5fb4;color:#fff;padding:8px 10px;text-align:left;font-weight:600}
td{padding:7px 10px;border-bottom:1px solid #eef1f5}
.gaps{background:#fff;border-radius:10px;padding:12px 16px;margin-bottom:16px;font-size:13px}
.gaps li{margin:3px 0}
.ab{display:flex;gap:14px;flex-wrap:wrap}.ab>div{flex:1;min-width:300px;background:#fff;border-radius:10px;padding:12px 14px}
"""


def eval_row(city, ps, use_llm, eval_date):
    query = ps["q"].format(c=city["city"])
    row = {"persona": ps["name"], "query": query}
    try:
        r2 = m2_planner.plan(city, query, ps["days"], use_llm=use_llm, date0=eval_date)
        row["m2"] = (metrics.evaluate(r2, city, query), r2)
    except Exception as e:  # noqa
        row["m2"] = (None, {"mode": f"m2_error({type(e).__name__})"})
    try:
        r7 = proposal_planner.plan(city, query, ps["days"], use_llm=use_llm, date0=eval_date)
        row["m7"] = (metrics.evaluate(r7, city, query), r7)
    except Exception as e:  # noqa
        row["m7"] = (None, {"mode": f"m7_error({type(e).__name__})"})
    return row


def cell(key, ev, res):
    if ev is None:
        return (f'<div><h4>{key}</h4>'
                f'<span class="badge b-red">❌ {html.escape(str(res.get("mode")))}</span></div>')
    viol = ev["hard_violations_final"]
    v_badge = (f'<span class="badge b-green">✅ {viol} 违规</span>' if viol == 0
               else f'<span class="badge b-red">❌ {viol} 违规</span>')
    g = res.get("grounding")
    g_line = ""
    if g:
        gaps = "、".join(x["name"] for x in g["gaps"]) or "无"
        g_line = (f'<span class="badge b-amber">🎯 落地率 {g["grounding_rate"]:.0%}'
                  f'（提案 {g["n_proposed"]}：精确 {g["exact"]}｜包含 {g["contain"]}｜'
                  f'模糊 {g["fuzzy"]}｜LLM {g["llm"]}）</span> '
                  f'<span class="badge b-blue">📚 库缺口：{html.escape(gaps)}</span><br>')
    return (f'<div><h4>{key}</h4>{v_badge} '
            f'{g_line}'
            f'<span class="badge b-blue">{ev["avg_adjacent_min"]}min 路网均程</span>'
            f'<span class="badge b-blue">{ev["n_pois"]} 点</span>'
            f'<span class="badge b-blue">耗时 {ev["latency_s"]}s</span>'
            f'<p class="note" style="font-size:12.5px">{html.escape((res["itinerary"]["days"][0].get("reason") or "")[:90])}…</p>'
            f'</div>')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--city", default=None)
    ap.add_argument("--eval-date", default=None)
    ap.add_argument("--title", default="🧪 M7 评测：经验提案 vs 库内直选")
    ap.add_argument("--out", default="m7_report.html")
    args = ap.parse_args()
    use_llm = (not args.no_llm) and llm_client.llm_available()
    cities = [args.city] if args.city else CITIES
    all_gaps, summary, blocks = [], [], []
    for cname in cities:
        city = poi_db.load_city(cname)
        rows = []
        for ps in PERSONAS:
            t0 = time.time()
            row = eval_row(city, ps, use_llm, args.eval_date)
            rows.append(row)
            for key in ("m2", "m7"):
                ev = row[key][0]
                if ev:
                    summary.append({"city": cname, "key": key, "persona": ps["name"], "ev": ev})
            g = row["m7"][1].get("grounding")
            if g:
                for gap in g["gaps"]:
                    all_gaps.append(f'{cname}｜{gap["name"]}')
            print(f"✅ [{cname}] {ps['name']}（{time.time()-t0:.0f}s）", flush=True)
        # 每城对比块
        detail = ""
        for row in rows:
            detail += (f'<h3 style="margin:14px 0 6px">{html.escape(row["persona"])}：'
                       f'{html.escape(row["query"])}</h3>'
                       f'<div class="ab">{cell("M2 库内直选", row["m2"][0], row["m2"][1])}'
                       f'{cell("M7 经验提案", row["m7"][0], row["m7"][1])}</div>')
        blocks.append(f'<h2>{cname}</h2>{detail}')

    # 汇总表
    def agg(key, city=None):
        evs = [s["ev"] for s in summary if s["key"] == key and (city is None or s["city"] == city)]
        if not evs:
            return "—" * 5
        n = len(evs)
        return (f'{sum(e["hard_violations_final"] for e in evs)}',
                f'{round(sum(e["avg_adjacent_min"] for e in evs)/n)}min',
                f'{round(sum(e["n_pois"] for e in evs)/n, 1)}',
                f'{round(sum(e["latency_s"] for e in evs)/n, 1)}s')

    srows = ""
    for cname in cities:
        for key, label in [("m2", "M2 库内直选"), ("m7", "M7 经验提案")]:
            v, m, p, l = agg(key, cname)
            srows += (f"<tr><td>{cname}</td><td>{label}</td><td>{v}</td><td>{m}</td>"
                      f"<td>{p}</td><td>{l}</td></tr>")

    gaps_html = ("".join(f"<li>📚 {html.escape(g)}</li>" for g in sorted(set(all_gaps)))
                 or "<li>无缺口——提案全部落地</li>")
    llm_note = "LLM 已接入" if use_llm else "离线模式（仅管线冒烟）"
    doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>TripAgent M7 A/B 评测</title><style>{CSS}</style></head><body><div class="wrap">
<h1>{html.escape(args.title)}</h1>
<p class="note">{llm_note}｜5 城 × {len(PERSONAS)} persona｜M7 = LLM 世界知识自由提案 → 四级落地匹配 → TOPTW 求解</p>
<h2>📊 汇总</h2>
<table><tr><th>城市</th><th>方案</th><th>硬违规(总)</th><th>路网均程</th><th>日均 POI</th><th>耗时</th></tr>
{srows}</table>
<h2>📚 POI 库缺口（M7 未落地的提案 = 扩容采购清单）</h2>
<ul class="gaps">{gaps_html}</ul>
{''.join(blocks)}
</div></body></html>"""
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, args.out)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"\n📄 报告已生成: {path}")


if __name__ == "__main__":
    main()
