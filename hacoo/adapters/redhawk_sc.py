"""RedHawk-SC adapter — IR/EM 签核 (redhawk_sc 持久 Tcl 会话)。

Ansys RedHawk-SC: 设计导入 -> scenario 创建 -> IR/EM 分析 -> 违例提取。
可执行文件: HACOO_REDHAWK_SC_BIN 或 PATH 中的 redhawk_sc。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .base import ToolAdapter, api_method
from .shell import TclShell, resolve_binary


class RedHawkSCAdapter(ToolAdapter):
    tool_name = "redhawk_sc"

    def __init__(self) -> None:
        self._shell: Optional[TclShell] = None
        self.design: Dict[str, Any] = {}
        self.scenarios: List[Dict[str, Any]] = []

    def _rh(self) -> TclShell:
        if self._shell is None:
            exe = resolve_binary("redhawk_sc", ["redhawk_sc"])
            self._shell = TclShell(exe)
        return self._shell

    def __del__(self) -> None:
        if self._shell:
            self._shell.close()

    def _require_design(self) -> None:
        if not self.design:
            raise RuntimeError("no design imported; call api_load_design first")

    # ------------------------------------------------------------ api_*

    @api_method(capability="write", description="Import design (DEF/LEF/lib) into RedHawk-SC.")
    def api_load_design(self, def_file: str, top: str, lef: list, libs: list) -> Dict[str, Any]:
        """import design + 创建 analysis view。"""
        rh = self._rh()
        rh.cmd("set lefs {%s}" % " ".join(lef))
        rh.cmd("set libs {%s}" % " ".join(libs))
        out = rh.cmd("import_design -def {%s} -lef $lefs -lib $libs -top %s"
                     % (def_file, top), timeout=1800)
        if "Error" in out:
            raise RuntimeError("import_design failed:\n%s" % out[-1500:])
        self.design = {"def": def_file, "top": top, "lef": lef, "libs": libs}
        return {"data": {"imported": True, "top": top},
                "text": "design '%s' imported into RedHawk-SC" % top}

    @api_method(capability="write", description="Run IR drop analysis (static|dynamic).")
    def api_run_ir_analysis(self, analysis_type: str = "static") -> Dict[str, Any]:
        """创建并执行 IR scenario。"""
        self._require_design()
        if analysis_type not in ("static", "dynamic"):
            raise ValueError("analysis_type must be static|dynamic")
        rh = self._rh()
        t0 = time.time()
        out = rh.cmd("create_scenario -type %s_ir -name %s_ir_run"
                     % (analysis_type, analysis_type), timeout=14400)
        record = {"kind": "ir_%s" % analysis_type, "elapsed_s": round(time.time() - t0, 1)}
        self.scenarios.append(record)
        return {"data": record, "text": "%s IR scenario done in %.1fs"
                % (analysis_type, record["elapsed_s"]),
                "artifacts": [{"kind": "log_tail", "content_tail": out[-2000:]}]}

    @api_method(capability="write", description="Run EM analysis (signal/PG).")
    def api_run_em_analysis(self, em_type: str = "pg") -> Dict[str, Any]:
        """EM scenario (pg | signal)。"""
        self._require_design()
        if em_type not in ("pg", "signal"):
            raise ValueError("em_type must be pg|signal")
        rh = self._rh()
        t0 = time.time()
        out = rh.cmd("create_scenario -type em_%s -name em_%s_run" % (em_type, em_type),
                     timeout=14400)
        record = {"kind": "em_%s" % em_type, "elapsed_s": round(time.time() - t0, 1)}
        self.scenarios.append(record)
        return {"data": record, "text": "EM (%s) scenario done in %.1fs"
                % (em_type, record["elapsed_s"]),
                "artifacts": [{"kind": "log_tail", "content_tail": out[-2000:]}]}

    @api_method(capability="read", description="Extract worst violations from latest scenario.")
    def api_report_violations(self, limit: int = 50) -> Dict[str, Any]:
        """提取最近 scenario 的违例统计与最差实例。"""
        self._require_design()
        report = self._rh().cmd("report_violations -limit %d" % limit, timeout=1800)
        return {"data": {"scenarios_done": len(self.scenarios),
                         "latest": self.scenarios[-1] if self.scenarios else None},
                "text": report[-3000:]}

    @api_method(capability="read", description="Dump current RedHawk-SC session state.")
    def api_get_design_state(self) -> Dict[str, Any]:
        return {"data": {"design": self.design or None,
                         "scenarios": list(self.scenarios)}}
