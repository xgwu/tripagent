# 城市库扩充流水线

新增城市或批量补点的标准流程。目标：库内数据可信、口径统一、变更可回归。

## 1. 立项

- 确认目标城市在 `webui/server.py` 的 `CITIES` 列表里（新城市需同步添加）。
- 点位规模：首版 30~60 个，覆盖经典必去 + 亲子 + 博物馆 + 美食/夜市 + 远郊大点。

## 2. 数据采集（三源核实）

每个 POI 的关键字段必须经 **三个独立来源** 交叉核实，不一致时以官方为准：

| 字段 | 优先来源 | 交叉来源 |
|---|---|---|
| 门票价格 | 景区官网/政府公示 | 携程、同程 |
| 开放时间 | 官网/公众号 | OTA 列表页 |
| 坐标 | 地图App拾取 | POI 地址反查 |
| 建议游玩时长 | OTA 攻略均值 | 常识口径（半天 2-4h / 全天 5h+） |
| 评分 | 归一化为 {3,4,5} | 携程+同程+大众点评均值取整 |

核实结论写入 `note` 字段留痕（来源+日期），例如野生动物世界 HZ057 的 note。

## 3. 生成库文件

- 参考 `scripts/expand_pois_cd.py` 等既有脚本写一城一脚本（便于 review 与重放）。
- 字段 schema 见 `scripts/validate_city.py` 的 `REQUIRED`；`area` 用片区词
  （west/south/north/central/suburb…），远郊点必须标 `suburb`。
- 动物/博物馆等需求标签必须打全（`KEYWORD_TAGS` 靠它召回）。

## 4. 校验门禁

```bash
python scripts/validate_city.py <城市>   # 必须通过（警告可人工确认）
python scripts/eval_regression.py       # 全量回归必须 13/13
python scripts/test_far_big_regroup.py  # 守门单测
```

## 5. 验收与发布

1. 真实 LLM 端到端跑 2~3 条该城典型查询（含亲子/经典各一），确认 0 违规、0 幻觉。
2. 本地提交 → 发布线上 → 推送 GitHub（顺序：先线上后 GitHub）。
