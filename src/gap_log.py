# -*- coding: utf-8 -*-
"""POI 库缺口台账：线上真实使用产生的落地缺口持久化（JSONL 追加写）。

背景：/api/plan 每次规划返回 grounding.gaps（LLM 提案未能在库内落地的点）
后即丢弃——缺口无台账，无法支撑「按缺口决定扩哪个城的库」。本模块把每次
面向用户的规划响应中的缺口摘要追加写入 data/poi_gap_log.jsonl，一行一次
规划（含缓存命中——缓存回放同样代表真实用户需求）：

  {"ts": "...", "city": "苏州", "cities": ["苏州"], "query": "...",
   "days": 3, "mode": "m7", "grounding_rate": 0.92, "n_proposed": 12,
   "gaps": [{"name": "...", "note": "...", "day": 2}]}

写入失败静默（OSError 不拖垮规划主链路）；汇总接口按 城市×缺口点名
统计频次，直接对接 docs/city-pipeline.md 扩城 SOP。
"""
import json
import os
import threading
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
LOG_PATH = os.path.join(DATA_DIR, "poi_gap_log.jsonl")

_LOCK = threading.Lock()  # 多线程 HTTP 服务下的追加写互斥
_MAX_QUERY_LEN = 120
_MAX_NOTE_LEN = 60


def append_gap_record(city: str | None, cities: list | None, query: str,
                      days: int | None, mode: str | None,
                      grounding: dict | None, path: str = LOG_PATH) -> bool:
    """规划响应组装完成后记录缺口摘要。无 gaps 时跳过（零噪音）。

    city/cities：单城传 city，跨城传 cities（city 传 None）。
    grounding：plan 结果里的 grounding 字段（可为 None）。
    返回是否实际写入一行。
    """
    g = grounding or {}
    gaps = g.get("gaps") or []
    if not gaps:
        return False
    rec = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "city": city,
        "cities": list(cities) if cities else ([city] if city else []),
        "query": (query or "")[:_MAX_QUERY_LEN],
        "days": days,
        "mode": mode,
        "grounding_rate": g.get("grounding_rate"),
        "n_proposed": g.get("n_proposed"),
        "gaps": [{"name": x.get("name"), "note": (x.get("note") or "")[:_MAX_NOTE_LEN],
                  "day": x.get("day")} for x in gaps],
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False  # 台账是旁路：任何写失败不影响规划返回


def summarize(path: str = LOG_PATH) -> dict:
    """按 城市×缺口点名 汇总频次。容忍损坏行（跳过），供扩城 SOP 决策。

    返回 {"records": 记录数, "lines": 总行数, "by_city": {城: 次数},
          "top_gaps": [{"city","name","count","queries": [代表查询]}]}（按频次降序）。
    """
    by_key: dict = {}
    by_city: dict = {}
    records = lines = 0
    if not os.path.exists(path):
        return {"records": 0, "lines": 0, "by_city": {}, "top_gaps": []}
    with open(path, encoding="utf-8") as f:
        for line in f:
            lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # 半截写/损坏行跳过
            records += 1
            city = rec.get("city") or "+".join(rec.get("cities") or []) or "未知"
            by_city[city] = by_city.get(city, 0) + 1
            for g in rec.get("gaps") or []:
                name = (g.get("name") or "").strip()
                if not name:
                    continue
                key = (city, name)
                ent = by_key.setdefault(key, {"city": city, "name": name,
                                              "count": 0, "queries": []})
                ent["count"] += 1
                q = (rec.get("query") or "").strip()
                if q and q not in ent["queries"] and len(ent["queries"]) < 3:
                    ent["queries"].append(q)
    top = sorted(by_key.values(), key=lambda x: -x["count"])
    return {"records": records, "lines": lines, "by_city": by_city, "top_gaps": top}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="POI 库缺口台账汇总")
    ap.add_argument("--path", default=LOG_PATH, help="JSONL 台账路径")
    ap.add_argument("--top", type=int, default=20, help="展示前 N 个缺口")
    a = ap.parse_args()
    s = summarize(a.path)
    print(f"记录 {s['records']} 条 / {s['lines']} 行")
    for c, n in sorted(s["by_city"].items(), key=lambda kv: -kv[1]):
        print(f"  {c}: {n} 次规划出现缺口")
    for ent in s["top_gaps"][:a.top]:
        print(f"  [{ent['city']}] {ent['name']} ×{ent['count']}"
              + (f"  如: {ent['queries'][0]}" if ent["queries"] else ""))
    if not s["top_gaps"]:
        print("（暂无缺口记录）")
