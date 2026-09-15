# -*- coding: utf-8 -*-
"""2026-09-15 点名/远郊召回缺陷：确定性复现/回归。

缺陷现象：query「广州2天，晚上想去宝业路宵夜街吃宵夜」→ LLM 提案 13 个点里**一个都不是它**，
而落地率 100%（库内完全可落地）→ 该点在 TOPTW 池里从未出现（`toptw_dropped` 为空 =
**能排但从不被选**）。SZ062 莲花岛同族。两条独立成因：

  1. **召回层**：远郊特色点（`area=suburb`）在库内菜单里只有名字，LLM 无从判断远近，
     长期不被提案 → 菜单加「（郊区）」标注 + prompt 加「远郊深度体验规则」；
     用户**点名**的点位则完全不赌 LLM——`_inject_named_pois` 确定性注入。
  2. **必选与对账口径**：忠实模式下所有主选利润相同，注入点排队尾（rank=0）恰好第一个被
     求解器丢弃（实测 GZ069 注入后立刻被剔）→ 点名点与住宿锚点同列 `forced`；被发现剔除时
     必须 `named_lost` 披露。**口径是「需求点名的全量库内点位」（`named_pois`），不是
     「本次注入了什么」（`named_injected`）**——LLM 恰好自己提案了点名点时注入会被跳过，
     用后者会「用户点名被剔却零提示」（苏州「1天想去莲花岛」实测）。
  3. **下游守门不得吞掉目标**：`m2_planner._fix_food_detours`（美食配套绕行守门）的前提是
     「咖啡是配套不是目标」，但**莲花岛本身 `category=food`（阳澄湖农家乐集群、suburb、
     dur 2h）** 被判成「造成绕路的配套餐饮」→ 换成市内餐厅，点名点被静默顶掉。修＝
     `_is_destination_food`（郊区/长停留/农家乐类豁免）+ `protect`（用户点名点豁免）。

另含三个「夜间型点被 horizon 判死」的回归：餐块预扣 `MEAL_BUFFER_MIN=120` 把求解器
预算压到 630min（19:30），18:00 后开门的点结构性排不进（GZ069/GZ066 同族）。

用法：python scripts/test_named_inject.py
"""
import io, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src import poi_db, proposal_planner as pp, toptw  # noqa: E402

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


def _city_pois(name):
    c = poi_db.load_city(name)
    return c, {p["id"]: p for p in (poi_db.parse_poi(x, c) for x in c["pois"])}


GZ, GZ_ALL = _city_pois("广州")
SH, SH_ALL = _city_pois("上海")
SZ, SZ_ALL = _city_pois("苏州")


# ==================== 用例 1：names_mentioned_in 点名识别 ====================
print("== 用例 1：点名识别（norm_name / core_name / 长度门槛）")
_q1 = "广州2天，晚上想去宝业路宵夜街吃宵夜，白天逛沙面岛"
hits1 = [p["id"] for p in poi_db.names_mentioned_in(_q1, list(GZ_ALL.values()))]
print("  命中:", [(p["id"], p["name"]) for p in poi_db.names_mentioned_in(_q1, list(GZ_ALL.values()))])
case("1a 点名的宵夜街被识别", "GZ069" in hits1, str(hits1))
case("1b 点名的沙面岛被识别（含括号补充名主干匹配）", "GZ034" in hits1, str(hits1))
case("1c 未点名的点位不被误收",
     len(hits1) == 2, f"hits={hits1}")

# 长度门槛：2 字名（外滩/西湖）不判——「去外滩看看」里的「外滩」太通用，
# 若判命中会把「外滩」类地名变成强制必选，误伤面远大于收益
hits2 = [p["id"] for p in poi_db.names_mentioned_in("上海2天，想去外滩看看", list(SH_ALL.values()))]
case("1d 2 字点名（外滩）不触发（长度门槛 ≥3）", not hits2, str(hits2))
case("1e norm_name 与落地匹配同口径（去括号/空格/大小写）",
     poi_db.norm_name("广州塔（小蛮腰）") == poi_db.norm_name("广州塔(小蛮腰)")
     and pp._norm("a·B") == "ab", f"{poi_db.norm_name('广州塔（小蛮腰）')}")
case("1f core_name 去括号补充",
     poi_db.core_name("广州塔（小蛮腰）") == "广州塔", poi_db.core_name("广州塔（小蛮腰）"))


# ==================== 用例 2：_inject_named_pois 确定性注入 ====================
print("\n== 用例 2：确定性注入（不依赖 LLM 是否采纳）")
dm = {1: ["GZ007"], 2: ["GZ034"]}
g = {}
inj = pp._inject_named_pois(dm, GZ_ALL, _q1, g)
print("  注入:", [(p["id"], p["name"], d) for p, d in inj], "| grounding:", g.get("named_injected"))
case("2a 点名的宵夜街被注入", any(p["id"] == "GZ069" for p, _ in inj), str([p["id"] for p, _ in inj]))
case("2b 注入写入 grounding.named_injected（可审计）", bool(g.get("named_injected")),
     str(g.get("named_injected")))
case("2c 注入落进某一天的点位列表",
     any("GZ069" in dm.get(d, []) for d in dm), str(dm))
case("2d 注入天为几何最近的一天（同片区顺路）",
     all(dm.get(d) and "GZ069" in dm[d] for p, d in inj if p["id"] == "GZ069"),
     str(dm))

_q_named = "广州2天，晚上想去宝业路宵夜街吃宵夜"
dm2 = {1: ["GZ069"], 2: ["GZ007"]}
g2 = {}
inj2 = pp._inject_named_pois(dm2, GZ_ALL, _q_named, g2)
case("2e 已在行程内的点名点不重复注入",
     not inj2 and g2.get("named_injected") is None,
     f"inj={[p['id'] for p, _ in inj2]} g={g2.get('named_injected')}")
# ⚠️ 二次修正回归（2026-09-15 踩坑）：LLM **自己提案了点名点**时，注入被跳过 →
# named_injected 为空。若 forced/对账都读 named_injected，该点就既不被必选、被剔后也不
# 披露（苏州「1天想去莲花岛」实测）。故 named_pois 必须始终记录需求点名的**全量**点位。
case("2g LLM 已提案点名点时，named_pois 仍记录全量（对账口径不丢）",
     [n["id"] for n in (g2.get("named_pois") or [])] == ["GZ069"],
     str(g2.get("named_pois")))

# 对照：需求未点名任何库内点位 → 不注入（不误伤普通 query）
dm3 = {1: ["GZ007"], 2: ["GZ034"]}
g3 = {}
inj3 = pp._inject_named_pois(dm3, GZ_ALL, "广州2天经典深度游", g3)
case("2f 对照：未点名 → 零注入", not inj3 and not g3.get("named_injected"),
     f"inj={inj3} g={g3.get('named_injected')}")


# ==================== 用例 3：必选集合口径（forced_ids_for） ====================
# 单纯注入不够：忠实模式所有主选利润相同，注入点排队尾（rank=0）会被求解器第一个丢掉
# （实测 GZ069 注入后立刻被剔）。必选口径抽成纯函数 `forced_ids_for`，这里验契约 +
# 源码守卫（防未来重构把 wiring 改漏）。
# 说明：forced 只抬利润、不突破时间硬约束——点本身不可行时照旧被剔，由 `named_lost` 披露。
print("\n== 用例 3：必选集合口径 + wiring 源码守卫")
case("3a 无锚点无点名 → 空集", pp.forced_ids_for(None, {}) == set(),
     str(pp.forced_ids_for(None, {})))
case("3b 点名的点进入必选",
     pp.forced_ids_for(None, {"named_pois": [{"id": "GZ069", "name": "宝业路宵夜街"}]}) == {"GZ069"},
     str(pp.forced_ids_for(None, {"named_pois": [{"id": "GZ069", "name": "宝业路宵夜街"}]})))
case("3b2 兼容旧字段 named_injected（防御性回退）",
     pp.forced_ids_for(None, {"named_injected": [{"id": "GZ069"}]}) == {"GZ069"},
     str(pp.forced_ids_for(None, {"named_injected": [{"id": "GZ069"}]})))
case("3b3 named_pois 优先于 named_injected（两字段并存时以全量口径为准）",
     pp.forced_ids_for(None, {"named_pois": [{"id": "GZ069"}],
                              "named_injected": [{"id": "GZ007"}]}) == {"GZ069"},
     str(pp.forced_ids_for(None, {"named_pois": [{"id": "GZ069"}],
                                  "named_injected": [{"id": "GZ007"}]})))
case("3c 点名为空列表 → 不误加",
     pp.forced_ids_for(None, {"named_pois": []}) == set(),
     str(pp.forced_ids_for(None, {"named_pois": []})))
_hg = pp.hotel_mod.hard_guarantee_enabled
_anchor = {"id": "GZ001"}
_named = {"named_pois": [{"id": "GZ069", "name": "宝业路宵夜街"}]}
try:
    pp.hotel_mod.hard_guarantee_enabled = lambda: True     # 开关打开
    case("3d 锚点开关打开 → 锚点与点名取并集",
         pp.forced_ids_for(_anchor, _named) == {"GZ001", "GZ069"},
         str(pp.forced_ids_for(_anchor, _named)))
    pp.hotel_mod.hard_guarantee_enabled = lambda: False    # 开关关闭（默认）
    case("3e 开关关闭 → 锚点不强制、点名仍强制（点名是硬需求，不受开关控制）",
         pp.forced_ids_for(_anchor, _named) == {"GZ069"}
         and pp.forced_ids_for(_anchor, {}) == set(),
         f"有开关={pp.forced_ids_for(_anchor, _named)} 无开关={pp.forced_ids_for(_anchor, {})}")
finally:
    pp.hotel_mod.hard_guarantee_enabled = _hg

_src = open(os.path.join(ROOT, "src", "proposal_planner.py"), encoding="utf-8").read()
case("3f 源码守卫：_compose_m7 走 forced_ids_for（唯一口径）",
     "forced = forced_ids_for(anchor_poi, grounding)" in _src, "proposal_planner._compose_m7")
case("3g 源码守卫：forced_ids_for 读 named_pois（全量口径，带 named_injected 回退）",
     'named = grounding.get("named_pois") or grounding.get("named_injected") or []' in _src,
     "proposal_planner.forced_ids_for")
case("3h 源码守卫：注入点在落地后修整链最前（后续 gate 可见）",
     "_inject_named_pois(day_map, all_pois, query, grounding)" in _src, "_post_ground_fixups")
case("3i 源码守卫：named_pois 无条件记录全量点名点（不依赖是否注入）",
     'grounding["named_pois"] = [{"id": p["id"], "name": p["name"]' in _src,
     "proposal_planner._inject_named_pois")
case("3j 源码守卫：点名对账读 named_pois（丢失必须披露，不静默）",
     '"type": "named_lost"' in _src
     and '_named_all = grounding.get("named_pois") or grounding.get("named_injected")' in _src,
     "plan() 末尾")


# ==================== 用例 4：夜间型点 horizon 判死 ====================
# 餐块预扣 MEAL_BUFFER_MIN=120 把预算压到 630min：GZ066（19:00 开门 + 1.5h）需要
# to_min(19:00)=600 + 90 = 690 > 630 → 恒不可行。放宽后按池内最晚需求给预算。
print("\n== 用例 4：夜间型点不被 horizon 判死")
_late = [GZ_ALL["GZ069"], GZ_ALL["GZ066"]]
_allp4 = {p["id"]: p for p in _late}
_ids4 = [p["id"] for p in _late]

_keep = toptw.LATE_POINT_MARGIN_MIN
toptw.LATE_POINT_MARGIN_MIN = -10 ** 9      # 对照：等价于关闭放宽（放宽条件恒为假）
try:
    ctl = toptw.solve_day(_late, _ids4, GZ, _allp4, lock_mains=True)[0]
finally:
    toptw.LATE_POINT_MARGIN_MIN = _keep
fix = toptw.solve_day(_late, _ids4, GZ, _allp4, lock_mains=True)[0]
print("  对照(无放宽):", ctl, "| 修复后:", fix)
case("4a 对照：19:00 开门的夜游点被 horizon 判死", "GZ066" not in ctl, str(ctl))
case("4b 修复后：夜游点入选", "GZ066" in fix, str(fix))
case("4c 18:00 开门的宵夜街两版都在（放宽只补不该判死的点）",
     "GZ069" in ctl and "GZ069" in fix, f"ctl={ctl} fix={fix}")
case("4d 放宽上限不超过真实日窗（不破坏防过度打包）",
     toptw.LATE_POINT_MARGIN_MIN == 60 and
     toptw.MEAL_BUFFER_MIN == 120, f"margin={toptw.LATE_POINT_MARGIN_MIN}")

# ==================== 用例 5：美食绕行守门不得吞掉「目的地型餐饮」 ====================
# 真实案例：苏州「1天，想去莲花岛吃大闸蟹」。莲花岛在库里是 category=food（阳澄湖农家乐
# 集群）、area=suburb、dur=2h → 被 _fix_food_detours 当成「造成绕路的配套餐饮」换成市内
# 餐厅，用户点名的核心体验被静默顶掉（剔除记录还被吞，行程里看不出发生过什么）。
print("\n== 用例 5：_is_destination_food 判据 + _fix_food_detours 豁免")
from src import m2_planner as m2  # noqa: E402
case("5a 莲花岛（suburb food，dur 2h）判为目的地型餐饮",
     m2._is_destination_food(GZ_ALL.get("GZ069")) is not None
     and m2._is_destination_food(SZ_ALL["SZ062"]) is True,
     f"SZ062 area={SZ_ALL['SZ062'].get('area')} dur={SZ_ALL['SZ062'].get('dur')}")
case("5b 市内短停留餐厅不判为目的地型（守门仍有效）",
     m2._is_destination_food({"category": "food", "name": "某某面馆", "area": "center",
                              "dur": 1.0}) is False, "")
case("5c 郊区餐饮一律判目的地型（郊区不可能是顺路配套）",
     m2._is_destination_food({"category": "food", "name": "某咖啡馆", "area": "suburb",
                              "dur": 0.5}) is True, "")
case("5d 长停留（≥2h）判目的地型",
     m2._is_destination_food({"category": "food", "name": "某饭庄", "area": "center",
                              "dur": 2.0}) is True, "")
case("5e 农家乐类名判目的地型",
     m2._is_destination_food({"category": "food", "name": "阳澄湖蟹庄", "area": "center",
                              "dur": 1.0}) is True, "")

_dm5 = {1: ["SZ005", "SZ062", "SZ021"]}          # 山塘街 + 莲花岛 + 松鹤楼（市内正餐）
_allp5 = dict(SZ_ALL)
_fix5, fx5 = m2._fix_food_detours({d: list(v) for d, v in _dm5.items()}, SZ, _allp5,
                                   None, None, None, protect={"SZ062"})
case("5f 点名/目的地型餐饮不被换成市内餐厅",
     "SZ062" in _fix5[1], f"day={[SZ_ALL[i]['name'] for i in _fix5[1]]} fixes={fx5}")

# 对照：关掉目的地判据且不给 protect → 复现缺陷（莲花岛被换掉）
_dest_orig = m2._is_destination_food
m2._is_destination_food = lambda p: False
try:
    _fix5c, fxc = m2._fix_food_detours({d: list(v) for d, v in _dm5.items()}, SZ, _allp5,
                                       None, None, None, protect=set())
finally:
    m2._is_destination_food = _dest_orig
print("  对照 day:", [SZ_ALL[i]["name"] for i in _fix5c[1]], "| fixes:", fxc)
case("5g 对照：无豁免时点名餐饮被顶替（缺陷可复现）",
     "SZ062" not in _fix5c[1], f"day={[SZ_ALL[i]['name'] for i in _fix5c[1]]}")

_src_m2 = open(os.path.join(ROOT, "src", "m2_planner.py"), encoding="utf-8").read()
case("5h 源码守卫：compose 把点名点作为 protect 传给守门",
     "protect={p[\"id\"] for p in poi_db.names_mentioned_in(query or \"\", all_pois.values())}" in _src_m2,
     "m2_planner.compose 阶段3.4")

print("\n" + ("全部用例通过" if not FAIL else f"失败用例：{FAIL}"))
sys.exit(1 if FAIL else 0)