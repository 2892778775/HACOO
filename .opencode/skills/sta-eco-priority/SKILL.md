---
name: sta-eco-priority
description: STA violation triage for ECO — rank violations by slack × affected paths × fix cost, detect setup/hold fix conflicts (buffer insertion worsening hold-critical paths), and hand an ordered, conflict-annotated fix list to APR. Use when timing signoff leaves violations and the human asks for an ECO plan or fix priority.
---

# STA ECO Priority (Skills 层)

目标: signoff 后有 violation 时, 不是"见一个修一个", 而是产出
**带排序 + 冲突标记的修复清单**移交 APR, 让一轮 ECO 修掉最多路径、
引入最少新问题。SPEC 出处: SPEC-evolution-and-domain-upgrade §C.4 SO-2。

## 步骤

1. **取数**: `api_report_timing(path_type="max")` (setup) 与
   `api_report_timing(path_type="min")` (hold)。MCMM 场景用
   `api_mcmm_report_all` 按场景组取最差值 (SO-1)。violation 清单必须
   来自后端返回, 禁止估计。
2. **分组**: 按 path group / 共同 startpoint-endpoint 域把路径聚成
   violation group——同组一次修复可清掉多条路径。
3. **排序**: priority = |slack| × 组内路径数 × 修复成本系数:

   | 修复手段 | 成本系数 | 说明 |
   |---|---|---|
   | Vt swap (HVT→LVT) | 1 | 无布局扰动, 首选 |
   | resize (upsizing) | 2 | 局部扰动小 |
   | buffer 插入 | 3 | 加 delay, **hold 冲突高风险** |
   | 逻辑重构 / 重综合 | 5 | 最后手段, 通常升级人工 |

   先查 KB: `hacoo_knowledge_query(agent="sta", keyword="ECO")` 复用
   历史修复成本经验 (如 "本 block 某 group Vt swap 收益差")。
4. **冲突检测 (关键)**: 对每组 violation 检查同区域 hold slack——
   hold slack < margin (默认 0.05ns) 的路径若用 buffer 插入修 setup
   会恶化 hold, 必须:
   - 标记 `conflict: true` 并注明受影响的 hold 路径;
   - 策略降级为 Vt swap / resize (不增加 delay 的手段);
   - 若低成本手段无法收敛, 升级人工而不是硬插 buffer。
5. **记录 + 移交**: `hacoo_decision_record(agent="sta", stage="sta_eco",
   action="eco_priority", rationale=<排序结果+冲突标记>,
   params={"ranked": [{group, slack, paths, method, priority}],
           "conflicts": [<hold-critical 路径>], "margin": 0.05})`,
   然后把 ordered list 移交 apr 执行 (EC-1 eco_loop 的诊断阶段即本 skill)。

## 纪律

- 排序依据 (slack/路径数/成本) 全部写入决策 rationale——APR 或人类
  调整顺序时能看到原始依据, 便于复核 (P04 复核队列)。
- 一轮移交的组数建议 <= 5, 超出说明设计问题较大, 先报告人类再动手。
- ECO 执行后必须增量复检 (min 和 max 都重报), 冲突组是复检重点。
- 修复经验 (某手段在本 block 的实际收益) 沉淀 `hacoo_knowledge_add`,
  tags 带 design 画像 (X-2 落地后按 profile 检索)。

## 验收锚点

bench `sta-eco-priority-01` (mock): 混合 violation 场景 → 输出排序 +
conflict 标记 + 决策记录齐全。
