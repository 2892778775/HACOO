"""Voltus adapter — IR/EM + thermal 签核 (voltus -nowin 持久 Tcl 会话)。

static/dynamic IR drop、EM 分析、热分析; 违例解析自 report_rail 输出。
可执行文件: HACOO_VOLTUS_BIN 或 PATH 中的 voltus;
开发/测试可用 HACOO_VOLTUS_MOCK=1 注入录制型 mock shell (接口行为一致)。

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

_VIOL_RE = re.compile(r"(\d+)\s+(?:violations?|Violations?)")

# Voltus hotspot 报告行: rank instance x y layer ir_drop(mV) (# 开头为注释)
_VOLTUS_LINE_RE = re.compile(
    r"^(\d+)\s+(\S+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(\S+)\s+(-?\d+\.?\d*)\s*$")

# mock 模式的确定性应答, 按 Voltus 真实输出风格编写,
# 使 parse_ir_hotspots 在 mock 下被严肃测试。
_MOCK_CANNED = {
    "report_rail -type ir -hotspots": (
        "# Voltus Rail Analysis - IR Drop Hotspots\n"
        "# design: chip   net: VDD\n"
        "#rank  instance              x        y        layer  ir_drop(mV)\n"
        "1      u_core/u_alu/mult_u1  120.500  340.250  met5   98.5\n"
        "2      u_core/lsu_u3         400.000  512.750  met7   76.8\n"
        "3      u_mem/ctrl_u4          55.250   60.000  met4   65.1\n"),
}


def parse_ir_hotspots(report: str, top_n: int = 20) -> Dict[str, Any]:
    """Voltus IR hotspot 报告解析 (纯函数)。

    返回 {"hotspots": [{"instance","x","y","layer","ir_drop_mv"}],
           "worst_drop_mv": float|None}; 按压降降序, 截断 top_n;
    解析不到时 hotspots 为空 (宽容解析)。
    """
    hotspots: List[Dict[str, Any]] = []
    for line in report.splitlines():
        m = _VOLTUS_LINE_RE.match(line.strip())
        if not m:
            continue
        hotspots.append({"instance": m.group(2), "x": float(m.group(3)),
                         "y": float(m.group(4)), "layer": m.group(5),
                         "ir_drop_mv": float(m.group(6))})
    hotspots.sort(key=lambda h: h["ir_drop_mv"], reverse=True)
    hotspots = hotspots[:top_n]
    return {"hotspots": hotspots,
            "worst_drop_mv": hotspots[0]["ir_drop_mv"] if hotspots else None}


class VoltusAdapter(ToolAdapter):
    tool_name = "voltus"

    def __init__(self) -> None:
        self._shell: Optional[TclShell] = None
        self.design: Dict[str, Any] = {}
        self.analyses: List[Dict[str, Any]] = []

    def _voltus(self) -> TclShell:
        if self._shell is None:
            if os.environ.get("HACOO_VOLTUS_MOCK") == "1":
                self._shell = MockTclShell("voltus", canned=_MOCK_CANNED)
            else:
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

    @api_method(capability="read", description="Report top-N IR drop hotspots (instance/x/y/layer/mV).")
    def api_emir_report_hotspots(self, top_n: int = 20) -> Dict[str, Any]:
        """SO-7: IR drop hotspot Top N 结构化输出, 按压降降序;
        ir_drop_mv 扁平 key 供观测层趋势检测。"""
        self._require_design()
        if not any(a["kind"].startswith("ir_") for a in self.analyses):
            raise RuntimeError("no IR analysis done; call api_run_ir_analysis first")
        report = self._voltus().cmd("report_rail -type ir -hotspots %d" % top_n,
                                    timeout=1800)
        parsed = parse_ir_hotspots(report, top_n)
        return {"data": {"top": self.design.get("top"), "top_n": top_n,
                         "ir_drop_mv": parsed["worst_drop_mv"], **parsed},
                "text": report[-4000:],
                "artifacts": [{"kind": "report", "name": "ir_hotspots",
                               "content_tail": report[-2000:]}]}

    @api_method(capability="read", description="Dump current Voltus session state.")
    def api_get_design_state(self) -> Dict[str, Any]:
        return {"data": {"design": self.design or None,
                         "analyses": list(self.analyses)}}
