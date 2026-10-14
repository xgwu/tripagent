"""守卫：天数上限按目的地类型动态（2026-10-14，新疆开城前置 ①）

背景：原单一 `MAX_DAYS=5` 对新疆（北疆环线 7-8 天、南北疆 12-15 天）是硬伤——
「开了城也只能玩 1/4」。本守卫钉：
  A. days_cap_for 的判据（市内 5 / 自驾 10 / 大区 10 / 多城 12）
  B. 旧行为**零回归**（市内/景区城仍是 5）
  C. clamp_days 向后兼容（不传 cap 时 = MAX_DAYS）
  D. 口径单一（webui 与 MCP 不再各写一套天数上限）
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.query_days import (days_cap_for, clamp_days,                          # noqa: E402
                            MAX_DAYS, DEFAULT_DAYS,
                            DAYS_CAP_DEFAULT, DAYS_CAP_SELF_DRIVE,
                            DAYS_CAP_MULTI_CITY)

fails, passes = [], []


def case(name, cond, info=""):
    (passes if cond else fails).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{info}]" if info else ""))


print("=== A. 天数上限判据 ===")
case("A1 常量：默认 8 / 自驾 12 / 多城 15",
     DAYS_CAP_DEFAULT == 8 and DAYS_CAP_SELF_DRIVE == 12 and DAYS_CAP_MULTI_CITY == 15)
case("A2 无目的地无自驾词 → 8（普通城市深度游，原 5 过紧）",
     days_cap_for([], "上海2日游") == 8, str(days_cap_for([], "上海2日游")))
case("A3 普通城市（成都）→ 8",
     days_cap_for(["成都"], "成都8天深度游") == 8)
case("A4 自驾词（新疆8天自驾）→ 12",
     days_cap_for(["乌鲁木齐"], "新疆8天自驾游") == 12)
case("A5 大尺度地名（北疆环线8天，无自驾词）→ 12",
     days_cap_for(["乌鲁木齐"], "新疆北疆环线8天") == 12)
case("A6 需求里点名大区地（未解析出城市）→ 12",
     days_cap_for([], "喀纳斯禾木8日游") == 12)
case("A7 两城 → 12", days_cap_for(["乌鲁木齐", "喀纳斯"], "两地游") == 12)
case("A8 三城 → 15", days_cap_for(["乌鲁木齐", "喀纳斯", "伊犁"], "新疆游") == 15)
case("A9 取最大值（自驾 + 三城）→ 15",
     days_cap_for(["乌鲁木齐", "喀纳斯", "伊犁"], "自驾12天") == 15)

print()
print("=== B. 用户实际天数不被改动（只放宽上限）===")
case("B1 说 3 天就是 3 天（不因上限高而被撑大）",
     clamp_days(3, cap=days_cap_for([], "上海3日")) == (3, False, 3))
case("B2 说 2 天就是 2 天", clamp_days(2, cap=8) == (2, False, 2))
case("B3 需求 20 天 > 上限 → 截断到 8 并标记",
     clamp_days(20, cap=8) == (8, True, 20))
case("B4 提不到天数 → 默认 2", clamp_days(None, cap=8) == (DEFAULT_DAYS, False, None))

print()
print("=== C. clamp_days 向后兼容 + 口径单一 ===")
case("C1 不传 cap 时按 MAX_DAYS 截断",
     clamp_days(20) == (MAX_DAYS, True, 20), str(clamp_days(20)))
case("C2 MAX_DAYS 与 DAYS_CAP_DEFAULT 一致（防两套口径漂移）",
     MAX_DAYS == DAYS_CAP_DEFAULT, f"MAX_DAYS={MAX_DAYS}")

print()
print("=== D. 口径单一（无第三套硬编码）===")
_qd = open(os.path.join(ROOT, "src", "query_days.py"), encoding="utf-8").read()
case("D1 query_days 提供 days_cap_for", "def days_cap_for(" in _qd)
case("D2 大尺度地名表存在（_LONG_HAUL_CITIES）", "_LONG_HAUL_CITIES" in _qd)
_web = open(os.path.join(ROOT, "webui", "server.py"), encoding="utf-8").read()
case("D3 webui 导入并调用 days_cap_for",
     "days_cap_for" in _web and "cap=_cap_m" in _web and "cap=_cap_1" in _web)
# ⚠️ 排除注释：原实现文本被写进注释作对照（本项目老坑——「校验脚本自己的正则
#    比被测对象更危险」，见 panel_guard 的 \b 边界教训）。
_web_code = "\n".join(ln for ln in _web.splitlines()
                      if not ln.lstrip().startswith("#"))
case("D4 webui 代码中不再有 `1 <= d <= 5` 天数硬编码（注释里的对照文本不算）",
     "1 <= d <= 5" not in _web_code)
_srv = open(os.path.join(ROOT, "mcp", "tripplanner_mcp_server.py"),
            encoding="utf-8").read()
case("D5 MCP 天数上限不再是旧的 7（改指真相 + 取最宽 15）",
     "MIN_DAYS, MAX_DAYS = 1, 7" not in _srv
     and "DAYS_CAP_MULTI_CITY" in _srv and "MIN_DAYS, MAX_DAYS = 1, 15" in _srv)

print()
print("=== E. 8 天真能跑（天数不被截断）===")
_r = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0,'.');"
     "from src.config import apply_env; apply_env();"
     "from src import poi_db, proposal_planner as pp;"
     "c=poi_db.load_city('成都');"
     "r=pp.plan(c,'成都8天深度游',days=8,use_llm=False);"
     "d=(r.get('itinerary') or {}).get('days') or [];"
     "print(len(d))"],
    cwd=ROOT, capture_output=True, text=True, timeout=180)
got = (_r.stdout or "").strip().splitlines()[-1] if _r.stdout.strip() else "?"
case("E1 8 天规划返回 8 天（不受旧 5 天上限拦截）", got == "8", f"返回 {got}")

print()
print("=" * 50)
if fails:
    print(f"[FAIL] {len(fails)}/{len(passes)+len(fails)} 项未通过：{fails}")
    sys.exit(1)
print(f"[PASS] 全绿（{len(passes)} 项）")
