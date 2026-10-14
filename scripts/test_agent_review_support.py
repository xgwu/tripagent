"""守卫：宿主多轮审阅支持（2026-10-13）

背景（Kenny 架构问题）：宿主 agent 有多轮审阅能力，但合理点常被求解器硬约束剔除
⇒ 反复重算。实测调研结论：拆约束无效（去掉它们点也不回归，瓶颈是物理时长），
正解是 ① 把「为什么装不下」回传给宿主；② 给宿主「减点」原语（此前只有
move/replace，二者都要求点集不变）。

本守卫钉两件事：
  A. 弃点原因**必须**回传到宿主（plan_view + plan_summary）
  B. 「减点」原语存在且守门正确（REMOVE_TOOL）
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []
passes = []


def chk(name, cond, info=""):
    (passes if cond else fails).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{info}]" if info else ""))


print("=== A. 弃点原因回传（宿主能看到「为什么装不下」）===")

# A1: plan_view 合并 toptw_dropped / mains_dropped
pv_src = open(os.path.join(ROOT, "src", "plan_view.py"), encoding="utf-8").read()
chk("A1 plan_view 合并 toptw_dropped（求解器弃点不再丢失）",
    "toptw_dropped" in pv_src and "mains_dropped" in pv_src)
chk("A2 plan_view 去重（按 id+day，防修复链与求解器重复计入）",
    "_seen" in pv_src and "key =" in pv_src)
chk("A3 弃点记录带 reason 与 day（宿主可据此决策）",
    '"reason": p.get("reason")' in pv_src and '"day": p.get("day")' in pv_src)

# A4: plan_summary 输出原因
sv_src = open(os.path.join(ROOT, "mcp", "tripplanner_mcp_server.py"),
              encoding="utf-8").read()
chk("A4 plan_summary 按原因分组输出", "_by_reason" in sv_src)
chk("A5 plan_summary 给宿主可操作提示（remove_poi 指路）",
    "tripplanner_remove_poi" in sv_src and "时间预算装不下" in sv_src)

# A6: 实跑 —— 造一份 plan，验证 dropped 带 reason 出现在摘要里
spec = importlib.util.spec_from_file_location(
    "_srv_guard", os.path.join(ROOT, "mcp", "tripplanner_mcp_server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
_fake = {"city": "武汉", "days": 2, "mode": "m7", "dayList": [
    {"day": 1, "theme": "黄鹤楼", "travel_km": 5, "finish": "17:00",
     "timeline": [{"type": "poi", "start": "09:00", "name": "黄鹤楼"}]}],
    "droppedPois": [{"name": "极地海洋公园", "reason": "TOPTW 求解：时间预算内无法纳入（利润权衡）"},
                    {"name": "某闭馆点", "reason": "当日闭馆（周一）"}]}
_sum = srv.plan_summary(_fake)
chk("A6 摘要里出现弃点原因原文", "时间预算内无法纳入" in _sum, _sum[-120:])
chk("A7 摘要里出现「当日闭馆」另一类原因", "当日闭馆" in _sum)
chk("A8 摘要给出可操作建议", "remove_poi" in _sum)

print()
print("=== B. 减点原语（REMOVE_TOOL）===")
chk("B1 REMOVE_TOOL 常量存在", hasattr(srv, "REMOVE_TOOL")
    and srv.REMOVE_TOOL == "tripplanner_remove_poi")
_names = [t["name"] for t in srv.TOOLS]
chk("B2 已注册进 TOOLS", srv.REMOVE_TOOL in _names, str(len(_names)) + " 个工具")
_t = srv.TOOL_BY_NAME.get(srv.REMOVE_TOOL) or {}
_sch = (_t.get("inputSchema") or {})
chk("B3 schema 必填 city/day/poi_id/day_ids",
    set(_sch.get("required") or []) == {"city", "day", "poi_id", "day_ids"})
chk("B4 description 说明「只有本工具能减点」",
    "只有本工具能主动减点" in (_t.get("description") or ""))
chk("B5 不发提案话术（无 proposal/dry_run）",
    "proposal" not in str(_sch))

# B6: 源码锚点 —— 走 /api/remove（**不能**复用 /api/move，后者守门要求点集一致）
chk("B6 handler 走 /api/remove（move 的「点集一致」守门正是减点要绕过的）",
    "/api/remove" in sv_src and "remain = [i for i in day_ids if i != poi_id]" in sv_src)
chk("B7 守门：当天只剩一个点时拒绝（不产生空天）",
    "当天只剩这一个点" in sv_src)
chk("B8 回执校验「真的少了那个点」（gone）", "gone" in sv_src and "removedOk" in sv_src)
# B9: 云端端点存在且加了「真子集」底线（防误用本端点增点/换点）
_web = open(os.path.join(ROOT, "webui", "server.py"), encoding="utf-8").read()
chk("B9 云端 /api/remove 端点存在", 'u.path == "/api/remove"' in _web)
chk("B10 /api/remove 要求真子集（不做增点/换点）",
    "真子集" in _web and "set(ordered) < set(day_ids)" in _web)

print()
print("=== C. 版本闸门（新增工具必须 +1）===")
chk("C1 SERVER_VERSION 已 +1（0.6.1-m5）", 'SERVER_VERSION = "0.6.1-m5"' in sv_src)
chk("C2 PANEL_VERSION 已 +1（m24）", 'PANEL_VERSION = "m24"' in sv_src)
_st = open(os.path.join(ROOT, "mcp", "selftest.py"), encoding="utf-8").read()
chk("C3 selftest 工具数期望同步为 11", "len(tools) == 11" in _st)

print()
print(f"{'='*50}")
if fails:
    print(f"[FAIL] {len(fails)}/{len(passes)+len(fails)} 项未通过：{fails}")
    sys.exit(1)
print(f"[PASS] 全绿（{len(passes)} 项）")
