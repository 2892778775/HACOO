"""Flow 引擎 — 模板化流程编排, 每个 stage 由指定 Agent 经网关完整控制路径执行。

- stage 的 agent 被禁用 -> run 立即失败, 并记录 AGENT_DISABLED 决策
  ("未参与本次实验的 Agent 不会被执行")
- 每个 stage 自动写一条决策记录 (accountability): 谁/何时/依据/结果
- run manifest 存 runs/<run_id>.json, 支持 api_flow_status / api_flow_trace
- 长流程可用 metadata.async_=true 异步执行, api_task_status 轮询
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from hacoo.comm.protocol import RPCRequest

from .templates import get_template, list_templates

log = logging.getLogger("hacoo.flow")


def _render(value: Any, params: Dict[str, Any]) -> Any:
    """args 模板替换: "${key}" -> params[key]。"""
    if isinstance(value, str):
        for k, v in params.items():
            value = value.replace("${%s}" % k, str(v))
        return value
    if isinstance(value, dict):
        return {k: _render(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, params) for v in value]
    return value


class FlowEngine:
    def __init__(self, gateway: Any, runs_dir: str = "runs") -> None:
        self.gateway = gateway  # 反向引用: stage 走 gateway.handle 完整控制路径
        self.runs_dir = runs_dir
        os.makedirs(runs_dir, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 执行

    def start(self, template_name: str, params: Dict[str, Any],
              token: str, user: str) -> Dict[str, Any]:
        from hacoo import agents  # 延迟导入避免环
        template = get_template(template_name)
        merged = dict(template.get("params", {}))
        merged.update(params or {})
        run_id = "run-" + uuid.uuid4().hex[:10]
        run = {
            "run_id": run_id, "template": template_name, "params": merged,
            "user": user, "status": "running", "started_at": time.time(),
            "finished_at": None, "stages": [],
            "enabled_agents": agents.load_enabled(self.gateway.config.agents_file),
        }
        self._save(run)
        try:
            self._execute(run, template, merged, token)
        except Exception as exc:  # 兜底: 引擎自身异常也落盘
            log.exception("flow run %s crashed", run_id)
            run["status"] = "failed"
            run["error"] = "ENGINE_ERROR: %s" % exc
        run["finished_at"] = time.time()
        self._save(run)
        return run

    def _execute(self, run: Dict[str, Any], template: Dict[str, Any],
                 params: Dict[str, Any], token: str) -> None:
        from hacoo import agents
        run_id = run["run_id"]
        instance_id: Optional[str] = None
        try:
            # flow 内所有 stage 共享一个后端实例 (跨 stage 状态连续, R6)
            spawn_req = RPCRequest(
                method="api_spawn_instance", arguments={"tool": "mock_eda"},
                metadata={"token": token, "agent": "flow", "run_id": run_id})
            resp = self.gateway.handle(spawn_req)
            if resp.status != "ok":
                raise RuntimeError("spawn failed: %s" % (resp.error or {}).get("message"))
            instance_id = resp.data["instance_id"]

            for stage in template["stages"]:
                entry = {"name": stage["name"], "agent": stage["agent"],
                         "method": stage["method"], "status": "running",
                         "started_at": time.time()}
                run["stages"].append(entry)
                self._save(run)

                # 决策记录: 引擎自动记账 (谁/依据什么/做了什么)
                decision = self.gateway.decisions.record(
                    agent=stage["agent"], run_id=run_id, stage=stage["name"],
                    action=stage["method"],
                    rationale="flow template '%s' stage" % run["template"],
                    params=_render(stage["args"], params))

                # 禁用检查: 被禁 Agent 的 stage 直接失败, 绝不执行
                if not agents.is_enabled(stage["agent"], self.gateway.config.agents_file):
                    entry["status"] = "failed"
                    entry["error"] = "AGENT_DISABLED: agent '%s' is not enabled for this run" % stage["agent"]
                    self.gateway.decisions.outcome(
                        decision["decision_id"], "failed", note=entry["error"])
                    run["status"] = "failed"
                    run["error"] = entry["error"]
                    return

                req = RPCRequest(
                    method=stage["method"],
                    arguments=_render(stage["args"], params),
                    instance_id=instance_id,
                    metadata={"token": token, "agent": stage["agent"], "run_id": run_id})
                resp = self.gateway.handle(req)
                entry["finished_at"] = time.time()
                entry["request_id"] = req.request_id  # 关联审计日志
                if resp.status != "ok":
                    entry["status"] = "failed"
                    entry["error"] = (resp.error or {}).get("message", "unknown")
                    entry["error_code"] = (resp.error or {}).get("code")
                    self.gateway.decisions.outcome(
                        decision["decision_id"], "failed",
                        metrics={"elapsed_ms": resp.elapsed_ms}, note=entry["error"])
                    run["status"] = "failed"
                    run["error"] = "stage '%s' failed: %s" % (stage["name"], entry["error"])
                    return
                entry["status"] = "ok"
                entry["result_text"] = resp.text
                self.gateway.decisions.outcome(
                    decision["decision_id"], "ok",
                    metrics={"elapsed_ms": resp.elapsed_ms},
                    note=resp.text or "")
            run["status"] = "success"
        finally:
            if instance_id:
                rel = RPCRequest(method="api_release_instance",
                                 arguments={"instance_id": instance_id},
                                 metadata={"token": token, "agent": "flow",
                                           "run_id": run_id})
                self.gateway.handle(rel)

    # ------------------------------------------------------------ 查询

    def _path(self, run_id: str) -> str:
        safe = "".join(c for c in run_id if c.isalnum() or c in "-_")
        return os.path.join(self.runs_dir, "%s.json" % safe)

    def _save(self, run: Dict[str, Any]) -> None:
        with self._lock:
            with open(self._path(run["run_id"]), "w", encoding="utf-8") as fh:
                json.dump(run, fh, ensure_ascii=False, indent=2)

    def status(self, run_id: str) -> Dict[str, Any]:
        path = self._path(run_id)
        if not os.path.isfile(path):
            raise KeyError("unknown run_id: '%s'" % run_id)
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def trace(self, run_id: str) -> Dict[str, Any]:
        """问题定位视图: run manifest + 决策时间线 + 关联审计条目。"""
        run = self.status(run_id)
        decisions = self.gateway.decisions.query(run_id=run_id, limit=500)
        audit_entries = self.gateway.audit.query(run_id)
        return {"run": run, "decisions": decisions, "audit": audit_entries}

    def list_templates(self) -> List[Dict[str, Any]]:
        return list_templates()
