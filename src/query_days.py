# -*- coding: utf-8 -*-
"""自然语言天数提取（webui 与 CLI 共用单一实现）。

从 query 提取行程天数：「3天」「玩 4 天」「两日」「一天」…，提不到返回 None。
数字后必须跟 天/日（「带5岁孩子」不会误匹配）。先剥离日期表达式，
避免「10月1日」的「1日」被误读成 1 天。

⚠️ 2026-09-17 修复：此前正则只认 1-5（`[1-5一二两三四五]`），用户说「6天」
时 **返回 None**，上层 `extract_days(query) or 2` 于是静默回落成 **2 天**——
既没给出可支持的上限 5，也没有任何提示，是最糟的一种失败方式。
现在解析 1-10 的真实值，**是否收敛到上限由调用方用 clamp_days 决定并告知用户**。
"""
import re

_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

MAX_DAYS = 5      # 规划上限（求解与库容量口径）
DEFAULT_DAYS = 2  # 完全提不到天数时的默认值


def extract_days(query: str):
    """从自然语言需求提取行程天数（**真实值**，可能超过 MAX_DAYS），提不到返回 None。"""
    q = (query or "")
    # 日期区间「10月1日到3日」→ 天数 = 3-1+1（同月内；跨月不处理，走默认天数）
    mr = re.search(r"(\d{1,2})月(\d{1,2})[日号]\s*[到至]\s*(\d{1,2})[日号]", q)
    if mr:
        b, c = int(mr.group(2)), int(mr.group(3))
        if b <= c and 1 <= c - b + 1 <= 10:
            return c - b + 1
    q = re.sub(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日号]?", "", q)  # 2026-10-01 / 2026年10月1日
    q = re.sub(r"\d{1,2}月\d{1,2}[日号]", "", q)                   # 10月1日 / 9月30号
    # 日期残片清理（不伤「3日亲子游」这类天数表达）：
    # 两位数「11日」「30号」必是日期（行程天数上限 10）；「到3日」这类连接词后残片同理
    q = re.sub(r"\d{2}[日号]", "", q)
    q = re.sub(r"(?<=[到至,—-])\d{1,2}[日号]", "", q)
    m = re.search(r"([1-9]|10|[一二两三四五六七八九十])\s*[天日]", q)
    if not m:
        return None
    c = m.group(1)
    return int(c) if c.isdigit() else _CN_NUM[c]


def clamp_days(raw, default: int = DEFAULT_DAYS) -> tuple:
    """(可用天数, 是否被上限截断, 用户原始诉求)。

    raw 为 extract_days 的结果（可能是 None 或 >MAX_DAYS 的真实值）。
    被截断时调用方**必须**把 clamped 透出给用户，不能静默改需求。
    """
    if not raw:
        return default, False, None
    n = int(raw)
    if n > MAX_DAYS:
        return MAX_DAYS, True, n
    return max(1, n), False, n
