#!/usr/bin/env bash
# TripAgent 城市 onboarding 一条命令流水线（固化自武汉扩库经验）。
#
# 用法（在项目根目录执行）：
#   scripts/onboard_city.sh new <城市> [--prefix XX ...]   # 新城市：六步流水线（add_city.py）
#   scripts/onboard_city.sh expand <城市>                  # 已有城市扩库后回填
#
# expand 四步（全部幂等，可中断重跑）：
#   1. 交通缓存回填   expand_travel_cache.py --auto（自动检测缓存无记录的新点，L1 OSRM + L2 高德）
#   2. 照片补齐       fetch_poi_photos.py（已缓存有图的点自动跳过）
#   3. 闭馆日回填     backfill_closed_days.py（从 note 提取「周X闭馆」）
#   4. 离线冒烟       eval_regression.py（12 用例 0 违规才算过）
#
# 注意：
#   - 需要 secrets.json 提供 amap_key（deepseek_key 冒烟不需要）
#   - expand 前请确认新 POI 已人工入库（坐标/时长/标签），本脚本只做回填不做采集
#   - 严禁对 travel_cache 做 --force 全量重建，会覆盖已刷的 L2 高德数据
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-/Users/wuxiaogang/.workbuddy/binaries/python/envs/default/bin/python}"
[ -x "$PY" ] || PY=python3

MODE="${1:-}"; shift || true

case "$MODE" in
  new)
    CITY="${1:-}"; [ -n "$CITY" ] || { echo "用法: onboard_city.sh new <城市> [--prefix XX]"; exit 1; }
    shift
    echo "==> [1/2] add_city.py 六步流水线（采集/校坐标/建缓存/照片/注册/冒烟）"
    "$PY" scripts/add_city.py "$CITY" "$@"
    echo "==> [2/2] 闭馆日回填"
    "$PY" scripts/backfill_closed_days.py
    echo "完成。提醒：add_city 采集的营业信息是高德快照，closed_days 已回填但建议抽查；"
    echo "新城市未进 CITIES 评测名单的话，手动把 expand_travel_cache.py CITIES / eval 相关列表补上。"
    ;;
  expand)
    CITY="${1:-}"; [ -n "$CITY" ] || { echo "用法: onboard_city.sh expand <城市>"; exit 1; }
    N=$( "$PY" -c "import json,io;print(len(json.load(io.open('data/${CITY}_pois.json',encoding='utf-8'))['pois']))" )
    echo "==> $CITY 当前库存 $N 个 POI"
    echo "==> [1/4] 交通缓存回填（--auto 自动检测新点）"
    "$PY" scripts/expand_travel_cache.py --auto
    echo "==> [2/4] 照片补齐"
    "$PY" scripts/fetch_poi_photos.py
    echo "==> [3/4] 闭馆日回填"
    "$PY" scripts/backfill_closed_days.py
    echo "==> [4/4] 离线回归冒烟"
    "$PY" scripts/eval_regression.py
    echo "完成：$CITY 扩库回填全链路结束。建议再跑一次 eval_m7.py --city $CITY 看 M7 落地率变化。"
    ;;
  *)
    grep '^#' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
