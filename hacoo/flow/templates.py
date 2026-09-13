"""Flow 模板 — 预定义设计流程 (flow 编排知识, 类似 ISF flow 定义的本地镜像)。

每个 stage: {name, agent, method, args}。args 值中 "${param}" 会被
flow_start 传入的参数替换。stage 的 agent 被禁用时整个 run 失败并记录
AGENT_DISABLED 决策 — 保证"未参与的 Agent 不会偷偷执行"。
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
