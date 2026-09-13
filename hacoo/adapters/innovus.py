"""Innovus adapter — APR (innovus -nowin 持久 Tcl 会话)。

阶段化 API: floorplan -> place -> cts -> route -> opt, 每步记录历史,
支持 3D IC 场景的 die 分区参数透传。
可执行文件: HACOO_INNOVUS_BIN 或 PATH 中的 innovus。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from .base import ToolAdapter, api_method
from .shell import TclShell, resolve_binary

_STAGES = ("floorplan", "place", "cts", "route", "opt")


class InnovusAdapter(ToolAdapter):
    tool_name = "innovus"

    def __init__(self) -> None:
        self._shell: Optional[TclShell] = None
        self.design: Dict[str, Any] = {}
        self.stage_history: List[Dict[str, Any]] = []

    def _inv(self) -> TclShell:
        if self._shell is None:
            exe = resolve_binary("innovus", ["innovus"])
            self._shell = TclShell(exe, ["-nowin", "-no_logv"])
        return self._shell

    def __del__(self) -> None:
        if self._shell:
            self._shell.close()

    def _require_design(self) -> None:
        if not self.design:
            raise RuntimeError("no design initialized; call api_load_design first")

    # ------------------------------------------------------------ api_*

    @api_method(capability="write", description="Init design: read LEF/libs/netlist, set top.")
    def api_load_design(self, netlist: str, top: str,
                        lef: list, libs: list) -> Dict[str, Any]:
        """init_design 风格的初始化 (LEF + lib + netlist)。"""
        inv = self._inv()
        inv.cmd("set init_lef_file {%s}" % " ".join(lef))
        inv.cmd("set init_mmmc_file {view_definition.tcl}")
        inv.cmd("set init_verilog {%s}" % netlist)
        inv.cmd("set init_top_cell {%s}" % top)
        out = inv.cmd("init_design", timeout=900)
        if "ERROR" in out or "**ERROR" in out:
            raise RuntimeError("init_design failed:\n%s" % out[-1500:])
        self.design = {"netlist": netlist, "top": top, "lef": lef, "libs": libs}
        return {"data": {"initialized": True, "top": top},
                "text": "design '%s' initialized in Innovus" % top}

    @api_method(capability="write", description="Run one APR stage: floorplan|place|cts|route|opt.")
    def api_run_apr(self, stage: str, extra_tcl: str = "") -> Dict[str, Any]:
        """执行单个 APR 阶段; extra_tcl 可注入 3D IC 分区等定制命令。"""
        self._require_design()
        if stage not in _STAGES:
            raise ValueError("stage must be one of %s" % list(_STAGES))
        inv = self._inv()
        commands = {
            "floorplan": "floorPlan -su 1.0 0.6 4.0 4.0 4.0 4.0",
            "place": "place_design",
            "cts": "ccopt_design",
            "route": "routeDesign",
            "opt": "optDesign -postRoute",
        }
        t0 = time.time()
        if extra_tcl:
            inv.cmd(extra_tcl, timeout=1800)
        out = inv.cmd(commands[stage], timeout=7200)
        if "**ERROR" in out:
            raise RuntimeError("%s failed:\n%s" % (stage, out[-1500:]))
        record = {"stage": stage, "elapsed_s": round(time.time() - t0, 1)}
        self.stage_history.append(record)
        return {"data": dict(record, stages_done=len(self.stage_history)),
                "text": "%s done in %.1fs" % (stage, record["elapsed_s"])}

    @api_method(capability="read", description="Report congestion / timing QoR snapshot.")
    def api_report_qor(self) -> Dict[str, Any]:
        """QoR 快照: setup WNS/TNS + congestion 概要。"""
        self._require_design()
        inv = self._inv()
        timing = inv.cmd("report_timing -machine_readable", timeout=600)
        congestion = inv.cmd("reportCongestion 2>/dev/null || echo NA", timeout=300)
        wns_m = re.search(r"WNS\s*[:=]\s*(-?\d+\.?\d*)", timing)
        tns_m = re.search(r"TNS\s*[:=]\s*(-?\d+\.?\d*)", timing)
        return {"data": {"wns": float(wns_m.group(1)) if wns_m else None,
                         "tns": float(tns_m.group(1)) if tns_m else None,
                         "stages_done": len(self.stage_history)},
                "text": (timing + "\n" + congestion)[-3000:]}

    @api_method(capability="write", description="Save design DB (write_db).")
    def api_save_db(self, path: str) -> Dict[str, Any]:
        """保存 Innovus DB 快照。"""
        self._require_design()
        self._inv().cmd("write_db {%s}" % path, timeout=900)
        return {"text": "db saved: %s" % path}

    @api_method(capability="read", description="Dump current Innovus session state.")
    def api_get_design_state(self) -> Dict[str, Any]:
        return {"data": {"design": self.design or None,
                         "stage_history": list(self.stage_history)}}
