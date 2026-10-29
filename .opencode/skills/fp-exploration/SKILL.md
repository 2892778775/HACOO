---
name: fp-exploration
description: Floorplan multi-scenario exploration for a block — run several utilization/aspect/macro-placement candidates through fp_create + place_opt, compare congestion/QoR side by side, and recommend the best floorplan. Use when the human asks to explore or compare floorplan options (e.g. "这个 block 用多大 utilization 合适", "对比几个 floorplan 方案").
---

# Floorplan Exploration (Skills 层)

目标: 对同一 block 以多组 floorplan 方案试跑, 用数据而非直觉选型。
SPEC 出处: SPEC-evolution-and-domain-upgrade §C.3 BE-3; 并行 fan-out 形态见 X-3。

## 适用判据

进入本 skill 的信号: block 初次 floorplan / 上次 flow 在 place 或 route 阶段
congestion 失控 / macro 数量或形态变化。若已有签核过的 floorplan 且仅做 ECO,
不要重新探索 — 走 eco-scope-control。

## 探索步骤

1. **查知识库**: `hacoo_knowledge_query(agent="apr", keyword="utilization congestion")`
   — 复用同节点/同类型 block 的历史选型经验 (X-2: 带 design 画像 tag 的条目优先)。
2. **定方案集**: 2~4 组候选, 每组 = (utilization, aspect_ratio, macro 策略)。
   默认梯度: 基准 util ±0.10 (如 0.60/0.70/0.80); macro 多的 block 加一组
   调整 macro 摆放 (halo/通道加宽) 的方案。方案集与判据先记录:
   `hacoo_decision_record(agent="apr", stage="fp_exploration",
   action="plan_scenarios", rationale=..., params={"scenarios": [...]})`。
3. **逐方案试跑** (每方案同一实例内顺序执行, 工具状态连续):
   `api_fp_create(utilization, aspect_ratio, core_margin)` →
   `api_fp_place_macros()` → `api_place_opt()`。
   每步返回 data 自带阶段指标 (BE-2 增强): utilization / hotspot_count /
   overflow_max。
   平台支持 fan-out 并行时 (X-3), 同参数组分发多实例跑再汇总。
4. **补 QoR**: 需要时加 `api_cts_build` + `api_cts_report` (skew/latency_max/clock_power)
   与 `api_route_opt` (drc_count), 但探索期以 congestion 为主判据,
   不必每方案跑完全程 — 控制试跑成本。
5. **对比并推荐**: 汇总对比表 (utilization / hotspot_count / 可选 skew、
   drc_count), 推荐规则:
   - 首选 hotspot_count 最低者;
   - 并列时取 utilization 高者 (面积效率高);
   - hotspot 集中在 macro 通道的方案, 即使总数低也要标注风险。
   推荐结论必须记录: `hacoo_decision_record(action="recommend",
   rationale=<对比表+推荐理由>)`, 行动后 `hacoo_decision_outcome` 闭环。

## 对比表格式 (rationale / 汇报中保持此结构)

```
scenario  util   aspect  hotspots  skew(ns)  drc
S1        0.70   1.0     3         -         -
S2        0.80   1.0     6         -         -
S3        0.60   1.0     1         -         -
推荐: S3 (congestion 最低; 面积代价 ~+17% 可接受)
```

## 纪律 (与 _AGENT_TEMPLATE 一致)

- 不得编造后端没返回的数字 — 对比表每个数值必须来自 api_* 返回的 data。
- 探索中验证了新经验 (如 "本节点 util>0.75 必炸") → `hacoo_knowledge_add` 沉淀。
- 人类在对话中纠正选型判据 → `hacoo_feedback` 落库, 不只当场改口。
- flow run 结束后 `hacoo_insights_scan(agent="apr")`, 向人类简报 new insight。
- 被禁/越权 (AGENT_DISABLED/AGENT_FORBIDDEN) 时停下上报, 不绕过策略。

## 验收锚点

bench `apr-fp-exploration-01` (mock): 3 方案试跑, 断言各方案
utilization/hotspot_count 指标各异、推荐决策 rationale 含对比表、
指标全部进观测层。
