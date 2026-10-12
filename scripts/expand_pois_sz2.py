# -*- coding: utf-8 -*-
"""苏州 POI 二期扩容（SZ076-087，12 个）—— 2026-10-12 定向补库。

缺口来源（`dist/_analyze_suzhou.py` 分析）：
  A 组「园林」：苏州 9 座世遗园林库里只有 8 座，**独缺环秀山庄**（叠石假山天花板）；
     并补市中小园林群（怡园/曲园/可园/五峰园），补齐「园林之城」叙事。
  B 组「博物馆 + 相城空白」：**免费室内场馆**（雨天/亲子刚需）与**相城北片区**
     几乎空白 —— 补考古博物馆/吴文化/丝绸/昆曲/评弹/园林博物馆 + 荷塘月色。

- 坐标：高德 place/text API（首个非「停车场/售票处/出入口/码头」结果），带 1.2s
  节流 + 失败重试，避开 QPS 限流；失败或偏移 >2km 时退回人工坐标兜底（fb，已实测校准）。
- closed_days 统一「周X」；博物馆普遍周一闭馆（考古博物馆周三闭馆）。
- 幂等：按 name/id 已存在则跳过。

用法：python scripts/expand_pois_sz2.py [--dry]
"""
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CITY = "苏州"
SLEEP = 1.2  # 避开高德 place/text QPS 限流

NEW_POIS = [
    # ---- A 组：园林（补「9 座世遗」最后一块 + 市中小园林群）----
    dict(name="环秀山庄", kw="环秀山庄", id="SZ076", category="culture",
         tags=["园林", "世界遗产", "假山", "小众", "经典"],
         fb=(31.3109, 120.6135),
         duration_h=1.0, open="07:30", close="17:30", best_time="morning", price=15, rating=5,
         area="central", family_ok=True, closed_days=[],
         note="苏州9座世遗园林之一（库内此前独缺此园），清初叠山大师戈裕良杰作，"
              "占地仅3亩却「独步江南」，假山不许攀爬只能远观；15元（学生7.5），"
              "需「苏州园林旅游」公众号提前实名预约（2026-10 携程/同程核价）"),
    dict(name="怡园", kw="怡园 人民路", id="SZ077", category="culture",
         tags=["园林", "小众", "安静", "经典"],
         fb=(31.3075, 120.6213),
         duration_h=1.0, open="07:30", close="17:30", best_time="afternoon", price=15, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="人民路1265号，乐桥地铁站旁，晚清顾文彬建，博采众园之长（复廊/假山/荷池俱佳），"
              "闹中取静紧邻观前街；15元（学生7.5），2026-10 携程/同程核价"),
    dict(name="曲园", kw="曲园 马医科", id="SZ078", category="culture",
         tags=["园林", "免费", "小众", "名人故居", "安静"],
         fb=(31.3096, 120.6185),
         duration_h=1.0, open="09:00", close="16:30", best_time="morning", price=0, rating=4,
         area="central", family_ok=True, closed_days=["周一"],
         note="马医科巷43号，晚清朴学大师俞樾故居，取「曲则全」之意；免费（现场登记即可），"
              "周一闭馆，冬春赏腊梅、春日白玉兰，《骄阳似我》取景地（2026-10 同程核实）"),
    dict(name="可园", kw="可园 沧浪亭街", id="SZ079", category="culture",
         tags=["园林", "小众", "安静"],
         fb=(31.2954, 120.6250),
         duration_h=1.0, open="07:30", close="17:30", best_time="morning", price=25, rating=4,
         area="south", family_ok=True, closed_days=[],
         note="沧浪亭街3号，与沧浪亭一墙之隔（原正谊书院），粉墙黛瓦古朴淡雅，"
              "可与沧浪亭买联票串联；25元（学生/老人12.5），2026-10 携程/同程核价"),
    dict(name="五峰园", kw="五峰园", id="SZ080", category="culture",
         tags=["园林", "免费", "小众", "安静"],
         fb=(31.3171, 120.6098),
         duration_h=0.5, open="08:00", close="16:00", best_time="afternoon", price=0, rating=4,
         area="northwest", family_ok=True, closed_days=[],
         note="阊门内下塘五峰园弄15号，明代迷你小园，五座太湖石峰立池边得名，十分钟转完；"
              "免费预约，常无人清静（2026-10 同程核实）"),

    # ---- B 组：博物馆 / 免费室内场馆 + 相城空白 ----
    dict(name="苏州考古博物馆", kw="苏州考古博物馆", id="SZ081", category="culture",
         tags=["博物馆", "历史", "室内", "雨天", "亲子", "免费"],
         fb=(31.2558, 120.5928),
         duration_h=1.5, open="09:00", close="17:00", best_time="any", price=0, rating=4,
         area="south", family_ok=True, closed_days=["周三"],
         note="江苏首座考古专题馆（石湖景区内越堤路321号，2026新馆），展陈1200件出土文物、"
              "80%首展，复原「东吴大墓」；免费预约，周三闭馆、16:40停止入内（2026-10 腾讯新闻/携程核实）"),
    dict(name="吴文化博物馆", kw="吴文化博物馆", id="SZ082", category="culture",
         tags=["博物馆", "历史", "室内", "雨天", "亲子", "免费"],
         fb=(31.2537, 120.6486),
         duration_h=2.0, open="09:00", close="17:00", best_time="any", price=0, rating=4,
         area="south", family_ok=True, closed_days=["周一"],
         note="吴中区澹台街9号（宝带桥/澹台湖畔），国家一级博物馆1.8万㎡，以「考古探吴中」"
              "「风雅颂吴中」展陈呈现吴地千年文脉；免费预约，周一闭馆（2026-10 同程/腾讯新闻核实）"),
    dict(name="苏州丝绸博物馆", kw="苏州丝绸博物馆", id="SZ083", category="culture",
         tags=["博物馆", "丝绸", "非遗", "室内", "雨天", "亲子", "免费"],
         fb=(31.3228, 120.6182),
         duration_h=1.5, open="09:00", close="17:00", best_time="any", price=0, rating=4,
         area="north", family_ok=True, closed_days=["周一"],
         note="人民路2001号（北寺塔旁，地铁4号线北寺塔站），中国首座丝绸专业馆、国家一级博物馆，"
              "藏6400余件文物，设少儿丝绸科普馆；免费，周一闭馆、16:00停止入馆（2026-10 腾讯新闻核实）"),
    dict(name="中国昆曲博物馆", kw="中国昆曲博物馆", id="SZ084", category="culture",
         tags=["博物馆", "昆曲", "非遗", "室内", "文化", "免费"],
         fb=(31.3131, 120.6357),
         duration_h=1.0, open="09:00", close="16:30", best_time="afternoon", price=0, rating=4,
         area="central", family_ok=True, closed_days=["周一"],
         note="平江路中张家巷14号（全晋会馆内），昆曲（人类口述和非物质遗产）专题馆，"
              "苏州「三朵金花」之一；免费，周一闭馆（2026-10 携程核实）"),
    dict(name="中国苏州评弹博物馆", kw="苏州评弹博物馆", id="SZ085", category="culture",
         tags=["博物馆", "评弹", "非遗", "室内", "文化", "免费"],
         fb=(31.3128, 120.6348),
         duration_h=1.0, open="09:00", close="16:30", best_time="afternoon", price=0, rating=4,
         area="central", family_ok=True, closed_days=["周一"],
         note="平江路中张家巷3号，与昆曲博物馆相邻，评弹（苏州「三朵金花」之一）专题馆；"
              "免费，周一闭馆（2026-10 携程核实）"),
    dict(name="苏州园林博物馆", kw="苏州园林博物馆", id="SZ086", category="culture",
         tags=["博物馆", "园林", "室内", "雨天", "亲子", "免费"],
         fb=(31.3231, 120.6293),
         duration_h=1.0, open="09:00", close="16:30", best_time="afternoon", price=0, rating=4,
         area="central", family_ok=True, closed_days=["周一"],
         note="东北街202号，紧邻拙政园/苏博本馆，中国首座园林专题博物馆，讲透造园手法；"
              "免费，周一闭馆（2026-10 携程核实）"),
    dict(name="荷塘月色湿地公园", kw="荷塘月色湿地公园", id="SZ087", category="nature",
         tags=["湿地", "荷花", "赏花", "亲子", "户外", "夜景"],
         fb=(31.4089, 120.5835),
         duration_h=2.0, open="08:30", close="17:00", best_time="afternoon", price=25, rating=4,
         area="north", family_ok=True, closed_days=[],
         note="相城区太阳路4575号，3A 级荷花主题城市湿地公园（2000亩、500余种荷花睡莲王莲），"
              "补相城北片区空白；25元/夜场19.9，7-10月开至22:00（21:30停止入园），"
              "6月底-8月盛花最值（2026-10 携程/同程核价）"),
]


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


BAD_TOKENS = ("停车场", "售票处", "出入口", "游客中心", "保管部", "码头")


def amap_geocode(kw: str, key: str, retries: int = 3):
    """返回 (lat, lng, 命中名) 或 None。带节流 + 重试（高德 place/text 有 QPS 限流）。"""
    if not key:
        return None
    url = ("https://restapi.amap.com/v3/place/text?keywords=" + urllib.parse.quote(kw)
           + "&city=" + urllib.parse.quote(CITY)
           + f"&citylimit=true&key={key}&offset=5")
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(retries):
        try:
            with op.open(url, timeout=10) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            if d.get("status") == "1":
                for cand in d.get("pois") or []:
                    if any(t in cand.get("name", "") for t in BAD_TOKENS):
                        continue
                    loc = cand["location"].split(",")
                    return float(loc[1]), float(loc[0]), cand.get("name", "")
        except Exception:
            pass
        time.sleep(SLEEP)
    return None


def main():
    sys.path.insert(0, ROOT)
    from src.config import load_config
    key = os.environ.get("AMAP_KEY") or load_config().get("amap_key") or ""

    dry = "--dry" in sys.argv
    path = os.path.join(DATA, f"{CITY}_pois.json")
    doc = json.load(open(path, encoding="utf-8"))
    pois = doc["pois"]
    existing_names = {p["name"] for p in pois}
    existing_ids = {p["id"] for p in pois}

    added = 0
    for np in NEW_POIS:
        if np["name"] in existing_names or np["id"] in existing_ids:
            print(f"跳过（已存在）: {np['name']}")
            continue
        hit = amap_geocode(np["kw"], key)
        if hit is None:
            coord, src = np["fb"], "manual(amap none)"
        else:
            lat, lng, hname = hit
            dist = haversine_km(lat, lng, *np["fb"])
            if dist > 2.0:
                coord, src = np["fb"], f"manual(amap Δ{dist:.1f}km «{hname}»)"
            else:
                coord, src = (round(lat, 6), round(lng, 6)), f"amap«{hname}»"
        poi = dict(id=np["id"], name=np["name"], category=np["category"],
                   tags=np["tags"], lat=coord[0], lng=coord[1],
                   duration_h=np["duration_h"], open=np["open"], close=np["close"],
                   best_time=np["best_time"], price=np["price"], rating=np["rating"],
                   area=np["area"], family_ok=np["family_ok"],
                   note=np["note"], closed_days=np["closed_days"])
        pois.append(poi)
        added += 1
        print(f"+ {np['id']} {np['name']} [{src}] @ ({coord[0]:.4f}, {coord[1]:.4f})")
        time.sleep(SLEEP)

    if added and not dry:
        pois.sort(key=lambda p: p["id"])
        json.dump(doc, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"共新增 {added} 个（{CITY} 现有 {len(pois)} 个）" + ("  [dry 未写盘]" if dry else ""))


if __name__ == "__main__":
    main()
