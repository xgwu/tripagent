#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""红队：证明 `build_bundle.py --self-check` 的那些断言**不是空的**。

为什么需要
----------
`--self-check` 的价值全在「真能抓到坏包」上。一个写错的断言（锚点漂了、比错了对象、
条件恒真）会永远打绿 —— 和没有守卫一样，但更危险，因为它给人安全感。
所以守卫必须**可被击穿**：注入真实的坏法，确认它变红、且红在**对应**那条上。

做法（关键设计）
----------------
把 `build_bundle.py` 复制成**同目录**的临时变体 `mcp/_bundle_mutant.py`，只改变体的
内容、**绝不碰仓库里的真文件**。为什么必须同目录：`build_bundle` 的 `HERE = dirname(__file__)`
决定了 `SERVER`/`PANEL` 从哪找、`REPO`/`DIST` 落在哪 —— 放到别处会因路径错而红成一片，
红的就不是「被注入的那条」，红队结论失效。

⚠️ 上一版（`dist/_redteam_bundle.py`，未入库）是**直接改真文件再还原**：进程中途被
杀掉就会在仓库里留下一个坏的 `build_bundle.py`，而且没人知道。改为临时变体后，
最坏情况只是留下一个 `_bundle_mutant.py`（下次运行会覆盖），真文件永远不动。

用法
----
    python mcp/bundle_redteam.py        # 跑全部注入
    python mcp/bundle_redteam.py -v     # 额外打印每次变红的断言明细

退出码：0 = 每条注入都被捕获（守卫有效）／1 = 有守卫失效或锚点漂移（必须修）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.join(HERE, "build_bundle.py")
# 变体必须与真文件同目录（见 docstring 的路径原因）
MUTANT = os.path.join(HERE, "_bundle_mutant.py")
PY = sys.executable

# 每条：(说明, 原文锚点, 注入后内容, 期望变红的断言片段)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # 静默不可用①：zip 丢可执行位 ⇒ 解压后启动器不可执行，宿主只报一句 spawn 失败
    ("zip 丢可执行位",
     "zi.external_attr = ((0o100755 if ex else 0o100644) << 16)",
     "zi.external_attr = (0o100644 << 16)",
     "启动器带可执行位"),
    # 静默不可用②：.sh 成 CRLF ⇒ mac/linux 报 `\r: command not found`
    # 🔴 注入锚点别写错：`open(..., newline="")` **仍写 LF**（只是不翻译），不会变 CRLF；
    #    真正的回归是**漏掉 newline 参数**（默认 newline=None ⇒ 在 Windows 上把 \n 翻成 \r\n）。
    ("启动器写成 CRLF",
     'with open(path, "w", encoding="utf-8", newline="\\n") as f:',
     'with open(path, "w", encoding="utf-8") as f:',
     "无 CRLF"),
    # 静默不可用③：mcp.json 占位符少一处 ⇒ 收包人照抄注册，cwd/env 指着「<你放的那个目录>」
    ("mcp.json 少一处占位符",
     '"TRIPPLANNER_HOME": "<你放的那个目录>"',
     '"TRIPPLANNER_HOME": ""',
     "占位符 3 处"),
    # 静默不可用④：包内缺 remote.json ⇒ 面板「后端不可达」，但启动器不报错
    ("包内不写 remote.json",
     'with open(os.path.join(pkg, "remote.json"), "w", encoding="utf-8") as f:',
     'with open(os.path.join(pkg, "_unused.json"), "w", encoding="utf-8") as f:',
     "remote.json 与启动器同层"),
]


def _run(path: str) -> tuple[int, str]:
    r = subprocess.run([PY, path, "--self-check"], cwd=REPO, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _red_lines(out: str) -> list[str]:
    """只认真正落红（✗）的断言行 —— 否则「✓ 无 CRLF」这种通过行会被误判成命中。"""
    return [ln.strip() for ln in out.splitlines() if ln.strip().startswith("✗")]


def main() -> int:
    ap = argparse.ArgumentParser(description="红队：证明铺包自检可被击穿")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每次变红的断言明细")
    args = ap.parse_args()

    if not os.path.exists(SRC):
        print(f"✗ 找不到 {SRC}")
        return 1
    orig = open(SRC, encoding="utf-8").read()

    # 基线：真文件必须先是绿的，否则「注入才变红」这个因果不成立
    rc0, out0 = _run(SRC)
    print(f"基线（真文件）--self-check 退出码 = {rc0}" + ("  ✓" if rc0 == 0 else "  ✗"))
    if rc0 != 0:
        print(out0)
        print("✗ 基线不绿：自检本身就失败，先修它再看红队结果（否则结论无意义）")
        return 1

    bad = 0
    try:
        for name, old, new, expect in MUTATIONS:
            if old not in orig:
                # 锚点漂移 ⇒ 这条红队已经**静默失效**了（测的是空气），必须算失败
                print(f"[✗ 锚点漂移] 「{name}」：源码里找不到注入锚点（自检/LF 写法可能已改）")
                bad += 1
                continue
            assert old != new
            with open(MUTANT, "w", encoding="utf-8", newline="\n") as f:
                f.write(orig.replace(old, new, 1))
            rc, out = _run(MUTANT)
            reds = _red_lines(out)
            hit = any(expect in ln for ln in reds)
            caught = rc != 0 and hit
            if not caught:
                bad += 1
            print(f"[{'击穿 OK' if caught else '❌ 守卫失效'}] 注入「{name}」"
                  f"→ 退出码 {rc}，期望断言「{expect}」{'出现在 ✗ 行' if hit else '未出现在 ✗ 行'}")
            if args.verbose or not caught:
                for ln in reds:
                    print("        " + ln)
    finally:
        if os.path.exists(MUTANT):
            os.remove(MUTANT)

    total = len(MUTATIONS)
    print(f"\n{'🟢' if not bad else '🔴'} {total - bad}/{total} 条注入被捕获"
          f"{' ⇒ 自检有效' if not bad else ' ⇒ 有守卫失效（自检形同虚设，必须修）'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
