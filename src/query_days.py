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

MAX_DAYS = 5      # **默认**规划上限（求解与库容量口径）—— 见 days_cap_for()
DEFAULT_DAYS = 2  # 完全提不到天数时的默认值

# 🔴 2026-10-14（新疆开城前置）：**天数上限按目的地类型放宽**。
#   原单一常量 MAX_DAYS=5 对「大尺度目的地」是硬伤：新疆北疆环线 7-8 天、
#   南北疆 12-15 天，5 天上限下「开了城也只能玩 1/4」。而放宽到全局又会
#   让市内 2 日游多算（求解预算/LLM 成本随天数线性涨）。
#   ⇒ 按「目的地类型」给上限：自驾/大区线放宽、市内游保持 5。
#   ⚠️ 判据复用现有主题画像（sequencer.THEME_PROFILES 的正则），**不新造正则**
#     —— 两套判据必然漂移，是本项目的老坑（见 select_rules 注释）。
#   ⚠️ 本函数是**天数上限的唯一真相**：webui / MCP / 自检一律调它，
#     别再各自硬编码 5 或 7（此前 MCP 写 7、webui 写 5，已漂移）。
DAYS_CAP_DEFAULT = 5        # 默认（市内/景区城）：与旧行为一致
DAYS_CAP_SELF_DRIVE = 10    # 自驾长途线（「自驾/租车/road trip」）：北疆环线 7-8 天
DAYS_CAP_MULTI_CITY = 12    # 多城联游（≥3 城）：南北疆 12-15 天取 12 保可控

_SELF_DRIVE_RE = re.compile(r"自驾|自己开车|开车去|租车|驾车|road\s*trip|driving",
                            re.IGNORECASE)

# 🔴 「大尺度目的地」判据：这些目的地的行程天然 >5 天（环线/大区线），
#   即使用户没说「自驾」。判据是**目的地本身**（与是否开车无关）——
#   北疆环线 7-8 天、南北疆 12-15 天，跟怎么去没关系。
#   ⚠️ 只列「确属大尺度」的；别把普通景区城（如喀纳斯单城）算进来。
_LONG_HAUL_CITIES = (
    "乌鲁木齐", "喀纳斯", "伊犁", "阿勒泰", "吐鲁番", "哈密", "库尔勒",
    "喀什", "阿克苏", "库车", "那拉提", "巴音布鲁克", "禾木", "布尔津",
    "赛里木湖", "塔县", "和田", "克拉玛依", "天池", "可可托海",
)


def days_cap_for(cities=None, query: str = "") -> int:
    """该次规划的天数上限（按目的地类型/出行方式动态）。

    cities：目的地清单（多城联游时 >1）。数量越多上限越高。
    query ：原始需求（识别自驾主题 / 大尺度地名）。
    取各判据的**最大值**（宽松侧），保证「自驾 + 多城」不被小值卡住。

    ⚠️ 判据只影响**上限**，不改用户实际天数：用户说 3 天就是 3 天，
       只有「需求 > 上限」才截断并显式提示。
    """
    cap = DAYS_CAP_DEFAULT
    cl = [c for c in (cities or []) if c]
    n_cities = len(cl)
    if n_cities >= 3:
        cap = max(cap, DAYS_CAP_MULTI_CITY)
    elif n_cities == 2:
        cap = max(cap, DAYS_CAP_SELF_DRIVE)
    # 大尺度目的地（新疆/长线大区）：其行程天然 >5 天
    if any(c in _LONG_HAUL_CITIES for c in cl):
        cap = max(cap, DAYS_CAP_SELF_DRIVE)
    if query:
        if _SELF_DRIVE_RE.search(query):
            cap = max(cap, DAYS_CAP_SELF_DRIVE)
        # 需求里点名了大尺度地名（用户没说具体城市时也能放宽）
        if any(c in query for c in _LONG_HAUL_CITIES):
            cap = max(cap, DAYS_CAP_SELF_DRIVE)
    return cap


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


def clamp_days(raw, default: int = DEFAULT_DAYS, cap: int | None = None) -> tuple:
    """(可用天数, 是否被上限截断, 用户原始诉求)。

    raw 为 extract_days 的结果（可能是 None 或 >cap 的真实值）。
    cap 为本次天数上限；不传时回落 MAX_DAYS（**向后兼容**旧调用点）。
    被截断时调用方**必须**把 clamped 透出给用户，不能静默改需求。
    """
    lim = int(cap) if cap else MAX_DAYS
    if not raw:
        return default, False, None
    n = int(raw)
    if n > lim:
        return lim, True, n
    return max(1, n), False, n
