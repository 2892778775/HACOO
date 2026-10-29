# HACOO 贡献指南

## 升级验收规则（SPEC-evolution B.3.4）

任何升级（Agent / skill / adapter / 平台功能）落地后，必须把对应提案
`api_upgrade_update --to done`。标记 done 之前，以下三条**全部**满足：

1. **兼容性层**：`python -m tests.test_e2e` 全绿（含契约快照测试
   `test_contract_snapshot`）。契约快照对「新增方法 / 新增可选参数」宽松
   （测试自动更新 `tests/contracts/` 快照）；对「删除/改名方法、删除参数、
   参数 required 收窄、参数类型变化」严格（测试失败）——破坏性变更必须
   显式更新快照（`python -m hacoo compat snapshot`）并在 commit message
   声明 `BREAKING`。
2. **能力层**：`python -m hacoo bench compare --baseline baselines/<agent>@<old>.json`
   对受影响 agent **无 pass→fail 回退**；提案 `acceptance` 中点名的
   bench case 必须 fail→pass（或新建即 pass）。
3. **版本留痕**：`python -m hacoo bench save-baseline --agent <name>` 生成
   新基准（`baselines/<agent>@<new-version>.json`，git 追踪），并在提案
   `version_after` 中记录版本号（`api_upgrade_update --to done
   --version-after sta@1.3.0`）。

## 版本 bump 约定（SPEC-evolution A.3）

- 知识库新增条目 → **patch**
- 新增/修改 skill 或工具白名单 → **minor**
- Agent 职责变化 → **major**

bump 方式：`python -m hacoo agents version <name> <x.y.z>`（写入
`hacoo.agents.json` 的 `versions` 覆盖表），然后 `agents sync` 让
`.opencode/agent/hacoo-*.md` frontmatter 带上新版本。

## 新增通用方法的纪律（五处同步）

新 `api_*` 通用方法必须同步登记五处，缺一不可：

1. `hacoo/gateway/registry.py` — `MethodSpec`（含参数签名与 capability）
2. `hacoo/gateway/gateway.py` — `_exec_generic` 分发
3. `hacoo/access/mcp_server.py` — `TOOLS` + `_call_tool`
4. `hacoo/access/sdk.py` — 便捷方法
5. `hacoo/cli.py` — 子命令

新增配置项登记进 `hacoo/config.py` 的环境变量注释清单；测试数据目录一律
走 `start_test_server` 沙箱重定向，不得污染工作区的 `logs/` `knowledge/`
`runs/`。
