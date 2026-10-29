"""RedHawk-SC adapter — IR/EM 签核 (redhawk_sc 持久 Tcl 会话)。

Ansys RedHawk-SC: 设计导入 -> scenario 创建 -> IR/EM 分析 -> 违例提取。
可执行文件: HACOO_REDHAWK_SC_BIN 或 PATH 中的 redhawk_sc;
开发/测试可用 HACOO_REDHAWK_SC_MOCK=1 注入录制型 mock shell (接口行为一致)。

SO-7: api_emir_report_hotspots —— IR drop hotspot Top N 结构化输出
(instance/坐标/层/压降), ir_drop_mv 扁平 key 供观测层趋势检测。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional

from .base import ToolAdapter, api_method
from .shell import MockTclShell, TclShell, resolve_binary

# RedHawk-SC hotspot 报告行: INSTANCE X Y LAYER IR_DROP(mV) (表头含 INSTANCE)
_RH_LINE_RE = re.compile(
    r"^(\S+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(\S+)\s+(-?\d+\.?\d*)\s*$")

# mock 模式的确定性应答, 按 RedHawk-SC 真实输出风格编写,
# 使 parse_ir_hotspots 在 mock 下被严肃测试。
_MOCK_CANNED = {
    "report_ir_drop": (
        "========================================\n"
        "RedHawk-SC IR Drop Hotspot Report\n"
        "Design: chip   Scenario: static_ir_run\n"
        "========================================\n"
        "INSTANCE                 X(um)     Y(um)     LAYER   IR_DROP(mV)\n"
        "u_core/u_alu/mult_u1     120.500   340.250   M5      98.5\n"
        "u_core/u_alu/add_u2      122.100   338.000   M5      87.2\n"
        "u_core/lsu_u3            400.000   512.750   M7      76.8\n"
        "u_mem/ctrl_u4             55.250    60.000   M4      65.1\n"
        "----------------------------------------\n"
        "Worst IR drop: 98.5 mV\n"),
}


def parse_ir_hotspots(report: str, top_n: int = 20) -> Dict[str, Any]:
    """RedHawk-SC IR hotspot 报告解析 (纯函数)。

    返回 {"hotspots": [{"instance","x","y","layer","ir_drop_mv"}],
           "worst_drop_mv": float|None}; 按压降降序, 截断 top_n;
    解析不到时 hotspots 为空 (宽容解析)。
    """
    hotspots: List[Dict[str, Any]] = []
    for line in report.splitlines():
        m = _RH_LINE_RE.match(line.strip())
        if not m or m.group(1).upper() == "INSTANCE":
            continue
        hotspots.append({"instance": m.group(1), "x": float(m.group(2)),
                         "y": float(m.group(3)), "layer": m.group(4),
                         "ir_drop_mv": float(m.group(5))})
    hotspots.sort(key=lambda h: h["ir_drop_mv"], reverse=True)
    hotspots = hotspots[:top_n]
    return {"hotspots": hotspots,
            "worst_drop_mv": hotspots[0]["ir_drop_mv"] if hotspots else None}


class RedHawkSCAdapter(ToolAdapter):
    tool_name = "redhawk_sc"

    def __init__(self) -> None:
        self._shell: Optional[TclShell] = None
        self.design: Dict[str, Any] = {}
        self.scenarios: List[Dict[str, Any]] = []

    def _rh(self) -> TclShell:
        if self._shell is None:
            if os.environ.get("HACOO_REDHAWK_SC_MOCK") == "1":
                self._shell = MockTclShell("redhawk_sc", canned=_MOCK_CANNED)
            else:
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

    @api_method(capability="read", description="Report top-N IR drop hotspots (instance/x/y/layer/mV).")
    def api_emir_report_hotspots(self, top_n: int = 20) -> Dict[str, Any]:
        """SO-7: IR drop hotspot Top N 结构化输出, 按压降降序;
        ir_drop_mv 扁平 key 供观测层趋势检测。"""
        self._require_design()
        if not any(s["kind"].startswith("ir_") for s in self.scenarios):
            raise RuntimeError("no IR analysis done; call api_run_ir_analysis first")
        report = self._rh().cmd("report_ir_drop -max %d" % top_n, timeout=1800)
        parsed = parse_ir_hotspots(report, top_n)
        return {"data": {"top": self.design.get("top"), "top_n": top_n,
                         "ir_drop_mv": parsed["worst_drop_mv"], **parsed},
                "text": report[-4000:],
                "artifacts": [{"kind": "report", "name": "ir_hotspots",
                               "content_tail": report[-2000:]}]}

    @api_method(capability="read", description="Dump current RedHawk-SC session state.")
    def api_get_design_state(self) -> Dict[str, Any]:
        return {"data": {"design": self.design or None,
                         "scenarios": list(self.scenarios)}}
