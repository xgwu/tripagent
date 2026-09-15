# -*- coding: utf-8 -*-
"""贪婪排序器 + 硬约束校验/修复 —— M1 的「轻量优化层」（非 TOPTW）。

输入：LLM（或基线）给出的每日 POI 集合
输出：按贪婪最近邻 + 建议时段重排后的时间轴，附约束校验报告
"""
import re

from . import poi_db

DAY_END_SLOT = "21:30"
MAX_MEAL_WAIT_H = 1.5     # 美食 POI 早到餐窗的最多等待时长，超过则判违规交修复链剔除
FOOD_PREF_WIN = {"lunch": "lunch", "dinner": "dinner", "evening": "dinner"}  # best_time → 首选餐窗
FULL_DAY_H = 8.0          # 时长 ≥8h 视为「全天大点」（迪士尼/海昌类）：须独占一天，豁免里程预算剔除
MEAL_LEAD_H = 0.5         # 通用（不限慢节奏）：到达距饭点 ≤30min 先用餐再游览——
                          # 堵「到达差几分钟不插、游完过窗尾不补」的窗口缝隙
                          # （2026-09-14 报障 10：报障 7 只给慢节奏加了 LEAD，
                          # 普通亲子档 11:45 到 2h 游览点 + 20min 车程 → 13:05 过窗尾，午餐消失）
MEAL_EXIT_GRACE_H = 0.75  # 游完出来/收尾补餐的窗尾宽限（报障 12）：2~4h 游览横跨餐窗
                          # 时到达太早（LEAD 不触发）、出来已过窗尾几十分钟 → 午餐被
                          # 「途中解决」吞掉（2026-09-15 上博 10:15-13:15 案例）。刚出
                          # 景点 ≤45min 内补块「晚午餐」现实合理，与 MAX_MEAL_WAIT_H 对称
MEAL_END_LEAD_H = 1.5     # 收尾场景（末点游完）晚餐提前量：17:15 收工离 18:00 开餐 45min
                          # < MEAL_LEAD_H 线 → 晚餐块整个不出现。与迟到午餐守门同 1.5h 口径
                          # （16:30 前收工仍留白，不破坏傍晚留白设计）
MEAL_EARLY_TOL_H = 1.0    # 餐窗提前容差（2026-09-15 日内空档治理）：正餐点可早于餐窗起点
                          # 最多 1h 开吃（午餐 11:00 起、晚餐 17:00 起），不必干等到
                          # 12:00/18:00 整点。旧写法 start=max(t2, ws, open_h) 把餐厅钉死在
                          # 整点：10:45 到达的天白等 75min——实测这正是日内空档的唯一成因
                          # （南京科举博物馆→绿柳居 75min、北京故宫→四季民福 75min、
                          # 苏州琵琶语→朱鸿兴 87min、武汉长江大桥→户部巷 45min）。
GAP_NOTICE_MIN = 60       # 日内空档披露阈值（分钟）：≥该值的空白记入 gaps 并提示用户

# 慢节奏（老人/轮椅/不累/慢节奏/悠闲/宽松）：全链路宽松化口径。
# 定义在 sequencer（最底层），m2_planner/proposal_planner 从此 re-export，
# 避免 planner → sequencer 单向依赖被打破成环。
# 「太累」覆盖 不要太累/别太累/勿太累/太累了 等高频变体（2026-09-14 报障 9：
# 旧正则只有「不累/勿太累」，「不要太累」不含「不累」子串 → 全链路慢节奏旁路，
# 提案 10+ 点/天 TOPTW 塞满到 20:30）。
SLOW_PACE_RE = re.compile(r"老人|轮椅|行动不便|腿脚不便|慢节奏|悠闲|不累|太累|宽松")

# 亲子出行：全链路「不适合儿童的点」过滤口径。同样定义在最底层 sequencer，
# 由 m2_planner re-export（proposal_planner 亦经其引用），避免依赖成环。
# 词表与 retrieval.py / baseline.py 的 family 映射对齐（亲子/孩子/娃/儿童/小朋友/5岁）。
# 注意「小朋友」：检索层认它、但原 proposal_planner 的正则漏了——判据与词表不一致
# 会导致「检索按亲子召回、选点层却不当亲子过滤」。这里按语义直接列词（「娃」「孩子」
# 这类词在中文行程语料里几乎只指小孩），不再依赖「带 X」句式。
FAMILY_RE = re.compile(r"亲子|遛娃|娃|孩子|小孩|儿童|小朋友|宝宝|幼儿")


def is_family_query(query: str | None) -> bool:
    """亲子出行判定（亲子/遛娃/带娃/带孩子/带小孩/儿童/小朋友/宝宝/幼儿）。"""
    return bool(query and FAMILY_RE.search(query))


def is_full_day(p: dict) -> bool:
    """全天大点判定：duration_h ≥ 8（远郊主题乐园等，单程即接近/突破每日里程预算）。"""
    try:
        return float(p.get("duration_h") or 0) >= FULL_DAY_H
    except (TypeError, ValueError):
        return False

# ---- 主题画像：按出行方式设定每日交通里程预算（km），游玩≠拉练，超预算剔除远点 ----
# 优先级从上到下（一个查询命中多个主题时取最严格匹配项之前先按此序）
# 注：family（亲子）不设里程上限——远郊大点与片区错配由提案层 _far_big_point_regroup 守门，
#     守门层剔点会误伤亲子行程的必要远点（如极地海洋公园/野生动物世界）
THEME_PROFILES = [
    ("cycling", re.compile(r"骑行|骑车|单车|自行车|cycling|bike", re.IGNORECASE), 15.0),
    ("hiking", re.compile(r"徒步|暴走|city\s*walk|遛弯", re.IGNORECASE), 8.0),
]


def detect_theme(query: str | None) -> str | None:
    """从需求文字识别出行主题（cycling/hiking），无匹配返回 None。"""
    if not query:
        return None
    for name, pat, _cap in THEME_PROFILES:
        if pat.search(query):
            return name
    return None


def theme_km_cap(query: str | None) -> float | None:
    """需求命中主题画像 → 返回每日里程预算（km）；否则 None。"""
    if not query:
        return None
    for _name, pat, cap in THEME_PROFILES:
        if pat.search(query):
            return cap
    return None


def cycle_km_cap(query: str | None) -> float | None:
    """兼容保留：等价于 theme_km_cap（骑行命中即 15km，否则 None）。"""
    return 15.0 if query and _CYCLE_RE.search(query) else None


def travel_mode(query: str | None) -> str | None:
    """主题 → 通行时间模型（poi_db.travel_hours 的 mode 参数）。

    cycling/hiking 有专属速度模型（骑行/步行）；family 等仍按车驾混合口径。
    """
    theme = detect_theme(query)
    return theme if theme in ("cycling", "hiking") else None


_CYCLE_RE = re.compile(r"骑行|骑车|单车|自行车|cycling|bike", re.IGNORECASE)

# ---- 单日主题：「其中一天亲近自然骑行」类表述 ----
# 主题关键词与「一天」同句出现 → 主题只作用于承载天，其余天保持默认车驾口径。
# 否则整单骑行口径会把非主题日的 1.2~6km 接驳也标成「骑行」（苏州 3 天案例）。
_DAY_SCOPED_RE = re.compile(
    r"一[天日][^，。;；！!？?]{0,8}(骑行|骑车|单车|自行车|徒步|暴走|city\s*walk)",
    re.IGNORECASE)

# 自然锚点特征：tag 命中或名称含自然地理词 —— 单日主题承载天按命中数打分挑选
_NATURE_TAGS = {"自然", "公园", "湿地", "湖泊", "户外", "郊野", "森林", "骑行", "徒步"}
_NATURE_NAME_RE = re.compile(r"公园|湿地|湖|山|岛|森林|绿道|郊野")


def theme_scoped(query: str | None) -> bool:
    """主题是否为「单日主题」（如“其中一天骑行”）。无主题时恒为 False。"""
    if not query or not detect_theme(query):
        return False
    return bool(_DAY_SCOPED_RE.search(query))


def scoped_theme_day(day_map: dict, all_pois: dict, query: str | None) -> int | None:
    """单日主题的承载天号：按自然锚点得分选天（并列取更小天号）。

    打分：tag 强信号（自然/公园/户外…）每个计 2 分，名称弱信号（公园/湿地/湖…）
    计 1 分——城市湖滨/山名景点（金鸡湖、虎丘）只算弱信号，避免误抢骑行日。
    无主题 / 非单日主题 → None（主题全局生效，维持原口径）；
    有主题但行程中无任何自然信号 → None（无处承载，不伪造骑行日）。
    """
    if not theme_scoped(query):
        return None
    best, best_s = None, 0
    for d in sorted(day_map):
        pois = [all_pois[i] for i in day_map.get(d, []) if i in all_pois]
        s = sum(2 * len(set(p.get("tags") or []) & _NATURE_TAGS)
                + (1 if _NATURE_NAME_RE.search(p.get("name", "")) else 0)
                for p in pois)
        if s > best_s:
            best, best_s = d, s
    return best


# 咖啡/茶饮（饮品店）识别信号。
# ⚠️ 不得用裸「茶」做子串：粤式酒楼的老字号标签是「早茶」「茶点」「茶楼」，
# 裸「茶」会把陶陶居/广州酒家/点都德/莲香楼这类**正餐**误判为咖啡馆。
# 误判后果（2026-09-15 报障17 实测）：美食分支 `not is_cafe(p)` 整段跳过 →
# ① 该 POI 无 meal 标记（前端不显示 🍽，用户分不清哪个是用餐点）
# ② 通用餐块照常插入 → 同一天出现「餐厅 + 午餐块 + 餐厅 + 晚餐块」= 两顿午饭
_CAFE_NAME_RE = re.compile(
    r"咖啡|茶饮|奶茶|果茶|coffee|cafe|星巴克|瑞幸|luckin|喜茶|奈雪|茶颜|蜜雪",
    re.IGNORECASE)
_CAFE_TAG_SUBSTR = ("咖啡", "coffee", "cafe", "茶饮", "奶茶", "果茶")


def is_cafe(p: dict) -> bool:
    """咖啡馆/茶饮类：food 类目但非正餐——不参与餐窗竞争，不顶替正餐餐块。

    判据只认**饮品店**强信号（咖啡/茶饮/奶茶/果茶/连锁茶饮品牌）；粤式「早茶」
    「茶点」「茶楼」属正餐标签，必须判为 False（否则正餐会被踢出餐窗竞争）。
    """
    if p.get("category") != "food":
        return False
    if _CAFE_NAME_RE.search(p.get("name") or ""):
        return True
    return any(any(s in t.lower() for s in _CAFE_TAG_SUBSTR)
               for t in (p.get("tags") or []))


def _build_timeline(pois: list, city: dict, day_no: int, weekday: str | None = None,
                    hotel: dict | None = None, mode: str | None = None,
                    slow: bool = False) -> dict:
    """hotel：M6 住宿锚点 —— 每日从酒店出发、day_end 前返回酒店（虚拟节点，dur=0）。

    mode：出行方式（None=车驾 | cycling/hiking）—— 通行时间与 hop 标签随之切换。
    美食约束：category=food 的正餐类 POI 只能安排在用餐时段内（开吃时刻落在餐窗），
    且每个餐窗最多 1 家正餐（占用后该窗不再插入普通餐块）；排不进 → 违规跳过。
    咖啡馆/茶饮（is_cafe）不算正餐：按普通景点排（时段自由），餐块照插——
    午餐时段永远是正餐，咖啡只作为逛点间隙的休憩。
    """
    meals = city["meal_slots"]
    day_start = poi_db.hhmm_to_h(city["day_start"])
    day_end = poi_db.hhmm_to_h(city["day_end"])
    t = day_start
    timeline, travel_km, travel_h = [], 0.0, 0.0
    violations, repairs = [], 0
    prev = hotel  # 无酒店锚点时 prev=None，行为与 M5 完全一致
    meal_keys = {"lunch": poi_db.hhmm_to_h(meals["lunch"][0]),
                 "dinner": poi_db.hhmm_to_h(meals["dinner"][0])}
    meal_windows = {k: (poi_db.hhmm_to_h(meals[k][0]), poi_db.hhmm_to_h(meals[k][1]))
                    for k in meal_keys}
    used_meals = set()
    # 正餐优先权（报障 15）：本日尚未处理的正餐点各自认领一个餐窗，通用餐块在这些窗位
    # 让位——否则「游完就地补餐/收尾晚餐」会以更早时刻抢窗（11:30 就占掉午餐），正餐点
    # 到场只剩另一窗、等到超 MAX_MEAL_WAIT_H → 判违规被剔，前端提示「美食POI未能安排进
    # 用餐时段」。正餐点处理时释放认领；它最终排不进时，违规兜底与收尾补餐仍会接管，
    # 饭不会没有。
    _food_win = _assign_food_windows(
        [q for q in pois if q.get("category") == "food" and not is_cafe(q)], city)
    pending_food_win = {k: 0 for k in meal_keys}
    for _w in _food_win.values():
        pending_food_win[_w] = pending_food_win.get(_w, 0) + 1

    def _fill_wait_with_meal(open_t: float):
        """把尚未安排的餐块放进「等待开门」的空白里。

        等待开门是硬窗口造成的、无法压缩的空白——但至少该吃饭：旧实现里这段空白
        既没有安排也没有餐块（2026-09-15 空档治理实测：花城广场 16:45 出来 → 珠江夜游
        19:00 开门，2h15 空白 + 晚餐被判「途中已解决」，双输）。餐块优先对齐餐窗起点，
        对不齐就紧贴开门时刻；放不下 1h 餐块则不插（交给后续 LEAD/收尾补餐兜底）。
        """
        nonlocal t
        for key in ("lunch", "dinner"):
            if key in used_meals or pending_food_win.get(key, 0) > 0:
                continue
            ws, _we = meal_windows[key]
            if open_t < ws - MEAL_LEAD_H:
                continue                      # 走到该点时这餐还没到点，别提前吃
            if ws > t and ws + 1.0 <= open_t + 1e-9:
                start = ws                    # 对齐餐窗起点（窗内吃完再进点）
            else:
                start = max(t, open_t - 1.0, ws - MEAL_EARLY_TOL_H)
            if start + 1.0 > open_t + 1e-9:
                continue                      # 餐块放不进这段等待
            timeline.append({"type": "meal",
                             "name": "午餐" if key == "lunch" else "晚餐",
                             "start": _fmt(start), "end": _fmt(start + 1.0)})
            used_meals.add(key)
            t = start + 1.0

    def _insert_generic_meals(cur_t, late_grace_h: float = 0.0, lead_h: float | None = None):
        """普通餐块：到达时刻已跨过饭点（含 ≤30min 提前量）且该餐未被占用 → 插入。

        LEAD 通用化（2026-09-14 报障 10）：到达时刻距饭点 ≤30min 也先吃再逛，
        不再区分慢节奏——普通口径下 11:45 到达下一个 2h 游览点会「先逛（横跨
        12:00-13:00 餐窗）→ 出来+车程 13:05 已过窗尾」，全天无午餐。慢节奏 0.5h
        先行验证过效果，普通档同样受此缝隙影响（报障 7 修复只覆盖了慢节奏）。
        已过餐窗尾（we）→ 视为该餐已在途中/游览中解决，标记占用不再重试。

        late_grace_h（2026-09-15 报障 12）：窗尾宽限——游完景点刚出来 ≤45min 时
        补「晚午餐/晚餐」现实合理（2~4h 游览横跨餐窗：到达太早 LEAD 不触发、
        出来刚过窗尾，两不沾）。lead_h：饭点提前量覆盖，收尾场景放宽到 1.5h。
        """
        nonlocal t
        lead = MEAL_LEAD_H if lead_h is None else lead_h
        for key, mstart in meal_keys.items():
            # 该窗已被尚未处理的正餐点认领 → 通用餐块让位（报障 15）
            if key in used_meals or pending_food_win.get(key, 0) > 0:
                continue
            if cur_t >= mstart - lead:
                # 迟到午餐守门：距晚餐窗开始不足 1.5h 不再补午餐（背靠背两餐不合常理），
                # 视为该餐已在途中/游览中解决，只保留晚餐
                if key == "lunch" and "dinner" not in used_meals \
                        and cur_t >= meal_keys["dinner"] - 1.5:
                    used_meals.add("lunch")
                    continue
                if cur_t > meal_windows[key][1] + late_grace_h + 1e-9:
                    used_meals.add(key)  # 已过窗尾（含宽限）：这餐只能算途中解决了
                    continue
                timeline.append({"type": "meal", "name": "午餐" if key == "lunch" else "晚餐",
                                 "start": _fmt(t), "end": _fmt(t + 1.0)})
                t += 1.0
                used_meals.add(key)

    for p in pois:
        is_last = p is pois[-1]
        if p.get("category") == "food" and not is_cafe(p):
            # ---- 美食 POI：必须落入未占用的餐窗 ----
            # 释放本点认领的餐窗（此后不再需要通用餐块让位）；首选窗与 _insert_foods
            # 的落位目标同源（_assign_food_windows），避免两处口径漂移
            _pw = _food_win.get(p["id"])
            if _pw and pending_food_win.get(_pw, 0) > 0:
                pending_food_win[_pw] -= 1
            th = poi_db.travel_hours(prev, p, mode) if prev is not None else 0.0
            t2 = t + th
            pref = _pw or FOOD_PREF_WIN.get(p.get("best_time"), "lunch")
            win_order = [pref] + [k for k in meal_keys if k != pref]
            for key in win_order:
                ws, we = meal_windows[key]
                if key in used_meals:
                    continue
                start = max(t2, ws - MEAL_EARLY_TOL_H, p["open_h"])  # 早于开门则顺延到开门
                # 注：ws 用「餐窗起点 - 提前容差」而非窗起点本身——早到不必干等到整点，
                # 见 MEAL_EARLY_TOL_H 注释（该行是日内空档的唯一成因）
                if start > we + 1e-9:      # 已过该餐窗（开吃时刻晚于窗尾）
                    continue
                # 早到等待：最多等 MAX_MEAL_WAIT_H（防连锁推迟后续景点/留出大空档）。
                # 全天末点豁免已取消——13:15 到达末点餐厅等到 18:00 开餐会在时间轴
                # 上留 4.75h 空档（2026-09-14 老人 case：虎丘+双餐厅日）；「傍晚
                # 游完顺路吃晚餐」的合理场景等待 ≤1h，不受此限影响
                if start - t2 > MAX_MEAL_WAIT_H:
                    continue
                if prev is not None:
                    travel_h += th
                    km_f = poi_db.haversine_km(prev["lat"], prev["lng"], p["lat"], p["lng"])
                    travel_km += km_f
                    timeline.append({"type": "hop", "name": _hop_label(km_f, th, mode),
                                     "start": _fmt(t), "end": _fmt(t2), "min": int(round(th * 60)),
                                     "km": round(km_f, 1)})
                # 营业时间/闭馆日/当日上限校验（与非美食 POI 同一套）
                if weekday and weekday in p.get("closed_days", []):
                    violations.append({"poi": p["name"], "day": day_no,
                                       "reason": f'当日闭馆（{"、".join(p.get("closed_days", []))}）'})
                if start + p["dur"] > p["close_h"] + 1e-9:
                    violations.append({"poi": p["name"], "day": day_no,
                                       "reason": f'到达{_fmt(start)}+{p["dur"]}h超出营业时间({p["open"]}-{p["close"]})'})
                if start + p["dur"] > day_end:
                    violations.append({"poi": p["name"], "day": day_no,
                                       "reason": f'超出当日活动时间上限 {city["day_end"]}'})
                used_meals.add(key)  # 一个餐窗最多一个美食 POI，普通餐块也不再插
                timeline.append({"type": "poi", "id": p["id"], "name": p["name"],
                                 "start": _fmt(start), "end": _fmt(start + p["dur"]),
                                 "arrive": _fmt(t2), "meal": key})
                t = start + p["dur"]
                prev = p
                break
            else:
                violations.append({"poi": p["name"], "day": day_no,
                                   "reason": "美食POI未能安排进用餐时段（餐窗已被占用或早到等待超限）"})
                # 该吃的饭照吃，只是这家店去不了；窗尾宽限与「游完就地补餐」同口径
                # （刚过窗尾 ≤45min 补晚午餐/晚餐），避免「餐厅被剔 + 该餐无餐块」双输
                _insert_generic_meals(t, late_grace_h=MEAL_EXIT_GRACE_H)
            continue

        # ---- 非美食 POI：原逻辑 ----
        hop = None
        if prev is not None:
            th = poi_db.travel_hours(prev, p, mode)
            km = poi_db.haversine_km(prev["lat"], prev["lng"], p["lat"], p["lng"])
            travel_h += th
            travel_km += km
            hop = {"type": "hop", "name": _hop_label(km, th, mode),
                   "start": _fmt(t), "end": _fmt(t + th), "min": int(round(th * 60)),
                   "km": round(km, 1)}
            t += th
        arrive = t
        if hop:
            timeline.append(hop)  # 通行段信息行：先走完这段路，再谈吃饭/等待/游览
        # 餐块：若到达时刻已跨过饭点且该餐未安排，先吃再逛（通行途中用餐）
        _insert_generic_meals(t)
        if t < p["open_h"]:
            # 等待开门（硬窗口，无法压缩）：若有尚未安排的餐块且放得下，先在等待里吃饭
            _fill_wait_with_meal(p["open_h"])
            t = p["open_h"]
        # M5：日期感知 —— 闭馆日为硬约束（违规会被二级修复剔除，保证最终行程不踩闭馆）
        if weekday and weekday in p.get("closed_days", []):
            violations.append({"poi": p["name"], "day": day_no,
                               "reason": f'当日闭馆（{"、".join(p.get("closed_days", []))}）'})
        if t + p["dur"] > p["close_h"] + 1e-9:
            violations.append({"poi": p["name"], "day": day_no,
                               "reason": f'到达{_fmt(t)}+{p["dur"]}h超出营业时间({p["open"]}-{p["close"]})'})
        if t + p["dur"] > day_end:
            violations.append({"poi": p["name"], "day": day_no,
                               "reason": f'超出当日活动时间上限 {city["day_end"]}'})
        timeline.append({"type": "poi", "id": p["id"], "name": p["name"],
                         "start": _fmt(t), "end": _fmt(t + p["dur"]),
                         "arrive": _fmt(arrive)})
        # 游览覆盖餐窗：仅长游（≥4h，古镇/乐园类）游览时段内自然解决用餐，
        # 不在游览结束后补打「迟到午餐」类错位餐块。无门槛时 2h 博物馆恰好
        # 横跨餐窗也会吞掉餐块（2026-09-14 老人 case：苏博 11:45-13:45 吞午餐）。
        v_end = t + p["dur"]
        if v_end - t >= 4.0:
            for key, (ws, we) in meal_windows.items():
                if key not in used_meals and t <= ws + 1e-9 and v_end >= we - 1e-9:
                    used_meals.add(key)
        t = v_end
        # 游完就地补餐（报障 10）：游完恰在窗内就先吃饭再走——否则去下一景点的
        # 车程会把时刻推过窗尾（12:45 出来 + 20min 车程 = 13:05 > 13:00 窗尾），
        # 午餐被「途中解决」吞掉。≥4h 长游的覆盖标记在上方已处理，此处不会重复。
        # 窗尾宽限 45min（报障 12）：2~4h 游览横跨餐窗时到达太早 LEAD 不触发、
        # 出来刚过窗尾（13:25 上博出来，窗尾 13:00）——两不沾的盲区补「晚午餐」。
        _insert_generic_meals(t, late_grace_h=MEAL_EXIT_GRACE_H)
        prev = p
    # 收尾补餐：末点游览结束后已到饭点且餐窗未占用 → 补餐块再返程
    # （松鹤楼类餐点被求解器剔除后，之前全天可能一块餐窗都没有——2026-09-14 老人 case）
    # 窗尾宽限 + 晚餐提前量 1.5h（报障 12）：17:15 收工离 18:00 开餐 45min 也要有
    # 晚餐安排；16:30 前收工仍留白（不破坏傍晚留白设计，与守门口径对称）。
    if pois:
        _insert_generic_meals(t, late_grace_h=MEAL_EXIT_GRACE_H, lead_h=MEAL_END_LEAD_H)
    # M6：返程腿 —— day_end 前回到酒店（无酒店不约束）
    if hotel is not None and pois:
        th = poi_db.travel_hours(prev, hotel, mode)
        travel_h += th
        travel_km += poi_db.haversine_km(prev["lat"], prev["lng"], hotel["lat"], hotel["lng"])
        t += th
        # 住宿锚点展示名：去掉 resolve_hotel 附加的「（住宿锚点）」「（市中心附近）」括注
        _hname = re.sub(r"（[^）]*）\s*$", "", hotel.get("name") or "").strip() or "酒店"
        timeline.append({"type": "hotel", "name": f"返回{_hname}",
                         "start": _fmt(t - th), "end": _fmt(t)})
        if t > day_end + 1e-9:
            violations.append({"poi": "返程", "day": day_no,
                               "reason": f'返回酒店时刻{_fmt(t)}超出当日活动时间上限 {city["day_end"]}'})
        # 出发行（2026-09-15 报障 18）：此前「从酒店出发」只体现为时间轴首行的一个
        # 无源 hop（↳ 驾车 15 分钟），酒店名全程不出现——多城联游时前端更是完全看不到
        # 酒店（plan_multi 不返回 hotel 键，徽标/地图标记一起丢）。显式插入起点行，
        # 前端按 type=hotel 渲染 🏨，用户一眼看到今天从哪出发、回哪住。
        timeline.insert(0, {"type": "hotel", "name": _hname,
                            "start": _fmt(day_start), "end": _fmt(day_start)})
    _by_id = {p["id"]: p for p in pois}
    return {"timeline": timeline, "travel_km": travel_km, "travel_h": travel_h,
            "violations": violations, "repairs": repairs, "finish": _fmt(t),
            "gaps": scan_gaps(timeline, _by_id),
            "weekday": weekday}  # P1-4：周几随天透出，前端展示闭馆日语境


def scan_gaps(timeline: list, by_id: dict | None = None) -> list:
    """扫描日内空档：相邻两项活动之间、扣除通行时间后仍 ≥ GAP_NOTICE_MIN 的空白。

    空档不是「排错了」，而是时间轴的真实组成（下一站 17:00 才开门 / 餐点未到）；
    但它必须被显式记录与披露——否则前端只看到两块活动之间凭空少了 2.5h，像 bug
    （2026-09-15 空档治理：慢节奏靠移除夜间点规避，通用场景此前无人管）。
    成因分类（供文案与诊断）：
      wait_open 为衔接下一站开放时间（硬窗口，不可压缩）
      wait_meal 等待餐点/餐窗（餐点提前容差已压缩到 ≤1h）
      free      无窗口约束的自由活动/休整
    首项活动之前不计（从第一项活动起算），避免出发时刻噪声。
    """
    by_id = by_id or {}
    gaps = []
    prev_end, prev_name, pending = None, "", 0
    for e in timeline:
        if e.get("type") == "hop":
            pending += int(e.get("min") or 0)
            continue
        if e.get("type") not in ("poi", "meal", "hotel"):
            continue
        try:
            s, en = _hm_min(e["start"]), _hm_min(e["end"])
        except (KeyError, ValueError, TypeError):
            continue
        if prev_end is not None:
            idle = s - prev_end - pending
            if idle >= GAP_NOTICE_MIN:
                nxt = by_id.get(e.get("id")) if e.get("type") == "poi" else None
                if e.get("type") == "meal":
                    # 措辞覆盖两种子情形：餐块落在餐窗起点（真在等饭点）／餐块被提前塞到
                    # 晚开门点之前（此时段本就没有待游览点位）。用「无待游览点位」统一表述
                    reason, note = "wait_meal", "等待餐点（该时段无待游览点位）"
                elif nxt is not None and (nxt.get("open_h") or 0) * 60 > prev_end + pending:
                    reason, note = "wait_open", f"{nxt.get('name', '')} {nxt.get('open', '')} 开门"
                else:
                    reason, note = "free", "自由活动/返回休整"
                gaps.append({"start": _min_fmt(prev_end + pending), "end": e["start"],
                             "min": idle, "after": prev_name,
                             "before": e.get("name"), "reason": reason, "note": note})
        prev_end, prev_name, pending = en, e.get("name", ""), 0
    return gaps


def _hm_min(hm) -> int:
    """"HH:MM" → 自 00:00 起的分钟数（scan_gaps 内部按分钟运算）。"""
    h, m = str(hm).split(":")[:2]
    return int(h) * 60 + int(m)


def _min_fmt(minutes) -> str:
    """分钟数 → "HH:MM"（注意与 _fmt 区分：_fmt 收**小时 float**）。

    踩坑（2026-09-15）：scan_gaps 全程按分钟运算，却直接调 `_fmt(prev_end+pending)`
    → 16:00 的 960 分钟被当成 960 小时，披露文案显示 "960:00"。
    """
    m = int(minutes)
    return f"{m // 60:02d}:{m % 60:02d}"


def _fmt(h: float) -> str:
    return f"{int(h):02d}:{int(round((h - int(h)) * 60)):02d}"


def _hop_label(km: float, th: float, mode: str | None = None) -> str:
    """通行段标签（随出行方式切换）：
    默认：<1.2km 步行，否则车程；骑行：<1.2km 步行 / 1.2~8km 骑行 / >8km 车程；
    徒步：<3km 步行，否则车程。"""
    if mode == "cycling":
        mode_s = "步行" if km < poi_db.HOP_WALK_KM else \
            ("骑行" if km <= poi_db.CYCLE_MAX_KM else "车程")
    elif mode == "hiking":
        mode_s = "步行" if km < poi_db.WALK_MODE_MAX_KM else "车程"
    else:
        mode_s = "步行" if km < poi_db.HOP_WALK_KM else "车程"
    return f"{mode_s} {int(round(th * 60))} 分钟 · {km:.1f}km"


def _day_score_key(p: dict):
    pref = {"morning": 0, "any": 1, "afternoon": 2, "evening": 3}.get(p.get("best_time", "any"), 1)
    return pref


def _assign_food_windows(foods: list, city: dict) -> dict:
    """正餐点 → 餐窗 的确定性分配（午市偏好优先，同窗超额顺延到另一窗）。

    `_insert_foods` 的落位目标与 `_build_timeline` 里「通用餐块让位」共用此表——
    两边口径必须一致，否则又会回到「通用餐块先占窗、正餐点到场无窗可排」的
    优先级倒挂（报障 15：不管怎么排程，前端都提示美食 POI 未进用餐时段）。
    开门晚于窗尾的窗对该点不可用 → 不分配（避免为排不进的点空留餐窗）。
    """
    if not foods:
        return {}
    meals = city["meal_slots"]
    win_mid = {k: (poi_db.hhmm_to_h(meals[k][0]) + poi_db.hhmm_to_h(meals[k][1])) / 2
               for k in ("lunch", "dinner")}
    win_end = {k: poi_db.hhmm_to_h(meals[k][1]) for k in ("lunch", "dinner")}
    wins_left = list(win_mid)
    assigned = {}
    for f in sorted(foods, key=lambda p: (
            win_mid.get(FOOD_PREF_WIN.get(p.get("best_time"), "lunch"), 12.5), -p["rating"])):
        pref = FOOD_PREF_WIN.get(f.get("best_time"), "lunch")
        cands = ([pref] if pref in wins_left else []) + [k for k in wins_left if k != pref]
        open_h = float(f.get("open_h", 0) or 0)
        close_h = float(f.get("close_h", 24) or 24)
        # 可用 = 开门早于窗尾 **且** 关门晚于窗中点（后者保证真有重叠时段可坐下吃饭：
        # 只做午市的小店 07:00-14:00 若只看开门时刻会被派到 18:30 的晚餐窗，到场必然
        # 超 MAX_MEAL_WAIT_H 被剔 —— 2026-09-15 报障 15 残余构型）
        pick = next((k for k in cands
                     if open_h < win_end[k] - 1e-9 and close_h > win_mid[k] + 1e-9), None)
        if pick:
            wins_left.remove(pick)
            assigned[f["id"]] = pick
    return assigned


def food_window_plan(ids: list, all_pois: dict, city: dict,
                     mode: str | None = None) -> dict:
    """当天正餐点 → 餐窗可行性计划（主选对账 / 补位过滤共用）。

    返回 {"assign": {pid: "lunch"|"dinner"}, "infeasible": [pid], "reason": {pid: str}}。
    两类不可行：
      ① 营业时间与剩余餐窗不匹配（只做午市的小店被挤到晚餐窗）；
      ② 被派到晚餐窗，但当天行程撑不到晚餐时段——到得太早只能干等，超
         MAX_MEAL_WAIT_H 必被剔（典型：景点少、15:15 就收工的天还排晚餐餐厅）。
    判据刻意保守（预算 = Σdur + Σ腿时 + 1h 午餐块），宁可在主选层提前降级，
    也不让用户看到「美食 POI 未能安排进用餐时段」的提示（报障 15 残余）。
    """
    pts = [all_pois[i] for i in ids if i in all_pois]
    foods = [p for p in pts if p.get("category") == "food" and not is_cafe(p)]
    if not foods:
        return {"assign": {}, "infeasible": [], "reason": {}}
    assign = _assign_food_windows(foods, city)
    infeasible, reason = [], {}
    for f in foods:
        if f["id"] not in assign:
            infeasible.append(f["id"])
            reason[f["id"]] = "营业时间与当天剩余餐窗不匹配"
    dinner_food = [f for f in foods if assign.get(f["id"]) == "dinner"]
    if dinner_food:
        # 有效晚餐起点 = 餐窗起点 - 提前容差（与 _build_timeline / _insert_foods 同口径）
        dinner_start = (poi_db.hhmm_to_h(city["meal_slots"]["dinner"][0])
                        - MEAL_EARLY_TOL_H)
        day_start = poi_db.hhmm_to_h(city["day_start"])
        dinner_ids = {f["id"] for f in dinner_food}
        # 晚餐餐厅由 _insert_foods 放在序列末尾（最贴近晚餐窗）→ 到达时刻 ≈ 其余各点
        # 跑完的时刻。此处取「最早到达」下界（忽略等待与餐块）：连最早到达都已超过
        # 等待上限 ⇒ 必然被剔。反之（够晚）则留给 sequencer 精确时间轴判定。
        rest_pts = [all_pois[i] for i in ids if i in all_pois and i not in dinner_ids]
        arrive = day_start * 60 + sum(float(p.get("dur") or 0) * 60 for p in rest_pts)
        arrive += sum(poi_db.travel_hours(a, b, mode) * 60
                      for a, b in zip(rest_pts, rest_pts[1:]))
        if not any(assign.get(f["id"]) == "lunch" for f in foods):
            arrive += 60.0  # 当天无午餐餐厅 → 午间还有通用餐块占 1h
        if dinner_start * 60 - arrive > MAX_MEAL_WAIT_H * 60:
            for f in dinner_food:
                infeasible.append(f["id"])
                reason[f["id"]] = (f"当天行程结束过早，晚餐到达将等待超"
                                   f"{MAX_MEAL_WAIT_H:.1f}h 上限")
    return {"assign": assign, "infeasible": infeasible, "reason": reason}


def _insert_foods(seq_rest: list, foods: list, hotel, city: dict,
                  mode: str | None = None) -> list:
    """餐窗感知的美食点插入：把 foods 逐个插入 seq_rest 的
    「绕行最小 + 最贴近其目标餐窗时刻」位置（配合 _build_timeline 的美食硬约束）。"""
    if city is None or not foods:
        return list(seq_rest) + list(foods)
    seq = list(seq_rest)
    meals = city["meal_slots"]
    win_mid = {k: (poi_db.hhmm_to_h(meals[k][0]) + poi_db.hhmm_to_h(meals[k][1])) / 2
               for k in ("lunch", "dinner")}
    # 窗位分配与 _build_timeline 的让位口径统一（同窗超额顺延另一窗）
    win_of = _assign_food_windows(foods, city)
    # 美食点按目标餐窗先后插入（午市偏好先插，避免两个点抢同一窗）
    foods = sorted(foods, key=lambda p: (win_mid.get(FOOD_PREF_WIN.get(p.get("best_time"), "lunch"), 12.5), -p["rating"]))
    day_start = poi_db.hhmm_to_h(city["day_start"])
    for f in foods:
        target_win = win_of.get(f["id"]) or FOOD_PREF_WIN.get(f.get("best_time"), "lunch")
        target = win_mid.get(target_win, 12.5)
        # 预计算序列各位置的到达时刻（day_start 起累计 travel+dur，跨过饭点补偿餐块 1h）
        t_est, times, prev_q = day_start, [day_start], None
        for q in seq:
            tt = poi_db.travel_hours(prev_q, q, mode) if prev_q is not None else \
                (poi_db.travel_hours(hotel, q, mode) if hotel is not None else 0)
            t_est += tt + q["dur"]
            for _ms in win_mid.values():  # 跨过饭点 → 排序器会插餐块，预估补偿
                if t_est - tt - q["dur"] < _ms <= t_est:
                    t_est += 1.0
            times.append(t_est)
            prev_q = q
        ws_t, we_t = (poi_db.hhmm_to_h(meals[target_win][0]), poi_db.hhmm_to_h(meals[target_win][1]))
        best_pos, best_cost = 0, float("inf")
        for pos in range(len(seq) + 1):
            a = seq[pos - 1] if pos > 0 else hotel
            b = seq[pos] if pos < len(seq) else None
            detour = poi_db.travel_hours(a, f, mode) if a is not None else 0
            if b is not None and a is not None:
                detour += poi_db.travel_hours(f, b, mode) - poi_db.travel_hours(a, b, mode)
            arrive_est = times[pos] + (poi_db.travel_hours(a, f, mode) if a is not None else 0)
            # 早到等待：与 _build_timeline 同口径（餐窗起点 - 提前容差）
            wait = max(0.0, (ws_t - MEAL_EARLY_TOL_H) - arrive_est)
            late = max(0.0, arrive_est - we_t)          # 晚于窗尾（基本必被剔除）
            cost = detour + 0.6 * wait + 10.0 * late
            if wait > MAX_MEAL_WAIT_H and pos < len(seq):
                cost += 10.0  # 日中空等过久会被 _build_timeline 剔除，规避该位置
            if cost < best_cost:
                best_pos, best_cost = pos, cost
        seq.insert(best_pos, f)
    return seq


def order_day(day_pois: list, start_poi=None, hotel=None, city: dict | None = None,
              mode: str | None = None) -> list:
    """贪婪最近邻 + 建议时段偏置：从早到晚排一条线（有酒店则从酒店出发选首点）。

    mode：出行方式 → 通行时间口径与时间轴一致（骑行主题下按骑行时间找最近邻）。
    city 传入时启用餐窗感知：先排非美食线，再把美食 POI 插入到
    「绕行最小 + 最贴近其目标餐窗时刻」的位置（配合 _build_timeline 的美食硬约束）。
    """
    if not day_pois:
        return []
    # 咖啡馆/茶饮不是正餐：不参与餐窗竞争，当普通景点排（时段自由，间隙休憩）
    foods = [p for p in day_pois if p.get("category") == "food" and not is_cafe(p)]
    rest = [p for p in day_pois if p.get("category") != "food" or is_cafe(p)]

    def _greedy(pool: list) -> list:
        if not pool:
            return []
        remaining = list(pool)
        if hotel is not None:
            cur = min(remaining, key=lambda p: poi_db.travel_hours(hotel, p, mode))
        else:
            cur = start_poi or max(remaining, key=lambda p: (p["rating"] - 3 * _day_score_key(p), p["rating"]))
        seq = [cur]
        remaining.remove(cur)
        while remaining:
            nxt = min(remaining, key=lambda p: (
                poi_db.travel_hours(cur, p, mode) + 0.8 * abs(_day_score_key(p) - _day_score_key(cur)),
                -p["rating"]))
            seq.append(nxt)
            remaining.remove(nxt)
            cur = nxt
        return seq

    if city is None or not foods:
        return _greedy(day_pois)

    return _insert_foods(_greedy(rest), foods, hotel, city, mode)


def _repair_by_drop(day_pois: list, city: dict, day_no: int, max_drop: int = 3,
                    weekday: str | None = None, hotel: dict | None = None,
                    mode: str | None = None, slow: bool = False):
    """二级修复：重排后仍有违规 → 剔除肇事 POI（模拟 Agent Loop 的剔除+反馈）。"""
    pois = list(day_pois)
    dropped = []
    for _ in range(max_drop):
        tl = _build_timeline(order_day(pois, hotel=hotel, city=city, mode=mode),
                             city, day_no, weekday, hotel, mode=mode, slow=slow)
        if not tl["violations"]:
            break
        bad_name = tl["violations"][-1]["poi"]
        bad = next((p for p in pois if p["name"] == bad_name), None)
        if bad is None and bad_name == "返程" and pois:
            # M6 修复链补洞：返程超时的「肇事点」不在列表里 → 剔除离酒店最远的点（返程腿最长的贡献者）
            bad = (max(pois, key=lambda p: poi_db.travel_hours(p, hotel, mode))
                   if hotel is not None else pois[-1])
        if bad is None or len(pois) <= 1:
            break
        pois.remove(bad)
        dropped.append({"id": bad["id"], "name": bad["name"],
                        "reason": tl["violations"][-1]["reason"]})
    tl = _build_timeline(order_day(pois, hotel=hotel, city=city, mode=mode),
                         city, day_no, weekday, hotel, mode=mode, slow=slow)
    tl["dropped"] = dropped
    return tl


def _cap_km_repair(day_pois: list, city: dict, day_no: int, weekday: str | None,
                   hotel: dict | None, cap: float, theme: str | None = None,
                   mode: str | None = None, slow: bool = False):
    """主题里程预算修复：当日交通里程超预算 → 迭代剔除「最远腿」POI（贡献最长绕行的点）再重排。

    约束感知：每次剔点只接受「里程收敛且不引入新违规」的候选（按最远腿降序逐个试剔）；
    剔谁都违规则停止——硬约束优先于里程预算，宁可超预算也不产出违规行程。
    """
    label = {"cycling": "骑行", "hiking": "徒步", "family": "亲子"}.get(theme, "骑行")
    pois = list(day_pois)
    dropped = []
    while len(pois) > 2:
        tl = _build_timeline(order_day(pois, hotel=hotel, city=city, mode=mode),
                             city, day_no, weekday, hotel, mode=mode, slow=slow)
        if tl["travel_km"] <= cap and not tl["violations"]:
            break

        # 全天大点豁免：迪士尼/海昌类（时长≥8h）须独占一天，不参与「最远腿」剔除——
        # 否则亲子游必剔迪士尼（单程 ~19km，往返必超预算），与常识相悖；
        # 当天只剩余全天大点时接受里程超额（远郊大点当天交通预算必然突破，属合理例外）
        droppable = [p for p in pois if not is_full_day(p)]
        if not droppable:
            tl["dropped"] = dropped
            return tl

        def _far_leg(p, _pois=pois):
            others = [q for q in _pois if q is not p]
            return max((poi_db.haversine_km(p["lat"], p["lng"], q["lat"], q["lng"])
                        for q in others), default=0.0)
        chosen, chosen_tl = None, None
        for cand in sorted(droppable, key=_far_leg, reverse=True):
            trial = [p for p in pois if p is not cand]
            ttl = _build_timeline(order_day(trial, hotel=hotel, city=city, mode=mode),
                                  city, day_no, weekday, hotel, mode=mode, slow=slow)
            if not ttl["violations"]:  # 剔它不引入新违规才接受
                chosen, chosen_tl = cand, ttl
                break
        if chosen is None:
            break
        pois.remove(chosen)
        dropped.append({"id": chosen["id"], "name": chosen["name"],
                        "reason": f"{label}里程超预算（>{cap:.0f} km/天），剔除远点收敛路线"})
        tl = chosen_tl
    tl = _build_timeline(order_day(pois, hotel=hotel, city=city, mode=mode),
                         city, day_no, weekday, hotel, mode=mode, slow=slow)
    tl["dropped"] = dropped
    return tl


def build_itinerary(day_map: dict, city: dict, all_pois: dict, order_given: bool = True,
                    date0: str | None = None, hotel: dict | None = None,
                    query: str | None = None) -> dict:
    """day_map: {1: [poi_id,...], ...}  —— 尊重 LLM 给定顺序(order_given)，逐日排时序。

    date0: 行程起始日期（YYYY-MM-DD）——传入后按天推算星期，闭馆日作为硬约束校验/剔除。
    query: 命中主题画像（骑行/徒步）时，通行时间与 hop 标签按该方式的速度模型计算。
    """
    result_days, all_violations, total_km = [], [], 0.0
    all_dropped = []
    cap = theme_km_cap(query)
    theme = detect_theme(query)
    mode = travel_mode(query)
    # 慢节奏餐窗保障：到达时刻临近饭点（≤30min）即先吃再逛。query 自带口径，
    # 调用方无需显式传参（M1/M2/M7 全部生效）；非慢节奏行为完全不变
    slow = bool(SLOW_PACE_RE.search(query or ""))
    # 单日主题（“其中一天骑行”）：主题口径（通行模型 + 里程预算）仅作用于承载天，
    # 其余天回退默认车驾——非主题日的接驳不该被标成骑行/徒步
    theme_day = scoped_theme_day(day_map, all_pois, query)
    for d in sorted(day_map):
        ids = day_map[d]
        pois = [all_pois[i] for i in ids if i in all_pois]
        wd = poi_db.trip_weekday(date0, d) if date0 else None
        # 正餐餐窗容量前置修剪：每窗 ≤1 家正餐是硬约束，候选超额时在进时间轴前剔除
        # （评分最高的留午/晚各一家），避免修复链 max_drop 上限内剔不干净残留违规。
        # 咖啡馆/茶饮不算正餐、不占餐窗，不参与修剪。
        pre_drop = []
        foods = [p for p in pois if p.get("category") == "food" and not is_cafe(p)]
        if len(foods) > 2:
            by_win = {}
            for p in foods:
                by_win.setdefault(FOOD_PREF_WIN.get(p.get("best_time"), "lunch"), []).append(p)
            keep_ids = {p["id"] for w in ("lunch", "dinner")
                        for p in sorted(by_win.get(w, []), key=lambda q: -q["rating"])[:1]}
            extra = [p for p in foods if p["id"] not in keep_ids]
            if extra:
                pre_drop = [{"id": p["id"], "name": p["name"],
                             "reason": "美食餐窗容量已满（每窗最多 1 家），剔除超额美食点"}
                            for p in extra]
                extra_ids = {p["id"] for p in extra}
                pois = [p for p in pois if p["id"] not in extra_ids]
        seq = pois if order_given else order_day(pois, hotel=hotel, city=city,
                                                 mode=mode if theme_day in (None, d) else None)
        mode_d = mode if theme_day in (None, d) else None
        cap_d = cap if theme_day in (None, d) else None
        # P2：尊重给定顺序（非正餐点相对顺序不变），但正餐点做最小绕行位重插——
        # 餐窗感知成本函数兜底；咖啡馆不算正餐，留在原相对位置（时段自由）
        if order_given and city is not None:
            _foods = [p for p in seq if p.get("category") == "food" and not is_cafe(p)]
            if _foods and len(seq) > len(_foods):
                seq = _insert_foods([p for p in seq
                                     if not (p.get("category") == "food" and not is_cafe(p))],
                                    _foods, hotel, city, mode_d)
        tl = _build_timeline(seq, city, d, wd, hotel, mode=mode_d, slow=slow)
        # 硬约束修复先行（重排 → 剔点），收敛到 0 违规；预算收敛在其结果上做，
        # 避免旧序「预算修复后违规修复链从全量重建」把预算成果冲掉
        if tl["violations"]:
            # 一级修复：放弃原顺序，贪婪重排
            repaired = order_day(pois, hotel=hotel, city=city, mode=mode_d)
            tl2 = _build_timeline(repaired, city, d, wd, hotel, mode=mode_d, slow=slow)
            tl["repairs"] = len(tl["violations"])
            if len(tl2["violations"]) < len(tl["violations"]):
                tl2["reordered"] = True
                tl2["repairs"] = tl["repairs"]
                tl = tl2
            else:
                tl["reordered"] = False
            # 二级修复：重排无效说明总量超载 → 剔除肇事 POI
            if tl["violations"]:
                tl3 = _repair_by_drop(pois, city, d, weekday=wd, hotel=hotel,
                                      mode=mode_d, slow=slow)
                tl3["repairs"] = tl["repairs"]
                tl3["reordered"] = True
                tl = tl3
        # 主题画像：每日里程预算收敛（约束感知剔点，不引入新违规）
        if cap_d and tl["travel_km"] > cap_d:
            kept = [p for p in pois
                    if p["id"] not in {x["id"] for x in tl.get("dropped", [])}]
            if len(kept) > 2:
                tl = _cap_km_repair(kept, city, d, wd, hotel, cap_d, theme,
                                    mode=mode_d, slow=slow)
        all_violations.extend(tl["violations"])
        all_dropped.extend(tl.get("dropped", []))
        total_km += tl["travel_km"]
        tl["dropped"] = pre_drop + tl.get("dropped", [])  # 前置修剪与修复链剔除合并
        result_days.append({"day": d, **tl})
    # 日内空档汇总（2026-09-15 空档治理）：逐日 scan_gaps 结果升到行程级，
    # 供 compose 生成用户可见提示（此前空档是"隐形的"，前端只看到时间轴凭空断开）
    day_gaps = [{"day": d["day"], **g} for d in result_days for g in d.get("gaps", [])]
    return {"days": result_days, "total_violations": len(all_violations),
            "total_travel_km": total_km, "dropped_pois": all_dropped,
            "gaps": day_gaps}
