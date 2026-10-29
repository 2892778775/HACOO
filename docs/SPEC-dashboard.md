# HACOO 可视化操作平台 SPEC（Dashboard）

> 目标读者：Kimi Code（实现方）。
> 前置依赖：`docs/SPEC-agent-growth-loop.md`（反馈/复核/insight/观测）、
> `docs/SPEC-evolution-and-domain-upgrade.md`（升级管理/基准测试）。
> 本 SPEC 定义 HACOO 的 Web 可视化操作平台：工程师通过浏览器完成
> 日常 80% 的平台交互（对话入口 OpenCode 保留，Dashboard 承担看板 +
> 批量操作 + 表单类工作）。
>
> 风格约定与现状锚点沿用 `SPEC-agent-growth-loop.md` §0。

---

## 1. 技术选型与架构

### 1.1 选型：Streamlit

理由：
- 数据源全是 JSONL + SDK 调用，Streamlit 零前端成本
- 团队工程师 Python 熟练，可自维护、自扩展（符合"每个 stage 工程师
  运维自己的 Agent"的平台文化）
- 表单/按钮/表格/图表能力覆盖全部需求场景

依赖处理：`streamlit` 加入 `requirements.txt` 但设为可选——核心层保持
零第三方依赖（`python -m hacoo serve` 不需要 streamlit），仅
`python -m hacoo dashboard` 时检查导入，缺失则给出
`pip install streamlit` 引导。

### 1.2 架构原则：SDK 优先，绝不绕过网关

```
浏览器
  ↓
Streamlit app (dashboard/)
  ↓ 所有读写操作走 HacooClient SDK (hacoo/access/sdk.py)
HACOO server (python -m hacoo serve)
  ↓
Gateway (auth/quota/audit) → adapters / knowledge / decisions / ...
```

**铁律**：Dashboard 的一切写操作（反馈、复核、Agent 启停、flow 启动、
升级提案）必须经 SDK 走网关——保留多用户隔离、配额、审计与决策问责。
**禁止**直接改写 `logs/*.jsonl` / `knowledge/*.jsonl` 文件。

唯一例外：**离线只读模式**（见 §6）。

### 1.3 代码结构：数据层与 UI 层分离（可测试性）

```
hacoo/
  dashboard_data.py        # 纯函数数据层: 输入 HacooClient, 返回 dict/DataFrame
                           # 不含任何 streamlit import —— 可单测 (见 §8)
dashboard/
  app.py                   # streamlit 入口 (st.set_page_config + 侧边栏导航)
  pages/
    01_overview.py
    02_runs.py
    03_flow_launcher.py
    04_review.py
    05_knowledge.py
    06_insights.py
    07_upgrades.py
    08_agents.py
    09_bench.py
    10_observations.py
```

启动命令：`python -m hacoo dashboard [--port 8501]`
（内部 `subprocess` 调 `streamlit run dashboard/app.py`；
配置项 `HACOO_DASH_PORT`，默认 8501，登记进 `hacoo/config.py` 注释清单）

### 1.4 鉴权与多用户

- 侧边栏顶部：server 地址（默认 `127.0.0.1:9877`）+ token 输入框
- token 存入 `st.session_state`，所有 SDK 调用带该 token
- `hacoo_whoami` 显示当前身份/角色/配额；未连接成功时全页面显示
  连接引导而不是报错堆栈

---

## 2. 页面详设

### P01 总览（Overview）

| 区块 | 数据层函数 | 内容 |
|---|---|---|
| Agent 状态条 | `dd_agents_status(client)` | 9 个 Agent 的 enabled/version 徽章（版本依赖进化 SPEC §A.3） |
| 最近 Run | `dd_recent_runs(client, n=10)` | run_id / 模板 / 状态 / 时长，点击跳 P02 详情 |
| 待办计数 | `dd_todo_counts(client)` | 待复核决策数 / new insight 数 / proposed 升级提案数，点击跳对应页 |
| 配额与身份 | SDK `whoami()` | 用户/角色/实例占用 |

### P02 Run 中心

- Run 列表：按状态/时间筛选
- Run 详情（核心问责视图，数据来自 `api_flow_trace`）：
  - stage 时间线（横向步骤条，ok/failed/skipped 着色）
  - 决策表：每行 决策 agent/action/rationale/status/metrics，
    每行末尾 **反馈按钮**（弹表单调 `hacoo_feedback`，`decision_id` 自动带上）
  - 关联审计条目（request_id 级，折叠展示）
  - 指标趋势图：该 agent 历史 run 的 WNS/TNS 折线
    （数据来自观测层 `api_observations_query`，`st.line_chart`）

### P03 Flow 启动台

- 模板选择（`api_list_flow_templates`）+ 参数表单（按模板 params 动态生成）
- **Agent 参与矩阵**：9 个 Agent 的 checkbox（当前 enabled 状态为默认值），
  修改走 `agents enable/disable` 对应管理接口（管理员 token 才显示可编辑，
  否则只读 + "联系管理员"提示）
- ISF flow 启动表单：project / flow 类型 / block / flow_path /
  data_version / design_version / usertcl → 提交后展示返回的
  `airflow_urls`（可点击）与 `flow_id`
- 启动后跳 P02 跟踪

### P04 决策复核（Review Queue）

- 数据源 `api_review_queue`；按 agent 筛选
- 每条目卡片：决策摘要 / 知识条目内容 / evidence 链接
- 操作按钮：**批准** / **打回**（`api_knowledge_review`，打回必填 note）
- 顶部：**主动反馈表单**（不绑定具体决策的纠正/建议，
  调 `hacoo_feedback`）

### P05 知识库浏览器

- 按 agent 选择 + 关键词/tag 检索（`api_knowledge_query`）
- 条目卡片显示：symptom/root_cause/resolution/source/weight/
  reuse_count/review_status/agent_version
- 排序权重可视化（weight 与 reuse_count 徽标），source=human_correction
  高亮——让工程师看到"我教过它什么"

### P06 Insight 中心

- `api_insights_list`，按 kind（anomaly/trend/blind_spot/self_issue）/
  status 筛选；self_issue 类置顶标红
- 每条：title/detail/evidence 展开/proposed_action
- 操作：**确认**（可选 auto_kb 生成知识条目、create_upgrade 生成提案）/
  **忽略**（dismiss）
- 顶部按钮："立即扫描"（`api_insights_scan`）+ 上次扫描时间

### P07 升级管理

- 提案看板：按 status 分列（proposed/accepted/in_progress/done/rejected）
  的简易 kanban（`st.columns` 实现）
- 新建提案表单（kind/target/motivation/acceptance 必填校验）
- 状态流转按钮（按合法流转表渲染可用按钮，非法流转不显示）

### P08 Agent 管理

- 表格：agent / title / stage / version / enabled / 工具白名单 /
  知识条目数 / 决策成功率（近 30 天，数据层聚合）
- 操作：enable/disable 开关、sync 按钮（管理员）
- 点击展开：该 agent 的最近决策列表 + 知识库 Top 10（按 weight×reuse）

### P09 基准与兼容性

- `bench run` 触发 + 历史 `bench_runs/` 结果表格（离线读取该目录，
  只读不写）
- baseline 对比视图：pass 率 delta、fail→pass / pass→fail 清单
- 契约快照状态：最近一次 `compat check` 结果

### P10 观测与日志（排障）

- `api_observations_query` 查询界面（agent/tool/run_id/时间窗筛选）
- error_signatures 聚类展示（数据层聚合 Top N）
- 审计日志 tail 查看（只读）

---

## 3. 通用 UI 规范

- 全中文界面；技术术语保留英文（STA/APR/WNS/run_id 等）
- 所有列表页：刷新按钮 + "上次刷新时间"（Streamlit 无自动推送，
  长任务用 `st.empty()` + 轮询占位，间隔 5s）
- 所有写操作：按钮 → `st.confirm` 二次确认 → 成功后 toast + 页面 rerun
- 错误展示：SDK 抛出的结构化错误码（UNAUTHORIZED/QUOTA_EXCEEDED 等）
  翻译为中文引导文案，不直接抛堆栈
- 颜色语义：ok/success=绿，failed=红，pending/running=黄，
  skipped/dismissed=灰

---

## 4. 数据层接口清单（`hacoo/dashboard_data.py`）

```python
# 每个函数: 输入 HacooClient + 筛选参数, 返回 list[dict] 或 dict
# 命名前缀 dd_ (dashboard data); 全部可脱离 streamlit 单测
dd_agents_status(client) -> list[dict]
dd_recent_runs(client, limit=10) -> list[dict]
dd_todo_counts(client) -> dict          # {pending_decisions, new_insights, proposed_upgrades}
dd_run_trace(client, run_id) -> dict
dd_metric_trend(client, agent, metric_key, limit=50) -> list[dict]  # [{ts, value, run_id}]
dd_review_queue(client, agent=None) -> list[dict]
dd_knowledge_search(client, agent, keyword="", tags=None) -> list[dict]
dd_insights(client, agent=None, kind=None, status=None) -> list[dict]
dd_upgrades_by_status(client) -> dict   # {status: [proposal]}
dd_agent_stats(client, agent, days=30) -> dict  # 决策成功率等聚合
dd_observations(client, agent=None, tool=None, run_id=None, limit=200) -> list[dict]
```

## 5. 新通用方法缺口

Dashboard 需要的读接口基本已由前两份 SPEC 覆盖；如实现中发现缺口
（如"列出全部 run 的摘要列表"目前只有单 run 查询），允许新增
**只读**通用方法 `api_runs_list(limit, status)`：
读 `runs/*.json` manifest 汇总，capability=read，按五处同步规范登记。
除此之外不得为本 SPEC 新增写接口——写路径全部复用既有方法。

## 6. 离线只读模式

server 未启动时，Dashboard 可降级为直接读 JSONL 文件的**只读**模式：
- 侧边栏明确黄色横幅："离线只读模式——server 未运行，写操作不可用"
- 仅 P01/P02(历史 run)/P05/P09/P10 可用，且全部操作按钮隐藏
- 实现方式：`dashboard_data.py` 的每个函数加 `offline_dir` 参数分支，
  读文件而非调 SDK；UI 层不感知差异

## 7. 配置项

```
HACOO_DASH_PORT        Dashboard 端口 (默认 8501)
HACOO_DASH_OFFLINE     "1" 强制离线只读模式 (默认 0)
```

## 8. 测试要求

- `dashboard_data.py` 全函数单测：在 `start_test_server` 沙箱上构造数据
  （种子 run/决策/知识/insight），断言返回结构与聚合数值正确
- 离线模式：构造临时 JSONL 目录，断言相同函数离线分支返回一致结构
- UI 层不做自动化测试（Streamlit 限制），但 `dashboard/app.py` 与
  每个 page 必须能被 `python -c "import ..."` 语法检查（CI 里加
  import 冒烟测试，streamlit 未安装时 skip）
- 回归标准：前两份 SPEC 的全部测试 + 本 SPEC 数据层测试全绿

## 9. 验收清单

- [ ] `python -m hacoo dashboard` 启动，浏览器打开总览页
- [ ] 完整走通一条闭环：P03 启动 sta_only flow → P02 查看决策链 →
      对一条决策点"反馈"提交纠正 → P04 复核队列出现并批准 →
      P05 知识库可见该条目且 weight=5
- [ ] P06 扫描产生 insight → 确认（auto_kb + create_upgrade）→
      P07 看板 proposed 列出现提案 → 流转到 done
- [ ] 离线模式启动，横幅正确、写按钮全隐藏
- [ ] 错误 token 登录时全页面显示引导而非堆栈

## 10. 明确不做

- 对话式交互（那是 OpenCode 的职责，Dashboard 不做 chat 框）
- 用户/角色管理界面（沿用 auth file，不做注册/改密 UI）
- 实时推送/WebSocket（轮询足够）
- 移动端适配
- 多语言
