# TripAgent 🧭

LLM + TOPTW 混合行程规划系统 —— **经验提案 → 库内落地 → 求解排程 · 幻觉结构性归零**。

LLM 负责创意提案，本地结构化 POI 库 + 运筹优化负责把提案变成「真实存在、时间可行、不踩闭馆、节奏合理」的可执行行程。LLM 不可用时全链路确定性降级，服务永不中断。

> M1–M7 各阶段里程碑详情见 [docs/README-milestones.md](docs/README-milestones.md)。

## 核心链路（M7 模式）

```
需求文本 → 天数 NLP 提取 → LLM 经验提案（注入库内菜单）
        → 三级落地匹配（精确/包含/LLM 语义 + 分店归一化）
        → 缺口自动二次补点 → 主题里程预算收敛（骑行15/徒步8/亲子15 km·天）
        → 贪婪/TOPTW 排序 → 时间轴硬约束校验 → 两级修复链
        → 结构化行程 + 质量徽章（落地率/违规/闭馆避开）
```

降级路径：LLM 不可用 → 离线规则引擎；ortools 缺失 → M1 贪婪排序；高德不可用 → SVG 手绘图 + 直线折线。

## 功能特性

- **5 城 220 POI**（上海/杭州/南京/苏州/武汉），10,574 对真实路网交通矩阵（OSRM L1 + 高德 L2 精刷，0 缺失）
- **硬约束体系**：营业时间、闭馆日（日期感知）、餐窗美食（每窗 ≤1）、返程酒店、主题每日里程预算（骑行/徒步/亲子）
- **高德地图**：真实底图 + 按日着色 marker + 骑行/步行实际路网折线 + POI 静态图缩略
- **反馈闭环**：日卡「🔁 换一个」—— 同类目邻近换点并重排当天时间轴
- **多城联游**：「苏杭 4 天」自动识别跨城，按天分配逐城规划合并（跨城模式忽略酒店锚点）
- **分享短链**：行程快照 `?share=<id>` 打开即渲染；导出行程单可打印/存 PDF
- **雨天感知**：需求含雨天意图时室内场馆加权（LLM 提示 + 离线排序双路径）
- **公开护栏**：滑动窗口限流（8 次/分/IP）、统计埋点、访客自带 Key 锁内注入

## 快速开始

```bash
# 1. 依赖（唯一可选第三方依赖为 ortools）
pip install -r requirements.txt

# 2. 配置密钥（config.json 不进 git，见 .gitignore）
{
  "deepseek_api_key": "sk-...",   # DeepSeek LLM（缺省走离线兜底）
  "amap_key": "...",              # 高德 Web 服务 Key（路径规划/静态图）
  "amap_js_key": "...",           # 高德 JS API Key（前端底图）
  "m1_model": "deepseek-chat"
}

# 3. 启动（默认 8765；云端读 PORT 环境变量并绑 0.0.0.0）
python webui/server.py 8765

# 4. 打开 http://127.0.0.1:8765
```

数据重建（可选）：

```bash
python scripts/build_travel_cache.py                  # OSRM 全城交通矩阵
AMAP_KEY=xxx python scripts/expand_travel_cache.py    # 高德 L2 精刷新增点
python scripts/eval_regression.py                     # 基准回归（0 违规断言）
```

## API

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/cities` | GET | 城市元信息 + LLM/地图 Key 可用性 |
| `/api/plan` | GET | 规划：`city/query/date/hotel/llm`；天数从 query 提取（1–5，默认 2）；查询含 ≥2 城自动跨城 |
| `/api/replace` | POST | 反馈换点：`{city, day, poi_id, day_ids, used_ids, query, date0, hotel_text}` |
| `/api/route` | GET | 高德路径规划代理：`mode=riding\|walking&o=lng,lat&d=lng,lat`（磁盘缓存） |
| `/api/share` | POST/GET | 行程快照保存 / 读取 |
| `/api/stats` | GET | 调用量/延迟统计 |

## 架构

```
webui/        单页前端（零框架）+ 纯 stdlib HTTP 服务（QuickBindServer）
src/          proposal_planner(M7 提案+落地) / m2_planner(TOPTW 求解) / m1_planner(贪婪)
              sequencer(时间轴+硬约束+修复链+主题画像) / offline_planner(离线兜底)
data/         *_pois.json ×5 城 / travel_cache.json / route_cache.json / shares/
scripts/      交通缓存构建与 POI 扩库脚本 / eval_regression.py 基准回归
```

## 免责

行程时间为估算，出发前请再次确认景点当日开放情况。高德/OSRM/DeepSeek 服务条款适用其各自平台。
