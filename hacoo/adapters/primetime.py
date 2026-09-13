"""PrimeTime adapter — STA 签核 (pt_shell 持久会话)。

api_* 方法封装 pt_shell Tcl 命令语义; 报告输出解析为结构化数据
(WNS/TNS/路径), 原始文本同时作为 text output 返回 (T4)。
可执行文件: HACOO_PRIMETIME_BIN 或 PATH 中的 pt_shell。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .base import ToolAdapter, api_method
from .shell import TclShell, resolve_binary

_SLACK_RE = re.compile(r"slack\s*\((?:VIOLATED|MET)\)\s*(-?\d+\.?\d*)")


class PrimeTimeAdapter(ToolAdapter):
    tool_name = "primetime"

    def __init__(self) -> None:
        self._shell: Optional[TclShell] = None
        self.design: Dict[str, Any] = {}
        self.libs: List[str] = []
        self.linked = False

    # ------------------------------------------------------------ shell 管理

    def _pt(self) -> TclShell:
        if self._shell is None:
            exe = resolve_binary("primetime", ["pt_shell"])
            self._shell = TclShell(exe)
        return self._shell

    def __del__(self) -> None:  # backend 进程退出时兜底
        if self._shell:
            self._shell.close()

    def _require_linked(self) -> None:
        if not self.linked:
            raise RuntimeError("no design linked; call api_load_design first")

    # ------------------------------------------------------------ api_*

    @api_method(capability="write", description="Read design netlist and link in PrimeTime.")
    def api_load_design(self, path: str, top: str) -> Dict[str, Any]:
        """read_verilog + current_design + link_design。"""
        pt = self._pt()
        pt.cmd("read_verilog {%s}" % path)
        pt.cmd("current_design %s" % top)
        out = pt.cmd("link_design %s" % top)
        if "Error" in out:
            raise RuntimeError("link_design failed:\n%s" % out[-1500:])
        self.design = {"path": path, "top": top}
        self.linked = True
        return {"data": {"linked": True, "top": top},
                "text": "design '%s' linked in PrimeTime" % top}

    @api_method(capability="write", description="Set link_library/search_path from .db files.")
    def api_read_liberty(self, path: str) -> Dict[str, Any]:
        """把 .db 加入 link_library (需在 link 前调用, 否则触发重新 link)。"""
        pt = self._pt()
        self.libs.append(path)
        lib_expr = " ".join(["*"] + self.libs)
        pt.cmd('set_app_var link_library "%s"' % lib_expr)
        pt.cmd('set_app_var search_path ". ./libs"')
        if self.linked:  # 库变化后重新 link + update
            pt.cmd("link_design %s" % self.design["top"])
            pt.cmd("update_timing -full")
        return {"text": "link_library now: %s" % lib_expr}

    @api_method(capability="write", description="Read SDC constraints.")
    def api_read_sdc(self, path: str) -> Dict[str, Any]:
        """read_sdc 约束文件。"""
        self._require_linked()
        out = self._pt().cmd("read_sdc {%s}" % path)
        if "Error" in out:
            raise RuntimeError("read_sdc failed:\n%s" % out[-1500:])
        return {"text": "sdc loaded: %s" % path}

    @api_method(capability="read", description="update_timing + report_timing, returns WNS/TNS and paths.")
    def api_report_timing(self, max_paths: int = 20, path_type: str = "max") -> Dict[str, Any]:
        """时序报告: 解析 WNS/TNS 与关键路径 slack, 原始报告随 text 返回。"""
        self._require_linked()
        pt = self._pt()
        pt.cmd("update_timing -full")
        report = pt.cmd("report_timing -max_paths %d -delay_type %s -nosplit"
                        % (max_paths, path_type), timeout=600)
        slacks = [float(v) for v in _SLACK_RE.findall(report)]
        wns = min(slacks) if slacks else None
        tns = round(sum(s for s in slacks if s < 0), 4) if slacks else 0.0
        return {"data": {"top": self.design.get("top"), "path_type": path_type,
                         "wns": wns, "tns": tns, "paths_analyzed": len(slacks),
                         "slacks": slacks[:max_paths]},
                "text": report[-4000:],
                "artifacts": [{"kind": "report", "name": "report_timing",
                               "content_tail": report[-2000:]}]}

    @api_method(capability="read", description="report_constraint -all_violators summary.")
    def api_report_constraint(self) -> Dict[str, Any]:
        """约束违例汇总 (max_transition/max_capacitance/min_pulse_width 等)。"""
        self._require_linked()
        report = self._pt().cmd("report_constraint -all_violators -nosplit", timeout=600)
        violators = len(re.findall(r"^\s{2}\S", report, flags=re.M))
        return {"data": {"violator_lines": violators}, "text": report[-4000:]}

    @api_method(capability="read", description="Dump current PrimeTime session state.")
    def api_get_design_state(self) -> Dict[str, Any]:
        return {"data": {"design": self.design or None, "libs": list(self.libs),
                         "linked": self.linked}}
