# -*- coding: utf-8 -*-
"""M8：为新增 POI 扩充交通缓存（L1 OSRM + L2 高德，只填涉及新点的对）。

⚠️ 严禁 --force 全量重建（会覆盖已刷的 4626 对高德 L2）；本脚本只新增涉及
新点（NEW_PREFIXES 精确 ID）的对，已有缓存值保留（L2 结果优先覆盖）。

用法：AMAP_KEY 可选；python -X utf8 scripts/expand_travel_cache.py [--no-l2]
"""
import io
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from build_travel_cache import (  # noqa: E402
    AMAP_DIRECTION, CITY_FACTOR, MIN_MIN, _cache_path, load_cache, save_cache)

DATA = os.path.join(ROOT, "data")
CITIES = ["上海", "南京", "杭州", "武汉", "苏州"]
NEW_IDS = {
    "上海": ["SH029", "SH030", "SH031", "SH032", "SH033", "SH034", "SH035", "SH036", "SH037", "SH038", "SH039", "SH040", "SH041", "SH042", "SH043", "SH044"],
    "南京": ["NJ029", "NJ030", "NJ031", "NJ032", "NJ033"],
    "杭州": ["HZ051", "HZ052", "HZ053", "HZ054", "HZ055"],
    "武汉": ["WH029", "WH030", "WH031", "WH032", "WH033", "WH034"],
    "苏州": ["SZ029", "SZ030"],
}
OSRM_TABLE = "https://router.project-osrm.org/table/v1/driving/{coords}?annotations=duration"
QPS = 0.15


def amap_pair(key, o, d):
    url = AMAP_DIRECTION.format(o=f"{o[1]:.6f},{o[0]:.6f}", d=f"{d[1]:.6f},{d[0]:.6f}", key=key)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa
        return None
    if data.get("status") != "1" or not data.get("route", {}).get("paths"):
        return None
    sec = int(data["route"]["paths"][0]["duration"])
    return max(MIN_MIN, round(sec / 60.0 * 1.1))


def main():
    use_l2 = "--no-l2" not in sys.argv
    key = os.environ.get("AMAP_KEY", "")
    if use_l2 and not key:
        key = json.load(io.open(os.path.join(ROOT, "config.json"), encoding="utf-8")).get("amap_key", "")
    cache = load_cache()
    minutes = cache["minutes"]
    n_l1 = n_l2 = n_new_pairs = 0
    for city in CITIES:
        pois = json.load(io.open(os.path.join(DATA, f"{city}_pois.json"), encoding="utf-8"))["pois"]
        pos = {p["id"]: (p["lat"], p["lng"]) for p in pois}
        new_ids = [i for i in NEW_IDS[city] if i in pos]
        if not new_ids:
            print(f"{city}: 无新增点，跳过")
            continue
        # ---- L1: OSRM 全城矩阵（N<100），只落涉及新点的对，不覆盖已有值 ----
        ids = list(pos)
        coords = ";".join(f"{pos[i][1]:.6f},{pos[i][0]:.6f}" for i in ids)
        req = urllib.request.Request(OSRM_TABLE.format(coords=coords),
                                     headers={"User-Agent": "tripagent-m8/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            t = json.loads(resp.read().decode("utf-8"))
        if t.get("code") != "Ok":
            raise RuntimeError(f"OSRM error: {t}")
        idx = {pid: k for k, pid in enumerate(ids)}
        for a in new_ids:
            row = minutes.setdefault(a, {})
            for b in ids:
                if b == a:
                    continue
                if b in row and row[b] is not None:
                    continue
                sec = t["durations"][idx[a]][idx[b]]
                if sec is not None:
                    row[b] = max(MIN_MIN, round(sec / 60.0 * CITY_FACTOR, 1))
                    n_l1 += 1
            # 反向：a 作为目的
            for b in ids:
                if b == a:
                    continue
                rb = minutes.setdefault(b, {})
                if a in rb and rb[a] is not None:
                    continue
                sec = t["durations"][idx[b]][idx[a]]
                if sec is not None:
                    rb[a] = max(MIN_MIN, round(sec / 60.0 * CITY_FACTOR, 1))
                    n_l1 += 1
        save_cache(cache)
        print(f"{city}: L1 补 {n_l1} 对（累计），新点 {len(new_ids)}")
        # ---- L2: 高德刷新涉及新点的对 ----
        if use_l2 and key:
            city_l2 = 0
            for a in new_ids:
                for b in ids:
                    if b == a:
                        continue
                    m = amap_pair(key, pos[a], pos[b])
                    if m is not None:
                        minutes[a][b] = m
                        city_l2 += 1
                    time.sleep(QPS)
                for b in ids:
                    if b == a:
                        continue
                    m = amap_pair(key, pos[b], pos[a])
                    if m is not None:
                        minutes[b][a] = m
                        city_l2 += 1
                    time.sleep(QPS)
                save_cache(cache)
            n_l2 += city_l2
            print(f"{city}: L2 高德更新 {city_l2} 对")
    # 统计新点覆盖率
    miss = 0
    for city, ids in NEW_IDS.items():
        for a in ids:
            row = minutes.get(a, {})
            for b in ids:
                if a != b and row.get(b) is None:
                    miss += 1
    total = sum(len(v) for v in minutes.values())
    print(f"完成：L1 补 {n_l1} 对 | L2 更新 {n_l2} 对 | 总缓存 {total} 对 | 新点互达缺失 {miss}")


if __name__ == "__main__":
    main()
