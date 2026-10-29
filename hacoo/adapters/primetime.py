"""PrimeTime adapter — STA 签核 (pt_shell 持久会话)。

api_* 方法封装 pt_shell Tcl 命令语义; 报告输出解析为结构化数据
(WNS/TNS/路径), 原始文本同时作为 text output 返回 (T4)。
可执行文件: HACOO_PRIMETIME_BIN 或 PATH 中的 pt_shell;
开发/测试可用 HACOO_PRIMETIME_MOCK=1 注入录制型 mock shell (接口行为一致)。

SO-1: MCMM (multi-corner multi-mode) 场景管理 —— api_mcmm_create_scenario /
api_mcmm_report_all; 跨场景报告解析为 per-scenario WNS/TNS + 全局最差值。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from .base import ToolAdapter, api_method
from .shell import MockTclShell, TclShell, resolve_binary

_SLACK_RE = re.compile(r"slack\s*\((?:VIOLATED|MET)\)\s*(-?\d+\.?\d*)")
_SCENARIO_RE = re.compile(r"Scenario\s+'([^']+)'")

# mock 模式的确定性应答: 4 场景 (func/test x ss/ff) 跨场景时序报告,
# 按 PT 真实输出风格编写, 使 parse_mcmm_report 在 mock 下被严肃测试。
# SO-3: report_timing -max_paths 为 SI-aware 报告 (含 delta delay/noise
# 行), 供 si_enabled=True 路径回归。
_MOCK_CANNED = {
    "report_timing -scenarios": (
        "****************************************\n"
        "Report : timing -scenarios {func.ss_0v99_125c func.ff_1v10_m40c "
        "test.ss_0v99_125c test.ff_1v10_m40c}\n"
        "Design : chip\n"
        "****************************************\n"
        "Scenario 'func.ss_0v99_125c'\n"
        "  slack (VIOLATED)  -0.152\n"
        "  slack (VIOLATED)  -0.080\n"
        "  slack (MET)        0.012\n"
        "Scenario 'func.ff_1v10_m40c'\n"
        "  slack (MET)        0.031\n"
        "  slack (MET)        0.064\n"
        "Scenario 'test.ss_0v99_125c'\n"
        "  slack (VIOLATED)  -0.210\n"
        "  slack (MET)        0.005\n"
        "Scenario 'test.ff_1v10_m40c'\n"
        "  slack (MET)        0.045\n"
    ),
    # SO-3: SI-aware 报告 (含 crosstalk delta delay / noise 分量行);
    # 解析必须兼容这些额外行且 slack 提取不受影响。
    "report_timing -max_paths": (
        "****************************************\n"
        "Report : timing -max_paths 5 -delay_type max (SI-aware)\n"
        "Design : chip\n"
        "****************************************\n"
        "  Startpoint: u_reg_0 (rising edge-triggered)\n"
        "  data arrival delta delay (SI)            0.018\n"
        "  crosstalk noise bump at u_net_42 (V)     0.042\n"
        "  slack (VIOLATED)  -0.143\n"
        "  Startpoint: u_reg_1 (rising edge-triggered)\n"
        "  data arrival delta delay (SI)            0.011\n"
        "  slack (VIOLATED)  -0.071\n"
        "  Startpoint: u_reg_2 (rising edge-triggered)\n"
        "  slack (MET)        0.022\n"
    ),
}


def parse_mcmm_report(report: str) -> Dict[str, Any]:
    """report_timing -scenarios 输出解析 (纯函数)。

    按 Scenario '<name>' 块切分, 块内收集 slack 值算 WNS/TNS。
    返回 {"scenarios": {name: {"wns","tns","paths"}},
           "global_wns": float|None, "worst_scenario": str|None};
    解析不到场景块时 scenarios 为空、指标为 None (宽容解析)。
    """
    blocks: Dict[str, List[float]] = {}
    current: Optional[str] = None
    for line in report.splitlines():
        m = _SCENARIO_RE.search(line)
        if m:
            current = m.group(1)
            blocks.setdefault(current, [])
            continue
        if current is not None:
            blocks[current].extend(float(v) for v in _SLACK_RE.findall(line))
    scenarios: Dict[str, Dict[str, Any]] = {}
    for name, slacks in blocks.items():
        scenarios[name] = {
            "wns": min(slacks) if slacks else None,
            "tns": round(sum(s for s in slacks if s < 0), 4) if slacks else 0.0,
            "paths": len(slacks),
        }
    global_wns: Optional[float] = None
    worst: Optional[str] = None
    for name, s in scenarios.items():
        if s["wns"] is not None and (global_wns is None or s["wns"] < global_wns):
            global_wns = s["wns"]
            worst = name
    return {"scenarios": scenarios, "global_wns": global_wns,
            "worst_scenario": worst}



class PrimeTimeAdapter(ToolAdapter):
    tool_name = "primetime"

    def __init__(self) -> None:
        self._shell: Optional[TclShell] = None
        self.design: Dict[str, Any] = {}
        self.libs: List[str] = []
        self.linked = False
        self.scenarios: List[Dict[str, Any]] = []  # SO-1: MCMM 场景定义 (R6)

    # ------------------------------------------------------------ shell 管理

    def _pt(self) -> TclShell:
        if self._shell is None:
            if os.environ.get("HACOO_PRIMETIME_MOCK") == "1":
                self._shell = MockTclShell("primetime", canned=_MOCK_CANNED)
            else:
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

    @api_method(capability="read", description="update_timing + report_timing, returns WNS/TNS and paths; si_enabled adds crosstalk delta/noise analysis.")
    def api_report_timing(self, max_paths: int = 20, path_type: str = "max",
                          si_enabled: bool = False) -> Dict[str, Any]:
        """时序报告: 解析 WNS/TNS 与关键路径 slack, 原始报告随 text 返回。
        SO-3: si_enabled=True 时先 set_si_options 开启 crosstalk
        delta delay / noise 分析 (签核默认 SI-aware), 报告含 delta 分量行,
        slack 提取兼容 (解析忽略 delta/noise 行, 数值不受影响)。"""
        self._require_linked()
        pt = self._pt()
        if si_enabled:
            pt.cmd("set_si_options -delta_delay true -noise true")
        pt.cmd("update_timing -full")
        report = pt.cmd("report_timing -max_paths %d -delay_type %s -nosplit"
                        % (max_paths, path_type), timeout=600)
        slacks = [float(v) for v in _SLACK_RE.findall(report)]
        wns = min(slacks) if slacks else None
        tns = round(sum(s for s in slacks if s < 0), 4) if slacks else 0.0
        return {"data": {"top": self.design.get("top"), "path_type": path_type,
                         "si_enabled": bool(si_enabled),
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

    # ------------------------------------------------------------ SO-1: MCMM

    @api_method(capability="write",
                description="Create an MCMM scenario (mode x corner) with per-scenario libs/SDC.")
    def api_mcmm_create_scenario(self, name: str, mode: str, corner: str,
                                 lib_paths: list, sdc: str = "") -> Dict[str, Any]:
        """create_scenario -name/-mode/-corner; 场景内设置 link_library 并按需
        读入 SDC, 最后置 active。场景定义记入实例状态 (R6), 重名拒绝。"""
        self._require_linked()
        if any(s["name"] == name for s in self.scenarios):
            raise RuntimeError("scenario already exists: %s" % name)
        pt = self._pt()
        pt.cmd("create_scenario -name %s -mode %s -corner %s" % (name, mode, corner))
        pt.cmd("current_scenario %s" % name)
        if lib_paths:
            lib_expr = " ".join(["*"] + list(lib_paths))
            pt.cmd('set_app_var link_library "%s"' % lib_expr)
        if sdc:
            out = pt.cmd("read_sdc {%s}" % sdc)
            if "Error" in out:
                raise RuntimeError("read_sdc failed in scenario %s:\n%s"
                                   % (name, out[-1500:]))
        pt.cmd("set_scenario_status %s -active true" % name)
        self.scenarios.append({"name": name, "mode": mode, "corner": corner,
                               "lib_paths": list(lib_paths), "sdc": sdc})
        return {"data": {"scenario": name, "mode": mode, "corner": corner,
                         "total_scenarios": len(self.scenarios)},
                "text": "scenario '%s' (%s/%s) created and active" % (name, mode, corner)}

    @api_method(capability="read",
                description="Report timing across all scenarios: per-scenario + global WNS/TNS.")
    def api_mcmm_report_all(self, max_paths: int = 5) -> Dict[str, Any]:
        """跨场景时序报告: report_timing -scenarios, 解析每场景 WNS/TNS、
        全局最差值与最差场景名; 扁平 wns key 供观测层趋势检测。"""
        self._require_linked()
        if not self.scenarios:
            raise RuntimeError("no MCMM scenarios; call api_mcmm_create_scenario first")
        pt = self._pt()
        names = " ".join(s["name"] for s in self.scenarios)
        pt.cmd("update_timing -full")
        report = pt.cmd("report_timing -scenarios {%s} -max_paths %d -nosplit"
                        % (names, max_paths), timeout=900)
        parsed = parse_mcmm_report(report)
        return {"data": {"top": self.design.get("top"), "max_paths": max_paths,
                         "wns": parsed["global_wns"], **parsed},
                "text": report[-4000:],
                "artifacts": [{"kind": "report", "name": "mcmm_report_timing",
                               "content_tail": report[-2000:]}]}

    @api_method(capability="read", description="Dump current PrimeTime session state.")
    def api_get_design_state(self) -> Dict[str, Any]:
        return {"data": {"design": self.design or None, "libs": list(self.libs),
                         "linked": self.linked,
                         "scenarios": list(self.scenarios)}}
