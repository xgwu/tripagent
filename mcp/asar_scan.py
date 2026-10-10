#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在宿主 `app.asar` 里按**原始字节**搜关键字并打印上下文 —— 协议取证工具。

为什么需要它：本项目对宿主行为的结论**一律要源码级证据**（「面板开几次」「哪些字段
算 App」「信任怎么记」都是这么定的），而 Electron 打出来的 `app.asar` **未压缩**，
按字节搜比先解包再 grep 快得多，也不用装 asar 工具。

用法（本机路径固定，也可用 `--asar` 覆盖）：
    python mcp/asar_scan.py openHostApp            # 默认前后 600/1200 字节、最多 8 处
    python mcp/asar_scan.py resourceUri 400 800 12
    python mcp/asar_scan.py --asar /path/app.asar "handleAgentToolCall"

⚠️ 输出可能很长；只截你关心的窗口。**别把结果当规范**：命中片段是压缩后的等价源码，
变量名已混淆（`e/t/n/r`），读懂逻辑靠上下文，不要照抄标识符。

已用它定下的事实（见 `mcp/README.md` 的「协议要点」与「M6.3」）：
  - 去重键 `${sessionId}::${toolCallId}` ⇒ 每次工具调用各开一扇面板
  - `findAppByToolName` / `resolveAppDefinition` ⇒ 只有工具定义的 `_meta.ui.resourceUri` 算 App
  - 存储字段白名单 `type/command/args/env/cwd/url/headers/timeout`
"""
from __future__ import annotations

import argparse
import io
import os
import sys

DEFAULT_ASAR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "Programs", "WorkBuddy", "resources", "app.asar")


def scan(asar: str, needle: str, before: int, after: int, maxhits: int) -> int:
    if not os.path.exists(asar):
        print(f"❌ 找不到 app.asar：{asar}")
        return 1
    nb = needle.encode("utf-8")
    print(f"asar = {asar}\nsize = {os.path.getsize(asar):,}  needle = {needle!r}")
    data = open(asar, "rb").read()
    hits, i = [], 0
    while True:
        j = data.find(nb, i)
        if j < 0:
            break
        hits.append(j)
        i = j + 1
    print(f"命中 {len(hits)} 处")
    for n, j in enumerate(hits[:maxhits]):
        chunk = data[max(0, j - before): j + after]
        txt = chunk.decode("utf-8", "replace")
        print(f"\n{'=' * 18} #{n} @ {j:,} {'=' * 18}\n{txt}")
    return 0


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="按字节搜宿主 app.asar 并打印上下文")
    ap.add_argument("needle", help="要搜的字符串（区分大小写）")
    ap.add_argument("before", nargs="?", type=int, default=600, help="命中点前取多少字节")
    ap.add_argument("after", nargs="?", type=int, default=1200, help="命中点后取多少字节")
    ap.add_argument("maxhits", nargs="?", type=int, default=8, help="最多打印几处")
    ap.add_argument("--asar", default=DEFAULT_ASAR, help="app.asar 路径")
    a = ap.parse_args()
    return scan(a.asar, a.needle, a.before, a.after, a.maxhits)


if __name__ == "__main__":
    sys.exit(main())
