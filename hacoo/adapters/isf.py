"""ISF 自动化平台适配器 (mock 实现, API 形态对齐真实 ISF)。

真实部署时把方法体替换为公司 ISF 的 REST/RPC 调用即可 —— 签名与返回
结构保持稳定, 上层 Agent 无感知 (T2: 语义差异封装在 api_* 方法之后)。

覆盖 ISF 职责: 前后端 EDA 工具/流程串接、设计 DB 管理、CICD、
Airflow 架构的 flow 监视。
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List

from .base import ToolAdapter, api_method


class ISFAdapter(ToolAdapter):
    """ISF 平台 mock: 实例内持久保存 DB / flow / pipeline 状态 (R6)。"""

    tool_name = "isf"

    def __init__(self) -> None:
        self.db: Dict[str, Dict[str, Any]] = {}        # 设计 DB: key -> {data, version, ts}
        self.flows: Dict[str, Dict[str, Any]] = {}     # flow_id -> 状态
        self.pipelines: List[Dict[str, Any]] = []      # CICD 触发历史

    # ------------------------------------------------------------ flow 串接

    @api_method(capability="write",
                description="Submit a flow to ISF (chains EDA stages into one orchestrated run).")
    def api_isf_submit_flow(self, flow_name: str, stages: list,
                            description: str = "") -> Dict[str, Any]:
        """提交 flow 到 ISF 编排 (mock: 登记并置为 running)。"""
        flow_id = "isf-" + uuid.uuid4().hex[:10]
        self.flows[flow_id] = {
            "flow_id": flow_id, "flow_name": flow_name, "stages": stages,
            "description": description, "status": "running",
            "submitted_at": time.time(), "dag_id": "hacoo_%s" % flow_name,
        }
        return {"data": self.flows[flow_id],
                "text": "flow '%s' submitted to ISF as %s (airflow dag: hacoo_%s)"
                        % (flow_name, flow_id, flow_name)}

    @api_method(capability="read",
                description="Query ISF flow status (Airflow DagRun state).")
    def api_isf_flow_status(self, flow_id: str) -> Dict[str, Any]:
        """查询 flow/DagRun 状态 (mock: 提交 2s 后视为 success)。"""
        flow = self.flows.get(flow_id)
        if flow is None:
            raise RuntimeError("unknown flow_id: %s" % flow_id)
        if flow["status"] == "running" and time.time() - flow["submitted_at"] > 2.0:
            flow["status"] = "success"
        return {"data": dict(flow)}

    @api_method(capability="read",
                description="List Airflow DAGs monitored by ISF.")
    def api_isf_airflow_dags(self) -> Dict[str, Any]:
        """列出 Airflow 监视的 DAG 及其最近状态 (mock)。"""
        dags = [{"dag_id": f["dag_id"], "latest_state": f["status"],
                 "flow_id": fid} for fid, f in self.flows.items()]
        return {"data": {"dags": dags, "total": len(dags)}}

    # ------------------------------------------------------------ 设计 DB

    @api_method(capability="write",
                description="Save a design DB snapshot (versioned key-value).")
    def api_isf_db_save(self, key: str, data: dict) -> Dict[str, Any]:
        """保存设计 DB 快照, 自动版本递增。"""
        version = self.db.get(key, {}).get("version", 0) + 1
        self.db[key] = {"data": data, "version": version, "ts": time.time()}
        return {"data": {"key": key, "version": version},
                "text": "db[%s] saved at version %d" % (key, version)}

    @api_method(capability="read",
                description="Query a design DB snapshot by key.")
    def api_isf_db_query(self, key: str) -> Dict[str, Any]:
        """读取设计 DB 快照。"""
        entry = self.db.get(key)
        if entry is None:
            raise RuntimeError("no db entry for key: %s" % key)
        return {"data": {"key": key, **entry}}

    # ------------------------------------------------------------ CICD

    @api_method(capability="write",
                description="Trigger a CICD regression pipeline.")
    def api_isf_cicd_trigger(self, pipeline: str, branch: str = "main") -> Dict[str, Any]:
        """触发 CICD 回归 (mock: 记录并返回 queued)。"""
        run = {"pipeline": pipeline, "branch": branch,
               "run_no": len(self.pipelines) + 1, "status": "queued", "ts": time.time()}
        self.pipelines.append(run)
        return {"data": dict(run),
                "text": "pipeline '%s' run #%d queued on %s"
                        % (pipeline, run["run_no"], branch)}
