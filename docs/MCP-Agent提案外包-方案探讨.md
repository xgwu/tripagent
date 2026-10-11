# MCP「提案外包」架构探讨（Agent-Proposal）

> ⚠️ **状态更新（2026-10-11 晚，Kenny 实测）**：本文探讨的「让宿主 agent 自动走提案
> 路径」那一步（Phase 2.5：`instructions` 硬化 + `plan_trip` description 硬化 + `prompts`
> 模板）**实测效果不佳 ⇒ 已全部回退**。`proposal` / `dry_run` 能力**保留为可选高级用法**，
> 但**默认走服务端提案**（agent 只理解需求 + 调 `plan_trip`）。权威现状见
> `mcp/README.md` 里程碑「Phase 2.5 / 回退」与 `## 🤝 Agent 提案外包工作流` 章首注记。

> 探讨稿 · 2026-10-11 · 起因：能否把「理解用户需求 + 生成景点提案」交给**调用方 agent** 来做，
> MCP 只负责「用算法验证 POI 真实存在」+「做合理的线路规划」，从而省掉不必要的 LLM 调用？

---

## 0. 结论先行

| 判断 | 结论 |
|---|---|
| **方向** | ✅ 成立。这与本项目 M7.5 已确立的「世界知识主导选点」哲学一致 —— 只是把那个 LLM 从**服务端**搬到**调用方** |
| **收益** | ⚠️ 比直觉小：省的是**重的那一次**（A 提案），不是「很多次」。实测服务端 token **−34%**、墙钟 **−56%**（17.1s → 7.5s） |
| **被忽略的真问题** | 🔴 外包提案后，服务端剩余成本里**文案重生成占 61%** —— 而它**不能**外包（必须对齐求解后的最终时间轴） |
| **落地方式** | **共存，不替换**：`plan_trip` 加可选 `proposal`，服务端提案保留为兜底 |
| **前置条件** | 🔴 先做**发现工具**（让 agent 能查真库）。没有它，agent 必然幻觉点名字 —— 这正是 M1 存在的理由 |
| **契约通道** | 只有**工具定义（schema + description）**可靠。本项目 M6.6 已验证：agent 只读工具定义，**不读**返回字段 |

---

## 1. 现状：一次规划的 LLM 成本结构（实测）

**方法**：给 `llm_client._post`（`chat()` 的唯一出口）打桩，记录每次调用的用途与 token；
真跑 `proposal_planner.plan()`，并 bypass 提案缓存以取得冷启动计数。
**用例**：杭州 3 天 ·「喜欢自然风光和人文历史，节奏从容」· 库内 57 个 POI。

| 阶段 | 代码位置 | 次数 | token | 说明 |
|---|---|---|---|---|
| **A 提案** | `proposal_planner.plan()` :1018 | 1 | ~2 800 | 唯一「重生成」型调用；缓存 7 天（`PROPOSE_PROMPT_VER=v10`） |
| **B 落地匹配** | `_llm_match()` :343 | **0** | 0 | 仅在确定性匹配失败时触发；本例名全部命中 |
| **B 修正提案** | `_revise_proposal()` :482 | 1 | ~3 400 | 闭环：gaps / 落地率<0.85 / compose 剔除≥2 / 跨天片区重叠 |
| **C 文案重生成** | `m2_planner` `REGEN_PROMPT` :941 | 5–6 | ~5 200 | **每天一次**（`ThreadPoolExecutor(max_workers=3)` 并行） |
| **合计** | | **7–8** | **~12 000** | 墙钟 16–17s |

两条反直觉的读数：

1. **A 只占 1/8 次调用（≈25% token）** —— 「省掉提案 = 省掉很多调用」不成立。
2. 🔴 **C（文案）次数最多、token 最大（≈45%）**，且**按天线性增长** —— 5 天行程仅文案就可能 5–10 次。

---

## 2. 可行性边界：谁掌握数据，谁就该做决定

```
              ┌──────────────────── agent 侧（可外包）────────────────────┐
   选点意图   │  世界知识：这城有什么值得去、招牌体验、区域常识            │
              │  会话上下文：用户此前说过什么、纠正过什么、真实偏好        │
              │  → 决定「去哪些点、什么主题、什么节奏」                   │
              └──────────────────────────────────────────────────────────┘
              ┌──────────────────── server 侧（不可外包）──────────────────┐
   约束求解   │  POI 库存在性：这个名字库里有吗、对应哪个 id              │
              │  硬数据：时长 / 开放时间 / 闭馆日 / 价格                  │
              │  交通矩阵 + 里程预算（62,793 对缓存）                     │
              │  TOPTW 求解 + 修复链（时间窗/里程/亲子/餐窗）             │
              │  → 决定「能不能排、怎么排最优」                           │
              └──────────────────────────────────────────────────────────┘
```

⇒ **分界线：「选点意图」可外包；「约束与求解」不可外包。**
这正是你描述的形态 —— MCP 只做「验证真实存在 + 线路规划」，两者恰好是 `_ground()` 与 `compose/TOPTW`。

> 📌 历史上 M7.5 已经做过同样的选择（见 `docs/README-milestones.md`）：
> 「世界知识主导选点：LLM 凭旅行常识决定去哪，系统只负责落地 + 约束 + 求解」。
> 本提案 = **把这个 LLM 的位置从服务端移到调用方**，其余不变。

---

## 3. 量化收益（成对对照实测）

同一进程、同一用例、同一日期，两次真实规划：

| | 服务端调用 | 服务端 token | 墙钟 |
|---|---|---|---|
| ① 现状（服务端自己提案） | 8 | 12 169 | 17.1 s |
| ② 提案由 agent 提供 | 6 | 8 050 | 7.5 s |
| **差** | **−2** | **−4 119（−34%）** | **−9.6 s（−56%）** |

**为什么 token 只降 34%、墙钟却降 56%？**
因为 A 是**串行头部的重生成调用**（completion ≈1 300 tok），删掉它直接缩短关键路径，
再叠加一天文案被复用（C 从 6 → 5）。

还能继续压：

- **agent 直接给 `poi_id`** → 消灭 B 落地匹配（本例已是 0；别名/新点场景才会 >0）。
- **提案自身守规则** → B 修正轮次趋零（最多 2 轮 × ~3 400 tok）。

---

## 4. 🔴 被忽略的真问题：文案重生成（C）

② 里服务端剩余 8 050 tok 中，**C 占 4 925（61%）**。

**为什么不能外包**：文案（`theme` / `reason` / `tips`）必须**逐字对齐求解后的最终时间轴** ——
主题里写了「宋城怀古」却被求解器剔了宋城，就是硬伤（`REGEN_PROMPT` 明文规定这条）。
而 agent 给的是**求解前**的意图，它**看不到** TOPTW 的取舍结果。

**能省的部分**：若某天点位集与顺序都没被求解器改动 → 复用 agent 给的文案，**跳过该天 regen**。
代码里已有 `reuse_days` 机制（现仅用于 r2 闭环，`proposal_planner.plan()` :1122-1130），
把它延伸到**首轮**即可 —— 实测 ② 的 C 从 6 降到 5，正是这条在起作用。

⇒ **外包提案顺带解锁了一个独立优化**：文案复用面扩大。

> 进阶（Phase 3 可评估）：**两段式** —— server 返回最终时间轴后，请 agent 写文案并回写。
> 可省掉 C，代价是多一次往返 + 需要新的回写接口。

---

## 5. 契约发现：agent 怎么知道要干活（核心疑问）

**唯一可靠的通用通道 = 工具定义（schema + description）。**

依据是本项目自己的教训（M6.6，2026-10-10）：

> agent / 宿主只读**工具定义**（`_meta.ui` / description / inputSchema），**不读**工具**返回字段**。
> 那轮在服务端做「会话级封锁」拦不住宿主，只拦住了听话的模型 ——
> **约束必须写进「决策者能读到的地方」。**

四个配套手段（按优先级）：

| # | 手段 | 作用 |
|---|---|---|
| 1 | **发现工具** `tripplanner_find_pois(city?, keyword?, category?, limit?)` | 🔴 **前置**。返回库内真实 POI（id/name/category/duration/best_time/closed_days）。没有它，agent 只能凭世界知识编名字 → 落地失败 → 触发修正轮 → **反而更贵** |
| 2 | **可选参数 `proposal` + 自解释 schema** | 参数**存在本身就是信号**；JSON Schema 的 `description` 直接写契约 |
| 3 | **校验回执** `dry_run` / `validate_proposal` | 就是你说的「用算法验证 POI 真实存在」：返回 `{ok, unmatched:[{name, candidates:[…]}], warnings:[…]}`，agent 自纠正后再正式提交 |
| 4 | **description 写明兜底** | 「不提供 `proposal` 时服务端会自己生成」—— 既兜底又引导 |

> 补充：WorkBuddy **Skill** 可作 WorkBuddy 内的**强化通道**（写工作流、给示例），
> 但**不能当主通道** —— 其它 MCP 客户端没有 skill 机制。

---

## 6. 方案（分阶段，共存不替换）

| 阶段 | 内容 | 风险 |
|---|---|---|
| **Phase 0** | ✅ **已落地（2026-10-11）** —— 发现工具 `tripplanner_find_pois`（MCP）+ `GET /api/pois`（云端）。**不改** `plan_trip` | 零风险，独立有价值（agent 能查库、能对话） |
| **Phase 1** | ✅ **代码完成（2026-10-11，见 §10）** —— `plan_trip` 加可选 `proposal` / `dry_run`；返回加 `planSource`。**不传则走旧路径** | 低（共存不替换，全部入口显式报错不静默） |
| **Phase 2** | Skill 文档 + 评测口径（`eval_regression` 增 agent-proposal 模式） | 低 |
| **Phase 3** | 文案优化：扩大 `reuse_days` 复用面；评估两段式回写 | 中 |

### 工具契约草案

```jsonc
// tripplanner_plan_trip（扩展后）
{
  "query":    "用户原话（必填）",
  "city":     "可选，显式城市",
  "days":     "可选，天数",
  "date":     "可选，起始日期",
  "hotel":    "可选，住宿锚点",

  // 🆕 可选：调用方 agent 给出的提案。**不传则服务端自己提案（现状行为）**
  "proposal": [
    { "day": 1, "theme": "主题", "reason": "思路",
      "stops":      [ { "id": "HZ001", "note": "为什么去" } ],   // id 优先；无 id 用 name
      "alternates": [ { "name": "西溪湿地" } ] }
  ],
  "mode": "auto"   // auto | server | agent，默认 auto
}
```

---

## 7. 风险清单

| # | 风险 | 说明 | 缓解 |
|---|---|---|---|
| 1 | **确定性 / 可复现性** | 现在 `temperature=0.2, seed=42` + 7 天缓存 ⇒ 同 query 同结果；agent 提案每次都不同 | 缓存改按「落地后的提案」；评测拆双模式 |
| 2 | **规则口径漂移** | 14 条规则现在在服务端 `PROPOSE_PROMPT`（`BASE_RULES` + 条件规则）；搬给 agent 会变两份 | 规则单一真相源：用工具暴露（`get_guidelines`）；服务端修复链**必须保留**（确定性 gate） |
| 3 | **token 账** | agent 自己也要花 token，**不是净省** | 收益重新定位为：**延迟 + 服务端成本 + 上下文质量** |
| 4 | **质量下限** | 弱 agent 的提案可能比 `deepseek-chat` + 规则差 | 自动回退闸门：落地率/剔除数超阈值 → 回落服务端提案 |
| 5 | **多城联游** | `plan.city` 是 `"+".join(cities)`，**不是城名** | 提案 schema 按天带 city；发现工具支持多城 |
| 6 | **UI 直连路径** | webapp 不走 agent | 服务端提案**必须保留** |

---

## 8. 建议

1. ✅ **做，但共存** —— 不删服务端提案（兜底 + 向后兼容 + UI 直连）。
2. ✅ **先做 Phase 0（发现工具）** —— 它是任何外包方案的前置，且本身独立有价值。
3. 🔴 **别把收益预期定在「省掉很多调用」** —— 实测是 −34% token / −56% 延迟，
   省的是**「重的那一次」**（A 提案）。
4. 💡 **文案重生成（C）是更大的优化空间**，且**可以独立于本次改造先做**（扩大 `reuse_days`）。
5. 契约一律写进**工具 schema + description**，不依赖 skill。

---

## 9. Phase 0 实现记录（2026-10-11，已落地）

**MCP 工具**：`tripplanner_find_pois(city?, keyword?, category?, limit?)`
**云端端点**：`GET /api/pois?city=&keyword=&category=&limit=`

| 项 | 决定 | 理由 |
|---|---|---|
| 数据在哪查 | **云端端点**，MCP 只做代理 | M5 起 MCP 是**瘦客户端**（分享包里没有 `data/`）；与 `/api/cities`、`/api/replace` 同一套理由 |
| 工具开面板？ | **否**（不带 `_meta.ui`） | 它是查询工具，每次调用弹一扇面板会重演 M6.3 的堆叠问题 |
| 不改 `plan_trip` | **是** | 零风险、可独立发布；Phase 1 才动 `plan_trip` |
| 返回带坐标？ | **否** | 坐标只有面板地图需要（走 `poi_coords`）；agent 不需要，省上下文 |
| 匹配口径 | 复用 `poi_db.norm_name` / `core_name` | 本项目铁律：两套归一化必然漂移 |
| `city` 留空 | 全库（31 城） | 允许「这词在哪个城有」的横向发现 |
| 参数全空 | 报错 + 指引（不空跑全库） | 无条件的全库罗列没有意义 |

**依据（契约通道）**：工具的 `description` 是唯一可靠的「告诉 agent 该干什么」的通道
（M6.6 已证：宿主/agent 只读**工具定义**，不读返回字段）。故 description 明写
「查**库内真实存在**的 POI … 别凭世界知识直接报点名字（库里没有的点排不进行程）」。

**守卫**：`scripts/test_find_pois.py`（25 项 —— 云端过滤 11 + 端点接线 2 + 工具契约 5 + MCP 行为 7）；
`mcp/selftest.py` 新增 6 项真跑（走云端）。
**版本**：`SERVER_VERSION` 0.5.1-m5 → **0.5.2-m5**；`WEBUI_VERSION` m8 → **m9**（两者都必须部署）。

> 下一步（Phase 1，待 Kenny 拍板）：给 `plan_trip` 加**可选** `proposal` 参数 + `dry_run` 校验回执。
> 不传 `proposal` ⇒ 完全走旧路径（共存不替换）。

---

## 10. Phase 1 实现记录（2026-10-11，代码完成，待部署）

**MCP 工具**：`tripplanner_plan_trip` 新增可选 `proposal`（逐天数组，schema 必填 `day`+`stops`）
与 `dry_run`（boolean）。**required 仍只有 `query`**；工具仍**不带 `_meta`**（面板入口唯一）。
契约写进 description：先 `find_pois` 对齐事实 → `dry_run=true` 校验 → 去掉 dry_run 正式提交；
不传 proposal = 服务端照旧提案。

**云端**（`webui/server.py`，`WEBUI_VERSION` m9→**m10**）：
- `POST /api/plan`：do_POST 解析 JSON body 合并进 query 后**复用 do_GET 同一段处理器**
  （零逻辑分叉）。为什么必须 POST：提案 JSON 可能远超 URL/请求头长度（nginx 头默认 8K）。
- `proposal` 宽容归一化：数组（契约面形状）⇒ 包成 `{"days":[…]}`（内核形状）。
- `dry_run=1` ⇒ 短路返回 `proposal_planner.validate_proposal(...)` 回执，**不规划**。
- 带 proposal 的 m7 请求**绕过 LLM 结果缓存**（提案每次都可能不同）；
  `proposal_mode=auto|server|agent`（服务端参数，MCP 面不暴露；agent 必带 proposal）。
- **显式拒绝**（不静默丢）：多城联游 + proposal / mode≠m7 + proposal / 内核导入失败。

**内核**（`src/proposal_planner.py`）：`plan()` 加 `agent_proposal` ⇒ 有天清单就跳过
A 阶段 LLM 与缓存，直接进 B 落地（未落地 gaps 仍走既有修正闭环）；
`plan_source` 全路径落账（`agent`/`server`/`server_fallback`）；
新增 `validate_proposal()`（dry_run 回执，与正式规划**同一套** `_ground_one`，零 LLM；
未命中附 `_name_candidates` 候选 + 结构性告警：跨天重复/天号越界/空天/单天超员/无名点位）。
`src/plan_view.py::normalize_plan` 透传 `planSource`。

**守卫**：🆕 `scripts/test_agent_proposal.py`（36 项：回执口径 10 + 注入点 5 + webui 接线 9 +
工具契约 7 + MCP 行为 5，远端打桩不打网络）。
**HTTP 探针**：`dist/_probe_agent_proposal_http.py` **10/10** —— POST dry_run 全命中/未命中+候选、
坏参五路 400、多城拒绝、health=m10、**真实规划端到端 `planSource=agent`（10.2s）**。

**版本**：`SERVER_VERSION` 0.5.2-m5 → **0.5.3-m5**（必须部署）；`WEBUI_VERSION` m9 → **m10**。
`/api/health.stage` 仍 M5；`PANEL_VERSION` 不动（面板零改动）。
**门禁**：`release_gate` 五步全绿；`--mcp` 的 MCP 自检在**部署前**会钉在
「cloud=0.5.2-m5/m9 vs local=0.5.3-m5/m10」——预期，部署后复验。

**踩坑**：契约面（MCP schema / 方案稿）把 proposal 定义为**数组**，内核吃 `{"days":[…]}` ——
第一版服务端只认对象 ⇒ POST 直接 400。修法：服务端宽容归一化（数组包一层），
**契约面形状以 MCP schema 为准**。

> 下一步（Phase 2）：Skill 文档 + 评测口径（`eval_regression` 增 agent-proposal 模式）。

---

## 11. Phase 2 实现记录（2026-10-11，已落地）

**评测口径**（`scripts/eval_regression.py --agent-proposal`）：
- 🆕 `AGENT_CASES`（4 个代表性城市：上海亲子 3 日 / 杭州骑行 / 苏州园林 / 黄果树——控制 B 阶段 LLM 成本）。
- 🆕 `_draft_proposal()`：**确定性拟案器**模拟「理想 agent」——只用库内 POI（rating 降序 + id 升序，
  亲子查询过滤 `family_ok=false`，必含招牌点置顶），不用 LLM ⇒ 可复现。
- 断言七条：dry_run 全命中 / `planSource=agent` / 不落 M2 兜底 / 天数正确 / **0 违规** /
  落地率 ≥80% / **提案保留率 ≥40%**（观察性「整批丢弃」守卫——修正闭环会合法重排/替换
  提案：杭州 Day2 整日重组、黄果树夜游点→大瀑布，单例保留率天然波动 50~100%，
  首版 80%/60% 均假红；「提案被消费」另有 planSource=agent 硬断言兜底）。
  装填策略两轮迭代：固定切片（撑爆当日容量，保留率 50~62%）→ 时长预算贪心（6.5h/天、≤4 点）
  → 加 **area 多样性**（同片区全局 ≤2 点；主题盲拟案曾把西湖沿线堆 6 点 ⇒ 整日重排假红）。
- 门禁同 `--llm`（B 落地/修正走真实 LLM，缺 key 显式退出码 2）。

**Skill 文档**（双落点）：
- `mcp/README.md` 🆕「🤝 Agent 提案外包工作流」一节——面向任意 MCP 客户端：四步工作流、
  proposal 契约、成本收益表、硬约束与反模式（含「stops 必须带 name」这种 dry_run 口径细节）。
- 用户级 Skill `~/.workbuddy/skills/trip-agent-proposal/SKILL.md`——WorkBuddy 强化通道
  （§5 结论：skill 只能当强化通道，主通道仍是工具 schema/description，已由 Phase 1 完成）。
- 探针 `scripts/_probe_agent_proposal_eval.py`——拟案器 + dry_run 离线验证（零成本，4/4 全命中）。

---

## 附：实测脚本（证据）

- `dist/_probe_llm_calls.py` —— 单次规划的 LLM 调用分类计数 + token。
- `dist/_probe_agent_proposal.py` —— 成对对照：现状 vs agent 提供提案。

两者均通过给 `llm_client._post` 打桩实现，不修改任何生产代码；
`dist/` 已 gitignore（内含明文凭据，不入库）。
