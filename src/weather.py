# -*- coding: utf-8 -*-
"""行程天气/节假日感知（提示层，不改变规划结果）。

数据源：open-meteo 免费接口（无需 key），按城市中心坐标取未来日期的
日降水概率。任何网络/解析失败都静默返回空——天气提示缺失不影响规划。
另提供周末/法定假日前的出行提示（纯日期计算，0 外部依赖）。
"""
import datetime as _dt
import json
import urllib.request

OPEN_METEO = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lng}"
              "&daily=precipitation_probability_max&timezone=auto"
              "&start_date={d0}&end_date={d1}")
RAIN_P = 60          # 降水概率 ≥60% 视为雨天
_CACHE: dict = {}    # (city, date0, days) → [{date, rain_p}]，进程内缓存


def _fmt_d(d: _dt.date) -> str:
    return d.isoformat()


def fetch_daily(city: dict, date0: str, days: int) -> list | None:
    """取 date0 起 days 天的日降水概率列表 [{"date","rain_p"}]；失败返回 None。"""
    try:
        d0 = _dt.date.fromisoformat(date0)
    except ValueError:
        return None
    if days < 1 or days > 16:  # open-meteo 免费档预报窗口
        return None
    key = (city.get("city"), date0, days)
    if key in _CACHE:
        return _CACHE[key]
    url = OPEN_METEO.format(lat=city["center"]["lat"], lng=city["center"]["lng"],
                            d0=_fmt_d(d0), d1=_fmt_d(d0 + _dt.timedelta(days=days - 1)))
    out = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tripagent/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        probs = data["daily"]["precipitation_probability_max"]
        out = [{"date": _fmt_d(d0 + _dt.timedelta(days=i)),
                "rain_p": int(p) if p is not None else 0}
               for i, p in enumerate(probs)]
    except Exception:  # noqa: 网络失败/超时/字段缺失一律降级为无天气提示
        out = None
    _CACHE[key] = out
    return out


def trip_notices(city: dict, date0: str | None, days: int) -> list:
    """生成天气/节假日类通知（notices 口径：[{type, message, ...}]）。"""
    notices: list = []
    if not date0:
        return notices
    try:
        d0 = _dt.date.fromisoformat(date0)
    except ValueError:
        return notices
    # 节假日/周末提示：周六日出发提示早到避峰（法定假日表需外部依赖，此处覆盖双休）
    if d0.weekday() >= 5:
        notices.append({"type": "weekend", "date": date0,
                        "message": f"{date0} 为周末，热门景点人流较高，建议开园即到、错峰用餐"})
    # 降雨提示：逐日降水概率 ≥60% 提示室内备选
    daily = fetch_daily(city, date0, days)
    if daily:
        rainy = [x for x in daily if x["rain_p"] >= RAIN_P]
        if rainy:
            seg = "、".join(f"Day {i + 1}（{x['date']} {x['rain_p']}%）"
                            for i, x in enumerate(rainy))
            notices.append({"type": "rain", "days": [i + 1 for i, _ in enumerate(rainy)],
                            "message": f"{seg} 降水概率较高，建议安排博物馆等室内点位并备雨具"})
    return notices
