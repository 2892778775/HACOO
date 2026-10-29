---
name: dft-coverage-closure
description: DFT coverage closure loop — when ATPG test/fault coverage misses target, classify untestable faults by cause (AU/UD/ND and subclasses), pick a strategy per dominant class (test point insertion / pattern budget & type adjustment / constraint or black-box handling), rerun ATPG, and re-measure; at most 3 rounds, escalate to human with the fault-class breakdown if still below target. Use when api_dft_report_coverage shows coverage below target and the human asks to close coverage.
---

# DFT Coverage Closure (Skills 层)

目标: ATPG 覆盖率 (test_coverage / fault_coverage) 未达标时, 用
"读覆盖率 → 未测故障归因分类 → 按主类选策略 → 复跑 ATPG" 的闭环收敛,
而不是盲目加 pattern。SPEC 出处: SPEC-evolution-and-domain-upgrade §C.1 FE-5
(依赖 FE-4 的归一化覆盖率三字段)。

## 迭代循环 (每轮)

1. **读覆盖率**: `api_dft_report_coverage()` → 归一三字段
   `{test_coverage, fault_coverage, untestable}` (FE-4, Tessent/DFTMAX
   已归一)。对照 target (默认 98.0%, 项目可配) 判定是否达标。
2. **归因分类**: 未达标时, 先查 KB 再分类 —
   `hacoo_knowledge_query(agent="dft", keyword="coverage")` /
   `keyword="AU"` / `keyword="test point"`。untestable 故障按成因归类:

   | 类别 | 含义 | 典型占比信号 | 主策略 |
   |---|---|---|---|
   | AU (ATPG Untestable) | 结构不可控/不可观测 (时钟门控、组合环、黑盒边界) | untestable 高且集中 | **test point 插入** (control/observe point), 或黑盒 macro 加 wrapper/BYPASS |
   | UD (Undetected) | ATPG 引擎未找到 pattern | pattern 预算耗尽 | **提高 pattern 预算** + 换 fault model (stuck-at → transition) 分轮跑 |
   | ND (Not Detected/孕检残留) | 时序相关或约束缺失导致 | 约束/SDC 问题 | 修测试约束 (test mode SDC), 与 synthesis 对齐 DFT-aware 约束 |

   归因结论必须先记录决策:
   `hacoo_decision_record(agent="dft", stage="dft",
   action="coverage_fix_round<N>", rationale=<覆盖率读数+主类判定+策略>,
   params={"test_coverage": ..., "target": ..., "dominant_class": "AU|UD|ND",
   "strategy": ...})`。
3. **执行策略 + 复跑**: 按主类执行一轮 (test point 插入后需重新
   `api_dft_insert_scan`; 纯 pattern 调整直接 `api_dft_atpg(patterns=<新预算>)`),
   再 `api_dft_report_coverage()` 复测。
4. **判收敛**: `test_coverage >= target` → 记录 `coverage_converged`
   决策并退出; 每轮结束 `hacoo_decision_outcome` 闭环 (metrics 带本轮
   test_coverage/fault_coverage/untestable)。

## 终止条件 (任一命中即退出循环)

- **收敛**: test_coverage 达标。
- **预算耗尽**: 达到 max_rounds (默认 3) 仍未达标 → 升级人工,
  `hacoo_feedback(agent="dft", content=<覆盖率轨迹+归因分类+已试策略>)`
  并停止 — 覆盖率收尾往往需要结构级改动 (人工决策)。
- **平台期**: 连续 2 轮 test_coverage 不再改善 → 换归因维度重查
  (untestable 构成是否变化), 仍无效则升级人工。
- **指标恶化**: 某轮覆盖率更差 → 回滚本轮改动 (test point 撤销 /
  pattern 预算回退), 记 failed outcome, 换次优策略。

## 纪律 (与 _AGENT_TEMPLATE 一致)

- 每轮只改一个变量 (test point 或 pattern 预算或约束), 否则无法归因。
- 覆盖率读数必须来自 `api_dft_report_coverage` 返回, 禁止估计;
  Tessent/DFTMAX 报告差异已在 adapter 内封装, skill 层只读归一三字段。
- test point 插入是结构改动: 插入前必须 decision_record (含插入位置
  依据), 且插入后必须重跑 insert_scan + ATPG 全链路再复测。
- 收敛中沉淀新经验 (如 "本设计 AU 集中在时钟门控, 加 control point 后
  +2.1%") → `hacoo_knowledge_add` (带 design profile, 见 X-2 纪律);
  flow 结束后 `hacoo_insights_scan(agent="dft")`。
- 覆盖率达标后提醒移交 synthesis (DFT 后 netlist 需时序复查) 或 apr,
  保持阶段衔接。

## 验收锚点

bench `dft-closure-01` (mock): 未达标场景 (target 99.0 > mock 读数) 输出
归因分类 (dominant_class) + 策略建议 + 决策记录闭环, 且 KB 命中已有
coverage 经验 (dft-cov-01 种子)。
