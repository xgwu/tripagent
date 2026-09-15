#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一测试 runner：一次跑完全部 Python 单测 + 前端 JS 守卫。

为什么需要它
------------
1. `scripts/test_*.py` 已有 13 个，覆盖面彼此有交叠，手工逐个跑必然漏
   （本仓库吃过「只跑了前几个」的亏）。
2. `release_gate.sh` 原先用 `for t in scripts/test_*.py` 通配，只匹配 .py，
   **漏掉 node 的 `test_webui_food_mark.js`**。
3. 编排逻辑写在 shell 里，在本机（Git Bash shim 故障：dirname/grep/head 一律
   not found）根本执行不了 → 门禁形同虚设。放进 Python 后本机与 CI 同一条命令。

用法
----
    python scripts/run_tests.py                  # 跑全部
    python scripts/run_tests.py gap named        # 只跑文件名含 gap 或 named 的
    python scripts/run_tests.py --list           # 只列出将要跑的用例
    python scripts/run_tests.py --strict         # 缺 node 时把 JS 用例记为失败（默认跳过）
    python scripts/run_tests.py -v               # 失败时打印完整输出（默认只打尾部 25 行）

退出码：0 全绿 / 1 有失败 / 2 无用例可跑
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TIMEOUT_S = 300
TAIL_LINES = 25


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONPATH", str(ROOT))
    return env


def _find_node() -> str | None:
    """定位 node：$NODE_BIN → PATH → managed 运行时目录（跨机器，不硬编码绝对路径）。"""
    cand = os.environ.get("NODE_BIN")
    if cand and Path(cand).exists():
        return cand
    found = shutil.which("node")
    if found:
        return found
    hits: list[Path] = []
    for pat in (".workbuddy/binaries/node/versions/*/node.exe",
                ".workbuddy/binaries/node/versions/*/bin/node"):
        hits += list(Path.home().glob(pat))
    return str(sorted(hits)[-1]) if hits else None


def _node_path() -> str | None:
    """前端守卫若需要第三方包，从 managed node workspace 取 NODE_PATH。"""
    cand = os.environ.get("NODE_PATH")
    if cand:
        return cand
    p = Path.home() / ".workbuddy" / "binaries" / "node" / "workspace" / "node_modules"
    return str(p) if p.exists() else None


def collect(filters: list[str]) -> list[tuple[str, Path]]:
    cases: list[tuple[str, Path]] = []
    for p in sorted(SCRIPTS.glob("test_*.py")):
        cases.append(("py", p))
    for p in sorted(SCRIPTS.glob("test_*.js")):
        cases.append(("js", p))
    if filters:
        low = [f.lower() for f in filters]
        cases = [c for c in cases if any(f in c[1].name.lower() for f in low)]
    return cases


def run_case(kind: str, path: Path, strict: bool) -> tuple[str, str, str, float]:
    """返回 (状态, 说明, 输出, 耗时秒)。"""
    env = _env()
    if kind == "py":
        cmd = [sys.executable, str(path)]
    else:
        node = _find_node()
        if not node:
            msg = "未找到 node（可设 NODE_BIN，或确保 node 在 PATH）"
            return ("FAIL" if strict else "SKIP"), msg, "", 0.0
        cmd = [node, str(path)]
        np = _node_path()
        if np:
            env["NODE_PATH"] = np
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "FAIL", f"超时（>{TIMEOUT_S}s）", "", time.time() - t0
    except OSError as exc:
        return "FAIL", f"启动失败：{exc}", "", time.time() - t0
    out = r.stdout or ""
    if (r.stderr or "").strip():
        out += ("\n" if out else "") + r.stderr
    return ("PASS" if r.returncode == 0 else "FAIL"), "", out, time.time() - t0


def _tail(text: str, verbose: bool) -> str:
    lines = text.rstrip().splitlines()
    if not verbose and len(lines) > TAIL_LINES:
        lines = [f"    ...（省略前 {len(lines) - TAIL_LINES} 行，-v 看全量）"] + lines[-TAIL_LINES:]
    return "\n".join("    " + ln for ln in lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="统一测试 runner（Python 单测 + 前端 JS 守卫）")
    ap.add_argument("filters", nargs="*", help="只跑文件名含这些关键词的用例")
    ap.add_argument("--list", action="store_true", help="只列出用例，不执行")
    ap.add_argument("--strict", action="store_true", help="缺 node 时把 JS 用例记为失败")
    ap.add_argument("-v", "--verbose", action="store_true", help="失败时打印完整输出")
    args = ap.parse_args()

    cases = collect(args.filters)
    if not cases:
        print("❌ 没有匹配到任何用例（检查关键词，或用 --list 看看有哪些）")
        return 2
    if args.list:
        for kind, path in cases:
            print(f"  [{kind}] {path.relative_to(ROOT)}")
        print(f"共 {len(cases)} 个用例")
        return 0

    py_n = sum(1 for k, _ in cases if k == "py")
    js_n = len(cases) - py_n
    print(f"== 统一测试 runner：{py_n} 个 Python 单测 + {js_n} 个前端守卫 ==")
    print(f"   python: {sys.executable}")
    if js_n:
        node = _find_node()
        print(f"   node  : {node or '（未找到 → JS 用例将跳过）'}")

    results: list[tuple[str, str, Path, str, str, float]] = []
    t_all = time.time()
    for part, title in (("py", "Python 单测"), ("js", "前端 JS 守卫")):
        group = [(k, p) for k, p in cases if k == part]
        if not group:
            continue
        print(f"\n-- {title}（{len(group)} 个）--")
        for kind, path in group:
            status, note, out, secs = run_case(kind, path, args.strict)
            results.append((status, kind, path, note, out, secs))
            mark = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
            print(f"  {mark}  {path.name:<32} {secs:6.1f}s {note}")

    failed = [r for r in results if r[0] == "FAIL"]
    skipped = [r for r in results if r[0] == "SKIP"]
    passed = [r for r in results if r[0] == "PASS"]

    if failed:
        print("\n== 失败详情 ==")
        for _s, _k, path, note, out, _t in failed:
            print(f"\n--- {path.name} {note} ---")
            print(_tail(out, args.verbose) if out.strip() else "    （无输出）")

    total_s = time.time() - t_all
    print(f"\n{len(passed)} passed / {len(failed)} failed / {len(skipped)} skipped"
          f"  （总耗时 {total_s:.1f}s）")
    if skipped:
        print("⚠️  跳过：" + "、".join(r[2].name for r in skipped) +
              "（设 NODE_BIN 指向 node 可纳入；--strict 可把跳过当失败）")
    if failed:
        print("❌ 测试未全绿：" + "、".join(r[2].name for r in failed))
        return 1
    print("🟢 全部用例通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
