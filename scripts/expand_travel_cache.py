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
sys.path.insert(0, ROOT)  # src.config 需要（load_config 在 main 内延迟导入）
from build_travel_cache import (  # noqa: E402
    AMAP_DIRECTION, CITY_FACTOR, MIN_MIN, _cache_path, load_cache, save_cache)

DATA = os.path.join(ROOT, "data")
CITIES = ["上海", "南京", "杭州", "武汉", "苏州", "成都", "北京", "广州", "盐城"]
NEW_IDS = {
    "上海": ["SH045", "SH046", "SH047", "SH048", "SH049", "SH050", "SH051", "SH052", "SH053", "SH054", "SH055", "SH056", "SH057", "SH058", "SH059", "SH060", "SH061", "SH062", "SH063", "SH064", "SH065", "SH066", "SH067", "SH068"],
    "南京": ["NJ029", "NJ030", "NJ031", "NJ032", "NJ033"],
    "杭州": ["HZ051", "HZ052", "HZ053", "HZ054", "HZ055"],
    "武汉": ["WH029", "WH030", "WH031", "WH032", "WH033", "WH034",
             "WH035", "WH036", "WH037", "WH038", "WH039", "WH040", "WH041",
             "WH042", "WH043", "WH045", "WH046", "WH047", "WH048", "WH049", "WH050"],
    "苏州": ["SZ029", "SZ030"],
    "成都": ["CD081", "CD082", "CD083", "CD085", "CD086", "CD087", "CD088", "CD089"],
    "北京": [],   # 北京全库由 add_city.py 建缓存；--auto 模式自动检测缺失对
    "广州": [],   # 广州全库由 build_travel_cache.py 建缓存；--auto 模式自动检测缺失对
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
    auto = "--auto" in sys.argv  # 自动检测：缓存中无记录的 POI 视为新点（与 NEW_IDS 并集）
    key = os.environ.get("AMAP_KEY", "")
    if use_l2 and not key:
        from src.config import load_config
        key = load_config().get("amap_key", "")
    cache = load_cache()
    minutes = cache["minutes"]
    n_l1 = n_l2 = n_new_pairs = 0
    for city in CITIES:
        pois = json.load(io.open(os.path.join(DATA, f"{city}_pois.json"), encoding="utf-8"))["pois"]
        pos = {p["id"]: (p["lat"], p["lng"]) for p in pois}
        new_ids = [i for i in NEW_IDS[city] if i in pos]
        if auto:
            auto_ids = [i for i in pos if i not in minutes]
            for i in auto_ids:
                if i not in new_ids:
                    new_ids.append(i)
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
    # 统计新点覆盖率（--auto 时统计全部无记录点）
    miss = 0
    for city in CITIES:
        pois = json.load(io.open(os.path.join(DATA, f"{city}_pois.json"), encoding="utf-8"))["pois"]
        ids = ([p["id"] for p in pois if p["id"] not in minutes] if auto
               else [i for i in NEW_IDS[city]])
        for a in ids:
            row = minutes.get(a, {})
            for b in ids:
                if a != b and row.get(b) is None:
                    miss += 1
    total = sum(len(v) for v in minutes.values())
    print(f"完成：L1 补 {n_l1} 对 | L2 更新 {n_l2} 对 | 总缓存 {total} 对 | 新点互达缺失 {miss}")


if __name__ == "__main__":
    main()
