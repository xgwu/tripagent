# TripAgent 🧭

LLM 世界知识 × OR-Tools TOPTW 混合行程规划系统 —— **世界知识主导选点，确定性系统负责落地 + 约束 + 求解**。

通用大模型的旅行「品味」很好，但直接生成的行程不可行：地点幻觉、营业时间冲突、跨天重复、里程不现实。TripAgent 让 LLM 凭世界知识决定「去哪、为什么去」，本地结构化 POI 库 + 运筹优化保证行程**物理可行**（真实路网时间、营业窗口、闭馆日、餐窗、里程预算）且**结构最优**（TOPTW 排序与可行性）。防幻觉不靠限制提案，而靠落地匹配——未落地的提案自动成为「POI 库缺口采购清单」，反向驱动数据扩容。LLM 不可用时全链路确定性降级，服务永不中断。

> 🌐 **线上运行中**：https://tripagent-planner2-80644.app.workbuddy.host/
>
> 📖 深度文档：[里程碑详情](docs/README-milestones.md) · [项目报告 v3](docs/TripAgent-项目报告.html) · [架构设计](docs/TripAgent-架构设计.html) · [TOPTW 算法报告](docs/TripAgent-TOPTW算法报告.html)

| 9 城市 | 573 POI | 39,295 对交通缓存 | 评测硬违规 | 回归用例 | 端到端耗时 |
|---|---|---|---|---|---|
| 沪/京/宁/粤/蓉/杭/汉/苏 | 结构化库（三源核实） | OSRM L1 + 高德 L2 实况 | **0**（五城×双方案 20/20） | **15/15**（9 城全覆盖） | **12.9s**（优化前 23.3s，-45%） |

## 核心链路（M7 经验提案 + M11 忠实执行）

```
需求文本 → 0·多轮澄清（LLM 保守判断需求缺失，至多追问一问，选项卡片交互）
        → A·世界知识提案（LLM 自由生成多日行程：主选 + note + alternates + 招牌/片区/餐窗规则，prompt v9）
        → B·落地匹配（四级回库：精确 → 分支变体 → 包含/模糊 difflib≥0.62 → LLM 辅助；失败记入库缺口台账）
        → C·TOPTW 求解（OR-Tools 逐日：营业时间窗 + 酒店锚点 + 闭馆硬过滤；忠实模式池=主选，只排序）
        → D·闭环与文案（跨天重平衡 → alt 净零换位 → 缺口/剔除回传 LLM 修正 ≤2 轮 → 文案对齐最终时间轴）
```

**四层防护**保证产出质量：

- **提案层**：招牌体验规则（亲子→迪士尼）、全天大点独占日、远郊大点片区规则、雨天室内引导、傍晚密度规则、**慢节奏规则**、**贯穿偏好规则**、**正餐规则**（午晚各一家、默认 1 家）
- **守门层**：远郊大点片区守门——距市中心 >12km 的 4h+ 大点与 10km 外片区错配混排时，求解前自动重排到最近的天；family 严格档下 5h+ 远郊点独占一日；**环湖/海岛主题日豁免不拆**；跨天片区分散（相邻两天 <1.2km 点位对触发闭环）
- **求解层**：**忠实执行模式**（默认开）——池=主选、主选以 10⁶ 利润锁定、落地 ≤ 提案、剔除原因单一；可回退为「主选高利润 + 双源备选换点」
- **闭环层**：落地缺口 / 落地率<85% / 主选被剔 ≥2 处 → 带原因回传修正再求解（≤2 轮防震荡）；备选补位**净零换位**（同主题优先）；需求标签（动物/博物馆等 16 类白名单）点位被剔光且无同主题保留 → 前端 ⚠️ 明确通知，而非静默丢失

降级路径：LLM 不可用 → 离线规则引擎；ortools 缺失 → 贪婪排序；高德不可用 → SVG 手绘图 + 直线折线；天气/节假日接口失败 → 静默跳过。

## 功能特性

- **9 城 573 POI**（广州 79 / 成都 75 / 苏州 75 / 北京 74 / 上海 69 / 杭州 57 / 武汉 50 / 南京 49），39,295 对真实路网交通缓存（L1 OSRM + L2 高德实况，L3 直线兜底），597 条实拍图缓存（9 城 **100% 覆盖**）
- **硬约束体系**：营业时间、闭馆日（日期感知，全库回填）、**跨零点闭店归一化**、餐窗美食（每窗 ≤1，咖啡馆非正餐分流）、返程酒店锚点、主题每日里程预算（骑行/徒步）
- **节奏档**：慢节奏（老人/轮椅/行动不便/不要太累）全链路宽松化——2–3 点/天、白天为主、真夜间点全清、站数下限 3；傍晚空窗自然留白（不自动补点）
- **餐窗保障**：正餐 POI 优先占窗、到达距饭点 ≤30min 先吃再逛、游完就地补餐、窗尾宽限与收尾晚餐提前量、**餐窗提前容差**（餐厅不必干等到 12:00/18:00 整点）+ **日内空档逐日披露**（求解器不惩罚空档，至少不静默）
- **贯穿性偏好**：「最好临湖」「环湖骑行」类需求穿透全链路（判据 + prompt 规则 + 湖线点利润加成 + 剔点/补位/补天三处保主题）
- **远郊守门 + 主题保真**：几何合理性事前保证（而非修复链事后剔点误杀主选）；补位同主题优先 + 需求丢失显式通知
- **点名与远郊召回**：用户点名的库内点位**确定性注入**（不依赖 LLM 是否采纳）+ 必选对账（排不进则显式告知）+ **目的地型餐饮豁免**（农家乐/湖鲜馆不被当作绕路配套剔除）+ 夜间点求解预算放宽（19:00 开门的点位可入选）
- **多轮澄清**：需求缺失时保守追问一轮（选项卡片），已与需求抽取合并为单次 LLM 调用
- **跨城联游**：「苏杭 4 天」自动识别跨城，按天分配逐城规划合并 + 住宿城市天数倾斜 + 跨城美食窗去重
- **性能工程**：天复用增量重解 + TOPTW 线程池并行 + at-solution 提前停止 + 提案级缓存（7 天 TTL）——端到端 23.3s → 首次 12.9s / 缓存命中 8.3s，质量零回归
- **天气/节假日感知**：open-meteo 免 key，≥60% 降水提示室内 + 备雨具
- **高德地图**：真实底图 + 按日着色 marker + 骑行/步行实际路网折线 + POI 静态图缩略
- **反馈闭环**：日卡「🔁 换一个」—— 同主题邻近换点并重排当天时间轴（确定性补位优先）
- **分享与导出**：行程快照短链 `?share=<id>`、PNG 长图 / ICS 日历导出、行程单打印/存 PDF
- **五段式进度条**：澄清→提案→落地→求解→文案分阶段轮询，等待可感知
- **公开护栏**：滑动窗口限流（8 次/300s/IP）、统计埋点、`/api/cities` 不回传服务端 Key

## 快速开始

```bash
# 1. 依赖（唯一可选第三方依赖为 ortools）
pip install -r requirements.txt

# 2. 配置密钥：复制 secrets.example.json 为 secrets.json 并填入真实值（secrets.json 不进 git）
#    deepseek_api_key / amap_key（Web 服务）/ amap_js_key（前端 JSAPI）
#    业务配置（模型名、功能开关等）放 config.json，随 git 提交
#    开关：anchor_hard_guarantee（住宿锚点硬保障，默认 false）/ toptw_faithful_mode（忠实执行，默认 true）

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
python scripts/run_tests.py                      # 全部单测：14 个 Python 单测 + 1 个前端守卫，一次跑完（推荐）
python scripts/run_tests.py gap named            # 只跑文件名含关键词的
python scripts/eval_regression.py [--llm]        # 回归评测：15 固化用例 / 9 城，离线 CI / --llm 真实全链路
python scripts/probe_perf.py                     # 性能探针：LLM/TOPTW 各环节耗时插桩
python scripts/release_gate.py                   # 发布门禁：编译→单测→回归→9 城库校验（纯 Python，本机与 CI 同一条命令）
python scripts/validate_city.py                  # POI 库全量校验

# 单个专项单测（改对应模块时按需跑）
python scripts/test_city_registry.py             # 城市注册表守卫（9 城×4 表，开城/加城必跑）
python scripts/test_cross_midnight_close.py      # 跨零点闭店归一化（含死点复现对照）
python scripts/test_gap_manage.py                # 日内空档治理（含关掉新参数复现旧缺陷的对照）
python scripts/test_named_inject.py              # 点名召回与必选对账（含目的地型餐饮豁免对照）
python scripts/test_multi_hotel.py               # 多城联游住宿锚点字段契约（时间轴 hotel 行 / plan_multi 透出 / notice Day 重映射）
python scripts/test_faithful_mode.py             # 忠实执行模式
node scripts/test_webui_food_mark.js             # 前端用餐图标（18 项）

# 开城 / 扩库
scripts/onboard_city.sh new 广州 --prefix GZ      # 新城市 onboarding（六步流水线）
scripts/onboard_city.sh expand 上海               # 已有城市扩库回填（四步）
python scripts/fill_travel_cache_gaps.py 广州     # 既有城补新增点的交通对（纯增量，只写缺失键）
python scripts/backfill_poi_photos.py 广州        # 图片补缺（多变体 + 周边搜索 + magic-bytes 实测）
python scripts/gap_report.py                      # POI 库缺口台账汇总（线上真实使用缺口持久化）
```

## API

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/cities` | GET | 城市元信息（`pois{id:{id,name,lat,lng,category}}` / `n_pois` / `n_closed`）+ LLM/地图 Key 可用性 |
| `/api/clarify` | POST | 多轮澄清（保守追问一轮，选项卡片） |
| `/api/plan` | GET | 规划：`city/query/date/hotel/llm`；天数从 query 提取（1–5，默认 2）；查询含 ≥2 城自动跨城；`fresh=1` 跳缓存 |
| `/api/progress` | GET | 规划进度轮询（五段式分阶段状态） |
| `/api/replace` | POST | 反馈换点：同主题邻近换点并重排当天时间轴 |
| `/api/route` | GET | 高德路径规划代理：`mode=riding\|walking&o=lng,lat&d=lng,lat`（磁盘缓存） |
| `/api/photo` | GET | POI 实拍图（缓存 597 条，9 城 100%，缺失回退瓦片） |
| `/api/share` | POST / GET | 行程快照保存 / 读取（`GET /api/share/<id>`） |
| `/api/stats` | GET | 调用量/延迟/补位命中率统计 |

> ⚠️ 行程响应中 `result.itinerary.days[].timeline[]` 的元素字段为 `type`（`poi`/`hop`/`meal`/`hotel`）/ `id` / `name` / `start` / `end`，**不是** `kind` / `poi_id`；`city_meta` 不下发 open/close/rating。多城联游时 `hotel` 只在住宿城那几天出现（出发/返回行），路线绘制以「该天时间轴是否含 `hotel` 行」为判据。

## 架构

```
webui/        单页前端（零框架，729 行）+ 纯 stdlib HTTP 服务（1,131 行，QuickBindServer）
src/          17 文件 4,174 行
              proposal_planner(M7 提案+落地+守门+餐窗对账) / m2_planner(TOPTW 求解编排) / m1_planner(贪婪)
              sequencer(时间轴+硬约束+修复链+餐窗分配+慢节奏档) / toptw(OR-Tools 建模+忠实模式)
              weather(天气感知) / gap_log(POI 缺口台账) / hotel(住宿锚点) / query_days(天数解析) / offline_planner(离线兜底)
data/         *_pois.json ×9 城 / travel_cache.json(39,295 对) / route_cache.json / photo_cache.json / shares/
scripts/      40 个运维与测试脚本 5,231 行（扩城/扩库/校验/门禁/探针/回归/守卫/缓存补缺/统一测试 runner）
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

## 里程碑（M1–M16）

- **M1** 验证假设：LLM 库内组线，幻觉结构性归零
- **M2** 求解层：OR-Tools TOPTW（营业硬窗+利润函数）+ 每日文案重生成
- **M3** 交通与调参：OSRM 真实路网矩阵、软时间窗 + 罚分封顶、6 persona 评测
- **M4** 多城扩展：5 城 162 POI、高德 L2 实况路况、token 压缩
- **M5** 日期感知：closed_days 建模 + 全链路硬约束
- **M6** 多日衔接：酒店锚点 + 跨天去重 + 餐块预算预扣
- **M7** 经验提案：世界知识提案 → 落地 → 求解；缺口探测器
- **M7.5** 对照落地：《TOPTW+LLM 混合方案》P0/P1 全实现，对标 Google 论文补跨天重平衡
- **M8** 数据工程：add_city 一键扩城 / onboard_city.sh 流水线；北京 onboarding、成都补全
- **M8.5** 性能工程：端到端 23.3s → 12.9s（-45%），质量零回归
- **M9** 主题保真：「亲子动物行程无动物点」三层根因修复（数据+守门+保真通知）
- **M10** 工程化收尾：天气/节假日感知、提案缓存健壮化、发布门禁、城市库校验器+SOP、缺口台账
- **M11** 忠实执行模式：选点权收归提案层，TOPTW 只排序与保可行（池=主选、落地 ≤ 提案）
- **M12** 体验保真四条线：慢节奏档（含「不要太累」正则旁路修复）/ 餐窗优先权与对账 / 贯穿性偏好 / 跨城住宿锚点与天数倾斜
- **M13** 城库扩容与开城广州：苏州 30→75、南京 33→49、广州 0→79，8 城 528 点、交通 37,315 对、图片 100%
- **M14** 正确性审计与守卫：跨零点闭店归一化（救活两个「死点」）、KTV 新子类（4 家广州 KTV）、城市注册表守卫、缓存改动纯度校验；depot 换位与亲子 gate 下沉主链路
- **M15** 空档治理与点名召回：日内空档 357→60min（餐窗提前容差 + 等待段补餐 + 空档分类披露）、点名/远郊召回三层修复、19:00 后开门点 horizon 放宽、统一测试 runner 与纯 Python 发布门禁、多城联游住宿锚点可见性（`plan_multi` 字段透出 + 时间轴 hotel 行 + 分段 notice Day 重映射）
- **M16** 开城盐城（第 9 城，45 POI）：全库 9 城 573 点、交通缓存 39,295 对、实拍图 9 城 100%；开城流水线跑通「采集 → 精修 → OSRM 矩阵 → 照片 → 七处注册 → 九城校验 → LLM 冒烟」

## Roadmap

- **P0** 行程交互式微调（点选换/删/换时段，复用 reuse 增量重解）· 城市库批量扩充（**已开盐城（第 9 城，45 点）；下一城候选西安/深圳/厦门/青岛/长沙**，其余候选深圳/厦门/青岛/长沙）· 预算感知（price 进决策 + 费用明细）
- **P1** ~~召回层补强~~ ✅ 已完成（M15）· 偏好记忆 · 评测集扩至 30+ 真实查询进发布门禁 · 文案 LLM 缓存 · 热门时段避让 · 观测看板
- **P2** 跨城交通衔接（高铁段作为日间转移）· 多人协作投票 · DeepSeek 流式输出 · 国际化（en_name）

### 缺陷台账

**已修复**

- ✅ `toptw.py` depot 换位失效（M14 / `0457603`）——元组赋值 + `list.index(obj)` 双重陷阱致交换被整体撤销；换位提前到 `RoutingIndexManager` 构造**之前** + 基于 id 的下标（守卫 `test_depot_anchor.py`）
- ✅ `family_ok=false` 仅在离线链路生效（M14 / `0457603`）——判据下沉 `sequencer.is_family_query` + `poi_db.is_family_ok`，确定性 gate 挂六个入口（守卫 `test_family_gate.py`）
- ✅ 日内空档（M15 / `d25119e`）——≥45min 空档逐条核对后**全部出现在正餐点之前**（餐厅被钉在 12:00/18:00 整点）；`MEAL_EARLY_TOL_H=1.0` + 等待段补餐 + `scan_gaps` 分类披露，同扫描 357min → 60min（守卫 `test_gap_manage.py`）
- ✅ 点名／远郊点「能排但不被提案」（M15 / `d25119e`）——三层修复：确定性注入 → 必选与对账口径（`named_pois`）→ 目的地型餐饮豁免（守卫 `test_named_inject.py`）
- ✅ 19:00 后开门的点结构性不可排（M15 / `d25119e`）——`LATE_POINT_MARGIN_MIN=60` 按池内最晚需求放宽求解预算（上限真实日窗）
- ✅ 聚合层丢字段：多城联游时住宿锚点在前端完全不可见（M15 / `007a1e8`，报障 18）——`plan_multi` 自建结果字典漏透出单城 `plan()` 的 `hotel`/`notices`，叠加「时间轴不显示酒店名」与「恢复字段后 `drawRealRoutes` 又给非住宿城补酒店腿」；修＝透出 `hotel` + 时间轴插 `type=hotel` 出发/返回行 + 路线判据改「该天是否含 hotel 行」+ `_lift_seg_notices` 按偏移重写分段 Day 号（守卫 `test_multi_hotel.py`）

**遗留（主动不修）**

- 亲子约束只到「点级」——能拦掉不适宜点位，但没有时段级节奏保障（如「上午户外、下午室内」的带娃节律）
- TOPTW 不惩罚行程内部空档——动 solver 风险大；M15 已消除最大成因并改为**披露**（`_gap_notices` 逐日告知空档时长与成因），不再静默
- 多城联游只有**单城**住宿锚点——`_hotel_city_probe` 只解析酒店归属的那一座城（住宿城天数 +1），非住宿城那几天没有落脚锚点（如苏杭联游的苏州天）；要支持「每城一个锚点」需扩展为多城解析

## 免责

行程时间为估算，出发前请再次确认景点当日开放情况。高德/OSRM/DeepSeek/open-meteo 服务条款适用其各自平台。
