# -*- coding: utf-8 -*-
"""把一个新城注册到所有消费方（8 个编辑点，4 个文件）。纯文本 splice，保留原格式。

为什么单独一个脚本：onboard_from_poilist.py 只负责落 POI 库与缓存，
而注册要改 4 个文件里各存一份的城市元数据副本，分开做便于 review。

**必须同时改 expand_travel_cache 的 CITIES 和 NEW_IDS**：后者用 `NEW_IDS[city]`
直接下标（不是 `.get()`），只加 CITIES 会在运行时 KeyError；
而 test_city_registry 的守卫只查 CITIES —— 属于「守卫过、运行炸」的漏网情形。

用法：
  python scripts/register_city.py --city 贵阳 --prefix GY --citycode 0851 [--dry]
"""
import argparse
import ast
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


def targets(city, prefix, citycode):
    """(文件, 变量名, 类型, 键, 值文本)。类型 list 时键=值。"""
    return [
        ("webui/server.py", "CITIES", "list", city, city),
        ("webui/server.py", "_ID_PREFIX_CITY", "dict", prefix, f'"{city}"'),
        ("webui/server.py", "_CITYCODE", "dict", city, f'"{citycode}"'),
        ("scripts/fetch_poi_photos.py", "CITYCODE", "dict", city, f'"{citycode}"'),
        ("scripts/expand_travel_cache.py", "CITIES", "list", city, city),
        ("scripts/expand_travel_cache.py", "NEW_IDS", "dict", city, "[]"),
        ("eval_m7.py", "CITIES", "list", city, city),
        ("eval_ab.py", "CITIES", "list", city, city),
    ]


def literal_span(src, name):
    """返回 `name = {...}` / `name = [...]` 的 (开括号, 闭括号, 末个有效字符) 下标。

    不直接用 ast 的 end_col_offset：它是 **UTF-8 字节**偏移，而这些文件含中文，
    字节偏移与字符偏移不等长，当地址用会定位错位（实测拿到 'g'）。
    这里改为自行扫描括号，跳过字符串与注释，与编码无关。
    """
    m = re.search(rf"(?m)^{re.escape(name)}\s*=\s*", src)
    if not m:
        return None
    i = m.end()
    if src[i] not in "[{":
        return None
    op = src[i]
    cl = "]" if op == "[" else "}"
    depth, j, last = 0, i, None
    while j < len(src):
        c = src[j]
        if c in "\"'":                       # 跳过字符串（内含括号/井号不算）
            q, j = c, j + 1
            while j < len(src):
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == q:
                    break
                j += 1
            last = j                          # 字符串内容算「有效字符」
            j += 1
            continue
        if c == "#":                         # 跳过注释
            while j < len(src) and src[j] != "\n":
                j += 1
            continue
        if c == op:
            depth += 1
        elif c == cl:
            depth -= 1
            if depth == 0:
                return i, j, last
        if not c.isspace():
            last = j
        j += 1
    return None


def splice(src, span, entry):
    """把 entry 插到**最后一个条目所在行的行尾**，保持原缩进风格。

    不能简单地插在闭括号前：`"广州": [],   # 注释` 这种行尾注释会被甩到新条目后面，
    注释就挂错了对象。
    """
    i, j, k = span
    has_comma = src[k] == ","
    line_start = src.rfind("\n", 0, k) + 1
    line = src[line_start:k + 1]
    indent = line[:len(line) - len(line.lstrip())]
    line_end = src.find("\n", k)
    if line_end == -1 or line_end > j:        # 末条目与闭括号同行 → 直接插在闭括号前
        return src[:j] + (" " if has_comma else ", ") + entry + src[j:]
    return (src[:line_end] + ("" if has_comma else ",") + "\n" + indent
            + entry + src[line_end:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", required=True)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--citycode", required=True)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    if len(a.prefix) != 2 or not a.prefix.isalpha():
        raise SystemExit(f"❌ 前缀必须是 2 个字母（守卫用 id[:2] 查表），当前 {a.prefix!r}")

    # 先全量检查（不写盘），再统一落盘：任一处失败不留半成品
    planned = []          # (path, var, entry_text 或 None=已存在)
    for path, var, kind, key, val in targets(a.city, a.prefix, a.citycode):
        src = io.open(path, encoding="utf-8", newline="").read()
        span = literal_span(src, var)
        if span is None:
            raise SystemExit(f"❌ {path} 里找不到 {var} = [...]/{{...}}")
        lit = ast.literal_eval(src[span[0]:span[1] + 1])
        present = (key in lit) if kind == "dict" else (val in lit)
        entry = f'"{val}"' if kind == "list" else f'"{key}": {val}'
        planned.append((path, var, None if present else entry))

    for path, var, entry in planned:
        print(f"  {'·' if entry is None else '✅'} {path:<34}{var:<20}"
              f"{'已存在' if entry is None else '新增'}")

    if a.dry:
        print("\n[dry-run] 未写盘。")
        return

    edited = {}
    for path, var, entry in planned:
        if entry is None:
            continue
        src = edited.get(path) or io.open(path, encoding="utf-8",
                                          newline="").read()
        span = literal_span(src, var)   # splice 会移动偏移，逐次重新定位
        edited[path] = splice(src, span, entry)
    for path, src in edited.items():
        io.open(path, "w", encoding="utf-8", newline="").write(src)
        print(f"  写出 {path}")

    print(f"\n✅ {a.city}（{a.prefix} / {a.citycode}）注册完成。"
          f"跑 python scripts/test_city_registry.py 核验。")


if __name__ == "__main__":
    main()
