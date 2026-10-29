"""Flow 模板 — 预定义设计流程 (flow 编排知识, 类似 ISF flow 定义的本地镜像)。

每个 stage: {name, agent, method, args}。args 值中 "${param}" 会被
flow_start 传入的参数替换。stage 的 agent 被禁用时整个 run 失败并记录
AGENT_DISABLED 决策 — 保证"未参与的 Agent 不会偷偷执行"。

X-3 fan-out: stage 可带 parallel: [{params}, ...] — 同一 agent 开多个
独立实例并行执行 (每分支独立 spawn/release, 互不共享状态), 可选 setup
子阶段在分支内先执行 (分支实例状态自含); 全部完成后按 compare
{metrics, pick: min|max} 选出优胜分支。fan-out 数受
max_instances_per_user 配额限制, 超限报 QUOTA_EXCEEDED。
"""
from __future__ import annotations

from typing import Any, Dict, List

TEMPLATES: Dict[str, Dict[str, Any]] = {
    # 完整 block 实现 + 签核流程 (示例; 真实环境由 ISF 编排, 这里做本地等价物)
    "block_signoff": {
        "description": "block 级实现到签核: synthesis -> apr -> sta -> pv -> irem -> thermal -> sipi",
        "params": {"design_path": "chip.v", "top": "chip"},
        "stages": [
            {"name": "load_design", "agent": "synthesis",
             "method": "api_load_design",
             "args": {"path": "${design_path}", "top": "${top}"}},
            {"name": "synthesize", "agent": "synthesis",
             "method": "api_run_synthesis", "args": {"effort": "medium"}},
            {"name": "sta_report", "agent": "sta",
             "method": "api_report_timing", "args": {"max_paths": 20}},
            {"name": "sta_state", "agent": "sta",
             "method": "api_get_design_state", "args": {}},
        ],
    },
    # 只做 STA 的最小流程 (演示"自由搭配": 只启用 sta agent 即可运行)
    "sta_only": {
        "description": "仅 STA: 加载设计 -> 时序报告",
        "params": {"design_path": "chip.v", "top": "chip"},
        "stages": [
            {"name": "load_design", "agent": "sta",
             "method": "api_load_design",
             "args": {"path": "${design_path}", "top": "${top}"}},
            {"name": "sta_report", "agent": "sta",
             "method": "api_report_timing", "args": {"max_paths": 10}},
        ],
    },
    # EC-1: 时序 ECO 收敛循环 — sta 诊断 -> apr 执行 ECO -> sta 增量验证,
    # 每轮结束按 converge_when 判定; 超 max_iter 未收敛升级人工 (escalated)。
    # 决策链可 trace: 循环级决策 + 每轮子阶段决策 (params 带 _iteration)。
    "eco_loop": {
        "description": "时序 ECO 收敛循环: sta 诊断 (SO-2 排序) -> apr 执行 ECO -> "
                       "sta 增量验证, wns 达标收敛, 超限升级人工",
        "params": {"design_path": "chip.v", "top": "chip",
                   "target_wns": 0.0, "max_iter": 5,
                   "eco_strategy": "size_up"},
        "stages": [
            {"name": "load_design", "agent": "sta",
             "method": "api_load_design",
             "args": {"path": "${design_path}", "top": "${top}"}},
            {"name": "eco_iterate", "agent": "sta",
             "loop": {"max_iter": "${max_iter}",
                      "converge_when": {"metric": "wns", "op": ">=",
                                        "value": "${target_wns}"},
                      "source": "verify"},
             "body": [
                 {"name": "diagnose", "agent": "sta",
                  "method": "api_report_timing", "args": {"max_paths": 10}},
                 {"name": "apply_eco", "agent": "apr",
                  "method": "api_run_eco",
                  "args": {"strategy": "${eco_strategy}"}},
                 {"name": "verify", "agent": "sta",
                  "method": "api_report_timing", "args": {"max_paths": 10}},
             ]},
        ],
    },
    # X-4: 经 ISF 提交 flow — 引擎按方法反查 tool 懒 spawn isf 实例;
    # fw_start 把 ${run_id} 挂进 ISF record (反向), 返回的 flow_id 被引擎
    # 收集进 manifest isf_flow_ids (正向); data_version/design_version
    # 同时写入 manifest, 复盘可追溯 "这次 run 用的哪版数据"。
    "isf_flow_submit": {
        "description": "X-4: 经 ISF 提交 flow (mock/live 自适应): isfsetup -> "
                       "fw start (挂 run_id) -> flows 核对, 双向引用落 manifest",
        "params": {"project": "N3CTV1", "flow": "APR+TC+PV", "block": "BLOCK1",
                   "flow_path": "/FLOW/PATH/NAME", "usertcl": "",
                   "data_version": "", "design_version": ""},
        "stages": [
            {"name": "isf_setup", "agent": "flow",
             "method": "api_isf_setup", "args": {"project": "${project}"}},
            {"name": "fw_start", "agent": "flow",
             "method": "api_isf_fw_start",
             "args": {"flow": "${flow}", "block": "${block}",
                      "flow_path": "${flow_path}", "usertcl": "${usertcl}",
                      "data_version": "${data_version}",
                      "design_version": "${design_version}",
                      "run_id": "${run_id}"}},
            {"name": "flows_check", "agent": "flow",
             "method": "api_isf_flows", "args": {}},
        ],
    },
    # X-3: 时序报告 fan-out — 多组 max_paths 并行试跑 (每分支独立实例,
    # 分支内先自含 load_design), 完成后按 wns 越大越好选出优胜分支。
    "timing_fanout": {
        "description": "X-3 fan-out 示例: 同一 STA 报告按多组 max_paths 并行试跑, "
                       "compare 按 wns 选最优分支 (演示并行探索)",
        "params": {"design_path": "chip.v", "top": "chip"},
        "stages": [
            {"name": "explore", "agent": "sta",
             "method": "api_report_timing",
             "args": {"max_paths": "${max_paths}"},
             "setup": [{"name": "load_design", "agent": "sta",
                        "method": "api_load_design",
                        "args": {"path": "${design_path}", "top": "${top}"}}],
             "parallel": [{"max_paths": 5}, {"max_paths": 20}],
             "compare": {"metrics": ["wns"], "pick": "max"}},
        ],
    },
}


def list_templates() -> List[Dict[str, Any]]:
    return [{"name": name, "description": t["description"],
             "params": t["params"], "stages": [s["name"] for s in t["stages"]]}
            for name, t in sorted(TEMPLATES.items())]


def get_template(name: str) -> Dict[str, Any]:
    if name not in TEMPLATES:
        raise KeyError("unknown flow template: '%s' (available: %s)"
                       % (name, ", ".join(sorted(TEMPLATES))))
    return TEMPLATES[name]
