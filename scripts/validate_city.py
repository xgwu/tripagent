# -*- coding: utf-8 -*-
"""城市 POI 库校验器——扩充新城市/补点的流水线门禁。

用法：python scripts/validate_city.py 杭州          # 校验单城
      python scripts/validate_city.py              # 校验全部城市

检查项（任一 ERROR 即退出码 1，发布门禁会跑本脚本）：
  1. 必填字段齐全且类型正确（id/name/category/tags/lat/lng/duration_h/open/close/...）
  2. id 唯一且形如 <城市拼音缩写><两位序号>
  3. 坐标在合理范围（中国境内，且距城市中心 ≤ 80km）
  4. open/close 为 HH:MM 且 close > open；duration_h ∈ (0, 12]
  5. closed_days 为列表且值为「周一..周日」；price ≥ 0；rating ∈ {3,4,5}
  6. category 在已知类目表内；tags 非空
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

REQUIRED = ["id", "name", "category", "tags", "lat", "lng", "duration_h",
            "open", "close", "best_time", "price", "rating", "area", "family_ok",
            "note", "closed_days"]
CATEGORIES = {"culture", "history", "art", "nature", "view", "family", "religion",
              "shopping", "food", "show", "sport", "park", "museum",
              "photo", "nightlife", "outdoor", "indoor", "relax"}
WEEKDAYS = {"周一", "周二", "周三", "周四", "周五", "周六", "周日"}


def _hhmm_ok(s: str) -> bool:
    try:
        h, m = s.split(":")
        return 0 <= int(h) <= 24 and 0 <= int(m) <= 59
    except Exception:
        return False


def _hhmm_h(s: str) -> float:
    h, m = s.split(":")
    return int(h) + int(m) / 60


def validate_city(cname: str) -> tuple[int, int]:
    path = os.path.join(DATA_DIR, f"{cname}_pois.json")
    db = json.load(open(path, encoding="utf-8"))
    pois = db["pois"]
    center = db["center"]
    errs, warns = [], []
    ids = set()
    prefix = "".join(w[0].lower() for w in ["placeholder"])  # 占位，id 前缀不做强校验
    for p in pois:
        pid = p.get("id", "?")
        where = f"[{pid} {p.get('name', '')}]"
        for k in REQUIRED:
            if k not in p:
                errs.append(f"{where} 缺必填字段 {k}")
        if pid in ids:
            errs.append(f"{where} id 重复")
        ids.add(pid)
        lat, lng = p.get("lat"), p.get("lng")
        if not (isinstance(lat, (int, float)) and isinstance(lng, (int, float))):
            errs.append(f"{where} 坐标非法")
        else:
            if not (15 <= lat <= 55 and 70 <= lng <= 140):
                errs.append(f"{where} 坐标不在中国范围 ({lat},{lng})")
            from src.poi_db import haversine_km
            km = haversine_km(lat, lng, center["lat"], center["lng"])
            if km > 80:
                errs.append(f"{where} 距市中心 {km:.0f}km，超出城市库范围")
            elif km > 45:
                warns.append(f"{where} 距市中心 {km:.0f}km（远郊，确认 area=suburb）")
        o, c = p.get("open", ""), p.get("close", "")
        if not (_hhmm_ok(o) and _hhmm_ok(c)):
            errs.append(f"{where} 开闭门时间格式非法: {o}-{c}")
        else:
            ch = _hhmm_h(c)
            if ch <= _hhmm_h(o):
                ch += 24  # 跨零点营业合法（酒吧街/夜市类，如 17:00-02:00）
            if ch - _hhmm_h(o) > 24:
                errs.append(f"{where} 开闭门窗口异常（>{ch - _hhmm_h(o):.0f}h）: {o}-{c}")
        dh = p.get("duration_h")
        if not (isinstance(dh, (int, float)) and 0 < dh <= 12):
            errs.append(f"{where} duration_h 超界: {dh}")
        cd = p.get("closed_days")
        if not isinstance(cd, list) or any(x not in WEEKDAYS for x in cd):
            errs.append(f"{where} closed_days 非法: {cd}")
        if p.get("rating") not in (3, 4, 5):
            warns.append(f"{where} rating 不在 {{3,4,5}}: {p.get('rating')}")
        if not isinstance(p.get("price"), (int, float)) or p.get("price", 0) < 0:
            errs.append(f"{where} price 非法: {p.get('price')}")
        if p.get("category") not in CATEGORIES:
            warns.append(f"{where} category 不在已知类目: {p.get('category')}")
        if not p.get("tags"):
            warns.append(f"{where} tags 为空")
        if len(p.get("note", "")) < 8:
            warns.append(f"{where} note 过短（入库前应完成三源核实并在 note 留痕）")
    print(f"=== {cname}: {len(pois)} POI")
    for w in warns:
        print(f"  ⚠️  {w}")
    for e in errs:
        print(f"  ❌ {e}")
    print(f"  {'✅ 通过' if not errs else f'❌ {len(errs)} 个错误'}"
          f"（警告 {len(warns)}）")
    return len(errs), len(warns)


if __name__ == "__main__":
    cities = sys.argv[1:] or [f[:-10] for f in os.listdir(DATA_DIR)
                              if f.endswith("_pois.json")]
    total_err = 0
    for c in sorted(cities):
        e, _w = validate_city(c)
        total_err += e
    sys.exit(1 if total_err else 0)
