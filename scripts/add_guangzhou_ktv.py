# -*- coding: utf-8 -*-
"""入库广州 4 家 KTV（GZ076-GZ079）。

数据来源：高德 place/text + place/detail（2026-09-15 实时），坐标 GCJ-02。
字段惯例对齐广州库既有 nightlife 点；family_ok=false（KTV 不适宜低龄儿童，
与南京 NJ021 1912 街区同处理）。营业时间写真实值（跨零点由 parse_poi 归一化）。

用法：python scripts/add_guangzhou_ktv.py
"""
import io, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PATH = os.path.join(ROOT, "data", "广州_pois.json")

NEW = [
    {
        "id": "GZ076",
        "name": "纯K（岗顶店）",
        "category": "nightlife",
        "tags": ["KTV", "量贩式", "天河路商圈", "夜唱"],
        "lat": 23.133457,
        "lng": 113.337063,
        "duration_h": 2.0,
        "open": "09:00",
        "close": "06:00",
        "best_time": "evening",
        "price": 64,
        "rating": 5,
        "area": "east",
        "family_ok": False,
        "note": "天河路 518 号地中海壹荟 3 楼（岗顶地铁站），全国连锁量贩式 KTV，人均约 64 元；09:00 至次日 06:00 通宵营业，紧邻太古汇（0.5km）、正佳广场（1.0km），逛街后可续摊；房间建议提前预订",
        "closed_days": [],
    },
    {
        "id": "GZ077",
        "name": "堂会（缤缤店）",
        "category": "nightlife",
        "tags": ["KTV", "粤式", "北京路", "夜生活"],
        "lat": 23.115473,
        "lng": 113.264880,
        "duration_h": 2.0,
        "open": "11:00",
        "close": "03:00",
        "best_time": "evening",
        "price": 83,
        "rating": 4,
        "area": "central",
        "family_ok": False,
        "note": "北京路商圈海印缤缤广场 9-11 层，广州本土高端 KTV 品牌，人均约 83 元；距天字码头 0.65km（珠江夜游下船后可步行续摊）、北京路步行街 0.67km；营业至次日 03:00",
        "closed_days": [],
    },
    {
        "id": "GZ078",
        "name": "魅KTV·AI辅唱（花城汇店）",
        "category": "nightlife",
        "tags": ["KTV", "AI辅唱", "珠江新城", "连锁"],
        "lat": 23.126390,
        "lng": 113.325700,
        "duration_h": 2.0,
        "open": "12:00",
        "close": "06:00",
        "best_time": "evening",
        "price": 50,
        "rating": 4,
        "area": "east",
        "family_ok": False,
        "note": "花城汇北区东翼负一层（冼村街道黄埔大道西 74 号），主打 AI 辅唱的连锁 KTV，人均约 50 元；距花城广场 0.66km、正佳广场 0.65km、广州塔 2.2km，看完珠江夜景可步行前往；12:00 至次日 06:00 营业",
        "closed_days": [],
    },
    {
        "id": "GZ079",
        "name": "CxPARTY KTV（太古仓店）",
        "category": "nightlife",
        "tags": ["KTV", "太古仓", "派对", "夜生活"],
        "lat": 23.091130,
        "lng": 113.257172,
        "duration_h": 2.0,
        "open": "10:00",
        "close": "06:00",
        "best_time": "evening",
        "price": 56,
        "rating": 4,
        "area": "south",
        "family_ok": False,
        "note": "海珠工业大道北富力家信商业中心三层，与太古仓码头（0.56km）同一夜生活片区，人均约 56 元；傍晚在太古仓看日落、喝精酿后可步行续摊；10:00 至次日 06:00 营业",
        "closed_days": [],
    },
]

# ---------- 探测原文件缩进 ----------
raw = open(PATH, encoding="utf-8").read()
indent = None
for cand in (None, 0, 1, 2, 4):
    if json.dumps(json.loads(raw), ensure_ascii=False, indent=cand).strip() == raw.strip():
        indent = cand
        break
print(f"原文件格式：indent={indent}  ({len(raw)} chars)")
assert indent is not None, "无法探测原文件缩进，拒绝写回"

data = json.loads(raw)
pois = data["pois"]
print(f"现有 POI：{len(pois)} 个，末位 id={pois[-1]['id']} {pois[-1]['name']}")

# ---------- 断言：id 唯一 ----------
ids = {p["id"] for p in pois}
for n in NEW:
    assert n["id"] not in ids, f"id 冲突：{n['id']}"


def norm(s: str) -> str:
    return re.sub(r"[\s（）()·\-—・]+", "", s).lower()


# ---------- 断言：名称级重复（上批 NJ039 漏检根因＝只查 id）----------
existing_norm = [(norm(p["name"]), p["id"], p["name"]) for p in pois]
for n in NEW:
    nn = norm(n["name"])
    # 品牌词（去掉分店后缀）也要查：如「纯K（岗顶店）」→「纯k」
    brand = re.sub(r"（.*?）", "", n["name"]).strip()
    bn = norm(brand)
    for en, eid, ename in existing_norm:
        assert nn != en, f"名称完全重复：{n['name']} vs {eid} {ename}"
        # 互为子串且长度接近 → 疑似同一地点
        if nn in en or en in nn:
            assert min(len(nn), len(en)) / max(len(nn), len(en)) < 0.6, \
                f"名称疑似重复（子串重叠）：{n['name']} vs {eid} {ename}"
        if bn and len(bn) >= 2 and (bn in en or en in bn):
            print(f"  ⚠️ 品牌词重叠提示：{n['name']} ↔ {eid} {ename}")
print("✅ 名称级重复断言通过")

# ---------- 追加 ----------
data["pois"] = pois + NEW
open(PATH, "w", encoding="utf-8").write(
    json.dumps(data, ensure_ascii=False, indent=indent))
print(f"✅ 已写入 {len(NEW)} 个点 → 共 {len(data['pois'])} 个")

# ---------- 回读校验 ----------
chk = json.load(open(PATH, encoding="utf-8"))
assert len(chk["pois"]) == len(pois) + len(NEW)
assert [p["id"] for p in chk["pois"][-4:]] == ["GZ076", "GZ077", "GZ078", "GZ079"]
raw2 = open(PATH, encoding="utf-8").read()
assert json.dumps(chk, ensure_ascii=False, indent=indent).strip() == raw2.strip(), "写回格式漂移"
print("✅ 回读校验通过（格式与内容一致）")
for p in chk["pois"][-4:]:
    print(f"   {p['id']} {p['name']} | {p['category']} | {p['open']}-{p['close']} | "
          f"¥{p['price']} | rating {p['rating']} | area {p['area']} | family_ok {p['family_ok']}")
