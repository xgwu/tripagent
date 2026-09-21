# -*- coding: utf-8 -*-
"""按「热度排序的 POI 名单」开城（贵州批次专用，可复用于其他省）。

为什么不用 add_city.py 的类目泛搜：
  它按 CATEGORY_KEYWORDS 搜「博物馆/公园/步行街…」，选出来的混着大量非景点，
  且 10 城实测全部撞满 cap=80 —— 真正决定留哪 80 个的是 `sort(-rating, name)`，
  而 rating 在修复前几乎全被压成 4.0，等于按名字排序（见 2026-09-17 审查缺陷 5）。
  本脚本改为「外部热度名单选点 + 高德补字段」：选点依据是真实热度而非名字。

用法：
  python scripts/onboard_from_poilist.py --city 安顺 --prefix AS --citycode 0853 \
      --xlsx D:/Users/xgwu/Downloads/71755673-sql-3.xlsx [--cap 80] [--dry]

流水线（幂等，可中断重跑）：
  1. 从 xlsx 取该城 60km 内的点，按热度序取前 cap 个
  2. 高德 place/text 补营业时间/类目/评分/图片，写 note 留痕（来源+日期）
  3. 落盘 data/{city}_pois.json（schema 对齐 validate_city.REQUIRED）
  4. OSRM /table 建同城交通矩阵，**纯增量**合入 travel_cache.json
  5. 照片写入 photo_cache.json
  6. 打印需人工核实的字段清单
注册（7 处）与校验不在本脚本内，由 --register 单独执行，便于 review。
"""
import argparse
import datetime
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from xml.etree import ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_keep = [sys.stdout, sys.stderr]
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from src.config import load_config  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
TODAY = datetime.date.today().isoformat()

# 高德 type 前缀 → 项目类目（对齐 validate_city.CATEGORIES）
TYPE_MAP = [
    ("风景名胜;世界遗产", "culture"), ("风景名胜;国家级景点", "nature"),
    ("风景名胜;水利景观", "nature"), ("风景名胜;山川", "nature"),
    ("风景名胜;公园广场", "park"), ("风景名胜;宗教", "religion"),
    ("风景名胜;纪念", "history"), ("风景名胜", "view"),
    ("科教文化服务;博物馆", "museum"), ("科教文化服务;美术馆", "art"),
    ("科教文化服务;文艺", "art"), ("科教文化服务;科技馆", "family"),
    ("科教文化服务", "culture"),
    ("餐饮服务", "food"), ("购物服务", "shopping"),
    ("体育休闲服务;度假疗养", "relax"), ("体育休闲服务;影剧院", "show"),
    ("体育休闲服务;娱乐场所", "nightlife"), ("体育休闲服务", "sport"),
    ("住宿服务", "_skip"), ("公司企业", "_skip"), ("政府机构", "_skip"),
    ("交通设施服务", "_skip"), ("金融保险", "_skip"), ("生活服务", "_skip"),
    ("医疗保健", "_skip"), ("汽车", "_skip"),
]
# 类目默认时长（高德不提供，需人工核实；口径同 add_city.CATEGORY_DEFAULTS）
DUR = {"culture": 1.5, "history": 1.5, "museum": 2.0, "art": 1.5,
       "nature": 2.5, "view": 1.5, "park": 1.5, "religion": 1.0,
       "family": 2.5, "food": 1.5, "shopping": 1.5, "show": 2.0,
       "sport": 2.0, "nightlife": 1.5, "relax": 2.5, "outdoor": 3.0,
       "indoor": 1.5}
DEFAULT_OPEN = {"food": ("10:30", "21:00"), "nightlife": ("18:00", "22:00"),
                "shopping": ("10:00", "22:00"), "park": ("06:00", "21:00"),
                "nature": ("08:00", "17:30"), "museum": ("09:00", "17:00"),
                "art": ("09:00", "17:00"), "religion": ("07:00", "17:30")}
JUNK = re.compile(r"售票|停车场|游客中心|服务区|收费站|检票|入口|出口|导游|厕所|"
                  r"有限公司|旅行社|办事处|管理局|管理处|派出所|卫生院")


def api(path, params, key, tries=3):
    qs = urllib.parse.urlencode({**params, "key": key})
    for a in range(1, tries + 1):
        try:
            with OPENER.open(f"https://restapi.amap.com/v3/{path}?{qs}", timeout=15) as r:
                d = json.loads(r.read().decode("utf-8"))
            if d.get("status") == "1":
                return d
            time.sleep(0.4 * a)
        except Exception:
            time.sleep(0.6 * a)
    return {}


def hav(a, b):
    r = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    x = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(x))


def read_xlsx(path):
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{NS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))

    def cidx(ref):
        m = re.match(r"([A-Z]+)", ref)
        n = 0
        for ch in m.group(1) if m else "A":
            n = n * 26 + (ord(ch) - 64)
        return n - 1

    root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    out = []
    for r in root.iter(f"{NS}row"):
        cells = {}
        for c in r.findall(f"{NS}c"):
            t = c.get("t")
            v, isel = c.find(f"{NS}v"), c.find(f"{NS}is")
            if t == "inlineStr" and isel is not None:
                # 本数据源（SQL 导出）用的是 inlineStr，sharedStrings 表为空
                val = "".join(x.text or "" for x in isel.iter(f"{NS}t"))
            elif t == "s" and v is not None and v.text and int(v.text) < len(shared):
                val = shared[int(v.text)]
            else:
                val = v.text if v is not None else ""
            cells[cidx(c.get("r", "A1"))] = (val or "").strip()
        if cells:
            out.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return out


# 名称判据（优先于高德类目）：高德的「风景名胜」过粗，瀑布/古镇/溶洞全归一类，
# 而主题召回（KEYWORD_TAGS）恰恰依赖细分类目。中文景点名的信息量更大。
# 注意高德 type 是三级格式「风景名胜;风景名胜;国家级景点」，不能用二级前缀匹配。
NAME_RULES = [
    (r"博物馆|纪念馆|陈列馆|展览馆|科技馆|天文馆", "museum"),
    (r"美术馆|艺术馆|画院|文化艺术中心", "art"),
    (r"寺|庙|禅院|道观|教堂|塔院|祠", "religion"),
    (r"动物园|海洋|游乐|乐园|水世界|欢乐|亲子|马戏|极地|VR|XR|沉浸式|体验馆", "family"),
    (r"瀑布|溶洞|溪|峡谷|梯田|草原|森林|湿地|湖|河|江|温泉|山|岩|洞|天坑|地缝", "nature"),
    (r"古镇|古城|古村|古街|老街|屯堡|苗寨|侗寨|布依|会址|旧址|故居|遗址|城墙", "history"),
    (r"公园|广场|植物园|樱花园|花海", "park"),
    # 夜生活必须用明确词：曾用裸「秀」导致「甲秀楼」这个明代古楼被归为夜生活
    # → 连带 family_ok=False（地标被踢出亲子池）+ 营业时间被写成 18:00-22:00。
    (r"夜市|酒吧|夜游|夜景|演出|实景|剧场|脱口秀|相声|KTV|歌舞|灯光秀|音乐喷泉|酒馆|晚会|表演", "nightlife"),
    (r"美食|小吃|餐厅|酒楼|饭店|食府|粉|面馆", "food"),
    (r"步行街|商业街|购物|商城|万达|奥莱", "shopping"),
    (r"漂流|滑雪|滑草|攀岩|蹦极|索道|徒步|骑行|露营", "sport"),
    (r"度假|康养|民宿|温泉酒店|营地", "relax"),
    (r"观景|观光|大桥|眺望|打卡", "view"),
]


def pick_category(typestr, name, keytag=""):
    # 先按高德类目判断「该不该收」（住宿/公司/交通等直接跳过）
    for pre, cat in TYPE_MAP:
        if cat == "_skip" and typestr.startswith(pre):
            return "_skip"
    # 名称判据优先
    for pat, cat in NAME_RULES:
        if re.search(pat, name):
            return cat
    # keytag 比 type 细（如「国家级景点」「4A景区」）
    for pat, cat in NAME_RULES:
        if keytag and re.search(pat, keytag):
            return cat
    # 最后才用高德二级类目
    for pre, cat in TYPE_MAP:
        if cat != "_skip" and typestr.startswith(pre):
            return cat
    return "view"


def best_time_of(cat, open_h):
    if cat == "nightlife" or open_h >= 17:
        return "evening"
    if cat in ("museum", "art", "culture", "history"):
        return "morning"
    if cat == "food":
        return "lunch"
    return "any"


def area_of(dist_km, lat, lng, center):
    if dist_km > 12:
        return "suburb"
    dlat, dlng = lat - center[0], lng - center[1]
    if abs(dlat) < 0.02 and abs(dlng) < 0.02:
        return "central"
    ns = "north" if dlat > 0 else "south"
    ew = "east" if dlng > 0 else "west"
    return ns if abs(dlat) > abs(dlng) * 1.5 else (ew if abs(dlng) > abs(dlat) * 1.5
                                                  else f"{ns}{ew}")


MATCH_MAX_KM = 8.0   # 高德匹配点与名单坐标的最大容差


def enrich(name, citycode, key, ref=None):
    """高德补字段。返回 dict 或 None（脏数据/住宿类/误匹配）。

    ref：名单里的可信坐标 (lat, lng)。**必须做距离校验** —— citylimit=true 会把
    跨州的正确 POI 过滤掉，让附近的垃圾点顶上来：实测「织金洞」在 city=0853 下
    匹配到「S55仁望高速入口(织金方向)」、「红枫湖风景区」匹配到「红旗桥」
    （织金属毕节 0857、红枫湖属贵阳 0851，都不在安顺辖区）。
    不校验就会把高速入口当景点写进库。
    """
    cands = []
    d = api("place/text", {"keywords": name, "city": citycode, "citylimit": "true",
                           "extensions": "all", "offset": 3, "page": 1}, key)
    cands += (d.get("pois") or [])
    # citylimit 命中不了或偏太远时，放开城市限制再搜一次（靠坐标校验兜底正确性）
    d2 = api("place/text", {"keywords": name, "extensions": "all",
                            "offset": 3, "page": 1}, key)
    cands += (d2.get("pois") or [])
    for p in cands:
        typestr = p.get("type", "") or ""
        cat = pick_category(typestr, name, p.get("keytag", "") or "")
        if cat == "_skip":
            continue
        biz = p.get("biz_ext") or {}
        rating_raw = str(biz.get("rating") or "")
        m = re.search(r"\d+(?:\.\d+)?", rating_raw)
        rating = float(m.group(0)) if m else 0.0
        cost_m = re.search(r"\d+(?:\.\d+)?", str(biz.get("cost") or ""))
        # 字段名实测：景区营业时间在 biz_ext.opentime2（open_time 常为空数组）。
        # 格式是分季节复合文本：「9月1日至11月30日 07:00-18:30开放(16:00停止售票)；…」
        opentime = str(biz.get("open_time") or "") or str(biz.get("opentime2") or "")
        photos = [ph.get("url", "") for ph in (p.get("photos") or []) if ph.get("url")]
        try:
            lng, lat = [float(x) for x in p["location"].split(",")]
        except Exception:
            continue
        # 坐标校验：偏离名单坐标过远 = 误匹配（高速入口/同名桥梁等），丢弃
        if ref is not None and hav(ref, (lat, lng)) > MATCH_MAX_KM:
            continue
        return {"amap_name": p.get("name", ""), "cat": cat, "typestr": typestr,
                "keytag": p.get("keytag", "") or "",
                "rating": rating, "price": int(float(cost_m.group(0))) if cost_m else 0,
                "opentime": opentime, "photos": photos, "lat": lat, "lng": lng,
                "addr": p.get("address", "") or "", "tel": p.get("tel", "") or ""}
    return None


def parse_opentime(s, cat):
    """从高德 opentime2 抽 open/close；抽不到用类目默认。返回 (open, close, 是否实测)。

    高德景区营业时间是分季节复合文本：
      「9月1日至11月30日 07:00-18:30开放(16:00停止售票)；12月1日至次年2月28日 07:30-18:00…」
    营业时间是**硬约束**（排程按它判违规），故取所有季节的**保守交集**：
    最晚的开门时刻 + 最早的关门时刻。取第一段会在淡季给出过宽窗口。
    """
    s = s or ""
    if re.search(r"全天|24\s*小时", s):
        return "00:00", "23:59", True
    spans = re.findall(r"(\d{1,2}):(\d{2})\s*[-—~至]\s*(\d{1,2}):(\d{2})", s)
    if spans:
        opens = [int(h) * 60 + int(m) for h, m, _, _ in spans]
        closes = [int(h) * 60 + int(m) for _, _, h, m in spans]
        o_m, c_m = max(opens), min(closes)
        if c_m <= o_m:            # 交集为空（跨季差异过大）→ 退回第一段
            o_m = int(spans[0][0]) * 60 + int(spans[0][1])
            c_m = int(spans[0][2]) * 60 + int(spans[0][3])
        return (f"{o_m // 60:02d}:{o_m % 60:02d}",
                f"{c_m // 60:02d}:{c_m % 60:02d}", True)
    o, c = DEFAULT_OPEN.get(cat, ("09:00", "17:30"))
    return o, c, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", required=True)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--citycode", required=True, help="高德 citycode（区号），如 0853")
    ap.add_argument("--xlsx", required=True)
    ap.add_argument("--center", help="质心 lat,lng（默认高德地理编码）")
    ap.add_argument("--radius", type=float, default=60.0)
    ap.add_argument("--cap", type=int, default=80)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    key = load_config().get("amap_key")
    if not key:
        raise SystemExit("❌ secrets.json 缺 amap_key")

    if a.center:
        lat, lng = [float(x) for x in a.center.split(",")]
        center = (lat, lng)
    else:
        g = api("geocode/geo", {"address": a.city}, key)
        loc = (g.get("geocodes") or [{}])[0].get("location")
        if not loc:
            raise SystemExit(f"❌ 拿不到 {a.city} 质心，请用 --center")
        lo, la = loc.split(",")
        center = (round(float(la), 4), round(float(lo), 4))

    print(f"== 开城 {a.city}（前缀 {a.prefix} / citycode {a.citycode}）"
          f"{' [dry-run]' if a.dry else ''}")
    print(f"[1/6] 质心 {center}；从热度名单筛 {a.radius:.0f}km 内的点 …")
    rows = read_xlsx(a.xlsx)
    hdr = {h: i for i, h in enumerate(rows[0])}
    cand = []
    for rank, r in enumerate(rows[1:]):
        try:
            la = float(r[hdr["gaode_lat"]]); lo = float(r[hdr["gaode_lon"]])
        except (ValueError, IndexError, KeyError):
            continue
        nm = r[hdr["poiname"]]
        if JUNK.search(nm):
            continue
        d = hav(center, (la, lo))
        if d <= a.radius:
            cand.append({"name": nm, "rank": rank, "dist": d, "lat": la, "lng": lo,
                         "district": r[hdr.get("districtname", 0)]})
    # 名称去重（同一景区多个子条目保留热度最高的那个主条目）
    seen, uniq = set(), []
    for c in sorted(cand, key=lambda x: x["rank"]):
        base = re.split(r"[-·（(]", c["name"])[0].strip()
        if base in seen:
            continue
        seen.add(base)
        uniq.append(c)
    print(f"      名单内 {len(cand)} 个 → 去重后 {len(uniq)} 个 → 取热度前 {a.cap}")
    uniq = uniq[:a.cap]

    print(f"[2/6] 高德补字段（营业时间/类目/评分/图片）…")
    pois, photos, manual = [], {}, []
    for i, c in enumerate(uniq, 1):
        e = enrich(c["name"], a.citycode, key, ref=(c["lat"], c["lng"]))
        time.sleep(0.12)
        if not e:
            manual.append((c["name"], "高德无匹配或被判为非景点，已跳过"))
            continue
        o, cl, measured = parse_opentime(e["opentime"], e["cat"])
        pid = f"{a.prefix}{len(pois)+1:03d}"
        tags = [t for t in re.split(r"[;；]", e["typestr"])[:3] if t] or ["待补充"]
        note = (f"热度榜第 {c['rank']+1} 位（{c['district']}）；"
                f"坐标/类目来自高德 {TODAY}"
                + ("；营业时间取自高德实测" if measured
                   else f"；营业时间为 {e['cat']} 类目默认值，**待人工核实**"))
        pois.append({
            # 坐标用名单值（可信源）而非高德返回值：后者可能指向另一个入口/停车场，
            # 且 60km 圈判定与热度排序都是基于名单坐标做的，混用会前后不一致
            "id": pid, "name": c["name"], "category": e["cat"], "tags": tags,
            "lat": round(c["lat"], 6), "lng": round(c["lng"], 6),
            "duration_h": DUR.get(e["cat"], 1.5),
            "open": o, "close": cl,
            "best_time": best_time_of(e["cat"], int(o.split(":")[0])),
            "price": e["price"],
            "rating": max(3, min(5, int(round(e["rating"])) or 4)),
            "area": area_of(c["dist"], e["lat"], e["lng"], center),
            "family_ok": e["cat"] not in ("nightlife",),
            "note": note, "closed_days": [],
        })
        if e["photos"]:
            photos[pid] = e["photos"][0].replace("http://", "https://")
        if not measured:
            manual.append((c["name"], f"营业时间用了 {e['cat']} 默认 {o}-{cl}"))
        if i % 20 == 0:
            print(f"      {i}/{len(uniq)} …")
    print(f"      入库 {len(pois)} 个，有图 {len(photos)} 个")

    if a.dry:
        print("\n[dry-run] 未落盘。类目分布：")
        import collections
        print("   ", dict(collections.Counter(p["category"] for p in pois)))
        print("    area：", dict(collections.Counter(p["area"] for p in pois)))
        print(f"    需人工核实 {len(manual)} 项")
        for n, why in manual[:12]:
            print(f"      · {n[:24]}：{why}")
        return

    doc = {"city": a.city, "center": {"lat": center[0], "lng": center[1]},
           "day_start": "09:00", "day_end": "21:30",
           "meal_slots": {"lunch": ["12:00", "13:00"], "dinner": ["18:00", "19:00"]},
           "meal_cost": {"lunch": 50, "dinner": 80}, "pois": pois}
    path = os.path.join(ROOT, "data", f"{a.city}_pois.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f"[3/6] 落盘 {path}")

    # OSRM 交通矩阵（纯增量：只写本城主键，绝不触碰既有城市）
    from scripts import build_travel_cache as btc
    cache_path = os.path.join(ROOT, "data", "travel_cache.json")
    cache = json.load(open(cache_path, encoding="utf-8"))
    before = {k: dict(v) for k, v in cache["minutes"].items()}
    minutes, prefix = btc.build_osrm(a.city)
    added = 0
    for k, v in minutes.items():
        row = cache["minutes"].setdefault(k, {})
        added += sum(1 for b in v if b not in row)   # 真正新增的对数
        row.update(v)
    cache["cities"][a.city] = {"source": "osrm-table-driving",
                               "city_factor": btc.CITY_FACTOR, "id_prefix": prefix}
    # 纯度校验：合并后，既有主键及其行内既有对必须逐条不变。
    # 不能用「既有 key 不得以本城前缀开头」——重跑已开城市时 before 里已含本城前缀，
    # 那样的断言必炸，而且真正该防的「既有对被改」反而没查到。
    for k, row in before.items():
        now = cache["minutes"].get(k)
        assert now is not None, f"既有主键丢失: {k}"
        for b, val in row.items():
            assert now.get(b) == val, f"既有对被改: {k} -> {b}"
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    print(f"[4/6] OSRM 矩阵：新增 {added} 对（既有 {len(before)} 主键逐条未变）")

    pc_path = os.path.join(ROOT, "data", "photo_cache.json")
    pc = json.load(open(pc_path, encoding="utf-8"))
    pc.update(photos)
    with open(pc_path, "w", encoding="utf-8") as f:
        json.dump(pc, f, ensure_ascii=False)
    print(f"[5/6] 照片：新增 {len(photos)} 条（缓存共 {len(pc)} 条）")

    print(f"[6/6] 需人工核实 {len(manual)} 项（营业时间/闭馆日）：")
    for n, why in manual[:15]:
        print(f"      · {n[:26]}：{why}")
    if len(manual) > 15:
        print(f"      …还有 {len(manual)-15} 项，见 note 字段「待人工核实」")
    print(f"\n✅ {a.city} 落盘完成，{len(pois)} POI。"
          f"接下来跑注册（7 处）与 validate_city。")


if __name__ == "__main__":
    main()
