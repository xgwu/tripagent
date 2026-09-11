# -*- coding: utf-8 -*-
"""M3-1 求解器参数扫描：MAIN_BONUS × SOFT_W。

思路：LLM 库内选择不依赖求解器参数 → LLM 阶段每个 persona 只调用一次并缓存
（output/tune_cache.json，--fresh 强制重调），随后离线扫参数组合，
用「主选保留率 / 时段合规率 / 硬约束违规 / 相邻均距 / 标签覆盖」选平衡点。

用法（需 OR-Tools venv）：
  python scripts/tune_params.py            # 有缓存用缓存，无缓存先调 LLM
  python scripts/tune_params.py --fresh    # 重新调 LLM
"""
import io
import itertools
import json
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src import poi_db, retrieval, m1_planner, sequencer, toptw, metrics  # noqa: E402

PERSONAS = [
    {"name": "亲子休闲", "query": "带5岁孩子去杭州玩2天，不要太累，最好有动物或者博物馆", "days": 2},
    {"name": "经典文化", "query": "杭州2天经典深度游，喜欢历史文化、寺庙和茶文化", "days": 2},
    {"name": "拍照轻旅", "query": "杭州2天，喜欢拍照打卡和小众地方，晚上想逛夜市", "days": 2},
]
GRID_MAIN_BONUS = [300, 600, 1000]
GRID_SOFT_W = [0, 2, 5]


def load_llm_stage(fresh: bool):
    cache_path = os.path.join(ROOT, "output", "tune_cache.json")
    if os.path.exists(cache_path) and not fresh:
        with open(cache_path, encoding="utf-8") as f:
            print("LLM 阶段：使用缓存", cache_path)
        return json.load(open(cache_path, encoding="utf-8"))
    city = poi_db.load_city("杭州")
    out = {}
    for ps in PERSONAS:
        t0 = time.time()
        r = m1_planner.plan(city, ps["query"], ps["days"], use_llm=True)
        if not r["mode"].startswith("llm"):
            raise RuntimeError(f"LLM 不可用: {r['mode']}")
        out[ps["name"]] = {
            "query": ps["query"], "days": ps["days"],
            "day_map": {str(d["day"]): [s["id"] for s in d["timeline"] if s["type"] == "poi"]
                        for d in r["itinerary"]["days"]},
        }
        print(f"  LLM {ps['name']} 完成 {time.time()-t0:.0f}s day_map={out[ps['name']]['day_map']}")
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    json.dump(out, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return out


def run_combo(city, cands, all_pois, llm_stage, main_bonus, soft_w):
    agg = {"viol": 0, "mains": [0, 0], "bt": [0, 0], "km": [], "pois": 0, "tags": []}
    per_persona = {}
    for name, st in llm_stage.items():
        used_all = {i for ids in st["day_map"].values() for i in ids}
        day_map, used_backup = {}, set()
        n_mains = n_kept = 0
        for d in sorted(st["day_map"], key=int):
            mains = [all_pois[i] for i in st["day_map"][d] if i in all_pois]
            pool = m2_planner_pool(mains, cands, used_all, used_backup)
            used_backup.update(p["id"] for p in pool if p["id"] not in {m["id"] for m in mains})
            ordered, dropped, ok = toptw.solve_day(pool, st["day_map"][d], city, all_pois,
                                                   main_bonus=main_bonus, soft_w=soft_w)
            day_map[int(d)] = ordered if (ok and ordered) else st["day_map"][d]
            if ok and ordered:
                n_mains += len(st["day_map"][d])
                n_kept += sum(1 for i in st["day_map"][d] if i in set(ordered))
        itin = sequencer.build_itinerary(day_map, city, all_pois)
        fake = {"mode": f"tune(mb={main_bonus},sw={soft_w})", "days": st["days"],
                "latency_s": 0, "candidates": len(cands),
                "invalid_poi_ids": [], "llm_raw_violations": 0,
                "mains_kept": f"{n_kept}/{n_mains}", "itinerary": itin}
        ev = metrics.evaluate(fake, city, st["query"])
        agg["viol"] += ev["hard_violations_final"]
        mk = ev["mains_kept"].split("/")
        agg["mains"][0] += int(mk[0]); agg["mains"][1] += int(mk[1])
        bt = ev["best_time_rate"].split("/")
        agg["bt"][0] += int(bt[0]); agg["bt"][1] += int(bt[1])
        agg["km"].append(ev["avg_adjacent_km"])
        agg["pois"] += ev["n_pois"]
        tc = ev["tag_coverage"].split("/")
        agg["tags"].append(int(tc[0]) / int(tc[1]))
        per_persona[name] = ev
    return {
        "main_bonus": main_bonus, "soft_w": soft_w,
        "violations": agg["viol"],
        "mains_kept": f'{agg["mains"][0]}/{agg["mains"][1]}',
        "mains_rate": agg["mains"][0] / max(1, agg["mains"][1]),
        "best_time": f'{agg["bt"][0]}/{agg["bt"][1]}',
        "best_time_rate": agg["bt"][0] / max(1, agg["bt"][1]),
        "avg_km": round(sum(agg["km"]) / len(agg["km"]), 2),
        "pois": agg["pois"],
        "tag_cov": round(sum(agg["tags"]) / len(agg["tags"]), 2),
    }, per_persona


def m2_planner_pool(mains, cands, used_all, used_backup):
    from src.m2_planner import _build_day_pool
    return _build_day_pool(mains, cands, used_all, used_backup)


def main():
    fresh = "--fresh" in sys.argv
    llm_stage = load_llm_stage(fresh)
    city = poi_db.load_city("杭州")
    all_pois = {p["id"]: poi_db.parse_poi(p, city) for p in city["pois"]}
    cands = retrieval.recall(city, "杭州")  # 召回是确定性的；池半径过滤在逐日做

    print(f"\n{'MB':>5} {'SW':>3} | {'违规':>4} {'主选保留':>8} {'时段合规':>8} {'均距km':>6} {'POI':>4} 标签覆盖")
    print("-" * 66)
    results = []
    for mb, sw in itertools.product(GRID_MAIN_BONUS, GRID_SOFT_W):
        row, _ = run_combo(city, cands, all_pois, llm_stage, mb, sw)
        results.append(row)
        print(f"{mb:>5} {sw:>3} | {row['violations']:>4} "
              f"{row['mains_kept']:>8} {row['best_time']:>8} "
              f"{row['avg_km']:>6} {row['pois']:>4} {row['tag_cov']}")
    best = max(results, key=lambda r: (r["violations"] == 0, r["mains_rate"],
                                       r["best_time_rate"], r["tag_cov"], -r["avg_km"]))
    print(f"\n推荐: MAIN_BONUS={best['main_bonus']}, SOFT_W={best['soft_w']}  -> {best}")
    out = os.path.join(ROOT, "output", "tune_results.json")
    json.dump(results, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("已写入", out)


if __name__ == "__main__":
    main()
