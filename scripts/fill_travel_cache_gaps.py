# -*- coding: utf-8 -*-
"""为增量新增的 POI 补交通对（只填缺失键，绝不覆盖既有值）。

为什么不用 build_travel_cache.py：它的 main() 对 source=="osrm-table-driving" 的城市
直接跳过（新增点补不进去），而 --force 是全量重建（会连其它 7 城一起重查，且 OSRM
公共服务器在全量重建时有限流风险）。本脚本只对目标城发一次 /table 整表请求，然后
**仅把缓存里缺失的键写回**，口径与既有缓存完全一致（osrm-table-driving + CITY_FACTOR）。

用法：python scripts/fill_travel_cache_gaps.py 广州
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
OSRM_TABLE = "https://router.project-osrm.org/table/v1/driving/{coords}?annotations=duration"
CITY_FACTOR = 1.4
MIN_MIN = 15


def detect_indent(path: str):
    raw = open(path, encoding="utf-8").read()
    for cand in (None, 0, 1, 2, 4):
        if json.dumps(json.loads(raw), ensure_ascii=False, indent=cand).strip() == raw.strip():
            return cand, raw
    return None, raw


def main(city: str):
    path = os.path.join(DATA_DIR, "travel_cache.json")
    indent, _ = detect_indent(path)
    assert indent is not None, "travel_cache.json 格式无法探测，拒绝写回"
    cache = json.load(open(path, encoding="utf-8"))
    minutes = cache["minutes"]
    pois = json.load(open(os.path.join(DATA_DIR, f"{city}_pois.json"), encoding="utf-8"))["pois"]
    ids = [p["id"] for p in pois]

    missing = []
    for a in ids:
        row = minutes.get(a)
        for b in ids:
            if a == b:
                continue
            if row is None or b not in row:
                missing.append((a, b))
    print(f"{city}: {len(ids)} 点，缺失对 {len(missing)}")
    if not missing:
        print("无缺口，无需请求 OSRM")
        return

    coords = ";".join(f"{p['lng']:.6f},{p['lat']:.6f}" for p in pois)
    url = OSRM_TABLE.format(coords=coords)
    print(f"OSRM /table 请求（{len(coords)} 字符）...", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "tripagent-m4/1.0"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM error: {data}")
    dur = data["durations"]
    pos = {pid: i for i, pid in enumerate(ids)}

    filled = 0
    for a, b in missing:
        sec = dur[pos[a]][pos[b]]
        if sec is None:
            continue
        minutes.setdefault(a, {})[b] = max(MIN_MIN, round(sec / 60.0 * CITY_FACTOR, 1))
        filled += 1
    print(f"填入 {filled} / {len(missing)} 对（OSRM 返回 None 的留空，由 L3 直线兜底）")

    # 兜底：仍未填上的新点对，用同城既有对的直线-分钟比中位数自校准补上（对称）
    import math

    def hav(a_, b_):
        R = 6371.0
        la1, lo1, la2, lo2 = map(math.radians, (a_[0], a_[1], b_[0], b_[1]))
        x = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
        return 2 * R * math.asin(math.sqrt(x))

    coord = {p["id"]: (p["lat"], p["lng"]) for p in pois}
    ratios = []
    for a in ids[:20]:
        for b in ids[:20]:
            if a == b:
                continue
            m = minutes.get(a, {}).get(b)
            if m:
                km = hav(coord[a], coord[b])
                if km > 0.5:
                    ratios.append(m / km)
    ratios.sort()
    med = ratios[len(ratios) // 2] if ratios else 3.7
    print(f"直线自校准中位数：{med:.2f} min/km（n={len(ratios)}）")

    none_count = 0
    for a in ids:
        for b in ids:
            if a == b:
                continue
            row = minutes.setdefault(a, {})
            if row.get(b) is None:
                km = hav(coord[a], coord[b])
                row[b] = max(MIN_MIN, round(km * med, 1))
                none_count += 1
    print(f"兜底补齐 None 对：{none_count}")

    open(path, "w", encoding="utf-8").write(
        json.dumps(cache, ensure_ascii=False, indent=indent))
    print(f"✅ 已写回（indent={indent}） 总对数={sum(len(v) for v in minutes.values())}")

    # 回读校验 + None 全扫
    chk = json.load(open(path, encoding="utf-8"))["minutes"]
    miss2, nones = 0, []
    for a in ids:
        for b in ids:
            if a == b:
                continue
            v = chk.get(a, {}).get(b)
            if v is None:
                miss2 += 1
                nones.append((a, b))
    print(f"回读：仍未覆盖 {miss2} 对，None {len(nones)}")
    assert miss2 == 0, f"仍有缺口：{nones[:10]}"


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "广州")
