# -*- coding: utf-8 -*-
"""红队：证明 `scripts/test_multicity_coords.py` 真能抓到「多城坐标」这类回归。

纪律（沿用 `mcp/bundle_redteam.py` / `mcp/redteam_panel_single.py`）：
**绝不动真文件** —— 本脚本用**运行期打桩**（monkeypatch）注入坏实现，再在同进程里
用 `runpy` 跑一遍守测试，断言「基线绿 → 注入后变红，且红在**对应那条**用例」。
锚点找不到 / 注入后仍绿（＝守卫对该缺陷不敏感）都算失败。

三个变异体，分别对应缺陷的三条链：
  ① `poi_coords` 退回「只认单个城名」＝ 原缺陷本身（报障：地图不渲染）
  ② `_day_city` 退回「只信传入城名、不看 POI id 前缀」＝ 多城换点必 400
  ③ `_day_city` 整个缺失（＝修复前的状态）

用法：python mcp/redteam_multicity_coords.py
"""
import contextlib
import io
import os
import runpy
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from src import plan_view          # noqa: E402
from webui import server as sv     # noqa: E402

GUARD = os.path.join(ROOT, "scripts", "test_multicity_coords.py")
FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


def run_guard():
    """跑一遍守测试，返回 (退出码, 输出)。"""
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            runpy.run_path(GUARD, run_name="__main__")
        except SystemExit as e:
            code = int(e.code or 0)
    return code, buf.getvalue()


# ---------------- 变异体 ----------------
_ORIG_COORDS = plan_view.poi_coords
_ORIG_DAY_CITY = getattr(sv, "_day_city", None)


def mutant_coords_single(city):
    """原缺陷：把 `A+B` 当单个城名去 load_city ⇒ 永远返回空表。"""
    if not city:
        return {}
    m = {}
    try:
        city_data = plan_view.poi_db.load_city(city)
        for p in city_data.get("pois", []):
            pp = plan_view.poi_db.parse_poi(p, city_data)
            if pp.get("id") is not None:
                m[pp["id"]] = (pp.get("lat"), pp.get("lng"))
    except Exception:  # noqa: BLE001
        pass
    return m


def mutant_day_city_naive(city_field, poi_ids):   # noqa: ARG001
    """只看传入城名：多城拼接串一律判非法 ⇒ 换点/挪时段 400。"""
    return (city_field or "").strip() or None


MUTANTS = [
    ("① poi_coords 退回单城（原缺陷：地图不渲染）",
     lambda: (setattr(plan_view, "poi_coords", mutant_coords_single),
              plan_view._COORD_CACHE.clear()),
     lambda: setattr(plan_view, "poi_coords", _ORIG_COORDS),
     ["1c", "2c", "4a"]),
    ("② _day_city 忽略 POI id 前缀（多城换点必 400）",
     lambda: setattr(sv, "_day_city", mutant_day_city_naive),
     lambda: setattr(sv, "_day_city", _ORIG_DAY_CITY),
     ["3b", "3c", "3d"]),
    ("③ _day_city 整个缺失（＝修复前的状态）",
     lambda: (delattr(sv, "_day_city"), plan_view._COORD_CACHE.clear()),
     lambda: setattr(sv, "_day_city", _ORIG_DAY_CITY),
     ["3a"]),
]

print("== 基线（未注入，必须先绿）==")
_base_code, _ = run_guard()
case("基线守测试通过", _base_code == 0, f"exit={_base_code}")

print("\n== 注入坏实现 ==")
for label, apply, restore, want_tags in MUTANTS:
    try:
        apply()
        code, out = run_guard()
    finally:
        restore()
        plan_view._COORD_CACHE.clear()
    hit = []
    if "项失败" in out:
        summary = out.split("项失败", 1)[1]        # 只看失败汇总块，免撞 ✅ 行
        hit = [t for t in want_tags
               if any(line.strip().startswith(f"- {t}") for line in summary.splitlines())]
    # 恢复后必须重新变绿（证明「红」是注入引起的，不是守卫自己坏了）
    red_on_inject = code != 0
    case(f"{label} → 守测试变红", red_on_inject, f"exit={code}")
    case(f"{label} → 红在对应用例 {want_tags}", bool(hit), f"命中 {hit}")
    code2, _ = run_guard()
    case(f"{label} → 恢复后重新变绿（因果成立）", code2 == 0, f"exit={code2}")

print()
if FAIL:
    print(f"❌ 红队 {len(FAIL)} 项未通过（守卫可能已静默失效）：")
    for f in FAIL:
        print("   -", f)
    raise SystemExit(1)
print("✅ 红队全部捕获：3/3 变异体都被对应断言打红，且恢复后回到绿")
