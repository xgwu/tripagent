# -*- coding: utf-8 -*-
"""TOPTW 忠实执行模式（toptw_faithful_mode）确定性单元测试（纯离线，无需 LLM）：

1) 开关语义：env 未设置默认开启；"0"/"false" 关闭；
2) 忠实模式 alts 不进池：LLM 备选计数 n_llm_alts=0（对照组=2）；
3) 超载日：主选锁定仍装不下 → 剔点 reason 含「忠实执行」（对照组「利润权衡」）；
4) 闭馆主选：池构造前剔除，reason「当日闭馆」，其余主选全保留。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import poi_db, m2_planner  # noqa: E402

city = poi_db.load_city("上海")
all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}

# ---- 用例 0：开关语义 ----
os.environ.pop("TOPTW_FAITHFUL_MODE", None)
assert m2_planner.faithful_mode_enabled() is True, "默认应开启"
os.environ["TOPTW_FAITHFUL_MODE"] = "0"
assert m2_planner.faithful_mode_enabled() is False, "env=0 应关闭"
os.environ["TOPTW_FAITHFUL_MODE"] = "false"
assert m2_planner.faithful_mode_enabled() is False, "env=false 应关闭"
os.environ["TOPTW_FAITHFUL_MODE"] = "1"
assert m2_planner.faithful_mode_enabled() is True, "env=1 应开启"
os.environ.pop("TOPTW_FAITHFUL_MODE", None)
print("用例0 ✅ 开关语义（默认开 / 0/false 关 / 1 开）")


def _run(day_map, alt_map=None, date0=None, cands=None):
    return m2_planner._solve_all_days(
        city, "", day_map, all_pois, cands or [], alt_map, None,
        date0, None, 2.0, 600.0, 1.0)


# ---- 用例 1：忠实模式 alts 不进池 ----
dm1 = {1: ["SH001", "SH003", "SH009", "SH010"]}  # 4 个 dur<=2 市区点，时间充裕
r_faith = _run(dict(dm1), alt_map={1: ["SH011", "SH012"]})
assert r_faith["faithful_mode"] is True
assert r_faith["n_llm_alts"] == 0, f"忠实模式 alts 不应进池（n_llm_alts={r_faith['n_llm_alts']}）"
assert r_faith["n_mains_kept"] == 4 and r_faith["n_mains"] == 4, \
    f"充裕场景主选应全保留：{r_faith['n_mains_kept']}/{r_faith['n_mains']}"
landed = set(r_faith["final_day_map"][1])
assert landed <= {"SH001", "SH003", "SH009", "SH010"}, f"落地混入非主选：{landed}"
print(f"用例1a ✅ 忠实模式：主选 4/4 全保留、alts 不进池（落地 {sorted(landed)}）")

os.environ["TOPTW_FAITHFUL_MODE"] = "0"
r_legacy = _run(dict(dm1), alt_map={1: ["SH011", "SH012"]})
assert r_legacy["faithful_mode"] is False
assert r_legacy["n_llm_alts"] == 2, f"对照组 alts 应进池（n_llm_alts={r_legacy['n_llm_alts']}）"
print(f"用例1b ✅ 对照组（开关关闭）：alts 进池计数 2，落地 {sorted(r_legacy['final_day_map'][1])}")
os.environ.pop("TOPTW_FAITHFUL_MODE", None)

# ---- 用例 2：超载日，主选锁定仍装不下 → 剔点 + reason 区分 ----
dm2 = {1: ["SH006", "SH026", "SH019", "SH037"]}  # dur 8+6+5+4=23h >> horizon 10.5h
r_over = _run(dict(dm2))
reasons = [d["reason"] for d in r_over["solver_dropped"]]
assert r_over["n_mains_kept"] < r_over["n_mains"], \
    f"超载日应有主选被剔（kept={r_over['n_mains_kept']}/{r_over['n_mains']}）"
assert reasons and all("忠实执行" in r for r in reasons), f"reason 未区分忠实口径：{reasons}"
assert 0 < r_over["n_mains_kept"], "不应全灭（至少保留 1 个主选）"
print(f"用例2a ✅ 忠实模式超载：kept={r_over['n_mains_kept']}/{r_over['n_mains']}，"
      f"reason={reasons[0]}")

os.environ["TOPTW_FAITHFUL_MODE"] = "0"
r_over_legacy = _run(dict(dm2))
reasons_l = [d["reason"] for d in r_over_legacy["solver_dropped"]]
assert reasons_l and all("利润权衡" in r for r in reasons_l), f"对照组 reason 应为利润权衡：{reasons_l}"
print(f"用例2b ✅ 对照组超载：reason={reasons_l[0]}")
os.environ.pop("TOPTW_FAITHFUL_MODE", None)

# ---- 用例 3：闭馆主选在池构造前剔除（date0=2026-09-14 周一，SH038 天文馆闭馆）----
dm3 = {1: ["SH038", "SH001", "SH003"]}
r_closed = _run(dict(dm3), date0="2026-09-14")
closed_recs = [d for d in r_closed["solver_dropped"] if d["id"] == "SH038"]
assert closed_recs and "当日闭馆" in closed_recs[0]["reason"], f"闭馆剔除记录缺失：{r_closed['solver_dropped']}"
assert "SH038" not in r_closed["final_day_map"][1], "闭馆点不应落地"
assert r_closed["n_mains_kept"] == 2, f"其余主选应全保留：{r_closed['n_mains_kept']}/{r_closed['n_mains']}"
print(f"用例3 ✅ 闭馆主选剔除（reason={closed_recs[0]['reason']}），其余主选 2/2 保留")

print("\n全部 4 组用例通过")
