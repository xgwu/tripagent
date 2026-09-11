# TripAgent M1–M7 —— 「检索 → LLM 库内选择 → TOPTW 求解」验证项目

对应《TOPTW+LLM 混合方案可行性分析》：
- **M1**（已完成 ✅）：验证核心假设「LLM 库内组线显著优于标签过滤」——幻觉结构性归零、优化层修复价值被量化。
- **M2**（已完成 ✅）：OR-Tools TOPTW 求解层 + 文案重生成。
- **M3**（已完成 ✅）：OSRM 真实路网交通矩阵 + best_time 软时间窗 + 参数扫描 + 评测扩容（6 personas）。
- **M5**（已完成 ✅）：日期感知（closed_days 建模 + 全链路约束）+ 跨城评测（eval_ab 5 城化）。
- **M6**（已完成 ✅）：多日行程衔接 —— 酒店锚点（每日起终点）+ 跨天去重 + 求解器餐块预算。
- **M7**（已完成 ✅）：经验提案模式 —— 回归最初架构意图：「LLM 世界知识自由提案 → 落地匹配到 POI 库 → TOPTW 求解」，防幻觉不靠限制提案、而靠落地匹配；未落地清单 = POI 库缺口探测器。
- **M7.5**（已完成 ✅）：世界知识主导 + 混合方案对照落地（闭环反馈 P0 / 备选 alternates P1），见下节。

## M7.5 新增（世界知识主导 + 《TOPTW与LLM混合方案分析》对照落地）

设计取向明确为**世界知识主导选点**：LLM 凭旅行常识决定去哪，系统只负责「落地 + 约束 + 求解」，不做硬编码选点。

- **提案世界知识优先**（79f4be2）：PROPOSE_PROMPT 的库内清单从「待选项」降级为「查漏补缺参考」；不得虚构，但选点品味交给 LLM。
- **招牌体验规则**（58ba52c）：prompt 要求 LLM 用世界知识判断需求核心期待——城市招牌大点（上海迪士尼/北京环球影城/广州长隆）与亲子类需求高度匹配时须进主选、独占一天、勿放 alternates；仅明确排斥时才可不放。修复「上海3日亲子游不推迪士尼」（根因：DeepSeek 把 3 天设计为市区主题日，主选+备选均无迪士尼；落地/TOPTW 各层均正常，属提案层缺失）。
- **全天大点豁免**（e82ac9e）：duration_h≥8 的 POI（迪士尼等）豁免主题里程预算（亲子 20km/天），单程 19km 的远点不再被 `_cap_km_repair` 剔除。
- **闭环反馈 + 阈值重做（对照文档 P0）**（be4ef0f）：落地 gaps / 落地率<0.85 → REVISE prompt 打包未落地地点回传 LLM 修正再落地；compose 剔除≥2 → 带剔除原因回传重求解；共享 MAX_REVISE_ROUNDS=2，`grounding.revise_rounds` 透出。
- **LLM 主选 + 备选 alternates（对照文档 P1）**（f939e5c）：提案每天新增 0-2 个 alternates 槽位；落地进 `alt_map`（不计 gaps/落地率），compose 时并入当日 TOPTW 池但不进主选 rank（低利润权重）；匹配层对住宿类库点免疫（不排酒店/商铺）。
- **住宿锚点三级匹配**（eaf0dd6/9efc2ee）：「住迪士尼附近」不再高德裸搜命中市区店铺——L0 库内地标匹配（rating≥4）优先；锚点硬保障（注入+TOPTW forced 必选）做成开关 `anchor_hard_guarantee`（e4bd445，**默认关**——世界知识主导，评测脚本内显式开启）。
- **亲子里程预算调参**（c2d3472/db00b6d）：THEME_PROFILES family 12→20 km/天。
- **健壮化**：extract_days「N日」天数识别修复（44f9db1）；正则未命中时 LLM 结构化抽取兜底（2d5203a）；武汉 POI 34→50（b1695f8）；密钥剥离至 secrets.json（gitignore），config.json 回归 git（7b207aa）。
- **前端文案透出**（1287255）：每日 reason 文案（LLM 对齐最终时间轴重生成）渲染到网页 Day 卡、分享 HTML、PNG 长图三处（长图预计算高度纳入文案行数）。
- **回归评测**（`scripts/eval_regression.py`）：12 固化用例（含上海亲子必含迪士尼两例），支持离线确定性（CI）与 `--llm` 全链路两种模式；当前离线 12/12、LLM 12/12。
- **部署**：WebUI 常驻入口 `https://tripagent-planner2.app.workbuddy.host/`（Python 单端口 http 服务）。

## M7 新增（经验提案模式）

- **三段式**（`src/proposal_planner.py`，`main.py --proposal`，WebUI 模式 radio「M7（经验提案）」）：
  A 提案：LLM 凭世界知识自由生成行程（不限列表，故意放开）；
  B 落地：四级匹配回库 —— 精确 → 包含 → 模糊（difflib ≥0.62）→ LLM 辅助批量匹配；失败剔除并记入 `grounding.gaps`；
  C 求解：落地后 day_map 喂给 `m2_planner.compose`（M2 阶段2-4 抽取复用：TOPTW + 修复链 + 文案重生成）。
- **评测**（`eval_m7.py`）：M2 vs M7 五城对比；首轮结论：两模式硬违规均 0；M7 路网均程持平略优、日均 POI 略低（库覆盖上限所致）、耗时 +3~5s（多一次提案调用）；24 个库缺口（科技馆/动物园/寺庙类为主）= POI 扩容采购清单。
- WebUI 行程单页面内嵌直显（iframe srcdoc），打印/新标签打开；`/api/stats` 用量统计；IP 限流（8 次/60s）。


## M6 新增（酒店锚点 + 跨天去重）

- **酒店锚点**（`src/hotel.py`，`main.py --hotel`）：三级解析 —— ① 高德地点搜索（AMAP_KEY，限定城市，酒店是库外锚点）② 显式坐标 `--hotel "名称@lng,lat"` ③ 市中心兜底。产出虚拟 POI 节点（id=HOTEL，dur=0，全天开放），无缝进入 travel_hours / 求解器 / 排序器。
- **求解器**：酒店作为 depot（`solve_day(hotel=...)`）—— 每日强制从酒店出发并返回（返程腿计入 End ≤ horizon），主选换位逻辑在有酒店时停用；结果序列自动剥离 HOTEL。
- **排序器**：时间轴新增 `hotel` 类型行（出发腿并入首段通行、`返回酒店` 收尾），day_end 前未回酒店记硬约束违规；贪婪排序从酒店最近点出发。
- **跨天去重**：m1_planner 对 day_map 按天序去重（同 POI 只保留首次出现，`n_dup_across_days` 计数）；LLM prompt 明示「同一 POI 不得出现在多天」；M2 备选池沿用 used_all/used_backup 双重排除，求解器不可能跨天选重复点。
- **餐块预算修复**：求解器 horizon 预扣 `MEAL_BUFFER_MIN=120`（排序器实测会插午餐+晚餐各 1h）——根治「求解器内可行、插餐后整体后移溢出 day_end」的打包过载问题（M5 苏州 M2 残留违规的同根因）。

## M5 新增（日期感知 + 跨城评测）

- **closed_days 字段建模**：POI schema 新增 `closed_days: ["周一", ...]`（`scripts/backfill_closed_days.py` 从 note 的「周X闭馆」模式提取，162 POI 回填、12 个有闭馆日）。
- **全链路日期感知**（`main.py --date 2026-09-14` / `m1/m2_planner.plan(..., date0=...)`）：
  - **LLM 层**：prompt 注入各天星期 + 压缩卡标注「闭馆:周一」，软引导不排闭馆日；
  - **求解器层（M2）**：每日求解池**硬过滤**当日闭馆 POI，主选闭馆 → 求解器强制换点（记入 `toptw_dropped`，理由「当日闭馆」）；
  - **校验层（M1/M2 共用）**：`sequencer` 新增「当日闭馆」硬约束违规 → 走既有两级修复链（重排/剔除），结构上保证最终行程 0 闭馆违规。
- **跨城评测**（`eval_ab.py` 重写）：5 城 × 6 persona（菜系本地化）× 3 方案，`--city` 单城、`--eval-date` 压力测试；汇总表新增**闭馆违规**列（>0 即日期感知失效）；输出 `output/m5_multi_city_report.html`。
- **poi_db 新增**：`WEEKDAY_NAMES` + `trip_weekday(date_str, day_no)`；`travel_cache.json` 支持高德 L2 数据（M4，~2900/5474 对已为真实路况整数分钟）。

## M3 新增（交通分层 + 软时间窗 + 调参）

- **交通矩阵分层**：`scripts/build_travel_cache.py` 一次请求 OSRM `/table` 拿 50×50 真实驾车时长矩阵 → `data/travel_cache.json`（L1，运行时直读）；L2 地图 API（高德/腾讯）留 TODO；L3 直线×系数兑底（缓存 miss 时自动启用）。OSRM car 档偏乐观，`CITY_FACTOR=1.4` 修正。
- **best_time 软时间窗**：SLOT_WINDOWS 映射 morning/afternoon/evening/lunch → 维度软上下界（`SetCumulVarSoftUpper/LowerBound`），每偏差 1 分钟罚 SOFT_W=5 分。
- **参数扫描**（`scripts/tune_params.py`，LLM 阶段缓存后离线扫 MAIN_BONUS×SOFT_W）：
  - MAIN_BONUS 300~1000 **结果完全一致**（主选本来 28/28 全保留；M2 时期的大幅删点其实是窗口语义 bug）
  - SOFT_W=5 最优：均距 3.33→2.87km、标签覆盖 0.74→0.85、时段合规提升
- **重要修复：求解器窗口语义错位**——原实现「到达 ≤ 闭店」，与 sequencer 的「到达+游玩 ≤ 闭店」不一致（游船 16:00 到达、17:30 玩完超时）。修正为到达截止 = 闭店 − 游玩时长。**这个 bug 也是 M2 时期主选被大幅删掉的真凶**。
- **评测口径升级**：直线距离 km 对齐为 OSRM 路网分钟（`avg_adjacent_min`），与求解器目标一致；新增 `best_time_rate`、`mains_kept`。personas 3→6（新增雨天室内/美食漫步/户外自然）。

**M3 后六画像结论**：硬约束违规 18/18 全 0；路网均程 M2 全面不劣于 M1（雨天室内 21→16min 显著最优）；M2 求解器 2/2 日成功。

## M4 新增（多城市 + L2 地图 API + token 优化）

- **多城市 POI 库**：南京/上海/苏州/武汉各 28 个 POI（schema 与杭州一致；上海由老 TripAgent 种子增强，其余三城新建），合计 5 城 162 POI。`main.py "需求" --city 苏州 --m2 --days 2` 即可规划任意城市。
- **L2 高德驾车 API**：`scripts/build_travel_cache.py --l2`（需 `AMAP_KEY` 环境变量），对缓存对逐个刷新实时路况时长（×1.1 修正），支持 `--l2-sample` 限量试跑；Key 缺失/无效自动只做 L1，单对查询失败返回 None 走 L3 兜底。缓存格式升级为多城合并（`cities` + `minutes`，M3 单城格式自动迁移）。
- **候选卡片 token 优化**：两级卡片——召回序 Top（池的 55%，8~20 间自适应）保留完整卡片（标签/建议时段/小贴士），其余压缩为单行（ID/名称/分类/时长/营业/价格/时段）。实测（DeepSeek ≈0.6 tok/汉字）：
  - 杭州 45 候选：2426→1879 tok（省 23%）
  - 28 候选城市：≈1570→1340 tok（省 13~14%），四城真实 prompt ≈1300~1350 tok
- **SYSTEM_PROMPT 泛化**：城市名参数化（原硬编码「杭州本地规划师」）， cuisine 关键词（本帮菜/苏帮菜/汉味）并入美食召回。
- **验证**：四城 LLM 冒烟全部 `mode=llm`、幻觉 0、违规 0、标签覆盖 2/3~4/5；苏州 M2 求解 2/2 日成功+文案重生成；体验编排在线（苏州挑艺圃/耦园小众线、武汉命中粮道街/万松园宵夜）。

## M2 架构（在 M1 之上新增）

```
LLM 主选（有序） ──→ 每日候选池 = 主选(高利润) + 地理邻近备选(低利润)
                              ↓
        OR-Tools RoutingModel 单日 TOPTW：营业时间窗 + 通行时间硬约束
        利润 = 主选标记1000 + 评分×60 + LLM顺位×15；不可行点按利润权衡放弃
                              ↓
        最终时间轴（餐块由排序器插入） → 约束复核 → LLM 重生成每日文案
```

**M2/M7 运行需要 ortools**（WorkBuddy managed venv）：
```bash
/Users/wuxiaogang/.workbuddy/binaries/python/envs/default/bin/python eval_ab.py
```

## OR-Tools 踩坑记录（本构建 ortools · pywrapcp）

1. **`RoutingIndexManager` 参数顺序是反的**：传 `(1, n, 0)` 会得到 n 辆车、1 个节点。必须显式 `(n, 1, [0], [0])`。
2. **可选节点（disjunction）的自环弧 transit 必须为 0**，否则维度传播不可行（CP Solver fail）。`full_transit(i, j)` 先判 `i == j`。
3. **depot 是真实 POI 时不能用 `fix_start_cumul_to_zero=True`**——首点营业窗口未必包含 0 点（08:30），需 `False` 并显式设 depot 窗口。
4. 时间窗需钳位到 `[0, horizon]`（00:00 开放的开放式景点换算后为负）。
5. **（M3）节点 CumulVar = 到达时刻，不含本节点游玩时长**——若业务语义是「结束前须离开」，窗口上界要减去游玩时长，否则求解器认为可行、 sequencer 校验仍会违规。
6. **（M3）软时间窗 API 在维度上**：`dimension.SetCumulVarSoftUpperBound(index, bound, coeff)`，不在 IntVar 上。


## 架构（M1 范围）

```
用户需求 ──→ 多路召回（标签/关键词/热度/地理）──→ 候选卡片 (~45个)
                                                      ↓
                              LLM 结构化输出（只能引用候选 ID，幻觉结构性归零）
                                                      ↓
                              排序器：贪婪最近邻 + 建议时段 + 午晚餐块
                              修复链：LLM 原序 → 重排 → 剔除（模拟 Agent Loop 反馈）
                                                      ↓
                              评测指标（TripTailor 口径子集）
```

⚠️ M1 **不含 TOPTW**（排序层是贪婪最近邻，仅作占位）。与 Google 方案对应：候选卡片 ≈ retrieval grounding，剔除 ≈ substitutes 反向操作。

## 运行

```bash
# 真实 A/B（需要 Key）
export DEEPSEEK_API_KEY=sk-xxx          # 或 OPENAI_API_KEY + OPENAI_BASE_URL + M1_MODEL
python eval_ab.py                       # 5 城 × 6 persona × 3 方案 → output/m5_multi_city_report.html
python eval_ab.py --city 苏州           # 单城快跑
python eval_ab.py --eval-date 2026-09-14  # 周一起始，闭馆约束压力测试

# 单次规划
python main.py "带5岁孩子去杭州玩2天，不要太累" --days 2
python main.py "苏州2天园林深度游" --city 苏州 --date 2026-09-14 --m2   # 日期感知 + TOPTW
python main.py "杭州2天亲子游" --hotel 西湖国宾馆 --m2                  # 酒店锚点（AMAP_KEY 自动定位）
python main.py "上海3日亲子游" --city 上海 --days 3 --proposal          # M7 经验提案（世界知识主导）

# 回归评测（12 固化用例；--llm 走真实全链路，默认离线确定性）
python scripts/eval_regression.py

# 无 Key 时自动降级离线兜底（管线冒烟用，不代表 M1 真实体验）
python eval_ab.py --no-llm
```

配置：`config.json`（业务配置，进 git）+ `secrets.json`（deepseek_api_key / amap_key / amap_js_key，gitignore），由 `src/config.py` 合并加载。

Windows 注意：`PYTHONIOENCODING=utf-8` 已在脚本内处理 stdout；JSON 落盘均为 UTF-8。

## 评测指标

| 维度 | 指标 | M1 目标 |
|---|---|---|
| 可行性 | 修复后硬约束违规（营业时间/时间窗/**闭馆日**） | 0 |
| 可行性 | LLM 原始排序违规数（修复前） | 观测值，衡量优化层价值 |
| 幻觉 | invalid_poi_ids 数量 | 0（结构性保证，这就是 M1 要验证的） |
| 合理性 | 相邻 POI 平均直线距离（TripTailor 口径） | 显著优于基线；参考：真人 7.3km |
| 个性化 | 查询意图标签覆盖率 | 观测值 |
| 反馈 | dropped_pois（修复剔除清单） | 应反馈给 LLM 做替代推荐（M2） |

## 文件结构

```
data/杭州_pois.json   50 个 POI（名称/分类/坐标GCJ-02近似/时间窗/时长/标签/评分）—— 由老 TripAgent poi-data.js 增强而来
data/travel_cache.json OSRM 50×50 真实路网驾车时长矩阵（×1.4 城市修正）
scripts/build_travel_cache.py  L1 交通矩阵构建（OSRM /table）+ L2 高德增量刷新
scripts/backfill_closed_days.py M5-1 闭馆日回填（note 模式 → closed_days）
scripts/tune_params.py         M3-1 参数扫描（MAIN_BONUS × SOFT_W）
src/poi_db.py         加载 + haversine + 通行时间（L1 OSRM 缓存优先，L3 直线兜底）
src/retrieval.py      多路召回 → 候选卡片文本
src/llm_client.py     OpenAI 兼容 API（纯标准库 urllib，含 429 指数退避）
src/m1_planner.py     M1 主链路 + LLM 失败降级链
src/offline_planner.py 离线兜底（地理锚点聚类），仅管线测试用
src/sequencer.py      排序 + 硬约束校验 + 两级修复（重排/剔除）
src/toptw.py          M2/M3 单日 TOPTW（硬时间窗 + best_time 软时间窗 + 利润函数）
src/m2_planner.py     M2 主链路（检索→LLM 选择→求解→闭环→文案重生成）
src/proposal_planner.py M7 提案链路（世界知识提案→落地匹配→闭环反馈→compose 复用）
src/hotel.py          M6 酒店锚点解析（高德/L0 地标匹配/坐标/市中心兜底）
src/config.py         config.json + secrets.json 合并加载
src/baseline.py       旧方案基线：关键词→标签硬过滤 + 评分贪心
src/metrics.py        评测指标（违规/路网均程/标签覆盖/时段合规/主选保留）
eval_ab.py            三方 A/B harness（基线/M1/M2）→ HTML 报告
eval_m7.py            M2 vs M7 五城对比评测
scripts/eval_regression.py  12 用例固化回归（离线 CI + --llm 全链路）
webui/                常驻 WebUI（server.py + index.html，单端口）
```

## 坐标与时间窗数据说明

坐标为人工标定的 GCJ-02 近似值（精度 ~100m 级），仅供 M1 距离指标使用；M2 接入地图 API 后以实时路径规划替换。营业时间为常规值，实际接入需数据更新管道（见可行性分析 §3.2）。
