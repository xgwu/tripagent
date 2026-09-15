# TripAgent 里程碑档案（M1–M7 详录 · M8 起见下节与项目报告）

> 📌 本文是**里程碑历史档案**，M1–M7 部分保留原始记录。M8 及之后的完整演进（含 M8.5 性能工程、M9 主题保真、M10 工程化、M11 忠实执行、M12 体验保真、M13 开城广州、M14 正确性审计、M15 空档治理与点名召回）请以 **[项目报告 v3](TripAgent-项目报告.html)** 为准；架构级说明见 **[架构设计文档](TripAgent-架构设计.html)**（已更新至 M15），求解器原理见 **[TOPTW 算法报告](TripAgent-TOPTW算法报告.html)**。
>
> 当前基线：**8 城 528 POI · 37,315 对交通缓存 · 回归 14/14（8 城全覆盖）· 143 commits**。

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
- **多轮澄清（对照文档 P1 收官）**：规划前新增 `/api/clarify`——LLM 保守判断需求是否缺失实质影响设计的信息（天数未提/同行人未提/强偏好歧义），至多追问一个问题（2~4 个选项），前端追问卡片支持选项/自由文本/跳过；回答拼入需求后走正常规划。缓存 7 天，LLM 不可用或判断失败静默降级为直接规划。
- **住宿锚点三级匹配**（eaf0dd6/9efc2ee）：「住迪士尼附近」不再高德裸搜命中市区店铺——L0 库内地标匹配（rating≥4）优先；锚点硬保障（注入+TOPTW forced 必选）做成开关 `anchor_hard_guarantee`（e4bd445，**默认关**——世界知识主导，评测脚本内显式开启）。
- **亲子里程预算调参**（c2d3472/db00b6d）：THEME_PROFILES family 12→20 km/天。
- **健壮化**：extract_days「N日」天数识别修复（44f9db1）；正则未命中时 LLM 结构化抽取兜底（2d5203a）；武汉 POI 34→50（b1695f8）；密钥剥离至 secrets.json（gitignore），config.json 回归 git（7b207aa）。
- **跨天重平衡（对照 Google 论文 stage-2 局部搜索）**：各日 TOPTW 独立求解后，确定性「移动 POI 到更近日簇」局部搜索——接受条件 0 违规 + 0 修复剔除 + 总里程改善>0.5km，每次移动扣 2km 相似度罚分（尊重 LLM 初稿）；全天大点与 forced 锚点不动，重求解掉点则整体回滚（`m2_planner._crossday_rebalance`，`n_day_moves` 透出，单元测试 `scripts/test_crossday_rebalance.py`）。注：检索型备选经 `_build_day_pool` 本就并入 M7 池（与 alternates 双源），无需额外改动。
- **前端文案透出**（1287255）：每日 reason 文案（LLM 对齐最终时间轴重生成）渲染到网页 Day 卡、分享 HTML、PNG 长图三处（长图预计算高度纳入文案行数）。
- **回归评测**（`scripts/eval_regression.py`）：12 固化用例（含上海亲子必含迪士尼两例，**现已扩至 14 用例 / 8 城**），支持离线确定性（CI）与 `--llm` 全链路两种模式；当前离线 12/12、LLM 12/12。
- **部署**：WebUI 常驻入口 `https://tripagent-planner2-80644.app.workbuddy.host/`（Python 单端口 http 服务）。
- **M7.5 全量评测（落地验证，2026-09-12 重跑 `eval_m7.py`）**：5 城 × 2 persona × 2 方案，真实 LLM 全链路。结果——
  - **20/20 全部 0 硬违规**（M2/M7 各 10）；
  - **M7 落地率 10/10 全部 100%**（提案 7~12 点/次，四级匹配精确为主），**库缺口清零**——首轮评测的 24 个缺口（科技馆/动物园/寺庙类）经多轮扩库后全部消化，缺口探测器当前无输出；
  - 点数均值 M7 9.1 vs M2 8.7；路网均程 M7 20.7min vs M2 20.0min（基本持平）；耗时 M7 18.8s vs M2 10.4s（+8.4s = 提案 + 闭环一次 LLM 调用成本，换来世界知识选点与招牌体验）；
  - **onboarding 固化**（123e493）：`scripts/onboard_city.sh`（new=新城市六步流水线 / expand=扩库回填四步）+ `expand_travel_cache.py --auto`（自动检测缓存无记录新点，不再手动维护 ID 清单）。

## M8 新增（行程质量打磨八连 + 交通模型分化，2026-09-11/12）

线上反馈驱动的八轮修复与增强（全部真实 LLM 端到端验证 + 回归通过）：

- **每日 Tips**（f522a1f）：REGEN 一次调用顺带产出 1~3 条实用建议（早到避峰/串玩/返程），网页/分享/PNG 三处渲染；闭馆日事实注入提示词。
- **折叠式剔除原因**（cc247ea）：`toptw_dropped` 原因在 Day 卡底部 `<details>` 折叠展示，求解器决策透明化。
- **hop 通行段**（494556c）：时间轴插入相邻点间通行行（步行/骑行/车程 + 分钟 + km），插在餐块前保证时间序；每日理由升级为「选点+排序」依据。
- **骑行/徒步交通模型分化**（4aa58b0）：`travel_hours(mode)`——cycling <1.2km 步行 / 1.2~6km 骑行 12km/h / >6km 车程；hiking <3km 步行；步行/骑行单程下限降 5min；mode 穿透 sequencer/TOPTW/重平衡/metrics；修复链顺序重构为「硬约束修复先行 → 里程预算收敛在后」并升级为约束感知剔点（修复旧序预算成果被冲掉）。
- **跨天片区分散**（db6cf78）：「武康路连去两天」修复——PROPOSE 片区分散规则（同街区 1km 内点位只出现一天、咖啡每天 ≤1 家）+ 确定性守门 `_crossday_area_overlap`（相邻天 <1.2km 点位对记「片区重复」）接入闭环 REVISE；`area_overlap` 诊断透出。
- **tips 闭馆幻觉修复**（6c9a860）：官方核实上海城市规划展示馆周三闭馆（库内数据正确），tips 的「周一闭馆」是 LLM 常识幻觉——REGEN 注入闭馆日数据 + 确定性守门（与数据矛盾的闭馆 tips 剔除）。
- **日内填空**（1cb3054）：finish 距 day_end 空窗 >2h → 从未用召回池补晚间可行点（逐点试加全量校验，违规/剔除/里程预算任一不过即拒），修复博物馆类行程 15 点收尾；`day_fills` 透出。
- **美食配套绕行治理三层**（53e7dea + 3cabfc1）：「为一家网红咖啡店横穿城区」修复——P0 `_fix_food_detours`（food 点绕行 >25min → 顺路同类店替换或剔除，替代池含带咖啡 tag 场馆保意图）；P1 PROPOSE 配套顺路规则（配套位于相邻主选间/1.5km 内）；P2 正餐点最小绕行位重插（`_insert_foods` 复用）。
- **咖啡馆非正餐**（fe9596d）：午餐时段被排咖啡馆修复——`is_cafe()` 识别，咖啡不再抢餐窗/占修剪名额，正餐餐块永远照插；PROPOSE 正餐规则。端到端三天午餐全为正餐餐馆、咖啡仅傍晚休憩。
- **软窗罚分封顶 + 傍晚密度**（2d872a5）：SOFT_CAP_MIN=60min 等效封顶 300 分（旧线性无上限致 morning 点永远进不了晚间，时段空间白扔）；PROPOSE 傍晚密度规则（博物馆类主题每天搭配 1-2 个晚间型点）。
- **数据资产扩容**：北京 onboarding（80 POI，`add_city.py` 六步流水线，离线冒烟 0 违规；15 个场馆补周一闭馆）；成都库清脏 4 条（高德「地标」误采商户）+ 补招牌 8 点（天府广场/人民公园/锦里/武侯祠正馆/东郊记忆/望江楼公园/青羊宫/都江堰）；金沙遗址博物馆 2025-12~2027-04 闭馆改造期间不入库（官网公告核实）；`expand_travel_cache.py` 增补成都/北京。

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

# 回归评测（14 固化用例；--llm 走真实全链路，默认离线确定性）
python scripts/eval_regression.py

# 无 Key 时自动降级离线兜底（管线冒烟用，不代表 M1 真实体验）
python eval_ab.py --no-llm
```

配置：`config.json`（业务配置，进 git）+ `secrets.json`（deepseek_api_key / amap_key / amap_js_key，gitignore），由 `src/config.py` 合并加载。

Windows 注意：`PYTHONIOENCODING=utf-8` 已在脚本内处理 stdout；JSON 落盘均为 UTF-8。

## M11–M15 新增（2026-09-13/15 · 线上反馈驱动迭代）

### M11 忠实执行模式：选点权收归提案层（adb3fa7，默认开）
求解器按「时间预算内利润最大化」填满时间的目标，与用户的节奏意图（慢节奏/带老人/不要太累）**直接打架且优化器总会赢**。修复是把选点权整体交还提案层：`toptw.solve_day(lock_mains=True)` 时池 = 主选、主选叠 10⁶ 利润锁定、**落地 ≤ 提案**、剔除原因收敛为单一口径（「时间预算内无法纳入（主选锁定仍装不下）」）；补位只走阶段 3 `_alt_substitute`（**净零换位**，不增站点数），跳过 `_feedback_loop` 加点。env `TOPTW_FAITHFUL_MODE=0` / config `toptw_faithful_mode:false` 可回退。单测 `scripts/test_faithful_mode.py`（4 例）。

### M12 体验保真四条线（2248054 → 9c575f8 → 66f3d6b，报障 7–16）
- **节奏线**：慢节奏档全链路宽松化（老人/轮椅/行动不便/不累/**太累**）——2–3 点/天、白天为主、真夜间点全清、站数下限统一 3、提案截断 stops≤4；确定性兜底七处（主选移除 / alt 跳过夜间+cap4 / MIN_STOPS / TOPTW 禁池彻底化 / 提案截断 / 补强池三重过滤 / rebalance `min_keep`）。⚠️ **报障 9 教训**：`SLOW_PACE_RE` 漏「太累」致「不要太累」不含「不累」连续子串 → 慢节奏档**整链旁路**；**正则改动必须用真实 LLM 实测**（离线确定性路径测不出 LLM 门控）。**报障 11**：夜间点判据精准化为 `poi_db.is_night_only`（nightlife 类目 or `open_h≥16.5`），不按 best_time 一刀切（外滩等全天开放点误杀致薄天）。
- **餐窗线**：正餐 POI 餐窗优先权（`_assign_food_windows` 统一分配 + 通用餐块让位认领）+ 主选层提前降级（`food_window_plan` / `_reconcile_food_windows`，写 `grounding.food_demoted`）+ PROPOSE_PROMPT **v7** 正餐规则（≤2 家且午晚各一家、默认 1 家）；`MEAL_LEAD_H=0.5` 通用化 + 游完就地补餐 + `MEAL_EXIT_GRACE_H=0.75` 窗尾宽限 + `MEAL_END_LEAD_H=1.5` 收尾提前量（治 2~4h 游览横跨餐窗的午餐盲区）。线上美食剔除 4 → 0。
- **偏好线**：「临湖/环湖」贯穿性偏好穿透全链路——`poi_db.is_lake_poi` 判据 + prompt 贯穿偏好规则（跨天片区分散为偏好让位）+ `LAKE_BONUS=150` 点级利润加成（`_bonus` 必须 int）+ 剔点/补位/补天三处保主题。另修 `_far_big_point_regroup` 拆杀环湖主题日（被判错配点全部 >12km 远郊则豁免不拆）+ `CYCLE_MAX_KM` 6→8km。
- **跨城线**：跨城住宿锚点探测 `_hotel_city_probe`（bigram≥0.6，个人 Key 限流 0.35s 节流）+ 天数住宿倾斜（住宿城市 +1，每城 ≥1）+ 跨城美食窗去重 `_dedupe_food_windows`。
- **附修**：住宿锚点防线（query 无「住/酒店/民宿/宾馆/客栈」则不采纳 LLM 抽的 hotel，防「都在太湖边」被抽成住宿区域）；移除 `_fill_evenings` 自动补晚间点（用户指令，傍晚空窗自然留白）；前端用餐 🍽 图标（报障 16：`city_meta` 未下发 category 致判据恒为假，改用 timeline `meal` 字段）。

### M13 城库扩容与开城广州（944e125 → 7222dc3）
苏州 30→75（骑行/咖啡/美食/环太湖四批，含环太湖沿线缺口 SZ067–075）、南京 33→49（删重复 NJ018/NJ035）、**广州开城 0→79**（culture14/food15/nature11/history8/religion6/shopping6/view5/family5/nightlife8/show1，17 点带 closed_days）。全库 **8 城 528 点**、交通缓存 **37,315 对**（0 None）、照片缓存 8 城 **100% 覆盖**。开城流水线固化：采集 → 精修落盘 → 交通矩阵 → 照片 → 城市注册 → `validate_city.py` → 回归 → LLM 冒烟 → 提交 → 部署 → 线上 fresh 验收。

### M14 正确性审计与守卫（9b176e4 → 7222dc3）
- **跨零点闭店归一化**：`parse_poi` 中 `close_h ≤ open_h` → `+24`。修复 GZ069 宝业路宵夜街 / NJ021 1912 街区两个**自入库起从未可排**的「死点」（根因：02:00 解成 2.0 < day_start 9.0，被预剔除/时间窗/排序器三重判死）。全库仅 2 点触发，526 点零影响；单测 `scripts/test_cross_midnight_close.py`（8 组，含还原 close_h 的「从模型完全消失」对照）。
- **KTV 新子类**：广州入库 4 家量贩/派对 KTV（GZ076 纯K岗顶 / GZ077 堂会缤缤 / GZ078 魅KTV花城汇 / GZ079 CxPARTY 太古仓），`nightlife` + `family_ok=false`——全库首个 KTV 子类。
- **城市注册表守卫**：广州开城只改了 `fetch_poi_photos.py` 的 `CITYCODE`，漏改 `webui/server.py` 的 `_ID_PREFIX_CITY`/`_CITYCODE` → `/api/photo` 对 GZ 全 400（报障 17「全城不出图」）。新增 `scripts/test_city_registry.py`（8 城 × 4 张映射表 + 前缀识别硬断言），开城/加城必跑。
- **缓存改动纯度校验手法**：改大缓存 JSON 前后用 `git show HEAD:<file>` 拉旧版逐键比对，断言「既有键改动 0 / 删除 0，仅新增 N」。
- **顺带修复**：`is_cafe` 判据过宽（裸「茶」子串误杀广州 4 家粤菜老字号「早茶」→ 一天两顿午饭）；`/api/cities` 不再回传 Web 服务 Key（安全加固 ea5d12e）。

### M15 空档治理与点名召回（d25119e · 一次迭代修三个缺陷）

- **空档治理**：全量扫描 13 用例 × 全部天数，≥45min 空档共 **357min**，且逐条核对后**全部出现在正餐点之前**——旧 `start = max(t2, ws, p["open_h"])` 把餐厅钉在 12:00/18:00 整点，10:45 到场也要干等 75min（南京科举博物馆→绿柳居、北京故宫→四季民福、苏州琵琶语→朱鸿兴、武汉长江大桥→户部巷 同族）。修＝`MEAL_EARLY_TOL_H=1.0` 提前容差 + `_fill_wait_with_meal`（等待开门段先吃饭）+ `scan_gaps`/`_gap_notices`（空档分类 `wait_open`/`wait_meal`/`free` 并逐日披露）。同扫描 **357 → 60min**。单测 `scripts/test_gap_manage.py`（14 例，含关掉 `MEAL_EARLY_TOL_H` 复现旧空档的对照）。
- **点名／远郊召回三层（缺一层就白干）**：① **召回**——`poi_db.names_mentioned_in`（长度 ≥3 子串）+ `_inject_named_pois` **确定性注入**（几何最近天，不依赖 LLM 是否采纳），菜单郊区点标「（郊区）」+ PROPOSE_PROMPT **v9** 远郊规则；② **必选与对账**——`forced_ids_for` 的口径是「需求点名的**全量**库内点」（`named_pois`）而非「本次注入了什么」（`named_injected`）——LLM 恰好自己提案了点名点时后者为空，会让该点既不被列必选、被剔后也不披露；丢失必发 `named_lost`；③ **下游守门**——`_fix_food_detours`（设计前提是「咖啡是配套不是目标」）把 `category=food` 的**目的地型餐饮**当绕路配套换掉（莲花岛＝阳澄湖农家乐集群、`suburb`、dur 2h，对照实测理由原文「莲花岛（绕行 110 分钟且无顺路替代，剔除）」）→ `_is_destination_food`（郊区／停留 ≥2h／农家乐类名）+ `protect`（点名点豁免）。单测 `scripts/test_named_inject.py`（26 例，含对照）。实测宝业路落地 18:00-19:30、莲花岛落地 11:26-13:26。
- **夜间点 horizon 放宽**：`MEAL_BUFFER_MIN=120` 把求解预算压到 630min（09:00→19:30），18:00 后开门的点（GZ066 珠江夜游 19:00 / GZ069 宝业路 18:00）**结构性不可行**，且**不出现在 `dropped` 里**（排查时极易误判为「提案层没提」）。新增 `LATE_POINT_MARGIN_MIN=60`，按池内最晚需求放宽、上限锁真实日窗；池中无晚间点时不触发。单测用「把常量置负无穷」做对照。
- **工程化收尾**：新增统一测试 runner `scripts/run_tests.py`（13 个 Python 单测 + 1 个前端守卫一次跑完，含 node 自动定位与关键词过滤）；发布门禁改**纯 Python 实现** `scripts/release_gate.py`——原 `release_gate.sh` 依赖 `dirname`/`grep`/`mktemp`，在本机 Git Bash shim 故障下根本执行不了，门禁形同虚设；`.sh` 保留为薄兼容入口，`.github/workflows/gate.yml` 改调 `.py`。
- **教训**：① 「LLM 听话」和「用户要求被满足」是两件事——必须**独立对账**，不能拿流水线内部记录当口径；② 判断某类目点是「配套」之前，先问**它会不会本身就是用户目标**（`category` 是粗标签，`food` 里既有咖啡馆也有农家乐目的地）；③ 探针要带**调用行号**（`traceback.extract_stack`），否则「哪一层改的」只能靠猜——本次一步定位到 `m2_planner.py:721 → 408`；④ 属性方差大的指标（LLM 逐次提案不同）不能作为唯一验证依据，最终要落到确定性单测。

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
scripts/eval_regression.py  14 用例固化回归（离线 CI + --llm 全链路）
webui/                常驻 WebUI（server.py + index.html，单端口）
```

## 坐标与时间窗数据说明

坐标为人工标定的 GCJ-02 近似值（精度 ~100m 级），仅供 M1 距离指标使用；M2 接入地图 API 后以实时路径规划替换。营业时间为常规值，实际接入需数据更新管道（见可行性分析 §3.2）。
