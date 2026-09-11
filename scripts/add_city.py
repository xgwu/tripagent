# -*- coding: utf-8 -*-
"""一键扩城流水线：python scripts/add_city.py <城市> [--prefix CD] [--center lat,lng]

流水线六步（全部幂等，可中断重跑）：
  1. 采集     高德 place/text 按类目关键词搜索（museum/park/动物园/美食…），
              每类目限量，name/uid 去重 → data/{city}_pois.json
  2. 校坐标   距质心 >60km 视为脏数据丢弃；同名 <200m 合并；center 取质心（或 --center）
  3. 建缓存   OSRM /table L1 真实路网 N×N 矩阵合入 data/travel_cache.json
  4. 照片     高德 photos 字段预取缩略图 → data/photo_cache.json
  5. 注册     server.py CITIES 列表 + eval_regression.py CASES 各追加一行（自动去重）
  6. 冒烟     离线 M1 规划「城市2天经典深度游」，断言 2 天 0 违规

说明：
- 采集来的 POI 营业信息是高德快照（closed_days 置空、note 标注「待核实」），
  正式使用前建议人工核实闭馆日（scripts/backfill_closed_days.py 可辅助）。
- 城市名拼音首字母前缀自动推断（内置常见城市表），推断不出需显式 --prefix。
- 需要 config.json 的 amap_key。

用法：
  python scripts/add_city.py 成都                       # 自动推断前缀 CD
  python scripts/add_city.py 西安 --prefix XA --dry     # 只看采集结果不落盘
"""
import argparse
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SERVER_PY = os.path.join(ROOT, "webui", "server.py")
EVAL_PY = os.path.join(ROOT, "scripts", "eval_regression.py")

# 类目 → 搜索关键词（每关键词限量采集）
CATEGORY_KEYWORDS = [
    ("culture",  ["博物馆", "美术馆"]),
    ("history",  ["历史古迹", "名人故居"]),
    ("family",   ["动物园", "科技馆", "游乐园"]),
    ("food",     ["老字号", "美食街"]),
    ("nature",   ["公园", "湖泊", "山"]),
    ("photo",    ["地标", "观景台"]),
    ("nightlife",["夜市", "夜游"]),
    ("shopping", ["商业街", "步行街"]),
]
# 类目默认属性（采集字段缺失时的兜底）
CATEGORY_DEFAULTS = {
    "culture":  dict(duration_h=1.5, open="09:00", close="17:00", family_ok=True),
    "history":  dict(duration_h=1.5, open="09:00", close="17:30", family_ok=True),
    "family":   dict(duration_h=2.5, open="09:00", close="17:00", family_ok=True),
    "food":     dict(duration_h=1.5, open="10:30", close="21:00", family_ok=True),
    "nature":   dict(duration_h=2.0, open="07:00", close="19:00", family_ok=True),
    "photo":    dict(duration_h=1.0, open="00:00", close="23:59", family_ok=True),
    "nightlife":dict(duration_h=1.5, open="18:00", close="22:00", family_ok=True),
    "shopping": dict(duration_h=1.5, open="10:00", close="22:00", family_ok=True),
}

# 常见旅游城市 → 拼音首字母前缀（不在表内需 --prefix）
PREFIX_MAP = {
    "成都": "CD", "西安": "XA", "重庆": "CQ", "广州": "GZ", "深圳": "SZ2",  # 深圳与苏州 SZ 冲突 → SZ2
    "北京": "BJ", "天津": "TJ", "青岛": "QD", "厦门": "XM", "长沙": "CS",
    "昆明": "KM", "贵阳": "GY", "桂林": "GL", "洛阳": "LY", "郑州": "ZZ",
    "合肥": "HF", "福州": "FZ", "济南": "JN", "大连": "DL", "哈尔滨": "HRB",
    "宁波": "NB", "无锡": "WX", "佛山": "FS", "东莞": "DG", "珠海": "ZH",
}


def amap_get(path: str, params: dict, key: str, opener) -> dict:
    qs = urllib.parse.urlencode({**params, "key": key})
    url = f"https://restapi.amap.com/v3/{path}?{qs}"
    for attempt in (1, 2, 3):
        try:
            with opener.open(url, timeout=12) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            if d.get("status") == "1":
                return d
            time.sleep(0.4 * attempt)
        except Exception:
            time.sleep(0.6 * attempt)
    return {}


def geocode_center(city: str, key: str, opener) -> dict:
    d = amap_get("geocode/geo", {"address": city}, key, opener)
    gl = (d.get("geocodes") or [{}])[0].get("location")
    if not gl:
        raise SystemExit(f"❌ 高德地理编码失败：拿不到 {city} 的市中心坐标，请用 --center lat,lng 指定")
    lng, lat = gl.split(",")
    return {"lat": round(float(lat), 4), "lng": round(float(lng), 4)}


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def collect(city: str, key: str, opener, per_kw: int) -> list:
    """类目关键词搜索 → 原始 POI 列表（uid 去重）。"""
    seen, raw = set(), []
    for cat, keywords in CATEGORY_KEYWORDS:
        n_cat = 0
        for kw in keywords:
            d = amap_get("place/text", {"keywords": kw, "city": city,
                                        "citylimit": "true", "offset": 25, "page": 1}, key, opener)
            for p in d.get("pois") or []:
                uid = p.get("uid") or p.get("id") or p.get("name")
                name = (p.get("name") or "").strip()
                if not name or uid in seen or name in seen:
                    continue
                try:
                    lng, lat = p["location"].split(",")
                    lat, lng = float(lat), float(lng)
                except Exception:
                    continue
                seen.add(uid)
                seen.add(name)
                raw.append(dict(name=name, category=cat, lat=lat, lng=lng,
                                rating=_rating(p), addr=p.get("address", "") or "",
                                typestr=p.get("type", "")))
                n_cat += 1
                if n_cat >= per_kw * len(keywords):
                    break
            time.sleep(0.15)
        print(f"  {cat:<9} 采集 {n_cat} 个")
    return raw


def _rating(p: dict) -> float:
    c = p.get("cost") or ""
    m = re.search(r"(\d)", str(p.get("biz_ext", {}).get("rating") or ""))
    if m:
        return float(m.group(1))
    return 4.0


def sanitize(raw: list, center: dict, prefix: str, cap: int = 80) -> list:
    """校坐标：丢离群点、近邻去重、续号编 ID；超 cap 按评分保留头部
    （OSRM 公共服务器 /table 上限 100 点，80 留余量；自建 OSRM 可调大）。"""
    kept = []
    for p in raw:
        if haversine_km(p["lat"], p["lng"], center["lat"], center["lng"]) > 60:
            continue  # 离群脏数据
        if any(p["name"] == k["name"] or haversine_km(p["lat"], p["lng"], k["lat"], k["lng"]) < 0.2
               for k in kept):
            continue
        kept.append(p)
    kept.sort(key=lambda x: (-x["rating"], x["name"]))
    kept = kept[:cap]
    for i, p in enumerate(kept, 1):
        p["id"] = f"{prefix}{i:03d}"
    return kept


def build_city_file(city: str, center: dict, pois: list, dry: bool) -> str:
    doc = {
        "city": city,
        "center": center,
        "day_start": "09:00", "day_end": "21:30",
        "meal_slots": {"lunch": ["12:00", "13:00"], "dinner": ["18:00", "19:00"]},
        "meal_cost": {"lunch": 50, "dinner": 80},
        "pois": [{
            "id": p["id"], "name": p["name"], "category": p["category"],
            "tags": [t for t in p["typestr"].split(";")[:3] if t] or ["待补充"],
            "lat": round(p["lat"], 6), "lng": round(p["lng"], 6),
            "duration_h": CATEGORY_DEFAULTS[p["category"]]["duration_h"],
            "open": CATEGORY_DEFAULTS[p["category"]]["open"],
            "close": CATEGORY_DEFAULTS[p["category"]]["close"],
            "best_time": "any", "price": 0, "rating": int(p["rating"]) or 4,
            "area": "central", "family_ok": CATEGORY_DEFAULTS[p["category"]]["family_ok"],
            "note": (f"坐标/名称来自高德采集{('，@' + p['addr']) if p['addr'] else ''}；"
                     "营业时间与闭馆日待人工核实"),
            "closed_days": [],
        } for p in pois],
    }
    path = os.path.join(ROOT, "data", f"{city}_pois.json")
    if not dry:
        json.dump(doc, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return path


def build_travel_cache(city: str) -> int:
    from scripts import build_travel_cache as btc  # noqa: 复用 OSRM 构建逻辑
    cache = btc.load_cache()
    if cache["cities"].get(city, {}).get("source") == "osrm-table-driving":
        print("  交通缓存已存在，跳过")
        return 0
    minutes, prefix = btc.build_osrm(city)
    cache["minutes"].update(minutes)
    cache["cities"][city] = {"source": "osrm-table-driving", "city_factor": btc.CITY_FACTOR,
                             "id_prefix": prefix}
    cache["cities"]["_meta"] = {"city_factor": btc.CITY_FACTOR, "amap_factor": btc.AMAP_FACTOR}
    btc.save_cache(cache)
    return sum(len(v) for k, v in minutes.items() if k.startswith(prefix))


def fetch_photos(city: str, pois: list, key: str) -> tuple:
    cache_path = os.path.join(ROOT, "data", "photo_cache.json")
    cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    got = 0
    for p in pois:
        pid = p["id"]
        if cache.get(pid):
            continue
        d = amap_get("place/text", {"keywords": p["name"], "city": city,
                                    "extensions": "all", "offset": 1, "page": 1}, key, opener)
        link = ""
        for poi in d.get("pois") or []:
            for ph in poi.get("photos") or []:
                u = ph.get("url") or ""
                if u.startswith("http://"):
                    u = "https://" + u[7:]
                if u.startswith("https://"):
                    link = u
                    break
            if link:
                break
        cache[pid] = link
        got += 1
        time.sleep(0.1)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    with_img = sum(1 for p in pois if cache.get(p["id"]))
    return got, with_img


def register_city(city: str) -> bool:
    """server.py CITIES 与 eval_regression.py CASES 追加（已存在则跳过）。"""
    changed = False
    src = open(SERVER_PY, encoding="utf-8").read()
    m = re.search(r'CITIES = \[([^\]]*)\]', src)
    if m and f'"{city}"' not in m.group(1):
        src = src.replace(m.group(0), m.group(0).replace("]", f', "{city}"]'))
        open(SERVER_PY, "w", encoding="utf-8").write(src)
        changed = True
    esrc = open(EVAL_PY, encoding="utf-8").read()
    case = f'    ("{city}", "{city}2天经典深度游，喜欢历史文化、寺庙和博物馆", 2, 0.8),\n'
    if f'("{city}"' not in esrc:
        esrc = esrc.replace("]\n\n\ndef main():", case + "]\n\n\ndef main():")
        open(EVAL_PY, "w", encoding="utf-8").write(esrc)
        changed = True
    return changed


def smoke(city: str) -> bool:
    from src import poi_db, m1_planner
    city_data = poi_db.load_city(city)
    r = m1_planner.plan(city_data, f"{city}2天经典深度游", 2, use_llm=False)
    it = r["itinerary"]
    ok = len(it["days"]) == 2 and it["total_violations"] == 0
    print(f"  规划 2 天 → 实际 {len(it['days'])} 天，违规 {it['total_violations']}，"
          f"落地 {len([s for d in it['days'] for s in d['timeline'] if s['type']=='poi'])} 点 → "
          f"{'✅' if ok else '❌'}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("city")
    ap.add_argument("--prefix", help="POI ID 前缀（默认按城市名自动推断）")
    ap.add_argument("--center", help="市中心 lat,lng（默认高德地理编码）")
    ap.add_argument("--per-kw", type=int, default=8, help="每关键词采集上限（默认 8）")
    ap.add_argument("--skip-photos", action="store_true")
    ap.add_argument("--dry", action="store_true", help="只采集校验，不落盘不注册")
    args = ap.parse_args()
    city = args.city

    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    key = cfg.get("amap_key")
    if not key:
        raise SystemExit("❌ config.json 缺少 amap_key")
    prefix = args.prefix or PREFIX_MAP.get(city)
    if not prefix:
        raise SystemExit(f"❌ 无法推断 {city} 的 ID 前缀，请用 --prefix 指定（如 CD）")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    print(f"== 扩城流水线：{city}（前缀 {prefix}）{' [dry-run]' if args.dry else ''} ==")
    center = None
    if args.center:
        lat, lng = args.center.split(",")
        center = {"lat": float(lat), "lng": float(lng)}
    else:
        center = geocode_center(city, key, opener)
    print(f"[1/6] 中心 {center}；按类目采集 ...")
    raw = collect(city, key, opener, args.per_kw)
    if len(raw) < 15:
        raise SystemExit(f"❌ 采集量过少（{len(raw)} 个），请检查城市名/关键词")
    print(f"[2/6] 校坐标去重：{len(raw)} → ", end="")
    pois = sanitize(raw, center, prefix)
    cats = {}
    for p in pois:
        cats[p["category"]] = cats.get(p["category"], 0) + 1
    print(f"{len(pois)} 个（类目 {dict(sorted(cats.items()))}）")
    if len(pois) < 15:
        raise SystemExit("❌ 去重后可用 POI 过少，扩城中止")
    print(f"[3/6] 落盘 {city}_pois.json ...")
    path = build_city_file(city, center, pois, args.dry)
    if args.dry:
        print("dry-run 结束（未落盘/未注册/未建缓存）")
        return
    n = build_travel_cache(city)
    print(f"[4/6] OSRM 交通矩阵：{n} 对合入 travel_cache.json")
    if args.skip_photos:
        print("[5/6] 跳过照片预取")
    else:
        fetched, with_img = fetch_photos(city, json.load(open(path, encoding="utf-8"))["pois"], key)
        print(f"[5/6] 照片预取：新取 {fetched}，有图 {with_img}/{len(pois)}")
    reg = register_city(city)
    print(f"[6/6] 注册：server.py CITIES + eval CASES {'已更新' if reg else '已存在，跳过'}")
    print("冒烟验证（离线 M1）:")
    ok = smoke(city)
    print(f"\n{'✅ 扩城完成' if ok else '⚠️ 冒烟未过（查看上方违规详情）'}：{city} 共 {len(pois)} POI，"
          f"重启 webui/server.py 后生效。建议：人工核实闭馆日（closed_days 目前全空）。")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
