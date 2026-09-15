# -*- coding: utf-8 -*-
"""POI 图片缺口补齐（fetch_poi_photos.py 的互补工具）。

`fetch_poi_photos.py` 做的是**全量首轮**采集（按城市批量，CITYCODE 限定）；
本脚本做的是**缺口定向补漏**：只处理 `data/photo_cache.json` 里为空值/缺失的点位，
用多关键词变体 + 坐标周边搜索兜底，并逐候选**实测可达性**才算成功。

为什么需要它（2026-09-15 报障17 沉淀）：
- 首轮全量取图命中率有限（点名含括号、连锁店分店名等易 miss）
- 高德图床部分 URL 返回 `application/octet-stream`，只看 Content-Type 会误杀
  真图 → 判据必须是「Content-Type image/* **或** magic bytes 命中图片格式」
- 图片缺失会直接让前端缩略图退化成地图瓦片，用户观感是「POI 没有图」

用法：
    python scripts/backfill_poi_photos.py              # 全部城市
    python scripts/backfill_poi_photos.py 苏州 南京      # 指定城市
"""
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

PC_PATH = "data/photo_cache.json"
CITYCODE = {"上海": "021", "杭州": "0571", "南京": "025", "苏州": "0512",
            "武汉": "027", "北京": "010", "成都": "028", "广州": "020"}

# 人工关键词变体（首轮 miss 的疑难点位；新增难点时在此登记）
VARIANTS = {
    "GZ016": ["广州十三行博物馆", "十三行博物馆"],
    "GZ046": ["长隆欢乐世界", "广州长隆欢乐世界", "长隆旅游度假区"],
    "GZ053": ["点都德大茶楼", "点都德"],
    "GZ060": ["陈添记", "陈添记鱼皮"],
    "GZ066": ["天字码头", "珠江夜游"],
    "GZ035": ["白云山", "白云山风景区"],
}

MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF")  # JPEG / PNG / GIF / WEBP


def _load_key() -> str:
    with open("secrets.json", encoding="utf-8") as f:
        return json.load(f)["amap_key"]


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _https(u: str) -> str:
    if not u:
        return ""
    if u.startswith("http://"):
        return "https://" + u[7:]
    return u if u.startswith("https://") else ""


def probe(url: str) -> bool:
    """实测 URL 是否真为图片（Content-Type image/* 或 magic bytes 命中）。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with OPENER.open(req, timeout=12) as r:
            ct = (r.headers.get("Content-Type") or "").lower()
            head = r.read(16)
        return ct.startswith("image") or any(head.startswith(m) for m in MAGIC)
    except Exception:
        return False


def fetch(key: str, kw: str, city: str, around: str | None = None) -> list:
    """高德 place/text 或 place/around，返回全部候选图片 URL。"""
    if around:
        q = urllib.parse.urlencode({"location": around, "keywords": kw, "radius": 800,
                                    "key": key, "extensions": "all", "offset": 10, "page": 1})
        api = "https://restapi.amap.com/v3/place/around?"
    else:
        q = urllib.parse.urlencode({"keywords": kw, "city": CITYCODE.get(city, ""),
                                    "key": key, "extensions": "all", "offset": 10, "page": 1})
        api = "https://restapi.amap.com/v3/place/text?"
    try:
        with OPENER.open(api + q, timeout=12) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        return []
    if d.get("status") != "1":
        return []
    out = []
    for poi in d.get("pois") or []:
        for ph in poi.get("photos") or []:
            u = _https(ph.get("url") or "")
            if u:
                out.append(u)
    return out


def backfill(city: str, key: str, pc: dict) -> tuple:
    """补一城缺口，返回 (总点数, 有图数, 本次补数)。"""
    path = f"data/{city}_pois.json"
    if not os.path.exists(path):
        return (0, 0, 0)
    pois = json.load(open(path, encoding="utf-8"))["pois"]
    miss = [p for p in pois if not pc.get(p["id"])]
    if not miss:
        print(f"[{city}] 已全覆盖 ✅")
        return (len(pois), len(pois), 0)
    print(f"[{city}] 待补 {len(miss)} 个")
    fixed = 0
    for p in miss:
        pid, name = p["id"], p["name"]
        kws = VARIANTS.get(pid) or ([name, name.split("（")[0]] if "（" in name else [name])
        got = ""
        for kw in kws:                       # 1) 文本搜索（全名 → 去括号 → 人工变体）
            for u in fetch(key, kw, city)[:5]:
                if probe(u):
                    got = u
                    break
            if got:
                break
            time.sleep(0.1)
        if not got:                          # 2) 坐标周边搜索兜底
            for u in fetch(key, kws[-1][:6], city, around=f"{p['lng']},{p['lat']}")[:5]:
                if probe(u):
                    got = u
                    break
        if got:
            pc[pid] = got
            fixed += 1
            print(f"   ✅ {pid} {name}")
        else:
            print(f"   ⚠️ {pid} {name} 仍无图（高德无实拍图，前端回退瓦片）")
        time.sleep(0.15)
    json.dump(pc, open(PC_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    return (len(pois), sum(1 for x in pois if pc.get(x["id"])), fixed)


def main() -> None:
    cities = sys.argv[1:] or list(CITYCODE)
    key = _load_key()
    pc = json.load(open(PC_PATH, encoding="utf-8"))
    total = 0
    rows = []
    for city in cities:
        tot, has, fx = backfill(city, key, pc)
        if tot:
            rows.append((city, tot, has, fx))
            total += fx
    print("\n== 汇总 ==")
    for city, tot, has, fx in rows:
        print(f"  {city:<4} {has}/{tot} = {has * 100 // tot}%  (本次补 {fx})")
    print(f"\n本次共补 {total} 张")


if __name__ == "__main__":
    main()
