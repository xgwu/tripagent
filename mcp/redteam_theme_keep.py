# -*- coding: utf-8 -*-
"""红队：证明 `mcp/panel_guard.py` 新增的「换点保留当日主题」两层断言**能被打红**。

做法（本仓红队的一贯纪律）：**绝不改真文件** —— 把面板 / webui 复制成临时变体，
注入「回退到老行为」的坏法后用 `PANEL_GUARD_PANEL=` / `PANEL_GUARD_WEBUI=` 指过去，
`finally` 里删掉变体。

注入三种坏法（都对应真实出现过的缺陷形态）：
  M1) 面板 applyDayToPlan 去掉「theme 缺失时沿用旧值」⇒ 主题被空串盖掉（报障原形）
  M2) 面板把判据写成无条件覆盖 ⇒ 云端将来回填的新主题被旧值盖掉（反向不变式）
  M3) webui swapPoi 去掉「Object.assign 后回填 theme」⇒ Web UI 同样丢主题

基线必须先绿：否则说明守卫本身坏了，而不是抓住了问题。
"""
import io
import os
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = os.path.join(ROOT, "mcp", "panel", "plan_panel.html")
INDEX = os.path.join(ROOT, "webui", "index.html")
GUARD = os.path.join(ROOT, "mcp", "panel_guard.py")
MUT_PANEL = os.path.join(ROOT, "mcp", "panel", "_rt_plan_panel.html")
MUT_INDEX = os.path.join(ROOT, "webui", "_rt_index.html")
PY = sys.executable

# 锚点（唯一）：改代码就得同步改这里 —— 锚点比被测对象更危险，故先校验唯一性
A_KEEP = "    if (oldDay && !dayObj.theme && oldDay.theme) dayObj.theme = oldDay.theme;\n"
A_OVER = "!dayObj.theme && oldDay.theme"
A_WEB = "      if(!d.theme)d.theme=keepTheme;"

LABEL_PANEL = "换点保留当日主题（面板 applyDayToPlan，node 真跑）"
LABEL_WEB = "换点保留当日主题（webui swapPoi，node 真跑）"


def run_guard(panel_path=None, index_path=None):
    env = dict(os.environ)
    if panel_path:
        env["PANEL_GUARD_PANEL"] = panel_path
    if index_path:
        env["PANEL_GUARD_WEBUI"] = index_path
    r = subprocess.run([PY, GUARD], cwd=ROOT, env=env,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    panel = open(PANEL, encoding="utf-8").read()
    index = open(INDEX, encoding="utf-8").read()
    misses = []
    if panel.count(A_KEEP) != 1:
        misses.append(f"A_KEEP（出现 {panel.count(A_KEEP)} 次）")
    if panel.count(A_OVER) != 1:
        misses.append(f"A_OVER（出现 {panel.count(A_OVER)} 次）")
    if index.count(A_WEB) != 1:
        misses.append(f"A_WEB（出现 {index.count(A_WEB)} 次）")
    if misses:
        print("❌ 基线失败：注入锚点在真文件里不是唯一 —— " + "、".join(misses))
        return 1

    rc, out = run_guard()
    print(f"[基线] 守卫 rc={rc}  {'✅ 绿' if rc == 0 else '❌ 红了（守卫本身有问题）'}")
    if rc != 0:
        print(out[-1500:])
        return 1

    mutants = [
        (LABEL_PANEL, "M1 面板去掉「theme 缺失时沿用旧值」", panel, A_KEEP, "", None),
        (LABEL_PANEL, "M2 面板改成无条件用旧值覆盖", panel, A_OVER, "oldDay.theme", None),
        (LABEL_WEB, "M3 webui 去掉 theme 回填", index, A_WEB, "", None),
    ]

    caught = 0
    try:
        for label, title, src, old, new, _ in mutants:
            body = src.replace(old, new, 1)
            is_panel = label == LABEL_PANEL
            path = MUT_PANEL if is_panel else MUT_INDEX
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(body)
            rc, out = run_guard(path if is_panel else None,
                                None if is_panel else path)
            hit = (rc != 0) and (f"[FAIL] {label}" in out)
            if hit:
                print(f"✅ 捕获：{title}  → 守卫变红且红在对应那条（rc={rc}）")
                caught += 1
            else:
                print(f"❌ 漏网：{title}  → rc={rc}（守卫没能识别或红错地方）")
                tail = [ln for ln in out.splitlines() if "[FAIL]" in ln]
                print("     " + ("\n     ".join(tail[-3:]) or "(无 FAIL 行)"))
    finally:
        for p in (MUT_PANEL, MUT_INDEX):
            if os.path.exists(p):
                os.remove(p)
        print("[清理] 临时变体已删除（真文件全程未改动）")

    print(f"\n== 红队结果 {caught}/{len(mutants)} 被捕获 ==")
    return 0 if caught == len(mutants) else 1


if __name__ == "__main__":
    sys.exit(main())
