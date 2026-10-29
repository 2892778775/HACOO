"""ISF 自动化平台适配器 — CLI 环境形态 (非 HTTP 服务)。

ISF 的实际形态: 每个 tape-out 项目有独立 ISF 环境。设计人员先执行
    isfsetup N3CTV1          # 激活 TO 项目环境 (修改 PATH / 环境变量)
再用 ISF 命令操作, 例如:
    fw start -flow APR+TC+PV -block BLOCK1 /FLOW/PATH/NAME \
        -usertcl user.tcl -data_version 20260920R -design_version 20260711

环境状态驻留在 shell 进程里, 因此本适配器为每个实例维护一个持久 bash
会话 (CmdShell): 实例 = 一个已绑定某 TO 项目的 ISF 环境。一个 HACOO
实例对应一个 TO 项目; 多项目并行时 spawn 多个实例。

无 ISF 环境时 (本地开发机 / CI) 自动回退 mock 模式: 接口行为一致,
状态记录在实例内存, 供流程开发与回归测试使用 (也可用 HACOO_ISF_MOCK=1
强制 mock)。

环境变量:
  HACOO_ISF_SHELL        持久 shell 可执行文件 (默认 bash, 其次 sh)
  HACOO_ISF_MOCK         "1" 强制 mock 模式 (测试用)
  HACOO_ISF_STATUS_CMD   flow 状态查询命令模板, 占位符 {flow_id}/{flow_path}/
                         {block}/{project}; 默认 "fw jobs" (ISF 内置 job 列表)
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import time
import uuid
from typing import Any, Dict, List, Optional

from .base import ToolAdapter, api_method
from .shell import CmdShell


class ISFAdapter(ToolAdapter):
    """ISF CLI 环境适配器: 实例内持久保存项目环境与会话状态 (R6)。"""

    tool_name = "isf"

    def __init__(self) -> None:
        self._shell: Optional[CmdShell] = None
        self._mock: Optional[bool] = None     # None=未探测, True=mock 模式
        self.project: Optional[str] = None    # 已 isfsetup 激活的 TO 项目
        self.flows: Dict[str, Dict[str, Any]] = {}  # flow_id -> 状态

    # ------------------------------------------------------------ 环境管理

    def _ensure_shell(self) -> None:
        """惰性启动持久 shell 并探测 ISF 可用性, 决定真实/mock 模式。"""
        if self._mock is not None:
            return
        if os.environ.get("HACOO_ISF_MOCK") == "1":
            self._mock = True
            return
        shell_bin = os.environ.get("HACOO_ISF_SHELL") or \
            shutil.which("bash") or shutil.which("sh")
        if not shell_bin:
            self._mock = True   # 无 POSIX shell (纯 Windows), 直接 mock
            return
        self._shell = CmdShell(shell_bin)
        try:
            out = self._shell.cmd("command -v isfsetup", timeout=15)
            self._mock = not out.strip()   # 找不到 isfsetup -> mock
        except RuntimeError:
            self._mock = True              # command -v 返回码非 0 -> 未安装
        if self._mock:
            self._shell.close()
            self._shell = None

    def __del__(self) -> None:  # backend 进程退出时兜底
        if self._shell:
            self._shell.close()

    @property
    def mode(self) -> str:
        self._ensure_shell()
        return "mock" if self._mock else "live"

    def _require_setup(self) -> None:
        if not self.project:
            raise RuntimeError("no ISF project activated; call api_isf_setup first")

    # ------------------------------------------------------------ api_*

    @api_method(capability="write",
                description="Activate an ISF tape-out project environment (isfsetup <project>).")
    def api_isf_setup(self, project: str) -> Dict[str, Any]:
        """isfsetup <project>: 激活项目环境。一个实例绑定一个项目; 换项目
        请 release 后重新 spawn (避免环境变量交叉污染)。"""
        self._ensure_shell()
        if self._mock:
            self.project = project
        else:
            out = self._shell.cmd("isfsetup %s" % shlex.quote(project), timeout=120)
            self.project = project
            return {"data": {"project": project, "mode": "live"},
                    "text": "ISF project '%s' activated\n%s" % (project, out[-2000:])}
        return {"data": {"project": project, "mode": "mock"},
                "text": "[mock] ISF project '%s' activated" % project}

    @api_method(capability="write",
                description="Start an ISF flow: fw start -flow <F> -block <B> <path> "
                            "[-usertcl t] [-data_version v] [-design_version v].")
    def api_isf_fw_start(self, flow: str, block: str, flow_path: str,
                         usertcl: str = "", data_version: str = "",
                         design_version: str = "", run_id: str = "",
                         timeout: int = 1800) -> Dict[str, Any]:
        """启动一个 ISF flow。flow 形如 "APR+TC+PV"; data_version 为 APR
        database 版本, design_version 为原始 netlist/sdc 版本。

        X-4 双向引用: run_id 可附 HACOO flow run id (模板里用
        ${run_id} 传入), 记录进 ISF flow record; 反向地, 本方法返回的
        flow_id 会被 flow 引擎收集进 run manifest 的 isf_flow_ids。

        fw start 的实际行为: 启动阶段阻塞终端 (从 ISF central 拉取 flow
        运行所需内容并初始化, 冷项目首次拉取可能需几分钟), 同时向终端
        输出 log; Airflow 接管后 (GUI 弹出或后台启动) 终端释放, flow 转
        由 Airflow 运行, 之后用 api_isf_flow_status (fw jobs) 跟踪。
        启动 log 中的 Airflow GUI URL 会被解析到返回的 airflow_urls。"""
        self._require_setup()
        parts = ["fw", "start", "-flow", flow, "-block", block, flow_path]
        if usertcl:
            parts += ["-usertcl", usertcl]
        if data_version:
            parts += ["-data_version", data_version]
        if design_version:
            parts += ["-design_version", design_version]
        command = " ".join(shlex.quote(p) for p in parts)
        flow_id = "isf-" + uuid.uuid4().hex[:10]
        record = {
            "flow_id": flow_id, "project": self.project, "flow": flow,
            "block": block, "flow_path": flow_path, "usertcl": usertcl,
            "data_version": data_version, "design_version": design_version,
            "run_id": run_id or None,  # X-4: ISF flow -> HACOO run 反向引用
            "command": command, "status": "running", "submitted_at": time.time(),
        }
        self.flows[flow_id] = record
        if self._mock:
            text = "[mock] %s" % command
        else:
            text = self._shell.cmd(command, timeout=float(timeout))
        urls = re.findall(r"https?://[^\s'\"<>]+", text or "")
        record["airflow_urls"] = urls
        return {"data": dict(record),
                "text": "flow %s started: %s\n%s" % (flow_id, command, (text or "")[-2000:])}

    @api_method(capability="read",
                description="Query status of a flow started in this ISF environment.")
    def api_isf_flow_status(self, flow_id: str) -> Dict[str, Any]:
        """查询 flow 状态。真实模式默认执行 `fw jobs` 并筛出与本 flow
        (flow_path / block) 相关的行; 也可用 HACOO_ISF_STATUS_CMD 覆盖为
        项目定制的查询命令模板 (占位符 {flow_id}/{flow_path}/{block}/{project})。
        mock 模式提交 2s 后视为 success。"""
        self._require_setup()
        flow = self.flows.get(flow_id)
        if flow is None:
            raise RuntimeError("unknown flow_id: %s (flows: %s)"
                               % (flow_id, ", ".join(self.flows) or "none"))
        if self._mock:
            if flow["status"] == "running" and time.time() - flow["submitted_at"] > 2.0:
                flow["status"] = "success"
            return {"data": dict(flow)}
        template = os.environ.get("HACOO_ISF_STATUS_CMD", "fw jobs")
        out = self._shell.cmd(template.format(**flow), timeout=120)
        # fw jobs 列出项目全部 job; 筛出与本 flow 相关的行方便阅读
        keys = [k for k in (flow.get("flow_path"), flow.get("block")) if k]
        matched = [ln for ln in out.splitlines()
                   if any(k in ln for k in keys)] if keys else []
        return {"data": {**flow, "matched_jobs": matched, "raw_status": out[-2000:]},
                "text": ("\n".join(matched) if matched else out)[-4000:]}

    @api_method(capability="write",
                description="Run any ISF command in the activated project environment.")
    def api_isf_run(self, command: str, timeout: int = 300) -> Dict[str, Any]:
        """泛命令通道: 在已激活的 ISF 环境里执行任意 ISF 命令 (fw list /
        fw stop / 数据版本查询 / CICD 触发等)。用于尚未封装为专用 api_*
        方法的 ISF 能力; 稳定后应沉淀为专用方法。"""
        self._require_setup()
        if self._mock:
            return {"data": {"command": command, "mode": "mock"},
                    "text": "[mock] executed in project %s: %s" % (self.project, command)}
        out = self._shell.cmd(command, timeout=float(timeout))
        return {"data": {"command": command, "mode": "live"},
                "text": out[-4000:]}

    @api_method(capability="read",
                description="List flows started in this ISF instance.")
    def api_isf_flows(self) -> Dict[str, Any]:
        """列出本实例 (本 TO 项目环境) 中启动过的 flow 及其状态。"""
        flows: List[Dict[str, Any]] = [dict(f) for f in self.flows.values()]
        return {"data": {"project": self.project, "mode": self.mode,
                         "flows": flows, "total": len(flows)}}
