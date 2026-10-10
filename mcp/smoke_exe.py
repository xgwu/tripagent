#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打包产物冒烟：把 `dist/tripplanner-mcp.exe` 当**真 MCP server** 拉起验一遍。

为什么必须有这一步（源码跑通 ≠ exe 跑通）：打包会悄悄改变两件事 ——
  ① 面板 HTML 的**位置**（`--add-data` + `sys._MEIPASS`）⇒ 找不到时 `read_panel()`
     会返回「面板文件读取失败」的**降级页**，宿主照样能渲染，只是用户看到一张错误页；
  ② 「项目根」的**推导**（`sys.frozen`）⇒ 错了就变成「连不上后端 / 状态写错地方」。
两种都不会抛异常，只会表现为「面板空白 / 后端不可达」，极难反查。

「怎么算跑通」的断言集中在 `mcp/bundle_check.py`（与 `build_bundle.py` **共用**，
避免同一口径两处各写一遍 —— 本项目已因 `mode` 口径三处漂移吃过亏）。
本文件只负责 **exe 形态特有**的额外检查：remote.json 同目录、注入的 base/凭据/地图 key、
状态落盘位置、以及「不写回仓库」。

用法：
    <venv python> mcp/smoke_exe.py                 # 默认 dist/tripplanner-mcp.exe
    <venv python> mcp/smoke_exe.py <exe路径>
没找到 exe 时**跳过（exit 0）**并提示先跑 `mcp/build_exe.py` 或 `mcp/build_bundle.py`。
"""
from __future__ import annotations

import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import bundle_check  # noqa: E402

DEFAULT_EXE = os.path.join(REPO, "dist", "tripplanner-mcp.exe")

fails: list = []


def check(label, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + label + (f"  -> {detail}" if detail else ""))
    if not cond:
        fails.append(label)


def main() -> int:
    exe = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXE)
    print(f"exe: {exe}")
    if not os.path.exists(exe):
        print(f"（跳过）没找到 {exe} —— 先执行：")
        print(f'    "{sys.executable}" mcp/build_exe.py')
        print(f'    或 "{sys.executable}" mcp/build_bundle.py --target win')
        return 0
    dist = os.path.dirname(exe)
    remote_json = os.path.join(dist, "remote.json")
    print(f"dist: {dist}")
    print(f"exe 大小: {os.path.getsize(exe) / 1048576:.1f} MB")
    print()

    # exe 形态特有：remote.json 必须与 exe 同目录，否则退回默认 base
    check("dist/remote.json 与 exe 同目录（分享包的必要条件）",
          os.path.exists(remote_json), remote_json)

    # 记录 exe **同目录**的 output（验状态落在包目录，不写回仓库）
    out_dir = os.path.join(dist, "output")
    stale = os.path.join(out_dir, "mcp_last_plan.json")
    if os.path.exists(stale):
        os.remove(stale)
    repo_state = os.path.join(REPO, "output", "mcp_last_plan.json")
    repo_before = os.path.getmtime(repo_state) if os.path.exists(repo_state) else None

    # ---- 共用断言（与 build_bundle.py 同一实现）----
    r = bundle_check.run([exe], cwd=dist, env_extra={"TRIPPLANNER_HOME": dist})
    for label, ok, detail in r.checks:
        check(label, ok, detail)

    # ---- exe 形态特有的注入检查 ----
    m_base = re.search(r'var\s+REMOTE_BASE\s*=\s*"(https?://[^"]+)"', r.html)
    check("exe 从同目录 remote.json 读到并注入了线上 base",
          bool(m_base), m_base.group(1) if m_base else "仍是占位符")
    check("exe 注入了 Basic 凭据",
          re.search(r'var\s+REMOTE_AUTH\s*=\s*"Basic [A-Za-z0-9+/=]+"', r.html) is not None)
    m_amap = re.search(r'var\s+amapKey\s*=\s*"([A-Za-z0-9]{16,})"', r.html)
    check("exe 注入了地图 key（本地没有 secrets.json ⇒ 走云端下发）",
          bool(m_amap),
          f"len={len(m_amap.group(1))}" if m_amap
          else "取不到 key 时面板只是没有地图，其余功能不受影响")

    check("状态落盘在**包目录** output/（分享包不写回仓库）", os.path.exists(stale), out_dir)
    later = os.path.getmtime(repo_state) if os.path.exists(repo_state) else None
    check("仓库 output/ 的行程**没有被 exe 改写**", later == repo_before,
          f"{repo_before} -> {later}")

    if r.stderr.strip():
        print("\n--- exe stderr 末 6 行 ---")
        for ln in r.stderr.strip().splitlines()[-6:]:
            print("   " + ln)

    print()
    if fails:
        print(f"[FAIL] {len(fails)} 项未通过：")
        for f in fails:
            print("   - " + f)
        return 1
    print("[PASS] 打包产物冒烟全绿 —— exe 可独立运行（零依赖、无 src/ data/）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
