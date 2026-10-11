# -*- coding: utf-8 -*-
"""红队：证明 `scripts/test_panel_single_open.py` **能被打红**。

做法（本仓红队的一贯纪律）：**绝不改真文件** ——
把 `mcp/tripplanner_mcp_server.py` 复制成**同目录**的临时变体，注入坏法后
用 `TRIPPLANNER_SERVER=<变体>` 跑守卫；`finally` 里删掉变体。

注入三种坏法（覆盖两个方向 + 一次历史回退）：
  M1) 给 `tripplanner_plan_trip` 加回 `_meta.ui.resourceUri`
      ⇒ 宿主又会「每次调用开一扇」⇒ 重拍堆面板（不变式 A 必须红）
  M2) 给 `tripplanner_open_plan` **摘掉** `_meta.ui.resourceUri`
      ⇒ 面板永远出不来（不变式 A 必须红）—— 与 M1 反向，证明 A 锁的是"恰好一个"
  M3) 复活**会话级封锁**：open_plan 第 2 次调用直接返回 `panelOpened=false`
      ⇒ **同一会话的第二个需求再也弹不出面板**（不变式 C 必须红）
      —— 这正是 2026-10-10 报障二的形态

基线必须先绿：否则说明守卫本身坏了，而不是抓住问题。
"""
import io
import os
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "mcp", "tripplanner_mcp_server.py")
MUT = os.path.join(ROOT, "mcp", "_rt_panel_single.py")   # 同目录：路径推断才对
GUARD = os.path.join(ROOT, "scripts", "test_panel_single_open.py")
PY = sys.executable

# 锚点（每个都必须在真文件里唯一 —— 锚点比被测对象更危险）
ANCHOR_META = (
    "        # 🔴 **刻意不带** `_meta.ui.resourceUri`：带了宿主就会「每次调用开一扇面板」⇒ 重拍时堆面板。\n"
)
ANCHOR_OPEN_META = (
    '        "_meta": {"ui": {"resourceUri": VIEWER_URI, "visibility": ["model", "app"]}},\n'
)
ANCHOR_SHOWN = "            nth = SESSION.note_panel_shown(iid)\n"


def run_guard(server_path=None):
    env = dict(os.environ)
    if server_path:
        env["TRIPPLANNER_SERVER"] = server_path
    r = subprocess.run([PY, GUARD], cwd=ROOT, env=env,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    src = open(SRC, encoding="utf-8").read()
    misses = []
    for name, anchor in (("META", ANCHOR_META), ("OPEN_META", ANCHOR_OPEN_META),
                         ("SHOWN", ANCHOR_SHOWN)):
        if src.count(anchor) != 1:
            misses.append(f"{name}（出现 {src.count(anchor)} 次）")
    if misses:
        print("❌ 基线失败：注入锚点在真文件里不是唯一 —— " + "、".join(misses))
        print("   （锚点比被测对象更危险：改了代码就得同步改这里）")
        return 1

    # ---- 基线：真文件必须绿 ----
    rc, out = run_guard()
    print(f"[基线] 守卫 rc={rc}  {'✅ 绿' if rc == 0 else '❌ 红了（守卫本身有问题）'}")
    if rc != 0:
        print(out[-1500:])
        return 1

    refusal = (
        "            if nth > 1:   # 🔴 红队注入：复活会话级封锁（历史坏法）\n"
        "                return ok(id_, {\n"
        "                    \"content\": [{\"type\": \"text\", \"text\": \"已展示过，不再开\"}],\n"
        "                    \"structuredContent\": {\"panelOpened\": False,\n"
        "                                          \"reason\": \"panel-already-open\"},\n"
        "                })\n"
    )
    mutants = {
        "M1 plan_trip 加回 _meta.ui（重拍又堆面板）": (
            ANCHOR_META,
            ANCHOR_META + '        "_meta": {"ui": {"resourceUri": VIEWER_URI, '
                          '"visibility": ["model", "app"]}},\n',
        ),
        "M2 open_plan 摘掉 _meta.ui（面板永远出不来）": (
            ANCHOR_OPEN_META,
            "",
        ),
        "M3 复活会话级封锁（第二个需求弹不出面板）": (
            ANCHOR_SHOWN,
            ANCHOR_SHOWN + refusal,
        ),
    }

    caught = 0
    try:
        for title, (old, new) in mutants.items():
            open(MUT, "w", encoding="utf-8", newline="").write(src.replace(old, new, 1))
            rc, out = run_guard(MUT)
            hit = "AssertionError" in out or "assert" in out
            if rc != 0 and hit:
                print(f"✅ 捕获：{title}  → 守卫变红（rc={rc}）")
                tail = [ln for ln in out.splitlines() if "assert" in ln.lower()][-1:]
                for ln in tail:
                    print("     " + ln.strip()[:160])
                caught += 1
            else:
                print(f"❌ 漏网：{title}  → rc={rc}（守卫没能识别）")
                print(out[-900:])
    finally:
        if os.path.exists(MUT):
            os.remove(MUT)
        print("[清理] 临时变体已删除：", os.path.basename(MUT),
              "（真文件全程未改动）")

    print(f"\n== 红队结果 {caught}/{len(mutants)} 被捕获 ==")
    return 0 if caught == len(mutants) else 1


if __name__ == "__main__":
    sys.exit(main())
