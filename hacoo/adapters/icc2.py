"""IC Compiler II adapter — APR (icc2_shell 持久 Tcl 会话)。

NDM 库模型: create_lib/read_verilog -> place_opt -> clock_opt -> route_opt。
可执行文件: HACOO_ICC2_BIN 或 PATH 中的 icc2_shell。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from .base import ToolAdapter, api_method
from .shell import TclShell, resolve_binary

_STAGES = ("place_opt", "clock_opt", "route_opt")


class ICC2Adapter(ToolAdapter):
    tool_name = "icc2"

    def __init__(self) -> None:
        self._shell: Optional[TclShell] = None
        self.design: Dict[str, Any] = {}
        self.stage_history: List[Dict[str, Any]] = []

    def _icc2(self) -> TclShell:
        if self._shell is None:
            exe = resolve_binary("icc2", ["icc2_shell"])
            self._shell = TclShell(exe)
        return self._shell

    def __del__(self) -> None:
        if self._shell:
            self._shell.close()

    def _require_design(self) -> None:
        if not self.design:
            raise RuntimeError("no design loaded; call api_load_design first")

    # ------------------------------------------------------------ api_*

    @api_method(capability="write", description="Create NDM lib from tech files and read netlist.")
    def api_load_design(self, netlist: str, top: str, lib_name: str,
                        tech_tf: str, ndm_libs: list) -> Dict[str, Any]:
        """create_lib (tech + reference NDM) + read_verilog + link_block。"""
        icc2 = self._icc2()
        icc2.cmd("create_lib %s -technology {%s} -ref_libs {%s}"
                 % (lib_name, tech_tf, " ".join(ndm_libs)), timeout=900)
        icc2.cmd("read_verilog -top %s {%s}" % (top, netlist), timeout=900)
        out = icc2.cmd("link_block", timeout=900)
        if "Error:" in out:
            raise RuntimeError("link_block failed:\n%s" % out[-1500:])
        self.design = {"netlist": netlist, "top": top, "lib_name": lib_name}
        return {"data": {"linked": True, "top": top, "lib": lib_name},
                "text": "design '%s' linked in ICC2 lib '%s'" % (top, lib_name)}

    @api_method(capability="write", description="Run one APR stage: place_opt|clock_opt|route_opt.")
    def api_run_apr(self, stage: str, extra_tcl: str = "") -> Dict[str, Any]:
        """执行单个优化阶段; extra_tcl 可注入定制命令 (如 3D IC bump 规划)。"""
        self._require_design()
        if stage not in _STAGES:
            raise ValueError("stage must be one of %s" % list(_STAGES))
        icc2 = self._icc2()
        t0 = time.time()
        if extra_tcl:
            icc2.cmd(extra_tcl, timeout=1800)
        out = icc2.cmd(stage, timeout=7200)
        if "Error:" in out:
            raise RuntimeError("%s failed:\n%s" % (stage, out[-1500:]))
        record = {"stage": stage, "elapsed_s": round(time.time() - t0, 1)}
        self.stage_history.append(record)
        return {"data": dict(record, stages_done=len(self.stage_history)),
                "text": "%s done in %.1fs" % (stage, record["elapsed_s"])}

    @api_method(capability="read", description="report_qor summary: WNS/TNS/area/DRC count.")
    def api_report_qor(self) -> Dict[str, Any]:
        """QoR 汇总。"""
        self._require_design()
        report = self._icc2().cmd("report_qor -summary", timeout=600)
        wns_m = re.search(r"WNS\s*[:=]?\s*(-?\d+\.?\d*)", report)
        tns_m = re.search(r"TNS\s*[:=]?\s*(-?\d+\.?\d*)", report)
        return {"data": {"wns": float(wns_m.group(1)) if wns_m else None,
                         "tns": float(tns_m.group(1)) if tns_m else None,
                         "stages_done": len(self.stage_history)},
                "text": report[-3000:]}

    @api_method(capability="write", description="Save block (save_block).")
    def api_save_db(self, label: str) -> Dict[str, Any]:
        """save_block -as <label>。"""
        self._require_design()
        self._icc2().cmd("save_block -as %s" % label, timeout=900)
        return {"text": "block saved as '%s'" % label}

    @api_method(capability="read", description="Dump current ICC2 session state.")
    def api_get_design_state(self) -> Dict[str, Any]:
        return {"data": {"design": self.design or None,
                         "stage_history": list(self.stage_history)}}
