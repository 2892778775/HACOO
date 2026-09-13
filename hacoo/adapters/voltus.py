"""Voltus adapter — IR/EM + thermal 签核 (voltus -nowin 持久 Tcl 会话)。

static/dynamic IR drop、EM 分析、热分析; 违例解析自 report_rail 输出。
可执行文件: HACOO_VOLTUS_BIN 或 PATH 中的 voltus。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from .base import ToolAdapter, api_method
from .shell import TclShell, resolve_binary

_VIOL_RE = re.compile(r"(\d+)\s+(?:violations?|Violations?)")


class VoltusAdapter(ToolAdapter):
    tool_name = "voltus"

    def __init__(self) -> None:
        self._shell: Optional[TclShell] = None
        self.design: Dict[str, Any] = {}
        self.analyses: List[Dict[str, Any]] = []

    def _voltus(self) -> TclShell:
        if self._shell is None:
            exe = resolve_binary("voltus", ["voltus"])
            self._shell = TclShell(exe, ["-nowin"])
        return self._shell

    def __del__(self) -> None:
        if self._shell:
            self._shell.close()

    def _require_design(self) -> None:
        if not self.design:
            raise RuntimeError("no design loaded; call api_load_design first")

    # ------------------------------------------------------------ api_*

    @api_method(capability="write", description="Load design + power intent for rail analysis.")
    def api_load_design(self, netlist: str, top: str, lef: list,
                        libs: list, upf: str = "") -> Dict[str, Any]:
        """read_netlist + PG 网络定义 (UPF 可选)。"""
        v = self._voltus()
        v.cmd("set init_lef_file {%s}" % " ".join(lef))
        v.cmd("read_netlist {%s} -top %s" % (netlist, top), timeout=900)
        if upf:
            v.cmd("read_power_intent -1801 {%s}" % upf, timeout=300)
        v.cmd("commit_power_intent" if upf else "globalNetConnect VDD -type pgpin -pin VDD -all",
              timeout=300)
        self.design = {"netlist": netlist, "top": top, "lef": lef, "libs": libs, "upf": upf}
        return {"data": {"loaded": True, "top": top},
                "text": "design '%s' loaded in Voltus" % top}

    @api_method(capability="write", description="Run rail analysis: static|dynamic IR drop.")
    def api_run_ir_analysis(self, analysis_type: str = "static") -> Dict[str, Any]:
        """set_rail_analysis_mode + analyze_rail (static/dynamic)。"""
        self._require_design()
        if analysis_type not in ("static", "dynamic"):
            raise ValueError("analysis_type must be static|dynamic")
        v = self._voltus()
        t0 = time.time()
        v.cmd("set_rail_analysis_mode -method %s -accuracy xd" % analysis_type)
        out = v.cmd("analyze_rail -type net VDD", timeout=14400)
        record = {"kind": "ir_%s" % analysis_type, "elapsed_s": round(time.time() - t0, 1)}
        self.analyses.append(record)
        return {"data": record, "text": "%s IR analysis done in %.1fs"
                % (analysis_type, record["elapsed_s"]),
                "artifacts": [{"kind": "log_tail", "content_tail": out[-2000:]}]}

    @api_method(capability="write", description="Run EM (electromigration) analysis.")
    def api_run_em_analysis(self) -> Dict[str, Any]:
        """analyze_rail -type em。"""
        self._require_design()
        v = self._voltus()
        t0 = time.time()
        out = v.cmd("analyze_rail -type em", timeout=14400)
        record = {"kind": "em", "elapsed_s": round(time.time() - t0, 1)}
        self.analyses.append(record)
        return {"data": record, "text": "EM analysis done in %.1fs" % record["elapsed_s"],
                "artifacts": [{"kind": "log_tail", "content_tail": out[-2000:]}]}

    @api_method(capability="write", description="Run thermal analysis (die/package model).")
    def api_run_thermal_analysis(self, power_map: str = "") -> Dict[str, Any]:
        """热分析; power_map 可指定功耗图文件 (3D 堆叠逐层)。"""
        self._require_design()
        v = self._voltus()
        t0 = time.time()
        if power_map:
            v.cmd("read_power_map {%s}" % power_map, timeout=600)
        out = v.cmd("analyze_thermal" if True else "", timeout=7200)
        record = {"kind": "thermal", "elapsed_s": round(time.time() - t0, 1),
                  "power_map": power_map or None}
        self.analyses.append(record)
        return {"data": record, "text": "thermal analysis done in %.1fs" % record["elapsed_s"],
                "artifacts": [{"kind": "log_tail", "content_tail": out[-2000:]}]}

    @api_method(capability="read", description="Report rail violations (IR/EM) with counts.")
    def api_report_violations(self, limit: int = 50) -> Dict[str, Any]:
        """report_rail 违例汇总: 数量 + 最差值。"""
        self._require_design()
        report = self._voltus().cmd("report_rail -output rail_report", timeout=1800)
        counts = [int(n) for n in _VIOL_RE.findall(report)]
        return {"data": {"violation_groups": len(counts),
                         "total_violations": sum(counts),
                         "analyses_done": len(self.analyses)},
                "text": report[-3000:]}

    @api_method(capability="read", description="Dump current Voltus session state.")
    def api_get_design_state(self) -> Dict[str, Any]:
        return {"data": {"design": self.design or None,
                         "analyses": list(self.analyses)}}
