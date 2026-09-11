# -*- coding: utf-8 -*-
"""P3：预取五城全部 POI 实拍图（高德 place/text extensions=all 的 photos 字段），
写入 data/photo_cache.json（pid → https 直链 或 ""，空串=无图）。
已缓存有图的点自动跳过；无图（空串）的点重跑时会重试。
用法：python scripts/fetch_poi_photos.py
"""
import json, os, sys, time, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CITYCODE = {"上海": "021", "杭州": "0571", "南京": "025", "苏州": "0512", "武汉": "027"}  # 新扩城市自动回退用中文城市名查询
CACHE = os.path.join(ROOT, "data", "photo_cache.json")


def fetch(name: str, city: str, key: str, opener) -> str:
    u = (f"https://restapi.amap.com/v3/place/text"
         f"?keywords={urllib.parse.quote(name)}&city={CITYCODE[city]}"
         f"&key={key}&extensions=all&offset=1&page=1")
    with opener.open(u, timeout=10) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    if d.get("status") != "1":
        return ""
    for poi in d.get("pois") or []:
        for ph in poi.get("photos") or []:
            link = ph.get("url") or ""
            if link.startswith("http://"):
                link = "https://" + link[7:]
            if link.startswith("https://"):
                return link
    return ""


def main():
    from src.config import load_config
    key = load_config().get("amap_key")
    if not key:
        print("secrets.json/config.json 缺少 amap_key")
        sys.exit(1)
    cache = {}
    if os.path.exists(CACHE):
        cache = json.load(open(CACHE, encoding="utf-8"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    have = fetched = miss = 0
    for city in CITYCODE:
        data = json.load(open(os.path.join(ROOT, "data", f"{city}_pois.json"),
                              encoding="utf-8"))
        for p in data["pois"]:
            pid = p["id"]
            if cache.get(pid):
                have += 1
                continue
            try:
                url = fetch(p["name"], city, key, opener)
            except Exception as e:  # noqa: 单点失败不中断
                print(f"  ! {pid} {p['name']}: {e}")
                continue
            cache[pid] = url
            fetched += 1
            if url:
                pass
            else:
                miss += 1
            time.sleep(0.05)
        # 每城落盘一次，中断可续跑
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    total = len(cache)
    withimg = sum(1 for v in cache.values() if v)
    print(f"完成：缓存 {total} 点（有图 {withimg}，无图 {total - withimg}）；"
          f"本次已有 {have}，新取 {fetched}（无图 {miss}）→ {CACHE}")


if __name__ == "__main__":
    main()
