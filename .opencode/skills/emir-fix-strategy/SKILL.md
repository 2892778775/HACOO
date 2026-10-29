---
name: emir-fix-strategy
description: EMIR hotspot fix strategy — attribute each hotspot cluster (insufficient decap / sparse PG grid / local high toggling), pick the matching fix, record the handoff rationale when dispatching layout changes to the apr agent, and re-verify with a fresh hotspot report. Use when IR drop or EM hotspots exceed budget and the human asks for a fix plan.
---

# EMIR Fix Strategy (Skills 层)

目标: IR drop / EM hotspot 超标时, 先归因再开方, 修复动作大多落在
版图侧 (apr 执行), irem 负责归因、策略与复验 —— 跨 agent 交接的理由
必须进决策链。SPEC 出处: SPEC-evolution-and-domain-upgrade §C.4 SO-8
(依赖 SO-7 的 api_emir_report_hotspots)。

## 流程

1. **取 hotspot**: `api_emir_report_hotspots(top_n)` →
   `{hotspots: [{instance,x,y,layer,ir_drop_mv}], worst_drop_mv}`。
2. **聚类 + 归因** (先查 KB:
   `hacoo_knowledge_query(agent="irem", keyword="hotspot")`):

   | hotspot 特征 | 归因 | 修复策略 | 执行方 |
   |---|---|---|---|
   | 分布广而均匀, worst_drop 略超预算 | decap 不足 | 加 decap 填充 | apr (decap 插入) |
   | 远离 PG 网格/边缘区域成片 | PG 网格稀疏 | 局部 PG 加密 / 加 strap | apr (PG 修复) |
   | 集中在高翻转逻辑 (ALU/乘加/总线) | 局部高翻转 | 时钟门控 / 逻辑重构 / 局部降频路径 | apr + synthesis 协同 |
   | 压降随工作模式波动大 | dynamic IR | 调压/调度与 decap 组合 | irem 主导再分析 |

3. **选策略 + 记录**: 每个归因一条
   `hacoo_decision_record(agent="irem", stage="emir_fix",
   action="attribution", params={"cluster": ..., "cause": ..., "worst_mv": ...})`
   与 `action="strategy", params={"strategy": ...}`。
4. **移交 apr**: 版图改动一条移交决策 —
   `action="handoff", rationale=<为什么该 apr 做/做什么/验收标准>,
   params={"handoff_to": "apr", "strategy": ..., "target_mv": ...}`。
   **移交不是结束**: irem 侧保留复验责任。
5. **复验闭环**: apr 执行后重跑 `api_emir_report_hotspots`,
   worst_drop_mv 对比入 `hacoo_decision_outcome(metrics={...})`;
   未达标回到第 2 步 (换策略维度), 连续 2 轮不改善升级人工
   (`hacoo_feedback`)。

## 纪律 (与 _AGENT_TEMPLATE 一致)

- 归因必须引用 hotspot 数据 (坐标/层/压降) 或 KB 条目, 禁止拍脑袋。
- 一次只移交一类修复给 apr, 多策略混做无法归因哪一步起效。
- 3D IC 场景注意层间热-压降耦合: hotspot 在上层 die 时, 联动
  thermal agent (TTV/热密度) 再定策略。
- 新归因模式沉淀 `hacoo_knowledge_add`; flow 结束后
  `hacoo_insights_scan(agent="irem")`。

## 验收锚点

bench `emir-fix-01` (mock): hotspot 报告 (u_alu 簇 M5 高压降) ->
归因 (局部高翻转) + 策略 (decap+PG) + 移交 apr 决策 (含交接理由)
齐全, worst_drop_mv 进观测层。
