# Kimi Code 实施提示词（HACOO 三份 SPEC）

> 用法：在项目根目录 `D:\Career\3DIC_Skill_Audit\Code\FluxEDA\FluxEDA`
> 打开 Kimi Code，先粘贴「§1 主控提示词」，然后按阶段依次粘贴
> §2~§5 的阶段提示词。每个阶段完成后确认测试全绿再进入下一阶段。
> 不要试图一个提示词做完所有 SPEC——阶段之间有依赖，且单阶段体量
> 已接近上下文舒适区的上限。

---

## §1 主控提示词（每次会话开头粘贴，建立共同语境）

```
你是 HACOO 平台的维护工程师。HACOO 是一个数字芯片设计多 Agent 协作平台
（Python，五层架构：Access/Comm/Gateway/Adapter/Runtime），位于当前项目目录。

【实施前必读，先读完再动手】
1. docs/FluxEDA.md            —— 平台架构 SPEC（五层协议）
2. README.md                  —— 现有能力与 CLI 用法
3. tests/test_e2e.py 的 start_test_server 函数 —— 测试沙箱约定

【铁律（违反即为失败）】
- 运行 python -m tests.test_e2e，动手前必须先确认现有测试全部通过；
  之后的每一步改动都不得破坏既有测试
- 存储一律 append-only JSONL + 读-改-写更新 + 锁，不引入任何外部数据库
- 核心层（hacoo/ 不含 dashboard）保持零第三方依赖
- 新通用方法必须五处同步：hacoo/gateway/registry.py（MethodSpec）、
  hacoo/gateway/gateway.py（分发）、hacoo/access/mcp_server.py（TOOLS）、
  hacoo/access/sdk.py（便捷方法）、hacoo/cli.py（子命令）
- 新增配置项登记进 hacoo/config.py 的环境变量注释清单
- 测试数据目录一律走沙箱重定向，不得污染工作区的 logs/ knowledge/ runs/
- 中文注释、英文标识符；结构化错误码沿用现有风格
  （ARGUMENT_ERROR/UNAUTHORIZED/AGENT_DISABLED 等）
- 每份 SPEC 末尾的「明确不做」章节是硬边界，不得越界实现
- 每完成一个阶段，运行全量测试确认全绿，并汇报：
  改了哪些文件 / 新增哪些测试 / 测试结果 / 与 SPEC 的偏差（如有）

【任务范围】docs/ 下三份 SPEC，实施顺序固定：
  阶段1: SPEC-agent-growth-loop.md（成长闭环：观测/反馈/insight）
  阶段2: SPEC-evolution-and-domain-upgrade.md 的 Part A + Part B（升级管理+验证体系）
  阶段3: SPEC-dashboard.md（可视化操作平台）
  阶段4: SPEC-evolution-and-domain-upgrade.md 的 Part C（领域 backlog，逐项来）
回复"已读完三份文件，理解铁律"后等待我的阶段指令。
```

---

## §2 阶段 1 提示词（成长闭环）

```
执行阶段1：严格按 docs/SPEC-agent-growth-loop.md 实现全部内容（§1~§4）。

实施顺序建议（每步后跑测试）：
1. hacoo/observations.py + 配置项 + .gitignore + 网关观测钩子 → 测试 1
2. knowledge.py schema 扩展（decision_id/review_status/weight/miss_log）→ 测试 2、4
3. 反馈三方法（api_feedback_submit/api_review_queue/api_knowledge_review）
   五处同步 → 测试 2、3
4. hacoo/insights.py（InsightStore + 4 检测器）+ 四个 api_insights_* /
   api_observations_query 五处同步 → 测试 5、6、7
5. agents/registry.py 的 _AGENT_TEMPLATE 追加两条纪律 + agents sync
6. README「成长闭环」小节

完成后运行 python -m tests.test_e2e 全量回归，按铁律第 9 条汇报。
注意：detect 阈值做成模块常量；钩子失败只记 warning 不得影响主调用。
```

---

## §3 阶段 2 提示词（升级管理 + 验证体系）

```
执行阶段2：严格按 docs/SPEC-evolution-and-domain-upgrade.md 的
Part A 和 Part B 实现（Part C 是 backlog，本阶段不做）。

实施顺序建议（每步后跑测试）：
1. hacoo/upgrades.py（UpgradeStore + 状态机 + acceptance 强制检查）
   → Part D 测试 1
2. agents/registry.py AgentSpec.version 字段 + agents.json 覆盖 +
   sync 写 frontmatter → Part D 测试 5
3. 三个 api_upgrade_* 五处同步 + insight confirm 的 create_upgrade 联动
   → Part D 测试 4
4. hacoo/compat.py + tests/contracts/ 快照机制 + test_contract_snapshot
   → Part D 测试 2
5. hacoo/bench.py + benchmarks/ 目录 + bench run/save-baseline/compare
   CLI + 每 agent 至少 1 个 golden case 示例 → Part D 测试 3
6. docs/CONTRIBUTING.md 写入 B.3.4 的升级验收规则

完成后全量回归 + 汇报。注意：契约快照对"新增"宽松（自动更新）、
对"删除/收窄"严格（测试失败）；bench 沙箱必须复用 start_test_server 模式。
```

---

## §4 阶段 3 提示词（Dashboard）

```
执行阶段3：严格按 docs/SPEC-dashboard.md 实现。

实施顺序建议：
1. hacoo/dashboard_data.py 数据层全部 dd_* 函数（先写这个，含单测）
   —— 这是本阶段的核心交付，UI 层只是它的渲染
2. 离线只读模式（offline_dir 分支 + 黄色横幅逻辑）
3. dashboard/app.py + 10 个 pages（按 P01→P10 顺序，
   每页写完手动 streamlit run 验证一次）
4. python -m hacoo dashboard 启动命令 + 配置项 + import 冒烟测试
5. requirements.txt 追加 streamlit（可选依赖，core 不 import）

铁律特别强调：所有写操作走 SDK 经网关，禁止直接写 logs//knowledge/ 文件；
写操作必须二次确认；错误码翻译为中文引导，不得露堆栈。
完成后全量回归 + 按 §9 验收清单逐项自检 + 汇报。
```

---

## §5 阶段 4 提示词（领域 backlog，一次只做一项）

```
执行阶段4：按 SPEC-evolution-and-domain-upgrade.md Part C 的 backlog，
本次只实现 <在此填入条目编号，如 FE-1>。

流程：
1. 先调用（或模拟）api_upgrade_propose 为该条目立项，acceptance 栏
   填写 SPEC 中对应的验收标准
2. 实现（真实 adapter 方法需遵循 T2 原则：工具差异封装在 adapter 内；
   本机无真实 EDA 二进制，用 mock 模式开发，binary 缺失报错走
   HACOO_<TOOL>_BIN 引导模式）
3. 按 acceptance 编写对应 bench golden case，放入 benchmarks/<agent>/
4. 验收三件套：全量 e2e 绿 + bench compare 无回退 + 目标 case pass
5. api_upgrade_update --to done 并记录 version_after
完成后汇报。等待我给出下一个条目编号。
```

---

## §6 排障提示词（卡住时使用）

```
停下来。运行 python -m tests.test_e2e，把失败测试的输出完整贴出来，
然后：1) 说明失败原因分析；2) 给出最小修复方案；3) 修复后重跑。
不要绕过测试（禁止改测试来迁就实现，除非证明是测试本身错误并说明理由）。
```


---

## §7 Backlog 条目提示词库（§5 的具体化，逐条复制粘贴）

> 前提：本会话已粘贴 §1 主控提示词。每条提示词自带实现锚点与验收口径。
> 已完成：~~BE-1~~（apr@1.1.0）。
> 通用口径（每条都适用，不再重复）：
> - 先 `api_upgrade_propose` 立项（acceptance 抄 SPEC 对应条目），完成后
>   `api_upgrade_update --to done` 并填 `version_after`
> - 本机无真实 EDA 二进制：adapter 的命令构造与报告解析一律做成**纯函数**
>   （`_build_*` / `_parse_*`），用 `tests/fixtures/` 样例文本单测；
>   真实二进制缺失时走 `resolve_binary` 引导报错，不在本机强跑
> - 收尾三件套：`python -m tests.test_e2e` 全绿 + `bench compare` 无回退 +
>   目标 bench case pass（依赖真实工具的 case 标注 `"requires": "<tool>"`，
>   本机 skip，公司 Linux 复跑）

### 第一波（地基）

#### FE-1 真实综合 adapter

```
实现 backlog FE-1：真实综合 adapter。
1. 新建 hacoo/adapters/synthesis.py，复用 hacoo/adapters/shell.py 的
   TclShell + resolve_binary 模式（参考 primetime.py 的既有写法）：
   - GenusAdapter (tool_name="genus", 二进制 HACOO_GENUS_BIN 或 genus -legacy_ui)
   - DCAdapter (tool_name="dc_shell", 二进制 HACOO_DC_SHELL_BIN 或 dc_shell-t)
   两个类实现同名方法：api_synth_elaborate(files:list, top:str)、
   api_synth_compile(effort:str="medium")、api_report_qor()。
   工具命令差异（read_hdl vs analyze/elaborate 等）封装在各自类内（T2 原则）。
2. hacoo/adapters/__init__.py 注册两个 tool；agents/registry.py 的
   synthesis agent 白名单加入 genus/dc_shell，然后 agents sync。
3. tests/fixtures/ 放 genus 与 dc 的 report_qor 样例文本各一份；
   _parse_qor 纯函数单测覆盖 WNS/TNS/area/leakage/total_power 提取。
4. bench case benchmarks/synthesis/syn-qor-01.json：requires=genus，
   断言 api_report_qor 返回 data 含上述五个字段。
5. 收尾三件套 + 汇报。
```

#### FE-2 QoR 报告解析器

```
实现 backlog FE-2：综合 QoR 报告解析（如 FE-1 已含解析则本项做指标归一）。
1. 确认 FE-1 的 _parse_qor 输出 key 归一为 {wns, tns, area, leakage,
   total_power}（单位注明在 description，数值用 float）。
2. 确认这些字段能被 observations 的 extract_metrics 白名单拾取
   （hacoo/observations.py 的已知 key 列表，缺则补）。
3. 单测：fixtures 样例 → 字段齐全且类型正确；观测钩子能把它们落盘。
4. 收尾三件套 + 汇报。
```

#### BE-2 APR 阶段指标提取

```
实现 backlog BE-2：ICC2/Innovus 的阶段指标提取（BE-1 的方法已就位，补解析）。
1. 在 icc2.py / innovus.py 各加（或扩展）方法：
   api_report_congestion()、api_cts_report()、api_route_drc_report()。
2. 纯函数解析器（fixtures 样例单测）：
   - congestion → {hotspot_count, overflow_max}
   - CTS → {skew, latency_max, clock_power}（report_clock_trees/
     report_ccopt_clock_trees 格式差异封装在各自 adapter）
   - route DRC → {drc_count, top_rules: [(rule, count)]}
3. metrics key 归一（两工具同名同义），确认进观测白名单。
4. bench case benchmarks/apr/apr-metrics-01.json（requires 真实工具，本机 skip）。
5. 收尾三件套 + 汇报。
```

#### SO-1 STA MCMM 场景管理

```
实现 backlog SO-1：PrimeTime MCMM。
1. primetime.py 新增：api_mcmm_create_scenario(name, mode, corner,
   lib_paths:list, sdc:str="")、api_mcmm_report_all(max_paths:int=5)。
   命令序列：create_scenario -mode .. -corner .. / set_scenario_status /
   report_timing -scenarios ...（PT 语义，封装在 adapter 内）。
2. _parse_mcmm_report 纯函数 + fixtures：输出 {scenarios: {name: {wns, tns}},
   global_wns, worst_scenario}。
3. bench case benchmarks/sta/sta-mcmm-01.json（requires=primetime）：
   4 场景 → per-scenario WNS + global WNS 全部解析正确。
4. sta agent 的 subagent prompt（registry.py _AGENT_TEMPLATE 或 sta 专属段落）
   补一句 MCMM 使用纪律；agents sync。
5. 收尾三件套 + 汇报。
```

#### SO-4 Calibre LVS/PERC 扩展

```
实现 backlog SO-4：Calibre 批处理扩展。
1. calibre.py 新增 api_calibre_lvs(rule_deck, layout, netlist) 与
   api_calibre_perc(rule_deck, layout, perc_type:str="esd")，
   走 run_batch 模式（参考既有 DRC 方法的 argv 构造）。
2. _parse_rve 纯函数（RVE 结果文件 → [{rule, count, bbox}], fixtures 单测）；
   LVS 报告解析 {shorts, opens, net_mismatch, result: PASS|FAIL}。
3. bench case benchmarks/pv/pv-lvs-01.json（requires=calibre）。
4. pv agent 白名单确认已含 calibre（应已有）；prompt 补 LVS/PERC 纪律。
5. 收尾三件套 + 汇报。
```

#### SO-7 EMIR hotspot 解析

```
实现 backlog SO-7：EMIR hotspot 结构化报告。
1. redhawk_sc.py 与 voltus.py 各加 api_emir_report_hotspots(top_n:int=20)，
   输出 {hotspots: [{instance, x, y, ir_drop_mv, layer}], worst_drop_mv}。
2. _parse_emir_report 纯函数 ×2（两工具报告格式各一 fixtures 单测）。
3. 确认 ir_drop_mv 进观测白名单（趋势检测要用）。
4. bench case benchmarks/irem/emir-hotspot-01.json（requires 真实工具）。
5. 收尾三件套 + 汇报。
```

#### X-1 趋势检测器指标方向表扩充

```
实现 backlog X-1：让 C 系指标全部可被趋势检测器命中。
1. hacoo/insights.py 的趋势检测器：扩充指标方向表——
   lower_better(恶化=下降): wns, tns, slack；
   higher_worse(恶化=上升): drc_count, violator_lines, hotspot_count,
   overflow_max, skew, latency_max, area, leakage, total_power, ir_drop_mv。
   未知 key 默认按 higher_worse 处理并记 debug 日志。
2. 单测：对 wns（下降恶化）与 drc_count（上升恶化）各构造 3 点序列，
   确认 trend insight 触发；反向序列不触发。
3. e2e：FE-2/BE-2/SO-7 的 metrics 种子数据 → scan → 对应 trend insight 出现。
4. 收尾三件套 + 汇报。
```

### 第二波（收敛环）

#### BE-4 route DRC 收敛 skill

```
实现 backlog BE-4：skill route-drc-convergence。
1. 新建 .opencode/skills/route-drc-convergence/SKILL.md：循环结构
   report_drc → 按类型分类(short/spacing/antenna) → 知识库检索修复策略 →
   执行修复 → 复检；DRC 数不降或超 3 轮则升级人工。每轮决策必须
   decision_record（含该轮 DRC 计数 metrics）。
2. mock_eda 如需支持可变 DRC 计数（按调用轮次递减），最小改动扩展之
   （不得破坏既有 32+ 测试）。
3. bench case benchmarks/apr/apr-route-drc-loop-01.json：mock 下跑 3 轮，
   断言 DRC 计数单调下降 + 每轮有决策记录。
4. apr agent prompt 引用该 skill；agents sync。
5. 收尾三件套 + 汇报。
```

#### SO-2 STA ECO 优先级 skill

```
实现 backlog SO-2：skill sta-eco-priority。
1. 新建 .opencode/skills/sta-eco-priority/SKILL.md：violation 按
   slack × 路径数 × 修复成本排序；setup/hold 修复冲突检测
   （修 setup 插入 delay 会恶化的 hold 路径必须标记 conflict）。
2. bench case benchmarks/sta/sta-eco-priority-01.json：mock 混合
   violation 场景 → 断言排序结果与 conflict 标记正确。
3. sta prompt 引用；agents sync。
4. 收尾三件套 + 汇报。
```

#### EC-1 eco_loop flow 模板（本阶段最大项，先做引擎扩展评估）

```
实现 backlog EC-1：eco_loop flow 模板。
1. 先读 hacoo/flow/engine.py 与 templates.py，评估当前引擎是否支持
   "阶段内循环 + 收敛判据 + 升级人工"。若只支持线性 stage：
   最小扩展模板 schema——stage 增加 loop: {max_iter, converge_when,
   escalate_when} 字段，引擎在 stage 内执行循环体直至收敛/上限/升级。
   不得破坏 block_signoff / sta_only 既有行为（e2e 已覆盖）。
2. 新增模板 eco_loop：sta 诊断（按 SO-2 排序）→ apr 执行 ECO →
   增量 sta/pv 验证 → 收敛判据（wns >= 阈值 或 violation=0）；
   超 max_iter 升级人工。全流程决策链可 trace。
3. e2e 测试：mock 下 eco_loop 完整跑通，trace 含全部迭代决策；
   超限时状态=escalated 且原因明确。
4. bench case benchmarks/sta/eco-loop-01.json。
5. 收尾三件套 + 汇报。若引擎扩展评估结论与设计冲突，先停下来汇报方案再动手。
```

#### FE-3 综合收敛 skill

```
实现 backlog FE-3：skill synth-convergence。
1. 新建 .opencode/skills/synth-convergence/SKILL.md：compile → report_qor →
   未达标时按知识库检索（QoR 问题类型 → 策略）选择下一轮
   effort/boundary_opt/ungroup 策略；最多 3 轮，不收敛升级人工。
2. bench case benchmarks/synthesis/synth-conv-01.json：mock 下 3 轮内
   WNS 单调改善 + 每轮有决策记录。
3. synthesis prompt 引用；agents sync。
4. 收尾三件套 + 汇报。
```

### 第三波（深水区，逐项来，每项开始前先确认前两波全部 done）

#### FE-4 DFT adapter

```
实现 backlog FE-4：DFT adapter。
1. 新建 hacoo/adapters/dft.py：TessentAdapter（tool_name="tessent"，
   HACOO_TESSENT_BIN / tessent -shell）+ DftMaxAdapter（tool_name="dftmax"，
   经 dc_shell-t）。方法：api_dft_insert_scan / api_dft_atpg(patterns:int) /
   api_dft_report_coverage() → {test_coverage, fault_coverage, untestable}。
2. 解析纯函数 + fixtures 单测；adapters/__init__.py 注册；
   dft agent 白名单 + sync。
3. bench benchmarks/dft/dft-cov-01.json（requires=tessent）。
4. 收尾三件套 + 汇报。
```

#### BE-3 fp-exploration skill（依赖 X-3，若 X-3 未做则先做 X-3）

```
实现 backlog BE-3：skill fp-exploration（前置：X-3 并行 fan-out 已完成）。
1. .opencode/skills/fp-exploration/SKILL.md：同一 block 以 ≥2 组
   utilization/aspect/macro 方案并行试跑（flow engine fan-out），
   汇总 congestion/QoR 对比表，推荐方案并 decision_record 说明理由。
2. bench benchmarks/apr/fp-explore-01.json：mock 下产出 ≥2 方案对比 +
   推荐决策记录。
3. apr prompt 引用；agents sync。
4. 收尾三件套 + 汇报。
```

#### SO-3 SI-aware STA

```
实现 backlog SO-3：PrimeTime SI-aware 支持。
1. primetime.py 的 api_report_timing 增加 si_enabled:bool=False；
   True 时命令序列含 set_si_options（delta delay / noise）与对应解析。
2. 单测：命令构造含 SI 设置；解析兼容含 delta 分量的报告 fixture。
3. bench benchmarks/sta/sta-si-01.json（requires=primetime）。
4. 收尾三件套 + 汇报。
```

#### SO-5 Calibre waiver 流程

```
实现 backlog SO-5：waiver 流程（问责敏感区，决策记录是硬性要求）。
1. calibre.py 新增 api_calibre_waive(rule, bbox, justification)：
   waiver 规则文件管理 + waive 后报告分 waived/unwaived。
2. adapter 内部强制：waive 操作无 decision_record 时抛错
   （在方法 docstring 与 subagent prompt 双重声明）。
3. bench benchmarks/pv/pv-waive-01.json：waive 后报告分类正确 +
   决策记录存在。
4. 收尾三件套 + 汇报。
```

#### SO-6 pv-debug-triage skill

```
实现 backlog SO-6：skill pv-debug-triage。
1. .opencode/skills/pv-debug-triage/SKILL.md：violation 按 rule/区域聚类 →
   判定真实问题（移交 apr）vs deck/设置问题（移交 flow）；输出分派表。
2. bench benchmarks/pv/pv-triage-01.json：混合 violation 输入，
   断言分派正确率（fixtures 中标注期望分派）。
3. pv prompt 引用；agents sync。
4. 收尾三件套 + 汇报。
```

#### SO-8 emir-fix-strategy skill

```
实现 backlog SO-8：skill emir-fix-strategy（依赖 SO-7）。
1. .opencode/skills/emir-fix-strategy/SKILL.md：hotspot 归因
   （decap 不足 / PG 网格稀疏 / 局部高翻转）→ 修复策略选择 →
   移交 apr 执行（跨 agent 协作，决策链记录交接理由）。
2. bench benchmarks/irem/emir-fix-01.json：hotspot 输入 → 归因 + 策略 +
   移交决策记录齐全。
3. irem/apr 两侧 prompt 都引用；agents sync。
4. 收尾三件套 + 汇报。
```

#### EC-2 eco-scope-control skill

```
实现 backlog EC-2：skill eco-scope-control（依赖 EC-1）。
1. .opencode/skills/eco-scope-control/SKILL.md：ECO 前检查受影响区域
   是否已 freeze；优先 spare cell / metal-only 方案；freeze 区域被波及
   时给出替代方案而不是直接动手。
2. bench benchmarks/apr/eco-scope-01.json：freeze 区域方案被拒绝 +
   替代方案输出 + 决策记录。
3. apr prompt 引用；agents sync。
4. 收尾三件套 + 汇报。
```

#### X-2 recipe 学习（知识库 design 画像）

```
实现 backlog X-2：知识条目按 design 画像检索。
1. knowledge.py：add() 增加 profile: dict 字段（foundry/node/block_type）；
   query() 增加 profile 参数——同 profile 命中条目排序加权（在
   weight × reuse_count 之上乘 profile 匹配系数，匹配维度数越多越高）。
2. subagent prompt 纪律：沉淀知识时必须带当前 design 的 profile tags。
3. 单测：同 keyword 下带匹配 profile 的条目排在通用条目之前。
4. bench benchmarks/sta/recipe-01.json。
5. 收尾三件套 + 汇报。
```

#### X-3 flow engine 并行 fan-out

```
实现 backlog X-3：flow engine 支持 fan-out。
1. hacoo/flow/engine.py：stage 增加 parallel: [{params}, {params}] 形态——
   同一 agent 开多个实例并行执行（注意每实例独立 spawn/release，
   互不共享状态），全部完成后进入 compare 步骤（模板内声明对比指标）。
2. 安全约束：fan-out 数受 max_instances_per_user 配额限制，超限报错
   QUOTA_EXCEEDED 而非静默排队。
3. e2e：2 路并行 + 结果汇总 + trace 完整；配额超限路径有测试。
4. bench benchmarks/flow/fanout-01.json。
5. 收尾三件套 + 汇报。
```

#### X-4 run ↔ 数据版本追踪

```
实现 backlog X-4：ISF 数据版本透传到 flow manifest。
1. flow/templates.py 与 engine.py：模板 params 支持 data_version /
   design_version；flow_start 时写入 runs/<run_id>.json manifest；
   flow_trace 输出包含版本字段。
2. isf adapter 的 api_isf_fw_start 记录已有版本字段——确认 run 与
   isf flow 的 flow_id 互相引用（manifest 里存 isf flow_id，
   isf record 里可附 run_id）。
3. e2e：manifest 含版本 + 双向引用。
4. 收尾三件套 + 汇报。
```

#### FE-5 dft-coverage-closure skill

```
实现 backlog FE-5：skill dft-coverage-closure（依赖 FE-4）。
1. .opencode/skills/dft-coverage-closure/SKILL.md：coverage 未达标 →
   undetectable fault 归因分类（AU/UD/ND 等）→ test point 插入 /
   pattern 调整策略 → 复跑 ATPG。
2. bench benchmarks/dft/dft-closure-01.json：未达标场景输出归因分类 +
   策略建议 + 决策记录。
3. dft prompt 引用；agents sync。
4. 收尾三件套 + 汇报。
```

#### X-5 跨域关联 insight（thermal × EMIR）

```
实现 backlog X-5：跨域关联检测器。
1. hacoo/insights.py 新增检测器 5（kind="cross_domain"）：
   thermal 与 irem 的 hotspot 观测按 (x, y) 坐标容差匹配
   （默认容差 5um，模块常量），重叠即生成关联 insight，
   evidence 双侧引用，通知两个 agent 的 owner。
2. signature 关键串含坐标网格（容差取整后），保证去重稳定。
3. 单测：重叠/不重叠两组坐标输入的触发与不触发。
4. 收尾三件套 + 汇报。
```
