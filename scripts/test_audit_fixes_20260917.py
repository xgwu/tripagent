# -*- coding: utf-8 -*-
"""2026-09-17 设计审查六个缺陷的修复守卫。

逐条对应审查结论，每条都带「复现旧缺陷的对照」，防止静默回退：

  1（高）TOPTW slack_max=0 禁止等待 → 19:00 开门的点结构性不可行。
         即使忠实模式给主选 1e6 利润也救不回来（利润解决不了不可行）。
         修复：slack 改为有界 WAIT_SLACK_MAX_MIN=120（与 GAP_WAIT_MAX_H 同口径）。
         注：不叠加跨度代价——实测压不掉结构性空档却会吃掉 POI。
  2（中）落地链包含匹配按 rating 取胜 → 短名劫持长提案名
         （「拙政园与狮子林」只落地拙政园，后半个点静默丢失）。
         修复：按覆盖率取胜 + 并列地名拆分（extra_ids）。
  3（中）跨天重复被静默吞掉且不降落地率 → 落地率显示 100% 但行程少了点，
         闭环不触发。修复：计入落地率 + dups 台账 + 回传 LLM 修正。
  4（低）toptw 注释声称 rank 是「相似度目标」，实际只影响选点不影响顺序，
         且忠实模式下被 1e6 淹没。修复：口径纠正（仅注释）。
  5（低）add_city._rating 只取评分首位数字（4.7→4.0）→ top-80 截断退化成
         按名字排序。修复：完整浮点解析 + 归一化到 {3,4,5}。
  6（低）extract_days 对「6天」返回 None → 静默变 2 天。
         修复：解析真实值 + clamp_days 显式告知截断。

用法：python scripts/test_audit_fixes_20260917.py
"""
import os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

# add_city 在导入期替换 sys.stdout（其 :35），必须最先导入，
# 否则它会把本脚本前面缓冲未刷的输出整段丢弃（本次实测踩到）。
import add_city as ac  # noqa: E402
from src import poi_db, sequencer, toptw, proposal_planner as pp  # noqa: E402
from src.query_days import extract_days, clamp_days, MAX_DAYS  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


CITY = {"day_start": "09:00", "day_end": "21:30", "center": {"lat": 30.25, "lng": 120.16}}


def _p(pid, name, o, c, dur, lat=30.250, lng=120.160, rating=4):
    return {"id": pid, "name": name, "open_h": o, "close_h": c, "dur": dur,
            "lat": lat, "lng": lng, "rating": rating, "best_time": "any",
            "category": "x", "area": "central"}


# ==================== 缺陷 1：TOPTW 允许有界等待 ====================
print("== 缺陷 1（高）：TOPTW slack —— 夜间点不再结构性不可行")
A = _p("T1", "上午馆", 9.0, 17.0, 2.0)
B = _p("T2", "下午馆", 9.0, 17.0, 1.5, lat=30.252, lng=120.162)
N = _p("T3", "夜市", 19.0, 23.0, 1.5, lat=30.251, lng=120.161)
ALL3 = {p["id"]: p for p in (A, B, N)}

o, d, s = toptw.solve_day([A, B, N], ["T1", "T2", "T3"], CITY, ALL3, lock_mains=True)
case("1a 上午+下午+夜间 三点全部排入", len(o) == 3 and "T3" in o,
     f"ordered={o} dropped={d}")

o2, _, _ = toptw.solve_day([A, N], ["T1", "T3"], CITY, ALL3, lock_mains=True)
case("1b 上午+夜间 两点也能排入（最小复现场景）", len(o2) == 2, f"ordered={o2}")

case("1c slack 有界且与 GAP_WAIT_MAX_H(2h) 同口径",
     toptw.WAIT_SLACK_MAX_MIN == 120, f"={toptw.WAIT_SLACK_MAX_MIN}")

# 复现旧缺陷：把 slack 调回 0，夜间点必被丢 —— 证明这不是「碰巧能排」
_orig = toptw.WAIT_SLACK_MAX_MIN
try:
    toptw.WAIT_SLACK_MAX_MIN = 0
    o_old, d_old, _ = toptw.solve_day([A, B, N], ["T1", "T2", "T3"], CITY, ALL3,
                                      lock_mains=True)
    case("1d 对照：slack=0 时夜间点必被丢（旧缺陷复现）",
         "T3" in d_old and len(o_old) < 3, f"ordered={o_old} dropped={d_old}")
finally:
    toptw.WAIT_SLACK_MAX_MIN = _orig

case("1e 不叠加跨度代价（实测会吃掉 POI 且压不掉结构性空档）",
     not hasattr(toptw, "SPAN_COST_W"))

# ==================== 缺陷 2：包含匹配按覆盖率 + 并列拆分 ====================
print("\n== 缺陷 2（中）：落地链不再被短名劫持")
_c = poi_db.load_city("苏州")
_all = {p["id"]: p for p in (poi_db.parse_poi(p, _c) for p in _c["pois"])}
_lodg = {r["id"] for r in _c["pois"] if pp._is_lodging(r)}
_by = [p for p in _all.values() if p["id"] not in _lodg]

pid, method, extra = pp._ground_one("拙政园与狮子林", _all, _by)
case("2a 并列地名两个点都落地（旧实现丢掉狮子林）",
     pid and extra and len(extra) == 1,
     f"主={_all[pid]['name'] if pid else None} 额外={[_all[e]['name'] for e in extra]} [{method}]")
case("2b 并列拆分标记为 split", method.startswith("split"), method)

pid2, m2, _ = pp._ground_one("拙政园", _all, _by)
case("2c 单地名精确匹配不受影响", m2 == "exact" and _all[pid2]["name"] == "拙政园", m2)

# 低覆盖率的包含匹配必须被标注出来（而不是伪装成可信命中）
_sh = poi_db.load_city("上海")
_shall = {p["id"]: p for p in (poi_db.parse_poi(p, _sh) for p in _sh["pois"])}
_shby = [p for p in _shall.values()
         if p["id"] not in {r["id"] for r in _sh["pois"] if pp._is_lodging(r)}]
pid3, m3, _ = pp._ground_one("外滩历史建筑群", _shall, _shby)
case("2d 短名低覆盖匹配被显式标注 contain_low", m3.startswith("contain_low"),
     f"{_shall[pid3]['name'] if pid3 else None} [{m3}]")
case("2e 覆盖率阈值存在且合理", 0.4 <= pp.CONTAIN_COVER_MIN <= 0.8,
     f"CONTAIN_COVER_MIN={pp.CONTAIN_COVER_MIN}")

# ==================== 缺陷 3：跨天重复计入落地率 ====================
print("\n== 缺陷 3（中）：跨天重复不再被静默吞掉")
_prop = {"days": [
    {"day": 1, "theme": "园林", "reason": "", "stops": [
        {"name": "拙政园", "note": ""}, {"name": "狮子林", "note": ""}]},
    {"day": 2, "theme": "重复", "reason": "", "stops": [
        {"name": "拙政园", "note": ""}, {"name": "留园", "note": ""}]},
]}
_dm, _th, _g = pp._ground(_prop, _c, _all, 2)
case("3a 重复点被计数", _g["dup_skipped"] == 1, f"dup_skipped={_g['dup_skipped']}")
case("3b 重复点进 dups 台账（含 day/poi_id）",
     len(_g["dups"]) == 1 and _g["dups"][0]["day"] == 2 and _g["dups"][0].get("poi_id"),
     str(_g["dups"]))
case("3c 落地率扣掉重复（旧口径会是 1.0）", _g["grounding_rate"] < 1.0,
     f"grounding_rate={_g['grounding_rate']}")
case("3d 重复会触发闭环修正",
     bool(_g["gaps"] or _g.get("dups")
          or _g["grounding_rate"] < pp.GROUNDING_RATE_MIN))
case("3e REVISE_PROMPT 含重复点的修正规则",
     "与前面某天重复" in pp.REVISE_PROMPT)
# 对照：无重复提案的落地率不受影响
_prop_ok = {"days": [{"day": 1, "theme": "", "reason": "", "stops": [
    {"name": "拙政园", "note": ""}, {"name": "狮子林", "note": ""}]}]}
_, _, _g2 = pp._ground(_prop_ok, _c, _all, 1)
case("3f 对照：无重复时落地率仍为 1.0", _g2["grounding_rate"] == 1.0,
     f"{_g2['grounding_rate']}")

# ==================== 缺陷 4：toptw 口径注释 ====================
print("\n== 缺陷 4（低）：RANK_BONUS 口径纠正")
import inspect  # noqa: E402
_tsrc = inspect.getsource(toptw)
case("4a 注释说明 rank 不影响访问顺序", "不影响访问顺序" in _tsrc)
case("4b 注释说明忠实模式下 rank 被 1e6 淹没",
     "淹没" in _tsrc and "1e6" in _tsrc)

# ==================== 缺陷 5：add_city 评分解析 ====================
print("\n== 缺陷 5（低）：add_city 评分解析与归一化")
case("5a 4.7 解析为 4.7（旧实现只取首位 → 4.0）",
     ac._rating({"biz_ext": {"rating": "4.7"}}) == 4.7)
case("5b 3.5 解析为 3.5", ac._rating({"biz_ext": {"rating": "3.5"}}) == 3.5)
case("5c 缺失/脏值回落 4.0",
     ac._rating({}) == 4.0 and ac._rating({"biz_ext": {"rating": "abc"}}) == 4.0)
case("5d 越界值（8.9）不污染排序", ac._rating({"biz_ext": {"rating": "8.9"}}) == 4.0)
_asrc = inspect.getsource(ac._rating)
# 只看代码行（去掉 docstring）：docstring 里会提到「cost 死代码」这件事本身
_acode = _asrc.split('"""')[-1]
case("5e 死代码 cost 赋值已删除", 'p.get("cost")' not in _acode)
_bsrc = inspect.getsource(ac.build_city_file)
case("5f 入库 rating 四舍五入并钳在 {3,4,5}",
     "round(p[\"rating\"])" in _bsrc and "min(5" in _bsrc and "max(3" in _bsrc)

# ==================== 缺陷 6：天数解析 ====================
print("\n== 缺陷 6（低）：天数解析不再静默改需求")
case("6a 「6天」解析出真实值 6（旧实现返回 None）", extract_days("上海和成都6天自驾联游") == 6)
case("6b 「8天」「十天」也能解析",
     extract_days("上海8天深度游") == 8 and extract_days("十天环游贵州") == 10)
d6, c6, w6 = clamp_days(extract_days("上海和成都6天自驾联游"))
case("6c 超上限收敛到 MAX_DAYS 且标记截断",
     d6 == MAX_DAYS and c6 is True and w6 == 6, f"days={d6} clamped={c6} want={w6}")
d4, c4, _ = clamp_days(extract_days("苏杭4天自驾联游"))
case("6d 上限内不标记截断", d4 == 4 and c4 is False)
dN, cN, _ = clamp_days(extract_days("上海随便玩玩"))
case("6e 提不到天数走默认且不标记截断", dN == 2 and cN is False)
case("6f 回归对照：「带5岁孩子」不误匹配成 5 天",
     extract_days("带5岁孩子去上海玩2天") == 2)
case("6g 回归对照：非天数量词不误匹配",
     extract_days("6人同行去上海") is None and extract_days("下午8点到上海") is None)
_ssrc = open(os.path.join(ROOT, "webui", "server.py"), encoding="utf-8").read()
case("6h server 单城路径已接 clamp_days（旧实现完全没 clamp）",
     "days, _clamped, _want = clamp_days(d)" in _ssrc)
case("6i 截断会作为 notice 透出给用户", '"kind": "days_clamped"' in _ssrc)

# ==================== 缺陷 7：prompt 规则堆积（v10 按需注入） ====================
# v9 把 14 条规则不分场景全量堆进 prompt，其中多条互相打架且靠散文声明优先级：
#   · 贯穿偏好规则 显式覆盖 跨天片区分散规则
#   · 慢节奏规则 声明豁免 傍晚密度规则
# v10 改为常开硬规则 + 条件规则按需注入，冲突从根源消失。
print("\n== 缺陷 7（中）：prompt 规则按需注入（v10）")
_ver = re.search(r'PROPOSE_PROMPT_VER = "v(\d+)"', inspect.getsource(pp))
case("7a 版本号已升到 v10+", _ver and int(_ver.group(1)) >= 10,
     f"v{_ver.group(1) if _ver else '?'}")

_r_plain, _ns_plain, _h_plain = pp.select_rules("上海2天经典深度游", None, None)
_r_slow, _ns_slow, _h_slow = pp.select_rules("上海2天，不要太累", None, None)
_r_fam, _ns_fam, _h_fam = pp.select_rules("带5岁孩子去上海玩2天", None, None)
_r_lake, _, _h_lake = pp.select_rules("苏州2天，最好临湖", None, None)
_r_anchor, _, _h_anchor = pp.select_rules("上海3日亲子游", None, "迪士尼附近")

case("7b 普通查询只注入常开规则 + 傍晚密度", _h_plain == [], str(_h_plain))
case("7c 慢节奏与傍晚密度互斥（v9 靠散文豁免，v10 直接不注入）",
     pp._RULE_SLOW in _r_slow and pp._RULE_EVENING not in _r_slow
     and pp._RULE_EVENING in _r_plain)
case("7d 亲子查询注入亲子 + 招牌体验",
     pp._RULE_FAMILY in _r_fam and pp._RULE_SIGNATURE in _r_fam
     and pp._RULE_FAMILY not in _r_plain)
case("7e 贯穿偏好仅在含「湖」时注入",
     pp._RULE_LAKE in _r_lake and pp._RULE_LAKE not in _r_plain)
case("7f 住宿锚点规则仅在有 hotel_text 时注入",
     pp._RULE_ANCHOR in _r_anchor and pp._RULE_ANCHOR not in _r_fam)
case("7g 每日停留点数随档位变化（慢节奏 2-3 / 亲子 4-5 / 默认 4-6）",
     _ns_slow.startswith("2-3") and _ns_fam.startswith("4-5")
     and _ns_plain.startswith("4-6"),
     f"slow={_ns_slow} family={_ns_fam} plain={_ns_plain}")
case("7h 常开硬规则在所有档位都在（正餐/备选/全天大点等）",
     all(r in _r_plain and r in _r_slow and r in _r_fam for r in pp.BASE_RULES),
     f"BASE_RULES {len(pp.BASE_RULES)} 条")

# 体积：普通查询应显著小于 v9 的全量 2849 字符
_body = pp.PROPOSE_PROMPT.format(city="上海", days=2, query="上海2天经典深度游",
                                 date_line="", weather_line="", n_stops=_ns_plain,
                                 rules="\n".join(_r_plain), library_hint="")
case("7i 普通查询 prompt 显著瘦身（v9 全量约 2849 字符）",
     len(_body) < 2000, f"v10={len(_body)} 字符")

# 判据复用既有实现，不新造正则（两套判据必然漂移是本项目老坑）
_ppsrc = inspect.getsource(pp.select_rules)
case("7j 判据复用 sequencer 现成正则，未新造",
     "sequencer.SLOW_PACE_RE" in _ppsrc and "sequencer.is_family_query" in _ppsrc
     and "re.compile" not in _ppsrc)
# 缓存键必须含规则集，否则不同规则组合会命中同一条缓存
case("7k 提案缓存键纳入命中的条件规则",
     "sorted(_rule_hits)" in inspect.getsource(pp))

print(f"\n{'❌ 失败 ' + str(len(FAIL)) + ' 项：' + '；'.join(FAIL) if FAIL else '🟢 全部通过'}")
sys.exit(1 if FAIL else 0)
