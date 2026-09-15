# -*- coding: utf-8 -*-
"""POI 库加载与地理计算。"""
import datetime
import json, math, os, re

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def trip_weekday(date_str: str, day_no: int) -> str:
    """行程第 day_no 天（从 date_str 起算）的星期名，如 trip_weekday('2026-09-14', 1)='周一'。"""
    d = datetime.date.fromisoformat(date_str)
    return WEEKDAY_NAMES[(d + datetime.timedelta(days=day_no - 1)).weekday()]

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# 市内混合交通（地铁+打车）有效速度假设：直线 ×1.4 绕路系数 / 18 km/h
CIRCUITY = 1.4
SPEED_KMH = 18.0
MIN_TRAVEL_H = 0.25
# 出行方式差异化速度模型（M8）：同一张路网/距离，按主题换速度与标签
CYCLE_KMH = 12.0          # 骑行有效速度（含等灯/避让）
WALK_KMH = 4.5            # 步行有效速度
HOP_WALK_KM = 1.2         # 通行段 <1.2km 视为步行（各模式共用阈值）
CYCLE_MAX_KM = 8.0        # 骑行模式下超过 8km 的腿改按车程（8km 内约 55min 可骑；
                          # 6km 会在古城尺度误伤——虎丘→双塔 6.2km 被标车程而文案说骑行）
WALK_MODE_MAX_KM = 3.0    # 徒步模式下步行上限（超过仍按车驾）
MIN_SLOW_TRAVEL_H = 5/60  # 步行/骑行的单程下限：不被 15min 车程下限吞掉短腿差异


def load_city(name: str = "杭州") -> dict:
    path = os.path.join(_DATA_DIR, f"{name}_pois.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---- 需求文本里的「点名点位」识别（2026-09-15 远郊/点名召回缺陷）----
# 「用户点名就必须排」是硬需求，不能赌 LLM 是否采纳：实测 query「广州2天，晚上想去
# 宝业路宵夜街吃宵夜」→ LLM 提案 13 个点里没有它，而落地率 100% 说明库内完全可落地，
# 于是这点在 TOPTW 池里从未出现（「能排但从不被选」）。
# 识别口径：规范化后 POI 名、或其去括号主干作为子串出现在规范化需求文本中，且长度 ≥3
# （外滩/西湖这类 2 字名不判，避免误命中）；与落地匹配共用 norm_name，防两套词表漂移。
NAME_MENTION_MIN = 3


def norm_name(s: str) -> str:
    """POI 名 / 任意文本规范化（去空白与常见分隔符、统一小写）。

    「落地匹配」与「点名识别」共用此口径——两套归一化必然漂移（本项目老坑）。
    """
    return re.sub(r"[\s·・（）()\-—_、，。…「」『』]", "", (s or "")).lower()


def core_name(s: str) -> str:
    """POI 名主干：去掉括号补充说明（「广州塔（小蛮腰）」→「广州塔」）。"""
    return re.sub(r"[（(][^（()）]*[)）]", "", s or "").strip()


def names_mentioned_in(text: str, pois) -> list:
    """识别需求文字里被点名的库内点位（确定性、无 LLM）。返回按传入顺序去重的 POI 列表。"""
    key = norm_name(text)
    if not key:
        return []
    hits, seen = [], set()
    for p in pois:
        pid = p.get("id")
        if pid in seen:
            continue
        nm = p.get("name") or ""
        for cand in {norm_name(nm), norm_name(core_name(nm))}:
            if len(cand) >= NAME_MENTION_MIN and cand in key:
                seen.add(pid)
                hits.append(p)
                break
    return hits


# ---- 湖线点判定（贯穿性湖偏好需求专用，如「湖边骑行」「最好临湖」）----
# 名字含湖岸词缀（湖/岛/湾/堤/码头/滨/岸/洲/渚）或 view 类目（观景台/山顶看湖）。
# 误伤面核过苏州 75 点：命中全部为湖线点（金鸡湖东方之门/独墅湖教堂/西山岛/
# 李公堤/月光码头/岱心湾/冲山岛…），无市区误报。
_LAKE_NAME_RE = re.compile(r"湖|岛|湾|堤|码头|滨|岸|洲|渚")


def is_lake_poi(p: dict) -> bool:
    return bool(_LAKE_NAME_RE.search(p.get("name", ""))) or p.get("category") == "view"


NIGHT_OPEN_H = 16.5  # 开门晚于该时刻=「只有夜间才可入」的点（原 m2_planner.LATE_OPEN_H 同值，2026-09-15 判定职责统一收编到 poi_db）


def is_night_only(p: dict) -> bool:
    """真夜间点：只有晚上才能/才适合去的点（酒吧夜市/夜间演出/17 点后才开门的场馆）。

    best_time=evening 但全天开放的点（外滩 0:00-23:59 / 南京路步行街 / 滨江步道类）
    不算——它们排白天毫无障碍（sequencer 排时只看 open/close，evening 只影响
    美食选窗与 food 排序权重）。慢节奏档过滤若按 best_time 一刀切会把这类点
    误杀导致天薄（2026-09-14 报障 11：上海亲子慢节奏 Day2/Day3 被 15:21/14:45 收工）。
    """
    if p.get("category") == "nightlife":
        return True
    return (p.get("open_h") or 0) >= NIGHT_OPEN_H


def is_family_ok(p: dict) -> bool:
    """亲子适配判定：字段显式为 false 才算「不适合带小孩去」。

    全库仅 7 个点标了 false（KTV/酒吧街区/大屠杀纪念馆/高强度徒步），
    字段缺省一律视为适合。选点链（主选/备选池/补位/补强）在亲子 query 下
    统一用本判据过滤——此前该字段只有 offline_planner 读，主链路无人拦。
    """
    return p.get("family_ok", True) is not False


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_travel_cache = None


def _load_travel_cache():
    """L1 交通矩阵（OSRM 真实路网，scripts/build_travel_cache.py 构建）。"""
    global _travel_cache
    if _travel_cache is None:
        path = os.path.join(_DATA_DIR, "travel_cache.json")
        _travel_cache = json.load(open(path, encoding="utf-8"))["minutes"] \
            if os.path.exists(path) else {}
    return _travel_cache


def travel_hours(p1: dict, p2: dict, mode: str | None = None) -> float:
    """两 POI 间通行时间（小时）。L1 缓存优先（OSRM 路网），L3 直线兜底。

    mode：出行方式（None=车驾混合 | "cycling"=骑行 | "hiking"=徒步）。
    骑行：<1.2km 按步行，1.2~8km 按骑行速度（路网距离 = 直线×绕路系数），
    >8km 骑不现实 → 回退车驾口径；徒步：<3km 按步行，超过仍按车驾。
    距离口径与既有 L3 模型一致，不引入新缓存。
    """
    km = haversine_km(p1["lat"], p1["lng"], p2["lat"], p2["lng"])
    if mode == "cycling":
        if km < HOP_WALK_KM:
            return max(MIN_SLOW_TRAVEL_H, km / WALK_KMH)
        if km <= CYCLE_MAX_KM:
            return max(MIN_SLOW_TRAVEL_H, km * CIRCUITY / CYCLE_KMH)
    elif mode == "hiking":
        if km < WALK_MODE_MAX_KM:
            return max(MIN_SLOW_TRAVEL_H, km * CIRCUITY / WALK_KMH)
    if p1.get("id") and p2.get("id"):
        m = _load_travel_cache().get(p1["id"], {}).get(p2["id"])
        if m is not None:
            return max(MIN_TRAVEL_H, m / 60.0)
    return max(MIN_TRAVEL_H, km * CIRCUITY / SPEED_KMH)


def hhmm_to_h(s: str) -> float:
    h, m = s.split(":")
    return int(h) + int(m) / 60.0


def parse_poi(p: dict, city: dict) -> dict:
    """给 POI 补充数值化字段。

    跨零点营业归一化：闭店时刻「不晚于」开门时刻（18:00-02:00 宵夜街、10:00-06:00
    KTV、00:00-00:00 全天点）在时钟上是跨过午夜的次日时刻，+24 归一到绝对小时。
    不做这一步则 close_h 小于 open_h，点位会被三重判死：
      1. toptw 预剔除 to_min(close)-dur < 0（-420-90 < 0）；
      2. toptw 时间窗 lo = to_min(open) > hi = to_min(close)-dur（540 > -510）；
      3. sequencer 任一到达时刻都满足 t + dur > close_h → 恒报「超出营业时间」。
    即该 POI 永远排不进任何行程（2026-09-15 实测 GZ069 宝业路宵夜街 / NJ021 1912
    街区两个 nightlife 点自入库起从未落地过）。归一化后 02:00 → 26.0，与 day_end
    上限共同决定它只能排在当天收尾时段，符合真实语义。
    """
    q = dict(p)
    q["open_h"] = hhmm_to_h(p["open"])
    close_h = hhmm_to_h(p["close"])
    if close_h <= q["open_h"]:
        close_h += 24.0
    q["close_h"] = close_h
    q["dur"] = float(p["duration_h"])
    q["dist_center_km"] = haversine_km(p["lat"], p["lng"], city["center"]["lat"], city["center"]["lng"])
    return q
