#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布门禁：语法编译 → 全部单测 → 离线回归 → 城市库校验。全绿才允许发布。

为什么是 Python 而不是 shell
----------------------------
原 `release_gate.sh` 依赖 `dirname` / `grep` / `mktemp` / `head`，在本机
（Git Bash shim 故障，这些命令一律 not found）根本执行不了 → 门禁形同虚设，
每次发版只能手工逐个跑测试。改为纯 Python 实现后，**本机与 CI 是同一条命令**，
shell 版只留一个薄兼容入口。

用法
----
    python scripts/release_gate.py            # 完整四步
    python scripts/release_gate.py --fast     # 跳过回归评测（本地快速自检）
    python scripts/release_gate.py --step 2   # 只跑第 2 步（单测）

退出码：0 全绿 / 1 某一步失败
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
STEPS = 4


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONPATH", str(ROOT))
    return env


def _run(args: list[str], label: str) -> tuple[bool, str]:
    """跑一条命令，返回 (是否成功, 合并输出)。失败时实时把输出打到屏幕。"""
    t0 = time.time()
    r = subprocess.run(args, cwd=str(ROOT), env=_env(), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "")
    if (r.stderr or "").strip():
        out += ("\n" if out else "") + r.stderr
    print(f"   （{label} 耗时 {time.time() - t0:.1f}s）")
    return r.returncode == 0, out


def step1_compile() -> bool:
    print(f"== 1/{STEPS} 语法编译 ==")
    ok, out = _run([PY, "-m", "compileall", "-q", "src", "webui", "scripts"], "compileall")
    if not ok:
        print(out)
    return ok


def step2_unit() -> bool:
    print(f"\n== 2/{STEPS} 单元测试（统一 runner，含前端 JS 守卫）==")
    ok, out = _run([PY, str(ROOT / "scripts" / "run_tests.py")], "run_tests")
    print(out.rstrip())
    return ok


def step3_regression() -> bool:
    print(f"\n== 3/{STEPS} 离线回归 ==")
    ok, out = _run([PY, str(ROOT / "scripts" / "eval_regression.py")], "eval_regression")
    tail = out.rstrip().splitlines()[-3:]
    print("\n".join("   " + ln for ln in tail))
    return ok


def step4_validate() -> bool:
    print(f"\n== 4/{STEPS} 城市库校验 ==")
    ok, out = _run([PY, str(ROOT / "scripts" / "validate_city.py")], "validate_city")
    n_pass = out.count("✅ 通过")
    if not ok:
        print(out)
        print("❌ 城市库校验未通过")
        return False
    print(f"   {n_pass} 个城市通过")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="发布前门禁（纯 Python 实现）")
    ap.add_argument("--fast", action="store_true", help="跳过第 3 步（离线回归）")
    ap.add_argument("--step", type=int, choices=range(1, STEPS + 1), help="只跑指定步骤")
    args = ap.parse_args()

    print(f"发布门禁（cwd={ROOT}）")
    print(f"   python: {PY}\n")

    todo = list(range(1, STEPS + 1))
    if args.fast:
        todo = [s for s in todo if s != 3]
    if args.step:
        todo = [args.step]

    fn = {1: step1_compile, 2: step2_unit, 3: step3_regression, 4: step4_validate}
    for s in todo:
        if not fn[s]():
            print(f"\n❌ 第 {s} 步未通过，门禁中止")
            return 1
    print("\n🟢 门禁全绿，可以发布")
    return 0


if __name__ == "__main__":
    sys.exit(main())
