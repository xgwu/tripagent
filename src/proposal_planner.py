# -*- coding: utf-8 -*-
"""M7 经验提案模式：LLM 世界知识自由提案 → 落地匹配到 POI 库 → 复用 M2 TOPTW 求解。

回归最初架构意图：「LLM 凭经验生成线路，再匹配库内 POI」，同时保留 M1-M6 的防幻觉成果：
- A 提案：LLM 自由发挥（允许提库外点，故意不设防）
- B 落地：精确/包含/模糊/LLM 四级匹配；匹配失败剔除并记入「POI 库缺口」清单
- C 求解：落地后的 day_map 喂给 m2_planner.compose（TOPTW + 修复链 + 文案）
"""
import difflib, hashlib, json, os, re, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import poi_db, llm_client, m1_planner, m2_planner, offline_planner, sequencer
from src import hotel as hotel_mod

# ---- P1 提案级缓存：同（prompt版本+城市+天数+需求+日期+库版本）复用上次 LLM 提案，跳过最贵的 A 阶段 ----
# 提案调用 temperature=0.2/seed=42 本身近似确定性，缓存纯省时无行为差异。
# key 含库内 POI 数量：扩城/补库后自动失效；PROPOSE_PROMPT_VER：prompt 文案变更时递增令旧缓存失效；
# TTL 7 天与 nl_cache 对齐。
PROPOSE_PROMPT_VER = "v10"  # v10：常开硬规则 + 按需注入条件规则（见 BASE_RULES 上方注释）
PROPOSAL_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "proposal_cache.json")
PROPOSAL_CACHE_TTL_S = 7 * 86400
_proposal_cache: dict | None = None
_pcache_lock = threading.Lock()  # 服务端多线程并发规划共用本缓存


def _pcache_get(key: str):
    global _proposal_cache
    with _pcache_lock:
        if _proposal_cache is None:
            try:
                with open(PROPOSAL_CACHE_PATH, encoding="utf-8") as f:
                    _proposal_cache = json.load(f)
            except Exception:  # noqa
                _proposal_cache = {}
        ent = _proposal_cache.get(key)
        if not ent or time.time() - ent.get("ts", 0) > PROPOSAL_CACHE_TTL_S:
            return None
        return ent.get("v")


def _pcache_put(key: str, val: dict) -> None:
    global _proposal_cache
    with _pcache_lock:
        if _proposal_cache is None:
            _proposal_cache = {}
        _proposal_cache[key] = {"ts": time.time(), "v": val}
        try:
            # 多实例合并：先重读磁盘（其他实例可能已写入新条目），按 ts 新者胜合并再落盘，
            # 防止多实例/多进程各自持有旧快照互相覆盖丢条目
            disk: dict = {}
            try:
                with open(PROPOSAL_CACHE_PATH, encoding="utf-8") as f:
                    disk = json.load(f)
            except Exception:  # noqa
                disk = {}
            for k, ent in disk.items():
                old = _proposal_cache.get(k)
                if not old or ent.get("ts", 0) >= old.get("ts", 0):
                    _proposal_cache[k] = ent
            tmp = PROPOSAL_CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_proposal_cache, f, ensure_ascii=False)
            os.replace(tmp, PROPOSAL_CACHE_PATH)
        except Exception:  # noqa: 缓存写失败不影响主流程
            pass

PROPOSE_SYSTEM = """你是一位资深旅行规划专家，深谙中国主要旅游城市的经典玩法与本地体验节奏。
请凭你的旅行知识自由设计行程——你的专业推荐优先；用户提示词末尾会附一份系统已收录的地点清单
（带完整数据、可直接排入），仅作查漏补缺的参考。不得虚构不存在的地点。
注意基本常识（如多数博物馆周一闭馆、热门景点需预留排队时间）。"""

# ---- 提案 prompt（v10）：常开硬规则 + 按需注入的条件规则 ----
# v9 的问题不是「有 few-shot 冗余」（它压根没有 few-shot），而是 14 条规则不分场景
# 全量堆进 prompt，其中多条互相打架且靠散文描述优先级：
#   · 贯穿偏好规则 显式声明覆盖 跨天片区分散规则
#   · 慢节奏规则 声明豁免 傍晚密度规则
# 把条件逻辑写进散文，模型难遵守、也无法验证哪条真在生效。v10 改为：
# 常开规则始终注入；条件规则**只在其判据命中时**注入（判据复用 sequencer 现成
# 正则，不新造，避免口径漂移）。冲突从根源消失——慢节奏命中时傍晚密度规则
# 根本不进 prompt，不需要再写「此规则下 X 豁免」。
_RULE_FULL_DAY = "全天大点规则：时长约 8 小时以上的大型景点（如迪士尼、海昌海洋公园这类主题乐园）须独占一整天，当天不要再排其他停留点。"
_RULE_FAR_BIG = "远郊大点片区规则：距市中心 12 公里以外的半日型大点（4 小时以上的海洋馆/动物园/乐园等，如萧山的海洋公园）当天只能与同一片区的点位组合、或独占一天，绝不要把它与 10 公里外的其他片区（如运河、西湖东岸）混排在同一天——车程往返加游玩必超一日里程预算；远郊大点想搭配市区点时，只搭它返回途中顺路的点。"
_RULE_AREA_SPREAD = "跨天片区分散规则：不同天安排不同片区——同一条马路/街区（如武康路、田子坊、外滩）及其 1 公里范围内的点位只能出现在其中一天，相邻两天绝不能去同一片区。咖啡店等口碑配套每天最多 1 家，跟着当天主片区选，不要两天都往同一片区跑。"
_RULE_ONWAY = "配套顺路规则：咖啡店/餐厅等配套必须顺路——选位于当天相邻主选之间、或紧邻某个主选（步行可达，约 1.5 公里内）的店，绝不要为了某家网红店跨区绕路（例如上午在世博、下午去陆家嘴，就选两家之间沿线的店，不要折道武康路）。"
_RULE_MEAL = "正餐规则：午餐/晚餐时段安排正经吃饭的地方（餐厅/名小吃/美食街），咖啡馆不能当正餐——咖啡只作为逛点之间的休憩加项，用户喜欢咖啡时另选顺路咖啡店，不要用它顶替午餐。每天正餐最多 2 家且必须午、晚各一家，默认只安排 1 家（你最想推荐的那一餐）——同一天绝不要排两家都吃午餐（或两家都吃晚餐）的餐厅；生煎/面馆/小吃这类只做午市（午后打烊）的店一天最多 1 家，系统只会把它们排在午餐。要排第 2 家正餐时，它必须是晚市营业的餐厅（营业到 19 点以后），并且当天要有晚间安排（17:30 之后仍有停留点）把行程延续到晚餐时段；当天停留点少、下午早早结束的行程不要排晚餐餐厅——到得太早只能干等，系统会把它剔除。想吃多家名店时，分到不同天。"
_RULE_ALT = "备选规则：每天可附 0-2 个 alternates——你认为时间充裕时值得加上的点、或主选可能闭馆/排队过久时的同区域替补；备选不必与主选相邻，系统会按约束自动取舍。替补必须与当天主题同质（湖线日的替补也是湖线点、园林日的替补也是园林类），不要拿不同主题的点替补。"
_RULE_SUBURB = "远郊深度体验规则：参考清单里标了「（郊区）」的点是本地特色体验（湖鲜/古镇/温泉/乡村/郊野徒步），需求涉及这些主题时应主动纳入主选，并与同片区点位安排在同一天；不要因为「远」就一律回避。"

# 常开硬规则（结构性约束，与需求内容无关，任何查询都适用）
BASE_RULES = [_RULE_FULL_DAY, _RULE_FAR_BIG, _RULE_AREA_SPREAD, _RULE_ONWAY,
              _RULE_MEAL, _RULE_ALT, _RULE_SUBURB]

# 条件规则：仅在判据命中时注入
_RULE_EVENING = "傍晚密度规则：博物馆/美术馆/展馆类场馆普遍 17 点前后闭馆，每天要为傍晚（17 点后）搭配至少 1 个晚间型停留点——夜展/灯光夜景/历史街区夜游/滨江步道/咖啡街区/书院茶馆等，避免傍晚大片空白；每天 4-6 个停留点中应含 1-2 个晚间型。"
_RULE_SLOW = "慢节奏规则：每天 2-3 个停留点、绝不超过 4 个，以白天为主、尽量 17:30 前收尾，不安排只有夜间才开放或运营的点（酒吧/夜市/夜间演出/17 点后才开门的场馆——外滩、滨江步道、商业街、全天开放的经典景点不算夜间型点，白天照常安排）（除非用户明确提到夜景/夜市/夜游）；优先选择地势平缓、有无障碍条件、步行距离短的点位（平地园林主园区/滨湖步道/商业综合体/游船），避免登山型（虎丘山顶/山峰类）、石板路长距离古巷、需要大量站立排队的点位；同一天点位间车程尽量短，午后可安排 1 个茶馆/咖啡类慢休点，整体以从容、留有休息余量为准。"
_RULE_FAMILY = "亲子规则：绝不安排 KTV、酒吧、夜店、商务会所等夜间娱乐场所，也不安排题材沉重的场所（战争/灾难类纪念馆）与高强度登山徒步；每天 4-5 个停留点为宜，上午与下午各安排 1 个适合儿童跑动的点位（动物园/主题乐园/海洋馆/公园）或动手型场馆（科技馆/自然博物馆），点位间车程尽量短、午后留一段弹性休息；餐饮优先选有儿童座位、出餐快的本地名店，避免重辣重口与需要长时间排队的网红店。"
_RULE_SIGNATURE = "招牌体验规则：先用世界知识判断用户需求的核心期待——每个城市都有公认必去的招牌景点（如上海的迪士尼度假区、北京环球影城、广州长隆），亲子/带娃类需求通常正期待这类招牌。若需求主题与某招牌景点高度匹配，必须把它作为主选排进某一天（独占一天，勿放 alternates）；只有当你有明确理由认为用户不会感兴趣（如需求明确排斥主题乐园）时才可不放。"
_RULE_LAKE = "贯穿偏好规则：用户表达的体验偏好按语义应贯穿全部天数（如「最好临湖」「湖边骑行」「每天都要湖景」），就每天围绕该偏好选不同的岸段/子片区（例：第一天太湖西山岛一线、第二天光福—湖东岸一线；或第一天金鸡湖环湖、第二天独墅湖/阳澄湖），同一天内仍集中相邻岸段顺路串联。此时**跨天片区分散规则为该偏好让位**：不同天不得重复同一条道路/同一批点位，但允许同属一个大湖/大景区的不同岸段。若某天确实无法延续该偏好，必须在 reason 里写明原因。alternates 也必须同主题：替补点同样来自该湖/景区的岸段，绝不要用市区咖啡/商场/街区替补湖线点。"
_RULE_ANCHOR = "住宿锚点规则：用户指定住宿位置（如「住迪士尼附近」）时，行程必须包含该位置对应的标志性景点（住迪士尼附近则必含迪士尼），且该景点独占一天、优先安排在第一天，其余天数再安排其他区域。"
_RULE_NAMED = "点名规则：需求里明确点名的地点（如「想去宝业路宵夜街」「一定要去莲花岛」）必须排进某一天的主选 stops——不要放进 alternates、也不要替换成同类其他地点；参考清单里有同名/近名点时直接用清单里的名字。"

PROPOSE_PROMPT = """请为{city}设计 {days} 天行程。
用户需求：{query}
{date_line}{weather_line}
自由发挥设计一条你认为体验最好的路线，包含每天的主题与停留点（每点一句话说明为什么值得去），每天 {n_stops} 个停留点。
停留点必须是游客可游览的真实地点（景点/场馆/历史街区/公园/餐厅/市场等），不要把酒店、商铺门店当作停留点（住宿由系统另行安排）。
路线设计常识：同一天的停留点尽量集中在相邻片区、顺路串联，避免一天内东西横跨全城；优先选择知名度高、位置明确易确认的地点。
{rules}
参考清单——以下{city}地点带完整数据（坐标/开放时间/适玩时长），排入即可直接落地；若与你更想推荐的地点重合，以你的专业判断为准：
{library_hint}
严格输出 JSON：
{{"days": [{{"day": 1, "theme": "主题", "reason": "整体思路", "stops": [{{"name": "灵隐寺", "note": "为什么去"}}], "alternates": [{{"name": "西溪湿地", "note": "时间充裕可加"}}]}}]}}"""

MATCH_SYSTEM = """你是 POI 匹配助手。给定用户行程中的地点名和 POI 库清单，判断每个地点对应库中哪个 POI。
同一地点的别称、旧称、近似名称、同品牌不同分店（如「XX（苏州河店）」对应库内「XX（武康路店）」）都应匹配到库中最接近的一项；
只有当库中确实没有该地点或其近似项时才输出 null。"""

MATCH_PROMPT = """## 行程中的地点
{stops}

## POI 库清单（id｜名称｜区域｜类目）
{library}

严格输出 JSON：{{"matches": [{{"stop": "地点名", "poi_id": "库内id或null"}}]}}"""

# ---- 闭环反馈（P0）：落地失败/约束剔除 → 反馈 LLM 修正 → 再落地，共享轮次预算 ----
MAX_REVISE_ROUNDS = 2     # 最多修正轮数（每轮一次 LLM 调用）
GROUNDING_RATE_MIN = 0.85 # 落地率低于此阈值必触发修正（有无 gaps 均触发）
DROP_FEEDBACK_MIN = 2     # compose 剔除点数达到该值才触发剔除原因反馈（避免小剔大动）
AREA_OVERLAP_KM = 1.2     # 相邻两天点位距离小于该值视为片区重复（「武康路连去两天」类问题）

REVISE_SYSTEM = """你是旅行行程修正助手。系统已尝试把一份行程提案落地到 POI 库，部分地点无法落地、
或被时间窗/里程等约束剔除。请在保留原行程结构与天数划分的前提下，输出一份修正版提案。"""

REVISE_PROMPT = """原需求：{query}

当前提案 JSON：
{proposal}

## 需要修正的地点
{issues}
规则：
- 「无法落地」：该地点在 POI 库中不存在，须替换为同类/同区域的真实地点（优先参考下方清单）或删除，不要保留原名。
- 「被约束剔除」：附有剔除原因（如里程预算、时间窗冲突），替换为与当天其他点更近、更兼容的点，或删除该点。
- 「片区重复」：该点与相邻一天的某个点同在一片区（附距离），把它换成其他片区的同类点，使各天片区互不重叠；配套（咖啡店等）跟随当天新片区选且必须顺路（位于相邻主选之间或紧邻某主选 1.5 公里内）。
- 「与前面某天重复」：同一个地点被排进了多天。保留它第一次出现的那天，把重复的这天换成同主题、不同片区的其他地点——绝不要简单删掉导致那天点数不足。
- 其余地点、主题、顺序尽量原样保留；不要增加新的无法落地的地点。

参考清单——以下{city}地点带完整数据（坐标/开放时间/适玩时长），排入即可直接落地：
{library_hint}

严格输出 JSON：
{{"days": [{{"day": 1, "theme": "主题", "reason": "整体思路", "stops": [{{"name": "灵隐寺", "note": "为什么去"}}]}}]}}"""


def select_rules(query: str, all_pois: dict | None = None,
                 hotel_text: str | None = None) -> tuple:
    """按需求挑选注入 prompt 的规则。返回 (规则列表, 每日停留点数区间文案, 命中的条件规则名)。

    判据全部复用既有实现（sequencer.SLOW_PACE_RE / is_family_query、
    _LAKE_PREF_RE、poi_db.names_mentioned_in），不新造正则——两套判据必然漂移
    是本项目的老坑。

    v9 → v10 的语义等价性（必须保持，否则是行为回归而非重构）：
      · 慢节奏命中 → 注入慢节奏规则、**不注入**傍晚密度规则
        （v9 靠「此规则下傍晚密度规则豁免」这句散文实现同样效果）
      · 贯穿偏好命中 → 注入贯穿偏好规则，它自带「跨天片区分散规则为其让位」
      · 慢节奏时每天 2-3 点（v9 同）；亲子 4-5 点；其余 4-6 点
    """
    q = query or ""
    slow = bool(sequencer.SLOW_PACE_RE.search(q))
    family = sequencer.is_family_query(q)
    lake = bool(_LAKE_PREF_RE.search(q))
    named = bool(all_pois and poi_db.names_mentioned_in(q, list(all_pois.values())))
    anchor = bool(hotel_text)

    rules, hit = list(BASE_RULES), []
    # 慢节奏与傍晚密度互斥：只注入其中一条
    if slow:
        rules.append(_RULE_SLOW)
        hit.append("slow")
    else:
        rules.append(_RULE_EVENING)
    if family:
        rules += [_RULE_FAMILY, _RULE_SIGNATURE]
        hit += ["family", "signature"]
    if lake:
        rules.append(_RULE_LAKE)
        hit.append("lake")
    if anchor:
        rules.append(_RULE_ANCHOR)
        hit.append("anchor")
    if named:
        rules.append(_RULE_NAMED)
        hit.append("named")
    n_stops = "2-3 个" if slow else ("4-5 个" if family else "4-6 个")
    return rules, n_stops, hit


def _crossday_area_overlap(day_ids: dict, all_pois: dict) -> list:
    """跨天片区重叠检测：相邻两天存在 <AREA_OVERLAP_KM 的点位对 → 晚一天的点记为「片区重复」。

    「武康路连去两天」类问题的确定性守门：M1 精确去重只挡同 POI 跨天重复，
    挡不住「武康路·安福路(Day1) + 武康路咖啡店(Day2)」这类同片区不同点的重复。
    只对晚一天的点出反馈（换点责任在后者）；供闭环 REVISE 消费。
    """
    from src.poi_db import haversine_km
    feedback, days = [], sorted(day_ids)
    for i, j in zip(days, days[1:]):
        for bid in day_ids.get(j, []):
            b = all_pois.get(bid)
            if not b:
                continue
            for aid in day_ids.get(i, []):
                a = all_pois.get(aid)
                if not a:
                    continue
                dkm = haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])
                if dkm < AREA_OVERLAP_KM:
                    feedback.append({
                        "name": b["name"], "day": j,
                        "reason": (f'与 Day{i} 的「{a["name"]}」同在一片区（相距 {dkm:.1f}km），'
                                   "相邻两天不要重复同一片区，请换成其他片区的同类点")})
                    break
    return feedback


def _norm(s: str) -> str:
    """落地匹配的归一化口径 —— 委托 poi_db.norm_name（与「点名识别」共用，防词表漂移）。"""
    return poi_db.norm_name(s)


def _branch_variants(name: str) -> list:
    """分店名归一化变体：剥离「（XX店）」类括号后缀与尾部「总店/分店」，用于同品牌跨分店匹配。

    如「% Arabica（苏州河店）」→「% Arabica」，可包含匹配到库内「% Arabica（武康路店）」。
    """
    out = []
    base = re.sub(r"[（(][^（）()]*店[）)]$", "", name).strip()
    base = re.sub(r"(总店|分店)$", "", base).strip()
    if base and base != name:
        out.append(base)
    return out


def _library_hint(all_pois: dict) -> str:
    """库内地点菜单（按类目分组），注入提案提示词引导优先选用库内点。

    郊区点位补「（郊区）」标注（2026-09-15 远郊召回缺陷）：菜单此前只给名字，
    LLM 无从判断远近，于是远郊特色点（莲花岛/西山岛/朱家角类）长期不被提案——
    标注后配合提案 prompt 的「远郊深度体验规则」使用。
    """
    groups = {}
    for p in all_pois.values():
        nm = p["name"]
        if p.get("area") in ("suburb", "far"):
            nm = f"{nm}（郊区）"
        groups.setdefault(p["category"], []).append(nm)
    return "\n".join(f"- {cat}：{'、'.join(names)}"
                     for cat, names in sorted(groups.items()))


# 包含匹配的最低覆盖率：库内名长度 / 提案名长度。低于此值说明「库内名只是提案名的
# 一小截」，属于短名劫持（「拙政园」吃掉「拙政园与狮子林」），应交给拆分或 LLM 匹配。
CONTAIN_COVER_MIN = 0.55
# 并列连接词：LLM 常把两个地点塞进一个 stop（「拙政园与狮子林」「灵隐寺和飞来峰」）
_CONJ_RE = re.compile(r"[、，,]|与|和|＋|\+|及")


def _contain_candidates(name: str, by_name: list) -> list:
    """包含匹配候选：返回 [(覆盖率, 库内名长度, poi)]，按覆盖率降序。

    覆盖率 = 库内名长度 / 提案名长度（双向包含时取两者较长者作分母），
    用它替代旧实现的「按 rating 取胜」——rating 与匹配质量无关，
    高分短名会劫持长提案名（2026-09-17 实测：外滩 吃掉「外滩历史建筑群」）。
    """
    out = []
    for nm in [name] + _branch_variants(name):
        kn = _norm(nm)
        if not kn:
            continue
        for p in by_name:
            pn = _norm(p["name"])
            if len(pn) < 2:
                continue
            if pn in kn or kn in pn:
                cover = min(len(pn), len(kn)) / max(len(pn), len(kn))
                out.append((cover, len(pn), p))
    out.sort(key=lambda x: (-x[0], -x[1], x[2]["id"]))
    return out


def _ground_one(name: str, all_pois: dict, by_name: list) -> tuple:
    """单点落地：返回 (poi_id|None, method, extra_ids)。

    四级：精确 → 包含（按覆盖率）→ 并列拆分 → 模糊。

    extra_ids：提案把多个地点写进同一个 stop 时（「拙政园与狮子林」），
    除主匹配外**额外**落地到的库内 id 列表 —— 旧实现会静默丢掉后半个地点。
    """
    key = _norm(name)
    if not key:
        return None, "empty", []
    for p in by_name:  # 1. 精确
        if _norm(p["name"]) == key:
            return p["id"], "exact", []

    # 2. 包含：按覆盖率取胜，且必须达到 CONTAIN_COVER_MIN
    cands = _contain_candidates(name, by_name)
    if cands and cands[0][0] >= CONTAIN_COVER_MIN:
        return cands[0][2]["id"], f"contain({cands[0][0]:.2f})", []

    # 3. 并列拆分：覆盖率不足往往是「一个 stop 写了两个地点」。逐段独立落地，
    #    首段作主匹配、其余进 extra_ids，避免后半个地点被静默丢弃。
    parts = [x.strip() for x in _CONJ_RE.split(name) if len(_norm(x)) >= 2]
    if len(parts) >= 2:
        hits = []
        for part in parts:
            pk = _norm(part)
            hit = next((p for p in by_name if _norm(p["name"]) == pk), None)
            if hit is None:
                pc = _contain_candidates(part, by_name)
                hit = pc[0][2] if (pc and pc[0][0] >= CONTAIN_COVER_MIN) else None
            if hit is not None and hit["id"] not in [h["id"] for h in hits]:
                hits.append(hit)
        if hits:
            return hits[0]["id"], f"split({len(hits)})", [h["id"] for h in hits[1:]]

    # 4. 模糊
    best, ratio = None, 0.0
    for p in by_name:
        r = difflib.SequenceMatcher(None, key, _norm(p["name"])).ratio()
        if r > ratio:
            best, ratio = p, r
    if best is not None and ratio >= 0.62:
        return best["id"], f"fuzzy({ratio:.2f})", []
    # 5. 覆盖率不足的包含匹配作为最后兜底（好过完全不落地），但标注低覆盖
    if cands:
        return cands[0][2]["id"], f"contain_low({cands[0][0]:.2f})", []
    return None, "unmatched", []


def _llm_match(unmatched: list, all_pois: dict, exclude_ids: set | None = None) -> dict:
    """LLM 辅助匹配剩余未落地项（批量一次调用）。返回 {stop_name: poi_id|None}。

    exclude_ids: 住宿服务类等不可作为停留点的库点 id，从菜单与结果两侧剔除。
    """
    if not unmatched:
        return {}
    exclude_ids = exclude_ids or set()
    lib = "\n".join(f'{p["id"]}｜{p["name"]}｜{p.get("area", "")}｜{p["category"]}'
                    for p in sorted(all_pois.values(), key=lambda x: x["id"])
                    if p["id"] not in exclude_ids)
    stops = "\n".join(f'- {u["name"]}' + (f'（{u["note"]}）' if u.get("note") else "")
                      for u in unmatched)
    try:
        raw = llm_client.chat([
            {"role": "system", "content": MATCH_SYSTEM},
            {"role": "user", "content": MATCH_PROMPT.format(stops=stops, library=lib)}],
            temperature=0.0, seed=42, max_tokens=400)  # P2 轻量路径：匹配结果为短 JSON
        parsed = llm_client.parse_json_safe(raw)
        out = {}
        for m in parsed.get("matches", []):
            pid = m.get("poi_id")
            out[m.get("stop", "")] = pid if (pid in all_pois and pid not in exclude_ids) else None
        return out
    except Exception:  # noqa: 匹配失败按未落地处理
        return {}


def _is_lodging(raw: dict) -> bool:
    """高德采集的住宿服务类点（宾馆/酒店）不是游览停留点，匹配层免疫。"""
    tags = "".join(raw.get("tags") or [])
    return "住宿" in tags or "宾馆" in tags


def _ground(proposal: dict, city: dict, all_pois: dict, days: int):
    """B 阶段：提案落地。返回 (day_map, themes, stats)。

    主选 stops 落地进 day_map；备选 alternates 落地进 stats["alt_map"]（{day: [poi_id]}，
    供 TOPTW 低利润权重换点），备选未落地不计入 gaps/落地率（可选性质）。
    """
    lodging_ids = {r["id"] for r in city["pois"] if _is_lodging(r)}
    by_name = [p for p in all_pois.values() if p["id"] not in lodging_ids]
    day_map, themes, seen = {}, {}, set()
    stats = {"n_proposed": 0, "exact": 0, "contain": 0, "fuzzy": 0, "llm": 0,
             "unmatched": 0, "dup_skipped": 0, "gaps": [],
             "n_alt": 0, "n_alt_hit": 0, "n_split_extra": 0, "dups": []}
    pending = []  # (day, name) 待 LLM 批量匹配
    pre = {}
    extra = {}    # (day, name) -> [额外 poi_id]（一个 stop 写了多个地点时）
    alt_map = {}  # day -> [poi_id]
    for d in proposal.get("days", []):
        for s in d.get("stops", []):
            stats["n_proposed"] += 1
            pid, method, extra_ids = _ground_one(s.get("name", ""), all_pois, by_name)
            if pid:
                pre[(d.get("day"), s.get("name"))] = (pid, method)
                if extra_ids:
                    extra[(d.get("day"), s.get("name"))] = extra_ids
                    stats["n_split_extra"] += len(extra_ids)
                _m0 = method.split("(")[0]
                stats["exact" if _m0 == "exact" else
                      ("contain" if _m0 in ("contain", "contain_low", "split")
                       else "fuzzy")] += 1
            else:
                pending.append({"day": d.get("day"), "name": s.get("name", ""),
                                "note": s.get("note", "")})
        # 备选 alternates：四级匹配，未落地静默丢弃（不计 gaps）
        for s in (d.get("alternates") or []):
            if not isinstance(s, dict) or not s.get("name"):
                continue
            stats["n_alt"] += 1
            pid, _m, _ex = _ground_one(s.get("name", ""), all_pois, by_name)
            if pid:
                alt_map.setdefault(d.get("day"), []).append(pid)
                for _e in _ex:  # 并列备选一并纳入（同为可选性质）
                    alt_map.setdefault(d.get("day"), []).append(_e)
                stats["n_alt_hit"] += 1
            else:
                pending.append({"day": d.get("day"), "name": s.get("name", ""),
                                "note": s.get("note", ""), "alt": True})
    llm_res = _llm_match(pending, all_pois, exclude_ids=lodging_ids)
    for u in pending:
        pid = llm_res.get(u["name"])
        if pid and u.get("alt"):
            alt_map.setdefault(u["day"], []).append(pid)
            stats["n_alt_hit"] += 1
        elif pid:
            pre[(u["day"], u["name"])] = (pid, "llm")
            stats["llm"] += 1
        elif not u.get("alt"):  # 主选未落地才计 gaps；备选可选性质，静默丢弃
            stats["unmatched"] += 1
            stats["gaps"].append({"name": u["name"], "note": u.get("note", ""),
                                  "day": u.get("day")})

    for d in proposal.get("days", []):
        dd = d.get("day")
        if not isinstance(dd, int) or not (1 <= dd <= days):
            continue
        ids = []
        for s in d.get("stops", []):
            nm = s.get("name")
            hit = pre.get((dd, nm))
            if not hit:
                continue
            if hit[0] in seen:
                # 跨天重复：LLM 把同一个点排进了两天。旧实现只 +1 计数就静默丢弃 ——
                # 既不进 gaps、也不降落地率（grounding_rate 只除 unmatched），
                # 于是「落地率 100% 但实际少了点」，闭环也不会触发修正。
                # 现在记入 dups 台账并在下方计入落地率分母的损失项。
                stats["dup_skipped"] += 1
                stats["dups"].append({"name": nm, "day": dd,
                                      "poi_id": hit[0],
                                      "note": s.get("note", "")})
                continue
            seen.add(hit[0])
            ids.append(hit[0])
            # 并列拆分出的额外地点：跟随同一天，同样参与跨天去重
            for _e in extra.get((dd, nm)) or []:
                if _e not in seen:
                    seen.add(_e)
                    ids.append(_e)
        if ids:
            day_map[dd] = ids
            themes[dd] = {"theme": d.get("theme", ""), "reason": d.get("reason", "")}
    # 备选去重：同天重复剔除（与主选/跨天重复由 compose 的 used_all 兜底）
    for dd in list(alt_map):
        alt_map[dd] = list(dict.fromkeys(alt_map[dd]))
        if not alt_map[dd]:
            del alt_map[dd]
    stats["alt_map"] = alt_map
    # 落地率口径（2026-09-17 修正）：分子须同时扣掉「未落地」与「跨天重复被丢」。
    # 旧口径只扣 unmatched，于是 LLM 把同一个点排进两天时，落地率仍显示 100%，
    # 而行程里实际少了一个点，闭环（GROUNDING_RATE_MIN=0.85）也不会被触发。
    _lost = stats["unmatched"] + stats["dup_skipped"]
    stats["grounding_rate"] = (round((stats["n_proposed"] - _lost) /
                                     max(stats["n_proposed"], 1), 3))
    return day_map, themes, stats


def _revise_proposal(city: dict, query: str, days: int, proposal: dict,
                     gaps: list, drops: list, hint: str,
                     dups: list | None = None) -> dict | None:
    """闭环反馈：把未落地 gaps + 约束剔除 drops + 跨天重复 dups 打包回 LLM，要一版修正提案。

    gaps: [{"name","note","day"}]；drops: [{"name","reason","day"}]；
    dups: [{"name","day","poi_id"}] —— 同一个点被排进多天，后出现的那天会被丢弃，
          必须让 LLM 换成别的点（否则那天凭空少一个点，且旧口径下落地率仍显示 100%）。
    LLM 不可用/解析失败返回 None（调用方保留原案）。
    """
    issues = []
    for g in gaps:
        issues.append(f'- 「{g["name"]}」（Day {g.get("day", "?")}）无法落地——POI 库中不存在')
    for d in drops:
        issues.append(f'- 「{d["name"]}」（Day {d.get("day", "?")}）被约束剔除——{d.get("reason", "")}')
    for u in (dups or []):
        issues.append(f'- 「{u["name"]}」（Day {u.get("day", "?")}）与前面某天重复——'
                      f'同一地点只能出现在一天，请把这天换成同主题的其他地点')
    if not issues:
        return None
    try:
        raw = llm_client.chat([
            {"role": "system", "content": REVISE_SYSTEM},
            {"role": "user", "content": REVISE_PROMPT.format(
                query=query, proposal=json.dumps(proposal, ensure_ascii=False),
                issues="\n".join(issues), city=city["city"], library_hint=hint)}],
            temperature=0.2, seed=42)
        revised = llm_client.parse_json_safe(raw)
        return revised if revised.get("days") else None
    except Exception:  # noqa: 网络/Key 问题按不可修正处理
        return None


# ---- 远郊大点同天片区守门（确定性，0 LLM 成本）----
# 根因：LLM 提案偶发把距市中心 >12km 的半日型大点（4h+，如萧山海洋公园）与
# 10km 外的片区（运河/西湖东岸）混排同天——往返车程+游玩必超亲子日里程预算，
# 修复链按「剔最远点」会把大点剔掉，需求主题（动物等）随之丢失。
# 求解前把错配点移到离它们最近的天：大点与同片区点组合或独占日，交由补强兜底。
FAR_BIG_KM = 12.0          # 远郊判定：距市中心超过该值
FAR_BIG_MIN_H = 4.0        # 半日型大点时长下限（8h+ 全天型另有独占日规则）
FAR_BIG_SPREAD_KM = 10.0   # 大点与同天其他点的最大允许直线距离
FAR_REGROUP_PASSES = 2     # 最多整理轮数（防跨天震荡）
FAMILY_SOLO_H = 5.0        # family：5h+ 远郊点建议独占日（开放窗口 ~7h，同天再排点必溢出）

# 贯穿性湖偏好检测（与 m2_planner._LAKE_PREF_RE 口径一致：query 含「湖」字）
_LAKE_PREF_RE = re.compile(r"湖")


def _is_family_query(query: str | None) -> bool:
    """亲子出行判定——委托 m2_planner.is_family_query。

    判据（FAMILY_RE）已下沉到最底层 sequencer，由 m2_planner re-export：
    选点层（TOPTW 池/补位/补强）与提案层（远郊独占日守门）必须共用同一口径，
    否则会出现「提案层按亲子守门、选点层却把 family_ok=false 的点排进来」。
    正则与原实现完全一致，行为不变。
    """
    return m2_planner.is_family_query(query)


def _far_big_point_regroup(day_map: dict, all_pois: dict, family: bool = False,
                           city: dict | None = None, query: str = "") -> list:
    moves = []
    if len(day_map) < 2:
        return moves
    # 挪点容量预检（报障 14）：目标天负载（dur+腿）超 TOPTW horizon 就不接——
    # 挪完 TOPTW 重解必剔（骑行 Day2 塞 8 点剔 5 主选实证）。city/query 缺省时
    # （旧调用方兼容）退化为不预检。
    _t_mode = sequencer.travel_mode(query or "") if query else None
    _horizon = m2_planner._day_horizon(city) if city else None
    for _pass in range(FAR_REGROUP_PASSES):
        moved_any = False
        # family：先标记所有「需独占日」的天——它们既不让位也不接收外来点（防两天互踢震荡）
        strict_days = {}
        if family:
            for d, ids in day_map.items():
                for i in ids:
                    p = all_pois.get(i)
                    if (p and p.get("dist_center_km", 0) > FAR_BIG_KM
                            and FAMILY_SOLO_H <= float(p.get("dur") or 0) < 8.0):
                        strict_days[d] = i
                        break
        for d in sorted(day_map):
            ids = day_map.get(d) or []
            if len(ids) < 2:
                continue
            pts = [all_pois[i] for i in ids if i in all_pois]
            # family 严格档：5h+ 远郊点建议独占日（先于普通大点判定）
            big = next((p for p in pts if family
                        and p.get("dist_center_km", 0) > FAR_BIG_KM
                        and FAMILY_SOLO_H <= float(p.get("dur") or 0) < 8.0), None)
            strict = big is not None
            if big is None:
                big = next((p for p in pts
                            if p.get("dist_center_km", 0) > FAR_BIG_KM
                            and float(p.get("dur") or 0) >= FAR_BIG_MIN_H), None)
            if big is None:
                continue
            if strict:
                # 独占日：同天所有其他停留点都让位（餐点由 sequencer 后续自动补，不受影响）
                misplaced = [i for i in ids if i != big["id"]]
            else:
                misplaced = [i for i in ids if i != big["id"]
                             and poi_db.haversine_km(all_pois[i]["lat"], all_pois[i]["lng"],
                                                     big["lat"], big["lng"]) > FAR_BIG_SPREAD_KM]
            if not misplaced:
                continue
            if not strict:
                others = [all_pois[i] for i in misplaced if i in all_pois]
                # 主题日豁免：被判「错配」的点全是远郊 → 这是环湖/海岛类远郊主题日，
                # 点间 10-30km 是环线正常尺度，不拆（拆去邻天市区动线会被 TOPTW
                # 以「时间预算内无法纳入」团灭，见环太湖骑行 case）。仅 family
                # 独占日语义（strict）优先，不走此豁免。
                if others and all(p.get("dist_center_km", 0) > FAR_BIG_KM for p in others):
                    continue
            for i in misplaced:
                best_d, best_km = None, None
                for d2 in day_map:
                    if d2 == d or d2 in strict_days:
                        continue  # 不移入另一个需独占日的天
                    # 容量预检：目标天加上 i 后 dur+腿 ≤ TOPTW horizon 才作候选
                    if _horizon is not None:
                        cand_ids = (day_map.get(d2) or []) + [i]
                        if m2_planner._day_load(cand_ids, all_pois, _t_mode) > _horizon:
                            continue
                    pts2 = [all_pois[j] for j in day_map.get(d2, [])
                            if j in all_pois and j != i]
                    # 空天也可作为落点（km 记 0）——独占日整理时尤其需要
                    km = 0.0 if not pts2 else sum(
                        poi_db.haversine_km(all_pois[i]["lat"], all_pois[i]["lng"],
                                            p["lat"], p["lng"]) for p in pts2) / len(pts2)
                    if best_km is None or km < best_km:
                        best_d, best_km = d2, km
                if best_d is None:
                    continue
                day_map[d].remove(i)
                day_map[best_d].append(i)
                moves.append({"id": i, "name": all_pois[i]["name"],
                              "from": d, "to": best_d})
                moved_any = True
        if not moved_any:
            break
    return moves


def _reconcile_food_windows(day_map: dict, all_pois: dict, city: dict,
                            query: str, grounding: dict) -> dict:
    """主选层餐窗对账（报障 13 改进 A + 报障 15 残余补强）。

    两件事都在主选层提前处理，避免 sequencer 剔点后前端出现
    「美食 POI 未能安排进用餐时段」：
      ① 同一天同一餐窗只保留 1 家正餐（按提案顺序，推荐度靠前者保留）；
      ② `sequencer.food_window_plan` 判定不可行的正餐直接降级——营业时间与
         剩余餐窗不匹配（只做午市的小店被挤到晚餐窗），或当天行程撑不到晚餐
         时段（早收工的天排晚餐餐厅必然干等超 MAX_MEAL_WAIT_H）。
    降级点**不回填 alt_map**（旧行为是回填）：它已被确定性判定当天排不进，
    留在补位池只会被 _alt_substitute 换回来、重新触发剔除提示。降级记录写入
    grounding["food_demoted"] 供前端披露。在 _post_ground_fixups 之后调用
    （补强注入的餐厅同样参与对账）。
    """
    mode = sequencer.travel_mode(query or "")
    demoted_all = []
    for d, ids in list(day_map.items()):
        plan = sequencer.food_window_plan(ids, all_pois, city, mode)
        bad = set(plan["infeasible"])
        seen_win: dict = {}
        for pid in ids:
            p = all_pois.get(pid)
            if not p or pid in bad:
                continue
            if p.get("category") == "food" and not sequencer.is_cafe(p):
                win = plan["assign"].get(pid) or sequencer.FOOD_PREF_WIN.get(
                    p.get("best_time"), "lunch")
                if win in seen_win:
                    bad.add(pid)
                else:
                    seen_win[win] = pid
        if not bad:
            continue
        keep = [pid for pid in ids if pid not in bad]
        if not keep:  # 降级会清空当天 → 至少留一家，交给 sequencer 走既有剔除路径
            keep, bad = ids[:1], set(ids[1:])
        day_map[d] = keep
        for pid in bad:
            p = all_pois.get(pid) or {}
            demoted_all.append({
                "name": p.get("name", ""), "day": d,
                "reason": plan["reason"].get(pid)
                          or "同一餐窗超出 1 家正餐，保留推荐度更高者"})
    if demoted_all:
        grounding["food_demoted"] = demoted_all
    return day_map


def _nearest_day(day_map: dict, all_pois: dict, poi: dict) -> int:
    """与 poi 几何最近的一天（并列时取点数更少者，再取更早的天）。"""
    best, best_key = None, None
    for d, ids in day_map.items():
        pts = [all_pois[i] for i in ids if i in all_pois]
        dist = min((poi_db.haversine_km(poi["lat"], poi["lng"], q["lat"], q["lng"])
                    for q in pts), default=float("inf"))
        key = (dist, len(pts), d)
        if best_key is None or key < best_key:
            best, best_key = d, key
    return best if best is not None else 1


def _inject_named_pois(day_map: dict, all_pois: dict, query: str, grounding: dict) -> list:
    """用户点名的库内点位 → 确定性注入（2026-09-15 点名召回缺陷修复）。

    LLM 对「用户点名」的遵守不稳定：实测 query「广州2天，晚上想去宝业路宵夜街吃宵夜」
    提案 13 个点里一个都不是它，而落地率 100% 说明该点在库内完全可用——于是它从未进入
    TOPTW 池（「能排但从不被选」）。落地后必须兜底，与住宿锚点硬保障同构。

    与住宿锚点的区别：**不受 anchor_hard_guarantee 开关控制**——住宿锚点是顺带约定，
    用户点名是硬需求。注入目标天 = 几何最近的那天（同片区顺路）；注入位置 = 当天队尾
    （晚间型点排收尾更自然；忠实模式 TOPTW 只排序不选点，插入位置仅影响 rank 利润）。
    返回 [(poi, day)]，供后续「是否真排进最终行程」的对账披露。

    ⚠️ 两个字段分工（2026-09-15 二次修正，踩过的坑）：
      `grounding["named_pois"]`   = **需求点名的全量库内点位**（无论是否注入、无论 LLM
        是否已提案）。这是「必选」与「对账披露」的口径来源。
      `grounding["named_injected"]` = 只记**本次真正由我们注入**的那些，作审计留痕。
    起初两者混用（forced/对账都读 named_injected），于是 LLM **恰好提案了点名点**时反而
    不被列为必选、被剔后也不披露——苏州「1天想去莲花岛」实测：LLM 提案了它，进 day_map
    即跳过注入 → named_injected 为空 → 求解器把它剔掉 → 用户看到行程里没有莲花岛且
    零提示。**「LLM 听话」和「用户要求被满足」是两件事，后者必须独立对账。**
    """
    named = poi_db.names_mentioned_in(query or "", list(all_pois.values()))
    if not named:
        return []
    grounding["named_pois"] = [{"id": p["id"], "name": p["name"], "area": p.get("area")}
                               for p in named]
    used = {pid for ids in day_map.values() for pid in ids}
    injected, notes = [], []
    for p in named:
        if p["id"] in used:
            continue
        d = _nearest_day(day_map, all_pois, p)
        day_map[d] = list(day_map.get(d, [])) + [p["id"]]
        used.add(p["id"])
        injected.append((p, d))
        notes.append({"id": p["id"], "name": p["name"], "day": d,
                      "reason": "需求点名点位，确定性注入（不依赖 LLM 是否采纳）"})
    if notes:
        grounding["named_injected"] = notes
    return injected


def _post_ground_fixups(day_map: dict, themes: dict, grounding: dict, city: dict,
                        all_pois: dict, days: int, query: str, anchor_poi: dict | None):
    """落地后确定性修整：锚点注入（开关控制）→ 缺天补齐 → 单天 MIN_STOPS 补强。"""
    # 用户点名锚定（2026-09-15）：放在最前——后续各 gate（亲子/慢节奏/片区重排/餐窗
    # 对账）都能看到它并给出可解释的处置，而不是被静默忽略
    _inject_named_pois(day_map, all_pois, query, grounding)
    # 住宿锚点硬保障（开关默认关）：锚点地标未进行程时确定性注入
    if anchor_poi is not None and hotel_mod.hard_guarantee_enabled():
        note = hotel_mod.ensure_landmark_in_day_map(day_map, days, anchor_poi)
        if note:
            grounding["anchor_injected"] = note
    # 慢节奏（老人/轮椅/不累）：主选中的「真夜间点」确定性移除——行程白天为主；
    # 且防止 evening 软时间窗把稀疏时间轴拉出数小时空档
    # （圆融天幕街 case：前一站 12:46 结束，等 18:00 开场，空档 5.2h）。
    # 真夜间点判据 = nightlife 类目或开门 ≥16.5（poi_db.is_night_only）——
    # 2026-09-14 报障 11：旧判据按 best_time=evening 一刀切，把全天开放的外滩/
    # 南京路步行街/陆家嘴滨江大道误杀（它们排白天毫无障碍），Day2/Day3 被
    # 15:21/14:45 收工变薄。用户明确表达夜景/夜市/夜游兴趣时不过滤。
    if (m2_planner.SLOW_PACE_RE.search(query or "")
            and not re.search(r"夜景|夜市|夜游|夜生活|灯光秀|夜花园", query or "")):
        removed_evening = []
        for d in list(day_map):
            kept = []
            for pid in day_map.get(d, []):
                p = all_pois.get(pid)
                if p and poi_db.is_night_only(p):
                    removed_evening.append({
                        "name": p["name"], "day": d,
                        "reason": "慢节奏行程：夜间型点已移除（白天为主、留足休息）"})
                else:
                    kept.append(pid)
            day_map[d] = kept
        if removed_evening:
            grounding["slow_evening_removed"] = removed_evening
    # 亲子出行：主选里 family_ok=false 的点确定性移除（KTV/酒吧街区/沉重题材纪念馆/
    # 高强度徒步）。该字段此前只有 offline_planner 读，主链路（提案 → TOPTW）无人拦——
    # LLM 自觉避开是概率不是保证，必须确定性兜底（同慢节奏档的教训）。
    # 位置在落地之后、天数保障/补强之前：既让 reason 可解释，也避免当天被剔成薄天
    # 后被补强随机填点。
    if m2_planner.is_family_query(query):
        removed_family = []
        for d in list(day_map):
            kept = []
            for pid in day_map.get(d, []):
                p = all_pois.get(pid)
                if p is not None and not poi_db.is_family_ok(p):
                    removed_family.append({
                        "name": p["name"], "day": d,
                        "reason": "亲子出行：不适合带儿童前往，已移除"})
                else:
                    kept.append(pid)
            day_map[d] = kept
        if removed_family:
            grounding["family_removed"] = removed_family
    # 天数保障：提案/落地后不足请求天数（LLM 少给一组或落地失败清空某天）→
    # 用离线规划从剩余未落地候选补齐缺口日
    if day_map:
        missing = [d for d in range(1, days + 1) if d not in day_map]
        if missing:
            used = {pid for ids in day_map.values() for pid in ids}
            rest = [p for i, p in all_pois.items() if i not in used]
            if rest:
                # 湖偏好：补天优先用湖线点池——离线规划器不识湖主题，按评分补
                # 会填进市区旗舰点，把主题天稀释成经典线（2026-09-14 报障案例）
                pool = rest
                if _LAKE_PREF_RE.search(query or ""):
                    lake = [p for p in rest if poi_db.is_lake_poi(p)]
                    if len(lake) >= 3:
                        pool = lake
                extra_map, extra_themes = offline_planner.plan_days(city, pool, query, len(missing))
                for k, ed in enumerate(sorted(extra_map)):
                    if k >= len(missing):
                        break
                    day_map[missing[k]] = extra_map[ed]
                    if ed in extra_themes:
                        themes[missing[k]] = extra_themes[ed]
                grounding["days_topped_up"] = missing
    # 远郊大点同天片区守门：错配点在求解前移到最近的天（防主题点被里程守门剔除）；
    # family 查询额外启用独占日档（5h+ 远郊点当天其他点全部让位）
    family = _is_family_query(query)
    regroup = _far_big_point_regroup(day_map, all_pois, family=family,
                                     city=city, query=query)
    if regroup:
        grounding["far_regroup"] = regroup
    # family 独占日建议：整理后同天仍有其他停留点的 5h+ 远郊点 → 明确提示用户
    if family:
        solo_suggest = []
        for d, ids in sorted(day_map.items()):
            for pid in ids:
                if pid not in all_pois:
                    continue
                p = all_pois[pid]
                if (len(ids) > 1 and p.get("dist_center_km", 0) > FAR_BIG_KM
                        and FAMILY_SOLO_H <= float(p.get("dur") or 0) < 8.0):
                    solo_suggest.append({
                        "type": "far_big_solo", "day": d, "name": p["name"],
                        "message": f"「{p['name']}」游玩约 {p.get('dur')} 小时且位于远郊，"
                                   f"当天还安排了 {len(ids)-1} 个点，节奏会偏赶，"
                                   f"建议把它单独安排一整天"})
        if solo_suggest:
            grounding["far_big_solo"] = solo_suggest
    # 单天补强：缺口剔除导致某天只剩 1-2 个点 → 从未用候选离线补足（P0-3 缺口二次提案）
    if day_map:
        # 慢节奏（老人/轮椅/不累）下限也是 3：2248054 曾降档到 2（「宽松是需求」），
        # 用户实测反馈 2 点/天半天收工偏薄（13:00 收工）。3 点 = 上午 2 点 + 下午 1 点
        # 或匀开，仍是宽松节奏；上限仍 ≤4（提案截断+补位 cap），宁少勿多不回退
        # 忠实执行模式（toptw_faithful_mode）降档为 2：提案点数即承诺，补强只防
        # 「落地全灭剩 0-1 点」的薄天兜底，补到 3 会违背「落地 ≤ 提案」承诺
        MIN_STOPS = 2 if m2_planner.faithful_mode_enabled() else 3
        used = {pid for ids in day_map.values() for pid in ids}
        topped = []
        for d in range(1, days + 1):
            if any(sequencer.is_full_day(all_pois[pid])
                   for pid in day_map.get(d, []) if pid in all_pois):
                continue  # 全天大点（迪士尼等）独占日不补小点——补了也会被时间约束剔除
            if family and any(
                    all_pois[pid].get("dist_center_km", 0) > FAR_BIG_KM
                    and FAMILY_SOLO_H <= float(all_pois[pid].get("dur") or 0) < 8.0
                    for pid in day_map.get(d, []) if pid in all_pois):
                continue  # family 5h+ 远郊点（野生动物世界类）同理：开放窗口仅 ~7h，不补点
            while len(day_map.get(d, [])) < MIN_STOPS:
                rest = [p for i, p in all_pois.items() if i not in used]
                if m2_planner.is_family_query(query):
                    # 亲子补强池过滤：family_ok=false 的点不得进补强池。
                    # 离线补强器（offline_planner）虽自带 family_ok 检查，但它依赖
                    # 从 query 解析出的 qtags；这里用统一判据再兜一层，
                    # 防「主选剔掉 → 补强又按评分捞回来」的闭环空转。
                    rest = [p for p in rest if poi_db.is_family_ok(p)]
                if sequencer.travel_mode(query or "") == "cycling":
                    # 骑行口径补强过滤（报障 14）：骑行合理半径 ≤12km，远郊点
                    # 往返 1.5h+ 与骑行矛盾——薄天宁少勿远（天文馆 70km 案例）。
                    # 慢节奏档另有 15km 过滤，取更严交集。
                    rest = [p for p in rest
                            if float(p.get("dist_center_km") or 0) <= 12.0]
                if m2_planner.SLOW_PACE_RE.search(query or ""):
                    # 慢节奏补强池过滤：① 真夜间点（nightlife/晚开门——会在稀疏时间轴
                    # 上等待开场拉出数小时空档，2026-09-14 报障 6 圆融天幕街；
                    # best_time=evening 全天开放点不算，报障 11 同判据精准化）；
                    # ② 徒步/登山类高体力点（PROPOSE_PROMPT 有「平缓无障碍」规则，
                    # 但离线补强器不识 query 体力画像，需确定性兜底）；
                    # ③ 远郊点（往返车程 1h+，对老人不友好，且市区近点足够填薄天）
                    rest = [p for p in rest
                            if not poi_db.is_night_only(p)
                            and not ({"徒步", "登山"} & set(p.get("tags") or []))
                            and float(p.get("dist_center_km") or 0) <= 15.0]
                if not rest:
                    break
                # 当天已有点的 10km 邻域优先——防止补点再造跨片区混排
                #（远郊大点日尤其重要：补进远处点会让里程守门再次剔掉大点）
                day_pts = [all_pois[pid] for pid in day_map.get(d, []) if pid in all_pois]
                near = [p for p in rest
                        if day_pts and min(poi_db.haversine_km(p["lat"], p["lng"],
                                                               q["lat"], q["lng"])
                                           for q in day_pts) <= 10.0]
                # 湖偏好：补点池优先湖线点（邻域内的湖点 → 任意湖点 → 原池），
                # 防止剔点后的薄天被市区点填满、湖主题消失
                pool = near or rest
                if _LAKE_PREF_RE.search(query or ""):
                    lake = [p for p in pool if poi_db.is_lake_poi(p)]
                    if lake:
                        pool = lake
                extra_map, _et = offline_planner.plan_days(city, pool, query, 1)
                new = [pid for pid in extra_map.get(1, []) if pid not in used]
                if not new:  # 候选耗尽或规划器无法给出新点
                    break
                need = MIN_STOPS - len(day_map.get(d, []))
                day_map[d] = day_map.get(d, []) + new[:need]
                used.update(new[:need])
                if d not in topped:
                    topped.append(d)
        if topped:
            grounding["days_stops_topped_up"] = topped
    # 慢节奏截断（最终 gate）：LLM 对「每天 2-3 点、不超 4」遵循不足——
    # 「带老人」单词触发下提案仍排 5 点/天。确定性截断至 4（保留提案序靠前的点），
    # 放在补强/同天调度之后，兜住前面所有环节的超员（2026-09-14 报障案例）
    if m2_planner.SLOW_PACE_RE.search(query or ""):
        truncated = []
        for d in sorted(day_map):
            ids = day_map.get(d, [])
            if len(ids) > 4:
                for pid in ids[4:]:
                    p = all_pois.get(pid)
                    if p:
                        truncated.append({
                            "name": p["name"], "day": d,
                            "reason": "慢节奏行程：单天超过 4 个点已截断（保留前 4 个）"})
                day_map[d] = ids[:4]
        if truncated:
            grounding["slow_truncated"] = truncated
    return day_map, themes, grounding


def forced_ids_for(anchor_poi: dict | None, grounding: dict) -> set:
    """求解器「必选点」集合（唯一口径，2026-09-15）。

    两类来源：
      1. 住宿锚点地标 —— 仅在 `anchor_hard_guarantee` 开关打开时（顺带约定，可关）
      2. **用户点名点位** —— 不受开关控制（硬需求）

    为什么点名点必须进必选：忠实执行模式下所有主选利润相同，求解器剔除时先丢利润最低者，
    而注入点排在队尾（rank=0）恰好第一个被丢（实测 GZ069 注入后立刻被剔除 → 行程里根本没有
    用户点名的地方）。用户点名是硬需求：「要丢就丢 LLM 提的点，不是丢它」。
    注意 forced 只抬利润、**不突破时间硬约束**——点本身不可行时照旧被剔，由 plan() 末尾的
    `named_lost` 对账披露。

    ⚠️ 口径用 `named_pois`（需求点名的**全量**库内点位），不是 `named_injected`（本次注入的
    那些）——LLM 恰好自己提案了点名点时 named_injected 为空，用后者会让该点既不被必选、
    也不被对账（踩过的坑，详见 `_inject_named_pois` docstring）。
    """
    forced = ({anchor_poi["id"]}
              if (anchor_poi and hotel_mod.hard_guarantee_enabled()) else set())
    named = grounding.get("named_pois") or grounding.get("named_injected") or []
    forced |= {n["id"] for n in named}
    return forced


def _compose_m7(city: dict, query: str, days: int, day_map: dict, themes: dict,
                date0: str | None, hotel: dict | None, anchor_poi: dict | None,
                grounding: dict, alt_map: dict | None,
                time_limit_s: float, main_bonus: float, soft_w: float,
                progress=None, reuse_days: dict | None = None) -> dict:
    """M7 复用 M2 compose（TOPTW + 修复链 + 文案），参数固定便于闭环重算。"""
    forced = forced_ids_for(anchor_poi, grounding)
    return m2_planner.compose(city, query, days, day_map, themes, use_llm=True,
                              date0=date0, hotel=hotel, time_limit_s=time_limit_s,
                              main_bonus=main_bonus, soft_w=soft_w,
                              progress=progress, reuse_days=reuse_days,
                              alt_map=alt_map,
                              meta={"candidates": None, "invalid_poi_ids": [],
                                    "n_dup_across_days": grounding["dup_skipped"],
                                    "hotel": ({"name": hotel["name"], "lat": hotel["lat"],
                                               "lng": hotel["lng"], "resolved": hotel["note"]}
                                              if hotel else None)},
                              mode="m7_proposal",
                              forced_ids=forced or None)


def plan(city: dict, query: str, days: int = 2, use_llm: bool = True,
         date0: str | None = None, hotel_text: str | None = None,
         time_limit_s: float = m2_planner.toptw.TIME_LIMIT_S,
         main_bonus: float = m2_planner.toptw.MAIN_BONUS,
         soft_w: float = m2_planner.toptw.SOFT_W, progress=None) -> dict:
    def _report(stage: str) -> None:
        if progress:
            try:
                progress(stage)
            except Exception:  # noqa: 进度上报失败不拖垮规划
                pass

    if not use_llm or not llm_client.llm_available():
        r = m1_planner.plan(city, query, days, use_llm=False, date0=date0,
                            hotel_text=hotel_text)
        r["mode"] = r["mode"] + " → m7_degraded"
        return r
    t0 = time.time()
    # ---- A 提案：世界知识自由生成（允许库外补充；注入库内菜单引导优先选用）----
    _report("proposal")
    all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}
    hint = _library_hint(all_pois)
    wd1 = poi_db.trip_weekday(date0, 1) if date0 else None
    date_line = (f"出发日期：{date0}（第 1 天为{wd1}），请结合常见闭馆常识安排顺序。\n" if date0 else "")
    # P2-5 天气感知：雨天意图 → 引导室内场馆优先、减少露天点位
    weather_line = ("天气提示：需求含雨天/下雨，请优先安排室内场馆（博物馆/美术馆/科技馆/室内乐园/商场等），"
                    "减少露天观景台、户外步道类点位。\n") if re.search(r"下雨|雨天|降雨|暴雨", query) else ""
    norm_q = re.sub(r"\s+", "", query).lower()
    # v10 按需注入：规则集随需求变化，必须进缓存键——否则「同 query 不同规则集」
    # 会命中同一条缓存（如 hotel_text 有无导致住宿锚点规则进出）
    _rules, _n_stops, _rule_hits = select_rules(query, all_pois, hotel_text)
    pkey = hashlib.sha1(json.dumps(
        [PROPOSE_PROMPT_VER, city["city"], days, norm_q, date0 or "",
         bool(weather_line), len(all_pois), sorted(_rule_hits)],
        ensure_ascii=False).encode("utf-8")).hexdigest()
    cached = _pcache_get(pkey)
    if cached is not None:
        proposal = cached
    else:
        raw = llm_client.chat([
            {"role": "system", "content": PROPOSE_SYSTEM},
            {"role": "user", "content": PROPOSE_PROMPT.format(city=city["city"], days=days,
                                                              query=query, date_line=date_line,
                                                              weather_line=weather_line,
                                                              n_stops=_n_stops,
                                                              rules="\n".join(_rules),
                                                              library_hint=hint)}],
            temperature=0.2, seed=42)
        proposal = llm_client.parse_json_safe(raw)
        if proposal.get("days"):
            _pcache_put(pkey, proposal)
    if not proposal.get("days"):
        r = m1_planner.plan(city, query, days, use_llm=True, date0=date0,
                            hotel_text=hotel_text)
        r["mode"] = "m7_proposal_failed → m2_fallback"
        return r

    # ---- B 落地 + 闭环反馈：未落地 gaps → LLM 修正 → 再落地（共享轮次预算）----
    _report("grounding")
    # 慢节奏提案层截断：LLM 对「每天 2-3 点、不超 4」遵循不足（「带老人」单词触发下
    # 仍排 5 点）。在落地匹配前把每天 stops 截到 4（保 rank 靠前的点），使提案展示、
    # day_map、TOPTW 输入三者一致——fixups 的 slow_truncated 只兜后续环节的超员
    _prop_truncated = []
    if m2_planner.SLOW_PACE_RE.search(query or ""):
        for d in proposal.get("days", []):
            stops = d.get("stops") or []
            if len(stops) > 4:
                _prop_truncated.append({
                    "day": d.get("day"),
                    "dropped": [s.get("name", "") for s in stops[4:]]})
                d["stops"] = stops[:4]
    day_map, themes, grounding = _ground(proposal, city, all_pois, days)
    if _prop_truncated:
        grounding["slow_proposal_truncated"] = _prop_truncated
    rounds_used = 0
    while (rounds_used < MAX_REVISE_ROUNDS
           and (grounding["gaps"] or grounding.get("dups")
                or grounding["grounding_rate"] < GROUNDING_RATE_MIN)):
        revised = _revise_proposal(city, query, days, proposal,
                                   grounding["gaps"], [], hint,
                                   dups=grounding.get("dups"))
        if revised is None:
            break  # LLM 不可用/解析失败，保留原案走离线补强
        dm2, th2, g2 = _ground(revised, city, all_pois, days)
        rounds_used += 1
        if not dm2 or g2["grounding_rate"] < grounding["grounding_rate"]:
            break  # 修正无益（更差/清空），保留原案
        proposal, day_map, themes, grounding = revised, dm2, th2, g2
    grounding["revise_rounds"] = rounds_used
    alt_map = grounding.pop("alt_map", {})

    # 落地后确定性修整：锚点注入 → 缺天补齐 → 单天补强
    anchor_poi = hotel_mod.match_landmark_poi(city, hotel_text)
    day_map, themes, grounding = _post_ground_fixups(
        day_map, themes, grounding, city, all_pois, days, query, anchor_poi)
    # 美食保障（报障 13 改进 A + 报障 15 残余）：主选层餐窗对账——同窗超额与
    # 「当天排不进」的餐厅提前降级，防 sequencer 剔点后前端提示美食未进餐窗
    day_map = _reconcile_food_windows(day_map, all_pois, city, query, grounding)
    if not day_map:  # 全部落地失败 → M2 兜底
        r = m2_planner.plan(city, query, days, use_llm=True, date0=date0,
                            hotel_text=hotel_text, time_limit_s=time_limit_s,
                            main_bonus=main_bonus, soft_w=soft_w)
        r["mode"] = "m7_grounding_empty → m2_fallback"
        r["proposal"] = proposal
        r["grounding"] = grounding
        return r

    # ---- C 求解：复用 M2 compose（TOPTW + 修复链 + 文案）----
    hotel = hotel_mod.resolve_hotel(city, hotel_text)
    r = _compose_m7(city, query, days, day_map, themes, date0, hotel,
                    anchor_poi, grounding, alt_map, time_limit_s, main_bonus, soft_w,
                    progress=progress)
    # 闭环第二触发点：约束剔除过多 / 相邻两天片区重复 → 带原因反馈修正 → 重落地重求解（一轮）
    # 注意口径：每日 dropped 只有修复链剔除；主选被 TOPTW 剔除在 r["mains_dropped"]——
    # 求解器剔掉 LLM 主选 = 世界知识被时间预算否决（如宋城/迪士尼），是最需要闭环的信号
    def _collect_issues(res) -> tuple[list, list]:
        drops = [{"name": dr.get("name", ""), "reason": dr.get("reason", ""), "day": d.get("day")}
                 for d in res["itinerary"]["days"] for dr in d.get("dropped", [])]
        seen_d = {x["name"] for x in drops}
        drops += [{"name": x.get("name", ""), "reason": x.get("reason", ""), "day": x.get("day")}
                  for x in res.get("mains_dropped", []) if x.get("name") not in seen_d]
        day_ids = {d["day"]: [s["id"] for s in d["timeline"] if s["type"] == "poi"]
                   for d in res["itinerary"]["days"]}
        return drops, _crossday_area_overlap(day_ids, all_pois)

    drops, overlap = _collect_issues(r)
    # 触发口径：剔除 ≥2 条，或存在任何跨天片区重复（单条即明显不合理，如武康路连去两天）
    if rounds_used < MAX_REVISE_ROUNDS and (len(drops) >= DROP_FEEDBACK_MIN or overlap):
        revised = _revise_proposal(city, query, days, proposal,
                                   grounding["gaps"], drops + overlap, hint)
        if revised is not None:
            dm2, th2, g2 = _ground(revised, city, all_pois, days)
            if dm2 and g2["grounding_rate"] >= grounding["grounding_rate"]:
                alt_map2 = g2.pop("alt_map", {})
                dm2, th2, g2 = _post_ground_fixups(
                    dm2, th2, g2, city, all_pois, days, query, anchor_poi)
                # 餐窗对账要同样作用于修正案：首轮对账只覆盖首轮 day_map，漏掉这里
                # 会让 revise 后新提案里「当天排不进」的餐厅重新被剔并弹提示
                if dm2:
                    dm2 = _reconcile_food_windows(dm2, all_pois, city, query, g2)
                if dm2:
                    # P1 增量重算：修正前后点位集合一致的天复用上次最终解与文案，
                    # 只对变化的天重解+重写文案（全变则等价全量，行为不劣化）
                    prev_days = {
                        d["day"]: {"ids": [s["id"] for s in d["timeline"] if s["type"] == "poi"],
                                   "copy": {"theme": d.get("theme", ""),
                                            "reason": d.get("reason", ""),
                                            "tips": d.get("tips", [])}}
                        for d in r["itinerary"]["days"]}
                    reuse_days = {d: rec for d, rec in prev_days.items()
                                  if rec["ids"] and dm2.get(d)
                                  and set(dm2[d]) == set(rec["ids"])}
                    # C 闭环重算是设计内的第二轮质量优化（非出错回退）：进度阶段
                    # 加 ":r2" 轮次后缀，前端据此显示「质量优化（第 2 轮）」而非
                    # 让用户误以为进度条倒退卡死
                    def _r2_progress(stage: str) -> None:
                        if progress:
                            try:
                                progress(stage + ":r2")
                            except Exception:  # noqa: 进度上报失败不拖垮规划
                                pass
                    r2 = _compose_m7(city, query, days, dm2, th2, date0, hotel,
                                     anchor_poi, g2, alt_map2,
                                     time_limit_s, main_bonus, soft_w,
                                     progress=_r2_progress if progress else None,
                                     reuse_days=reuse_days or None)
                    drops2, overlap2 = _collect_issues(r2)
                    # 修正确实减少问题总数（剔除+片区重复）才采纳，防震荡
                    if len(drops2) + len(overlap2) < len(drops) + len(overlap):
                        r = r2
                        proposal, grounding, alt_map = revised, g2, alt_map2
                        grounding["revise_rounds"] = rounds_used + 1
    final_day_ids = {d["day"]: [s["id"] for s in d["timeline"] if s["type"] == "poi"]
                     for d in r["itinerary"]["days"]}
    r["area_overlap"] = _crossday_area_overlap(final_day_ids, all_pois)
    final_kept_ids = {i for ids in final_day_ids.values() for i in ids}
    r["proposal"] = proposal
    r["grounding"] = grounding
    # 用户可读通知：compose 层需求满足检测 + 提案层独占日建议 + 天气/节假日提示
    from src import weather as weather_mod
    r["notices"] = (list(r.get("notices") or [])
                    + list(grounding.get("far_big_solo") or [])
                    + weather_mod.trip_notices(city, date0, days))
    # 点名对账（2026-09-15）：**需求点名**的库内点位仍可能被硬约束剔掉（时间预算/里程/
    # 闭馆/亲子）——必须显式披露，否则用户以为系统无视了他的明确要求。
    # 口径是 named_pois（全量点名点），不是 named_injected（只记本次注入的）：LLM 恰好自己
    # 提案了点名点时 named_injected 为空，用后者会「用户点名被剔却零提示」。
    _named_all = grounding.get("named_pois") or grounding.get("named_injected") or []
    _lost_named = [n for n in _named_all if n["id"] not in final_kept_ids]
    if _lost_named:
        r["notices"].append({
            "type": "named_lost", "dropped": [n["name"] for n in _lost_named],
            "message": (f"你点名的 {'、'.join(n['name'] for n in _lost_named)} "
                        f"因时间/里程/闭馆等硬约束未能排入，可考虑单独安排半天或减少其它点位")})
    r["latency_s"] = round(time.time() - t0, 1)  # A+B+C 全链路耗时
    return r
