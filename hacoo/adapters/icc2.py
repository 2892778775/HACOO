"""IC Compiler II adapter — APR (icc2_shell 持久 Tcl 会话)。

NDM 库模型: create_lib/read_verilog -> place_opt -> clock_opt -> route_opt。
可执行文件: HACOO_ICC2_BIN 或 PATH 中的 icc2_shell;
binary 缺失时报错并给出 HACOO_ICC2_BIN 配置指引。
开发/测试可用 HACOO_ICC2_MOCK=1 注入录制型 mock shell (接口行为一致)。

子阶段方法 (SPEC-evolution C.3 BE-1, T2 原则): api_fp_create /
api_fp_place_macros / api_place_opt / api_cts_build / api_cts_report /
api_route_opt —— 与 Innovus adapter 签名一致, 命令差异封装在本文件内。

BE-2: 各子阶段执行后追加报告命令, 由模块级纯函数 (parse_*_report)
将报告文本解析为结构化 metrics 并入返回 data, 经网关观测钩子进观测层。

BE-2 增强: 新增独立报告方法 api_report_congestion / api_route_drc_report,
api_cts_report 扩展 clock_power; 指标 key 与 Innovus 归一
(hotspot_count / overflow_max / skew / latency_max / clock_power / drc_count,
top_rules 为结构化列表不进 metrics)。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional

from .base import ToolAdapter, api_method
from .shell import MockTclShell, TclShell, resolve_binary

_STAGES = ("place_opt", "clock_opt", "route_opt")

# mock 模式的确定性应答 (按命令子串匹配), 供报告解析路径回归;
# 报告文本按 ICC2 真实输出风格编写, 使解析器在 mock 下被严肃测试。
# BE-3/BE-4: utilization/congestion/drc 三个报告改为序列应答 (MockTclShell
# 按命中次数依次返回, 耗尽后钳制末值), 建模"多方案试跑指标各异"与
# "迭代修复后 DRC 逐轮下降"; 首元素与历史静态值一致, 单次调用行为不变。
_MOCK_CANNED = {
    "report_clock_qor": ("Clock Tree QoR Summary (mock)\n"
                         "  clock 'clk': global skew = 0.042, "
                         "max insertion delay (latency) = 0.352, "
                         "min insertion delay = 0.118\n"
                         "  clock tree power (clk): 12.450 mW\n"),
    "report_qor": "WNS: 0.012\nTNS: 0.000\nArea: 12345.6\n",
    # BE-3 fp-exploration: 三个方案 (util 0.70/0.80/0.60) 依次试跑
    "report_utilization": [
        ("****************************************\n"
         "Report : utilization\n"
         "Design : chip\n"
         "Version: T-2022.03\n"
         "****************************************\n"
         "Core Area            : 12388.000\n"
         "Std Cell Area        :  8041.600\n"
         "Macro Area           :   630.000\n"
         "Utilization Ratio    : 0.700\n"),
        ("****************************************\n"
         "Report : utilization\n"
         "Design : chip\n"
         "Version: T-2022.03\n"
         "****************************************\n"
         "Core Area            : 10840.000\n"
         "Std Cell Area        :  8041.600\n"
         "Macro Area           :   630.000\n"
         "Utilization Ratio    : 0.800\n"),
        ("****************************************\n"
         "Report : utilization\n"
         "Design : chip\n"
         "Version: T-2022.03\n"
         "****************************************\n"
         "Core Area            : 14452.000\n"
         "Std Cell Area        :  8041.600\n"
         "Macro Area           :   630.000\n"
         "Utilization Ratio    : 0.600\n"),
    ],
    # BE-3: utilization 越高 congestion 越差 (0.70->3, 0.80->6, 0.60->1)
    "report_congestion": [
        ("****************************************\n"
         "Report : congestion\n"
         "Design : chip\n"
         "****************************************\n"
         "Both Dirs: Overflow = 18  Max = 6 (4 GRCs)  GRCs = 9 (0.06%)\n"
         "H Routing: Overflow = 7   Max = 3 (2 GRCs)\n"
         "V Routing: Overflow = 11  Max = 6 (2 GRCs)\n"
         "Congestion Hotspots: 3\n"),
        ("****************************************\n"
         "Report : congestion\n"
         "Design : chip\n"
         "****************************************\n"
         "Both Dirs: Overflow = 41  Max = 9 (8 GRCs)  GRCs = 22 (0.15%)\n"
         "H Routing: Overflow = 17  Max = 5 (4 GRCs)\n"
         "V Routing: Overflow = 24  Max = 9 (6 GRCs)\n"
         "Congestion Hotspots: 6\n"),
        ("****************************************\n"
         "Report : congestion\n"
         "Design : chip\n"
         "****************************************\n"
         "Both Dirs: Overflow = 4   Max = 2 (1 GRCs)  GRCs = 2 (0.01%)\n"
         "H Routing: Overflow = 2   Max = 1 (1 GRCs)\n"
         "V Routing: Overflow = 2   Max = 2 (1 GRCs)\n"
         "Congestion Hotspots: 1\n"),
    ],
    # BE-4 route-drc-convergence: short->reroute / spacing->widen /
    # antenna->via-pillar 逐轮修复, DRC 总数 5 -> 3 -> 1 -> 0
    "report_design": [
        ("****************************************\n"
         "Report : design -drc\n"
         "Design : chip\n"
         "****************************************\n"
         "DRC Summary (route):\n"
         "  Short violations      : 2\n"
         "  Spacing violations    : 3\n"
         "  Antenna violations    : 0\n"
         "  Total DRC violations  : 5\n"),
        ("****************************************\n"
         "Report : design -drc\n"
         "Design : chip\n"
         "****************************************\n"
         "DRC Summary (route):\n"
         "  Short violations      : 0\n"
         "  Spacing violations    : 2\n"
         "  Antenna violations    : 1\n"
         "  Total DRC violations  : 3\n"),
        ("****************************************\n"
         "Report : design -drc\n"
         "Design : chip\n"
         "****************************************\n"
         "DRC Summary (route):\n"
         "  Short violations      : 0\n"
         "  Spacing violations    : 0\n"
         "  Antenna violations    : 1\n"
         "  Total DRC violations  : 1\n"),
        ("****************************************\n"
         "Report : design -drc\n"
         "Design : chip\n"
         "****************************************\n"
         "DRC Summary (route):\n"
         "  Short violations      : 0\n"
         "  Spacing violations    : 0\n"
         "  Antenna violations    : 0\n"
         "  Total DRC violations  : 0\n"),
    ],
}


# ---------------------------------------------------------- BE-2 报告解析 (纯函数)
# 输入报告文本 -> 输出结构化 metrics dict; 解析不到的字段为 None

def parse_utilization_report(report: str) -> Dict[str, Optional[float]]:
    """report_utilization: 提取 core utilization ratio (0~1 小数)。"""
    m = re.search(r"utilization\s+ratio\s*[:=]?\s*([0-9]*\.?[0-9]+)",
                  report, re.IGNORECASE)
    return {"utilization": float(m.group(1)) if m else None}


def parse_congestion_report(report: str) -> Dict[str, Optional[int]]:
    """report_congestion: 提取 hotspot 数与最大 overflow (Both Dirs 行的 Max)。"""
    hot_m = re.search(r"hot\s*spots?[^0-9]*(\d+)", report, re.IGNORECASE)
    ovf_m = re.search(r"both\s+dirs[^\n]*?max\s*=\s*(\d+)", report, re.IGNORECASE)
    if ovf_m is None:
        ovf_m = re.search(r"max\s+overflow[^0-9]*(\d+)", report, re.IGNORECASE)
    return {"hotspot_count": int(hot_m.group(1)) if hot_m else None,
            "overflow_max": int(ovf_m.group(1)) if ovf_m else None}


def parse_clock_qor_report(report: str) -> Dict[str, Any]:
    """report_clock_qor -type summary: global skew / max latency / clock power。

    skew 与 latency_max 单位 ns; clock_power 单位随工具报告 (mW)。
    """
    skew_m = re.search(r"skew[^0-9\-]*(-?\d+\.?\d*)", report, re.IGNORECASE)
    lat_m = re.search(r"(?:latency|insertion delay)[^0-9\-]*(-?\d+\.?\d*)",
                      report, re.IGNORECASE)
    power_m = re.search(r"power[^0-9\-]*(-?\d+\.?\d*)", report, re.IGNORECASE)
    return {"skew": float(skew_m.group(1)) if skew_m else None,
            "latency_max": float(lat_m.group(1)) if lat_m else None,
            "clock_power": float(power_m.group(1)) if power_m else None,
            "units": "ns"}


def parse_drc_report(report: str) -> Dict[str, Any]:
    """report_design -drc: DRC 违例总数 + top_rules [(rule, count)]。

    top_rules 取各类违例行 (<type> violations: N) 中非零项, 按数量降序;
    为结构化列表, 不进扁平 metrics (extract_metrics 只拾取数值字段)。
    """
    total_m = re.search(r"total\s+DRC\s+violations?[^0-9]*(\d+)",
                        report, re.IGNORECASE)
    rules = [(m.group(1).strip(), int(m.group(2)))
             for m in re.finditer(
                 r"(?m)^\s*([A-Za-z][\w ]*?)\s*violations?\s*:\s*(\d+)\s*$",
                 report)
             if "total" not in m.group(1).lower()]
    rules = sorted((rc for rc in rules if rc[1] > 0), key=lambda rc: -rc[1])
    return {"drc_count": int(total_m.group(1)) if total_m else None,
            "top_rules": rules}


class ICC2Adapter(ToolAdapter):
    tool_name = "icc2"

    def __init__(self) -> None:
        self._shell: Optional[TclShell] = None
        self.design: Dict[str, Any] = {}
        self.stage_history: List[Dict[str, Any]] = []

    def _icc2(self) -> TclShell:
        if self._shell is None:
            if os.environ.get("HACOO_ICC2_MOCK") == "1":
                self._shell = MockTclShell("icc2", canned=_MOCK_CANNED)
            else:
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

    # -------------------------------------------- BE-1 子阶段方法 (T2 封装)
    # 与 Innovus adapter 的 6 个子阶段方法签名一致; ICC2/Innovus 命令差异
    # 仅封装在各方法体内。命令语义已查证:
    #   initialize_floorplan / create_placement -floorplan / place_opt /
    #   clock_opt / report_clock_qor / route_opt 均为 ICC2 真实命令
    #   (见 ICC2 Design Planning UG 与 RM flow 脚本)。

    def _run_substage(self, stage: str, commands: List[str],
                      extra_tcl: str = "",
                      report_command: Optional[str] = None,
                      metric_parser: Any = None) -> Dict[str, Any]:
        """子阶段公共执行骨架: extra_tcl 前置注入 -> 依次执行 -> 记录历史。

        BE-2: report_command 在子阶段命令后追加执行, metric_parser (纯函数,
        报告文本 -> metrics dict) 的解析结果并入 data, 使阶段指标经网关
        观测钩子进入观测层 (扁平数值字段被 extract_metrics 提取)。
        """
        self._require_design()
        icc2 = self._icc2()
        t0 = time.time()
        executed: List[str] = []
        if extra_tcl:
            icc2.cmd(extra_tcl, timeout=1800)
            executed.append(extra_tcl)
        for command in commands:
            out = icc2.cmd(command, timeout=7200)
            executed.append(command)
            if "Error:" in out:
                raise RuntimeError("%s failed (%s):\n%s" % (stage, command, out[-1500:]))
        metrics: Dict[str, Any] = {}
        if report_command:
            report = icc2.cmd(report_command, timeout=600)
            executed.append(report_command)
            metrics = metric_parser(report) if metric_parser else {}
        record = {"stage": stage, "elapsed_s": round(time.time() - t0, 1)}
        self.stage_history.append(record)
        data = dict(record, commands=executed,
                    stages_done=len(self.stage_history))
        data.update(metrics)
        return {"data": data,
                "text": "%s done in %.1fs: %s" % (stage, record["elapsed_s"],
                                                  "; ".join(executed))}

    @api_method(capability="write",
                description="Create floorplan: core utilization / aspect ratio / margins.")
    def api_fp_create(self, utilization: float = 0.7, aspect_ratio: float = 1.0,
                      core_margin: float = 2.0, extra_tcl: str = "") -> Dict[str, Any]:
        """initialize_floorplan 建 core 区域 (非 legacy ICC 的 create_floorplan);
        长宽比经 -side_ratio {1 r} 指定 (矩形 core)。"""
        command = ("initialize_floorplan -core_utilization %.3f -core_offset {%.2f}"
                   % (utilization, core_margin))
        if abs(aspect_ratio - 1.0) > 1e-6:
            command += " -side_ratio {1 %.3f}" % aspect_ratio
        # BE-2: 建 core 后 report_utilization 提取实际 utilization
        return self._run_substage("fp_create", [command], extra_tcl,
                                  report_command="report_utilization",
                                  metric_parser=parse_utilization_report)

    @api_method(capability="write",
                description="Place hard macros only (floorplan-stage macro placement).")
    def api_fp_place_macros(self, extra_tcl: str = "") -> Dict[str, Any]:
        """create_placement -floorplan 做 macro 摆放; plan.macro.macro_place_only
        app option 使命令在 macro 定位后即停, 不摆标准单元。"""
        return self._run_substage("fp_place_macros", [
            "set_app_options -name plan.macro.macro_place_only -value true",
            "create_placement -floorplan",
        ], extra_tcl)

    @api_method(capability="write", description="Placement + pre-CTS optimization (place_opt).")
    def api_place_opt(self, extra_tcl: str = "") -> Dict[str, Any]:
        """place_opt: 全局/细节布局 + pre-CTS 时序优化一条龙;
        BE-2: 完成后 report_congestion 提取 congestion hotspot 数。"""
        return self._run_substage("place_opt", ["place_opt"], extra_tcl,
                                  report_command="report_congestion",
                                  metric_parser=parse_congestion_report)

    @api_method(capability="write", description="Build clock trees (CTS) with target skew.")
    def api_cts_build(self, target_skew_ps: float = 50.0,
                      extra_tcl: str = "") -> Dict[str, Any]:
        """set_clock_tree_options -target_skew (ICC2 单位为 ns) +
        clock_opt -from build_clock -to route_clock (CTS 建树段)。"""
        return self._run_substage("cts_build", [
            "set_clock_tree_options -target_skew %.3f" % (target_skew_ps / 1000.0),
            "clock_opt -from build_clock -to route_clock",
        ], extra_tcl)

    @api_method(capability="read", description="Report clock tree QoR: skew / latency_max / clock_power.")
    def api_cts_report(self) -> Dict[str, Any]:
        """report_clock_qor -type summary: 解析 global skew / max latency /
        clock tree power (ICC2 报告原生单位 ns)。"""
        self._require_design()
        icc2 = self._icc2()
        report = icc2.cmd("report_clock_qor -type summary", timeout=600)
        data = dict(parse_clock_qor_report(report),
                    commands=["report_clock_qor -type summary"])
        return {"data": data, "text": report[-3000:]}

    @api_method(capability="read", description="Report routing congestion: hotspot_count / overflow_max.")
    def api_report_congestion(self) -> Dict[str, Any]:
        """report_congestion: BE-2 增强独立报告方法, 解析 hotspot 数与
        最大 overflow (命令已查证: ICC2 report_congestion, 见 Design Planning UG)。"""
        self._require_design()
        icc2 = self._icc2()
        report = icc2.cmd("report_congestion", timeout=600)
        data = dict(parse_congestion_report(report),
                    commands=["report_congestion"])
        return {"data": data, "text": report[-3000:]}

    @api_method(capability="read", description="Report route DRC: drc_count / top_rules.")
    def api_route_drc_report(self) -> Dict[str, Any]:
        """report_design -drc: BE-2 增强独立报告方法, 解析 route DRC 违例
        总数与按数量降序的 top_rules [(rule, count)] (top_rules 为结构化
        列表, 不进扁平 metrics)。"""
        self._require_design()
        icc2 = self._icc2()
        report = icc2.cmd("report_design -drc", timeout=600)
        data = dict(parse_drc_report(report),
                    commands=["report_design -drc"])
        return {"data": data, "text": report[-3000:]}

    @api_method(capability="write", description="Routing + post-route optimization (route_opt).")
    def api_route_opt(self, extra_tcl: str = "") -> Dict[str, Any]:
        """route_opt: 全局/细节布线 + post-route 时序/DRC 优化;
        BE-2: 完成后 report_design -drc 提取 route DRC 违例总数。"""
        return self._run_substage("route_opt", ["route_opt"], extra_tcl,
                                  report_command="report_design -drc",
                                  metric_parser=parse_drc_report)

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
