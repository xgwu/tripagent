"""城市注册一致性守卫（2026-09-15 报障17 沉淀）。

背景：开城广州时只补了 `scripts/fetch_poi_photos.py` 的 CITYCODE，漏了
`webui/server.py` 里**自带的同名副本** `_ID_PREFIX_CITY` / `_CITYCODE`
→ `/api/photo?id=GZ001` 直接 400 → 前端 POI 缩略图全部拿不到实拍图。

这类「同一份城市元数据在多个文件各存一份」的结构性风险必须靠测试兜住：
任何一城只要在 CITIES 里注册，就必须在**所有下游映射表**里有对应条目，
且该城 POI 库里的 id 前缀必须能被 `_ID_PREFIX_CITY` 识别。

纯文本解析（不 import 被测模块），避免 server 侧 stdout 包装等副作用。
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

FAIL = []


def ok(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAIL.append(msg)


def parse_list(path: str, name: str) -> list:
    """从源码里抽 `NAME = [...]` 列表字面量（字符串元素）。"""
    src = open(path, encoding="utf-8").read()
    m = re.search(rf"^{name}\s*=\s*\[(.*?)\]", src, re.M | re.S)
    if not m:
        return []
    return re.findall(r"[\"']([^\"']+)[\"']", m.group(1))


def parse_dict(path: str, name: str) -> dict:
    """从源码里抽 `NAME = {...}` 字典字面量（纯字符串键值）。"""
    src = open(path, encoding="utf-8").read()
    m = re.search(rf"^{name}\s*=\s*\{{(.*?)\}}", src, re.M | re.S)
    if not m:
        return {}
    body = m.group(1)
    pairs = re.findall(r"[\"']([^\"']+)[\"']\s*:\s*[\"']([^\"']+)[\"']", body)
    return dict(pairs)


def poi_prefixes(city: str) -> set:
    path = os.path.join("data", f"{city}_pois.json")
    if not os.path.exists(path):
        return set()
    d = json.load(open(path, encoding="utf-8"))
    pois = d["pois"] if isinstance(d, dict) else d
    return {p["id"][:2].upper() for p in pois if p.get("id")}


print("== 1. 抽取消费方注册表 ==")
cities = parse_list("webui/server.py", "CITIES")
id_prefix_city = parse_dict("webui/server.py", "_ID_PREFIX_CITY")
city_code_server = parse_dict("webui/server.py", "_CITYCODE")
city_code_photos = parse_dict("scripts/fetch_poi_photos.py", "CITYCODE")
cities_expand = parse_list("scripts/expand_travel_cache.py", "CITIES")
cities_m7 = parse_list("eval_m7.py", "CITIES")
cities_ab = parse_list("eval_ab.py", "CITIES")
reg_cities = sorted({c for c in re.findall(r"[\"']([\u4e00-\u9fa5]{2,4})[\"']", 
                                          open("scripts/eval_regression.py", encoding="utf-8").read())
                      if c in cities})

print(f"   CITIES(server)      = {cities}")
print(f"   CITIES(expand)      = {cities_expand}")
print(f"   CITIES(eval_m7)     = {cities_m7}")
print(f"   CITIES(eval_ab)     = {cities_ab}")
print(f"   CITIES(regression)  = {reg_cities}")
print(f"   _ID_PREFIX_CITY     = {id_prefix_city}")
print(f"   _CITYCODE(server)   = {city_code_server}")
print(f"   CITYCODE(photos)    = {city_code_photos}")
print()

print("== 2. 每城在所有映射表中都有条目 ==")
missing = []
for c in cities:
    miss = []
    if c not in id_prefix_city.values():
        miss.append("server._ID_PREFIX_CITY")
    if c not in city_code_server:
        miss.append("server._CITYCODE")
    if c not in city_code_photos:
        miss.append("fetch_poi_photos.CITYCODE")
    if c not in cities_expand:
        miss.append("expand_travel_cache.CITIES")
    if miss:
        missing.append((c, miss))
    ok(not miss, f"{c}: {'全部就位' if not miss else '缺 ' + ', '.join(miss)}")
print()

print("== 3. POI 库 id 前缀必须被 _ID_PREFIX_CITY 识别 ==")
for c in cities:
    prefs = poi_prefixes(c)
    if not prefs:
        ok(False, f"{c}: data/{c}_pois.json 缺失或无 POI")
        continue
    unknown = sorted(p for p in prefs if p not in id_prefix_city)
    ok(not unknown, f"{c}: 前缀 {sorted(prefs)} "
                    + ("全部已注册" if not unknown else f"→ 未注册 {unknown}（/api/photo 会 400）"))
print()

print("== 4. 映射表无悬挂条目（指向不存在的城市） ==")
for pref, cname in sorted(id_prefix_city.items()):
    ok(os.path.exists(os.path.join("data", f"{cname}_pois.json")),
       f"前缀 {pref} → {cname}（POI 库存在）")
print()

print("== 5. 评测/回归用例覆盖（提示性，不阻断） ==")
for label, lst in (("eval_m7", cities_m7), ("eval_ab", cities_ab), ("regression", reg_cities)):
    extra = [c for c in cities if c not in lst]
    print(f"   {label}: {len(lst)}/{len(cities)} 城"
          + (f"，未覆盖 {extra}" if extra else "，全覆盖"))

print()
print("全部通过" if not FAIL else f"失败 {len(FAIL)} 项：")
for f in FAIL:
    print("   -", f)
sys.exit(1 if FAIL else 0)
