# TripAgent 🧭

LLM 世界知识 × OR-Tools TOPTW 混合行程规划系统 —— **世界知识主导选点，确定性系统负责落地 + 约束 + 求解**。

通用大模型的旅行「品味」很好，但直接生成的行程不可行：地点幻觉、营业时间冲突、跨天重复、里程不现实。TripAgent 让 LLM 凭世界知识决定「去哪、为什么去」，本地结构化 POI 库 + 运筹优化保证行程**物理可行**（真实路网时间、营业窗口、闭馆日、餐窗、里程预算）且**结构最优**（TOPTW 利润最大化）。防幻觉不靠限制提案，而靠落地匹配——未落地的提案自动成为「POI 库缺口采购清单」，反向驱动数据扩容。LLM 不可用时全链路确定性降级，服务永不中断。

> 🌐 **线上运行中**：https://tripagent-planner2.app.workbuddy.host/
>
> 📖 深度文档：[里程碑详情](docs/README-milestones.md) · [项目报告 v2](docs/TripAgent-项目报告.html) · [架构设计](docs/TripAgent-架构设计.html) · [TOPTW 算法报告](docs/TripAgent-TOPTW算法报告.html)

| 7 城市 | 395 POI | 25,726 对交通缓存 | 评测硬违规 | M7 落地率 | 端到端耗时 |
|---|---|---|---|---|---|
| 沪/京/宁/蓉/杭/汉/苏 | 结构化库（三源核实） | OSRM L1 + 高德 L2 实况 | **0**（五城×双方案 20/20） | **100%**（10/10 组） | **12.9s**（优化前 23.3s，-45%） |

## 核心链路（M7 经验提案模式）

```
需求文本 → 0·多轮澄清（LLM 保守判断需求缺失，至多追问一问，选项卡片交互）
        → A·世界知识提案（LLM 自由生成多日行程：主选 + note + alternates + 招牌体验规则）
        → B·落地匹配（四级回库：精确 → 包含 → 模糊 difflib≥0.62 → LLM 辅助；失败记入库缺口）
        → C·TOPTW 求解（OR-Tools 逐日：营业时间窗 + 酒店锚点 + 闭馆硬过滤 + 利润函数）
        → D·闭环与文案（跨天重平衡 → 缺口/剔除回传 LLM 修正 ≤2 轮 → 文案对齐最终时间轴）
```

**四层防护**保证产出质量：

- **提案层**：招牌体验规则（亲子→迪士尼）、全天大点独占日、雨天室内引导、傍晚密度规则
- **守门层**：远郊大点片区守门——距市中心 >12km 的 4h+ 大点与 10km 外片区错配混排时，求解前自动重排到最近的天；family 严格档下 5h+ 远郊点独占一日
- **求解层**：主选高利润 + LLM 备选低权重 + 地理邻近检索备选（双源）入池，不可行按利润权衡换点；补位优先同主题备选（动物换动物）
- **闭环层**：落地缺口 / 落地率<85% / 主选被剔 ≥2 处 → 带原因回传修正再求解（≤2 轮防震荡）；需求标签（动物/博物馆等 16 类白名单）点位被剔光且无同主题保留 → 前端 ⚠️ 明确通知，而非静默丢失

降级路径：LLM 不可用 → 离线规则引擎；ortools 缺失 → 贪婪排序；高德不可用 → SVG 手绘图 + 直线折线；天气/节假日接口失败 → 静默跳过。

## 功能特性

- **7 城 395 POI**（成都 82 / 北京 75 / 上海 68 / 杭州 57 / 武汉 50 / 南京 33 / 苏州 30），25,726 对真实路网交通缓存（L1 OSRM + L2 高德实况，L3 直线兜底），396 点实拍图缓存
- **硬约束体系**：营业时间、闭馆日（日期感知，全库回填）、餐窗美食（每窗 ≤1，咖啡馆非正餐分流）、返程酒店锚点、主题每日里程预算（骑行/徒步/亲子）
- **远郊守门 + 主题保真**：几何合理性事前保证（而非修复链事后剔点误杀主选）；补位同主题优先 + 需求丢失显式通知
- **多轮澄清**：需求缺失时保守追问一轮（选项卡片），已与需求抽取合并为单次 LLM 调用
- **性能工程**：天复用增量重解 + TOPTW 线程池并行 + at-solution 提前停止 + 提案级缓存（7 天 TTL）——端到端 23.3s → 首次 12.9s / 缓存命中 8.3s，质量零回归
- **天气/节假日感知**：open-meteo 免 key，≥60% 降水提示室内 + 备雨具
- **高德地图**：真实底图 + 按日着色 marker + 骑行/步行实际路网折线 + POI 静态图缩略
- **反馈闭环**：日卡「🔁 换一个」—— 同主题邻近换点并重排当天时间轴（确定性补位优先）
- **多城联游**：「苏杭 4 天」自动识别跨城，按天分配逐城规划合并
- **分享与导出**：行程快照短链 `?share=<id>`、PNG 长图 / ICS 日历导出、行程单打印/存 PDF
- **五段式进度条**：澄清→提案→落地→求解→文案分阶段轮询，等待可感知
- **公开护栏**：滑动窗口限流（8 次/300s/IP）、统计埋点、访客自带 Key 锁内注入

## 快速开始

```bash
# 1. 依赖（唯一可选第三方依赖为 ortools）
pip install -r requirements.txt

# 2. 配置密钥：复制 secrets.example.json 为 secrets.json 并填入真实值（secrets.json 不进 git）
#    deepseek_api_key / amap_key（Web 服务）/ amap_js_key（前端 JSAPI）
#    业务配置（模型名、功能开关等）放 config.json，随 git 提交

# 3. 启动（默认 8765；云端读 PORT 环境变量并绑 0.0.0.0）
python webui/server.py 8765

# 4. 打开 http://127.0.0.1:8765
```

CLI 单次规划（注意：CLI 从环境变量读密钥，`webui/server.py` 会自动注入 secrets.json）：

```bash
python main.py "带5岁孩子去杭州玩2天，不要太累，最好有动物或者博物馆" --city 杭州 --days 2 --proposal --date 2026-10-01
```

## 工程化命令

```bash
python scripts/eval_regression.py [--llm]        # 回归评测：13 固化用例，离线 CI / --llm 真实全链路
python scripts/probe_perf.py                     # 性能探针：LLM/TOPTW 各环节耗时插桩
PY=python3 bash scripts/release_gate.sh          # 发布门禁：编译→单测→回归→7 城库校验（push/PR 由 GitHub Actions 自动执行）
python scripts/validate_city.py                  # POI 库全量校验
scripts/onboard_city.sh new 广州 --prefix GZ     # 新城市 onboarding（六步流水线）
scripts/onboard_city.sh expand 上海              # 已有城市扩库回填（四步）
python scripts/gap_report.py                     # POI 库缺口台账汇总（线上真实使用缺口持久化）
python scripts/build_travel_cache.py             # OSRM 全城交通矩阵重建
AMAP_KEY=xxx python scripts/expand_travel_cache.py  # 高德 L2 精刷新增点
```

## API

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/cities` | GET | 城市元信息 + LLM/地图 Key 可用性 |
| `/api/clarify` | POST | 多轮澄清（保守追问一轮，选项卡片） |
| `/api/plan` | GET | 规划：`city/query/date/hotel/llm`；天数从 query 提取（1–5，默认 2）；查询含 ≥2 城自动跨城 |
| `/api/progress` | GET | 规划进度轮询（五段式分阶段状态） |
| `/api/replace` | POST | 反馈换点：同主题邻近换点并重排当天时间轴 |
| `/api/route` | GET | 高德路径规划代理：`mode=riding\|walking&o=lng,lat&d=lng,lat`（磁盘缓存） |
| `/api/photo` | GET | POI 实拍图（缓存 396 点，缺失瓦片兜底） |
| `/api/share` | POST/GET | 行程快照保存 / 读取 |
| `/api/stats` | GET | 调用量/延迟/补位命中率统计 |

## 架构

```
webui/        单页前端（零框架，~700 行）+ 纯 stdlib HTTP 服务（~1,000 行，QuickBindServer）
src/          16 模块 ~3,300 行
              proposal_planner(M7 提案+落地+守门) / m2_planner(TOPTW 求解) / m1_planner(贪婪)
              sequencer(时间轴+硬约束+修复链+跨天重平衡) / toptw(OR-Tools 建模)
              weather(天气感知) / gap_log(POI 缺口台账) / hotel(住宿锚点) / offline_planner(离线兜底)
data/         *_pois.json ×7 城 / travel_cache.json / route_cache.json / photo_cache.json / shares/
scripts/      20+ 运维与测试脚本（扩城/扩库/校验/门禁/探针/回归/缺口台账）
.github/      ci.yml + gate.yml —— push/PR 自动执行发布门禁
```

评测基线（eval_m7.py · 5 城 × 2 persona × 2 方案，2026-09-12 重跑）：

| 指标 | M2（库内直选） | M7（世界知识提案） |
|---|---|---|
| 硬约束违规（10 组合计） | 0 | 0 |
| 落地率 | — | 10/10 组全部 100% |
| 库缺口 | — | 0（首轮 24 个已全部消化） |
| 日均 POI 数（均值） | 8.7 | 9.1 |
| 端到端耗时 | 10.4s | 12.9s |

## 里程碑（M1–M10）

- **M1** 验证假设：LLM 库内组线，幻觉结构性归零
- **M2** 求解层：OR-Tools TOPTW（营业硬窗+利润函数）+ 每日文案重生成
- **M3** 交通与调参：OSRM 真实路网矩阵、软时间窗、6 persona 评测
- **M4** 多城扩展：5 城 162 POI、高德 L2 实况路况、token 压缩
- **M5** 日期感知：closed_days 建模 + 全链路硬约束
- **M6** 多日衔接：酒店锚点 + 跨天去重 + 餐块预算预扣
- **M7** 经验提案：世界知识提案 → 落地 → 求解；缺口探测器
- **M7.5** 对照落地：《TOPTW+LLM 混合方案》P0/P1 全实现，对标 Google 论文补跨天重平衡
- **M8** 数据工程：add_city 一键扩城 / onboard_city.sh 流水线；北京 onboarding、成都补全
- **M8.5** 性能工程：端到端 23.3s → 12.9s（-45%），质量零回归
- **M9** 主题保真：「亲子动物行程无动物点」三层根因修复（数据+守门+保真通知）
- **M10** 工程化收尾：天气/节假日感知、提案缓存健壮化、发布门禁、城市库校验器+SOP

## Roadmap

- **P0** 行程交互式微调（点选换/删/换时段，复用 reuse 增量重解）· 城市库批量扩充（广州/深圳/西安/厦门/青岛/长沙）· 预算感知（price 进决策 + 费用明细）
- **P1** 偏好记忆 · 评测集扩至 30+ 真实查询进发布门禁 · 文案 LLM 缓存 · 热门时段避让 · 观测看板
- **P2** 跨城交通衔接（高铁段作为日间转移）· 多人协作投票 · DeepSeek 流式输出 · 国际化（en_name）

## 免责

行程时间为估算，出发前请再次确认景点当日开放情况。高德/OSRM/DeepSeek/open-meteo 服务条款适用其各自平台。
