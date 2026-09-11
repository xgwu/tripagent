# -*- coding: utf-8 -*-
"""M3/M4 交通矩阵构建：OSRM(L1) / 高德(L2) → data/travel_cache.json（多城合并）。

分层设计（对应可行性报告 §交通矩阵）：
- L1：同城预计算缓存（OSRM /table 真实路网驾车时长，本脚本一次性构建）
- L2：高德驾车路径规划 API（实时路况，需 AMAP_KEY；--l2 启用，逐对写入并持久化）
- L3：直线 × 绕路系数 / 速度（poi_db.travel_hours 的兜底，缓存 miss 时启用）

缓存结构：{"cities": {...}, "minutes": {poi_id: {poi_id: 分钟|None}}}
POI ID 按城市前缀唯一（HZ/NJ/SH/SZ/WH），多城矩阵可安全合并；跨城对不建（行程按城规划）。

用法：
  python scripts/build_travel_cache.py                 # 构建/补齐所有城市 L1
  python scripts/build_travel_cache.py --force         # 强制重建全部
  AMAP_KEY=xxx python scripts/build_travel_cache.py --l2 --l2-sample   # L2 限量试跑
"""
import glob
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
OSRM_TABLE = "https://router.project-osrm.org/table/v1/driving/{coords}?annotations=duration"
AMAP_DIRECTION = ("https://restapi.amap.com/v3/direction/driving?origin={o}&destination={d}"
                  "&strategy=0&extensions=base&key={key}")
CITY_FACTOR = 1.4   # OSRM car 档偏乐观（走高架/高速档），×1.4 逼近真实市内混行
AMAP_FACTOR = 1.1   # 高德已含实时路况，仅需轻度找车/停车修正
MIN_MIN = 15        # 单程下限（分钟）
QPS_GUST = 0.15     # L2 逐对请求间隔（秒），~6 QPS 上限防高德频控


def _cache_path():
    return os.path.join(DATA_DIR, "travel_cache.json")


def load_cache() -> dict:
    p = _cache_path()
    if os.path.exists(p):
        cache = json.load(open(p, encoding="utf-8"))
    else:
        cache = {"cities": {}, "minutes": {}}
    if "cities" not in cache:  # M3 单城旧格式迁移
        cache = {"cities": {cache.get("city", "杭州"): {"source": "osrm-table-driving",
                                                        "city_factor": cache.get("city_factor", CITY_FACTOR)}},
                 "minutes": cache.get("minutes", {})}
    return cache


def save_cache(cache: dict):
    json.dump(cache, open(_cache_path(), "w", encoding="utf-8"), ensure_ascii=False)


def build_osrm(city: str) -> tuple[dict, str]:
    """单城 OSRM N×N 矩阵 → (minutes, id_prefix)。"""
    with open(os.path.join(DATA_DIR, f"{city}_pois.json"), encoding="utf-8") as f:
        city_data = json.load(f)
    pois = city_data["pois"]
    prefix = pois[0]["id"][:2]
    coords = ";".join(f"{p['lng']:.6f},{p['lat']:.6f}" for p in pois)
    url = OSRM_TABLE.format(coords=coords)
    req = urllib.request.Request(url, headers={"User-Agent": "tripagent-m4/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM error: {data}")
    dur = data["durations"]
    minutes = {}
    for i, a in enumerate(pois):
        minutes[a["id"]] = {}
        for j, b in enumerate(pois):
            if i == j:
                continue
            sec = dur[i][j]
            minutes[a["id"]][b["id"]] = (
                max(MIN_MIN, round(sec / 60.0 * CITY_FACTOR, 1)) if sec is not None else None)
    return minutes, prefix


def amap_pair_minutes(key: str, o: tuple, d: tuple) -> int | None:
    """L2：高德驾车路径规划，返回含路况的通行分钟（strategy=0 速度优先）。"""
    url = AMAP_DIRECTION.format(
        o=f"{o[1]:.6f},{o[0]:.6f}", d=f"{d[1]:.6f},{d[0]:.6f}", key=key)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa
        return None
    if data.get("status") != "1" or not data.get("route", {}).get("paths"):
        return None
    sec = int(data["route"]["paths"][0]["duration"])
    return max(MIN_MIN, round(sec / 60.0 * AMAP_FACTOR))


def build_l2(city: str, key: str, cache: dict, max_pairs: int = 0,
             save=None) -> int:
    """L2 增量：对指定城市缓存对重查高德并写回（max_pairs>0 限量试跑）。

    save：增量落盘回调（每 200 对调用一次，防长跑中断丢进度）。
    QPS_GUST 轻微节流，避免触发高德频控（个人 Key ~3-5 QPS）。
    """
    import time
    with open(os.path.join(DATA_DIR, f"{city}_pois.json"), encoding="utf-8") as f:
        pois = {p["id"]: (p["lat"], p["lng"]) for p in json.load(f)["pois"]}
    minutes = cache["minutes"]
    n = 0
    done = 0
    for a_id, row in minutes.items():
        if a_id not in pois:
            continue
        for b_id in row:
            if b_id not in pois:
                continue
            m = amap_pair_minutes(key, pois[a_id], pois[b_id])
            done += 1
            if m is not None:
                row[b_id] = m
                n += 1
            if max_pairs and n >= max_pairs:
                print(f"L2 达到限量 {max_pairs} 对，暂停")
                return n
            if save is not None and done % 200 == 0:
                save(cache)
                print(f"  ...已处理 {done} 对（成功 {n}），进度已保存", flush=True)
            time.sleep(QPS_GUST)  # 节流防频控
    return n


def main():
    key = os.environ.get("AMAP_KEY", "")
    use_l2 = "--l2" in sys.argv and bool(key)
    if "--l2" in sys.argv and not key:
        print("⚠️ --l2 需要环境变量 AMAP_KEY，本轮仅构建 L1")
    cache = load_cache()
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*_pois.json"))):
        city = os.path.basename(path).replace("_pois.json", "")
        if city.startswith("_"):
            continue
        if cache["cities"].get(city, {}).get("source") == "osrm-table-driving" and "--force" not in sys.argv:
            print(f"{city}: 缓存已存在，跳过")
        else:
            print(f"{city}: OSRM 构建 ...", end=" ", flush=True)
            city_minutes, prefix = build_osrm(city)
            cache["minutes"].update(city_minutes)
            cache["cities"][city] = {"source": "osrm-table-driving", "city_factor": CITY_FACTOR,
                                     "id_prefix": prefix}
            print(f"{sum(len(v) for k, v in city_minutes.items() if k.startswith(prefix))} 对合入")
        if use_l2:
            print(f"{city}: L2 高德增量刷新（key 末4位 {key[-4:]}）...")
            n = build_l2(city, key, cache,
                         max_pairs=50 if "--l2-sample" in sys.argv else 0,
                         save=save_cache)
            print(f"  L2 更新 {n} 对")
        cache["cities"][city]["l2_amap"] = use_l2
    cache["cities"]["_meta"] = {"city_factor": CITY_FACTOR, "amap_factor": AMAP_FACTOR}
    save_cache(cache)
    total = sum(len(v) for v in cache["minutes"].values())
    print(f"完成：travel_cache.json | {len(cache['cities']) - 1} 城（含 _meta）| {total} 对缓存")


if __name__ == "__main__":
    main()
